#!/usr/bin/env python3
"""
Standalone test script to send hand-written ExperimentRequests to the running
Experiment Runner over A2A JSON-RPC (http://localhost:8005) and verify raw Observations.

Usage:
    python scripts/test_runner_a2a.py [--url http://localhost:8005]
"""

import argparse
import json
import os
import sys
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from surveyops.schemas import (
    ExperimentRequest,
    ExperimentType,
    Observation,
)
from surveyops.a2a_utils import to_a2a_text_part, from_a2a_message


def send_a2a_request(
    runner_url: str,
    request_obj: ExperimentRequest | None,
    raw_parts: list | None = None,
    secret: str | None = "dev-secret",
) -> tuple[int, dict]:
    if raw_parts is not None:
        parts = raw_parts
    elif request_obj is not None:
        parts = [to_a2a_text_part(request_obj).model_dump()]
    else:
        parts = []

    payload = {
        "jsonrpc": "2.0",
        "id": "standalone-test-1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": f"msg-standalone-{request_obj.request_id if request_obj else 'raw'}",
                "role": "user",
                "parts": parts,
            }
        },
    }

    headers = {}
    if secret:
        headers["X-Shared-Secret"] = secret

    resp = httpx.post(runner_url, json=payload, headers=headers, timeout=30.0)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}
    return resp.status_code, body


