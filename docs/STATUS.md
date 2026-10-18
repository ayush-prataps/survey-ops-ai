# Status

Last updated: 2026-10-17

## Done
- Synopsis drafted (needs alignment with DECISIONS.md, do not keep rewriting it)
- Architecture, workflow, requirements mapping documented
- Repo structure and ground-truth docs created
- Consistency check: docs/03-REQUIREMENTS.md vs 11 course requirements — see report below
- Minimal env: .venv (Python 3.11.14) with google-adk==2.12.0 + crewai==1.15.28 coexisting; requirements.txt pinned
- Spikes 5 and 6 resolved (see DECISIONS.md SPIKE RESULTS)
- A2A Protocol SDK spike completed: `google-adk[a2a]` + `crewai[a2a]` coexisting with `a2a-sdk==0.3.26`; throwaway end-to-end spike in `scripts/spikes/a2a_hello_world.py` verified (ADK RemoteA2aAgent calling CrewAI over A2A)
- Structured Pydantic payload spike completed in `scripts/spikes/a2a_structured_pydantic_spike.py`: roundtrip DiagnosisResult/RemediationResult, TextPart vs DataPart, CrewAI output_pydantic, negative guardrails tests, state isolation, and callback checks verified
- Target application built and running via Docker Compose (`infra/docker-compose.yml`):
  - 4 FastAPI services: `orders` (8001), `payment` (8002), `inventory` (8003), `auth` (8004) with structured JSON stdout logs
  - PostgreSQL container with seeded catalog and initial schema (`infra/postgres/init.sql`)
  - Explicit SQLAlchemy QueuePool and idempotency check on payment service
  - Prometheus (9090) scraping all 4 services every 15s (`infra/prometheus/prometheus.yml`)
  - Grafana (3000) provisioned with Prometheus datasource and target app telemetry dashboard
  - Headless Locust load generator (`infra/loadgen/`) driving continuous synthetic traffic (~2.5 req/s)
  - Integration tests in `tests/test_target_app.py` (7/7 passed, 10/10 total passed)
- Target app histogram buckets customized across all 4 services to `(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)` so p99 above 1s is visible on `/metrics`
- Step 2 completed (Fault injection, detection, stub /investigate):
  - Toxiproxy inserted in `infra/docker-compose.yml` between payment and Postgres (port 5433).
  - Fault scripts built: `eval/faults/inject_latency.py` and `eval/faults/clear_faults.py`. Injections write ground-truth records to `eval/ground_truth/` (gitignored).
  - Prometheus alert rules configured in `infra/prometheus/alert_rules.yml` (`HighP99Latency` and `HighErrorRate` over 1m window).
  - Alertmanager in compose (`infra/alertmanager/alertmanager.yml`) with webhook receiver to `http://surveyops-api:8000/investigate`, `send_resolved: false`, authenticated via Bearer token.
  - SurveyOps API stub in `surveyops/api/` with `POST /investigate` validating token, parsing Alertmanager webhooks to `InvestigatePayload` (symptom only), logging structured JSON, returning 200.
  - Trial reset script in `scripts/reset_trial.sh` (clears toxics and truncates payments/orders tables).
  - End-to-end verified: injected latency -> p99 jumped to >10s -> Prometheus alerts fired -> Alertmanager delivered webhook -> `/investigate` logged symptom payload. Clean reset verified.
  - Unit tests: `tests/test_investigate_api.py` (10/10 passed; 20/20 total tests pass).
- Step 3 completed (Schemas, A2A Transport Utils, Incident Deduplication):
  - Every model in `docs/04-CONTRACTS.md` implemented as Pydantic models across modular files in `surveyops/schemas/` with `__init__.py` exports.
  - Reconciled `InvestigatePayload.observed` as `float | None = None` in schema, API, and contracts doc.
  - Built `surveyops/a2a_utils.py` for model encoding to `TextPart` and safe decoding via typed `A2ADecodeResult[T]`.
  - Added incident deduplication to `/investigate` (in-memory store with clear TODO to migrate to Postgres; duplicates ignored; explicit close function & endpoint).
  - Test suite expanded to 42 tests (`pytest tests/ -v` all passing): model roundtrips, malformed rejection, a2a encode/decode, dedup logic.
