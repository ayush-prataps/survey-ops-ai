"""
Spike: Structured Pydantic Payload Validation across ADK -> CrewAI A2A Hop.

Verifies:
1. Exact DiagnosisResult and RemediationResult contracts from docs/04-CONTRACTS.md.
2. DataPart vs TextPart payload transmission over A2A.
3. CrewAI Task output_pydantic=RemediationResult integration with Stub LLM.
4. Negative tests:
   a) Malformed payload rejected cleanly by CrewAI side without server crash.
   b) Invalid JSON response caught as validation error on ADK side.
5. Session state isolation check: ADK session state never leaks across A2A boundary.
6. Callback check: ADK before/after agent callbacks fire and observe input/output.
"""

import asyncio
from enum import Enum
import json
import os
import threading
from typing import Any

# Suppress interactive telemetry prompts during spike run
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["CREWAI_TESTING"] = "true"

import uvicorn
from pydantic import BaseModel, ValidationError

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.request_handlers.default_request_handler import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    DataPart,
    Message,
    Part,
    Task as A2ATask,
    TaskState,
    TaskStatus,
    TextPart,
)
from a2a.utils import (
    get_data_parts,
    new_agent_text_message,
)

from crewai import Agent, Crew, Task
from crewai.llms.base_llm import BaseLLM

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types


# =====================================================================
# 1. Pydantic Models exactly as specified in docs/04-CONTRACTS.md
# =====================================================================

class FailureMode(str, Enum):
    connection_pool_exhaustion = "connection_pool_exhaustion"
    missing_index_n_plus_1 = "missing_index_n_plus_1"
    race_condition_idempotency = "race_condition_idempotency"
    pagination_off_by_one = "pagination_off_by_one"
    retry_storm_no_backoff = "retry_storm_no_backoff"


class Outcome(str, Enum):
    root_cause = "root_cause"
    budget_exhausted = "budget_exhausted"
    abstained = "abstained"


class DiagnosisResult(BaseModel):      # Diagnostician -> Remediator (A2A)
    incident_id: str
    outcome: Outcome
    root_cause: FailureMode | None
    endpoint: str
    supporting_proposition_id: str | None
    graph_edge_ref: str | None
    confidence: float | None
    ruled_out: list[str]
    iterations_used: int


class RemediationResult(BaseModel):    # Remediator output
    pr_url: str | None
    branch: str | None
    files_changed: list[str]
    tests_added: list[str]


# =====================================================================
# 2. Stub LLMs for CrewAI Remediator
# =====================================================================

class StubRemediatorLLM(BaseLLM):
    """Returns valid JSON or invalid string based on mode."""
    model: str = "stub-remediator"

    def __init__(self, mode: str = "valid"):
        super().__init__(model="stub-remediator")
        self._mode = mode

    def call(self, messages, *args, **kwargs) -> str:
        if self._mode == "invalid_json":
            return "NOT_VALID_JSON{pr_url: broken"
        return json.dumps({
            "pr_url": "https://github.com/ayush-prataps/survey-ops-ai/pull/1",
            "branch": "fix/connection-pool-timeout",
            "files_changed": ["services/target_app/orders/main.py"],
            "tests_added": ["tests/test_orders_pool_regression.py"]
        })

    async def acall(self, messages, *args, **kwargs) -> str:
        return self.call(messages, *args, **kwargs)


# =====================================================================
# 3. Server-side Agent Executor with Validation & State Leak Detection
# =====================================================================

server_state_leak_detected = False
last_received_payload_type = None

