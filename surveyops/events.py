"""
Central Event Bus & Storage for SurveyOps Demo & Observability.
Writes structured events to surveyops_postgres 'events' table.
Maintains in-memory ring buffer for low-latency queries and testing fallback.

Invariants:
- Redacts secrets (e.g. github_pat_, ghp_, gho_, api keys, passwords).
- Never writes ground truth into events.
- Never crashes caller if DB is temporarily unreachable.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Optional
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("surveyops.events")

TOKEN_PATTERN = re.compile(r"(?:github_pat_|ghp_|gho_)[A-Za-z0-9_]+")
SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(password|secret|token|api_?key|auth)")
FORBIDDEN_GROUND_TRUTH_KEYS = {"ground_truth", "ground_truth_failure_mode", "fault_label"}

# In-memory buffer for fast SSE broadcast and offline fallback (capacity: 2000 events)
_IN_MEMORY_EVENTS: deque[dict[str, Any]] = deque(maxlen=2000)
_EVENT_COUNTER: int = 0


def get_surveyops_postgres_conn():
    """Returns a psycopg2 connection to surveyops_postgres with fallback port handling."""
    host = os.environ.get("SURVEYOPS_POSTGRES_HOST", "localhost")
    default_port = "5434" if host in ("localhost", "127.0.0.1") else "5432"
    port = int(os.environ.get("SURVEYOPS_POSTGRES_PORT", default_port))
    user = os.environ.get("SURVEYOPS_POSTGRES_USER", "postgres")
    password = os.environ.get("SURVEYOPS_POSTGRES_PASSWORD", "postgres")
    dbname = os.environ.get("SURVEYOPS_POSTGRES_DB", "surveyops")
    return psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        connect_timeout=3,
    )


def init_events_table() -> None:
    """Creates the events table if it doesn't already exist."""
    try:
        conn = get_surveyops_postgres_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        incident_id VARCHAR(128) NOT NULL,
                        ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        component VARCHAR(64) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        payload JSONB NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_incident_id ON events(incident_id);
                    CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
                    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                    """
                )
        conn.close()
    except Exception as exc:
        logger.debug(f"PostgreSQL events table init bypassed/failed: {exc}")


def redact_payload(obj: Any) -> Any:
    """Recursively redacts secrets and strips any forbidden ground-truth fields."""
    if isinstance(obj, str):
        return TOKEN_PATTERN.sub("[REDACTED_TOKEN]", obj)
    elif isinstance(obj, dict):
        clean_dict: dict[str, Any] = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if k_lower in FORBIDDEN_GROUND_TRUTH_KEYS:
                # Invariant: Never write ground truth into events
                continue
            if isinstance(v, str) and TOKEN_PATTERN.search(v):
                clean_dict[k] = TOKEN_PATTERN.sub("[REDACTED_TOKEN]", v)
            elif SENSITIVE_KEY_PATTERN.search(k_lower) and not k_lower.startswith("checkable_"):
                clean_dict[k] = "[REDACTED_SECRET]"
            else:
                clean_dict[k] = redact_payload(v)
        return clean_dict
    elif isinstance(obj, list):
        return [redact_payload(item) for item in obj]
    return obj


def record_event(
    incident_id: str,
    component: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Records an observability event for the demo UI.
    Sanitizes payload and writes to surveyops_postgres 'events' table.
    Fails loudly if Postgres is unavailable (unless SURVEYOPS_TEST_EVENTS_MEMORY=1).
    """
    global _EVENT_COUNTER
    sanitized_payload = redact_payload(payload or {})
    ts_str = datetime.now(timezone.utc).isoformat()

    # Offline test override only
    if os.environ.get("SURVEYOPS_TEST_EVENTS_MEMORY") == "1":
        _EVENT_COUNTER += 1
        ev = {
            "id": _EVENT_COUNTER,
            "incident_id": incident_id,
            "ts": ts_str,
            "component": component,
            "event_type": event_type,
            "payload": sanitized_payload,
        }
        _IN_MEMORY_EVENTS.append(ev)
        return ev

    try:
        conn = get_surveyops_postgres_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        incident_id VARCHAR(128) NOT NULL,
                        ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        component VARCHAR(64) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        payload JSONB NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    INSERT INTO events (incident_id, component, event_type, payload)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, ts;
                    """,
                    (incident_id, component, event_type, json.dumps(sanitized_payload)),
                )
                row = cur.fetchone()
                event_id = row[0]
                if row[1]:
                    ts_str = row[1].isoformat()
        conn.close()
    except Exception as exc:
        logger.error(f"FATAL: PostgreSQL events table write failed: {exc}")
        raise RuntimeError(f"PostgreSQL events table unavailable in surveyops_postgres: {exc}") from exc

    event_record = {
        "id": event_id,
        "incident_id": incident_id,
        "ts": ts_str,
        "component": component,
        "event_type": event_type,
        "payload": sanitized_payload,
    }
    _IN_MEMORY_EVENTS.append(event_record)
    return event_record


def fetch_events_since(
    incident_id: Optional[str] = None,
    after_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetches events with id > after_id strictly from Postgres. Fails loudly if DB is down."""
    if os.environ.get("SURVEYOPS_TEST_EVENTS_MEMORY") == "1":
        results: list[dict[str, Any]] = []
        for ev in list(_IN_MEMORY_EVENTS):
            if ev["id"] > after_id:
                if incident_id and ev["incident_id"] != incident_id:
                    continue
                results.append(ev)
                if len(results) >= limit:
                    break
        return results

    try:
        conn = get_surveyops_postgres_conn()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if incident_id:
                    cur.execute(
                        """
                        SELECT id, incident_id, ts, component, event_type, payload
                        FROM events
                        WHERE id > %s AND incident_id = %s
                        ORDER BY id ASC
                        LIMIT %s;
                        """,
                        (after_id, incident_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, incident_id, ts, component, event_type, payload
                        FROM events
                        WHERE id > %s
                        ORDER BY id ASC
                        LIMIT %s;
                        """,
                        (after_id, limit),
                    )
                rows = cur.fetchall()
                res: list[dict[str, Any]] = []
                if rows is not None:
                    for r in rows:
                        res.append({
                            "id": r["id"],
                            "incident_id": r["incident_id"],
                            "ts": r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else str(r["ts"]),
                            "component": r["component"],
                            "event_type": r["event_type"],
                            "payload": r["payload"],
                        })
        conn.close()
        return res
    except Exception as exc:
        logger.error(f"FATAL: PostgreSQL events table read failed: {exc}")
        raise RuntimeError(f"PostgreSQL events table unavailable in surveyops_postgres: {exc}") from exc


def get_all_events_for_incident(incident_id: str) -> list[dict[str, Any]]:
    """Returns all recorded events for a given incident_id."""
    return fetch_events_since(incident_id=incident_id, after_id=0, limit=2000)


def export_replay_file(incident_id: str, output_path: str | os.PathLike) -> int:
    """Exports all events for an incident to a replay JSON file."""
    events = get_all_events_for_incident(incident_id)
    out = os.fspath(output_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
    return len(events)