- Step 4 completed (Experiment Runner & MCP Tools):
  - FastMCP unified server (`surveyops/mcp_servers/`) with 3 tools: `prometheus_query` (named templates), `fetch_logs` (Docker SDK stdout, ANSI stripped, length/byte capped, `<UNTRUSTED_SERVICE_LOGS>` delimited), and `run_load_burst` (concurrency/duration capped).
  - Pydantic models for all tool returns (`PrometheusQueryReturn`, `FetchLogsReturn`, `LoadBurstReturn`).
  - Deterministic ADK Runner agent (`surveyops/agents/runner/`) with before/after agent & tool callbacks logging structured JSON. Zero LLM prompt injection vulnerability when reading logs.
  - A2A integration: Runner served via `create_runner_a2a_app()` on port 8005 using `A2AFastAPIApplication` and `DefaultRequestHandler`.
  - Docker Compose dual-network isolation: `data_net` internal bridge network isolates `toxiproxy` from `surveyops` containers. Verified `surveyops_runner` cannot reach `toxiproxy:8474`.
  - Shared secret authentication and TTL auto-expiry added to `/incidents/close` and `/incidents/open`.
  - Standalone verification script `scripts/test_runner_a2a.py` testing live query_metrics, fetch_logs, load_burst, and 5 negative test cases against live container.
  - Live fault testing verified: injected latency fault -> payment load burst showed 3s latency and 500 errors -> payment logs captured `sqlalchemy.exc.TimeoutError: QueuePool limit...` safely wrapped with untrusted text flag.
  - Test suite expanded to 69 tests (`pytest tests/ -v` all passing; 25 runner/MCP tests, 15 investigate tests, 17 schema/a2a tests, 9 target app tests, 3 smoke tests).
- Step 5 completed (Surface Mapper & Storage):
  - Deterministic AST graph builder (`surveyops/agents/surface_mapper/tools/graph_builder.py`): discovers 31 nodes (endpoints, DB sessions, connection pools, internal functions) and 26 edges, including cross-service HTTP calls (`orders -> payment`, `orders -> auth`, `orders -> inventory`).
  - OpenAPI spec parser (`surveyops/agents/surface_mapper/tools/spec_parser.py`): parses all 4 services, extracts 16 endpoints with parameters and response status codes.
  - CrewAI Surface Mapper agent (`surveyops/agents/surface_mapper/agent.py`): uses `gemini-3.8-flash`, outputs unranked `Proposition` objects matching `PropositionList` via `output_pydantic`.
  - Deterministic guardrail (`surveyops/agents/surface_mapper/guardrails.py`): validates every proposition against OpenAPI endpoints, AST dependency edges, and `FailureMode` enum. Drops any hallucinated endpoints or invented edges.
  - Local disk cache (`propositions_cache.json`): ensures repeat runs and tests do not re-consume LLM quota.
  - SurveyOps internal PostgreSQL (`surveyops_postgres`, image `pgvector/pgvector:pg16`): added to `infra/docker-compose.yml` on isolated `ops_net` (not joined by target apps) with `propositions` table and `vector` extension.
  - Storage module (`surveyops/agents/surface_mapper/storage.py`): provides upsert and loading of propositions from PostgreSQL.
  - Structured callbacks (`surveyops/agents/surface_mapper/callbacks.py`): logs task execution, summary, and latency as structured JSON.
  - Mapper recall evaluation: achieves 100% recall (5/5) against planned faults from `docs/07-EVALUATION.md` (connection pool exhaustion, retry storm without backoff, missing DB index / N+1, race condition in idempotency, pagination off by one).
  - Test suite expanded to 88 tests (`pytest tests/ -v` all passing: 8 tool unit tests, 7 mapper guardrail/storage/callback/recall tests).
