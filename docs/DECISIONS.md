# Decisions

Format: date, decision, reason, alternatives rejected. Append only. Update OPEN items when resolved.

## Decided
- **2026-10-14** Agent frameworks are ADK and CrewAI only. Reason: course rule. Rejected: LiteLLM (not an agent framework but avoids any debate), LangGraph (keep out of this repo).
- **2026-10-14** Detection is deterministic (Prometheus rule + Alertmanager webhook), agents start after alert. Reason: continuous LLM polling burns rate-limited quota and is less reliable. Rejected: agent watching logs.
- **2026-10-14** `/investigate` payload is symptom only. Ground-truth label lives in a separate store read only by the scorer. Reason: keeps trials blind. Rejected: label in payload (earlier synopsis wording).
- **2026-10-14** Surface Mapper output is unranked. Reason: no runtime evidence to justify scores.
- **2026-10-14** Cross-framework A2A is Diagnostician (ADK) -> Remediator (CrewAI). Reason: course requires interop between the two frameworks.
- **2026-10-14** Report is a deterministic template, not a 5th agent. Reason: templating from validated data is not reasoning.
- **2026-10-14** Remediator never merges. Human-in-the-loop.
- **2026-10-14** Proposition carries `graph_edge_ref` passed to Remediator as a localization hint. Semantic search confirms or is fallback.
- **2026-10-14** Deadlines in the handout are negotiable with faculty. Optimize for a robust system, avoid overhead.
- **2026-10-14** Docs structure: one canonical AGENTS.md, thin tool-specific pointers, one topic per docs file.

## OPEN (decide, then move up)
1. Log source: Docker SDK stdout vs Loki.
2. Where propositions live: Postgres table (assumed) vs other.
3. Cloud provider and VM size.
4. Langfuse Cloud vs self-hosted.
7. Gemini tier allocation per agent.
8. Silent faults: invariant counters and probes vs scope them out.
9. Whether Surface Mapper graph/spec tools are wrapped as MCP or plain tools.
10. Confirm with faculty: synopsis length (handout says one page) and Phase-2 date.

## SPIKE RESULTS (2026-10-14, resolved items 5 and 6)

**5 – CrewAI native A2A support (RESOLVED)**
- Spike run on crewai==1.15.28 (Python 3.11.14, macOS arm64).
- `crewai.a2a` is a full native subpackage with submodules:
  `config`, `types`, `wrapper`, `auth`, `task_helpers`, `templates`, `updates`, `utils`, `extensions`, `errors`.
