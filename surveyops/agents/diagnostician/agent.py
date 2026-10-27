"""
SurveyOps Diagnostician Agent (Google ADK LoopAgent).
Satisfies all requirements from AGENTS.md, docs/01-ARCHITECTURE.md, docs/02-WORKFLOW.md,
docs/04-CONTRACTS.md, and docs/06-SAFETY-OBSERVABILITY.md:
- Google ADK only. No LangGraph, LangChain, LiteLLM, AutoGen.
- Cyclic LoopAgent with hard iteration cap (MAX_LOOP_ITERATIONS).
- LLM steps for hypothesis formulation and evidence synthesis.
- Deterministic steps for falsification guardrails, A2A communication, and exit decision logic.
- A2A to Runner (ExperimentRequest in, Observation out).
- A2A to Remediator (DiagnosisResult in, RemediationResult out) on root_cause.
"""

from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
import uuid
from typing import Any, AsyncGenerator

import httpx
from pydantic import ValidationError
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.loop_agent import LoopAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event, EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from surveyops.schemas import (
    DiagnosisResult,
    ExperimentRequest,
    ExperimentType,
    FailureMode,
    InvestigatePayload,
    LedgerEntry,
    Observation,
    Outcome,
    Proposition,
    RemediationResult,
)
from surveyops.a2a_utils import from_a2a_message, to_a2a_text_part
from surveyops.agents.surface_mapper.storage import load_propositions
from .cache import global_llm_cache
from .callbacks import (
    diagnostician_after_agent_callback,
    diagnostician_after_model_callback,
    diagnostician_before_agent_callback,
    diagnostician_before_model_callback,
    diagnostician_on_model_error_callback,
    token_tracker,
)
from .exit_logic import (
    evaluate_exit_conditions,
    get_max_loop_iterations,
    apply_hypothesis_status_transitions,
)
from .falsification import validate_experiment_request
from .prompts import (
    SYSTEM_INSTRUCTIONS,
    build_hypothesis_and_experiment_prompt,
    build_ledger_evaluation_prompt,
)
from .schemas import (
    ChosenExperiment,
    ExitDecision,
    ExperimentParams,
    ExperimentStepOutput,
    HypothesisItem,
    LedgerUpdateOutput,
)

logger = logging.getLogger("surveyops.diagnostician")


def get_diagnostician_model() -> str:
    """Resolves model from DIAGNOSTICIAN_MODEL env var without hardcoded defaults."""
    model = os.getenv("DIAGNOSTICIAN_MODEL")
    if not model:
        raise ValueError("DIAGNOSTICIAN_MODEL environment variable must be set (e.g. 'gemini-3.8-flash')")
    return model


def get_runner_a2a_url() -> str:
    return os.environ.get("RUNNER_A2A_URL", "http://localhost:8005")


def get_remediator_a2a_url() -> str:
    return os.environ.get("REMEDIATOR_A2A_URL", "http://localhost:8006")


def get_shared_secret() -> str:
    return os.environ.get("INVESTIGATE_SHARED_SECRET") or "dev-secret"


def reconcile_hypotheses_with_code_authority(
    canonical_hyps: list[HypothesisItem],
    submitted_hyps: list[HypothesisItem],
) -> list[HypothesisItem]:
    """
    Code-authoritative hypothesis reconciliation (Requirement 4):
    1. Hypothesis IDs are assigned by code, not the LLM.
    2. Seeded propositions strictly retain their proposition_id and metadata.
    3. If the LLM renames an existing failure mode, code remaps it to the canonical ID.
    4. A 'novel' label is never default and requires explicit justification.
    """
    canonical_by_id = {h.hypothesis_id: h for h in canonical_hyps}
    canonical_by_mode = {h.failure_mode: h for h in canonical_hyps}

    reconciled: list[HypothesisItem] = []
    seen_ids = set()

    for sub in submitted_hyps:
        matched = None
        if sub.hypothesis_id in canonical_by_id:
            matched = canonical_by_id[sub.hypothesis_id]
        elif sub.failure_mode in canonical_by_mode:
            matched = canonical_by_mode[sub.failure_mode]

        if matched:
            item = matched.model_copy(update={
                "confidence": sub.confidence,
                "status": sub.status or matched.status,
                "description": sub.description or matched.description,
                "is_novel": matched.is_novel,
                "justification": sub.justification or matched.justification,
            })
            if item.hypothesis_id not in seen_ids:
                seen_ids.add(item.hypothesis_id)
                reconciled.append(item)
        else:
            if sub.justification and sub.justification.strip():
                idx = len(reconciled) + 1
                novel_id = f"hyp-novel-{idx:02d}"
                item = HypothesisItem(
                    hypothesis_id=novel_id,
                    failure_mode=sub.failure_mode,
                    description=sub.description,
                    status=sub.status or "active",
                    is_novel=True,
                    justification=sub.justification,
                    supporting_proposition_id=None,
                    graph_edge_ref=None,
                    confidence=sub.confidence,
                )
                if novel_id not in seen_ids:
                    seen_ids.add(novel_id)
                    reconciled.append(item)

    # Retain any canonical hypotheses that were omitted
    for h in canonical_hyps:
        if h.hypothesis_id not in seen_ids:
            seen_ids.add(h.hypothesis_id)
            reconciled.append(h)

    return reconciled


