"""
Controller Service for SurveyOps Demo.
Runs in an isolated container that can reach Toxiproxy and target Postgres on data_net.
Protected by shared secret.
Agents CANNOT reach this service or Toxiproxy.

Endpoints:
- GET  /health
- POST /inject (injects toxic on payment-postgres proxy)
- POST /reset  (clears toxics on payment-postgres proxy & resets target tables)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sys
from typing import Any, Optional
import urllib.request
import urllib.error

from fastapi import FastAPI, Header, HTTPException, Request
import httpx
import psycopg2
from pydantic import BaseModel

logger = logging.getLogger("surveyops.controller")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[CONTROLLER] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

app = FastAPI(title="SurveyOps Controller", version="0.1.0")

SHARED_SECRET = os.environ.get("CONTROLLER_SHARED_SECRET", os.environ.get("INVESTIGATE_SHARED_SECRET", "dev-secret"))
TOXI_API = os.environ.get("TOXIPROXY_API", "http://toxiproxy:8474")
PROXY_NAME = "payment-postgres"
TOXIC_NAME = "latency-payment-postgres"
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
SURVEYOPS_API_URL = os.environ.get("SURVEYOPS_API_URL", "http://surveyops-api:8000")

TARGET_PG_HOST = os.environ.get("TARGET_POSTGRES_HOST", "postgres")
TARGET_PG_PORT = int(os.environ.get("TARGET_POSTGRES_PORT", "5432"))
TARGET_PG_USER = os.environ.get("TARGET_POSTGRES_USER", "postgres")
TARGET_PG_PASSWORD = os.environ.get("TARGET_POSTGRES_PASSWORD", "postgres")
TARGET_PG_DB = os.environ.get("TARGET_POSTGRES_DB", "target_app")


def _verify_auth(x_shared_secret: Optional[str], authorization: Optional[str]) -> None:
    token = x_shared_secret
    if not token and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    if SHARED_SECRET and token != SHARED_SECRET:
        logger.warning("Controller rejected unauthorized request")
        raise HTTPException(status_code=403, detail="Forbidden: invalid controller secret")


def _toxi_request(method: str, path: str, body: Optional[dict] = None, ignore_404: bool = False) -> dict:
    url = f"{TOXI_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and ignore_404:
            return {}
        err_body = exc.read().decode(errors="replace")
        logger.error(f"Toxiproxy {method} {path} failed: HTTP {exc.code} - {err_body}")
        raise HTTPException(status_code=502, detail=f"Toxiproxy error: {err_body}")
    except urllib.error.URLError as exc:
        logger.error(f"Toxiproxy connection failed at {TOXI_API}: {exc.reason}")
        raise HTTPException(status_code=503, detail=f"Cannot reach Toxiproxy: {exc.reason}")


class InjectRequest(BaseModel):
    latency_ms: int = 2000
    jitter_ms: int = 100
    trigger_traffic: bool = True
    traffic_count: int = 20


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "controller"}


@app.post("/inject")
async def post_inject(
    req: InjectRequest,
    x_shared_secret: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Injects latency toxic on payment-postgres proxy and drives traffic."""
    _verify_auth(x_shared_secret, authorization)

    # 1. Clear any existing toxic
    try:
        _toxi_request("DELETE", f"/proxies/{PROXY_NAME}/toxics/{TOXIC_NAME}", ignore_404=True)
    except Exception:
        pass

    # 2. Add latency toxic
    body = {
        "name": TOXIC_NAME,
        "type": "latency",
        "stream": "downstream",
        "toxicity": 1.0,
        "attributes": {
            "latency": req.latency_ms,
            "jitter": req.jitter_ms,
        },
    }
    toxic_res = _toxi_request("POST", f"/proxies/{PROXY_NAME}/toxics", body)
    logger.info(f"Injected toxic on '{PROXY_NAME}': {req.latency_ms}ms latency (+/- {req.jitter_ms}ms)")

    # 3. Optional: generate payment traffic to trigger pool saturation
    if req.trigger_traffic:
        asyncio.create_task(_send_payment_traffic(req.traffic_count))

    return {
        "status": "injected",
        "proxy": PROXY_NAME,
        "toxic": TOXIC_NAME,
        "latency_ms": req.latency_ms,
        "jitter_ms": req.jitter_ms,
    }


async def _send_payment_traffic(count: int = 20) -> None:
    """Sends background requests to /payment/charge."""
    import uuid
    # Within docker network or localhost
    targets = [
        "http://payment:8002/payment/charge",
        "http://localhost:8002/payment/charge",
        "http://127.0.0.1:8002/payment/charge",
    ]
    target_url = None
    for u in targets:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                r = await client.get(u.replace("/payment/charge", "/health"))
                if r.status_code == 200:
                    target_url = u
                    break
        except Exception:
            continue

    if not target_url:
        logger.warning("Could not reach payment service to drive background traffic")
        return

    logger.info(f"Driving {count} concurrent payment requests against {target_url}...")
    headers = {"Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=3.0) as client:
        tasks = []
        for i in range(count):
            body = {
                "order_id": f"ord-demo-{i}",
                "amount": 50.00,
                "currency": "USD",
                "idempotency_key": f"idemp-demo-{uuid.uuid4().hex[:12]}",
            }
            tasks.append(client.post(target_url, json=body, headers=headers))
        results = await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"Completed traffic burst: {len(results)} requests sent.")


