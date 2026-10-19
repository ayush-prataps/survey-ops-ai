#!/usr/bin/env python3
"""
Live Test Script for Diagnostician (Step 6).
Executes live evaluation against the target system under an active connection pool fault:
1. Starts the Remediator A2A stub on port 8006 in a background thread.
2. Injects latency toxic on payment->postgres proxy (triggering connection pool exhaustion).
3. Drives requests against /payment/charge so metrics and pool exhaustion errors accumulate.
4. Runs the Diagnostician 5 times to measure variance (outcome, iterations, tokens, wall time).
5. Compares final DiagnosisResult with ground truth AFTER runs complete.
6. Cleans up faults.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
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
)
from surveyops.agents.remediator.stub_server import app as remediator_app
from surveyops.agents.diagnostician import run_diagnostician, get_incident_ledger
from surveyops.agents.diagnostician.callbacks import token_tracker


def start_stub_remediator_in_thread(port: int = 8006):
    config = uvicorn.Config(app=remediator_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(30):
        if server.started:
            break
        time.sleep(0.1)
    return server


def generate_payment_traffic():
    """Generates synthetic requests against /payment/charge to produce pool saturation and error logs."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    body = {"order_id": "test-order-live", "amount": 99.99, "currency": "USD"}
    for i in range(15):
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5, help="Number of runs")
    args, _ = parser.parse_known_args()
    num_runs = args.runs

    print("=================================================================")
    print(f"  SURVEYOPS LIVE DIAGNOSTICIAN EVALUATION ({num_runs} RUNS)      ")
    print("=================================================================")

    # 1. Start Remediator A2A stub
    print("[1] Starting Remediator A2A stub on port 8006...")
    server = start_stub_remediator_in_thread(8006)
    print("    -> Remediator A2A stub online.")

    # 2. Clear faults first
    print("[2] Clearing any existing faults...")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # 3. Inject pool exhaustion fault
    print("[3] Injecting connection pool latency fault on payment-postgres proxy...")
    res = subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    fault_id = None
    for line in res.stdout.splitlines():
        if "Fault ID:" in line:
            fault_id = line.split("Fault ID:")[-1].strip()
    print(f"    -> Injected fault ID: {fault_id}")

    # 4. Generate traffic to trigger the fault and populate logs/gauges
    print("[4] Generating synthetic traffic to trigger pool saturation...")
    generate_payment_traffic()
    time.sleep(2.0)

    # 5. Execute Diagnostician runs
    results = []
    sample_ledger = None
    sample_diagnosis = None

    payload = InvestigatePayload(
        alert="HighP99Latency",
        service="payment",
        endpoint="/payment/charge",
        metric="http_request_duration_seconds",
        threshold=0.5,
        window_start=datetime.now(timezone.utc),
    )

    print(f"\n[5] Executing {num_runs} Diagnostician run(s)...")
    for run_idx in range(1, num_runs + 1):
        inc_id = f"inc-live-{run_idx}-{fault_id[:6] if fault_id else '000000'}"
        start_time = time.perf_counter()

        diagnosis: DiagnosisResult = await run_diagnostician(
            incident=payload,
            incident_id=inc_id,
            runner_url="http://localhost:8005",
            remediator_url="http://localhost:8006",
            shared_secret=os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret",
        )
        wall_time = round(time.perf_counter() - start_time, 2)
        usage = token_tracker.get_usage(inc_id)

        ledger_entries = get_incident_ledger(inc_id)

        results.append({
            "run": run_idx,
            "incident_id": inc_id,
            "outcome": diagnosis.outcome.value,
            "root_cause": diagnosis.root_cause.value if diagnosis.root_cause else None,
            "confidence": diagnosis.confidence,
            "self_reported_confidence": diagnosis.self_reported_confidence,
            "iterations": diagnosis.iterations_used,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "served_models": usage.get("served_models", []),
            "wall_time_s": wall_time,
            "ledger_count": len(ledger_entries),
        })
        print(f"  Run {run_idx}: Outcome={diagnosis.outcome.value}, RootCause={diagnosis.root_cause}, HeuristicScore={diagnosis.confidence}, SelfReportedConf={diagnosis.self_reported_confidence}, Iterations={diagnosis.iterations_used}, Tokens={usage['total_tokens']}, Time={wall_time}s")

        if run_idx == 1:
            sample_diagnosis = diagnosis
            sample_ledger = ledger_entries

        if run_idx < num_runs:
            print("    [Pacing] Waiting 10s before next run...")
            await asyncio.sleep(10.0)

    # 6. Clean up fault
    print("\n[6] Clearing faults after evaluation...")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # 7. Ground truth comparison (Strict invariant: read AFTER runs)
    print("\n[7] Ground truth comparison (eval/ground_truth/ read after runs):")
    gt_file = Path("eval/ground_truth") / f"{fault_id}.json"
    ground_truth = json.loads(gt_file.read_text()) if gt_file.exists() else {}
    gt_cause = ground_truth.get("failure_mode")

    print(f"  Ground Truth Root Cause: {gt_cause}")
    for r in results:
        match = (r["root_cause"] == gt_cause)
        print(f"  Run {r['run']} match: {match} (Diagnosed: {r['root_cause']})")

    # Save summary report for inspection
    summary_path = Path("eval") / "diagnostician_live_run_summary.json"
    summary_path.write_text(json.dumps({
        "fault_id": fault_id,
        "ground_truth": ground_truth,
        "sample_diagnosis": sample_diagnosis.model_dump(mode="json") if sample_diagnosis else None,
        "sample_ledger": [entry.model_dump() for entry in (sample_ledger or [])],
        "runs": results,
    }, indent=2))
    print(f"\n[8] Full evaluation summary saved to {summary_path}")

    print("\n--- SAMPLE LEDGER (Run 1 Trimmed) ---")
    for entry in (sample_ledger or []):
        print(f"Iter {entry.iteration} [{entry.hypothesis_id}] conf={entry.confidence:.2f} status={entry.status}")
        print(f"  + for: {entry.evidence_for}")
        print(f"  - against: {entry.evidence_against}")


if __name__ == "__main__":
    asyncio.run(main())