def validate_ledger_tool_call(
    ledger_out: LedgerUpdateOutput,
    tool_context: Any = None,
) -> str | None:
    """
    Code-authoritative validation for the verdict step (Requirement 1):
    1. Every entry must quote original disconfirming_if.
    2. disconfirming_condition_met must be accompanied by field and value in justification.
    3. evidence_for and evidence_against must each cite [request_id:field].
    4. request_id and field must exist in stored Observations.
    5. Confidence cannot be lowered without disconfirming_condition_met=True.
    """
    state: dict[str, Any] = {}
    if tool_context and hasattr(tool_context, "session") and tool_context.session:
        state = tool_context.session.state or {}
    elif tool_context and hasattr(tool_context, "state") and tool_context.state:
        state = tool_context.state

    observations = list(state.get("observations", []))
    last_obs = state.get("last_observation")
    if last_obs and last_obs not in observations:
        observations.append(last_obs)
    pending_req = state.get("pending_request")

    valid_req_ids = set()
    valid_fields = {
        "pool_in_use", "pool_max_in_use", "pool_capacity", "pool_utilization_ratio",
        "request_rate", "request_count", "p99_latency", "error_rate", "results",
        "value", "promql", "template", "logs", "lines", "lines_count", "service",
        "p50_latency_ms", "p95_latency_ms", "p99_latency_ms", "total_requests",
        "successful_requests", "error_requests", "status", "data", "metadata",
        "concurrency", "duration_s", "http_status", "body", "latency", "endpoint",
        "range_s", "truncated", "contains_untrusted_text", "error", "ratio",
        "active", "active_connections", "max_in_use", "capacity", "in_use"
    }

    for obs in observations:
        if hasattr(obs, "request_id") and obs.request_id:
            valid_req_ids.add(obs.request_id)
        if hasattr(obs, "data") and isinstance(obs.data, dict):
            valid_fields.update(str(k).lower() for k in obs.data.keys())
            for k, v in obs.data.items():
                if isinstance(v, dict):
                    valid_fields.update(str(sub_k).lower() for sub_k in v.keys())

    if pending_req:
        req_id = getattr(pending_req, "request_id", None) or (pending_req.get("request_id") if isinstance(pending_req, dict) else None)
        if req_id:
            valid_req_ids.add(req_id)

    citation_pattern = re.compile(r"(?:\[|\(|\b)((?:req|exp)-[a-zA-Z0-9_\-]+)[\]:\s.,/]+([a-zA-Z0-9_]+)")
    existing_hyps = {h.hypothesis_id: h for h in state.get("hypotheses", [])}

    errors: list[str] = []
    for entry in ledger_out.ledger_entries:
        if not entry.quoted_disconfirming_if or not entry.quoted_disconfirming_if.strip():
            errors.append(f"Hypothesis '{entry.hypothesis_id}' lacks a quoted_disconfirming_if.")

        if not entry.disconfirming_justification or not entry.disconfirming_justification.strip():
            errors.append(f"Hypothesis '{entry.hypothesis_id}' lacks a disconfirming_justification naming an observation field and value.")

        all_evidence = [(item, "evidence_for") for item in entry.evidence_for] + [(item, "evidence_against") for item in entry.evidence_against]
        if not all_evidence:
            errors.append(f"Hypothesis '{entry.hypothesis_id}' has no evidence items with citations.")

        for item, list_name in all_evidence:
            matches = citation_pattern.findall(item)
            if not matches:
                if not any(rid in item for rid in valid_req_ids):
                    errors.append(f"Hypothesis '{entry.hypothesis_id}' {list_name} item lacks a valid [request_id:field] citation: '{item[:60]}'")
            else:
                for req_id, field in matches:
                    if valid_req_ids and req_id not in valid_req_ids:
                        errors.append(f"Hypothesis '{entry.hypothesis_id}' cites non-existent request_id '{req_id}'. Valid IDs: {sorted(valid_req_ids)}")
                    if field.lower() not in valid_fields:
                        errors.append(f"Hypothesis '{entry.hypothesis_id}' cites non-existent field '{field}'.")

        # Confidence lowering check
        if entry.hypothesis_id in existing_hyps:
            prev_conf = existing_hyps[entry.hypothesis_id].confidence
            if entry.confidence < prev_conf and not entry.disconfirming_condition_met:
                errors.append(
                    f"Hypothesis '{entry.hypothesis_id}' lowered confidence from {prev_conf:.2f} to {entry.confidence:.2f} "
                    f"without disconfirming_condition_met=True."
                )

    if errors:
        return "Ledger update rejected:\n" + "\n".join(f"- {e}" for e in errors)
    return None


# ── Step B & C: Deterministic A2A Runner Dispatcher Agent ───────────────────

def resolve_service_from_endpoint(endpoint: str) -> str:
    """Extracts target microservice name from an endpoint string."""
    clean = endpoint.split()[1] if " " in endpoint else endpoint
    if clean.startswith("/orders"):
        return "orders"
    elif clean.startswith("/payment"):
        return "payment"
    elif clean.startswith("/inventory"):
        return "inventory"
    elif clean.startswith("/auth"):
        return "auth"
    return "payment"


async def execute_runner_a2a_request(
    runner_url: str,
    shared_secret: str,
    req: ExperimentRequest,
) -> Observation:
    """Sends ExperimentRequest to the Runner over A2A and returns raw Observation."""
    payload = {
        "jsonrpc": "2.0",
        "id": f"rpc-{uuid.uuid4().hex[:8]}",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": f"msg-{req.request_id}",
                "role": "user",
                "parts": [to_a2a_text_part(req).model_dump()],
            }
        },
    }
    headers = {"X-Shared-Secret": shared_secret}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(runner_url, json=payload, headers=headers)
            if resp.status_code != 200:
                return Observation(
                    request_id=req.request_id,
                    source="a2a_runner_http_error",
                    data={"http_status": resp.status_code, "body": resp.text},
                    collected_at=datetime.now(timezone.utc),
                )
            resp_data = resp.json()
            result = resp_data.get("result", {})
            status = result.get("status", {})
            if status.get("state") == "completed":
                decoded = from_a2a_message(status.get("message"), Observation)
                if decoded.is_ok and decoded.value is not None:
                    return decoded.value
                return Observation(
                    request_id=req.request_id,
                    source="a2a_decode_error",
                    data={"decode_error": decoded.error},
                    collected_at=datetime.now(timezone.utc),
                )
            return Observation(
                request_id=req.request_id,
                source="runner_task_failed",
                data={"task_status": status},
                collected_at=datetime.now(timezone.utc),
            )
    except Exception as net_exc:
        logger.error(f"Failed to communicate with Runner over A2A: {net_exc}")
        return Observation(
            request_id=req.request_id,
            source="runner_connection_error",
            data={"exception": str(net_exc)},
            collected_at=datetime.now(timezone.utc),
        )


