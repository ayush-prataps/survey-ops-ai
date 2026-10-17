"""
SurveyOps API stub: POST /investigate
Receives Alertmanager webhooks, checks shared secret, parses InvestigatePayload, logs it.
No LLM calls. No agent startup. Step 2 stub only.
"""

from datetime import datetime
import json
import logging
import os
import sys

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

# ── Structured logging ──────────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "service": "surveyops-api",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        obj.update(getattr(record, "extra_fields", {}))
        return json.dumps(obj)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logger = logging.getLogger("surveyops_api")
logger.setLevel(logging.INFO)
logger.handlers = [_handler]
logger.propagate = False

# ── Config ───────────────────────────────────────────────────────────────────
SHARED_SECRET = os.environ.get("INVESTIGATE_SHARED_SECRET", "")

# ── InvestigatePayload (from docs/04-CONTRACTS.md) ───────────────────────────
class InvestigatePayload(BaseModel):
    """Symptom-only payload. NEVER add root-cause or fault labels here."""
    alert: str
    service: str
    endpoint: str
    metric: str
    observed: float
    threshold: float
    window_start: datetime


# ── Alertmanager webhook shape (for reference, not persisted) ─────────────────
# Alertmanager POSTs:
# {
#   "version": "4",
#   "groupLabels": {"alertname": "...", "handler": "..."},
#   "alerts": [
#     {
#       "status": "firing",
#       "labels": {"alertname": "HighP99Latency", "handler": "/payment/charge", ...},
#       "annotations": {"description": "p99 latency is X on handler Y"},
#       "startsAt": "2026-10-17T10:00:00Z",
#       "endsAt":   "0001-01-01T00:00:00Z"
#     }
#   ]
# }
#
# GAP REPORT vs InvestigatePayload:
#   - alert       -> labels.alertname                               ✓ present
#   - service     -> NOT in Alertmanager payload. Inferred from handler label (e.g. "/payment/charge" -> "payment")
#   - endpoint    -> labels.handler                                 ✓ present
#   - metric      -> NOT in Alertmanager payload. Inferred from alertname
#                    ("HighP99Latency" -> "http_request_duration_seconds", "HighErrorRate" -> "http_requests_total")
#   - observed    -> NOT in Alertmanager payload. Alertmanager does not include the numeric observed value.
#                    Will log as -1.0 (sentinel) until Prometheus query fills it at diagnosis time.
#   - threshold   -> NOT in Alertmanager payload. Hardcoded from alert rule (0.5s / 0.05).
#   - window_start -> labels.startsAt or groupLabels                ✓ present as RFC3339 string


_ALERT_TO_METRIC = {
    "HighP99Latency": "http_request_duration_seconds",
    "HighErrorRate": "http_requests_total",
}
_ALERT_TO_THRESHOLD = {
    "HighP99Latency": 0.5,
    "HighErrorRate": 0.05,
}


def _service_from_handler(handler: str) -> str:
    """Infer service name from handler path prefix (e.g. '/payment/charge' -> 'payment')."""
    parts = handler.strip("/").split("/")
    return parts[0] if parts else "unknown"


def _parse_alert_to_payload(alert: dict) -> InvestigatePayload | None:
    """
    Map a single Alertmanager alert object to InvestigatePayload.
    Returns None for alerts that cannot be mapped (e.g. resolved alerts).
    Gaps are logged explicitly.
    """
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "")
    handler = labels.get("handler", "")
    starts_at = alert.get("startsAt", "")

    if not alertname or not handler:
        logger.warning("Alert missing alertname or handler label, skipping",
                       extra={"extra_fields": {"labels": labels}})
        return None

    service = _service_from_handler(handler)
    metric = _ALERT_TO_METRIC.get(alertname, "unknown_metric")
    threshold = _ALERT_TO_THRESHOLD.get(alertname, 0.0)

    try:
        window_start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        window_start = datetime.utcnow()

    # GAP: Alertmanager does not carry the numeric observed value.
    # Using -1.0 as sentinel; Diagnostician will query Prometheus at runtime.
    observed = -1.0

    return InvestigatePayload(
        alert=alertname,
        service=service,
        endpoint=handler,
        metric=metric,
        observed=observed,
        threshold=threshold,
        window_start=window_start,
    )


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="SurveyOps API", version="0.1.0-stub")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "surveyops-api"}


@app.post("/investigate", status_code=200)
async def investigate(
    request: Request,
    x_shared_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    """
    Receives Alertmanager webhook. Checks shared secret. Parses each firing alert
    into InvestigatePayload (symptom only). Logs and returns 200.

    This is the stub implementation: no agent is started.
    """
    # 1. Auth check (accepts X-Shared-Secret or Authorization: Bearer <token>)
    received_secret = x_shared_secret
    if not received_secret and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            received_secret = parts[1].strip()

    if SHARED_SECRET and received_secret != SHARED_SECRET:
        logger.warning("Rejected /investigate call: bad or missing shared secret")
        raise HTTPException(status_code=403, detail="Forbidden: bad shared secret")

    # 2. Parse raw Alertmanager body
    try:
        raw_body = await request.json()
    except Exception as exc:
        logger.error(f"Failed to parse webhook JSON body: {exc}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    logger.info(
        "Received Alertmanager webhook",
        extra={"extra_fields": {
            "version": raw_body.get("version"),
            "status": raw_body.get("status"),
            "num_alerts": len(raw_body.get("alerts", [])),
            "group_labels": raw_body.get("groupLabels", {}),
        }},
    )

    # 3. Map each firing alert to InvestigatePayload and log it
    accepted: list[dict] = []
    for alert_obj in raw_body.get("alerts", []):
        if alert_obj.get("status") != "firing":
            continue  # skip resolved alerts (send_resolved=false, but guard anyway)

        payload = _parse_alert_to_payload(alert_obj)
        if payload is None:
            continue

        try:
            # Validate roundtrip (catches any future schema drift)
            validated = InvestigatePayload.model_validate(payload.model_dump())
        except ValidationError as exc:
            logger.error(
                "InvestigatePayload validation failed",
                extra={"extra_fields": {"error": str(exc), "alert": alert_obj.get("labels", {})}},
            )
            continue

        logger.info(
            "Parsed symptom-only InvestigatePayload",
            extra={"extra_fields": validated.model_dump(mode="json")},
        )
        accepted.append(validated.model_dump(mode="json"))

    return {"accepted": len(accepted), "payloads": accepted}
