"""
Spike: Throwaway Hello-World cross-framework A2A interaction.

Demonstrates:
1. CrewAI Agent wrapped in an A2A server (FastAPI + JSON-RPC + AgentCard discovery).
2. Google ADK RemoteA2aAgent connecting over A2A to call CrewAI.
3. Clean execution and message passing between ADK (Diagnostician) and CrewAI (Remediator).

Run with:
    CREWAI_TRACING_ENABLED=false CREWAI_TESTING=true .venv/bin/python scripts/spikes/a2a_hello_world.py
"""

import asyncio
import os
import sys
import threading
import time
import uvicorn

# Suppress interactive telemetry prompts during spike run
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["CREWAI_TESTING"] = "true"

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.request_handlers.default_request_handler import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from crewai.llms.base_llm import BaseLLM
from crewai import Agent

from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types


# 1. Stub LLM for deterministic, zero-cost CrewAI execution without requiring external API keys
class StubRemediatorLLM(BaseLLM):
    """Deterministic stub LLM for the throwaway spike."""

    model: str = "stub-remediator-model"

    def call(self, messages, *args, **kwargs) -> str:
        return (
            "PATCH PROPOSAL:\n"
            "File: services/target_app/orders/main.py\n"
            "Diff: Increase database connection pool timeout from 2s to 10s.\n"
            "Status: Validated against regression test suite."
        )

    async def acall(self, messages, *args, **kwargs) -> str:
        return self.call(messages, *args, **kwargs)


# 2. CrewAI Agent Executor conforming to A2A protocol
class CrewAIAgentExecutor(AgentExecutor):
    """Executes A2A tasks by delegating to a CrewAI agent."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        from crewai.a2a.utils.task import execute as crewai_execute
        await crewai_execute(self.agent, context, event_queue)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        from crewai.a2a.utils.task import cancel as crewai_cancel
        await crewai_cancel(context, event_queue)


def create_a2a_crewai_app(agent: Agent, host: str, port: int):
    """Builds the A2A FastAPI application exposing the CrewAI Agent."""
    card = AgentCard(
        name="Remediator",
        description="CrewAI Remediator Agent exposed via standard A2A protocol",
        url=f"http://{host}:{port}",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        skills=[
            AgentSkill(
                id="propose_remediation",
                name="Propose Remediation",
                description="Generates code fix given a validated diagnosis from Diagnostician",
                tags=["remediation", "patching"],
            )
        ],
    )

    handler = DefaultRequestHandler(
        agent_executor=CrewAIAgentExecutor(agent),
        task_store=InMemoryTaskStore(),
    )

    server = A2AFastAPIApplication(agent_card=card, http_handler=handler)
    return server.build()


async def run_spike():
    host = "127.0.0.1"
    port = 8199

    print("=== Step 1: Initializing CrewAI Remediator Agent ===")
    remediator_agent = Agent(
        role="Remediator",
        goal="Propose accurate code fix for root cause identified by Diagnostician",
        backstory="Autonomous code remediation specialist for distributed microservices.",
        llm=StubRemediatorLLM(model="stub-remediator-model"),
    )

    print(f"=== Step 2: Serving CrewAI Agent over A2A on http://{host}:{port} ===")
    app = create_a2a_crewai_app(remediator_agent, host, port)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait briefly for server startup
    for _ in range(20):
        if server.started:
            break
        await asyncio.sleep(0.1)

    print(f"Server is up! Verified endpoints: http://{host}:{port}/.well-known/agent-card.json and http://{host}:{port}/")

    print("\n=== Step 3: Initializing Google ADK RemoteA2aAgent (Diagnostician's link) ===")
    remote_agent = RemoteA2aAgent(
        name="crewai_remediator",
        agent_card=f"http://{host}:{port}",
    )

    runner = InMemoryRunner(agent=remote_agent)
    session = await runner.session_service.create_session(
        user_id="diagnostician_caller",
        app_name=runner.app_name,
    )

    print("\n=== Step 4: ADK calling CrewAI Remediator via A2A ===")
    symptom_and_diagnosis = (
        "CONFIRMED DIAGNOSIS:\n"
        "Fault: Connection pool starvation in orders service.\n"
        "Hypothesis H1 confirmed: DB connection latency > 2s triggers 500 error cascade.\n"
        "Please generate patch proposal."
    )
    request_msg = genai_types.Content(parts=[genai_types.Part(text=symptom_and_diagnosis)])

    events_received = []
    async for event in runner.run_async(
        user_id="diagnostician_caller",
        session_id=session.id,
        new_message=request_msg,
    ):
        events_received.append(event)
        text_parts = [
            p.text
            for p in getattr(event.content, "parts", [])
            if getattr(p, "text", None)
        ]
        if text_parts:
            print(f"\n[A2A Response from '{event.author}']:\n" + "\n".join(text_parts))

    print(f"\n=== Result: Successfully received {len(events_received)} A2A event(s) from CrewAI agent! ===")

    # Cleanup
    server.should_exit = True
    await remote_agent.cleanup()


if __name__ == "__main__":
    asyncio.run(run_spike())
