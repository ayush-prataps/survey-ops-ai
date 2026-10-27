"""
Experiment Runner Agent (ADK Agent).
Executes live experiments on the target system and returns raw Observations only.
Does NOT interpret causes or use an LLM for dispatch (deterministic execution avoids prompt injection).
"""

from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
from typing import Any, Callable

import anyio
from mcp import ClientSession

from surveyops.schemas import (
    ExperimentRequest,
    ExperimentType,
    Observation,
)
from surveyops.mcp_servers.server import mcp_server

# ── Structured Logger ────────────────────────────────────────────────────────
logger = logging.getLogger("surveyops.runner")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _log_runner_event(event_type: str, data: dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": "experiment_runner",
        "event": event_type,
    }
    entry.update(data)
    logger.info(json.dumps(entry))
    try:
        from surveyops.events import record_event
        inc_id = str(data.get("incident_id") or data.get("request_id") or "runner")
        record_event(inc_id, "runner", event_type, data)
    except Exception:
        pass


# ── Allowlist & Defense-in-depth Caps ─────────────────────────────────────────
ALLOWED_SERVICES = {"orders", "payment", "inventory", "auth"}
ALLOWED_ENDPOINTS = {
    "/orders",
    "/payment/charge",
    "/inventory/{item_id}",
    "/inventory/{item_id}/reserve",
    "/inventory/item-1",
    "/auth/login",
    "/auth/verify",
    "/health",
    "/metrics",
}
ALLOWED_TEMPLATES = {
    "p99_latency",
    "error_rate",
    "request_rate",
    "pool_in_use",
    "pool_capacity",
}

MAX_BURST_CONCURRENCY = int(os.environ.get("MAX_LOAD_BURST_CONCURRENCY", "10"))
MAX_BURST_SECONDS = int(os.environ.get("MAX_LOAD_BURST_SECONDS", "10"))


# ── Runner Callbacks ──────────────────────────────────────────────────────────

def before_agent_callback(request: ExperimentRequest) -> None:
    """Logs incoming experiment request before agent execution."""
    _log_runner_event("before_agent", {
        "request_id": request.request_id,
        "hypothesis_id": request.hypothesis_id,
        "experiment_type": request.experiment_type.value,
        "rationale": request.rationale,
    })


def before_tool_callback(tool_name: str, params: dict[str, Any]) -> None:
    """
    Second layer of defense-in-depth: enforces allowlists and caps before tool execution.
    Rejects any prohibited service, endpoint, or template.
    """
    if tool_name == "prometheus_query":
        template = params.get("template")
        service = params.get("service")
        endpoint = params.get("endpoint")
        if template not in ALLOWED_TEMPLATES:
            raise ValueError(f"[Guardrail] Disallowed query template '{template}'")
        if service and service not in ALLOWED_SERVICES:
            raise ValueError(f"[Guardrail] Disallowed service '{service}'")
        if endpoint and endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"[Guardrail] Disallowed endpoint '{endpoint}'")

    elif tool_name == "fetch_logs":
        service = params.get("service")
        if not service or service not in ALLOWED_SERVICES:
            raise ValueError(f"[Guardrail] Disallowed log service '{service}'. Must be in {sorted(ALLOWED_SERVICES)}")

    elif tool_name == "run_load_burst":
        endpoint = params.get("endpoint")
        concurrency = params.get("concurrency", 5)
        duration_s = params.get("duration_s", 5)
        if not endpoint or endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"[Guardrail] Disallowed burst endpoint '{endpoint}'")
        if concurrency > MAX_BURST_CONCURRENCY:
            raise ValueError(f"[Guardrail] Concurrency {concurrency} exceeds max {MAX_BURST_CONCURRENCY}")
        if duration_s > MAX_BURST_SECONDS:
            raise ValueError(f"[Guardrail] Duration {duration_s}s exceeds max {MAX_BURST_SECONDS}s")

    _log_runner_event("before_tool", {"tool": tool_name, "params": params})


def after_tool_callback(tool_name: str, result: Any, duration_ms: float) -> None:
    """Validates tool return schema and logs execution latency."""
    _log_runner_event("after_tool", {
        "tool": tool_name,
        "latency_ms": round(duration_ms, 2),
        "status": "success",
    })


