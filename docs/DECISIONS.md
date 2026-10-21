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
- **2026-10-17** Ground-truth root cause for DB latency injection is `connection_pool_exhaustion` (FailureMode enum value). Reason: pool exhaustion is the application-level weakness the Remediator can patch (pool size, overflow, timeouts); DB latency is the external stressor that exposes it. Rejected: root cause = DB latency (infrastructure fault with no code fix).

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

**14 – Fault Injection, Alerting & Stub /investigate (DECIDED 2026-10-17)**
- **Toxiproxy transparent proxy:** Inserted in compose between payment service and Postgres on port 5433 (`payment-postgres` proxy forwarding to `postgres:5432`). Zero app code modifications required.
- **Alertmanager authentication:** Alertmanager v0.27 rejects arbitrary `headers` under `http_config`. Used native `authorization.type: Bearer` with `credentials_file: /tmp/token` populated dynamically from `INVESTIGATE_SHARED_SECRET` by the container entrypoint.
- **Dual auth support in `/investigate`:** Accepts either `X-Shared-Secret` or `Authorization: Bearer <secret>` headers.
- **Symptom-only contract & gap handling:** Incoming firing alerts parsed to `InvestigatePayload` without root-cause labels. Inferred fields and gaps (observed numeric value missing in Alertmanager webhook, logged as `-1.0` sentinel to be queried by Diagnostician) explicitly documented.
- **Ground-truth label & isolation:** `eval/faults/inject_latency.py` writes ground-truth records with `failure_mode: "connection_pool_exhaustion"` (matching `FailureMode` enum in `docs/04-CONTRACTS.md`) to `eval/ground_truth/` (gitignored). Kept strictly isolated from `surveyops/`.
- **Trial reset:** `scripts/reset_trial.sh` clears toxics and truncates payments/orders rows for reproducible testing.

**15 – Schemas, A2A Serialization & Incident Deduplication (DECIDED 2026-10-17)**
- **InvestigatePayload.observed optional:** Reconciled to `float | None = None`. Alertmanager webhooks omit numeric scalar observed metrics; making it optional allows clean ingestion while preserving the contract for Diagnostician runtime enrichment.
- **Pydantic schema modularity:** Created domain modules under `surveyops/schemas/` (`failure_modes`, `propositions`, `incidents`, `experiments`, `diagnosis`, `remediation`, `reports`) matching `docs/04-CONTRACTS.md`.
- **A2A transport utilities:** Built `surveyops/a2a_utils.py` providing `to_a2a_text_part` and `from_a2a_message` returning typed `A2ADecodeResult[T]`, safely handling `ValidationError` without unhandled exceptions.
- **Incident deduplication:** `/investigate` enforces at most one open incident per `(service, endpoint)` using in-memory `IncidentDedupStore` (with explicit TODO to persist in PostgreSQL table in Phase 2). Duplicate alerts are logged and ignored; incidents are closable via `close_incident()` or `POST /incidents/close`.

**16 – Experiment Runner, MCP Tools & Network Isolation (DECIDED 2026-10-17)**
- **Log Source Decision:** Docker SDK (read-only socket) selected for container log retrieval. Avoids heavy external logging infra (Loki/Elastic) while preserving container environment portability. All output is strictly sanitized: ANSI escape sequences stripped, line length truncated to 1000 chars, total payload capped to 50KB, wrapped in `<UNTRUSTED_SERVICE_LOGS>`, and flagged with `contains_untrusted_text=True` on the `Observation`.
- **Deterministic Runner Dispatch Decision:** Runner agent dispatches `ExperimentRequest` deterministically (mapping `ExperimentType` directly to vetted MCP tools) rather than via an LLM. Rationale: incoming requests are already structured Pydantic contracts and Runner's invariant is to return raw observations without interpreting causes. Deterministic execution eliminates prompt injection risks from untrusted log text, guarantees sub-millisecond dispatch overhead, and costs 0 LLM tokens. Note: an LLM-driven ADK version is a small drop-in change if needed for coursework viva.
- **MCP Server & SDK Layout:** FastMCP server in `surveyops/mcp_servers/server.py` (`mcp>=1.2.0,<2.0.0`), exposing three vetted tools: `prometheus_query` (named templates only: `p99_latency`, `error_rate`, `request_rate`, `pool_in_use`, `pool_capacity`), `fetch_logs` (Docker SDK stdout for allowed target services), and `run_load_burst` (concurrency and duration capped). Every tool return is validated into a Pydantic model (`PrometheusQueryReturn`, `FetchLogsReturn`, `LoadBurstReturn`).
- **Two-Layer Network Isolation:** Localhost port binding only isolates the host; containers sharing a Docker bridge can resolve and connect to internal container ports. Restructured `infra/docker-compose.yml` to introduce an internal `data_net` network. `toxiproxy` is placed exclusively on `data_net`. `payment` and `postgres` join both `data_net` and `target_net`. `surveyops` containers (`surveyops-api`, `surveyops_runner`) join `target_net` only. Network-level DNS and routing isolation prevents the Runner or API from ever reaching `toxiproxy:8474`.
- **A2A Interface & Transport:** Runner exposed as an ADK A2A agent on port 8005 using `A2AFastAPIApplication` and `DefaultRequestHandler` with `RunnerAgentExecutor`. Incoming messages decoded via `from_a2a_message(..., ExperimentRequest)`; outgoing raw `Observation`s returned as `TextPart` JSON. Validation failures or guardrail rejections transition task status to `failed` without crashing.

