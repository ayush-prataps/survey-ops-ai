"""
Callbacks for the Remediator agent (CrewAI).
Provides step_callback and task_callback logging structured JSON to disk/database/logger.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
from surveyops.agents.remediator.indexer import get_db_url

logger = logging.getLogger("surveyops.remediator.callbacks")


def init_callback_logs_table(db_url: Optional[str] = None) -> None:
    """Initializes the remediator_callback_logs table in surveyops_postgres."""
    url = db_url or get_db_url()
    try:
        conn = psycopg2.connect(url)
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS remediator_callback_logs (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(32) NOT NULL,
                    agent_name VARCHAR(64) NOT NULL,
                    tool_name VARCHAR(128),
                    latency_ms DOUBLE PRECISION,
                    errors TEXT,
                    token_usage JSONB,
                    details JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"Could not initialize remediator_callback_logs table: {exc}")


class RemediatorCallbackHandler:
    """CrewAI step and task callback handler recording structured JSON."""

    def __init__(self, db_url: Optional[str] = None, incident_id: Optional[str] = None) -> None:
        self.db_url = db_url or get_db_url()
        self.incident_id = incident_id or "remediator"
        self.step_logs: list[dict[str, Any]] = []
        self.task_logs: list[dict[str, Any]] = []
        self._step_start_time: float = time.time()
        init_callback_logs_table(self.db_url)

    def on_step(self, step_output: Any) -> None:
        """CrewAI step_callback: invoked after each step/tool execution."""
        latency = (time.time() - self._step_start_time) * 1000.0
        self._step_start_time = time.time()

        tool_name = None
        tool_input = None
        tool_output = None
        error_msg = None

        if hasattr(step_output, "tool"):
            tool_name = getattr(step_output, "tool", None)
            tool_input = str(getattr(step_output, "tool_input", ""))
            tool_output = str(getattr(step_output, "result", ""))
        elif isinstance(step_output, tuple) and len(step_output) >= 2:
            tool_name = str(step_output[0])
            tool_output = str(step_output[1])
        elif isinstance(step_output, dict):
            tool_name = step_output.get("tool")
            tool_input = step_output.get("tool_input")
            tool_output = step_output.get("result")

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "step",
            "agent_name": "Remediator",
            "tool_name": tool_name,
            "latency_ms": round(latency, 2),
            "errors": error_msg,
            "token_usage": {},
            "details": {
                "tool_input": tool_input[:200] if tool_input else None,
                "tool_output_snippet": tool_output[:200] if tool_output else None,
            },
        }

        self.step_logs.append(record)
        logger.info(f"Remediator step: {json.dumps(record)}")
        self._persist(record)

    def on_task(self, task_output: Any) -> None:
        """CrewAI task_callback: invoked upon task completion."""
        raw_output = getattr(task_output, "raw", str(task_output))
        token_usage = getattr(task_output, "token_usage", {}) or {}

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "task",
            "agent_name": "Remediator",
            "tool_name": None,
            "latency_ms": 0.0,
            "errors": None,
            "token_usage": token_usage if isinstance(token_usage, dict) else {},
            "details": {
                "output_length": len(raw_output) if raw_output else 0,
                "output_snippet": raw_output[:200] if raw_output else "",
            },
        }

        self.task_logs.append(record)
        logger.info(f"Remediator task completed: {json.dumps(record)}")
        self._persist(record)

    def _persist(self, record: dict[str, Any]) -> None:
        """Persists structured callback log to surveyops_postgres and events."""
        try:
            from surveyops.events import record_event
            record_event(
                self.incident_id,
                "remediator",
                record["event_type"],
                record,
            )
        except Exception:
            pass

        try:
            conn = psycopg2.connect(self.db_url)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO remediator_callback_logs (
                        event_type, agent_name, tool_name, latency_ms, errors, token_usage, details
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        record["event_type"],
                        record["agent_name"],
                        record.get("tool_name"),
                        record.get("latency_ms", 0.0),
                        record.get("errors"),
                        json.dumps(record.get("token_usage", {})),
                        json.dumps(record.get("details", {})),
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug(f"Could not persist callback log to DB: {exc}")
