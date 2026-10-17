import asyncio
import os
import time
from typing import Any
import httpx
import numpy as np

from .schemas import LoadBurstReturn

# Caps from env
MAX_LOAD_BURST_CONCURRENCY = int(os.environ.get("MAX_LOAD_BURST_CONCURRENCY", "10"))
MAX_LOAD_BURST_SECONDS = int(os.environ.get("MAX_LOAD_BURST_SECONDS", "10"))

# Service base URLs (host vs docker container aware)
ORDERS_URL = os.environ.get("ORDERS_SERVICE_URL", "http://localhost:8001")
PAYMENT_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://localhost:8002")
INVENTORY_URL = os.environ.get("INVENTORY_SERVICE_URL", "http://localhost:8003")
AUTH_URL = os.environ.get("AUTH_SERVICE_URL", "http://localhost:8004")

# Allowlisted endpoints of the target app ONLY
ENDPOINT_ROUTING = {
    "/orders": ("GET", f"{ORDERS_URL}/orders", None),
    "/payment/charge": ("POST", f"{PAYMENT_URL}/payment/charge", {"order_id": "probe", "amount": 1.0, "idempotency_key": "probe-key"}),
    "/inventory/item-1": ("GET", f"{INVENTORY_URL}/inventory/item-1", None),
    "/auth/verify": ("GET", f"{AUTH_URL}/auth/verify", None),
    "/health": ("GET", f"{ORDERS_URL}/health", None),
}


async def _send_probe_request(client: httpx.AsyncClient, method: str, url: str, body: dict | None) -> tuple[bool, float]:
    t0 = time.perf_counter()
    try:
        if method == "POST":
            # Append unique idempotency key to avoid 409
            data = dict(body) if body else {}
            data["idempotency_key"] = f"burst-{time.time_ns()}"
            resp = await client.post(url, json=data, timeout=3.0)
        else:
            resp = await client.get(url, timeout=3.0)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        is_success = resp.status_code < 400
        return is_success, dur_ms
    except Exception:
        dur_ms = (time.perf_counter() - t0) * 1000.0
        return False, dur_ms


async def execute_run_load_burst_async(
    endpoint: str,
    concurrency: int = 5,
    duration_s: int = 5,
) -> LoadBurstReturn:
    """
    Drives a bounded synthetic load burst against an allowlisted target app endpoint.
    Concurrency and duration are strictly capped.
    Rejects any endpoint not in the target app allowlist.
    """
    if endpoint not in ENDPOINT_ROUTING:
        raise ValueError(
            f"Disallowed endpoint '{endpoint}'. Allowed endpoints: {sorted(ENDPOINT_ROUTING.keys())}"
        )

    if concurrency > MAX_LOAD_BURST_CONCURRENCY:
        raise ValueError(
            f"Requested concurrency {concurrency} exceeds MAX_LOAD_BURST_CONCURRENCY limit of {MAX_LOAD_BURST_CONCURRENCY}"
        )

    if duration_s > MAX_LOAD_BURST_SECONDS:
        raise ValueError(
            f"Requested duration {duration_s}s exceeds MAX_LOAD_BURST_SECONDS limit of {MAX_LOAD_BURST_SECONDS}s"
        )

    concurrency = max(1, concurrency)
    duration_s = max(1, duration_s)

    method, target_url, body = ENDPOINT_ROUTING[endpoint]

    latencies_ms: list[float] = []
    success_count = 0
    error_count = 0
    stop_event = asyncio.Event()

    async with httpx.AsyncClient() as client:
        async def worker():
            nonlocal success_count, error_count
            while not stop_event.is_set():
                ok, lat = await _send_probe_request(client, method, target_url, body)
                latencies_ms.append(lat)
                if ok:
                    success_count += 1
                else:
                    error_count += 1
                await asyncio.sleep(0.05)  # slight spacing between iterations per worker

        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await asyncio.sleep(duration_s)
        stop_event.set()
        await asyncio.gather(*workers, return_exceptions=True)

    total_requests = success_count + error_count
    if latencies_ms:
        p50 = float(np.percentile(latencies_ms, 50))
        p95 = float(np.percentile(latencies_ms, 95))
        p99 = float(np.percentile(latencies_ms, 99))
    else:
        p50 = p95 = p99 = 0.0

    return LoadBurstReturn(
        endpoint=endpoint,
        concurrency=concurrency,
        duration_s=duration_s,
        total_requests=total_requests,
        successful_requests=success_count,
        error_requests=error_count,
        p50_latency_ms=round(p50, 2),
        p95_latency_ms=round(p95, 2),
        p99_latency_ms=round(p99, 2),
    )


def execute_run_load_burst(
    endpoint: str,
    concurrency: int = 5,
    duration_s: int = 5,
) -> LoadBurstReturn:
    """Synchronous wrapper for load burst tool."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(execute_run_load_burst_async(endpoint, concurrency, duration_s))
        return loop.run_until_complete(execute_run_load_burst_async(endpoint, concurrency, duration_s))
    except RuntimeError:
        return asyncio.run(execute_run_load_burst_async(endpoint, concurrency, duration_s))
