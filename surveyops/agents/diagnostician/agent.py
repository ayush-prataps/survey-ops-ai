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
from .exit_logic import evaluate_exit_conditions, get_max_loop_iterations
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


# ── Step B & C: Deterministic A2A Runner Dispatcher Agent ───────────────────

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
        incident_id = getattr(incident, "incident_id", "inc-unknown")
        iteration = ctx.session.state.get("iteration", 1)

        # 1. Validate structured LLM output with Pydantic in our own code
        step_output = None
        if isinstance(raw_output, dict):
            try:
                step_output = ExperimentStepOutput.model_validate(raw_output)
            except ValidationError as exc:
                logger.warning(f"Malformed LLM ExperimentStepOutput: {exc}")
        elif isinstance(raw_output, ExperimentStepOutput):
            step_output = raw_output

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

        # Update hypotheses list in session state
        updated_hyps = step_output.hypotheses or ctx.session.state.get("hypotheses", [])
        chosen = step_output.chosen_experiment
        if hasattr(chosen, "to_experiment_request"):
            req = chosen.to_experiment_request()
        elif isinstance(chosen, ExperimentRequest):
            req = chosen
        elif isinstance(chosen, dict):
            req = ExperimentRequest.model_validate(chosen)
        else:
            req = chosen

        # 2. Enforce Falsification Rule & Allowlist checks
        guardrail_error: str | None = None
        try:
            validate_experiment_request(req)
        except ValueError as val_err:
            guardrail_error = str(val_err)
            logger.warning(f"Experiment request rejected by guardrail: {guardrail_error}")

        observation: Observation
        disconfirming_tested = list(ctx.session.state.get("disconfirming_hypotheses_tested", []))

        if guardrail_error:
            # Produce synthetic observation recording rejection rather than crashing
            observation = Observation(
                request_id=req.request_id,
                source="diagnostician_falsification_guardrail",
                data={"error": guardrail_error, "status": "rejected_by_guardrail"},
                collected_at=datetime.now(timezone.utc),
            )
        else:
            # 3. Send over A2A to the Runner
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
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
                    headers = {"X-Shared-Secret": self.shared_secret}
                    resp = await client.post(self.runner_url, json=payload, headers=headers)

                    if resp.status_code != 200:
                        observation = Observation(
                            request_id=req.request_id,
                            source="a2a_runner_http_error",
                            data={"http_status": resp.status_code, "body": resp.text},
                            collected_at=datetime.now(timezone.utc),
                        )
                    else:
                        resp_data = resp.json()
                        result = resp_data.get("result", {})
                        status = result.get("status", {})
                        if status.get("state") == "completed":
                            decoded = from_a2a_message(status.get("message"), Observation)
                            if decoded.is_ok and decoded.value is not None:
                                observation = decoded.value
                                # Record successful disconfirming test for this hypothesis
                                if req.hypothesis_id not in disconfirming_tested:
                                    disconfirming_tested.append(req.hypothesis_id)
                            else:
                                observation = Observation(
                                    request_id=req.request_id,
                                    source="a2a_decode_error",
                                    data={"decode_error": decoded.error},
                                    collected_at=datetime.now(timezone.utc),
                                )
                        else:
                            observation = Observation(
                                request_id=req.request_id,
                                source="runner_task_failed",
                                data={"task_status": status},
                                collected_at=datetime.now(timezone.utc),
                            )
            except Exception as net_exc:
                logger.error(f"Failed to communicate with Runner over A2A: {net_exc}")
                observation = Observation(
                    request_id=req.request_id,
                    source="runner_connection_error",
                    data={"exception": str(net_exc)},
                    collected_at=datetime.now(timezone.utc),
                )

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
        incident_id = getattr(incident, "incident_id", "inc-unknown")
        iteration = ctx.session.state.get("iteration", 1)
        max_iterations = ctx.session.state.get("max_iterations", get_max_loop_iterations())
        disconfirming_tested = set(ctx.session.state.get("disconfirming_hypotheses_tested", []))
        hypotheses = ctx.session.state.get("hypotheses", [])

        # 1. Validate structured ledger output
        ledger_out = None
        if isinstance(raw_ledger, dict):
            try:
                ledger_out = LedgerUpdateOutput.model_validate(raw_ledger)
            except ValidationError as exc:
                logger.warning(f"Malformed LLM LedgerUpdateOutput: {exc}")
        elif isinstance(raw_ledger, LedgerUpdateOutput):
            ledger_out = raw_ledger

        existing_ledger = list(ctx.session.state.get("ledger", []))
        recommendation = "continue"

        if ledger_out is not None:
            recommendation = ledger_out.llm_recommendation
            # Append new ledger entries
            for entry in ledger_out.ledger_entries:
                existing_ledger.append(entry)
                # Sync confidence & status back to working hypotheses
                for h in hypotheses:
                    if h.hypothesis_id == entry.hypothesis_id:
                        h.confidence = entry.confidence
                        h.status = entry.status

        # 2. Evaluate exit conditions by code
        exit_decision: ExitDecision = evaluate_exit_conditions(
            hypotheses=hypotheses,
            tested_disconfirming_hypotheses=disconfirming_tested,
            iteration=iteration,
            max_iterations=max_iterations,
            llm_recommendation=recommendation,
        )

        logger.info(
            f"ExitDecision (iteration {iteration}/{max_iterations}): should_exit={exit_decision.should_exit}, "
            f"outcome={exit_decision.outcome.value}, reason={exit_decision.reason}"
        )

        if exit_decision.should_exit:
            top_h = exit_decision.top_hypothesis
            diagnosis = DiagnosisResult(
                incident_id=incident_id,
                outcome=exit_decision.outcome,
                root_cause=top_h.failure_mode if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                endpoint=endpoint,
                supporting_proposition_id=top_h.supporting_proposition_id if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                graph_edge_ref=top_h.graph_edge_ref if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
                confidence=top_h.confidence if (exit_decision.outcome == Outcome.root_cause and top_h) else None,
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
    iteration = state.get("iteration", 1)
    max_iters = state.get("max_iterations", get_max_loop_iterations())

    return build_hypothesis_and_experiment_prompt(
        incident=incident,
        propositions=propositions,
        hypotheses=hypotheses,
        ledger=ledger,
        last_observation=last_obs,
        iteration=iteration,
        max_iterations=max_iters,
    )


def get_ledger_evaluator_instruction(ctx: ReadonlyContext) -> str:
    state = getattr(ctx, "state", {})
    incident = state.get("incident")
    hypotheses = state.get("hypotheses", [])
    pending_request = state.get("pending_request", {})
    last_obs = state.get("last_observation")
    iteration = state.get("iteration", 1)
    max_iters = state.get("max_iterations", get_max_loop_iterations())

    return build_ledger_evaluation_prompt(
        incident=incident,
        hypotheses=hypotheses,
        pending_request=pending_request,
        last_observation=last_obs,
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

    # Step A: Propose/update hypotheses and pick experiment
    hypothesis_agent = LlmAgent(
        name="hypothesis_and_experiment_selector",
        model=resolved_model,
        instruction=get_hypothesis_agent_instruction,
        global_instruction=SYSTEM_INSTRUCTIONS,
        output_schema=ExperimentStepOutput,
        output_key="step_output_raw",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
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
    ledger_evaluator = LlmAgent(
        name="ledger_evaluator",
        model=resolved_model,
        instruction=get_ledger_evaluator_instruction,
        global_instruction=SYSTEM_INSTRUCTIONS,
        output_schema=LedgerUpdateOutput,
        output_key="ledger_output_raw",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
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
                all_props = [Proposition.model_validate(p) for p in cache_data.get("propositions", [])]
                propositions = [p for p in all_props if p.endpoint == incident.endpoint]
            except Exception as exc:
                logger.warning(f"Could not load from cache: {exc}")

    # 2. Seed candidate hypotheses
    hypotheses = seed_hypotheses_from_propositions(incident, propositions)

    # 3. Build LoopAgent and InMemoryRunner
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
        "last_observation": None,
        "observations": [],
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

    return diagnosis


_incident_ledgers: dict[str, list[LedgerEntry]] = {}


def get_incident_ledger(incident_id: str) -> list[LedgerEntry]:
    """Retrieves the full evidence ledger for a given incident."""
    return _incident_ledgers.get(incident_id, [])