**17 – Experiment Runner Hardening, Isolation & MCP Protocol (DECIDED 2026-10-17)**
- **p99 Latency Discrepancy & Prometheus Histogram Interpolation:** Under an active latency fault, Prometheus `p99_latency` returned `0.0495s` immediately post-injection, while synthetic burst measured ~3000ms. Root cause: Prometheus evaluates `histogram_quantile(0.99, rate(...[window]))` by linearly interpolating within histogram bucket boundaries. For the `le="0.05"` bucket, when 100% of historical samples in the rate window fall below 50ms (prior to sufficient 15s scrape intervals accumulating fault traffic), Prometheus computes $0 + 0.99 \times 0.05 = 0.0495\text{s}$. Once the scrape intervals (e.g. 60s/120s/300s) accumulate fault metrics, Prometheus correctly reports `10.0s` (the highest bucket ceiling). Synthetic burst tests live endpoints synchronously in real-time, instantly observing the 3000ms latency.
- **Docker Log Proxy Isolation:** Replaced direct `docker.sock` volume mount in Runner with an isolated `docker-log-proxy` container exposing TCP port 2375 (`DOCKER_HOST: tcp://docker_log_proxy:2375`). The proxy mounts `/var/run/docker.sock` read-only, restricts API methods to read-only `GET`, handles Docker HTTP chunked transfer encoding, and enforces a strict container allowlist (`target_orders`, `target_payment`, `target_inventory`, `target_auth`). Any request attempting to inspect or fetch logs from `toxiproxy`, `postgres`, or `prometheus` is blocked with HTTP 403 Forbidden.
- **MCP Protocol Enforcement via ClientSession:** Runner agent invokes MCP tools exclusively through an MCP `ClientSession` (`session.call_tool`) over in-memory streams connected to the FastMCP server, eliminating direct Python function imports and guaranteeing strict protocol separation.
- **Runner A2A Endpoint Authentication:** Runner A2A HTTP server enforces shared secret authentication (`X-Shared-Secret` or `Authorization: Bearer <secret>`) matching `INVESTIGATE_SHARED_SECRET` on all message and task endpoints, returning HTTP 403 Forbidden on unauthorized requests while leaving agent discovery (`/.well-known/agent.json`) unauthenticated.

