#!/usr/bin/env python3
"""
Live End-to-End Test for the Remediator (CrewAI).
1. Starts the production Remediator A2A server on port 8006.
2. Injects the connection pool latency fault on payment-postgres proxy.
3. Generates load against /payment/charge.
4. Executes Diagnostician, which diagnoses root_cause and hands off over A2A.
5. Remediator localizes, synthesizes patch + regression test, checks fail-before / pass-after,
   runs test suite, and opens GitHub PR.
6. Reports PR URL, files changed, diff, fail-before and pass-after results.
7. Closes PR, deletes remote branch, and restores target repository to main.
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

import httpx
import uvicorn
from dotenv import load_dotenv

load_dotenv()
os.environ["SURVEYOPS_LLM_CACHE_ENABLED"] = "false"

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from surveyops.schemas import (
    InvestigatePayload,
    DiagnosisResult,
    Outcome,
    FailureMode,
    RemediationResult,
)
from surveyops.agents.remediator.server import create_remediator_app, RemediatorExecutor
from surveyops.agents.diagnostician import run_diagnostician
from surveyops.agents.remediator.workspace import resolve_target_repo_dir, delete_branch_cleanly

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_remediator_live")


def start_remediator_server_in_thread(executor: RemediatorExecutor, port: int = 8006):
    app = create_remediator_app(host="127.0.0.1", port=port, executor=executor)
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(40):
        if server.started:
            break
        time.sleep(0.1)
    return server


def generate_payment_traffic():
    """Generates synthetic requests against /payment/charge to produce pool saturation and error logs."""
    import uuid
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {
            "order_id": f"test-order-e2e-{i}",
            "amount": 99.99,
            "currency": "USD",
            "idempotency_key": f"idemp-e2e-{uuid.uuid4().hex[:12]}",
        }
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


def close_github_pr(pr_url: str) -> None:
    """Closes GitHub PR using GITHUB_TOKEN."""
    token = os.getenv("GITHUB_TOKEN")
    if not token or not pr_url:
        return

    # Extract owner, repo, pull_number: https://github.com/owner/repo/pull/123
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
            "User-Agent": "SurveyOps-Test",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Closed GitHub PR #{pr_number}: HTTP {resp.status}")
    except Exception as exc:
        logger.warning(f"Could not close PR #{pr_number}: {exc}")


async def main():
    print("=" * 70)
    print("  SURVEYOPS LIVE REMEDIATOR (CREWAI) END-TO-END TEST")
    print("=" * 70)

    # 1. Start Remediator server
    print("[1] Starting Remediator A2A server on port 8006...")
    executor = RemediatorExecutor()
    server = start_remediator_server_in_thread(executor, 8006)
    print("    -> Remediator server online on http://127.0.0.1:8006")

    # 2. Clear faults
    print("[2] Clearing active faults...")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # 3. Inject pool fault
    print("[3] Injecting connection pool latency fault on payment-postgres proxy...")
    subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        check=True,
    )

    # 4. Generate traffic
    print("[4] Generating payment load to accumulate pool exhaustion errors...")
    generate_payment_traffic()
    time.sleep(2)

    # 5. Execute Diagnostician
    print("[5] Launching Diagnostician (ADK LoopAgent)...")
    payload = InvestigatePayload(
        alert="HighP99Latency",
        service="payment",
        endpoint="/payment/charge",
        metric="http_request_duration_seconds",
        threshold=0.5,
        window_start=datetime.now(timezone.utc),
    )
    inc_id = f"inc-e2e-{int(time.time())}"

    diag_result = await run_diagnostician(
        incident=payload,
        incident_id=inc_id,
        runner_url="http://localhost:8005",
        remediator_url="http://localhost:8006",
        shared_secret=os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret",
    )

    print(f"    -> Diagnostician concluded: outcome={diag_result.outcome.value}, root_cause={diag_result.root_cause}")

    # Wait for Remediator to complete
    print("[6] Waiting for Remediator to complete remediation...")
    remediation: RemediationResult | None = executor.last_remediation
    for _ in range(60):
        if executor.last_remediation and executor.last_remediation.pr_url:
            remediation = executor.last_remediation
            break
        await asyncio.sleep(1)

    target_repo = resolve_target_repo_dir()

    if not remediation or not remediation.pr_url:
        print("[-] Remediation did not produce a PR URL!")
        # Clear faults and exit
        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
        server.should_exit = True
        sys.exit(1)

    # 7. Extract git diff and test results
    branch = remediation.branch or ""
    print("\n" + "=" * 70)
    print("  LIVE REMEDIATION RESULTS")
    print("=" * 70)
    print(f"PR URL: {remediation.pr_url}")
    print(f"Branch: {branch}")
    print(f"Files Changed: {remediation.files_changed}")
    print(f"Tests Added: {remediation.tests_added}")

    print("\n--- REGRESSION TEST RESULTS (RULE 5) ---")
    if getattr(executor, "last_pre_run", None):
        pre = executor.last_pre_run
        print(f"Fail-Before Test (Unpatched Code): {'FAILED (as required by Rule 5)' if not pre.passed else 'PASSED (UNEXPECTED)'}")
        print(f"Fail-Before Output: {pre.stdout.strip() or pre.stderr.strip()}")
    if getattr(executor, "last_post_run", None):
        post = executor.last_post_run
        print(f"Pass-After Test (Patched Code): {'PASSED (as required by Rule 5)' if post.passed else 'FAILED'}")
        print(f"Pass-After Output: {post.stdout.strip() or post.stderr.strip()}")

    diff_res = subprocess.run(
        ["git", "diff", f"origin/main..{branch}"],
        cwd=target_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    print("\n--- GIT DIFF ---")
    print(diff_res.stdout if diff_res.stdout else "(No diff output)")

    # 8. Cleanup: Close PR and delete branch
    print("\n[8] Cleaning up: closing PR and deleting branch...")
    close_github_pr(remediation.pr_url)
    if branch:
        delete_branch_cleanly(target_repo, branch, remote=True)

    # Clear faults
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    server.should_exit = True
    print("[+] Live E2E test completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
