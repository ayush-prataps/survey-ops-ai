# Status

Last updated: 2026-10-19

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

- DeepSeek Model Evaluation Spike (`scripts/spikes/deepseek_replacement_spike.py`):
  - Model connector: Added `litellm` to `requirements.in` / `requirements.txt`; Decision 21 in `docs/DECISIONS.md`.
  - Model vars set to `deepseek/deepseek-flash` in `.env.example` and `.env`; no hardcoded model strings in repo.
  - Spike results:
    - ADK function calling alone: 10/10 passed (100%, avg ~1.0s).
    - Prompt injection resilience: DeepSeek 5/5 clean (0 jailbreaks, avg 2.33s), Gemini 5/5 clean (0 jailbreaks, avg 1.39s).
    - OpenTelemetry tracing: 190 spans recorded with model names and token counts.
    - ADK `output_schema`: 0/10 passed (0%, failed with HTTP 400: `This response_format type is unavailable now`).
    - CrewAI `output_pydantic`: 0/10 passed (0%, failed with HTTP 400: `This response_format type is unavailable now`).
  - Progression halted per requirement 4 stop rule (< 9/10 structured output threshold); live Diagnostician evaluation not executed.
- Step 6b completed (Structured-Output Adapter & deepseek/deepseek-flash Switch):
  - Reusable adapter built in `surveyops/llm_utils.py`:
    - ADK tool-calling mode (`build_adk_submission_tool`) creates dynamic submission tools matching Pydantic models; plain text model replies raise `PlainTextReplyError`.
    - CrewAI JSON mode (`execute_with_json_retry`) extracts JSON via regex fence stripping, validates with `model_validate_json`, and performs 1-shot retry on schema validation errors.
  - Ported Diagnostician agents (`hypothesis_agent`, `ledger_evaluator`) to submission tools. Ported Surface Mapper and Remediator stub to JSON mode.
  - Model configurations set to `deepseek/deepseek-flash` across all agents (`MAPPER_MODEL`, `DIAGNOSTICIAN_MODEL`, `REMEDIATOR_MODEL`, `BASELINE_MODEL`). Served model logged as `deepseek-flash` (DeepSeek-V4.1-Flash-1017) in incident run records.
  - LLM record/replay cache keys updated to include model name and mode in both `diagnostician/cache.py` and `surface_mapper/cache/`.
  - Full test suite: 119/119 unit and integration tests passing (`pytest tests/`).
  - Live Diagnostician evaluation under active latency fault (`connection_pool_exhaustion`): 1/1 runs succeeded, correctly identifying root cause at 0.82 confidence in 5 iterations (173.06s wall time, 307,586 tokens, 0 structure failures).
- Step 6c completed (Diagnostician Reasoning Quality, Exit Gate & Heuristic Confidence):
  - Fixed endpoint normalization bug (`POST /payment/charge` -> `/payment/charge`) preventing static proposition `SM-007` from seeding. Seeded propositions retain canonical IDs (`SM-006`, `SM-007`, `SM-008`).
  - Implemented verdict step validation (`validate_ledger_tool_call`): requires verbatim quoting of `disconfirming_if`, `disconfirming_justification` with field/value, bracketed `[request_id:field]` citations for stored observations, and rejection if confidence is lowered without `disconfirming_condition_met=True`. ADK native tool rejection provides 1-shot in-agent recovery.
  - Upgraded Prometheus MCP tools with connection pool metrics (`pool_max_in_use`, `pool_capacity`, `pool_utilization_ratio`) and request metrics.
  - Implemented deterministic exit gate in code (`evaluate_exit_conditions`): requires experiment specifically against leader's `disconfirming_if`, verdict "not met", margin >= `CONFIDENCE_MARGIN` (0.20), evidence matching `predicted_signal` keywords, and `heuristic_score >= CONFIDENCE_THRESHOLD` (0.80).
  - Implemented heuristic confidence score computation in code (`compute_heuristic_confidence_score`) from supporting observation count, disconfirming survival, and margin.
  - Completed 5-run live evaluation under active connection pool exhaustion with fixed `deepseek/deepseek-flash` model: 5/5 runs matched ground truth (100% accuracy, mean 1.8 iterations, mean 62,803 tokens, mean 49.4s wall time).
