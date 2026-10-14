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