@app.post("/reset")
def post_reset(
    x_shared_secret: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Clears all toxics and resets target app database tables."""
    _verify_auth(x_shared_secret, authorization)

    # 1. Clear toxics
    cleared = []
    try:
        toxics = _toxi_request("GET", f"/proxies/{PROXY_NAME}/toxics")
        for toxic in toxics:
            tname = toxic.get("name")
            if tname:
                _toxi_request("DELETE", f"/proxies/{PROXY_NAME}/toxics/{tname}", ignore_404=True)
                cleared.append(tname)
        logger.info(f"Cleared {len(cleared)} toxics from {PROXY_NAME}")
    except Exception as exc:
        logger.warning(f"Warning while clearing toxics: {exc}")

    # 2. Truncate target DB tables (payments, orders)
    pg_reset = False
    try:
        conn = psycopg2.connect(
            host=TARGET_PG_HOST,
            port=TARGET_PG_PORT,
            user=TARGET_PG_USER,
            password=TARGET_PG_PASSWORD,
            dbname=TARGET_PG_DB,
            connect_timeout=3,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE payments, orders RESTART IDENTITY CASCADE;")
        conn.close()
        pg_reset = True
        logger.info("Successfully truncated target DB payments & orders tables.")
    except Exception as pg_exc:
        logger.warning(f"Could not connect to target DB: {pg_exc}")

    # 3. Call surveyops-api /trial/reset (closes open incidents, clears dedup store & demo events)
    api_reset_result = {}
    try:
        headers = {"X-Shared-Secret": SHARED_SECRET}
        with httpx.Client(timeout=4.0) as client:
            resp = client.post(f"{SURVEYOPS_API_URL}/trial/reset", headers=headers)
            if resp.status_code == 200:
                api_reset_result = resp.json()
                logger.info("Successfully reset surveyops-api incidents & events.")
            else:
                logger.warning(f"surveyops-api /trial/reset returned HTTP {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.warning(f"Could not reach surveyops-api for trial reset: {exc}")

    return {
        "status": "reset",
        "cleared_toxics": cleared,
        "database_truncated": pg_reset,
        "surveyops_api_reset": api_reset_result,
    }


@app.get("/metrics/summary")
async def get_metrics_summary(
    request: Request,
    x_shared_secret: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """
    Returns named summary metrics for demo mini-strip.
    Rejects any arbitrary PromQL or unexpected query parameters.
    """
    _verify_auth(x_shared_secret, authorization)

    # Reject arbitrary PromQL parameters
    disallowed = {"query", "promql", "expr", "match[]", "select"}
    for k in request.query_params.keys():
        if k.lower() in disallowed:
            raise HTTPException(
                status_code=400,
                detail="Arbitrary PromQL queries are rejected. Only named summary metrics are supported.",
            )

    payment_p99_ms = 0.0
    pool_in_use = 0
    pool_capacity = 5
    alert_state = "normal"

    # 1. Query Prometheus
    async with httpx.AsyncClient(timeout=2.0) as client:
        # A. Payment P99
        try:
            p99_q = 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{handler=~".*payment.*"}[1m])))'
            r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": p99_q})
            if r.status_code == 200:
                data = r.json().get("data", {}).get("result", [])
                if data:
                    raw_val = float(data[0].get("value", [0, 0])[1])
                    if not (raw_val != raw_val):  # not NaN
                        payment_p99_ms = round(raw_val * 1000.0, 1)
        except Exception as exc:
            logger.debug(f"Prometheus p99 query failed: {exc}")

        # B. Pool in use & capacity
        try:
            r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": "payment_db_pool_in_use_connections"})
            if r.status_code == 200:
                data = r.json().get("data", {}).get("result", [])
                if data:
                    pool_in_use = int(float(data[0].get("value", [0, 0])[1]))

            r_cap = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": "payment_db_pool_capacity"})
            if r_cap.status_code == 200:
                data = r_cap.json().get("data", {}).get("result", [])
                if data:
                    pool_capacity = int(float(data[0].get("value", [0, 5])[1]))
        except Exception as exc:
            logger.debug(f"Prometheus pool metrics query failed: {exc}")

        # C. Alerts
        try:
            r_alerts = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
            if r_alerts.status_code == 200:
                alerts_list = r_alerts.json().get("data", {}).get("alerts", [])
                states = [a.get("state") for a in alerts_list if isinstance(a, dict)]
                if "firing" in states:
                    alert_state = "firing"
                elif "pending" in states:
                    alert_state = "pending"
                else:
                    alert_state = "normal"
        except Exception as exc:
            logger.debug(f"Prometheus alerts query failed: {exc}")

    # 2. Target Postgres query as backup / supplement for connection count
    try:
        conn = psycopg2.connect(
            host=TARGET_PG_HOST,
            port=TARGET_PG_PORT,
            user=TARGET_PG_USER,
            password=TARGET_PG_PASSWORD,
            dbname=TARGET_PG_DB,
            connect_timeout=2,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND state = 'active';",
                    (TARGET_PG_DB,),
                )
                row = cur.fetchone()
                if row and pool_in_use == 0:
                    pool_in_use = int(row[0])
        conn.close()
    except Exception:
        pass

    return {
        "payment_p99_ms": payment_p99_ms,
        "pool_in_use": pool_in_use,
        "pool_capacity": pool_capacity,
        "alert_state": alert_state,
    }

