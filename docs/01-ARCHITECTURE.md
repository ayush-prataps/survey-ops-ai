# Architecture

## Agents
| Agent | Framework | Role | Can access | Cannot do |
|---|---|---|---|---|
| Surface Mapper | CrewAI | Statically analyzes OpenAPI spec and code, produces falsifiable failure propositions per endpoint, before any fault | Static artifacts only (spec, code, dependency graph) | See runtime data or telemetry |
| Experiment Runner | ADK | Executes targeted experiments on the live system, returns raw observations | Only agent with live-system credentials (via MCP: Prometheus, logs, load burst) | Interpret why something failed |
| Diagnostician | ADK (LoopAgent) | Forms hypotheses, tries to falsify them, requests experiments, concludes or abstains | Incident symptom, stored propositions, Runner's raw output | Execute anything, touch live system |
| Remediator | CrewAI | Takes validated root cause, localizes file, patches, writes regression test, opens PR | Repo read, PR write (never merge) | Merge, edit CI workflows or secrets |

## Why separate by authority
If one agent both ran tests and interpreted results, it could shape the experiment to confirm what it already believes. Splitting the agent that acts from the agent that reasons makes falsification trustworthy. Surface Mapper isolation prevents it from rationalizing backward from a known failure.

## Why two frameworks (honest version)
ADK for the Runner and Diagnostician: needs a real cyclic loop with hard iteration cap (LoopAgent), session state carrying the evidence ledger across iterations, and A2A. CrewAI for Surface Mapper and Remediator: single-pass tool-using tasks where its task/crew abstraction fits. ADK could technically do all four. The split reflects task shape, and the course requires both frameworks. Do not claim CrewAI has no state or memory.

## Surface Mapper internals
1. Tool 1 (plain code, no LLM): builds dependency graph via Python `ast`. Internal call graph plus cross-service HTTP calls (httpx/requests calls and base URLs in config).
2. Tool 2 (plain code, no LLM): parses OpenAPI spec into endpoints, inputs, outputs, status codes.
3. LLM reasons over both, per endpoint: walks the endpoint's graph chain and proposes failure modes for what it touches (DB pool -> pool exhaustion; cross-service call -> timeout, retry storm; shared resource written by two paths -> race condition).
4. Output: structured list of `Proposition` objects (see 04-CONTRACTS.md). UNRANKED, no confidence scores, because it has no runtime evidence.
Limitation: static analysis misses dynamic or reflective calls and runtime config wiring. Best effort by design.

## Data stores
- Postgres table `propositions`: Surface Mapper output (OPEN: confirm, see DECISIONS.md).
- Postgres tables for incidents, ledger, callback logs.
- pgvector: semantic code index built by the Remediator's indexer, plus past incidents.
- Cloned repo on a workspace volume or local workspace (`TARGET_REPO_URL` pointing to `surveyops-target-app`). This standalone repo is the single authoritative source of truth for the target application code, test suite, and CI workflows; `services/target_app/` in this repository is legacy and non-authoritative.
- Target repository branch governance: `main` is protected by a GitHub ruleset (pull request required, no bypass, CI check required). The Remediator has rights only to push feature branches and open PRs (`pull-requests: write`, `contents: write`), never merge.
- Ground-truth store: separate from everything agents can reach. Written by the injection script, read only by the scorer.

## Dependency graph vs semantic index
Graph gives structure (who calls what), deterministic, used to predict failure modes. Semantic index finds code by meaning, used to localize the file to patch. Each proposition carries `graph_edge_ref`, which the Diagnostician passes to the Remediator as a starting point. Semantic search confirms or acts as fallback.

## Interop
- Diagnostician (ADK) <-> Runner (ADK): A2A.
- Diagnostician (ADK) -> Remediator (CrewAI): A2A. This is the cross-framework link the course requires.
- Postgres bridges frameworks for stored data. ADK session state and CrewAI task context are each framework's working memory.