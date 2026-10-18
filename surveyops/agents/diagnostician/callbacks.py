"""
Observability and Monitoring Callbacks for Diagnostician.
Satisfies Requirement 7 & Addendum:
- Structured JSON logging for before/after agent, model, and tool callbacks.
- Fields: incident_id, iteration, hypothesis_ids, request_id, tokens, latency, errors.
- TokenUsageTracker tracking tokens per incident.
- Rate-limit 429 detection and exponential backoff in on_model_error_callback.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Any
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from .cache import global_llm_cache

logger = logging.getLogger("surveyops.diagnostician")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _log_event(event_type: str, data: dict[str, Any]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": "diagnostician",
        "callback": event_type,
    }
    payload.update(data)
    logger.info(json.dumps(payload))


class TokenUsageTracker:
    """Tracks token consumption per incident for audit and cost analysis."""

    def __init__(self) -> None:
        self._usage: dict[str, dict[str, int]] = {}

    def add_tokens(self, incident_id: str, prompt_tokens: int, completion_tokens: int, total_tokens: int | None = None) -> None:
        if incident_id not in self._usage:
            self._usage[incident_id] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        tot = total_tokens if total_tokens is not None else (prompt_tokens + completion_tokens)
        self._usage[incident_id]["prompt_tokens"] += prompt_tokens
        self._usage[incident_id]["completion_tokens"] += completion_tokens
        self._usage[incident_id]["total_tokens"] += tot

    def get_usage(self, incident_id: str) -> dict[str, int]:
        return self._usage.get(incident_id, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def reset(self, incident_id: str | None = None) -> None:
        if incident_id:
            self._usage.pop(incident_id, None)
        else:
            self._usage.clear()


token_tracker = TokenUsageTracker()
_model_call_start_times: dict[str, float] = {}
_model_call_requests: dict[str, LlmRequest] = {}


def diagnostician_before_agent_callback(ctx: CallbackContext) -> None:
    state = getattr(ctx, "state", {}) or {}
    incident = state.get("incident")
    incident_id = state.get("incident_id") or (getattr(incident, "incident_id", None) if incident else None) or "unknown"
    iteration = state.get("iteration", 1)

    _log_event("before_agent", {
        "agent_name": ctx.agent_name,
        "incident_id": incident_id,
        "iteration": iteration,
    })


def diagnostician_after_agent_callback(ctx: CallbackContext) -> None:
    state = getattr(ctx, "state", {}) or {}
    incident = state.get("incident")
    incident_id = state.get("incident_id") or (getattr(incident, "incident_id", None) if incident else None) or "unknown"
    iteration = state.get("iteration", 1)

    _log_event("after_agent", {
        "agent_name": ctx.agent_name,
        "incident_id": incident_id,
        "iteration": iteration,
    })


def diagnostician_before_model_callback(ctx: Context, request: LlmRequest) -> LlmResponse | None:
    state = getattr(ctx.session, "state", {}) if hasattr(ctx, "session") else {}
    incident = state.get("incident")
    incident_id = state.get("incident_id") or (getattr(incident, "incident_id", None) if incident else None) or "unknown"
    iteration = state.get("iteration", 1)

    call_id = f"{incident_id}-{iteration}"
    _model_call_start_times[call_id] = time.perf_counter()
    _model_call_requests[call_id] = request

    _log_event("before_model", {
        "model": request.model,
        "incident_id": incident_id,
        "iteration": iteration,
    })

    # Record/Replay cache check
    cached = global_llm_cache.get_cached_response(request)
    if cached is not None:
        _log_event("model_cache_hit", {
            "model": request.model,
            "incident_id": incident_id,
            "iteration": iteration,
        })
        return cached

    return None


def diagnostician_after_model_callback(ctx: Context, response: LlmResponse) -> LlmResponse | None:
    state = getattr(ctx.session, "state", {}) if hasattr(ctx, "session") else {}
    incident = state.get("incident")
    incident_id = state.get("incident_id") or (getattr(incident, "incident_id", None) if incident else None) or "unknown"
    iteration = state.get("iteration", 1)

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    if response and response.usage_metadata:
        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        completion_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        total_tokens = getattr(response.usage_metadata, "total_token_count", 0) or (prompt_tokens + completion_tokens)

    token_tracker.add_tokens(incident_id, prompt_tokens, completion_tokens, total_tokens)

    call_id = f"{incident_id}-{iteration}"
    start_time = _model_call_start_times.pop(call_id, None)
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2) if start_time else None

    # Record to cache
    req = _model_call_requests.pop(call_id, None)
    if req and response:
        global_llm_cache.record_response(req, response)

    _log_event("after_model", {
        "incident_id": incident_id,
        "iteration": iteration,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
    })

    return None


async def diagnostician_on_model_error_callback(ctx: Context, request: LlmRequest, exc: Exception) -> LlmResponse | None:
    """Handles model errors: detects 429 rate limit or 503 unavailable, logs structured warning, and applies retry backoff."""
    import os
    import re
    from google import genai
    from google.genai import types as genai_types

    state = getattr(ctx.session, "state", {}) if hasattr(ctx, "session") else {}
    incident_id = state.get("incident_id", "unknown")
    err_str = str(exc)

    is_transient = "429" in err_str or "503" in err_str or "RESOURCE_EXHAUSTED" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str.lower()
    _log_event("on_model_error", {
        "incident_id": incident_id,
        "error": err_str,
        "is_transient": is_transient,
    })

    if is_transient:
        api_key = os.environ.get("GEMINI_API_KEY")
        # Check for explicit retryDelay in Google GenAI error message
        match = re.search(r"retryDelay[:\s]+(\d+(?:\.\d+)?)s?", err_str)
        if match:
            base_delay = float(match.group(1)) + 2.0
        elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            base_delay = 20.0
        else:
            base_delay = 5.0

        for attempt in range(1, 4):
            delay = base_delay * attempt
            logger.warning(f"[ModelRetry] Transient error for {incident_id}. Backing off {delay:.1f}s (attempt {attempt}/3)...")
            await asyncio.sleep(delay)
            try:
                client = genai.Client(api_key=api_key)
                resp = await client.aio.models.generate_content(
                    model=request.model,
                    contents=request.contents,
                    config=request.config,
                )
                if resp and resp.text:
                    logger.info(f"[ModelRetry] Successful recovery on attempt {attempt} for {incident_id}")
                    # Update token tracking for the successful call
                    prompt_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) if resp.usage_metadata else 0
                    completion_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) if resp.usage_metadata else 0
                    total_tokens = getattr(resp.usage_metadata, "total_token_count", 0) if resp.usage_metadata else (prompt_tokens + completion_tokens)
                    token_tracker.add_tokens(incident_id, prompt_tokens, completion_tokens, total_tokens)

                    llm_resp = LlmResponse(
                        content=genai_types.Content(parts=[genai_types.Part(text=resp.text)]),
                        usage_metadata=resp.usage_metadata,
                    )
                    global_llm_cache.record_response(request, llm_resp)
                    return llm_resp
            except Exception as retry_err:
                logger.warning(f"[ModelRetry] Retry attempt {attempt} failed: {retry_err}")

    return None
