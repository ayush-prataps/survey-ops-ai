"""
Live Demo Full-Chain Runner:
Executes the complete live chain:
1. Reset via demo UI (/demo/reset)
2. Inject fault via demo UI (/demo/inject)
3. Wait for Prometheus alert firing
4. Wait for /investigate webhook to arrive
5. Track Diagnostician startup, first experiment, and outcome
6. Track A2A to Remediator, PR creation, and deterministic report
7. Clean up PR and branch
8. Report exact timestamps and outcome
"""

import asyncio
import datetime
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import httpx
import psycopg2
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("live_demo_chain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEMO_UI_URL = "http://localhost:8050"
PROMETHEUS_URL = "http://localhost:9090"
DB_CONFIG = {"host": "localhost", "port": 5434, "user": "postgres", "password": "postgres", "dbname": "surveyops"}


def close_github_pr_and_branch(pr_url: str) -> None:
    token = os.getenv("GITHUB_TOKEN")
    if not token or not pr_url:
        return
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        return
    owner, repo, pr_number = m.group(1), m.group(2), m.group(3)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    branch_name = None
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "SurveyOps-Demo",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            branch_name = data.get("head", {}).get("ref")
    except Exception as exc:
        logger.warning(f"Could not fetch PR details #{pr_number}: {exc}")

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps({"state": "closed"}).encode("utf-8"),
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "SurveyOps-Demo",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Closed GitHub PR #{pr_number}: HTTP {resp.status}")
    except Exception as exc:
        logger.warning(f"Could not close PR #{pr_number}: {exc}")

    if branch_name and branch_name not in ("main", "master"):
        del_url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
        try:
            req = urllib.request.Request(
                del_url,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "SurveyOps-Demo",
                },
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Deleted remote branch {branch_name}: HTTP {resp.status}")
        except Exception as exc:
            logger.warning(f"Could not delete branch {branch_name}: {exc}")


async def run_single_chain(run_number: int) -> dict:
    print(f"\n{'='*70}\nSTARTING LIVE CHAIN RUN {run_number}\n{'='*70}")
    timings = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Reset
        print("[1] Resetting system via /demo/reset...")
        r = await client.post(f"{DEMO_UI_URL}/demo/reset", json={})
        timings["reset_called"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(f"    Reset response: {r.status_code}")

        # Wait until Prometheus alerts clear completely to avoid alert suppression
        print("    Waiting for Prometheus alerts to clear...")
        t_wait_clear = time.time()
        while time.time() - t_wait_clear < 45:
            try:
                resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
                firing = [a for a in resp.json().get("data", {}).get("alerts", []) if a.get("state") == "firing"]
                if not firing:
                    print("    Prometheus alerts cleared cleanly.")
                    break
            except Exception:
                pass
            await asyncio.sleep(2)
        await asyncio.sleep(2)

        # 2. Inject
        print("[2] Injecting fault via /demo/inject...")
        t_inject = time.time()
        r = await client.post(f"{DEMO_UI_URL}/demo/inject", json={})
        timings["inject_called"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(f"    Inject response: {r.status_code} ({r.text})")

        # 3. Wait for alert firing in Prometheus
        print("[3] Waiting for Prometheus alert to fire...")
        alert_fired = False
        while time.time() - t_inject < 90:
            try:
                resp = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
                data = resp.json().get("data", {}).get("alerts", [])
                for a in data:
                    if a.get("state") == "firing":
                        timings["alert_firing"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        print(f"    Alert firing at {timings['alert_firing']} (labels: {a.get('labels')})")
                        alert_fired = True
                        break
                if alert_fired:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        if not alert_fired:
            timings["error"] = "Alert did not fire within 90s"
            return timings

        # 4. Wait for /investigate webhook and incident opening
        print("[4] Waiting for Alertmanager /investigate webhook...")
        incident_id = None
        t_wait_inv = time.time()
        while time.time() - t_wait_inv < 90:
            try:
                conn = psycopg2.connect(**DB_CONFIG)
                cur = conn.cursor()
                cur.execute("SELECT incident_id, ts, payload FROM events WHERE event_type='incident_opened' ORDER BY id DESC LIMIT 1;")
                row = cur.fetchone()
                conn.close()
                if row:
                    incident_id = row[0]
                    timings["investigate_received"] = row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1])
                    print(f"    /investigate received at {timings['investigate_received']} (incident: {incident_id})")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        if not incident_id:
            timings["error"] = "/investigate did not open incident within 90s"
            return timings

        timings["incident_id"] = incident_id

        # 5. Monitor events table for Diagnostician start, first experiment, outcome, A2A, PR, report
        print(f"[5] Monitoring full investigation loop for {incident_id}...")
        t_start_loop = time.time()
        pr_url = None
        completed = False

        while time.time() - t_start_loop < 300:
            try:
                conn = psycopg2.connect(**DB_CONFIG)
                cur = conn.cursor()
                cur.execute("SELECT ts, component, event_type, payload FROM events WHERE incident_id=%s ORDER BY id ASC;", (incident_id,))
                events = cur.fetchall()
                conn.close()

                for ts, comp, ev_type, p in events:
                    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    payload = p or {}

                    if ev_type == "before_agent" and "diagnostician_start" not in timings:
                        timings["diagnostician_start"] = ts_str
                        print(f"    Diagnostician started at {ts_str} (agent: {payload.get('agent_name')})")

                    if ev_type in ("observation_received", "after_tool") and "first_experiment" not in timings:
                        timings["first_experiment"] = ts_str
                        print(f"    First experiment observation received at {ts_str}")

                    if ev_type == "investigation_completed" and "outcome" not in timings:
                        timings["outcome"] = payload.get("outcome", "unknown")
                        timings["outcome_timestamp"] = ts_str
                        timings["root_cause"] = payload.get("root_cause")
                        timings["confidence"] = payload.get("confidence")
                        timings["iterations_used"] = payload.get("iterations_used")
                        print(f"    Diagnostician concluded at {ts_str}: {timings['outcome']} ({timings.get('root_cause')})")

                    if ev_type == "investigation_failed" and "outcome" not in timings:
                        timings["outcome"] = "failed"
                        timings["outcome_timestamp"] = ts_str
                        timings["error"] = payload.get("error_reason") or payload.get("error")
                        print(f"    Diagnostician failed at {ts_str}: {timings['error']}")
                        completed = True

                    if ev_type in ("a2a_to_remediator", "remediation_started", "patch_applied") and "a2a_to_remediator" not in timings:
                        timings["a2a_to_remediator"] = ts_str
                        print(f"    A2A to Remediator at {ts_str}")

                    if ev_type == "pr_opened" and "pr_url" not in timings:
                        pr_url = payload.get("pr_url")
                        timings["pr_opened"] = ts_str
                        timings["pr_url"] = pr_url
                        print(f"    Pull Request opened at {ts_str}: {pr_url}")

                    if ev_type == "report_generated" and "report_generated" not in timings:
                        timings["report_generated"] = ts_str
                        timings["report_path"] = f"eval/reports/{incident_id}.json"
                        print(f"    Report generated at {ts_str}")
                        completed = True

                if completed or ("outcome" in timings and timings["outcome"] in ("abstained", "budget_exceeded")):
                    break
            except Exception as e:
                logger.warning(f"Error querying events: {e}")

            await asyncio.sleep(2)

        # 6. Cleanup PR if opened
        if pr_url:
            print(f"[6] Cleaning up PR {pr_url} and branch...")
            close_github_pr_and_branch(pr_url)

        # 7. Reset system
        await client.post(f"{DEMO_UI_URL}/demo/reset", json={})
        return timings


async def main():
    results = []
    for i in range(1, 4):
        res = await run_single_chain(i)
        results.append(res)
        print(f"\nRUN {i} SUMMARY:")
        print(json.dumps(res, indent=2))
        await asyncio.sleep(5)

    print("\n" + "="*70)
    print("ALL 3 RUNS COMPLETED")
    print("="*70)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
