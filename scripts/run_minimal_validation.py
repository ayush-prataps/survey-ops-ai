#!/usr/bin/env python3
"""
Minimal validation runner to verify positive detection with minimal token spend:
- Combines the 7 cleanly completed Control A runs
- Runs 2 Pool Fault runs (real pool latency fault)
- Runs 1 Control B run (5xx fault)
Saves results to eval/validation_rerun_results.json and runs scoring.
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

from surveyops.schemas import InvestigatePayload, DiagnosisResult, Outcome
from surveyops.agents.remediator.stub_server import app as remediator_app
from surveyops.agents.diagnostician import run_diagnostician, get_incident_ledger
from surveyops.agents.diagnostician.callbacks import token_tracker

CONTROL_A_RUNS = [
    {
        "batch": "ControlA",
        "run": 1,
        "incident_id": "inc-controla-1-1792569821",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 4,
        "prompt_tokens": 61058,
        "completion_tokens": 28929,
        "total_tokens": 89987,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 153.83,
        "ledger_count": 4,
    },
    {
        "batch": "ControlA",
        "run": 2,
        "incident_id": "inc-controla-2-1792569979",
        "outcome": "abstained",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 4,
        "prompt_tokens": 37847,
        "completion_tokens": 22009,
        "total_tokens": 59856,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 114.97,
        "ledger_count": 4,
    },
    {
        "batch": "ControlA",
        "run": 3,
        "incident_id": "inc-controla-3-1792570098",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 4,
        "prompt_tokens": 51739,
        "completion_tokens": 23647,
        "total_tokens": 75386,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 133.69,
        "ledger_count": 4,
    },
    {
        "batch": "ControlA",
        "run": 4,
        "incident_id": "inc-controla-4-1792570236",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 4,
        "prompt_tokens": 55656,
        "completion_tokens": 31301,
        "total_tokens": 86957,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 154.19,
        "ledger_count": 4,
    },
    {
        "batch": "ControlA",
        "run": 5,
        "incident_id": "inc-controla-5-1792570394",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 3,
        "prompt_tokens": 42178,
        "completion_tokens": 20722,
        "total_tokens": 62900,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 100.51,
        "ledger_count": 3,
    },
    {
        "batch": "ControlA",
        "run": 6,
        "incident_id": "inc-controla-6-1792570498",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 4,
        "prompt_tokens": 46555,
        "completion_tokens": 25817,
        "total_tokens": 72372,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 136.93,
        "ledger_count": 4,
    },
    {
        "batch": "ControlA",
        "run": 7,
        "incident_id": "inc-controla-7-1792570639",
        "outcome": "budget_exhausted",
        "root_cause": None,
        "heuristic_score": None,
        "self_reported_confidence": None,
        "iterations": 3,
        "prompt_tokens": 38647,
        "completion_tokens": 22549,
        "total_tokens": 61196,
        "served_models": ["deepseek-flash"],
        "wall_time_s": 106.29,
        "ledger_count": 3,
    },
]

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

def generate_fault_5xx_traffic():
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(10):
        body = {"order_id": f"f5xx-{i}-{int(time.time())}", "amount": 50.0, "idempotency_key": f"key-5xx-{i}-{int(time.time())}"}
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.0)
        except Exception:
            pass

def generate_pool_latency_traffic():
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {"order_id": f"pool-{i}-{int(time.time())}", "amount": 99.99, "idempotency_key": f"key-pool-{i}-{int(time.time())}"}
        try:
            httpx.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass

async def run_single_eval(label: str, run_idx: int, payload: InvestigatePayload) -> dict:
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
    start_stub_remediator_in_thread(8006)
    all_results = list(CONTROL_A_RUNS)
    print(f"Loaded {len(all_results)} Control A runs already verified.")

    # 1. Pool Fault: 2 runs
    print("\n>>> INJECTING POOL LATENCY FAULT (2 runs) <<<")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run([sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"], check=True)
    generate_pool_latency_traffic()
    await asyncio.sleep(2.0)

    for r in range(1, 3):
        payload_pool = InvestigatePayload(
            alert="HighP99Latency",
            service="payment",
            endpoint="/payment/charge",
            metric="http_request_duration_seconds",
            threshold=0.5,
            window_start=datetime.now(timezone.utc),
        )
        res = await run_single_eval("PoolFault", r, payload_pool)
        all_results.append(res)
        if r < 2:
            await asyncio.sleep(3.0)

    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    # 2. Control B: 1 run
    print("\n>>> INJECTING CONTROL B: 5xx FAULT (1 run) <<<")
    subprocess.run(["docker", "exec", "target_payment", "touch", "/tmp/fault_inject_5xx"], check=True)
    generate_fault_5xx_traffic()
    await asyncio.sleep(2.0)

    payload_b = InvestigatePayload(
        alert="HighErrorRate",
        service="payment",
        endpoint="/payment/charge",
        metric="http_requests_total",
        threshold=0.05,
        window_start=datetime.now(timezone.utc),
    )
    res_b = await run_single_eval("ControlB", 1, payload_b)
    all_results.append(res_b)

    subprocess.run(["docker", "exec", "target_payment", "rm", "-f", "/tmp/fault_inject_5xx"], check=True)

    out_file = Path("eval/validation_rerun_results.json")
    out_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n[DONE] Saved {len(all_results)} run results to {out_file}")

if __name__ == "__main__":
    asyncio.run(main())
