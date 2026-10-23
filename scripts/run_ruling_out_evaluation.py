#!/usr/bin/env python3
"""
Executes 6 Pool Fault runs and 3 Control B runs for the Ruling-Out fix evaluation.
Work branch: token-reduction-v2
Thinking: disabled
Records: outcome, iterations, tokens, status changes per run.
Saves results to eval/ruling_out_evaluation_results.json.
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
os.environ["MAX_INCIDENT_TOKEN_BUDGET"] = "100000"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from surveyops.schemas import (
    InvestigatePayload,
    DiagnosisResult,
    Outcome,
    FailureMode,
)
from surveyops.agents.remediator.stub_server import app as remediator_app
from surveyops.agents.diagnostician import run_diagnostician, get_incident_ledger
from surveyops.agents.diagnostician.agent import _incident_run_records
from surveyops.agents.diagnostician.callbacks import token_tracker


def start_stub_remediator(port: int = 8006):
    config = uvicorn.Config(app=remediator_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(30):
        if server.started:
            break
        time.sleep(0.1)
    return server


def generate_pool_traffic():
    """Generates concurrent requests to saturate payment DB pool under proxy latency."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {
            "order_id": f"pool-eval-{i}-{int(time.time()*1000)}",
            "amount": 99.99,
            "idempotency_key": f"key-eval-{i}-{int(time.time()*1000)}",
        }
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass


def generate_5xx_traffic():
    """Generates requests against 5xx-injected payment service."""
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(10):
        body = {
            "order_id": f"5xx-eval-{i}-{int(time.time()*1000)}",
            "amount": 50.0,
            "idempotency_key": f"key-5xx-{i}-{int(time.time()*1000)}",
        }
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.0)
        except Exception:
            pass


async def run_single(batch: str, idx: int, payload: InvestigatePayload) -> dict:
    inc_id = f"inc-eval-{batch.lower()}-run{idx}-{int(time.time())}"
    t0 = time.perf_counter()

    diagnosis: DiagnosisResult = await run_diagnostician(
        incident=payload,
        incident_id=inc_id,
        runner_url="http://localhost:8005",
        remediator_url="http://localhost:8006",
        shared_secret=os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret",
    )
    wall_time = round(time.perf_counter() - t0, 2)
    usage = token_tracker.get_usage(inc_id)
    rec = _incident_run_records.get(inc_id, {})
    status_changes = rec.get("status_changes", [])

    res = {
        "batch": batch,
        "run": idx,
        "incident_id": inc_id,
        "outcome": diagnosis.outcome.value,
        "root_cause": diagnosis.root_cause.value if diagnosis.root_cause else None,
        "confidence": diagnosis.confidence,
        "iterations": diagnosis.iterations_used,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "wall_time_s": wall_time,
        "status_changes": status_changes,
    }
    print(f"\n[{batch} Run {idx}] Outcome={res['outcome']} | Cause={res['root_cause']} | "
          f"Conf={res['confidence']} | Iters={res['iterations']} | Tokens={res['total_tokens']} | StatusChanges={len(status_changes)}")
    for sc in status_changes:
        print(f"    StatusChange: {sc.get('hypothesis_id')} {sc.get('from')} -> {sc.get('to')} (obs: {sc.get('observation_id')}): {sc.get('reason')}")
    return res


async def main():
    print("=================================================================")
    print("   RULING-OUT FIX EVALUATION: 6 POOL RUNS + 3 CONTROL B RUNS   ")
    print("=================================================================")

    start_stub_remediator(8006)
    all_runs = []

    # 1. Pool Fault: 6 runs
    print("\n>>> STARTING POOL FAULT BATCH (6 runs) <<<")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run(["docker", "exec", "target_payment", "rm", "-f", "/tmp/fault_inject_5xx"], check=True)
    subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        check=True,
    )
    generate_pool_traffic()
    await asyncio.sleep(2.0)

    for r in range(1, 7):
        generate_pool_traffic()
        payload_pool = InvestigatePayload(
            alert="HighP99Latency",
            service="payment",
            endpoint="/payment/charge",
            metric="http_request_duration_seconds",
            threshold=0.5,
            window_start=datetime.now(timezone.utc),
        )
        res = await run_single("PoolFault", r, payload_pool)
        all_runs.append(res)
        if r < 6:
            await asyncio.sleep(3.0)

    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # 2. Control B: 3 runs
    print("\n>>> STARTING CONTROL B BATCH (3 runs) <<<")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run(["docker", "exec", "target_payment", "touch", "/tmp/fault_inject_5xx"], check=True)
    generate_5xx_traffic()
    await asyncio.sleep(2.0)

    for r in range(1, 4):
        generate_5xx_traffic()
        payload_b = InvestigatePayload(
            alert="HighErrorRate",
            service="payment",
            endpoint="/payment/charge",
            metric="http_requests_total",
            threshold=0.05,
            window_start=datetime.now(timezone.utc),
        )
        res = await run_single("ControlB", r, payload_b)
        all_runs.append(res)
        if r < 3:
            await asyncio.sleep(3.0)

    subprocess.run(["docker", "exec", "target_payment", "rm", "-f", "/tmp/fault_inject_5xx"], check=True)

    out_file = Path("eval/ruling_out_evaluation_results.json")
    out_file.write_text(json.dumps(all_runs, indent=2))
    print(f"\n[DONE] Saved {len(all_runs)} runs to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