- Top-level exports: `A2AServerConfig`, `A2AClientConfig`, `A2AConfig`.
- `crewai.a2a.wrapper` **exists** but imports from an `a2a` Python package (Google A2A SDK) that is not in our dependency list and is NOT installed. Loading the wrapper raises `ModuleNotFoundError: No module named 'a2a'`.
- **Decision:** Use `crewai.a2a` config + server config objects natively to expose the Remediator as an A2A endpoint, but do **not** add `a2a` (Google's separate SDK) as a dependency — that would add a third framework. The Diagnostician (ADK) calls the Remediator's A2A endpoint over HTTP using ADK's built-in A2A client. The wrapper module is optional tooling; the config/types subpackages are usable standalone. Verify the full integration path in the Remediator spike before coding.

**6 – ADK `output_schema` vs tool use (RESOLVED)**
- Spike run on google-adk==2.12.0.
- `LlmAgent` has `output_schema: Optional[SchemaType] = None` and `tools` as separate fields.
- Source docstring (line 468–471 of llm_agent.py): *"The ADK supports using `output_schema` and `tools` together. It works by exposing tools during the thought loop and enforcing structure only on the final output."*
- **Decision:** `output_schema` does NOT disable tool use in ADK 2.12.0. The Diagnostician can use `output_schema=DiagnosisResult` alongside MCP tools without losing tool-calling capability. No workaround needed.

**11 – A2A Protocol SDK Cross-Framework Spike: ADK -> CrewAI (RESOLVED)**
- **Dependency check & Extras:**
  - `google-adk[a2a]` requires `a2a-sdk[http-server]>=0.3.4,<2`.
  - `crewai[a2a]` requires `a2a-sdk~=0.3.10` (`>=0.3.10, <0.4`).
  - Both cleanly resolve together to `a2a-sdk==0.3.26` with zero conflicts against `google-adk==2.12.0` and `crewai==1.15.28`.
  - Installing `google-adk[a2a]==2.12.0` and `crewai[a2a]==1.15.28` adds `a2a-sdk==0.3.26`, `aiocache==0.12.3`, `aiomcache==0.8.2`, `google-api-core==2.34.0`, `httpx-auth==0.23.1`, `proto-plus==1.29.0`, and `redis==8.1.0`.
  - `a2a-sdk` is the common protocol specification library implementing Agent2Agent (A2A), not a separate agent orchestration framework.
- **Throwaway spike built & verified:** `scripts/spikes/a2a_hello_world.py`
  - CrewAI Remediator Agent served via `A2AFastAPIApplication` (`a2a.server.apps.jsonrpc.fastapi_app`) with `DefaultRequestHandler` and `AgentExecutor` wrapping `crewai.a2a.utils.task.execute`.
  - Google ADK Diagnostician caller connects via `RemoteA2aAgent(name="crewai_remediator", agent_card="http://127.0.0.1:8199")` driven by `InMemoryRunner`.
- **Endpoints used:**
  - Agent Discovery: `GET /.well-known/agent-card.json` (and fallback `GET /.well-known/agent.json`)
  - Agent Execution: `POST /` (JSON-RPC)
  - Schema documentation: `GET /docs`, `GET /openapi.json`
- **What worked:**
  - ADK's `RemoteA2aAgent` fetched and validated the `AgentCard` at discovery time.
  - ADK sent the user diagnostic payload in an A2A task message to `POST /`.
  - CrewAI Remediator processed the task and emitted the response event back over the A2A event stream.
  - ADK received and displayed the patched code response cleanly (`Hello from CrewAI Remediator over A2A!`).
- **What failed initially & remedies applied:**
  - `InMemoryRunner.session_service.create_session`: Passing an arbitrary `app_name` caused `SessionNotFoundError` during `runner.run_async()`. Remedy: must supply `app_name=runner.app_name` (defaulting to `'InMemoryRunner'`).
  - CrewAI interactive trace prompt: CrewAI prompted to share traces via `[y/N]` stdin with a 20s timeout. Remedy: set environment variables `CREWAI_TRACING_ENABLED=false` and `CREWAI_TESTING=true`.
  - BaseLLM abstract requirements: CrewAI `BaseLLM` subclasses must declare a `model: str` attribute and implement both `call` and `acall`.
- **Decision:** Use official `a2a-sdk==0.3.26` via framework extras for the Diagnostician -> Remediator link. This satisfies Requirement 4 & Requirement 6 directly without custom HTTP shims.

**12 – Structured Pydantic Payloads across A2A Hop (RESOLVED)**
- **Part types:** Both work. `TextPart` with JSON string (`model_dump_json()`) is native in ADK and directly deserializable with `model_validate_json()`. `DataPart` works when using ADK's `context_builder` (CrewAI receives it via `get_data_parts()`). Recommendation: use `TextPart(json)` to avoid binary blob wrapping in Gemini parts.
- **CrewAI `output_pydantic`:** Works cleanly on `Task(output_pydantic=RemediationResult)`. `crew_output.pydantic` yields a fully validated Pydantic model instance when the stub LLM returns matching JSON.
- **Negative test (malformed input):** CrewAI rejected invalid `DiagnosisResult` with Pydantic `ValidationError`. Server caught it, returned a failed task status, and remained healthy without crashing.
- **Negative test (malformed output):** ADK side caught Pydantic `ValidationError` when the stub returned invalid JSON.
- **State isolation:** ADK session state is completely isolated across the A2A wire (server received 0 leaked keys). Everything Remediator needs must be passed explicitly in the message.
- **Callbacks:** ADK `before_agent_callback` and `after_agent_callback` fire around `RemoteA2aAgent`, observing input and output content parts.

**13 – Target App Architecture & Metrics Shape (DECIDED 2026-10-14)**
- Four microservices built: `auth`, `inventory`, `payment`, `orders` with SQLite/Postgres.
- Metrics engine: `prometheus-fastapi-instrumentator`. Verified metric names and labels:
  - Request counts: `http_requests_total{handler="...", method="...", status="2xx|4xx|5xx"}`
  - Latency: `http_request_duration_seconds_bucket{handler="...", method="...", le="..."}`
  - DB pool gauge on payment: `payment_db_pool_in_use_connections` and `payment_db_pool_capacity`.
- Database host abstraction: Payment service uses single env var `POSTGRES_HOST=postgres` to allow inserting Toxiproxy in Step 2 without code modifications.
- Traffic generator: Headless Locust container in compose generating continuous load (~2.5 req/s) to ensure Prometheus rules have continuous traffic to evaluate.