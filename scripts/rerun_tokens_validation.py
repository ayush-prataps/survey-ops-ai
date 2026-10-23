#!/usr/bin/env python3
"""
Token Reduction Validation Script:
Runs 3 Control A runs (healthy system) and 3 Pool Fault runs (injected pool latency).
Records:
- Batch and run index
- Outcome and root cause
- Confidence and iterations
- Prompt tokens, Completion tokens, Cached tokens, Reasoning tokens, Total tokens
- Wall clock time (s)
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


def generate_pool_latency_traffic():
    url = "http://localhost:8002/payment/charge"
    headers = {"Content-Type": "application/json"}
    for i in range(15):
        body = {
            "order_id": f"pool-traffic-{i}-{int(time.time())}",
            "amount": 99.99,
            "idempotency_key": f"key-pool-{i}-{int(time.time())}",
        }
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
        "cached_tokens": usage.get("cached_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
        "total_tokens": usage["total_tokens"],
        "served_models": usage.get("served_models", []),
        "wall_time_s": wall_time,
        "ledger_count": len(ledger_entries),
    }
    print(
        f"[{label} Run {run_idx}] Outcome={diagnosis.outcome.value} | "
        f"RootCause={res['root_cause']} | Score={diagnosis.confidence} | "
        f"Iters={diagnosis.iterations_used} | PromptTok={usage['prompt_tokens']} | "
        f"ComplTok={usage['completion_tokens']} | CachedTok={res['cached_tokens']} | "
        f"ReasoningTok={res['reasoning_tokens']} | TotalTok={usage['total_tokens']} | "
        f"Time={wall_time}s"
    )
    return res


async def main():
    start_stub_remediator_in_thread(8006)
    results = []

    # ── 1. Control A: 3 Healthy System Runs ──
    print("\n==============================================")
    print(">>> 1. RUNNING CONTROL A (3 Healthy Runs) <<<")
    print("==============================================")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run(["bash", "scripts/reset_trial.sh"], check=True)
    subprocess.run(["docker", "restart", "target_payment"], check=True)
    await asyncio.sleep(4.0)

    for r in range(1, 4):
        payload_a = InvestigatePayload(
            alert="HighP99Latency",
            service="payment",
            endpoint="/payment/charge",
            metric="http_request_duration_seconds",
            threshold=0.5,
            window_start=datetime.now(timezone.utc),
        )
        res = await run_single_eval("ControlA", r, payload_a)
        results.append(res)
        await asyncio.sleep(1.0)

    # ── 2. Pool Fault: 3 Faulted Runs ──
    print("\n==============================================")
    print(">>> 2. RUNNING POOL FAULT (3 Faulted Runs) <<<")
    print("==============================================")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run(["bash", "scripts/reset_trial.sh"], check=True)
    subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        check=True,
    )
    generate_pool_latency_traffic()
    await asyncio.sleep(2.0)

    for r in range(1, 4):
        payload_pool = InvestigatePayload(
            alert="HighP99Latency",
            service="payment",
            endpoint="/payment/charge",
            metric="http_request_duration_seconds",
            threshold=0.5,
            window_start=datetime.now(timezone.utc),
        )
        res = await run_single_eval("PoolFault", r, payload_pool)
        results.append(res)
        if r < 3:
            generate_pool_latency_traffic()
            await asyncio.sleep(2.0)

    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)

    out_file = Path("eval/token_reduction_rerun_results.json")
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\n[DONE] Saved {len(results)} run results to {out_file}")


if __name__ == "__main__":
    asyncio.run(main())