def main():
    parser = argparse.ArgumentParser(description="Test Experiment Runner over A2A")
    parser.add_argument("--url", default="http://localhost:8005", help="Runner A2A service URL")
    args = parser.parse_args()

    print(f"Connecting to Experiment Runner at {args.url}...")

    req_metrics = ExperimentRequest(
        request_id="req-live-01",
        hypothesis_id="hyp-db-latency",
        experiment_type=ExperimentType.query_metrics,
        params={"template": "p99_latency", "service": "payment", "endpoint": "/payment/charge", "range_s": 120},
        rationale="Check p99 latency during live run",
        disconfirming_if="p99_latency < 0.5s",
    )

    # ── Test 0: Authentication (Shared Secret Enforcement) ───────────────────
    print("\n[0] Testing Authentication: Missing and invalid secrets...")
    code_no_auth, _ = send_a2a_request(args.url, req_metrics, secret=None)
    assert code_no_auth == 403, f"Expected 403 for missing secret, got {code_no_auth}"
    print("  -> SUCCESS: Missing secret rejected with HTTP 403")

    code_bad_auth, _ = send_a2a_request(args.url, req_metrics, secret="wrong-secret")
    assert code_bad_auth == 403, f"Expected 403 for wrong secret, got {code_bad_auth}"
    print("  -> SUCCESS: Wrong secret rejected with HTTP 403")

    # ── Test 1: Query Metrics (Positive) ──────────────────────────────────────
    print("\n[1] Testing query_metrics (p99_latency on /payment/charge)...")
    code1, res1 = send_a2a_request(args.url, req_metrics)
    assert code1 == 200, f"Expected 200, got {code1}: {res1}"
    task1 = res1.get("result", {})
    state1 = task1.get("status", {}).get("state")
    assert state1 == "completed", f"Expected completed, got {state1}: {task1}"
    obs1_dec = from_a2a_message(task1.get("status", {}).get("message"), Observation)
    assert obs1_dec.is_ok, f"Failed to decode observation: {obs1_dec.error}"
    obs1 = obs1_dec.value
    print(f"  -> SUCCESS: Got Observation(source={obs1.source}, untrusted={obs1.contains_untrusted_text})")
    print(f"  -> PromQL executed: {obs1.data.get('promql')}")
    print(f"  -> Metrics results: {obs1.data.get('results')}")

    # ── Test 2: Fetch Logs (Positive & Untrusted Flag) ─────────────────────────
    print("\n[2] Testing fetch_logs (payment service via Docker Log Proxy)...")
    req_logs = ExperimentRequest(
        request_id="req-live-02",
        hypothesis_id="hyp-db-latency",
        experiment_type=ExperimentType.fetch_logs,
        params={"service": "payment", "since_s": 120, "limit": 10},
        rationale="Inspect payment logs for DB connection timeouts",
        disconfirming_if="No errors in logs",
    )
    code2, res2 = send_a2a_request(args.url, req_logs)
    assert code2 == 200, f"Expected 200, got {code2}: {res2}"
    task2 = res2.get("result", {})
    state2 = task2.get("status", {}).get("state")
    assert state2 == "completed", f"Expected completed, got {state2}"
    obs2_dec = from_a2a_message(task2.get("status", {}).get("message"), Observation)
    assert obs2_dec.is_ok, f"Failed to decode observation: {obs2_dec.error}"
    obs2 = obs2_dec.value
    assert obs2.contains_untrusted_text is True, "contains_untrusted_text must be True for logs"
    assert "<UNTRUSTED_SERVICE_LOGS" in obs2.data.get("logs", ""), "Logs must be enclosed in untrusted tags"
    print(f"  -> SUCCESS: Got Observation(source={obs2.source}, untrusted={obs2.contains_untrusted_text})")
    print(f"  -> Lines returned: {obs2.data.get('lines_count')}")

    # ── Test 3: Load Burst (Positive) ─────────────────────────────────────────
    print("\n[3] Testing load_burst (/orders)...")
    req_burst = ExperimentRequest(
        request_id="req-live-03",
        hypothesis_id="hyp-db-latency",
        experiment_type=ExperimentType.load_burst,
        params={"endpoint": "/orders", "concurrency": 2, "duration_s": 2},
        rationale="Apply synthetic burst to orders",
        disconfirming_if="Latency does not elevate",
    )
    code3, res3 = send_a2a_request(args.url, req_burst)
    assert code3 == 200, f"Expected 200, got {code3}: {res3}"
    task3 = res3.get("result", {})
    state3 = task3.get("status", {}).get("state")
    assert state3 == "completed", f"Expected completed, got {state3}"
    obs3_dec = from_a2a_message(task3.get("status", {}).get("message"), Observation)
    assert obs3_dec.is_ok
    obs3 = obs3_dec.value
    print(f"  -> SUCCESS: Got Observation(source={obs3.source})")
    print(f"  -> Stats: total={obs3.data.get('total_requests')}, p50={obs3.data.get('p50_latency_ms')}ms, p99={obs3.data.get('p99_latency_ms')}ms")

    # ── Negative Test 1: Disallowed Endpoint ──────────────────────────────────
    print("\n[4] Testing Negative: Disallowed endpoint (/admin/secret)...")
    req_neg_endpoint = ExperimentRequest(
        request_id="req-neg-01",
        hypothesis_id="hyp-1",
        experiment_type=ExperimentType.load_burst,
        params={"endpoint": "/admin/secret"},
        rationale="Negative test endpoint allowlist",
        disconfirming_if="test",
    )
    code_neg1, res_neg1 = send_a2a_request(args.url, req_neg_endpoint)
    assert code_neg1 == 200
    state_neg1 = res_neg1.get("result", {}).get("status", {}).get("state")
    assert state_neg1 == "failed", f"Expected state=failed, got {state_neg1}"
    print("  -> SUCCESS: Correctly rejected disallowed endpoint (state=failed)")

    # ── Negative Test 2: Toxiproxy Access ─────────────────────────────────────
    print("\n[5] Testing Negative: Toxiproxy service access...")
    req_neg_toxiproxy = ExperimentRequest(
        request_id="req-neg-02",
        hypothesis_id="hyp-1",
        experiment_type=ExperimentType.fetch_logs,
        params={"service": "toxiproxy"},
        rationale="Negative test toxiproxy isolation",
        disconfirming_if="test",
    )
    code_neg2, res_neg2 = send_a2a_request(args.url, req_neg_toxiproxy)
    assert code_neg2 == 200
    state_neg2 = res_neg2.get("result", {}).get("status", {}).get("state")
    assert state_neg2 == "failed", f"Expected state=failed, got {state_neg2}"
    print("  -> SUCCESS: Correctly rejected toxiproxy log access (state=failed)")

    # ── Negative Test 3: Unknown PromQL Template ──────────────────────────────
    print("\n[6] Testing Negative: Unknown PromQL template...")
    req_neg_template = ExperimentRequest(
        request_id="req-neg-03",
        hypothesis_id="hyp-1",
        experiment_type=ExperimentType.query_metrics,
        params={"template": "arbitrary_custom_query"},
        rationale="Negative test template allowlist",
        disconfirming_if="test",
    )
    code_neg3, res_neg3 = send_a2a_request(args.url, req_neg_template)
    assert code_neg3 == 200
    state_neg3 = res_neg3.get("result", {}).get("status", {}).get("state")
    assert state_neg3 == "failed", f"Expected state=failed, got {state_neg3}"
    print("  -> SUCCESS: Correctly rejected unknown PromQL template (state=failed)")

    # ── Negative Test 4: Load Burst Over Cap ──────────────────────────────────
    print("\n[7] Testing Negative: Load burst concurrency over cap...")
    req_neg_cap = ExperimentRequest(
        request_id="req-neg-04",
        hypothesis_id="hyp-1",
        experiment_type=ExperimentType.load_burst,
        params={"endpoint": "/orders", "concurrency": 100, "duration_s": 2},
        rationale="Negative test concurrency limit",
        disconfirming_if="test",
    )
    code_neg4, res_neg4 = send_a2a_request(args.url, req_neg_cap)
    assert code_neg4 == 200
    state_neg4 = res_neg4.get("result", {}).get("status", {}).get("state")
    assert state_neg4 == "failed", f"Expected state=failed, got {state_neg4}"
    print("  -> SUCCESS: Correctly rejected concurrency exceeding cap (state=failed)")

    # ── Negative Test 5: Malformed JSON ───────────────────────────────────────
    print("\n[8] Testing Negative: Malformed payload...")
    code_neg5, res_neg5 = send_a2a_request(args.url, None, raw_parts=[{"kind": "text", "text": "invalid { json"}])
    assert code_neg5 == 200
    state_neg5 = res_neg5.get("result", {}).get("status", {}).get("state")
    assert state_neg5 == "failed", f"Expected state=failed, got {state_neg5}"
    print("  -> SUCCESS: Correctly rejected malformed JSON (state=failed)")

    print("\n=======================================================")
    print(" ALL EXPERIMENT RUNNER STANDALONE TESTS PASSED!")
    print("=======================================================")


if __name__ == "__main__":
    main()