- Step 6d completed (Diagnostician Validation, Isolation, and Control Baselines):
  - Window contamination & isolation: Identified that `reset_trial.sh` leaves Prometheus TSDB and Docker daemon stdout logs unisolated. Anchored Prometheus queries to incident `window_start` (`range_s`) and implemented line-level RFC3339 timestamp filtering in `docker_logs_tool.py` to discard pre-incident logs. Added unit tests in `tests/test_runner_and_mcp_tools.py`.
  - Token inflation mitigation: Diagnosed that ~100k token overhead was driven by 100 lines of un-capped Docker logs repeated across tool-call validation cycles. Capped logs to 25 signal-focused lines and citations to 100 chars, reducing per-iteration prompt load by ~80%.
  - Terminology refactor: Renamed "calibrated confidence" in code and documentation to "heuristic confidence score".
  - Ground truth enum: Added `FailureMode.upstream_dependency_failure` for Control B.
  - Control A (Healthy, 5 runs): 4/5 abstained (0 root cause); 1 confident-wrong (`missing_index_n_plus_1` on Run 4).
  - Control B (5xx upstream fault with unsaturated pool, 5 runs): 5/5 abstained; connection pool exhaustion correctly ruled out in 5/5 runs. Confident-wrong = 0.
  - Repeat pool fault batch (5 runs): 3/5 diagnosed `connection_pool_exhaustion` (heuristic scores 0.88-0.89), 1 diagnosed `missing_index_n_plus_1`, 1 `budget_exhausted`.
  - Combined 10 Pool Fault Runs: 8/10 diagnosed `connection_pool_exhaustion` (80%), 1 missed cause, 1 budget exhausted.
  - Confident-Wrong across all 10 controls: 1/10 (10%).
- Step 6e completed (Diagnostician token reduction, no diagnostic logic changes):
  - Both Diagnostician LlmAgent steps use ADK `include_contents="none"`; successful submission tools set `skip_summarization`, while validation failures remain visible for in-step retry.
  - ADK 2.12.0 supports both settings and retains current-turn tool events in `none` mode; a live ledger rejection was corrected and resubmitted.
  - Five live pool-fault runs matched ground truth 5/5. Mean token use fell from 62,803 to 24,220 per run (61.4%); the before and after summaries are in `eval/diagnostician_live_run_summary_before.json` and `eval/diagnostician_live_run_summary.json`.
  - Available local tests: 113 passed, 1 skipped, 1 deselected with target-app integration tests excluded because their expected host ports were occupied by unrelated services.
  - Live benchmark required a temporary local MCP SDK 2.3.0 override because pinned LiteLLM 1.106.0 imports an MCP symbol absent from pinned MCP 1.28.1. The project virtual environment was restored to MCP 1.28.1 after the run.
- Step 6f completed (Confident-Wrong Elimination via Deterministic Checkable Signals & Positive Evidence Gate):
  - Autopsy of 2 confident-wrong runs (Control A run 4 and Pool batch 2) showed reliance on keyword matching for `predicted_signal` let `missing_index_n_plus_1` pass on healthy systems and real faults without positive proof.
  - Replaced keyword matching with `CheckableSignal` Pydantic model (`metric_name`, `comparator`, `threshold`, `log_pattern`). Regenerated cache and DB propositions; all 18 propositions now have a checkable signal.
  - Deterministic evaluation (`evaluate_checkable_signal_for_leader`) enforces that the leader must have positive empirical proof from cited observations (`has_positive_evidence=True`). Heuristic score capped at 0.35 without positive evidence, completely preventing ungrounded root-cause declarations.
  - Exit gate terminates with `Outcome.abstained` when no hypothesis has positive evidence upon loop completion or LLM conclude recommendation.
  - Enforced per-incident hard token budget (`MAX_INCIDENT_TOKEN_BUDGET`, default 60k) forcing `Outcome.budget_exhausted` when exceeded. Set Diagnostician temperature to `0.0`.
  - Dynamic prompt builders inject full ledger and observation history each turn when `include_contents="none"` is active. Added unit tests in `tests/test_diagnostician.py` (all 124 tests passing).
  - Validation re-run suite running 25 runs (Control A x10, Pool Fault x10, Control B x5).
