"""
SurveyOps API stub: POST /investigate
Receives Alertmanager webhooks, checks shared secret, parses InvestigatePayload, logs it.
Includes incident deduplication: one open incident per (service, endpoint).
No LLM calls. No agent startup. Step 3 stub.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from threading import Lock
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

# ── Structured logging ──────────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
INCIDENT_EXPIRY_SECONDS = int(os.environ.get("INCIDENT_EXPIRY_SECONDS", "900"))  # default: 15 min TTL


def _verify_auth(x_shared_secret: str | None, authorization: str | None) -> None:
    """Verifies X-Shared-Secret or Authorization: Bearer <token>. Raises 403 on mismatch."""
    received_secret = x_shared_secret
    if not received_secret and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            received_secret = parts[1].strip()

    if SHARED_SECRET and received_secret != SHARED_SECRET:
        logger.warning("Rejected API call: bad or missing shared secret")
        raise HTTPException(status_code=403, detail="Forbidden: bad shared secret")


# ── InvestigatePayload (matching docs/04-CONTRACTS.md & surveyops/schemas) ───
try:
    from surveyops.schemas import InvestigatePayload
except ImportError:
    class InvestigatePayload(BaseModel):
        """Symptom-only payload. NEVER add root-cause or fault labels here."""
        alert: str
        service: str
        endpoint: str
        metric: str
        observed: float | None = None
        threshold: float
        window_start: datetime


# ── Incident Deduplication Store (In-memory) ─────────────────────────────────
# TODO: Persist incident records and lock state to PostgreSQL incidents table
# once database migrations and schema management are implemented in Phase 2.
class IncidentDedupStore:
    """
    Tracks active incidents by (service, endpoint) in memory.
    Enforces at most one open incident per (service, endpoint) simultaneously.
    Auto-expires stale incidents after expiry_seconds (default 15m) to prevent permanent blocking.
    """
    def __init__(self, expiry_seconds: int = INCIDENT_EXPIRY_SECONDS) -> None:
        self._lock = Lock()
        self._open_incidents: dict[tuple[str, str], str] = {}
        self._incidents: dict[str, dict] = {}
        self.expiry_seconds = expiry_seconds

    def _purge_expired(self, now: datetime) -> None:
        """Auto-close any incident that exceeded expiry_seconds without a close call."""
        for key, inc_id in list(self._open_incidents.items()):
            inc = self._incidents.get(inc_id)
            if inc and inc.get("opened_at"):
                try:
                    opened_dt = datetime.fromisoformat(inc["opened_at"])
                    if (now - opened_dt).total_seconds() > self.expiry_seconds:
                        del self._open_incidents[key]
                        inc["status"] = "expired"
                        inc["closed_at"] = now.isoformat()
                        logger.warning(
                            "Auto-closed expired incident",
                            extra={"extra_fields": {
                                "incident_id": inc_id,
                                "service": key[0],
                                "endpoint": key[1],
                                "stuck_duration_s": (now - opened_dt).total_seconds(),
                            }},
                        )
                except Exception:
                    pass

    def is_open(self, service: str, endpoint: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._purge_expired(now)
            return (service, endpoint) in self._open_incidents

    def get_open_incident_id(self, service: str, endpoint: str) -> str | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._purge_expired(now)
            return self._open_incidents.get((service, endpoint))

    def open_incident(self, service: str, endpoint: str, payload_data: dict) -> tuple[str, bool]:
        """
        Attempts to open an incident for (service, endpoint).
        Returns (incident_id, is_new):
        - If already open: (existing_incident_id, False)
        - If new: (new_incident_id, True)
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            self._purge_expired(now)
            key = (service, endpoint)
            if key in self._open_incidents:
                return self._open_incidents[key], False

            incident_id = f"inc-{uuid.uuid4().hex[:8]}"
            self._open_incidents[key] = incident_id
            self._incidents[incident_id] = {
                "incident_id": incident_id,
                "service": service,
                "endpoint": endpoint,
                "opened_at": now.isoformat(),
                "closed_at": None,
                "status": "open",
                "payload": payload_data,
            }
            return incident_id, True

    def close_incident(self, service: str, endpoint: str) -> str | None:
        """Closes an open incident for (service, endpoint). Returns closed incident_id or None."""
        with self._lock:
            key = (service, endpoint)
            incident_id = self._open_incidents.pop(key, None)
            if incident_id and incident_id in self._incidents:
                self._incidents[incident_id]["status"] = "closed"
                self._incidents[incident_id]["closed_at"] = datetime.now(timezone.utc).isoformat()
            return incident_id

    def close_incident_by_id(self, incident_id: str) -> bool:
        """Closes an open incident by incident_id. Returns True if closed, False if not open."""
        with self._lock:
            for key, inc_id in list(self._open_incidents.items()):
                if inc_id == incident_id:
                    del self._open_incidents[key]
                    if incident_id in self._incidents:
                        self._incidents[incident_id]["status"] = "closed"
                        self._incidents[incident_id]["closed_at"] = datetime.now(timezone.utc).isoformat()
                    return True
            return False

    def list_open(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._purge_expired(now)
            return [self._incidents[inc_id] for inc_id in self._open_incidents.values()]

    def clear(self) -> None:
        """Clear all active and historical incidents (for test/trial reset)."""
        with self._lock:
            self._open_incidents.clear()
            self._incidents.clear()


dedup_store = IncidentDedupStore()


# Explicit functions to close incidents
def close_incident(service: str, endpoint: str) -> str | None:
    """Closes an open incident for (service, endpoint)."""
    closed_id = dedup_store.close_incident(service, endpoint)
    if closed_id:
        logger.info(
            "Closed incident",
            extra={"extra_fields": {"incident_id": closed_id, "service": service, "endpoint": endpoint}},
        )
    return closed_id


def close_incident_by_id(incident_id: str) -> bool:
    """Closes an open incident by incident_id."""
    success = dedup_store.close_incident_by_id(incident_id)
    if success:
        logger.info("Closed incident by id", extra={"extra_fields": {"incident_id": incident_id}})
    return success


# ── Alertmanager mapping helpers ─────────────────────────────────────────────
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
    Returns None for alerts that cannot be mapped.
    """
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "")
    handler = labels.get("handler", "")
    starts_at = alert.get("startsAt", "")

    if not alertname or not handler:
        logger.warning(
            "Alert missing alertname or handler label, skipping",
            extra={"extra_fields": {"labels": labels}},
        )
        return None

    service = _service_from_handler(handler)
    metric = _ALERT_TO_METRIC.get(alertname, "unknown_metric")
    threshold = _ALERT_TO_THRESHOLD.get(alertname, 0.0)

    try:
        window_start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        window_start = datetime.now(timezone.utc)

    # Reconciled: Alertmanager omits scalar observed float; set to None.
    # Diagnostician will query Prometheus directly at runtime.
    observed = None

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


@app.get("/incidents/open")
def get_open_incidents(
    x_shared_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    _verify_auth(x_shared_secret, authorization)
    return {"open_incidents": dedup_store.list_open()}


class CloseIncidentRequest(BaseModel):
    service: str | None = None
    endpoint: str | None = None
    incident_id: str | None = None


@app.post("/incidents/close")
def post_close_incident(
    req: CloseIncidentRequest,
    x_shared_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    _verify_auth(x_shared_secret, authorization)
    if req.incident_id:
        closed = close_incident_by_id(req.incident_id)
        if not closed:
            raise HTTPException(status_code=404, detail="Incident not found or already closed")
        return {"status": "closed", "incident_id": req.incident_id}
    if req.service and req.endpoint:
        closed_id = close_incident(req.service, req.endpoint)
        if not closed_id:
            raise HTTPException(status_code=404, detail="No open incident for this service and endpoint")
        return {"status": "closed", "incident_id": closed_id}
    raise HTTPException(status_code=400, detail="Must provide incident_id or both service and endpoint")


ENABLE_BACKGROUND_DIAGNOSTICIAN = os.environ.get("ENABLE_BACKGROUND_DIAGNOSTICIAN", "false").lower() in ("true", "1")


async def _run_diagnostician_background(incident_id: str, payload: InvestigatePayload) -> None:
    """Runs Diagnostician in the background and closes the incident in a finally block."""
    logger.info(
        "Starting background Diagnostician run",
        extra={"extra_fields": {"incident_id": incident_id, "endpoint": payload.endpoint}},
    )
    try:
        from surveyops.agents.diagnostician import run_diagnostician
        diag_start = time.time()
        diagnosis = await run_diagnostician(incident=payload, incident_id=incident_id)
        diag_dur = time.time() - diag_start
        logger.info(
            "Diagnostician run completed",
            extra={"extra_fields": {"incident_id": incident_id, "outcome": diagnosis.outcome.value}},
        )

        # Generate deterministic IncidentReport and save to DB and eval/reports/
        try:
            from surveyops.report import generate_and_save_report
            from surveyops.agents.diagnostician.agent import (
                get_incident_ledger,
                get_incident_run_record,
            )
            ledger = get_incident_ledger(incident_id)
            run_record = get_incident_run_record(incident_id)
            timings = {
                "detect": 60.0,
                "diagnose": diag_dur,
                "total": diag_dur + 60.0,
            }
            usage = {
                "prompt_tokens": run_record.get("prompt_tokens", 0),
                "completion_tokens": run_record.get("completion_tokens", 0),
                "total_tokens": run_record.get("total_tokens", 0),
            }
            report, json_path, md_path = generate_and_save_report(
                incident_id=incident_id,
                diagnosis=diagnosis,
                evidence_trail=ledger,
                timings=timings,
                usage=usage,
            )
            logger.info(
                f"Generated and saved incident report for {incident_id} to {json_path} and {md_path}",
                extra={"extra_fields": {"incident_id": incident_id, "report_json": str(json_path)}},
            )
        except Exception as rep_exc:
            logger.warning(f"Report generation failed (non-fatal): {rep_exc}")
    except Exception as exc:
        logger.error(
            f"Diagnostician background run failed for {incident_id}: {exc}",
            extra={"extra_fields": {"incident_id": incident_id, "error": str(exc)}},
            exc_info=True,
        )
    finally:
        closed = dedup_store.close_incident_by_id(incident_id)
        logger.info(
            "Closed incident in finally block",
            extra={"extra_fields": {"incident_id": incident_id, "closed": closed}},
        )


@app.post("/investigate", status_code=200)
async def investigate(
    request: Request,
    x_shared_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    """
    Receives Alertmanager webhook. Checks shared secret. Parses firing alerts into
    InvestigatePayload (symptom only). Deduplicates by (service, endpoint).
    Starts Diagnostician in background for newly opened incidents.
    """
    # 1. Auth check
    _verify_auth(x_shared_secret, authorization)

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
            "groupLabels": raw_body.get("groupLabels", {}),
        }},
    )

    # 3. Process firing alerts with deduplication
    accepted: list[dict] = []
    ignored_duplicates: list[dict] = []

    for alert_obj in raw_body.get("alerts", []):
        if alert_obj.get("status") != "firing":
            continue

        payload = _parse_alert_to_payload(alert_obj)
        if payload is None:
            continue

        try:
            validated = InvestigatePayload.model_validate(payload.model_dump())
        except ValidationError as exc:
            logger.error(
                "InvestigatePayload validation failed",
                extra={"extra_fields": {"error": str(exc), "alert": alert_obj.get("labels", {})}},
            )
            continue

        # Deduplication check
        incident_id, is_new = dedup_store.open_incident(
            validated.service,
            validated.endpoint,
            validated.model_dump(mode="json"),
        )

        if not is_new:
            logger.info(
                "Duplicate alert ignored for already open incident",
                extra={"extra_fields": {
                    "incident_id": incident_id,
                    "service": validated.service,
                    "endpoint": validated.endpoint,
                    "alert": validated.alert,
                }},
            )
            ignored_duplicates.append({
                "incident_id": incident_id,
                "service": validated.service,
                "endpoint": validated.endpoint,
                "alert": validated.alert,
            })
            continue

        logger.info(
            "Opened new incident for symptom-only payload",
            extra={"extra_fields": {
                "incident_id": incident_id,
                "payload": validated.model_dump(mode="json"),
            }},
        )
        accepted_item = validated.model_dump(mode="json")
        accepted_item["incident_id"] = incident_id
        accepted.append(accepted_item)

        # Trigger background Diagnostician run (Requirement 9)
        if ENABLE_BACKGROUND_DIAGNOSTICIAN:
            asyncio.create_task(_run_diagnostician_background(incident_id, validated))

    return {
        "accepted": len(accepted),
        "payloads": accepted,
        "ignored_duplicates": ignored_duplicates,
    }