- Model configuration and DeepSeek fallback policy configured:
  - Added Decision 19 to `docs/DECISIONS.md`: Using Gemini for now; DeepSeek is fallback with explicit triggers (billing/rate limits or Diagnostician structured output failure).
  - Configured per-agent environment variables (`MAPPER_MODEL`, `DIAGNOSTICIAN_MODEL`, `REMEDIATOR_MODEL`, `BASELINE_MODEL`) across `.env.example` and `.env` with no hardcoded model strings.
  - Test suite passes cleanly (91 tests passing).

- Step 6 completed (Diagnostician LoopAgent):
  - Google ADK `LoopAgent` in `surveyops/agents/diagnostician/`: hypothesis & experiment selector (`LlmAgent`), deterministic `A2ARunnerDispatcherAgent`, evidence ledger evaluator (`LlmAgent`), and deterministic `ExitDecisionAgent`.
  - Pure Python code exit conditions in `exit_logic.py`: enforces confidence threshold ($\ge 0.80$), margin threshold ($\ge 0.20$), and completion of at least one disconfirming experiment. Hard iteration cap `MAX_LOOP_ITERATIONS`.
  - Falsification guardrails (`falsification.py`): rejects requests without `disconfirming_if`, invalid metrics templates, disallowed services/endpoints, or out-of-bound burst parameters prior to network dispatch.
  - A2A interop: sends validated `ExperimentRequest` to Runner on port 8005; dispatches `DiagnosisResult` on `root_cause` to stub Remediator on port 8006 without crashing on transport failure.
  - Security & prompt hardening: log text flagged `contains_untrusted_text` encapsulated in `<UNTRUSTED_SYSTEM_DATA>` XML tags with explicit passive data instructions.
  - Callbacks & Observability: structured JSON logging across before/after agent, model, and tool callbacks; `TokenUsageTracker` for incident cost reporting; adaptive backoff for 429/503 errors; disk cache (`diagnostician_cache.json`) for token conservation.
  - Background trigger: wired `/investigate` endpoint in `surveyops/api/main.py` to trigger Diagnostician asynchronously and auto-close incident in `finally` block.
  - Test suite expanded to 106 tests (`pytest tests/ -v` all passing): 15 unit tests covering exit logic, falsification, malformed LLM outputs, Runner failures, untrusted log delimiters, seeding, and background trigger.
  - Live evaluation: 5 live runs executed against target application under active connection pool exhaustion fault with ground truth comparison performed post-run.

## In progress
- None

## Blocked
- None

## Next 3
1. Real Remediator agent implementation (CrewAI)
2. Remediator regression testing & PR generation to GitHub
3. End-to-end evaluation benchmark across full fault catalog

## Consistency check findings (2026-10-14)
See full report in session artifact. Short summary:
- All 11 requirements are covered in 03-REQUIREMENTS.md.
- One gap: req 5 (callbacks) mentions Postgres logging but 03-REQUIREMENTS.md omits it; 06-SAFETY-OBSERVABILITY.md has it.
- One gap: req 2 (MCP tools) — Surface Mapper tools are marked OPEN (plain vs MCP). Not a contradiction, just undecided.
- One contradiction: req 9 (observability) — 06-SAFETY-OBSERVABILITY.md says OTel instrumentation for CrewAI is "test early"; 03-REQUIREMENTS.md says OpenInference or OpenLIT — neither is in requirements.txt yet. Add when building that agent.
- One flag: `eval/` has a `baseline/` subdir not mentioned in AGENTS.md repo map (07-EVALUATION.md mentions it as a single-LLM baseline). Not a conflict, just an undocumented folder.

## Notes for agents
Update this file at the end of every work session. Keep it short.