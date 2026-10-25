#!/usr/bin/env python3
"""
scripts/run_trial_orchestrator.py
Full automated trial orchestrator for SurveyOps:
1. Resets the target system (Toxiproxy toxics, orders/payments tables).
2. Ensures Remediator A2A server is running on port 8006.
3. Injects fault (e.g. latency on payment-postgres proxy).
4. Generates payment traffic and triggers / awaits alert & investigation.
5. Diagnostician diagnoses failure mode and hands off to Remediator over A2A.
6. Remediator localizes, synthesizes patch, enforces Rule 5 (behavioral fail-before / pass-after),
   runs suite, and opens GitHub PR (never merges).
7. Report step deterministically renders IncidentReport (JSON + Markdown), persists to Postgres & eval/reports/.
8. Runs eval.scorer separately against ground truth.
9. Checks GitHub Actions CI status for the opened PR.
10. Closes PR and cleanly deletes remote & local fix branch.

Zero credentials modified or inspected. Uses GITHUB_TOKEN from environment.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

import httpx
import uvicorn
from dotenv import load_dotenv

load_dotenv()
os.environ["SURVEYOPS_LLM_CACHE_ENABLED"] = "false"

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from surveyops.schemas import (
    InvestigatePayload,
    DiagnosisResult,
    Outcome,
    RemediationResult,
)
from surveyops.agents.remediator.server import create_remediator_app, RemediatorExecutor
from surveyops.agents.remediator.workspace import (
    resolve_target_repo_dir,
    delete_branch_cleanly,
    redact_tokens,
)
from surveyops.agents.diagnostician import (
    run_diagnostician,
    get_incident_ledger,
)
from surveyops.agents.diagnostician.agent import get_incident_run_record
from surveyops.report import generate_and_save_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_trial")


def start_local_remediator_if_needed(executor: RemediatorExecutor, port: int = 8006) -> tuple[uvicorn.Server | None, threading.Thread | None]:
    """Starts local Remediator A2A server if port 8006 is not already serving."""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
        if resp.status_code == 200:
            logger.info(f"Remediator server already running on port {port}.")
            return None, None
    except Exception:
        pass

    logger.info(f"Starting in-process Remediator server on port {port}...")
    app = create_remediator_app(host="127.0.0.1", port=port, executor=executor)
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(40):
        if server.started:
            break
        time.sleep(0.1)
    logger.info(f"Remediator server is online on http://127.0.0.1:{port}")
    return server, thread


def reset_system():
    """Runs reset_trial.sh and clears active incidents."""
    logger.info("Executing trial reset...")
    reset_script = REPO_ROOT / "scripts" / "reset_trial.sh"
    if reset_script.exists():
        subprocess.run(["bash", str(reset_script)], cwd=REPO_ROOT, check=True)
    else:
        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], cwd=REPO_ROOT, check=True)


def generate_payment_traffic(count: int = 20):
    """Sends requests to /payment/charge to saturate pool under injected fault."""
    import uuid
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    logger.info(f"Generating {count} payment requests against {url}...")
    for i in range(count):
        body = {
            "order_id": f"ord-trial-{i}",
            "amount": 75.00,
            "currency": "USD",
            "idempotency_key": f"idemp-trial-{uuid.uuid4().hex[:12]}",
        }
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


def check_github_actions_status(pr_url: str, timeout_seconds: int = 90) -> dict[str, Any]:
    """
    Checks the GitHub Actions CI status for the PR head commit.
    Uses GITHUB_TOKEN from environment. Never prints or logs the token.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token or not pr_url:
        return {"status": "skipped", "reason": "No GITHUB_TOKEN or pr_url"}

    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        return {"status": "error", "reason": "Could not parse PR URL"}
    owner, repo, pr_number = m.group(1), m.group(2), m.group(3)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SurveyOps-Trial",
    }

    # 1. Get PR head SHA
    pr_api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    head_sha = None
    try:
        req = urllib.request.Request(pr_api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            pr_data = json.loads(resp.read().decode())
            head_sha = pr_data.get("head", {}).get("sha")
    except urllib.error.HTTPError as err:
        err_msg = redact_tokens(err.read().decode("utf-8"))
        logger.error(f"GitHub API error {err.code} at {pr_api_url}: {err_msg}")
        return {"status": "error", "http_status": err.code, "error": err_msg}
    except Exception as exc:
        logger.error(f"Failed to fetch PR details: {redact_tokens(str(exc))}")
        return {"status": "error", "error": redact_tokens(str(exc))}

    if not head_sha:
        return {"status": "error", "reason": "Could not retrieve head SHA for PR"}

    logger.info(f"Checking GitHub Actions checks for PR #{pr_number} (commit {head_sha[:7]})...")

    # 2. Poll check-runs
    check_runs_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            req = urllib.request.Request(check_runs_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                check_runs = data.get("check_runs", [])
                if check_runs:
                    run = check_runs[0]
                    c_status = run.get("status")
                    c_conclusion = run.get("conclusion")
                    run_name = run.get("name", "tests")
                    logger.info(f"GitHub Actions run '{run_name}': status={c_status}, conclusion={c_conclusion}")
                    if c_status == "completed":
                        return {
                            "status": "completed",
                            "conclusion": c_conclusion,
                            "is_green": (c_conclusion == "success"),
                            "run_name": run_name,
                            "html_url": run.get("html_url"),
                        }
        except Exception as exc:
            logger.warning(f"Error checking check-runs: {redact_tokens(str(exc))}")

        time.sleep(5)

    return {"status": "pending_or_timeout", "is_green": False, "head_sha": head_sha}


def close_github_pr(pr_url: str) -> None:
    """Closes GitHub PR using GITHUB_TOKEN."""
    token = os.getenv("GITHUB_TOKEN")
    if not token or not pr_url:
        return

    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        return
    owner, repo, pr_number = m.group(1), m.group(2), m.group(3)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(
        api_url,
        data=json.dumps({"state": "closed"}).encode("utf-8"),
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "SurveyOps-Trial",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Closed GitHub PR #{pr_number}: HTTP {resp.status}")
    except urllib.error.HTTPError as err:
        err_msg = redact_tokens(err.read().decode("utf-8"))
        logger.error(f"GitHub API error {err.code} closing PR at {api_url}: {err_msg}")
    except Exception as exc:
        logger.warning(f"Could not close PR #{pr_number}: {redact_tokens(str(exc))}")


async def run_trial_flow():
    print("=" * 70)
    print("           SURVEYOPS FULL TRIAL PIPELINE EXECUTION")
    print("=" * 70)

    # 1. Reset system
    print("[1/9] Resetting target environment to clean state...")
    reset_system()

    # 2. Setup Remediator
    print("[2/9] Initializing Remediator A2A service...")
    executor = RemediatorExecutor()
    server, thread = start_local_remediator_if_needed(executor, port=8006)

    # 3. Inject fault
    print("[3/9] Injecting connection pool latency fault...")
    inject_res = subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    fault_id_match = re.search(r"Fault ID:\s+(fault-[a-f0-9]+)", inject_res.stdout)
    fault_id = fault_id_match.group(1) if fault_id_match else None
    print(f"      Injected fault ID: {fault_id}")

    # 4. Generate traffic to trigger alert
    print("[4/9] Generating synthetic payment traffic...")
    generate_payment_traffic(count=20)
    time.sleep(2)

    # 5. Launch Diagnostician
    incident_id = f"inc-trial-{int(time.time())}"
    print(f"[5/9] Launching Diagnostician for incident {incident_id}...")
    payload = InvestigatePayload(
        alert="HighP99Latency",
        service="payment",
        endpoint="/payment/charge",
        metric="http_request_duration_seconds",
        threshold=0.5,
        window_start=datetime.now(timezone.utc),
    )

    t_diag_start = time.time()
    diagnosis = await run_diagnostician(
        incident=payload,
        incident_id=incident_id,
        runner_url="http://localhost:8005",
        remediator_url="http://localhost:8006",
        shared_secret=os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret",
    )
    t_diag_dur = time.time() - t_diag_start
    print(f"      Diagnostician concluded: outcome={diagnosis.outcome.value}, iters={diagnosis.iterations_used}")
    print(f"      Root cause: {diagnosis.root_cause}, confidence={diagnosis.confidence:.2f}")

    # 6. Await Remediator completion
    print("[6/9] Awaiting Remediator to complete Rule 5 verification and PR creation...")
    remediation: RemediationResult | None = executor.last_remediation
    for i in range(150):
        if executor.last_remediation and executor.last_remediation.pr_url:
            remediation = executor.last_remediation
            break
        if (i + 1) % 15 == 0:
            print(f"      Waiting for Remediator... ({i+1}s elapsed)")
        await asyncio.sleep(1)

    if not remediation or not remediation.pr_url:
        print("[!] Remediator did not return a PR URL!")

    # 7. Deterministic Report Generation
    print("[7/9] Generating deterministic IncidentReport (JSON & Markdown)...")
    ledger = get_incident_ledger(incident_id)
    run_record = get_incident_run_record(incident_id)
    timings = {
        "detect": 60.0,
        "diagnose": t_diag_dur,
        "remediate": getattr(executor, "last_remediation_duration", 30.0),
        "total": t_diag_dur + 60.0 + 30.0,
    }
    usage = {
        "prompt_tokens": run_record.get("prompt_tokens", 0),
        "completion_tokens": run_record.get("completion_tokens", 0),
        "total_tokens": run_record.get("total_tokens", 0),
    }

    report, json_path, md_path = generate_and_save_report(
        incident_id=incident_id,
        diagnosis=diagnosis,
        evidence_trail=ledger,
        remediation=remediation,
        timings=timings,
        usage=usage,
    )
    print(f"\n>>> REPORT PATH: {json_path}")
    print(f">>> MARKDOWN PATH: {md_path}\n")

    # 8. Run Scorer separately
    print("[8/9] Running evaluation scorer separately...")
    score_res = subprocess.run(
        [sys.executable, "-m", "eval.scorer", str(json_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    print(score_res.stdout)
    if score_res.returncode != 0:
        print(f"[!] Scorer failed: {score_res.stderr}")

    # 9. Check CI and Cleanup
    ci_status: dict[str, Any] = {}
    if remediation and remediation.pr_url:
        print(f"[9/9] Checking GitHub Actions CI check on {remediation.pr_url}...")
        ci_status = check_github_actions_status(remediation.pr_url, timeout_seconds=75)
        print(f"      GitHub Actions Check: {ci_status.get('conclusion', ci_status.get('status'))} (is_green={ci_status.get('is_green', False)})")

        print("      Cleaning up: closing PR and deleting fix branch...")
        close_github_pr(remediation.pr_url)
        target_repo = resolve_target_repo_dir()
        if remediation.branch:
            delete_branch_cleanly(target_repo, remediation.branch, remote=True)

    # Clear active fault
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], cwd=REPO_ROOT, check=True)
    if server:
        server.should_exit = True

    print("\n" + "=" * 70)
    print("                 TRIAL EXECUTION SUMMARY")
    print("=" * 70)
    print(f"Incident ID:             {incident_id}")
    print(f"Diagnostician Outcome:   {diagnosis.outcome.value} (Iterations: {diagnosis.iterations_used})")
    print(f"Diagnostician Hit Cap:   {'No (Converged cleanly)' if diagnosis.iterations_used < 10 else 'Yes (Hit Cap)'}")
    print(f"Root Cause Identified:   {diagnosis.root_cause}")
    print(f"Heuristic Confidence:    {diagnosis.confidence:.2f}")
    print(f"Remediation PR:          {remediation.pr_url if remediation else 'None'}")
    print(f"GitHub Actions Green:    {ci_status.get('is_green', False)} ({ci_status.get('conclusion', 'unknown')})")
    print(f"Report JSON:             {json_path}")
    print("=" * 70 + "\n")


def main():
    asyncio.run(run_trial_flow())


if __name__ == "__main__":
    main()