**18 – Surface Mapper: Propositions Storage, Tool Architecture & Guardrails (DECIDED 2026-10-17)**
- **Where Propositions Live:** Propositions are stored in SurveyOps's dedicated PostgreSQL database (`surveyops_postgres`, image `pgvector/pgvector:pg16`), in the `propositions` table with indexes on `endpoint` and `failure_mode`. Results are also cached on disk at `surveyops/agents/surface_mapper/cache/propositions_cache.json` so repeated test runs and orchestrations avoid redundant LLM invocations and token usage.
- **Plain Python Tools vs MCP for Surface Mapper:** Deterministic plain Python tools (`graph_builder.py` using AST, `spec_parser.py` parsing OpenAPI specs) chosen over MCP servers. Rationale: MCP is designed for external running services requiring credential and runtime isolation (such as the Experiment Runner querying Prometheus and Docker logs). Surface Mapper strictly analyzes static, local codebase artifacts before faults occur and never touches live systems. Plain Python tools eliminate IPC latency and process overhead while ensuring complete unit testability.
- **Deterministic Guardrails & Edge Reference Enforcement:** The LLM is never trusted to invent dependency edges or endpoints. A post-execution guardrail validates all generated propositions against the AST dependency graph and OpenAPI specs. Any proposition whose endpoint is not in the spec, whose `graph_edge_ref` is not an existing edge in the graph, or whose `failure_mode` is not in the `FailureMode` enum is dropped.
- **SurveyOps PostgreSQL & Network Isolation:** SurveyOps internal database is placed on a dedicated `ops_net` Docker bridge network. The target application services (`orders`, `payment`, `inventory`, `auth`, `toxiproxy`, `postgres`) do not join `ops_net`, maintaining strict network-level isolation between SurveyOps storage and target application infrastructure.

**19 – Primary Model Selection & DeepSeek Fallback Policy (DECIDED 2026-10-18)**
- Using Gemini for now; DeepSeek is the fallback, with switch triggers:
  1. After enabling billing for Gemini, rate limits or errors still block runs, or
  2. Gemini’s structured output fails repeatedly on the Diagnostician.
- Model selection is governed exclusively by per-agent environment variables (`MAPPER_MODEL`, `DIAGNOSTICIAN_MODEL`, `REMEDIATOR_MODEL`, `BASELINE_MODEL`) with no hardcoded model strings anywhere in the codebase.

**20 – Diagnostician Architecture, Falsification & Exit Decision Logic (DECIDED 2026-10-18)**
- **Google ADK LoopAgent Architecture:** Diagnostician implemented as an ADK `LoopAgent` with 4 sub-agents: `hypothesis_and_experiment_selector` (LlmAgent), `runner_dispatcher` (deterministic A2ARunnerDispatcherAgent), `ledger_evaluator` (LlmAgent), and `exit_decision` (deterministic ExitDecisionAgent). Hard cap enforced by `MAX_LOOP_ITERATIONS` (default 5, configurable via env).
- **Zero Execution Authority & Deterministic A2A Transport:** Diagnostician touches no live system tools directly. All interaction with the live target system is mediated via A2A JSON-RPC to the Runner on port 8005. Payloads (`ExperimentRequest`) are deterministically constructed, validated, and serialized; received `Observation` models are parsed safely.
- **Pre-Flight Falsification Rule & Guardrail Allowlists:** Pre-flight validator (`falsification.py`) enforces that every experiment specifies a non-empty `disconfirming_if` condition, checks template against vetted metrics allowlist (`p99_latency`, `error_rate`, `request_rate`, `pool_in_use`, `pool_capacity`), verifies service and endpoint against OpenAPI allowlist, and bounds concurrency and duration. Non-compliant requests are rejected before ever reaching the wire.
- **Pure Code Exit Logic:** Exit determination is strictly executed in Python code (`exit_logic.py`), not by LLM recommendation. `Outcome.root_cause` is declared only when: (1) top hypothesis confidence $\ge$ `CONFIDENCE_THRESHOLD` (0.80), (2) margin over top rival $\ge$ `CONFIDENCE_MARGIN` (0.20), (3) at least one disconfirming experiment was completed against it, and (4) hypothesis is active. Otherwise, the loop continues until `MAX_LOOP_ITERATIONS`, returning `budget_exhausted` or `abstained`.
- **A2A Remediator Handoff:** On `root_cause`, `DiagnosisResult` is dispatched over A2A to the Remediator stub on port 8006 using `to_a2a_text_part()`. Transport exceptions or non-200 responses are caught and logged without aborting or crashing the diagnostic process.
- **Untrusted Log Sandboxing & Prompt Guardrails:** Raw observation logs flagged with `contains_untrusted_text=True` are encapsulated in `<UNTRUSTED_SYSTEM_DATA>` XML blocks with explicit system instructions to treat logs as passive untrusted data rather than system instructions, neutralizing prompt injection vectors.
- **Structured JSON Callbacks, Token Tracking & Rate-Limit Backoff:** Before/after agent, model, and tool callbacks emit structured JSON lines tracking incident ID, iteration, tokens, model latency, and error states. `TokenUsageTracker` maintains an exact per-incident token audit. Transient 429 / 503 errors trigger backoff based on explicit `retryDelay` metadata.
- **LLM Record/Replay Cache:** Response cache keyed on model, response schema, system instructions, and request contents caches completions to disk (`diagnostician_cache.json`), saving token quota during automated test suites and repeated evaluations.

