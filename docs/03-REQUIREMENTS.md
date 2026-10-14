# Professor's Minimum Requirements -> Our Plan

| # | Requirement | How we satisfy it | Where | Status |
|---|---|---|---|---|
| 1 | Context sharing and state updates | ADK session state holds incident, propositions, hypotheses, ledger, iteration. CrewAI task context passes Surface Mapper -> propositions and Remediator inputs. Postgres bridges frameworks. | `surveyops/agents/*`, 04-CONTRACTS.md | Not started |
| 2 | 2+ external tools via MCP | Custom MCP servers: Prometheus query, log fetch, load burst (Runner); GitHub PR, repo read (Remediator). | `surveyops/mcp_servers/` | Not started |
| 3 | Structured output via Pydantic | Models for Proposition, ExperimentRequest, Observation, LedgerEntry, DiagnosisResult, RemediationResult, IncidentReport. Final report is JSON + rendered Markdown. CrewAI `output_pydantic`. ADK: verify `output_schema` vs tool-use limitation (OPEN). | `surveyops/schemas/` | Not started |
| 4 | Agentic design patterns | Reflection (challenge step), Tool Use, Planning (experiment selection), Multi-Agent orchestration, Loop with termination, Human-in-the-loop (PR review). | docs, agents | Designed |
| 5 | Monitoring and logging via callbacks | ADK before/after agent, model, tool callbacks. CrewAI step_callback and task_callback. Structured JSON logs to Postgres. | `surveyops/callbacks/` | Not started |
| 6 | A2A, interoperability | Diagnostician (ADK) -> Remediator (CrewAI) over A2A. Diagnostician <-> Runner over A2A. Check CrewAI native A2A support, else a2a-sdk wrapper (OPEN). | agents, DECISIONS.md | Designed |
| 7 | Only CrewAI and ADK | No other agent framework. No LiteLLM, no LangGraph in this repo. | whole repo | Rule set |
| 8 | Guardrails | Runner tool allowlist and load caps; Pydantic validation on tool returns; log sanitization against prompt injection; Remediator file/token limits; repo URL allowlist; `/investigate` shared secret. | 06-SAFETY-OBSERVABILITY.md | Designed |
| 9 | Observability and tracing | Langfuse via OpenTelemetry. ADK emits OTel. CrewAI via OpenInference or OpenLIT (test early). Cloud vs self-host OPEN. | infra, callbacks | Not started |
| 10 | End-to-end performance, cost, runtime | Per trial: time-to-detect, time-to-diagnosis, time-to-PR, iterations, tokens, notional cost at list price. Compare against single-LLM baseline. | 07-EVALUATION.md | Designed |
| 11 | Cloud deployment | Full Docker Compose stack on one cloud VM. Only dashboard public, rest on internal network. Deploy a skeleton early. | 05-INFRA.md | Not started |

## Other course rules
- Every member must be able to explain and modify any component. Roles rotate across phases.
- Public GitHub repo is mandatory.
- AI-generated code is treated as unfair means by the course. Keep authorship and understanding genuine.
- Deliverables per phase: charter, working prototype and progress report, then final app, repo, report, PPT, demo, viva.