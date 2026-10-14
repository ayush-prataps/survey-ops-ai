# Status

Last updated: 2026-10-14

## Done
- Synopsis drafted (needs alignment with DECISIONS.md, do not keep rewriting it)
- Architecture, workflow, requirements mapping documented
- Repo structure and ground-truth docs created
- Consistency check: docs/03-REQUIREMENTS.md vs 11 course requirements — see report below
- Minimal env: .venv (Python 3.11.14) with google-adk==2.12.0 + crewai==1.15.28 coexisting; requirements.txt pinned
- Spikes 5 and 6 resolved (see DECISIONS.md SPIKE RESULTS)
- Repo hygiene: folder structure matches AGENTS.md, .gitignore covers .env/.venv/__pycache__, .env.example has no real secrets
- Smoke test: tests/test_smoke.py — 3/3 passed

## In progress
- (Nothing currently blocked or in flight)

## Blocked
- None

## Next 3
1. Target app skeleton (3 to 4 FastAPI services) with `/metrics`, Docker Compose, Toxiproxy with one fault
2. Pydantic schemas in `surveyops/schemas/` matching 04-CONTRACTS.md
3. Verify Remediator A2A integration path (crewai.a2a config/types without the `a2a` SDK) — see DECISIONS.md item 5

## Consistency check findings (2026-10-14)
See full report in session artifact. Short summary:
- All 11 requirements are covered in 03-REQUIREMENTS.md.
- One gap: req 5 (callbacks) mentions Postgres logging but 03-REQUIREMENTS.md omits it; 06-SAFETY-OBSERVABILITY.md has it.
- One gap: req 2 (MCP tools) — Surface Mapper tools are marked OPEN (plain vs MCP). Not a contradiction, just undecided.
- One contradiction: req 9 (observability) — 06-SAFETY-OBSERVABILITY.md says OTel instrumentation for CrewAI is "test early"; 03-REQUIREMENTS.md says OpenInference or OpenLIT — neither is in requirements.txt yet. Add when building that agent.
- One flag: `eval/` has a `baseline/` subdir not mentioned in AGENTS.md repo map (07-EVALUATION.md mentions it as a single-LLM baseline). Not a conflict, just an undocumented folder.

## Notes for agents
Update this file at the end of every work session. Keep it short.