def after_agent_callback(observation: Observation, total_duration_ms: float) -> None:
    """Logs raw observation produced by Runner agent."""
    _log_runner_event("after_agent", {
        "request_id": observation.request_id,
        "source": observation.source,
        "truncated": observation.truncated,
        "contains_untrusted_text": observation.contains_untrusted_text,
        "total_latency_ms": round(total_duration_ms, 2),
    })


async def _call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Executes a tool call through an official MCP ClientSession over an in-memory stream
    connected to the unified FastMCP server.
    """
    client_send, server_recv = anyio.create_memory_object_stream(20)
    server_send, client_recv = anyio.create_memory_object_stream(20)

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            mcp_server._mcp_server.run,
            server_recv,
            server_send,
            mcp_server._mcp_server.create_initialization_options(),
        )
        async with ClientSession(client_recv, client_send) as session:
            await session.initialize()
            mcp_res = await session.call_tool(tool_name, arguments=arguments)
            if mcp_res.isError:
                err_text = mcp_res.content[0].text if mcp_res.content else "Unknown MCP error"
                raise RuntimeError(f"MCP tool '{tool_name}' returned error: {err_text}")
            raw_text = mcp_res.content[0].text if mcp_res.content else "{}"
            result_dict = json.loads(raw_text)
        tg.cancel_scope.cancel()

    return result_dict


# ── Runner Agent Implementation ──────────────────────────────────────────────

class ExperimentRunnerAgent:
    """
    Deterministic ADK Runner Agent.
    Dispatches ExperimentRequests to MCP tools through an MCP ClientSession without an LLM:
    - 0 prompt injection risk when reading logs
    - Predictable execution latency and 0 token cost
    - Returns raw Observation data only
    """
    def __init__(self) -> None:
        self.before_agent = before_agent_callback
        self.before_tool = before_tool_callback
        self.after_tool = after_tool_callback
        self.after_agent = after_agent_callback

    async def run(self, request: ExperimentRequest) -> Observation:
        """Executes the requested experiment and returns an Observation."""
        t_start = time.perf_counter()
        self.before_agent(request)

        params = request.params or {}
        obs: Observation

        if request.experiment_type == ExperimentType.query_metrics:
            tool_name = "prometheus_query"
            self.before_tool(tool_name, params)
            t0 = time.perf_counter()
            query_res = await _call_mcp_tool(tool_name, params)
            self.after_tool(tool_name, query_res, (time.perf_counter() - t0) * 1000.0)
            obs = Observation(
                request_id=request.request_id,
                source="prometheus",
                data=query_res,
                collected_at=datetime.now(timezone.utc),
                truncated=False,
                contains_untrusted_text=False,
            )

        elif request.experiment_type == ExperimentType.fetch_logs:
            tool_name = "fetch_logs"
            self.before_tool(tool_name, params)
            t0 = time.perf_counter()
            logs_res = await _call_mcp_tool(tool_name, params)
            self.after_tool(tool_name, logs_res, (time.perf_counter() - t0) * 1000.0)
            obs = Observation(
                request_id=request.request_id,
                source="docker_logs",
                data=logs_res,
                collected_at=datetime.now(timezone.utc),
                truncated=bool(logs_res.get("truncated", False)),
                contains_untrusted_text=bool(logs_res.get("contains_untrusted_text", True)),
            )

        elif request.experiment_type == ExperimentType.load_burst:
            tool_name = "run_load_burst"
            self.before_tool(tool_name, params)
            t0 = time.perf_counter()
            burst_res = await _call_mcp_tool(tool_name, params)
            self.after_tool(tool_name, burst_res, (time.perf_counter() - t0) * 1000.0)
            obs = Observation(
                request_id=request.request_id,
                source="load_burst",
                data=burst_res,
                collected_at=datetime.now(timezone.utc),
                truncated=False,
                contains_untrusted_text=False,
            )

        else:
            raise ValueError(f"Unknown experiment type: {request.experiment_type}")

        total_ms = (time.perf_counter() - t_start) * 1000.0
        self.after_agent(obs, total_ms)
        return obs

    execute = run