class StructuredRemediatorExecutor(AgentExecutor):
    """A2A Agent Executor validating incoming DiagnosisResult."""

    def __init__(self, llm_mode: str = "valid", use_output_pydantic: bool = True):
        self.llm_mode = llm_mode
        self.use_output_pydantic = use_output_pydantic
        self.remediator_agent = Agent(
            role="Remediator",
            goal="Propose patch for diagnosed fault",
            backstory="Senior Reliability & Backend Engineer",
            llm=StubRemediatorLLM(mode=llm_mode),
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        global server_state_leak_detected, last_received_payload_type

        task_id = context.task_id or "task-0"
        context_id = context.context_id or "ctx-0"

        # Check if ADK internal session state leaked over A2A wire
        raw_dump = str(context._params) if context._params else ""
        if "adk_secret_canary" in raw_dump:
            server_state_leak_detected = True

        # Extract payload from DataParts or TextPart
        data_parts = []
        if context.message and context.message.parts:
            data_parts = get_data_parts(context.message.parts)

        text_input = context.get_user_input()

        diagnosis: DiagnosisResult
        try:
            if data_parts:
                last_received_payload_type = "DataPart"
                diagnosis = DiagnosisResult.model_validate(data_parts[0])
            elif text_input:
                last_received_payload_type = "TextPart"
                diagnosis = DiagnosisResult.model_validate_json(text_input)
            else:
                raise ValueError("No payload provided in message parts")
        except (ValidationError, Exception) as val_err:
            # Clean rejection: emit failed task event with error message, do NOT crash server
            err_msg = f"Validation failed for incoming DiagnosisResult: {val_err}"
            await event_queue.enqueue_event(
                A2ATask(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=new_agent_text_message(err_msg, context_id, task_id),
                    ),
                )
            )
            return

        # Update LLM mode dynamically if changed
        self.remediator_agent.llm = StubRemediatorLLM(mode=self.llm_mode)

        # Execute CrewAI Task with output_pydantic
        if self.use_output_pydantic:
            crew_task = Task(
                description=f"Generate remediation for diagnosed fault: {diagnosis.root_cause} on {diagnosis.endpoint}",
                expected_output="RemediationResult model",
                agent=self.remediator_agent,
                output_pydantic=RemediationResult,
            )
        else:
            crew_task = Task(
                description=f"Generate remediation for diagnosed fault: {diagnosis.root_cause} on {diagnosis.endpoint}",
                expected_output="JSON string matching RemediationResult",
                agent=self.remediator_agent,
            )

        crew = Crew(agents=[self.remediator_agent], tasks=[crew_task])
        crew_output = await crew.akickoff()

        # Handle result formatting
        if self.use_output_pydantic and getattr(crew_output, "pydantic", None):
            remediation: RemediationResult = crew_output.pydantic
            result_str = remediation.model_dump_json()
        else:
            result_str = str(crew_output.raw)

        history = [context.message] if context.message else []
        history.append(new_agent_text_message(result_str, context_id, task_id))

        await event_queue.enqueue_event(
            A2ATask(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.completed),
                history=history,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def make_a2a_server(executor: AgentExecutor, port: int):
    card = AgentCard(
        name="Remediator",
        description="CrewAI Remediator Agent exposed via A2A",
        url=f"http://127.0.0.1:{port}",
        version="1.0.0",
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        skills=[AgentSkill(id="remediate", name="Remediate Bug", description="Generates patch proposal", tags=["remediation"])]
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    server_app = A2AFastAPIApplication(agent_card=card, http_handler=handler).build()
    config = uvicorn.Config(app=server_app, host="127.0.0.1", port=port, log_level="error")
    return uvicorn.Server(config)


# =====================================================================
# Main Spike Runner Testing All 6 Requirements
# =====================================================================

async def main():
    port = 8245
    print("=================================================================")
    print("  SPIKE: Structured Pydantic Payload Validation (ADK <-> CrewAI) ")
    print("=================================================================")

    current_executor = StructuredRemediatorExecutor(llm_mode="valid", use_output_pydantic=True)
    server = make_a2a_server(current_executor, port)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    for _ in range(25):
        if server.started:
            break
        await asyncio.sleep(0.1)

    print(f"[1] A2A Server online on port {port}")

    # Track callback invocations across turns
    callback_history = []

    def before_callback(ctx: CallbackContext):
        last_evt = ctx.session.events[-1] if ctx.session.events else None
        input_text = None
        if last_evt and last_evt.content and last_evt.content.parts:
            input_text = [p.text for p in last_evt.content.parts if getattr(p, "text", None)]
        callback_history.append({
            "phase": "before",
            "agent_name": ctx.agent_name,
            "session_state": dict(ctx.session.state),
            "input_preview": input_text[0][:60] if input_text else None,
            "event_count": len(ctx.session.events),
        })
        return None

    def after_callback(ctx: CallbackContext):
        last_evt = ctx.session.events[-1] if ctx.session.events else None
        output_text = None
        if last_evt and last_evt.content and last_evt.content.parts:
            output_text = [p.text for p in last_evt.content.parts if getattr(p, "text", None)]
        callback_history.append({
            "phase": "after",
            "agent_name": ctx.agent_name,
            "output_preview": output_text[0][:60] if output_text else None,
            "event_count": len(ctx.session.events),
        })
        return None

    remote_agent = RemoteA2aAgent(
        name="crewai_remediator",
        agent_card=f"http://127.0.0.1:{port}",
        before_agent_callback=before_callback,
        after_agent_callback=after_callback,
    )
    runner = InMemoryRunner(agent=remote_agent)

    # -----------------------------------------------------------------
    # Test 1 & 2 & 3 & 5: Happy path with valid DiagnosisResult (TextPart)
    # -----------------------------------------------------------------
    session = await runner.session_service.create_session(
        user_id="test_user",
        app_name=runner.app_name,
    )
    # Set ADK session state to test state isolation
    session.state["adk_secret_canary"] = "CONFIDENTIAL_DIAGNOSTIC_STATE"

    valid_diagnosis = DiagnosisResult(
        incident_id="INC-2026-001",
        outcome=Outcome.root_cause,
        root_cause=FailureMode.connection_pool_exhaustion,
        endpoint="POST /orders/checkout",
        supporting_proposition_id="SM-014",
        graph_edge_ref="orders.checkout() -> db.connection_pool",
        confidence=0.95,
        ruled_out=["missing_index_n_plus_1", "pagination_off_by_one"],
        iterations_used=3,
    )

    print("\n--- TEST 1: Sending Valid DiagnosisResult over A2A (TextPart JSON) ---")
    msg = genai_types.Content(parts=[genai_types.Part(text=valid_diagnosis.model_dump_json())])

    received_remediation = None
    async for event in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=msg,
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text:
                    received_remediation = RemediationResult.model_validate_json(p.text)

    print("Sent DiagnosisResult:")
    print(" ", repr(valid_diagnosis))
    print("Received & Validated RemediationResult on ADK side:")
    print(" ", repr(received_remediation))
    assert received_remediation is not None
    assert received_remediation.branch == "fix/connection-pool-timeout"
    assert "services/target_app/orders/main.py" in received_remediation.files_changed
    print(">>> Test 1 PASSED: Structured Pydantic payload roundtrip succeeded!")

    # -----------------------------------------------------------------
    # Test 2B: Try DataPart via context_builder on RemoteA2aAgent
    # -----------------------------------------------------------------
    print("\n--- TEST 2: Testing A2A DataPart Transmission ---")
    def data_part_context_builder(ctx, agent_name, converter):
        data_p = Part(root=DataPart(data=valid_diagnosis.model_dump(mode="json")))
        return [data_p], None

    data_remote_agent = RemoteA2aAgent(
        name="crewai_remediator_data",
        agent_card=f"http://127.0.0.1:{port}",
        context_builder=data_part_context_builder,
    )
    data_runner = InMemoryRunner(agent=data_remote_agent)
    data_session = await data_runner.session_service.create_session(
        user_id="test_user_data",
        app_name=data_runner.app_name,
    )

    data_part_remediation = None
    async for event in data_runner.run_async(
        user_id="test_user_data",
        session_id=data_session.id,
        new_message=genai_types.Content(parts=[genai_types.Part(text="trigger")]),
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text:
                    data_part_remediation = RemediationResult.model_validate_json(p.text)

    print(f"Server received payload type: {last_received_payload_type}")
    print("Received RemediationResult from DataPart input:", repr(data_part_remediation))
    assert data_part_remediation is not None
    print(">>> Test 2 PASSED: A2A DataPart successfully parsed into DiagnosisResult!")

    # -----------------------------------------------------------------
    # Test 4a: Negative Test - Malformed input to CrewAI (missing field & bad enum)
    # -----------------------------------------------------------------
    print("\n--- TEST 4a: Negative Test - Malformed Input Payload ---")
    malformed_json = json.dumps({
        "incident_id": "INC-BAD",
        "outcome": "not_a_valid_outcome_enum",
        # missing endpoint, root_cause, iterations_used, etc.
    })
    bad_msg = genai_types.Content(parts=[genai_types.Part(text=malformed_json)])

    malformed_rejection_event = None
    neg_session = await runner.session_service.create_session(
        user_id="test_user_neg",
        app_name=runner.app_name,
    )
    async for event in runner.run_async(
        user_id="test_user_neg",
        session_id=neg_session.id,
        new_message=bad_msg,
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text and "Validation failed" in p.text:
                    malformed_rejection_event = p.text
                    print(f"  CrewAI cleanly rejected payload with validation error:\n    {p.text[:120]}...")

    assert malformed_rejection_event is not None, "Expected clean validation rejection event from CrewAI"

    print("Server remains healthy. Probing server health with fresh valid request...")
    probe_session = await runner.session_service.create_session(
        user_id="test_user_probe",
        app_name=runner.app_name,
    )
    probe_remediation = None
    async for event in runner.run_async(
        user_id="test_user_probe",
        session_id=probe_session.id,
        new_message=msg,
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text:
                    probe_remediation = RemediationResult.model_validate_json(p.text)

    assert probe_remediation is not None, "Server should process valid request after bad request"
    print(">>> Test 4a PASSED: Malformed input rejected cleanly without server crash!")

    # -----------------------------------------------------------------
    # Test 4b: Negative Test - Stub returns invalid JSON to ADK
    # -----------------------------------------------------------------
    print("\n--- TEST 4b: Negative Test - Stub Returns Invalid JSON ---")
    current_executor.llm_mode = "invalid_json"
    current_executor.use_output_pydantic = False  # Let raw invalid string pass through

    invalid_json_caught = False
    inv_session = await runner.session_service.create_session(
        user_id="test_user_inv",
        app_name=runner.app_name,
    )
    async for event in runner.run_async(
        user_id="test_user_inv",
        session_id=inv_session.id,
        new_message=msg,
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text:
                    try:
                        RemediationResult.model_validate_json(p.text)
                    except ValidationError as ve:
                        invalid_json_caught = True
                        print(f"  ADK successfully caught ValidationError on invalid JSON: {type(ve).__name__}")
    assert invalid_json_caught is True
    print(">>> Test 4b PASSED: ADK side successfully caught validation error on malformed response!")

    # -----------------------------------------------------------------
    # Test 5: State Isolation Verification
    # -----------------------------------------------------------------
    print("\n--- TEST 5: State Isolation Check ---")
    print(f"ADK session state contained: 'adk_secret_canary' = '{session.state.get('adk_secret_canary')}'")
    print(f"Server state leak detected: {server_state_leak_detected}")
    assert server_state_leak_detected is False
    print(">>> Test 5 PASSED: Session state is completely isolated across A2A boundary!")

    # -----------------------------------------------------------------
    # Test 6: Callback Verification
    # -----------------------------------------------------------------
    print("\n--- TEST 6: Callback Check ---")
    print(f"Total callbacks logged: {len(callback_history)}")
    first_before = next((c for c in callback_history if c["phase"] == "before"), None)
    first_after = next((c for c in callback_history if c["phase"] == "after"), None)
    print("before_agent_callback fired:", first_before is not None)
    if first_before:
        print("  before callback saw agent:", first_before["agent_name"])
        print("  before callback saw session state:", first_before["session_state"])
        print("  before callback saw input preview:", first_before["input_preview"])
    print("after_agent_callback fired:", first_after is not None)
    if first_after:
        print("  after callback saw agent:", first_after["agent_name"])
        print("  after callback saw output preview:", first_after["output_preview"])
    assert first_before is not None
    assert first_after is not None
    print(">>> Test 6 PASSED: ADK callbacks fired and observed input and output!")

    print("\n=================================================================")
    print("  ALL 6 SPIKE TESTS COMPLETED SUCCESSFULLY!                      ")
    print("=================================================================")

    server.should_exit = True
    await remote_agent.cleanup()
    await data_remote_agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