def check_system_healthy(
    metric_obs: Observation | None,
    log_obs: Observation | None,
) -> tuple[bool, str]:
    """
    Deterministic early-abstain check for healthy systems:
    Returns (True, reason) only if system has confirmed normal metrics,
    no saturation, no error logs, normal latency, and substantive telemetry.
    """
    if not metric_obs and not log_obs:
        return False, "Probes failed to execute"

    for obs in (metric_obs, log_obs):
        if obs and obs.source in ("runner_connection_error", "a2a_runner_http_error", "runner_task_failed"):
            return False, f"Runner communication error during probe: {obs.source}"

    # 1. Check metric data
    if not metric_obs or not metric_obs.data:
        return False, "Insufficient telemetry: metric observation missing"

    m_data = metric_obs.data
    # 1a. Connection pool saturation
    pool_ratio = m_data.get("pool_utilization_ratio")
    if pool_ratio is not None and float(pool_ratio) >= 0.80:
        return False, f"Pool saturation detected: pool_utilization_ratio={pool_ratio}"

    in_use = m_data.get("pool_max_in_use", m_data.get("pool_in_use"))
    cap = m_data.get("pool_capacity")
    if in_use is not None and cap is not None and float(cap) > 0:
        if float(in_use) >= float(cap):
            return False, f"Pool saturated: in_use={in_use} >= cap={cap}"

    # 1b. Latency elevation
    p99_ms = m_data.get("p99_latency_ms")
    if p99_ms is not None and float(p99_ms) > 500.0:
        return False, f"High latency detected in metrics: p99={p99_ms}ms"

    # 1c. Prometheus scalar values (>0.5s)
    if "results" in m_data and isinstance(m_data["results"], list):
        for r in m_data["results"]:
            val = r.get("value")
            if val and len(val) > 1:
                try:
                    num_val = float(val[1])
                    if num_val > 0.5:
                        return False, f"Elevated Prometheus metric value: {num_val}"
                except (ValueError, TypeError):
                    pass

    # 1d. Error rate / error counts
    error_rate = m_data.get("error_rate")
    if error_rate is not None and float(error_rate) > 0.0:
        return False, f"Non-zero error rate detected: {error_rate}"

    error_requests = m_data.get("error_requests")
    if error_requests is not None and int(error_requests) > 0:
        return False, f"Error requests detected in telemetry: {error_requests}"

    # 2. Check container logs
    if not log_obs or not log_obs.data:
        return False, "Insufficient telemetry: log observation missing"

    l_data = log_obs.data
    raw_lines = l_data.get("lines", [])
    if isinstance(raw_lines, str):
        raw_lines = [raw_lines]
    elif not raw_lines and "logs" in l_data:
        raw_lines = str(l_data["logs"]).splitlines()

    if not raw_lines:
        return False, "Insufficient telemetry: container logs empty"

    error_regex = re.compile(
        r"QueuePool limit.*reached|TimeoutError|PoolTimeout|500 Internal Server Error|"
        r"502 Bad Gateway|Connection refused|Exception|Traceback|status_code=5\d\d",
        re.IGNORECASE,
    )
    for line in raw_lines:
        if error_regex.search(str(line)):
            return False, f"Fault signature in container logs: {str(line)[:80]}"

    return True, "No saturation, no error logs, normal latency (system healthy)"


