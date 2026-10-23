#!/usr/bin/env python3
"""
Single-run script for Diagnostician bisect.
Can be executed within any worktree or workspace directory.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
import time

load_dotenv_path = os.path.join(os.getcwd(), ".env")
if os.path.exists(load_dotenv_path):
    from dotenv import load_dotenv
    load_dotenv(load_dotenv_path)

os.environ["SURVEYOPS_LLM_CACHE_ENABLED"] = "false"
sys.path.insert(0, os.getcwd())

from surveyops.schemas import InvestigatePayload, DiagnosisResult
from surveyops.agents.diagnostician import run_diagnostician, get_incident_ledger
from surveyops.agents.diagnostician.callbacks import token_tracker


class BisectLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.model_calls = []
        self.validation_rejections = []
        self.exit_evals = []

    def emit(self, record):
        msg = self.format(record)
        if "after_model" in msg:
            try:
                data = json.loads(msg)
                if data.get("callback") == "after_model":
                    self.model_calls.append(data)
            except Exception:
                pass
        if "rejection" in msg.lower() or "rejected" in msg.lower():
            self.validation_rejections.append(msg)
        if "EXIT EVAL:" in msg or "ExitDecision" in msg:
            self.exit_evals.append(msg)


async def main():
    if len(sys.argv) < 3:
        print("Usage: run_bisect_single.py <run_label> <output_json_path>")
        sys.exit(1)

    run_label = sys.argv[1]
    output_path = sys.argv[2]

    # Attach log handler to diagnostician logger
    handler = BisectLogHandler()
    logging.getLogger("surveyops.diagnostician").addHandler(handler)
    logging.getLogger("surveyops.diagnostician").setLevel(logging.INFO)

    inc_id = f"inc-bisect-{run_label}-{int(time.time())}"
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
    ledger = get_incident_ledger(inc_id)
    usage = token_tracker.get_usage(inc_id)

    # Per-iteration tokens
    iter_tokens = {}
    for mc in handler.model_calls:
        it = mc.get("iteration", 1)
        if it not in iter_tokens:
            iter_tokens[it] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        iter_tokens[it]["prompt_tokens"] += mc.get("prompt_tokens", 0)
        iter_tokens[it]["completion_tokens"] += mc.get("completion_tokens", 0)
        iter_tokens[it]["total_tokens"] += mc.get("total_tokens", 0)
        iter_tokens[it]["calls"] += 1

    # Hypothesis status history & ruled out reasons
    status_history = {}
    ruled_out_details = {}
    for e in ledger:
        h_id = e.hypothesis_id
        if h_id not in status_history:
            status_history[h_id] = []
        entry_summary = {
            "iteration": e.iteration,
            "status": e.status,
            "confidence": e.confidence,
            "disconfirmed": e.disconfirming_condition_met,
            "justification": e.disconfirming_justification,
            "evidence_for": e.evidence_for,
            "evidence_against": e.evidence_against,
        }
        status_history[h_id].append(entry_summary)
        if e.status == "ruled_out" and h_id not in ruled_out_details:
            ruled_out_details[h_id] = {
                "iteration": e.iteration,
                "disconfirming_justification": e.disconfirming_justification,
                "evidence_against": e.evidence_against,
            }

    # Which gate blocked root cause
    gate_block_reason = None
    if diag.outcome.value != "root_cause":
        gate_block_reason = getattr(diag, "reason", None) or (handler.exit_evals[-1] if handler.exit_evals else "Unknown")

    result = {
        "run_label": run_label,
        "incident_id": inc_id,
        "outcome": diag.outcome.value,
        "root_cause": diag.root_cause.value if diag.root_cause else None,
        "confidence": diag.confidence,
        "iterations": diag.iterations_used,
        "wall_time_s": wall_time,
        "validation_rejections_count": len(handler.validation_rejections),
        "validation_rejections": handler.validation_rejections,
        "gate_block_reason": gate_block_reason,
        "per_iteration_tokens": iter_tokens,
        "total_prompt_tokens": usage["prompt_tokens"],
        "total_completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "status_history": status_history,
        "ruled_out_details": ruled_out_details,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n[Run {run_label} Complete]: outcome={result['outcome']}, root_cause={result['root_cause']}, iterations={result['iterations']}, tokens={result['total_tokens']}")


if __name__ == "__main__":
    asyncio.run(main())
