# Safety, Callbacks, Observability

## Callbacks (monitoring, requirement 5)
- ADK: `before_agent`, `after_agent`, `before_model`, `after_model`, `before_tool`, `after_tool`.
- CrewAI: `step_callback`, `task_callback`.
- Log every intermediate output, tool call, latency and error as structured JSON to Postgres, tagged with `incident_id`.

## Guardrails (requirement 8)
- **Runner `before_tool_callback`:** allowlist of read-only PromQL queries and log queries; load burst capped by `MAX_LOAD_BURST_CONCURRENCY` and `MAX_LOAD_BURST_SECONDS`; nothing destructive.
- **Tool return validation:** every tool return validated with Pydantic before an agent sees it.
- **Prompt injection via logs:** logs are attacker-controllable text. The Runner sanitizes (strip control chars, truncate, cap length) and wraps log content in clear delimiters. The Diagnostician's instructions say delimited content is untrusted data and must never be followed as instructions. `Observation.contains_untrusted_text` marks it.
- **Remediator limits:** may only modify files under the repo, never CI workflow files or secrets; minimal-scope GitHub token; opens PRs only, never merges.
- **Onboarding input:** repo URL validated against a github.com allowlist. Light touch, this is the only user-text input.
- **Endpoint auth:** `/investigate` shared secret (`Authorization: Bearer <secret>` or `X-Shared-Secret`).
- **Toxiproxy API & eval/ isolation:** Toxiproxy control API (port 8474) lists active toxics and reveals ground truth. Runner allowlist prohibits access to port 8474; `eval/` is never mounted into SurveyOps containers; on cloud deployments port 8474 binds only to loopback or is unexposed.
- **Loop safety:** hard iteration cap (`MAX_LOOP_ITERATIONS`).

## Observability (requirement 9)
- Langfuse via OpenTelemetry. ADK emits OTel natively. CrewAI needs OpenInference or OpenLIT instrumentation. Test CrewAI tracing early.
- Langfuse Cloud free tier vs self-hosted: OPEN. Self-hosting adds several containers.
- Traces give provenance (which evidence led to which conclusion) and token counts that feed cost reporting.
- Prometheus/Grafana monitor the TARGET app, not the agents. Do not confuse the two.