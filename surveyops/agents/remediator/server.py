"""
A2A Server for the Remediator Agent (CrewAI).
Receives DiagnosisResult as TextPart JSON, executes remediation, and returns RemediationResult.
Requires shared secret on A2A endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Optional
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
from surveyops.schemas import Outcome, DiagnosisResult, RemediationResult
from surveyops.a2a_utils import from_a2a_message, to_a2a_text_part
from surveyops.agents.remediator.agent import run_remediation

logger = logging.getLogger("surveyops.remediator.server")


class RemediatorExecutor(AgentExecutor):
    """Executes production CrewAI remediation on receiving an A2A DiagnosisResult."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model
        self.last_diagnosis: Optional[DiagnosisResult] = None
        self.last_remediation: Optional[RemediationResult] = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or f"task-{uuid.uuid4().hex[:8]}"
        context_id = context.context_id or f"ctx-{uuid.uuid4().hex[:8]}"

        result = from_a2a_message(context.message, DiagnosisResult)
        if not result.is_ok or result.value is None:
            err_msg = f"Remediator rejected payload: {result.error}"
            logger.warning(err_msg)
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

        diagnosis = result.value
        self.last_diagnosis = diagnosis

        # Reject non-root_cause cleanly
        if diagnosis.outcome != Outcome.root_cause:
            clean_msg = f"Remediator rejected diagnosis: outcome '{diagnosis.outcome}' is not root_cause."
            logger.info(clean_msg)
            rejected_result = RemediationResult(
                pr_url=None,
                branch=None,
                files_changed=[],
                tests_added=[],
            )
            response_msg = Message(
                role=Role.agent,
                parts=[to_a2a_text_part(rejected_result)],
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
            return

        # Execute remediation
        try:
            remediation = await asyncio.to_thread(run_remediation, diagnosis, model=self.model)
            self.last_remediation = remediation
            self.last_pre_run = getattr(run_remediation, "last_pre_run", None)
            self.last_post_run = getattr(run_remediation, "last_post_run", None)

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
        except Exception as exc:
            logger.exception(f"Remediation execution failed: {exc}")
            await event_queue.enqueue_event(
                A2ATask(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=new_agent_text_message(f"Remediation failed: {exc}", context_id, task_id),
                    ),
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def create_remediator_app(
    host: str = "0.0.0.0",
    port: int = 8006,
    model: Optional[str] = None,
    executor: Optional[RemediatorExecutor] = None,
) -> FastAPI:
    card = AgentCard(
        name="Remediator",
        description="Autonomous CrewAI Remediator Agent for patch synthesis, regression testing, and PR creation",
        url=f"http://{host}:{port}",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="remediate",
                name="Remediate",
                description="Localize, patch, regression test, and open GitHub PR for root cause failure",
                tags=["remediation", "patching", "github"],
            )
        ],
    )
    exec_instance = executor or RemediatorExecutor(model=model)
    handler = DefaultRequestHandler(agent_executor=exec_instance, task_store=InMemoryTaskStore())
    a2a_app = A2AFastAPIApplication(agent_card=card, http_handler=handler)
    fastapi_app = a2a_app.build()
    fastapi_app.state.executor = exec_instance

    @fastapi_app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "GET" and (
            request.url.path.startswith("/.well-known/") or request.url.path in ("/docs", "/openapi.json")
        ):
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        secret_header = request.headers.get("X-Shared-Secret")
        token = (
            auth_header[7:].strip()
            if auth_header and auth_header.startswith("Bearer ")
            else (secret_header.strip() if secret_header else None)
        )
        expected = os.environ.get("INVESTIGATE_SHARED_SECRET", "dev-secret")
        if not token or token != expected:
            return JSONResponse(status_code=403, content={"error": "Forbidden: invalid shared secret"})
        return await call_next(request)

    return fastapi_app


app = create_remediator_app()

if __name__ == "__main__":
    port = int(os.environ.get("REMEDIATOR_PORT", "8006"))
    uvicorn.run("surveyops.agents.remediator.server:app", host="0.0.0.0", port=port)
