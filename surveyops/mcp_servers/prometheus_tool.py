import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .schemas import PrometheusQueryReturn

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

ALLOWED_SERVICES = {"orders", "payment", "inventory", "auth"}
ALLOWED_ENDPOINTS = {
    "/orders",
    "/payment/charge",
    "/inventory/{item_id}",
    "/inventory/{item_id}/reserve",
    "/auth/login",
    "/auth/verify",
    "/health",
    "/metrics",
}

QUERY_TEMPLATES = {
    "p99_latency": 'histogram_quantile(0.99, sum by (le, handler) (rate(http_request_duration_seconds_bucket{__SELECTOR__}[__RANGE__s])))',
    "error_rate": '(sum by (handler) (rate(http_requests_total{__SELECTOR_COMMA__status="5xx"}[__RANGE__s])) or vector(0)) / (sum by (handler) (rate(http_requests_total{__SELECTOR__}[__RANGE__s])) > 0)',
    "request_rate": 'sum by (handler) (rate(http_requests_total{__SELECTOR__}[__RANGE__s]))',
    "pool_in_use": 'payment_db_pool_in_use_connections',
    "pool_capacity": 'payment_db_pool_capacity',
}


def _query_instant(promql: str) -> list[dict[str, Any]]:
    """Helper to execute an instant PromQL query against Prometheus."""
    params = urllib.parse.urlencode({"query": promql})
    url = f"{PROMETHEUS_URL}/api/v1/query?{params}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") == "success" and "data" in data:
            return data["data"].get("result", [])
    except Exception:
        pass
    return []


def _extract_scalar_value(result_list: list[dict[str, Any]]) -> float | None:
    """Extracts scalar float from Prometheus result vector if present."""
    if result_list and isinstance(result_list, list) and len(result_list) > 0:
        val = result_list[0].get("value")
        if val and len(val) >= 2:
            try:
                return float(val[1])
            except (ValueError, TypeError):
                pass
    return None


def execute_prometheus_query(
    template: str,
    service: str | None = None,
    endpoint: str | None = None,
    range_s: int = 300,
    window_start: str | None = None,
) -> PrometheusQueryReturn:
    """
    Executes a vetted named query template against Prometheus.
    Arbitrary PromQL is strictly rejected.
    When querying 'p99_latency', automatically bundles request rate, sample count,
    and DB pool metrics so latency cannot be misinterpreted when few samples finished.
    If window_start is provided, anchors range_s to the incident onset (preventing lookback into older trials).
    """
    if template not in QUERY_TEMPLATES:
        raise ValueError(
            f"Disallowed query template '{template}'. Allowed templates: {list(QUERY_TEMPLATES.keys())}"
        )

    if service is not None and service not in ALLOWED_SERVICES:
        raise ValueError(
            f"Disallowed service '{service}'. Allowed services: {sorted(ALLOWED_SERVICES)}"
        )

    if endpoint is not None and endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError(
            f"Disallowed endpoint '{endpoint}'. Allowed endpoints: {sorted(ALLOWED_ENDPOINTS)}"
        )

    # If window_start is given, anchor range_s to elapsed seconds since window_start
    if window_start:
        try:
            ws_str = str(window_start).replace("Z", "+00:00")
            ws_dt = datetime.fromisoformat(ws_str)
            if ws_dt.tzinfo is None:
                ws_dt = ws_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            elapsed_s = int((now_utc - ws_dt).total_seconds())
            range_s = min(max(elapsed_s + 5, 15), 3600)
        except Exception as exc:
            logger.warning(f"Could not parse window_start '{window_start}': {exc}")
            range_s = min(max(int(range_s), 10), 3600)
    else:
        # Cap range between 10s and 3600s
        range_s = min(max(int(range_s), 10), 3600)

    # Build safe query string from template
    raw_query = QUERY_TEMPLATES[template]
    if endpoint:
        query_str = raw_query.replace("__SELECTOR_COMMA__", f'handler="{endpoint}",')
        query_str = query_str.replace("__SELECTOR__", f'handler="{endpoint}"')
    else:
        query_str = raw_query.replace("__SELECTOR_COMMA__", "").replace("__SELECTOR__", "")

    query_str = query_str.replace("__RANGE__", str(range_s))

    # Execute primary HTTP GET to Prometheus
    results = _query_instant(query_str)

    req_rate: float | None = None
    req_count: float | None = None
    pool_in_use: float | None = None
    pool_max_in_use: float | None = None
    pool_capacity: float | None = None
    pool_utilization_ratio: float | None = None

    if template == "pool_in_use":
        # 1. Current value
        in_use_res = _query_instant("payment_db_pool_in_use_connections")
        pool_in_use = _extract_scalar_value(in_use_res)

        # 2. Max over window
        max_q = f"max_over_time(payment_db_pool_in_use_connections[{range_s}s])"
        max_res = _query_instant(max_q)
        pool_max_in_use = _extract_scalar_value(max_res)

        # 3. Capacity
        cap_res = _query_instant("payment_db_pool_capacity")
        pool_capacity = _extract_scalar_value(cap_res)

        # 4. Ratio to capacity
        effective_in_use = pool_max_in_use if pool_max_in_use is not None else pool_in_use
        if effective_in_use is not None and pool_capacity and pool_capacity > 0:
            pool_utilization_ratio = round(effective_in_use / pool_capacity, 4)

    # When querying p99_latency, bundle request rate/count and DB pool gauges
    elif template == "p99_latency":
        # 1. Request rate & request count
        selector = f'handler="{endpoint}"' if endpoint else ""
        rate_q = f"sum by (handler) (rate(http_requests_total{{{selector}}}[{range_s}s]))"
        inc_q = f"sum by (handler) (increase(http_requests_total{{{selector}}}[{range_s}s]))"

        rate_res = _query_instant(rate_q)
        inc_res = _query_instant(inc_q)
        req_rate = _extract_scalar_value(rate_res)
        req_count = _extract_scalar_value(inc_res)

        # 2. Pool gauges (immediate indicators of exhaustion before requests timeout)
        in_use_res = _query_instant("payment_db_pool_in_use_connections")
        max_q = f"max_over_time(payment_db_pool_in_use_connections[{range_s}s])"
        max_res = _query_instant(max_q)
        cap_res = _query_instant("payment_db_pool_capacity")

        pool_in_use = _extract_scalar_value(in_use_res)
        pool_max_in_use = _extract_scalar_value(max_res)
        pool_capacity = _extract_scalar_value(cap_res)

        effective_in_use = pool_max_in_use if pool_max_in_use is not None else pool_in_use
        if effective_in_use is not None and pool_capacity and pool_capacity > 0:
            pool_utilization_ratio = round(effective_in_use / pool_capacity, 4)

    return PrometheusQueryReturn(
        template=template,
        service=service,
        endpoint=endpoint,
        range_s=range_s,
        promql=query_str,
        results=results,
        request_rate=req_rate,
        request_count=req_count,
        pool_in_use=pool_in_use,
        pool_max_in_use=pool_max_in_use,
        pool_capacity=pool_capacity,
        pool_utilization_ratio=pool_utilization_ratio,
        status="success",
    )
