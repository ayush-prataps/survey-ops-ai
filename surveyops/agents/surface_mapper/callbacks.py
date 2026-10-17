from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Any, Optional


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "event": getattr(record, "event", None),
            "task": getattr(record, "task", None),
            "latency_ms": getattr(record, "latency_ms", None),
            "summary": getattr(record, "summary", None),
            "error": getattr(record, "error", None),
        }
        return json.dumps(log_obj)


logger = logging.getLogger("surface_mapper.callbacks")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class SurfaceMapperCallbackHandler:
    def __init__(self):
        self.logs: list[dict[str, Any]] = []
        self._start_time: float = time.perf_counter()

    def reset_timer(self):
        self._start_time = time.perf_counter()

    def task_callback(self, output: Any):
        """CrewAI task_callback logging structured JSON."""
        duration_ms = round((time.perf_counter() - self._start_time) * 1000, 2)
        task_desc = getattr(output, "description", None) or getattr(output, "name", "surface_mapper_task")
        raw_output = getattr(output, "raw", None) or str(output)
        summary = str(getattr(output, "summary", None) or raw_output[:250])
        error = getattr(output, "error", None)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "task_completion",
            "task": task_desc[:80] if task_desc else None,
            "latency_ms": duration_ms,
            "summary": summary,
            "error": str(error) if error else None,
        }
        self.logs.append(record)

        logger.info(
            f"Task completed in {duration_ms}ms",
            extra={
                "event": "task_completion",
                "task": record["task"],
                "latency_ms": duration_ms,
                "summary": summary,
                "error": record["error"],
            },
        )

    def step_callback(self, step_output: Any):
        """CrewAI step_callback logging structured JSON."""
        duration_ms = round((time.perf_counter() - self._start_time) * 1000, 2)
        summary = str(step_output)[:200]

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "step_executed",
            "task": "surface_mapper_step",
            "latency_ms": duration_ms,
            "summary": summary,
            "error": None,
        }
        self.logs.append(record)

        logger.info(
            "Agent executed step",
            extra={
                "event": "step_executed",
                "task": "surface_mapper_step",
                "latency_ms": duration_ms,
                "summary": summary,
                "error": None,
            },
        )
