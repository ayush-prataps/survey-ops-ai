#!/usr/bin/env python3
"""
Run 2 Pool Fault runs to verify reasoning_tokens and total_tokens with thinking disabled.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
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

from surveyops.schemas import InvestigatePayload, DiagnosisResult
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


async def main():
    start_stub_remediator_in_thread(8006)
    print(">>> Setting up Pool Fault trial <<<")
    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    subprocess.run(["bash", "scripts/reset_trial.sh"], check=True)
    subprocess.run(
        [sys.executable, "eval/faults/inject_latency.py", "--latency-ms", "2500", "--jitter-ms", "100"],
        check=True,
    )
    generate_pool_latency_traffic()
    await asyncio.sleep(2.0)

    results = []
    for r in range(1, 3):
        inc_id = f"inc-poolfault-nothink-{r}-{int(time.time())}"
        payload = InvestigatePayload(
            alert="HighP99Latency",
            service="payment",
            endpoint="/payment/charge",
            metric="http_request_duration_seconds",
            threshold=0.5,
            window_start=datetime.now(timezone.utc),
        )
        t0 = time.perf_counter()
        diag: DiagnosisResult = await run_diagnostician(
            incident=payload,
            incident_id=inc_id,
            runner_url="http://localhost:8005",
            remediator_url="http://localhost:8006",
            shared_secret=os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret",
        )
        wall_time = round(time.perf_counter() - t0, 2)
        usage = token_tracker.get_usage(inc_id)
        res = {
            "run": r,
            "incident_id": inc_id,
            "outcome": diag.outcome.value,
            "root_cause": diag.root_cause.value if diag.root_cause else None,
            "confidence": diag.confidence,
            "iterations": diag.iterations_used,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cached_tokens": usage.get("cached_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "total_tokens": usage["total_tokens"],
            "wall_time_s": wall_time,
        }
        print(f"\n[Pool Run {r} Result]: Outcome={res['outcome']} | RootCause={res['root_cause']} | ReasoningTokens={res['reasoning_tokens']} | TotalTokens={res['total_tokens']} | Time={wall_time}s")
        results.append(res)
        if r < 2:
            generate_pool_latency_traffic()
            await asyncio.sleep(2.0)

    subprocess.run([sys.executable, "eval/faults/clear_faults.py"], check=True)
    with open("eval/step1_nothink_pool_runs.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\n[Done] Results saved to eval/step1_nothink_pool_runs.json")


if __name__ == "__main__":
    asyncio.run(main())
