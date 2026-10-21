#!/usr/bin/env python3
"""
Validation Script for Diagnostician.
Runs:
- Control A: Healthy system (5 runs). Expected: no root_cause.
- Control B: Upstream 5xx fault with pool unsaturated (5 runs). Ground truth: FailureMode.upstream_dependency_failure.
- Batch Pool Fault: Repeat pool fault (5 runs) and report combined runs.
Measures: outcomes, root_cause, heuristic score, tokens per run, wall time, confident-wrong count.
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


def generate_normal_payment_traffic():
    """Generates a few normal 200 OK requests."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(3):
        body = {"order_id": f"norm-{i}-{int(time.time())}", "amount": 25.0, "idempotency_key": f"key-norm-{i}-{int(time.time())}"}
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.0)
        except Exception:
            pass


def generate_fault_5xx_traffic():
    """Generates requests against 5xx-injected endpoint."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(10):
        body = {"order_id": f"f5xx-{i}-{int(time.time())}", "amount": 50.0, "idempotency_key": f"key-5xx-{i}-{int(time.time())}"}
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.0)
        except Exception:
            pass


def generate_pool_latency_traffic():
    """Generates requests to saturate the connection pool under proxy latency."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {"order_id": f"pool-{i}-{int(time.time())}", "amount": 99.99, "idempotency_key": f"key-pool-{i}-{int(time.time())}"}
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


async def run_single_eval(
    label: str,
    run_idx: int,
    payload: InvestigatePayload,
) -> dict:
    inc_id = f"inc-{label.lower()}-{run_idx}-{int(time.time())}"
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

    res = {
        "batch": label,
        "run": run_idx,
        "incident_id": inc_id,
        "outcome": diagnosis.outcome.value,
        "root_cause": diagnosis.root_cause.value if diagnosis.root_cause else None,
        "heuristic_score": diagnosis.confidence,
        "self_reported_confidence": diagnosis.self_reported_confidence,
        "iterations": diagnosis.iterations_used,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "served_models": usage.get("served_models", []),
        "wall_time_s": wall_time,
        "ledger_count": len(ledger_entries),
    }
    print(f"[{label} Run {run_idx}] Outcome={diagnosis.outcome.value} | RootCause={res['root_cause']} | "
          f"HeuristicScore={diagnosis.confidence} | Tokens={usage['total_tokens']} | Time={wall_time}s")
    return res


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", choices=["control_a", "control_b", "pool_fault", "all"], default="all")
    args = parser.parse_args()

    print("=================================================================")
    print("      DIAGNOSTICIAN VALIDATION SUITE (CONTROLS & POOL FAULT)     ")
    print("=================================================================")

    start_stub_remediator_in_thread(8006)
    all_results = []

    # ──────────────────────────────────────────────────────────────────────────
    # CONTROL A: Healthy system (no fault injected, 10 runs)
    # ──────────────────────────────────────────────────────────────────────────
    if args.batch in ("control_a", "all"):
        print("\n>>> STARTING CONTROL A: Healthy System (10 runs) <<<")
        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
        subprocess.run(["docker", "exec", "target_payment", "rm", "-f", "/tmp/fault_inject_5xx"], check=True)
        generate_normal_payment_traffic()

        for r in range(1, 11):
            payload_a = InvestigatePayload(
                alert="HighP99Latency",
                service="payment",
                endpoint="/payment/charge",
                metric="http_request_duration_seconds",
                threshold=0.5,
                window_start=datetime.now(timezone.utc),
            )
            res = await run_single_eval("ControlA", r, payload_a)
            all_results.append(res)
            if r < 10:
                await asyncio.sleep(4.0)

    # ──────────────────────────────────────────────────────────────────────────
    # POOL FAULT: Two batches of 5 runs (10 runs total)
    # ──────────────────────────────────────────────────────────────────────────
    if args.batch in ("pool_fault", "all"):
        print("\n>>> STARTING POOL FAULT BATCH 1 (Runs 1-5) <<<")
        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
        subprocess.run(
            [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
            check=True,
        )
        generate_pool_latency_traffic()
        await asyncio.sleep(2.0)

        for r in range(1, 6):
            payload_pool = InvestigatePayload(
                alert="HighP99Latency",
                service="payment",
                endpoint="/payment/charge",
                metric="http_request_duration_seconds",
                threshold=0.5,
                window_start=datetime.now(timezone.utc),
            )
            res = await run_single_eval("PoolFault_Batch1", r, payload_pool)
            all_results.append(res)
            if r < 5:
                await asyncio.sleep(4.0)

        # Brief pause between batches
        print("\n>>> STARTING POOL FAULT BATCH 2 (Runs 6-10) <<<")
        generate_pool_latency_traffic()
        await asyncio.sleep(2.0)

        for r in range(6, 11):
            payload_pool = InvestigatePayload(
                alert="HighP99Latency",
                service="payment",
                endpoint="/payment/charge",
                metric="http_request_duration_seconds",
                threshold=0.5,
                window_start=datetime.now(timezone.utc),
            )
            res = await run_single_eval("PoolFault_Batch2", r, payload_pool)
            all_results.append(res)
            if r < 10:
                await asyncio.sleep(4.0)

        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # ──────────────────────────────────────────────────────────────────────────
    # CONTROL B: Different fault (5xx, pool unsaturated, 5 runs)
    # ──────────────────────────────────────────────────────────────────────────
    if args.batch in ("control_b", "all"):
        print("\n>>> STARTING CONTROL B: 5xx Fault / Upstream Gateway (5 runs) <<<")
        subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
        subprocess.run(["docker", "exec", "target_payment", "touch", "/tmp/fault_inject_5xx"], check=True)
        generate_fault_5xx_traffic()

        for r in range(1, 6):
            payload_b = InvestigatePayload(
                alert="HighErrorRate",
                service="payment",
                endpoint="/payment/charge",
                metric="http_requests_total",
                threshold=0.05,
                window_start=datetime.now(timezone.utc),
            )
            res = await run_single_eval("ControlB", r, payload_b)
            all_results.append(res)
            if r < 5:
                await asyncio.sleep(4.0)

        subprocess.run(["docker", "exec", "target_payment", "rm", "-f", "/tmp/fault_inject_5xx"], check=True)

    # Save summary
    out_file = Path("eval/validation_rerun_results.json")
    out_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n[DONE] Saved {len(all_results)} run results to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
