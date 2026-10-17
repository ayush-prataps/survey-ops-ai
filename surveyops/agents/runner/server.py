"""
A2A Server exposing Experiment Runner Agent.
Conforms strictly to docs/04-CONTRACTS.md A2A transport rules:
- Payloads travel as TextPart(model.model_dump_json())
- Rejects malformed requests with failed task status, never crashing
- Returns raw Observation data
"""

import os
import uuid
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

from surveyops.schemas import ExperimentRequest, Observation
from surveyops.a2a_utils import from_a2a_message, to_a2a_text_part
from .agent import ExperimentRunnerAgent


class RunnerAgentExecutor(AgentExecutor):
    """Executes A2A tasks by delegating to the deterministic ExperimentRunnerAgent."""

    def __init__(self, agent: ExperimentRunnerAgent | None = None) -> None:
        self.agent = agent or ExperimentRunnerAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or f"task-{uuid.uuid4().hex[:8]}"
        context_id = context.context_id or f"ctx-{uuid.uuid4().hex[:8]}"

        # 1. Decode incoming message into ExperimentRequest
        result = from_a2a_message(context.message, ExperimentRequest)
        if not result.is_ok or result.value is None:
            err_msg = f"ExperimentRunner rejected payload: {result.error}"
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

        request: ExperimentRequest = result.value

        # 2. Execute experiment via Runner Agent (catches guardrail errors)
        try:
            observation: Observation = await self.agent.run(request)
        except Exception as exc:
            err_msg = f"Experiment execution failed: {exc}"
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

        # 3. Return validated Observation encoded as A2A TextPart
        text_part = to_a2a_text_part(observation)
        response_message = Message(
            role=Role.agent,
            parts=[text_part],
            message_id=f"msg-{uuid.uuid4().hex[:8]}",
        )

        await event_queue.enqueue_event(
            A2ATask(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.completed,
                    message=response_message,
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or "task-cancel"
        context_id = context.context_id or "ctx-cancel"
        await event_queue.enqueue_event(
            A2ATask(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.canceled,
                    message=new_agent_text_message("Task was canceled", context_id, task_id),
                ),
            )
        )


def create_runner_a2a_app(host: str = "0.0.0.0", port: int = 8005) -> FastAPI:
    """Builds the A2A FastAPI application exposing the Experiment Runner Agent."""
    card = AgentCard(
        name="ExperimentRunner",
        description="ADK Experiment Runner Agent executing metrics queries, log fetches, and load bursts over A2A",
        url=f"http://{host}:{port}",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="execute_experiment",
                name="Execute Experiment",
                description="Executes a diagnostic experiment request and returns raw observations",
                tags=["telemetry", "metrics", "logs", "loadtest"],
            )
        ],
    )

    executor = RunnerAgentExecutor()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AFastAPIApplication(agent_card=card, http_handler=handler)
    fastapi_app = a2a_app.build()

    @fastapi_app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Allow agent discovery and documentation endpoints
        if request.method == "GET" and (
            request.url.path in ("/.well-known/agent.json", "/.well-known/agent-card.json", "/docs", "/openapi.json")
            or request.url.path.startswith("/.well-known/")
        ):
            return await call_next(request)

        # Require shared secret on all other requests (POST / JSON-RPC)
        auth_header = request.headers.get("Authorization")
        secret_header = request.headers.get("X-Shared-Secret")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif secret_header:
            token = secret_header.strip()

        expected_secret = os.environ.get("INVESTIGATE_SHARED_SECRET", "dev-secret")
        if not token or token != expected_secret:
            return JSONResponse(
                status_code=403,
                content={"error": "Forbidden: invalid or missing shared secret"},
            )
        return await call_next(request)

    return fastapi_app


# App instance for ASGI servers
app = create_runner_a2a_app(
    host=os.environ.get("RUNNER_HOST", "0.0.0.0"),
    port=int(os.environ.get("RUNNER_PORT", "8005")),
)

if __name__ == "__main__":
    port = int(os.environ.get("RUNNER_PORT", "8005"))
    uvicorn.run("surveyops.agents.runner.server:app", host="0.0.0.0", port=port, reload=False)