- Step 6g completed (Diagnostician Token Reduction, Prefix Caching, and Healthy Early-Abstain):
  - Added compact observation digests (`format_observation_digest`), 10-line signal log caps, and 1-line ledger entries post-iteration 1.
  - Trimmed static instructions and ordered static rules before variable context to enable DeepSeek prompt prefix caching (`cached_tokens` confirmed: 4.8k to 20.9k per run).
  - Telemetry callback upgraded to track `cached_tokens` and internal `reasoning_tokens` (DeepSeek-V4.1-Flash).
  - Added deterministic pre-LLM iteration-1 probes (`check_system_healthy`) via Runner. Healthy systems early-abstain in 0.1s at 0 tokens (Control A mean: 75k tokens -> 0 tokens, 100% reduction, outcomes unchanged).
  - All 127 pytest tests passing (`pytest tests/`).
- Step 6h completed (Diagnostician Regression Isolation & Ablation Study):
  - Disabled thinking/reasoning on `deepseek/deepseek-flash` via `extra_body={"thinking": {"type": "disabled"}}` in Diagnostician only (`reasoning_tokens` eliminated to 0).
  - Replayed historical observations against `check_system_healthy`, identified and fixed 3 false early-abstain bugs on faults.
  - Completed 9-run ablation study across pool faults: identified that pre-probes (c) broke diagnosis by feeding premature sub-threshold telemetry at $T=0$, causing the LLM to disconfirm and rule out the true root cause (`SM-014`/`SM-016`) on iteration 1.
  - All 127 pytest tests passing.
- Step 6i completed (Pool-Fault Regression Bisect Across Commits A, B, and C):
  - Executed 9 pool-fault runs (3 each) across commits A (`827d225`), B (`00e6228`), and C (`token-reduction-v2` reverted baseline) with thinking disabled.
  - Pinpointed regression onset to Commit B (`00e6228`): hard 60k token budget + uncompressed prompt growth cut diagnosis rate from 67% (2/3) to 33% (1/3) via early budget exhaustion.
  - Formulated generic hypothesis reactivation rule (2-observation / window-max rule-out, allow reactivation on empirical signal match).
  - All 127 pytest tests passing.
- Step 6j completed (Ruling-Out Fix, Reactivation, 100k Budget Cap, and Convergence Stop):
  - Implemented window-max evidence & 2-observation requirement for ruling out in `exit_logic.py`.
  - Implemented automatic CheckableSignal reactivation for `ruled_out` hypotheses with observation ID logging.
  - Implemented per-incident 100k token budget cap and 2-iteration convergence stop.
  - Executed 6 pool-fault runs and 3 Control B runs: pool accuracy 6/6 (100.0%), confident-wrong 0/9 (0.0%), mean tokens 18,259.
  - Added unit tests in `tests/test_diagnostician.py`; all 132 tests passing (`pytest tests/`).



- Step 7 completed (Remediator CrewAI & A2A):
  - CrewAI Remediator agent (`surveyops/agents/remediator/`): uses `REMEDIATOR_MODEL` (`deepseek/deepseek-flash`) via `execute_with_json_retry`, validates structured `PatchProposal` and `RemediationResult`, rejects non-root-cause outcomes (`abstained`, `budget_exhausted`).
  - Target repo separation: `TARGET_REPO_URL` (`https://github.com/ayush-prataps/surveyops-target-app.git`) is canonical; Surface Mapper and Remediator read from target workspace volume.
  - Semantic indexer (`indexer.py`): AST chunker and pgvector semantic index (`code_embeddings` in `surveyops_postgres`) confirming `graph_edge_ref` localization.
  - Pydantic tools (`tools.py`): strict path allowlist (service sources and tests only; rejects `.github/`, Dockerfiles, `.env`, traversal), untrusted comment delimiter, AST syntax validation prior to writing, sandboxed subprocess test execution (`run_tests`), GitHub PR opening (`github_open_pr`, never merges).
  - Rule 5 regression test enforcement: new test must fail on unpatched code and pass after patch; tests passing both ways or failing after patch are rejected with 1 retry.
  - Observability & callbacks (`callbacks.py`): `step_callback` and `task_callback` logging structured JSON to `remediator_callback_logs` in `surveyops_postgres`.
  - A2A server (`server.py`): FastAPI app on port 8006 with shared secret authentication middleware and non-blocking `asyncio.to_thread` execution.
  - Unit tests (`tests/test_remediator.py`): 15/15 tests covering allowlist, forbidden files, path traversal, injection in comments, AST rollback, Rule 5, malformed payloads. All 147 test suite tests pass.
  - Live E2E verification: pool fault injected -> Diagnostician converged on `connection_pool_exhaustion` (heuristic=0.88) -> A2A handoff to Remediator -> localized `payment/main.py` -> fail-before confirmed (`AssertionError`) -> patch applied -> pass-after confirmed -> PR opened on target repo -> branch and PR cleanly cleaned up.

