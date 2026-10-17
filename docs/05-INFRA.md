# Infrastructure

## Local stack (Docker Compose, `infra/docker-compose.yml`)
- Target app: orders, payment, inventory, auth (FastAPI), each exposing `/metrics`
- PostgreSQL (target app DB) and Postgres + pgvector (SurveyOps stores). Can be one instance with separate schemas if simpler.
- Toxiproxy between services and between payment and its DB, so faults can be injected without touching app code
- Prometheus (scrapes /metrics, evaluates alert rules)
- Alertmanager (webhook to `/investigate`)
- Grafana (human dashboard only, never agent-facing)
- SurveyOps backend (FastAPI): onboarding, `/investigate`, agent runtime
- Load generator (Locust or k6): constant background traffic
- Optional: Loki + Promtail for logs (OPEN)

## Why Docker
About 8+ processes on one network. Compose gives one-command startup, a private network for Toxiproxy placement, and clean resets between trials for a reproducible 30 to 50 trial benchmark.

## Detection rules (Prometheus)
Latency and error-rate rules are deterministic and basic on purpose. Detection is not the contribution, diagnosis is. Example shape (verify label names against real `/metrics` output):
```yaml
- alert: HighP99Latency
  expr: histogram_quantile(0.99, sum by (le, handler) (rate(http_request_duration_seconds_bucket[1m]))) > 0.5
  for: 1m
- alert: HighErrorRate
  expr: sum by (handler) (rate(http_requests_total{status="5xx"}[1m])) / sum by (handler) (rate(http_requests_total[1m])) > 0.05
  for: 1m
```
Silent faults need invariant-check metrics (see 02-WORKFLOW.md).

## Logs
Metrics (Prometheus) say how much and how often. Logs say what exactly happened. Both are needed. Options: structured JSON to stdout read via Docker SDK, or Loki. Either sits behind the `fetch_logs` MCP tool held only by the Runner. OPEN.

## Cloud deployment (required)
Plan: one cloud VM (GCP or AWS, student credits) running the full Compose stack. Deploy a skeleton early, because networking and Toxiproxy surprises appear on first remote deploy.
- Only the dashboard is publicly exposed.
- Everything else stays on the internal Docker network.
- `/investigate` requires shared-secret authentication (`Authorization: Bearer <secret>` or `X-Shared-Secret`).
- Toxiproxy API (port 8474) lists active toxics, which gives away injected faults: `eval/` must NEVER be mounted into `surveyops-api` or any agent container, Experiment Runner must not be given network route or credentials to port 8474, and on the cloud VM port 8474 must NEVER be exposed publicly.
- Secrets in `.env` on the VM, never in the repo.
Provider and VM size: OPEN.

## Resets between trials
Restart affected containers, clear Toxiproxy toxics, reset DB state, clear incident state. Script in `scripts/`.