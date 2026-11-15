"""
Observability bootstrap (requirement 9): exports LLM traces to a self-hosted
Langfuse instance.

Both agentic frameworks used in this project route their actual LLM calls
through `litellm` (ADK's `LiteLlm` wrapper for non-Gemini models, and CrewAI's
built-in litellm client), so a single litellm `CustomLogger` captures spans
for ADK and CrewAI alike (model, prompt/completion, token counts, latency,
cost) without needing separate per-framework instrumentation.

litellm's *built-in* `langfuse` callback requires langfuse SDK v4+, which is
OTel-based and only talks to Langfuse v3's `/api/public/otel` endpoint. This
project self-hosts Langfuse v2 (Postgres-only; v3 needs a ClickHouse/Redis/S3
stack whose MinIO image Docker Hub no longer serves anonymously), so instead
this module posts directly to Langfuse v2's stable batch ingestion API
(`/api/public/ingestion`), with no langfuse SDK dependency at all.

This is implemented as a `litellm.integrations.custom_logger.CustomLogger`
subclass registered via `litellm.callbacks`, not a plain function registered
via `litellm.success_callback`. litellm only invokes plain-function callbacks
on the *sync* `completion()` path (`litellm_logging.py`'s
`success_handler`/`failure_handler` gate `callable(callback)` on
`is_sync_request`); its *async* `acompletion()` path, which both ADK and
CrewAI actually use, only invokes `CustomLogger`/`CustomGuardrail` instances.
A plain-function callback registered via `success_callback` is therefore
silently never called for ADK/CrewAI's real traffic even though it fires for
a standalone sync `litellm.completion()` smoke test.

Call `init_observability(service_name)` once, as early as possible, in every
process that makes LLM calls (surveyops-api, runner, remediator).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("surveyops.observability")

_initialized = False


def init_observability(service_name: str) -> bool:
    """
    Registers a litellm CustomLogger that posts each LLM call (sync or async)
    to Langfuse's batch ingestion API, reading LANGFUSE_HOST/PUBLIC_KEY/
    SECRET_KEY from the environment. No-op (returns False) if those are not
    set, so local/dev runs without observability configured are unaffected.
    """
    global _initialized
    if _initialized:
        return True

    host = os.environ.get("LANGFUSE_HOST", "").strip()
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()

    if not host or not public_key or not secret_key:
        logger.warning(
            "Langfuse not configured (LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY unset); tracing disabled",
            extra={"extra_fields": {"service": service_name}},
        )
        return False

    import litellm

    litellm.callbacks = [_LangfuseIngestionLogger(host.rstrip("/"), (public_key, secret_key), service_name)]

    _initialized = True
    logger.info(
        "Observability initialized",
        extra={"extra_fields": {"service": service_name, "langfuse_host": host}},
    )
    return True


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return datetime.now(timezone.utc).isoformat()


class _LangfuseIngestionLogger(CustomLogger):
    """litellm CustomLogger that posts directly to Langfuse v2's ingestion API.

    Must subclass litellm's own CustomLogger: its async dispatch loop (in
    litellm_logging.py) gates on `isinstance(callback, CustomLogger)`, so a
    duck-typed object with the right method names is skipped.
    """

    def __init__(self, host: str, auth: tuple[str, str], service_name: str) -> None:
        self._host = host
        self._auth = auth
        self._service_name = service_name

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._emit(kwargs, response_obj, start_time, end_time, success=True)

    async def async_log_failure_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._emit(kwargs, response_obj, start_time, end_time, success=False)

    def log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._emit(kwargs, response_obj, start_time, end_time, success=True)

    def log_failure_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._emit(kwargs, response_obj, start_time, end_time, success=False)

    def _post_ingestion(self, events: list[dict[str, Any]]) -> None:
        try:
            requests.post(
                f"{self._host}/api/public/ingestion",
                json={"batch": events},
                auth=self._auth,
                timeout=5,
            )
        except Exception:
            logger.warning("Failed to post trace to Langfuse", exc_info=True)

    def _emit(self, kwargs: dict, completion_response: Any, start_time: Any, end_time: Any, success: bool) -> None:
        try:
            trace_id = str(uuid.uuid4())
            gen_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            model = kwargs.get("model", "unknown")
            messages = kwargs.get("messages", [])

            usage: dict[str, Any] = {}
            output: Any = None
            cost: float | None = None
            level = "DEFAULT"
            status_message = None

            if success and completion_response is not None:
                try:
                    u = completion_response.usage
                    usage = {
                        "input": u.prompt_tokens,
                        "output": u.completion_tokens,
                        "total": u.total_tokens,
                        "unit": "TOKENS",
                    }
                except Exception:
                    pass
                try:
                    output = completion_response.choices[0].message.content
                except Exception:
                    output = str(completion_response)
                try:
                    import litellm

                    cost = litellm.completion_cost(completion_response=completion_response)
                except Exception:
                    cost = None
            else:
                level = "ERROR"
                status_message = str(completion_response) if completion_response else "LLM call failed"

            metadata = {"service": self._service_name}
            if cost is not None:
                metadata["cost_usd"] = cost

            events = [
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": now,
                    "type": "trace-create",
                    "body": {
                        "id": trace_id,
                        "name": f"{self._service_name}:{model}",
                        "input": messages,
                        "output": output,
                        "tags": [self._service_name],
                        "metadata": metadata,
                    },
                },
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": now,
                    "type": "generation-create",
                    "body": {
                        "id": gen_id,
                        "traceId": trace_id,
                        "name": model,
                        "startTime": _iso(start_time),
                        "endTime": _iso(end_time),
                        "model": model,
                        "input": messages,
                        "output": output,
                        "usage": usage,
                        "level": level,
                        "statusMessage": status_message,
                        "metadata": metadata,
                    },
                },
            ]
            self._post_ingestion(events)
        except Exception:
            logger.warning("Failed to build/send Langfuse trace", exc_info=True)