**21 – LLM Provider Switch to DeepSeek & litellm Model Connector (DECIDED 2026-10-19)**
- **Reason for Switch:** Gemini free-tier daily project limits (strict 20 requests/day per project on `gemini-3.6-flash`/`gemini-3.8-flash`, along with tight 5 RPM limits) repeatedly blocked live 5-run evaluation suites. Switched all agent model configurations (`MAPPER_MODEL`, `DIAGNOSTICIAN_MODEL`, `REMEDIATOR_MODEL`, `BASELINE_MODEL`) to `deepseek/deepseek-chat`.
- **Privacy Note:** SurveyOps operates strictly on synthetic, containerized e-commerce mock services (`orders`, `payment`, `inventory`, `auth`). No production data or customer PII is ever processed. Prompts never include secrets, tokens, or live credentials. All infrastructure is deployed locally, and API requests to DeepSeek endpoints (hosted outside India) contain only sanitized code snippets, metric summaries, and synthetic log records.
- **Model Connector Status:** `litellm` is added as a model connector dependency used internally by Google ADK (`google.adk.models.lite_llm.LiteLlm`) and CrewAI (`crewai.LLM`). It serves strictly as a transport client to access OpenAI-compatible APIs; it is NOT used as an agent framework and is never imported into core `surveyops/` source code.
- **Switch-Back Triggers:** The project will evaluate reverting to Gemini if:
  1. Paid billing with elevated tier quota is activated on the Google Cloud project, OR
  2. DeepSeek's structured output (Pydantic schema validation) rate drops below 9/10 on ADK or CrewAI, OR
  3. API availability, network timeouts, or rate limits impact evaluation throughput.

**22 – DeepSeek-Flash Native vs. Fallback Paths Evaluation (DECIDED 2026-10-19)**
- **Model Evaluated:** `deepseek/deepseek-flash` (served as `deepseek-flash` / DeepSeek-V4.1-Flash-1017).
- **Native Path Incompatibility (HTTP 400):** Both ADK (`output_schema` alone and with tools) and CrewAI (`output_pydantic`) fail 0/10 with HTTP 400 (`"This response_format type is unavailable now"`). Root cause: DeepSeek's API does not support OpenAI Strict Structured Outputs (`response_format={"type": "json_schema"}`).
- **LiteLLM `json_object` Mode Supported:** Direct LiteLLM calls with `response_format={"type": "json_object"}` succeed without error, returning valid JSON objects under the reported model name `deepseek-flash`.
- **Viable Fallback Paths (100% Success):**
  1. *Tool-Call Path (ADK with NO `output_schema` + submit tool):* ADK agents calling `submit_diagnosis` and `submit_ledger_update` (validating Pydantic schemas inside the tool call) achieved **10/10 (100%)** success across both diagnostic and ledger tasks.
  2. *Plain JSON in Prompt + Pydantic Validation + 1 Retry:* Achieved **10/10 (100%) first-try** valid rates for both DiagnosisResult and LedgerUpdate schemas (0 retries needed).
- **Tool-Call Injection Resilience:** Tested against adversarial prompt injection embedded in untrusted log telemetry. DeepSeek remained 100% clean (0 jailbreaks), diagnosed the true root cause (`connection_pool_exhaustion`), explicitly flagged and disregarded the injected instructions in its rationale, recorded 4.33s latency and 1,008 total tokens (492 prompt / 516 completion), and confirmed returned model `deepseek-flash`.

