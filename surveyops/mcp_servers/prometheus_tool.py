import os
import urllib.parse
import urllib.request
import json
from typing import Any

from .schemas import PrometheusQueryReturn

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


def execute_prometheus_query(
    template: str,
    service: str | None = None,
    endpoint: str | None = None,
    range_s: int = 300,
) -> PrometheusQueryReturn:
    """
    Executes a vetted named query template against Prometheus.
    Arbitrary PromQL is strictly rejected.
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

    # Execute HTTP GET to Prometheus
    params = urllib.parse.urlencode({"query": query_str})
    url = f"{PROMETHEUS_URL}/api/v1/query?{params}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"Prometheus query failed for template '{template}': {exc}")

    results = []
    if data.get("status") == "success" and "data" in data:
        results = data["data"].get("result", [])

    return PrometheusQueryReturn(
        template=template,
        service=service,
        endpoint=endpoint,
        range_s=range_s,
        promql=query_str,
        results=results,
        status="success",
    )