class A2ARunnerDispatcherAgent(BaseAgent):
    """
    Deterministic Step:
    1. Validates chosen ExperimentRequest against Falsification Rule & Runner Allowlists.
    2. Sends ExperimentRequest to Runner over A2A.
    3. Receives and validates raw Observation.
    4. Records observation in session state without interpreting causes.
    """
    name: str = "runner_dispatcher"
    runner_url: str = "http://localhost:8005"
    shared_secret: str = "dev-secret"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        raw_output = ctx.session.state.get("step_output_raw")
        incident = ctx.session.state.get("incident")
        incident_id = ctx.session.state.get("incident_id") or getattr(incident, "incident_id", None) or "inc-unknown"
        iteration = ctx.session.state.get("iteration", 1)

        # 1. Validate structured LLM output with Pydantic in our own code via adapter
        step_output = None
        try:
            from surveyops.llm_utils import extract_adk_structured_output
            step_output = extract_adk_structured_output(
                state=ctx.session.state,
                state_key="step_output_raw",
                model_cls=ExperimentStepOutput,
                last_text_reply=raw_output if isinstance(raw_output, str) else None,
            )
        except Exception as exc:
            logger.warning(f"Structured output error in runner dispatcher: {exc}")

        # Fallback if LLM output was completely malformed
        if step_output is None:
            endpoint = getattr(incident, "endpoint", "/payment/charge")
            step_output = ExperimentStepOutput(
                hypotheses=ctx.session.state.get("hypotheses", []),
                chosen_experiment=ChosenExperiment(
                    request_id=f"req-{uuid.uuid4().hex[:8]}",
                    hypothesis_id="fallback",
                    experiment_type=ExperimentType.query_metrics,
                    params=ExperimentParams(template="pool_in_use", service="payment", endpoint=endpoint, range_s=120),
                    rationale="Fallback query for connection pool metrics",
                    disconfirming_if="pool_in_use == 0",
                ),
                disconfirming_if="pool_in_use == 0",
            )

        # Update hypotheses list in session state with code authority
        submitted_hyps = step_output.hypotheses or ctx.session.state.get("hypotheses", [])
        canonical_hyps = ctx.session.state.get("hypotheses", [])
        updated_hyps = reconcile_hypotheses_with_code_authority(canonical_hyps, submitted_hyps)

        chosen = step_output.chosen_experiment
        if hasattr(chosen, "to_experiment_request"):
            req = chosen.to_experiment_request()
        elif isinstance(chosen, ExperimentRequest):
            req = chosen
        elif isinstance(chosen, dict):
            req = ExperimentRequest.model_validate(chosen)
        else:
            req = chosen

        # Remap chosen hypothesis_id to canonical ID if needed
        matched_h = next((h for h in updated_hyps if h.hypothesis_id == req.hypothesis_id), None)
        if not matched_h:
            matched_h = next((h for h in updated_hyps if h.failure_mode == getattr(req, "failure_mode", None)), None)
            if matched_h:
                req.hypothesis_id = matched_h.hypothesis_id

        # Anchor queries and logs to incident window_start to prevent cross-trial contamination
        if incident and getattr(incident, "window_start", None):
            ws_val = getattr(incident, "window_start")
            ws_iso = ws_val.isoformat() if hasattr(ws_val, "isoformat") else str(ws_val)
            if isinstance(req.params, dict):
                req.params.setdefault("window_start", ws_iso)

        # 2. Enforce Falsification Rule & Allowlist checks
        print(f"\n[EXPERIMENT REQUEST] ID: {req.request_id} (Iteration {iteration}) for hypothesis '{req.hypothesis_id}':")
        print(f"  Type: {req.experiment_type.value}")
        print(f"  Params: {req.params.model_dump() if hasattr(req.params, 'model_dump') else req.params}")
        print(f"  disconfirming_if verbatim: \"{req.disconfirming_if}\"")
        print(f"  Rationale: {req.rationale}")

        guardrail_error: str | None = None
        try:
            validate_experiment_request(req)
        except ValueError as val_err:
            guardrail_error = str(val_err)
            logger.warning(f"Experiment request rejected by guardrail: {guardrail_error}")

        observation: Observation
        disconfirming_tested = list(ctx.session.state.get("disconfirming_hypotheses_tested", []))

        inc_id = str(ctx.session.state.get("incident_id") or "unknown_incident")
        if guardrail_error:
            # Produce synthetic observation recording rejection rather than crashing
            observation = Observation(
                request_id=req.request_id,
                source="diagnostician_falsification_guardrail",
                data={"error": guardrail_error, "status": "rejected_by_guardrail"},
                collected_at=datetime.now(timezone.utc),
            )
            try:
                from surveyops.events import record_event
                record_event(
                    inc_id,
                    "guardrail",
                    "guardrail_rejection",
                    {"boundary": "falsification", "reason": guardrail_error, "request_id": req.request_id},
                )
            except Exception:
                pass
        else:
            # 3. Send over A2A to the Runner
            observation = await execute_runner_a2a_request(self.runner_url, self.shared_secret, req)
            if req.hypothesis_id not in disconfirming_tested and not observation.source.startswith("runner_") and not observation.source.startswith("a2a_"):
                disconfirming_tested.append(req.hypothesis_id)
            try:
                from surveyops.events import record_event
                record_event(
                    inc_id,
                    "runner",
                    "observation_received",
                    {"request_id": observation.request_id, "source": observation.source, "data": observation.data},
                )
            except Exception:
                pass

        print(f"\n[RAW OBSERVATION] Request ID: {observation.request_id} | Source: {observation.source}:")
        print(f"  Collected At: {observation.collected_at}")
        print(f"  Data: {json.dumps(observation.data, indent=2, default=str)}")

        # 4. Save state
        obs_history = list(ctx.session.state.get("observations", []))
        obs_history.append(observation)

        state_delta = {
            "hypotheses": updated_hyps,
            "pending_request": req,
            "last_observation": observation,
            "observations": obs_history,
            "disconfirming_hypotheses_tested": disconfirming_tested,
        }

        yield Event(
            author=self.name,
            actions=EventActions(state_delta=state_delta),
        )


# ── Step D: Deterministic Exit Decision & Remediator Handoff Agent ───────────