- Step 7b completed (Behavioral Testing, Token Redaction, Container Isolation, Reporting & Scorer):
  - Invariants: Added mandatory token redaction rule to `AGENTS.md` for `github_pat_`, `ghp_`, and `gho_` tokens across logs, reports, and traces. Added unit test `test_token_redaction_in_logged_output_and_reports`.
  - Behavioral regression testing: Replaced constant checks with functional behavioral concurrency tests (QueuePool checkout capacity under concurrent burst) with clean cleanup in `finally` blocks.
  - Container isolation: Created `surveyops/agents/remediator/Dockerfile`, configured `remediator` service in `infra/docker-compose.yml` with isolated `remediator_workspace` volume and no host file mounts.
  - Deterministic report step: Implemented `surveyops/report/` producing validated `IncidentReport` (JSON + Markdown) without LLM, saving to `surveyops_postgres` and `eval/reports/`.
  - Evaluation scorer: Implemented `eval/scorer/` comparing reports against blind ground truth in `eval/ground_truth/`. Added unit tests in `tests/test_scorer.py`.
  - Single-command trial runner: Implemented `scripts/run_trial.sh` and `scripts/run_trial_orchestrator.py` automating reset, fault injection, traffic generation, diagnosis, remediation, reporting, scoring, CI check verification, and branch/PR cleanup.
  - Live E2E trial run: Diagnostician converged on `connection_pool_exhaustion` in 2 iterations (heuristic=0.80, 12,103 tokens); Remediator opened PR #2 on target repo; GitHub Actions CI check completed green (`success`); evaluation scorer verified correct (1.0); PR closed and branch deleted cleanly.
  - Test suite expanded to 151 tests (`pytest tests/` all passing).

- Step 7c completed (Remediator MCP Server Conversion):
  - FastMCP Remediator server (`surveyops/mcp_servers/remediator_server.py`): exposes `repo_read`, `apply_patch`, `write_test_file`, `run_tests`, and `github_open_pr` on `surveyops-remediator-tools`.
  - DRY architecture: execution logic lives in `surveyops/agents/remediator/tools.py` with flexible argument signatures supporting both positional, keyword, and MCP callers.
  - MCP ClientSession protocol: tools can be called via `execute_remediator_mcp_tool` over in-memory streams; full MCP dispatch supported in `run_remediation(..., use_mcp=True)` and via `REMEDIATOR_USE_MCP=true`.
  - CrewAI wrappers (`surveyops/agents/remediator/crewai_tools.py`): exposes MCP-backed tools for CrewAI agent tool execution.
  - Requirement 2 (2+ external tools via MCP) fully satisfied with both Runner and Remediator MCP servers complete.
  - Test suite expanded to 153 tests (`pytest tests/` passing 100%).

- Step 8 completed (Demo UI, Events Table, Controller Service, and Double-Blind Reveal):
  - Added `events` table in `surveyops_postgres` (`id`, `incident_id`, `ts`, `component`, `event_type`, `payload` JSONB) with indices and in-memory fallback.
  - Integrated structured event emission across ADK Diagnostician callbacks/agents, CrewAI Remediator callbacks/agents, the `/investigate` API handler, the Runner, and the deterministic report generator while retaining stdout logging.
  - Mandatory token redaction for `github_pat_`, `ghp_`, and `gho_` plus sensitive keys, and strict invariant preventing ground truth from ever entering events.
  - Controller service (`surveyops_controller` on port 8088): isolated on `data_net` and `controller_net` with shared secret auth; exposes `POST /inject` and `POST /reset`. Agents cannot reach controller or Toxiproxy.
  - Demo UI service (`surveyops_demo_ui` on port 8050): single static HTML page with vanilla JS and CSS, large projector typography, live SSE stream (`GET /events/stream`), incident inspection, and controller proxy endpoints.
  - Double-blind evaluation: ground truth is concealed until explicit execution of `POST /demo/reveal`, running `eval.scorer.scorer.score_incident` to reveal the verdict post-run.
  - Replay mode: supports exporting events to `eval/replays/<name>.json` and streaming historical replays at relative timing with prominent `REPLAY` branding.
  - Test suite expanded to 173 tests (`pytest tests/` passing 100%): 13 dedicated demo UI, event redaction, SSE stream, and controller auth tests.

