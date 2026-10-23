#!/usr/bin/env python3
"""
Replay check_system_healthy against historical observations from:
- Pool Fault (saturated pool, low pool with high latency, early onset)
- Control B (502 in logs, empty logs with 5xx error rate)
- Healthy Control A (low latency, zero pool in use, clean 200 OK logs)
"""
from datetime import datetime, timezone
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from surveyops.schemas import Observation, InvestigatePayload
from surveyops.agents.diagnostician.agent import check_system_healthy

now = datetime.now(timezone.utc)

test_cases = [
    {
        "name": "Pool Fault: Full saturation",
        "expected": False,
        "is_fault": True,
        "metric_obs": Observation(
            request_id="obs-pf-1",
            source="prometheus",
            data={"pool_utilization_ratio": 1.0, "pool_in_use": 5.0, "pool_capacity": 5.0, "results": [{"value": [100, "2.8"]}]},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-pf-1-logs",
            source="docker_logs",
            data={"service": "payment", "lines": ["QueuePool limit of size 5 overflow 10 reached"]},
            collected_at=now,
        ),
    },
    {
        "name": "Pool Fault: Early onset (pool ratio=0.2, p99=2.5s, clean logs)",
        "expected": False,
        "is_fault": True,
        "metric_obs": Observation(
            request_id="obs-pf-2",
            source="prometheus",
            data={"pool_utilization_ratio": 0.2, "pool_in_use": 1.0, "pool_capacity": 5.0, "p99_latency_ms": 2500.0, "results": [{"value": [100, "2.5"]}]},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-pf-2-logs",
            source="docker_logs",
            data={"service": "payment", "lines": ["INFO: processing charge"]},
            collected_at=now,
        ),
    },
    {
        "name": "Pool Fault: Empty logs & sub-threshold pool at instant of alert",
        "expected": False,
        "is_fault": True,
        "metric_obs": Observation(
            request_id="obs-pf-3",
            source="prometheus",
            data={"pool_utilization_ratio": 0.4, "pool_in_use": 2.0, "pool_capacity": 5.0},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-pf-3-logs",
            source="docker_logs",
            data={"service": "payment", "lines": []},
            collected_at=now,
        ),
    },
    {
        "name": "Control B: 502 in logs (pool ratio=0.0)",
        "expected": False,
        "is_fault": True,
        "metric_obs": Observation(
            request_id="obs-cb-1",
            source="prometheus",
            data={"pool_utilization_ratio": 0.0, "pool_in_use": 0.0, "pool_capacity": 5.0},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-cb-1-logs",
            source="docker_logs",
            data={"service": "payment", "lines": ["502 Bad Gateway: Upstream payment gateway unavailable"]},
            collected_at=now,
        ),
    },
    {
        "name": "Control B: Empty logs with high error rate (pool ratio=0.0, error_rate=0.10)",
        "expected": False,
        "is_fault": True,
        "metric_obs": Observation(
            request_id="obs-cb-2",
            source="prometheus",
            data={"pool_utilization_ratio": 0.0, "pool_in_use": 0.0, "pool_capacity": 5.0, "error_rate": 0.10, "error_requests": 5},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-cb-2-logs",
            source="docker_logs",
            data={"service": "payment", "lines": []},
            collected_at=now,
        ),
    },
    {
        "name": "Control A: True healthy system (p99=45ms, pool=0.0, clean 200 OK)",
        "expected": True,
        "is_fault": False,
        "metric_obs": Observation(
            request_id="obs-ca-1",
            source="prometheus",
            data={"pool_utilization_ratio": 0.0, "pool_in_use": 0.0, "pool_capacity": 5.0, "p99_latency_ms": 45.0, "error_rate": 0.0, "results": [{"value": [100, "0.045"]}]},
            collected_at=now,
        ),
        "log_obs": Observation(
            request_id="obs-ca-1-logs",
            source="docker_logs",
            data={"service": "payment", "lines": ["GET /health responded 200 in 2ms", "POST /payment/charge responded 200 in 35ms"]},
            collected_at=now,
        ),
    },
]