**23 – Structured Output Adapter & Provider Switch to deepseek/deepseek-flash (DECIDED 2026-10-19)**
- **Adapter Architecture (`surveyops/llm_utils.py`):** Because DeepSeek's API rejects OpenAI JSON schema structured outputs (`response_format={"type": "json_schema"}`) with HTTP 400, a reusable structured-output adapter was implemented:
  1. *ADK Agents (Tool-Calling Mode):* `build_adk_submission_tool` creates dynamic ADK tools matching target Pydantic schemas. Tool execution automatically validates kwargs against the schema and injects them into agent state. Plain-text model replies without tool calls raise `PlainTextReplyError`, enabling deterministic retry.
  2. *CrewAI Agents (JSON Mode):* `execute_with_json_retry` extracts JSON from markdown fences using regex (`extract_json_from_text`), parses with `model_validate_json`, and performs 1-shot retry feeding validation errors back to the model upon failure.
- **Model Alias Pinning & Served Model Auditing:** All four agent models (`MAPPER_MODEL`, `DIAGNOSTICIAN_MODEL`, `REMEDIATOR_MODEL`, `BASELINE_MODEL`) are configured to `deepseek/deepseek-flash`. The served model name (reported by API response headers as `deepseek-flash` / DeepSeek-V4.1-Flash-1017) is captured via `diagnostician_after_model_callback` and audited in the per-incident run record.
- **Cache Key Isolation:** LLM cache keys (`diagnostician/cache.py` and `surface_mapper/cache/`) incorporate the model name and execution mode (`tool_call`, `json_schema`, `plain_json`) to prevent cross-model or cross-mode cache collisions.
- **Data Governance & Privacy:** Prompts transmit only synthetic service topologies, metric aggregates, and sanitized logs. No credentials or customer data exist in the workspace or leave the host network.
- **End-to-End Validation:** Full test suite passes (119/119 tests). One live evaluation run against active connection pool exhaustion diagnosed the root cause in 5 iterations with 0 structure failures (173.06s wall time, 307,586 tokens).

## 2026-10-19: Diagnostician Reasoning Quality & Calibration Fixes
- **Item 0 Root Cause & Proposition Seeding:** Discovered that Surface Mapper stored endpoints with HTTP method prefixes (`POST /payment/charge`), while Prometheus alerts and `/investigate` use path-only strings (`/payment/charge`). Exact string comparison caused `load_propositions` to return empty, triggering synthetic `hyp-novel-rival` fallback. Fixed via `normalize_endpoint` in `storage.py` and `agent.py`, and populated all 18 propositions into SurveyOps Postgres.
- **Verdict Step Validation:** Embedded `validate_ledger_tool_call` validator hook inside `build_adk_submission_tool` for `submit_ledger_update`. Validates verbatim quoting of `disconfirming_if`, presence of `disconfirming_justification` with observation field and value, bracketed `[request_id:field]` citations matching stored observations, and rejection if confidence is lowered without `disconfirming_condition_met=True`. ADK native tool return loop enables 1-shot in-agent correction without crashing.
- **Evidence Quality Telemetry:** Enhanced Prometheus MCP tool (`query_metrics` for `pool_in_use`) to return current value, `pool_max_in_use` over window, `pool_capacity`, and `pool_utilization_ratio`. Latency (`p99_latency`) queries also bundle request rates and pool gauges. System instructions enforce that low p99 with low request count is weak evidence due to histogram scrape lag, gauges $\ge 80\%$ denote saturation, and log errors naming mechanisms must be addressed.
- **Heuristic Confidence Score in Code:** Independent heuristic confidence score computed in `compute_heuristic_confidence_score` from supporting cited observation count ($N_{obs} \ge 2 \implies 0.50$), disconfirming test survival (0.30), and confidence margin over runner-up ($\min(0.20, \text{margin} \times 0.4)$).
- **Exit Gate Criteria:** Evaluated strictly in code (`evaluate_exit_conditions`). `Outcome.root_cause` requires ALL of: (a) experiment run specifically against leader's `disconfirming_if`, (b) verdict "not met", (c) margin $\ge \text{CONFIDENCE_MARGIN}$ (0.20), (d) cited evidence matching `predicted_signal` keywords, and (e) heuristic confidence score $\ge \text{CONFIDENCE_THRESHOLD}$ (0.80).
- **5-Run Live Benchmark:** Executed on active `connection_pool_exhaustion` fault with fixed `deepseek/deepseek-flash` model. 5/5 runs accurately diagnosed ground truth (100% accuracy, mean iterations: 1.8, mean tokens: 62.8k, mean wall time: 49.4s). Heuristic confidence score and self-reported confidences logged for all runs. Ground truth read strictly post-run.

