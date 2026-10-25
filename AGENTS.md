# SurveyOps (survey-ops-ai): Agent Entry Point

This file is the source of truth. Read it first. Then read `docs/DECISIONS.md` and `docs/STATUS.md` before writing any code.

## What this project is
SurveyOps is an autonomous multi-agent system that diagnoses API failures in a multi-service backend and proposes a fix as a GitHub pull request. Its core idea: agents separated by authority, and a diagnostic loop that tries to falsify its own hypotheses before acting. It is a university project (CSE3101 Agentic AI, BML Munjal University). Team: Shruti Keshri, Ayush Pratap Singh, Shreya Saxena.

## Hard constraints (course requirements, non-negotiable)
1. Agent frameworks: ONLY Google ADK and CrewAI. No LangGraph, LangChain agents, LiteLLM, AutoGen or anything else in this repo.
2. All structured output uses Pydantic models.
3. All external tool access goes through MCP servers (at least two real tools).
4. Cross-framework A2A: Diagnostician (ADK) -> Remediator (CrewAI) is the required interop link. Diagnostician -> Runner is also A2A.
5. Every agent has monitoring callbacks and guardrail callbacks.
6. Tracing/observability via Langfuse (OpenTelemetry).
7. Must be deployed to a cloud platform.
8. Must report end-to-end performance, cost and runtime.
9. Code must be original and human-explainable. Every team member must be able to explain and modify any part.

## Invariants that must never break
- Surface Mapper never receives runtime data (logs, metrics, incidents).
- Only the Experiment Runner has credentials to the live target system.
- Runner never interprets causes. It returns raw observations only.
- Diagnostician has no execution authority. It requests experiments via A2A.
- Remediator never merges a PR. A human merges.
- `/investigate` payload carries the symptom only, never a root-cause label.
- Nothing under `surveyops/` may import or read `eval/ground_truth/`. Only `eval/scorer/` reads it, after a run.
- Detection (Prometheus rule + Alertmanager) is deterministic and non-LLM. Agents start only after an alert.
- Incident report is a deterministic template fill from the evidence ledger, not an LLM agent.
- Raw log text is untrusted input. Sanitize and delimit it before any LLM sees it.
- Toxiproxy control API (port 8474) reveals active faults: no agent (including Runner) may access port 8474, and `eval/` is never mounted into SurveyOps containers.
- Credentials and tokens (e.g. GITHUB_TOKEN) must never be printed or written to logs, reports, eval/ output, docs, or commits. All logged output, traces, and reports must redact tokens matching `github_pat_`, `ghp_`, or `gho_`. Remediator receives GITHUB_TOKEN only in its isolated container environment without access to host home or credential files.

## Agents at a glance
| Agent | Framework | Does | Cannot |
|---|---|---|---|
| Surface Mapper | CrewAI | Static analysis of spec + code -> unranked failure propositions | See runtime data |
| Experiment Runner | ADK | Executes experiments on live system, returns raw data | Interpret causes |
| Diagnostician | ADK (LoopAgent) | Hypothesize, challenge, request experiments, conclude or abstain | Execute anything |
| Remediator | CrewAI | Localize file, patch, regression test, open PR | Merge, touch CI/secrets |

## Repo map
- `docs/` ground truth documents (see table below)
- `surveyops/api/` FastAPI control backend (`/investigate`, onboarding)
- `surveyops/agents/` the four agents, one folder each
- `surveyops/mcp_servers/` MCP servers (prometheus, logs, loadtest, github)
- `surveyops/schemas/` Pydantic models (must match `docs/04-CONTRACTS.md`)
- `surveyops/callbacks/` monitoring and guardrail callbacks
- `surveyops/report/` deterministic report renderer
- `services/target_app/` orders, payment, inventory, auth (the system being diagnosed)
- `infra/` docker-compose, Prometheus, Alertmanager, Toxiproxy, load generator
- `eval/` fault definitions, ground truth, scorer, single-LLM baseline
- `tests/`, `scripts/`

## Where to look
| Need | File |
|---|---|
| Goals, scope, non-goals | docs/00-OVERVIEW.md |
| Agent boundaries, data stores | docs/01-ARCHITECTURE.md |
| End-to-end flow and example | docs/02-WORKFLOW.md |
| Professor's 11 requirements mapping | docs/03-REQUIREMENTS.md |
| Schemas, state keys, A2A/MCP shapes | docs/04-CONTRACTS.md |
| Docker, monitoring, cloud | docs/05-INFRA.md |
| Guardrails, callbacks, tracing | docs/06-SAFETY-OBSERVABILITY.md |
| Faults, baseline, metrics | docs/07-EVALUATION.md |
| Decisions and open questions | docs/DECISIONS.md |
| Progress | docs/STATUS.md |
| Viva prep | docs/VIVA.md |

## Working rules
- Before starting: read DECISIONS.md (especially OPEN items) and STATUS.md.
- After any major decision: append to DECISIONS.md. After each session: update STATUS.md.
- If code and docs disagree, stop and flag it. Do not silently pick one.
- Do not invent facts. Example numbers in docs (latencies, timings) are illustrative, not measured.
- When editing, show or make only the changed parts. Do not rewrite whole files without need.
- Python 3.11+. Type hints everywhere. No secrets in code. Use `.env`.
- Prefer the simplest thing that satisfies a requirement. No speculative abstractions.

## Commands (fill in as they become real)
- Run stack: `docker compose -f infra/docker-compose.yml up -d`
- Tests: `pytest tests/`
- Eval: `python -m eval.scorer` (TBD)