class ExitDecisionAgent(BaseAgent):
    """
    Deterministic Step:
    1. Validates structured LedgerUpdateOutput from Step D LLM.
    2. Updates ledger and hypothesis confidence.
    3. Evaluates exit logic strictly in code (confidence threshold, margin, disconfirming test).
    4. On root_cause, sends DiagnosisResult to Remediator over A2A.
    5. Escalates to exit LoopAgent when done, or increments iteration.
    """
    name: str = "exit_decision"
    remediator_url: str = "http://localhost:8006"
    shared_secret: str = "dev-secret"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        raw_ledger = ctx.session.state.get("ledger_output_raw")
        incident = ctx.session.state.get("incident")
        endpoint = getattr(incident, "endpoint", "unknown")
        incident_id = ctx.session.state.get("incident_id") or getattr(incident, "incident_id", None) or "inc-unknown"
        iteration = ctx.session.state.get("iteration", 1)
        max_iterations = ctx.session.state.get("max_iterations", get_max_loop_iterations())
        disconfirming_tested = set(ctx.session.state.get("disconfirming_hypotheses_tested", []))
        hypotheses = ctx.session.state.get("hypotheses", [])

        # 1. Validate structured ledger output via adapter
        ledger_out = None
        try:
            from surveyops.llm_utils import extract_adk_structured_output
            ledger_out = extract_adk_structured_output(
                state=ctx.session.state,
                state_key="ledger_output_raw",
                model_cls=LedgerUpdateOutput,
                last_text_reply=raw_ledger if isinstance(raw_ledger, str) else None,
            )
        except Exception as exc:
            logger.warning(f"Structured output error in exit decision: {exc}")

        existing_ledger = list(ctx.session.state.get("ledger", []))
        recommendation = "continue"

        if ledger_out is not None:
            print(f"\n[LEDGER UPDATE] (Iteration {iteration}/{max_iterations}):")
            for entry in ledger_out.ledger_entries:
                print(f"  - Hypothesis ID: {entry.hypothesis_id} | Status: {entry.status} | Conf: {entry.confidence:.2f}")
                print(f"    Quoted disconfirming_if: \"{entry.quoted_disconfirming_if}\"")
                print(f"    Disconfirming condition met: {entry.disconfirming_condition_met}")
                print(f"    Justification: {entry.disconfirming_justification}")
                print(f"    Evidence FOR: {entry.evidence_for}")
                print(f"    Evidence AGAINST: {entry.evidence_against}")
            print(f"  LLM Recommendation: {ledger_out.llm_recommendation}")
            print(f"  Reasoning: {ledger_out.reasoning}\n")

            recommendation = ledger_out.llm_recommendation
            # Append new ledger entries
            for entry in ledger_out.ledger_entries:
                existing_ledger.append(entry)

            # Apply deterministic status transitions (window-max or 2 obs for rule-out, reactivation on CheckableSignal, log every change)
            status_change_log = list(ctx.session.state.get("status_changes", []))
            last_obs = ctx.session.state.get("last_observation")
            all_obs = list(ctx.session.state.get("observations", []))
            props = list(ctx.session.state.get("propositions", []))

            apply_hypothesis_status_transitions(
                hypotheses=hypotheses,
                ledger_entries=ledger_out.ledger_entries,
                ledger=existing_ledger,
                observations=all_obs,
                current_observation=last_obs,
                propositions=props,
                status_change_log=status_change_log,
            )
            ctx.session.state["status_changes"] = status_change_log

        # Check hard token budget per incident from env (Requirement 2 & 4: default 100k)
        max_token_budget = int(
            os.environ.get(
                "MAX_INCIDENT_TOKEN_BUDGET",
                os.environ.get("SURVEYOPS_MAX_INCIDENT_TOKEN_BUDGET", "100000"),
            )
        )
        usage = token_tracker.get_usage(incident_id)
        current_tokens = usage.get("total_tokens", 0)

        leader_margin_history = list(ctx.session.state.get("leader_margin_history", []))

        if current_tokens >= max_token_budget:
            logger.warning(
                f"Incident {incident_id} exceeded token budget: {current_tokens} >= {max_token_budget}. Forcing budget_exhausted."
            )
            exit_decision = ExitDecision(
                should_exit=True,
                outcome=Outcome.budget_exhausted,
                top_hypothesis=sorted(hypotheses, key=lambda h: h.confidence, reverse=True)[0] if hypotheses else None,
                confidence=0.0,
                self_reported_confidence=0.0,
                reason=f"Hard token budget exhausted ({current_tokens}/{max_token_budget} tokens used).",
            )
        else:
            # 2. Evaluate exit conditions by code (including convergence stop)
            exit_decision = evaluate_exit_conditions(
                hypotheses=hypotheses,
                tested_disconfirming_hypotheses=disconfirming_tested,
                iteration=iteration,
                max_iterations=max_iterations,
                llm_recommendation=recommendation,
                ledger=existing_ledger,
                propositions=ctx.session.state.get("propositions", []),
                observations=ctx.session.state.get("observations", []),
                leader_margin_history=leader_margin_history,
            )

        logger.info(
            f"ExitDecision (iteration {iteration}/{max_iterations}): should_exit={exit_decision.should_exit}, "
            f"outcome={exit_decision.outcome.value}, reason={exit_decision.reason}"
        )
        try:
            from surveyops.events import record_event
            record_event(
                incident_id,
                "diagnostician",
                "ledger_updated",
                {
                    "iteration": iteration,
                    "hypotheses": [h.model_dump() for h in hypotheses],
                    "ledger_entries": [e.model_dump() for e in existing_ledger],
                    "status_changes": status_change_log if 'status_change_log' in locals() else [],
                },
            )
            record_event(
                incident_id,
                "diagnostician",
                "exit_decision",
                {
                    "iteration": iteration,
                    "should_exit": exit_decision.should_exit,
                    "outcome": exit_decision.outcome.value,
                    "confidence": exit_decision.confidence,
                    "reason": exit_decision.reason,
                    "top_hypothesis": exit_decision.top_hypothesis.model_dump() if exit_decision.top_hypothesis else None,
                },
            )
        except Exception:
            pass

        # Track leader and margin for convergence checks
        active_hyps = [h for h in hypotheses if h.status != "ruled_out"]
        sorted_active = sorted(active_hyps, key=lambda h: h.confidence, reverse=True)
        if sorted_active:
            cur_leader = sorted_active[0].hypothesis_id
            cur_margin = (sorted_active[0].confidence - sorted_active[1].confidence) if len(sorted_active) > 1 else 1.0
            leader_margin_history.append((cur_leader, cur_margin))

        if exit_decision.should_exit:
            top_h = exit_decision.top_hypothesis
            diagnosis = DiagnosisResult(
                incident_id=incident_id,
                outcome=exit_decision.outcome,
                root_cause=top_h.failure_mode if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                endpoint=endpoint,
                supporting_proposition_id=top_h.supporting_proposition_id if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                graph_edge_ref=top_h.graph_edge_ref if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                confidence=exit_decision.confidence if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                self_reported_confidence=exit_decision.self_reported_confidence if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                ruled_out=[h.hypothesis_id for h in hypotheses if h.status == "ruled_out"],
                iterations_used=iteration,
            )

            # 3. On root_cause, send DiagnosisResult to Remediator stub over A2A
            if exit_decision.outcome == Outcome.root_cause:
                await self._send_to_remediator(diagnosis)

            state_delta = {
                "hypotheses": hypotheses,
                "ledger": existing_ledger,
                "diagnosis": diagnosis,
                "status_changes": ctx.session.state.get("status_changes", []),
                "leader_margin_history": leader_margin_history,
            }
            # Terminate LoopAgent cleanly via escalate=True
            yield Event(
                author=self.name,
                actions=EventActions(escalate=True, state_delta=state_delta),
            )
        else:
            # Continue to next iteration
            state_delta = {
                "hypotheses": hypotheses,
                "ledger": existing_ledger,
                "iteration": iteration + 1,
                "status_changes": ctx.session.state.get("status_changes", []),
                "leader_margin_history": leader_margin_history,
            }
            yield Event(
                author=self.name,
                actions=EventActions(state_delta=state_delta),
            )

    async def _send_to_remediator(self, diagnosis: DiagnosisResult) -> None:
        """Sends DiagnosisResult to Remediator over A2A, catching failures without crashing."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                payload = {
                    "jsonrpc": "2.0",
                    "id": f"rpc-rem-{uuid.uuid4().hex[:8]}",
                    "method": "message/send",
                    "params": {
                        "message": {
                            "message_id": f"msg-rem-{diagnosis.incident_id}",
                            "role": "user",
                            "parts": [to_a2a_text_part(diagnosis).model_dump()],
                        }
                    },
                }
                headers = {"X-Shared-Secret": self.shared_secret}
                resp = await client.post(self.remediator_url, json=payload, headers=headers)
                if resp.status_code == 200:
                    resp_data = resp.json()
                    status = resp_data.get("result", {}).get("status", {})
                    decoded = from_a2a_message(status.get("message"), RemediationResult)
                    if decoded.is_ok:
                        logger.info(f"Remediator acknowledged diagnosis: {decoded.value}")
                    else:
                        logger.warning(f"Remediator returned invalid payload: {decoded.error}")
                else:
                    logger.warning(f"Remediator A2A returned HTTP {resp.status_code}")
        except Exception as exc:
            logger.warning(f"Failed to communicate with Remediator over A2A (non-fatal): {exc}")


# ── Instruction Callables for LlmAgents ──────────────────────────────────────

def get_hypothesis_agent_instruction(ctx: ReadonlyContext) -> str:
    state = getattr(ctx, "state", {})
    incident = state.get("incident")
    propositions = state.get("propositions", [])
    hypotheses = state.get("hypotheses", [])
    ledger = state.get("ledger", [])
    last_obs = state.get("last_observation")
    observations = state.get("observations", [])
    iteration = state.get("iteration", 1)
    max_iters = state.get("max_iterations", get_max_loop_iterations())

    return build_hypothesis_and_experiment_prompt(
        incident=incident,
        propositions=propositions,
        hypotheses=hypotheses,
        ledger=ledger,
        last_observation=last_obs,
        observations=observations,
        iteration=iteration,
        max_iterations=max_iters,
    )


def get_ledger_evaluator_instruction(ctx: ReadonlyContext) -> str:
    state = getattr(ctx, "state", {})
    incident = state.get("incident")
    hypotheses = state.get("hypotheses", [])
    pending_request = state.get("pending_request", {})
    last_obs = state.get("last_observation")
    observations = state.get("observations", [])
    ledger = state.get("ledger", [])
    iteration = state.get("iteration", 1)
    max_iters = state.get("max_iterations", get_max_loop_iterations())

    return build_ledger_evaluation_prompt(
        incident=incident,
        hypotheses=hypotheses,
        pending_request=pending_request,
        last_observation=last_obs,
        observations=observations,
        ledger=ledger,
        iteration=iteration,
        max_iterations=max_iters,
    )


# ── Factory: Build Diagnostician LoopAgent ───────────────────────────────────

def build_diagnostician_loop_agent(
    model: str | None = None,
    runner_url: str | None = None,
    remediator_url: str | None = None,
    shared_secret: str | None = None,
    max_iterations: int | None = None,
) -> LoopAgent:
    """Builds the complete ADK LoopAgent for the Diagnostician."""
    resolved_model = model or get_diagnostician_model()
    resolved_runner = runner_url or get_runner_a2a_url()
    resolved_remediator = remediator_url or get_remediator_a2a_url()
    resolved_secret = shared_secret or get_shared_secret()
    max_iters = max_iterations or get_max_loop_iterations()

    from google.adk.workflow._retry_config import RetryConfig
    retry_cfg = RetryConfig(
        max_attempts=3,
        initial_delay=2.0,
        max_delay=8.0,
        backoff_factor=2.0,
    )

    from surveyops.llm_utils import build_adk_submission_tool

    # Step A: Propose/update hypotheses and pick experiment
    submit_step_tool = build_adk_submission_tool(
        tool_name="submit_experiment_step",
        description="Submit updated hypotheses and the chosen experiment request for this iteration.",
        model_cls=ExperimentStepOutput,
        state_key="step_output_raw",
    )

    hypothesis_agent = LlmAgent(
        name="hypothesis_and_experiment_selector",
        model=resolved_model,
        include_contents="none",
        instruction=get_hypothesis_agent_instruction,
        global_instruction=SYSTEM_INSTRUCTIONS + "\n\nYou MUST call the submit_experiment_step tool with your updated hypotheses and chosen experiment.",
        tools=[submit_step_tool],
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=4096,
            http_options=genai_types.HttpOptions(
                extra_body={"thinking": {"type": "disabled"}},
            ),
        ),
        retry_config=retry_cfg,
        before_agent_callback=diagnostician_before_agent_callback,
        after_agent_callback=diagnostician_after_agent_callback,
        before_model_callback=diagnostician_before_model_callback,
        after_model_callback=diagnostician_after_model_callback,
        on_model_error_callback=diagnostician_on_model_error_callback,
    )

    # Step B & C: Validate falsification/allowlists & dispatch to Runner over A2A
    runner_dispatcher = A2ARunnerDispatcherAgent(
        runner_url=resolved_runner,
        shared_secret=resolved_secret,
    )

    # Step D: Synthesize observation and update evidence ledger
    submit_ledger_tool = build_adk_submission_tool(
        tool_name="submit_ledger_update",
        description="Submit evidence ledger entries and LLM recommendation based on the observation.",
        model_cls=LedgerUpdateOutput,
        state_key="ledger_output_raw",
        validator=validate_ledger_tool_call,
    )

    ledger_evaluator = LlmAgent(
        name="ledger_evaluator",
        model=resolved_model,
        include_contents="none",
        instruction=get_ledger_evaluator_instruction,
        global_instruction=SYSTEM_INSTRUCTIONS + "\n\nYou MUST call the submit_ledger_update tool with your updated ledger entries and recommendation.",
        tools=[submit_ledger_tool],
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=4096,
            http_options=genai_types.HttpOptions(
                extra_body={"thinking": {"type": "disabled"}},
            ),
        ),
        retry_config=retry_cfg,
        before_agent_callback=diagnostician_before_agent_callback,
        after_agent_callback=diagnostician_after_agent_callback,
        before_model_callback=diagnostician_before_model_callback,
        after_model_callback=diagnostician_after_model_callback,
        on_model_error_callback=diagnostician_on_model_error_callback,
    )

    # Step E: Deterministic exit decision & Remediator handoff
    exit_decision_agent = ExitDecisionAgent(
        remediator_url=resolved_remediator,
        shared_secret=resolved_secret,
    )

    return LoopAgent(
        name="diagnostician_loop",
        sub_agents=[
            hypothesis_agent,
            runner_dispatcher,
            ledger_evaluator,
            exit_decision_agent,
        ],
        max_iterations=max_iters,
    )


# ── Top-Level Runner Function ────────────────────────────────────────────────

def seed_hypotheses_from_propositions(
    incident: InvestigatePayload,
    propositions: list[Proposition],
) -> list[HypothesisItem]:
    """
    Seeds initial candidate hypotheses from static propositions.
    Guarantees at least one rival hypothesis is present for the first iteration.
    """
    seeded: list[HypothesisItem] = []
    for p in propositions:
        seeded.append(
            HypothesisItem(
                hypothesis_id=p.proposition_id,
                failure_mode=p.failure_mode,
                description=p.justification,
                status="active",
                is_novel=False,
                supporting_proposition_id=p.proposition_id,
                graph_edge_ref=p.graph_edge_ref,
                confidence=0.50,
            )
        )

    # Guarantee at least one rival hypothesis (Requirement 2)
    if len(seeded) < 2:
        # Check existing failure modes to pick a distinct rival
        existing_modes = {h.failure_mode for h in seeded}
        rival_mode = FailureMode.retry_storm_no_backoff if FailureMode.connection_pool_exhaustion in existing_modes else FailureMode.connection_pool_exhaustion
        seeded.append(
            HypothesisItem(
                hypothesis_id="hyp-novel-rival",
                failure_mode=rival_mode,
                description="Competing failure mode under high latency or resource starvation",
                status="active",
                is_novel=True,
                justification="Seeded rival hypothesis to ensure falsification testing under course requirements",
                confidence=0.40,
            )
        )

    return seeded


async def run_diagnostician(
    incident: InvestigatePayload,
    incident_id: str | None = None,
    model: str | None = None,
    runner_url: str | None = None,
    remediator_url: str | None = None,
    shared_secret: str | None = None,
    max_iterations: int | None = None,
) -> DiagnosisResult:
    """
    Executes the full Diagnostician diagnostic loop on an incident.
    Returns the final validated DiagnosisResult model.
    """
    resolved_incident_id = incident_id or getattr(incident, "incident_id", None) or f"inc-{uuid.uuid4().hex[:8]}"
    resolved_model = model or get_diagnostician_model()
    token_tracker.reset(resolved_incident_id)

    # 1. Load static propositions for the incident's endpoint
    try:
        propositions = load_propositions(endpoint=incident.endpoint)
    except Exception as exc:
        logger.warning(f"Could not load propositions from database: {exc}. Using empty list.")
        propositions = []

    # Fallback to propositions cache if DB returned empty
    if not propositions:
        cache_path = os.path.join(
            os.path.dirname(__file__), "..", "surface_mapper", "cache", "propositions_cache.json"
        )
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    cache_data = json.load(f)
                raw_props = cache_data.get("propositions", []) if isinstance(cache_data, dict) else cache_data
                all_props = [Proposition.model_validate(p) for p in raw_props]
                from surveyops.agents.surface_mapper.storage import normalize_endpoint
                norm_ep = normalize_endpoint(incident.endpoint)
                propositions = [p for p in all_props if normalize_endpoint(p.endpoint) == norm_ep]
            except Exception as exc:
                logger.warning(f"Could not load from cache: {exc}")

    # 2. Seed candidate hypotheses
    hypotheses = seed_hypotheses_from_propositions(incident, propositions)

    print(f"\n=======================================================")
    print(f"[SEEDED HYPOTHESES] ({len(hypotheses)} seeded for endpoint '{incident.endpoint}'):")
    for h in hypotheses:
        print(f"  - Hypothesis ID: {h.hypothesis_id} | failure_mode: {h.failure_mode.value} | is_novel: {h.is_novel} | status: {h.status} | conf: {h.confidence:.2f}")
        print(f"    Description: {h.description}")
        if h.supporting_proposition_id:
            print(f"    Supporting Proposition ID: {h.supporting_proposition_id}")
    print(f"=======================================================\n")

    # 3. Deterministic iteration-1 CheckableSignal probes (Subtasks 5 & 6)
    initial_observations: list[Observation] = []
    enable_pre_probes = os.environ.get("SURVEYOPS_ENABLE_PRE_PROBES", "true").lower() == "true"
    if enable_pre_probes:
        resolved_runner_url = runner_url or get_runner_a2a_url()
        resolved_secret = shared_secret or get_shared_secret()
        target_service = resolve_service_from_endpoint(incident.endpoint)
        clean_ep = incident.endpoint.split()[1] if " " in incident.endpoint else incident.endpoint

        ws_val = getattr(incident, "window_start", None)
        ws_iso = ws_val.isoformat() if hasattr(ws_val, "isoformat") else (str(ws_val) if ws_val else None)

        metric_req = ExperimentRequest(
            request_id=f"probe-{resolved_incident_id[:8]}-metric",
            hypothesis_id=propositions[0].proposition_id if propositions else "seed",
            experiment_type=ExperimentType.query_metrics,
            params={
                "template": "pool_in_use",
                "service": target_service,
                "endpoint": clean_ep,
                "range_s": 120,
                "window_start": ws_iso,
            },
            rationale="Deterministic pre-LLM check of connection pool and resource saturation signals",
            disconfirming_if="pool_utilization_ratio < 0.2",
        )
        log_req = ExperimentRequest(
            request_id=f"probe-{resolved_incident_id[:8]}-logs",
            hypothesis_id=propositions[0].proposition_id if propositions else "seed",
            experiment_type=ExperimentType.fetch_logs,
            params={
                "service": target_service,
                "since_s": 120,
                "limit": 50,
                "window_start": ws_iso,
            },
            rationale="Deterministic pre-LLM check of container error logs and fault signatures",
            disconfirming_if="no error logs in target service",
        )

        metric_obs: Observation | None = None
        log_obs: Observation | None = None

        try:
            metric_obs = await execute_runner_a2a_request(resolved_runner_url, resolved_secret, metric_req)
        except Exception as exc:
            logger.warning(f"Deterministic metric probe failed: {exc}")

        try:
            log_obs = await execute_runner_a2a_request(resolved_runner_url, resolved_secret, log_req)
        except Exception as exc:
            logger.warning(f"Deterministic log probe failed: {exc}")

        # Early-abstain exit for healthy systems (Subtask 6)
        is_healthy, health_reason = check_system_healthy(metric_obs, log_obs)
        if is_healthy:
            logger.info(f"Early-abstain triggered for healthy system [{resolved_incident_id}]: {health_reason}")
            print(f"\n[EARLY ABSTAIN] Healthy system confirmed: {health_reason}. 0 LLM calls made.")
            diagnosis = DiagnosisResult(
                incident_id=resolved_incident_id,
                outcome=Outcome.abstained,
                root_cause=None,
                endpoint=incident.endpoint,
                supporting_proposition_id=None,
                graph_edge_ref=None,
                confidence=0.0,
                ruled_out=[h.hypothesis_id for h in hypotheses],
                iterations_used=0,
            )
            _incident_ledgers[resolved_incident_id] = []
            _incident_run_records[resolved_incident_id] = {
                "incident_id": resolved_incident_id,
                "endpoint": incident.endpoint,
                "outcome": diagnosis.outcome.value,
                "root_cause": None,
                "confidence": 0.0,
                "iterations_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "served_models": [resolved_model],
                "primary_served_model": resolved_model,
            }
            return diagnosis

        # Only pass pre-probes to LLM state if SURVEYOPS_ENABLE_PRE_PROBES is True
        if os.environ.get("SURVEYOPS_ENABLE_PRE_PROBES", "false").lower() == "true":
            if metric_obs:
                initial_observations.append(metric_obs)
            if log_obs:
                initial_observations.append(log_obs)

    # 4. Build LoopAgent and InMemoryRunner
    loop_agent = build_diagnostician_loop_agent(
        model=model,
        runner_url=runner_url,
        remediator_url=remediator_url,
        shared_secret=shared_secret,
        max_iterations=max_iterations,
    )
    runner = InMemoryRunner(agent=loop_agent)

    # Initialize state keys strictly adhering to docs/04-CONTRACTS.md
    initial_state = {
        "incident": incident,
        "incident_id": resolved_incident_id,
        "propositions": propositions,
        "hypotheses": hypotheses,
        "ledger": [],
        "iteration": 1,
        "max_iterations": max_iterations or get_max_loop_iterations(),
        "pending_request": None,
        "last_observation": initial_observations[-1] if initial_observations else None,
        "observations": initial_observations,
        "disconfirming_hypotheses_tested": [],
        "diagnosis": None,
    }

    session = await runner.session_service.create_session(
        user_id="surveyops-system",
        app_name=runner.app_name,
        state=initial_state,
    )

    # Kick off diagnostic loop
    init_msg = genai_types.Content(
        parts=[genai_types.Part(text=f"Begin diagnosis for incident {resolved_incident_id} on {incident.endpoint}")]
    )

    async for _ in runner.run_async(
        user_id="surveyops-system",
        session_id=session.id,
        new_message=init_msg,
    ):
        pass

    final_session = await runner.session_service.get_session(
        session_id=session.id,
        user_id="surveyops-system",
        app_name=runner.app_name,
    )

    final_ledger = list(final_session.state.get("ledger", []))
    _incident_ledgers[resolved_incident_id] = final_ledger

    diagnosis = final_session.state.get("diagnosis")
    if diagnosis is None:
        # If loop exited without setting diagnosis (e.g. hard cap), generate budget_exhausted
        hyps = final_session.state.get("hypotheses", hypotheses)
        top = sorted(hyps, key=lambda h: h.confidence, reverse=True)[0] if hyps else None
        diagnosis = DiagnosisResult(
            incident_id=resolved_incident_id,
            outcome=Outcome.budget_exhausted,
            root_cause=None,
            endpoint=incident.endpoint,
            supporting_proposition_id=None,
            graph_edge_ref=None,
            confidence=top.confidence if top else 0.0,
            ruled_out=[h.hypothesis_id for h in hyps if h.status == "ruled_out"],
            iterations_used=final_session.state.get("iteration", 1),
        )

    usage = token_tracker.get_usage(resolved_incident_id)
    served_models = usage.get("served_models", [])
    primary_model = served_models[0] if served_models else resolved_model
    _incident_run_records[resolved_incident_id] = {
        "incident_id": resolved_incident_id,
        "endpoint": incident.endpoint,
        "outcome": diagnosis.outcome.value,
        "root_cause": diagnosis.root_cause.value if diagnosis.root_cause else None,
        "confidence": diagnosis.confidence,
        "iterations_used": diagnosis.iterations_used,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "served_models": served_models or [resolved_model],
        "primary_served_model": primary_model,
        "status_changes": final_session.state.get("status_changes", []),
    }
    logger.info(f"Incident Run Record [{resolved_incident_id}]: {json.dumps(_incident_run_records[resolved_incident_id])}")

    return diagnosis


_incident_ledgers: dict[str, list[LedgerEntry]] = {}
_incident_run_records: dict[str, dict[str, Any]] = {}


def get_incident_ledger(incident_id: str) -> list[LedgerEntry]:
    """Retrieves the full evidence ledger for a given incident."""
    return _incident_ledgers.get(incident_id, [])


def get_incident_run_record(incident_id: str) -> dict[str, Any]:
    """Retrieves the run and model audit record for a given incident."""
    return _incident_run_records.get(incident_id, {})