def check_system_healthy_fixed(
    metric_obs: Observation | None,
    log_obs: Observation | None,
) -> tuple[bool, str]:
    if not metric_obs and not log_obs:
        return False, "Probes failed to execute"

    for obs in (metric_obs, log_obs):
        if obs and obs.source in ("runner_connection_error", "a2a_runner_http_error", "runner_task_failed"):
            return False, f"Runner communication error during probe: {obs.source}"

    # 1. Check metric data
    if not metric_obs or not metric_obs.data:
        return False, "Insufficient telemetry: metric observation missing"

    m_data = metric_obs.data
    # 1a. Connection pool saturation
    pool_ratio = m_data.get("pool_utilization_ratio")
    if pool_ratio is not None and float(pool_ratio) >= 0.80:
        return False, f"Pool saturation detected: pool_utilization_ratio={pool_ratio}"

    in_use = m_data.get("pool_max_in_use", m_data.get("pool_in_use"))
    cap = m_data.get("pool_capacity")
    if in_use is not None and cap is not None and float(cap) > 0:
        if float(in_use) >= float(cap):
            return False, f"Pool saturated: in_use={in_use} >= cap={cap}"

    # 1b. Latency elevation
    p99_ms = m_data.get("p99_latency_ms")
    if p99_ms is not None and float(p99_ms) > 500.0:
        return False, f"High latency detected in metrics: p99={p99_ms}ms"

    # 1c. Prometheus scalar values (>0.5s or threshold)
    if "results" in m_data and isinstance(m_data["results"], list):
        for r in m_data["results"]:
            val = r.get("value")
            if val and len(val) > 1:
                try:
                    num_val = float(val[1])
                    if num_val > 0.5:
                        return False, f"Elevated Prometheus metric value: {num_val}"
                except (ValueError, TypeError):
                    pass

    # 1d. Error rate / error counts
    error_rate = m_data.get("error_rate")
    if error_rate is not None and float(error_rate) > 0.0:
        return False, f"Non-zero error rate detected: {error_rate}"

    error_requests = m_data.get("error_requests")
    if error_requests is not None and int(error_requests) > 0:
        return False, f"Error requests detected in telemetry: {error_requests}"

    # 2. Check container logs
    if not log_obs or not log_obs.data:
        return False, "Insufficient telemetry: log observation missing"

    l_data = log_obs.data
    raw_lines = l_data.get("lines", [])
    if isinstance(raw_lines, str):
        raw_lines = [raw_lines]
    elif not raw_lines and "logs" in l_data:
        raw_lines = str(l_data["logs"]).splitlines()

    if not raw_lines:
        return False, "Insufficient telemetry: container logs empty"

    error_regex = re.compile(
        r"QueuePool limit.*reached|TimeoutError|PoolTimeout|500 Internal Server Error|"
        r"502 Bad Gateway|Connection refused|Exception|Traceback|status_code=5\d\d",
        re.IGNORECASE,
    )
    for line in raw_lines:
        if error_regex.search(str(line)):
            return False, f"Fault signature in container logs: {str(line)[:80]}"

    return True, "No saturation, no error logs, normal latency (system healthy)"


print("=== REPLAYING check_system_healthy_fixed ===")
failures = []
for tc in test_cases:
    is_healthy, reason = check_system_healthy_fixed(tc["metric_obs"], tc["log_obs"])
    matched = (is_healthy == tc["expected"])
    flag = "OK" if matched else "BUG: FALSE ABSTAIN ON FAULT!" if (tc["is_fault"] and is_healthy) else "UNEXPECTED"
    print(f"[{flag}] {tc['name']}")
    print(f"  -> Returned: is_healthy={is_healthy} ({reason})")
    print(f"  -> Expected: is_healthy={tc['expected']}")
    if not matched:
        failures.append(tc["name"])

print(f"\nTotal test cases: {len(test_cases)}, Mismatches: {len(failures)}")
if failures:
    print("Mismatched cases:")
    for f in failures:
        print(f"  - {f}")

