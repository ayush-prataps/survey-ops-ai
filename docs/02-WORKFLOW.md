# Workflow

All numbers below are ILLUSTRATIVE, not measured. Replace with real values from test runs.

## Setup (once per project)
1. Developer submits repo URL and OpenAPI spec on the dashboard (URL validated against github.com allowlist).
2. Backend runs `git clone --depth 1` into a Docker volume. Deterministic, not an agent.
3. Surface Mapper produces propositions, stored in Postgres.
4. Remediator's indexer chunks repo and stores embeddings in pgvector.

## Simulated production
Target app (orders, payment, inventory, auth + Postgres) runs continuously in Docker Compose behind Toxiproxy. A background traffic generator (Locust or k6) sends constant requests, because no traffic means no metrics and nothing to detect.

## Incident flow
1. **Fault injected.** Eval script calls Toxiproxy (e.g. latency on payment -> Postgres). Same script writes true cause to the ground-truth store. Agents never see it.
2. **Symptoms.** Payment requests slow down, metrics change.
3. **Detection (no LLM).** Prometheus scrapes /metrics every ~15s. Alert rule fires (e.g. p99 latency on POST /payment/charge > 0.5s for 1m). Expect ~1 to 2 minutes from injection to trigger.
4. **Trigger.** Alertmanager POSTs a webhook to `/investigate` on the SurveyOps backend (not the target app). Payload is symptom only. Requires shared-secret authentication (`Authorization: Bearer <secret>` or `X-Shared-Secret`).
5. **Diagnostician starts.** Loads propositions for the affected endpoint into session state. Picks leading hypothesis.
6. **Loop.** Observe -> Hypothesize -> Challenge ("what evidence would disprove this?") -> A2A experiment request to Runner -> Runner queries Prometheus/logs/drives small load burst, returns raw data -> update ledger and confidence -> repeat.
7. **Exit conditions (three):** (a) one hypothesis dominates, (b) iteration budget exhausted, (c) legitimate abstention "insufficient evidence". (b) and (c) produce no PR.
8. **Remediation.** Diagnostician sends validated root cause + `graph_edge_ref` to Remediator over A2A. Remediator localizes file, patches, writes regression test, opens PR. GitHub Actions runs tests. Human reviews and merges.
9. **Report.** Deterministic template fills root cause, evidence trail, confidence, ruled-out hypotheses, PR link.
10. **Scoring.** Scorer compares diagnosis to ground truth after the run.

## Worked example (illustrative)
Fault: connection pool exhaustion in payment. p99 on POST /payment/charge goes from ~120ms to ~2.4s. Leading hypothesis `connection_pool_exhaustion`, competitor `retry_storm`. Iteration 1: ask Runner for connection wait time, active pool connections, small concurrent burst. Raw result shows wait time spike, pool at max, errors scaling with concurrency. Iteration 2: ask for logs showing duplicate or retried calls, none found, retry storm ruled out. Loop exits (a). Remediator adds explicit pool size limit and timeout with backoff, plus concurrent-load regression test.

## Silent faults
Race condition in idempotency, off-by-one pagination: these return 200 at normal speed, so latency and error-rate rules never fire. Handling: invariant checks that expose counters (e.g. `duplicate_charges_total`) or a synthetic probe validating response shape, with alerts on those. Alternative: scope them out of the alert-triggered path and say so openly. Decide in DECISIONS.md.

## Why an error line is not enough
An error like `PaymentServiceTimeout` is a symptom. It cannot distinguish pool exhaustion, a missing index or injected latency. The Diagnostician runs experiments that come out differently under each cause.w