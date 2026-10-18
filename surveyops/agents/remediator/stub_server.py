"""
Stub A2A Server for Remediator (Step 6 only, do not build real Remediator).
Provides a lightweight A2A endpoint receiving DiagnosisResult and returning RemediationResult.
Used by the Diagnostician when concluding root_cause.
"""

import os
import uuid
from typing import Any
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    Message,
    Role,
    Task as A2ATask,
    TaskState,
    TaskStatus,
)
from a2a.utils import new_agent_text_message
from surveyops.schemas import DiagnosisResult, RemediationResult
from surveyops.a2a_utils import from_a2a_message, to_a2a_text_part


class StubRemediatorExecutor(AgentExecutor):
    """Executes stub remediation on receiving a validated DiagnosisResult."""

    def __init__(self, simulate_failure: bool = False) -> None:
        self.simulate_failure = simulate_failure
        self.last_received_diagnosis: DiagnosisResult | None = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or f"task-{uuid.uuid4().hex[:8]}"
        context_id = context.context_id or f"ctx-{uuid.uuid4().hex[:8]}"

        result = from_a2a_message(context.message, DiagnosisResult)
        if not result.is_ok or result.value is None:
            err_msg = f"StubRemediator rejected payload: {result.error}"
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

        self.last_received_diagnosis = result.value

        if self.simulate_failure:
            await event_queue.enqueue_event(
                A2ATask(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=new_agent_text_message("Simulated Remediator failure", context_id, task_id),
                    ),
                )
            )
            return

        remediation = RemediationResult(
            pr_url="https://github.com/survey-ops-ai/target-app/pull/101",
            branch=f"fix/{result.value.root_cause.value if result.value.root_cause else 'incident'}",
            files_changed=["services/target_app/payment/main.py"],
            tests_added=["tests/test_payment_resilience.py"],
        )

        response_msg = Message(
            role=Role.agent,
            parts=[to_a2a_text_part(remediation)],
            message_id=f"msg-{uuid.uuid4().hex[:8]}",
        )
        await event_queue.enqueue_event(
            A2ATask(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.completed,
                    message=response_msg,
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def create_stub_remediator_app(host: str = "0.0.0.0", port: int = 8006, simulate_failure: bool = False) -> FastAPI:
    card = AgentCard(
        name="RemediatorStub",
        description="Stub A2A Remediator Agent for testing Diagnostician handoff",
        url=f"http://{host}:{port}",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(id="remediate", name="Remediate", description="Stub remediation", tags=["remediation"])],
    )
    executor = StubRemediatorExecutor(simulate_failure=simulate_failure)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    a2a_app = A2AFastAPIApplication(agent_card=card, http_handler=handler)
    fastapi_app = a2a_app.build()

    @fastapi_app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "GET" and (request.url.path.startswith("/.well-known/") or request.url.path in ("/docs", "/openapi.json")):
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        secret_header = request.headers.get("X-Shared-Secret")
        token = auth_header[7:].strip() if auth_header and auth_header.startswith("Bearer ") else (secret_header.strip() if secret_header else None)
        expected = os.environ.get("INVESTIGATE_SHARED_SECRET", "dev-secret")
        if not token or token != expected:
            return JSONResponse(status_code=403, content={"error": "Forbidden: invalid shared secret"})
        return await call_next(request)

    return fastapi_app


app = create_stub_remediator_app()

if __name__ == "__main__":
    port = int(os.environ.get("REMEDIATOR_PORT", "8006"))
    uvicorn.run("surveyops.agents.remediator.stub_server:app", host="0.0.0.0", port=port)