- Step 9 completed (Architecture-First Demo UI Overhaul & Hardened Event Persistence):
  - Postgres enforced as single source of truth for events: eliminated silent in-memory fallback in `events.py` (raises `RuntimeError` loudly on DB connection error), ensuring all containers share the identical persistent event stream.
  - Fixed 5 root causes of previous inject inaction: missing event call in controller `/inject`, runner crash loop caused by eager CrewAI import in `mcp_servers`, surveyops-api Docker packaging missing package files, and background thread termination.
  - Data-driven SVG architecture visualization: driven by `architecture_config.json`, mapping Docker networks (`target_net`, `data_net`, `ops_net`, `controller_net`, `external`), 18 distinct components (blue for software, green for agents), protocol edges, and dashed red forbidden paths with `[X]` markers.
  - Interactive Node Inspector, 1-line Phase Narration, active-incident Diagnostician iteration table, and live mini-strip.
  - Controller `GET /metrics/summary`: strictly named Prometheus & Postgres queries for payment p99, pool in-use/capacity, and alert state; rejects arbitrary PromQL with HTTP 400.
  - Simplified Controls: exactly 3 visible header buttons (`Inject`, `Reset`, `Reveal`) with secondary Replay options menu. Pure flat dark neutral palette, no emojis, no gradients, no glow effects, projector-readable Inter typography, and inline status messages without browser `alert()` popups.
  - Test suite expanded to 178 tests (`pytest tests/` passing 100%).

- Step 10 completed (Alert-to-Agent Gap Closure, Reliability Tuning, and Safety Banner):
  - Alertmanager configuration: set `group_wait: 10s` and `repeat_interval: 5m` in `infra/alertmanager/alertmanager.yml`.
  - Guaranteed `finally` cleanup in `_run_diagnostician_background` on `surveyops_api` releasing incident locks on all exit paths.
  - Added authenticated `POST /trial/reset` on `surveyops_api` invoked synchronously by controller on `/reset`.
  - Served static architecture config at `/static/architecture_config.json` with fallback and unit tests.
  - Verified toxic clearing and strip recovery: latency dropped from 10s to 49.5ms in 28s, alert normalized at 35s.
  - Demo UI displays `"Alert firing, waiting for /investigate"` when firing before incident creation, and surfaces a visible warning banner after 90s timeout.
  - Full live end-to-end cycle verified: alert firing -> `/investigate` received -> Diagnostician loop (3 iters) -> Remediator A2A -> PR #5 opened -> postmortem report written to DB -> PR and branch cleaned up.
  - Pinned `litellm==1.105.1` and `mcp==1.28.1`, installed Remediator test dependencies, added `git clean -fd` to avoid untracked test accumulation.
  - Test suite expanded to 180 tests (`pytest tests/` passing 100%).

## In progress
- None

## Blocked
- None

## Next 3
1. End-to-end evaluation benchmark across full fault catalog (5 faults).
2. Incident report deterministic template fill refinement for silent/ambiguous faults.
3. Final cloud deployment and Viva preparation.

## Consistency check findings (2026-10-14)
See full report in session artifact. Short summary:
- All 11 requirements are covered in 03-REQUIREMENTS.md.
- One gap: req 5 (callbacks) mentions Postgres logging but 03-REQUIREMENTS.md omits it; 06-SAFETY-OBSERVABILITY.md has it.
- One gap: req 2 (MCP tools) — Surface Mapper tools are marked OPEN (plain vs MCP). Not a contradiction, just undecided.
- One contradiction: req 9 (observability) — 06-SAFETY-OBSERVABILITY.md says OTel instrumentation for CrewAI is "test early"; 03-REQUIREMENTS.md says OpenInference or OpenLIT — neither is in requirements.txt yet. Add when building that agent.
- One flag: `eval/` has a `baseline/` subdir not mentioned in AGENTS.md repo map (07-EVALUATION.md mentions it as a single-LLM baseline). Not a conflict, just an undocumented folder.

## Notes for agents
Update this file at the end of every work session. Keep it short.