## 2026-10-19: Diagnostician Validation, Isolation, and Control Baselines
- **Window Contamination & Trial Isolation:** `reset_trial.sh` clears Toxiproxy toxics and resets Postgres tables, but leaves Prometheus TSDB data and Docker stdout logs intact. Fixed cross-trial contamination by:
  1. Anchoring Prometheus queries to incident `window_start` (`range_s = max(15, min(3600, int(now - window_start)))`).
  2. Filtering Docker container logs line-by-line via RFC3339 timestamps to drop any log line emitted prior to `window_start`.
- **Token Inflation Breakdown & Reduction:** In un-capped runs, ~100k tokens per run was dominated by raw stdout Docker logs (100 lines with multi-line tracebacks) compounded across tool retries. Resolved without losing cited evidence by:
  1. Capping log lines to 25 lines prioritizing error signal lines (`error`, `timeout`, `exception`, `failed`, `traceback`, `pool`, `5xx`).
  2. Capping verbose past evidence citations in the prompt to 100 characters. Single-iteration prompts reduced from ~80k to ~15k tokens.
- **Terminology:** Replaced "calibrated confidence" with "heuristic confidence score" across code and documentation to accurately reflect rule-based scoring.
- **Ground Truth Enum:** Added `upstream_dependency_failure = "upstream_dependency_failure"` to `FailureMode` for Control B.
- **Empirical Validation Outcomes (15 runs on deepseek/deepseek-flash):**
  - *Control A (Healthy system, 5 runs):* 4/5 abstained (0 root cause); 1 confident-wrong (`missing_index_n_plus_1` on Run 4).
  - *Control B (5xx upstream fault with unsaturated pool, 5 runs):* 5/5 abstained; connection pool exhaustion correctly ruled out in 5/5 runs. 0 confident-wrong.
  - *Pool Fault Repeat Batch (5 runs):* 3/5 diagnosed `connection_pool_exhaustion` (scores 0.88-0.89), 1 diagnosed `missing_index_n_plus_1` (score 0.91), 1 `budget_exhausted`.
  - *Combined 10 Pool Fault Runs:* 8/10 diagnosed `connection_pool_exhaustion` (80%), 1 missed cause, 1 budget exhausted.
  - *Confident-Wrong Across Controls:* 1 / 10 runs (10%).

## 2026-10-21: Confident-Wrong Elimination via Deterministic Checkable Signals & Positive Evidence Gate
- **Root Cause Autopsy:** Control A run 4 and Pool Batch 2 falsely diagnosed `missing_index_n_plus_1` because `predicted_signal` matching relied on generic substring keywords ("payment", "charge", "idempotency") present in normal logs and tracebacks. Without requiring positive proof, the absence of disconfirmation plus keyword matches inflated the heuristic score to 0.90+.
- **Machine-Checkable Signal (`CheckableSignal`):** Extended `Proposition` schema with `checkable_signal: CheckableSignal` (validated by Pydantic: `metric_name`, `comparator`, `threshold`, `log_pattern`). All 18 Surface Mapper propositions carry a checkable signal in cache and SurveyOps Postgres. Propositions without a valid checkable signal cannot win.
- **Deterministic Positive Evidence Gate:** Replaced keyword matching with `evaluate_checkable_signal_for_leader`. Evaluates cited observations against the signal. Requires `has_positive_evidence=True`; without it, heuristic confidence score is capped at 0.35, blocking `Outcome.root_cause`.
- **Positive Evidence Exit Condition:** Declaring root cause requires positive empirical evidence, not merely surviving falsification. If the loop terminates or LLM recommends conclude/abstain without positive evidence, the system deterministically exits with `Outcome.abstained`.
- **Token Budget & Determinism:** Configured hard token budget (`MAX_INCIDENT_TOKEN_BUDGET`, default 60k) forcing `Outcome.budget_exhausted` when exceeded. Set Diagnostician agents to `temperature=0.0`.
- **Full History Injection:** Custom prompt builders format full ledger history and all cited observations into prompt instructions each turn, ensuring full context even with ADK `include_contents="none"`.