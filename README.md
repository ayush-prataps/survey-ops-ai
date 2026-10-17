# SurveyOps

An autonomous multi-agent system that diagnoses API failures and proposes fixes as pull requests. Agents are separated by authority, and the diagnostic loop tries to disprove its own hypotheses before acting.

Built with Google ADK and CrewAI, connected over A2A, with tools exposed via MCP.

## Status
Early development. See `docs/STATUS.md`.

## Docs
Start with `AGENTS.md`, then `docs/00-OVERVIEW.md`.

## Quick start
TBD. Will be: copy `.env.example` to `.env`, then `docker compose -f infra/docker-compose.yml up -d`.

## Run the target app
The target application is a simulated multi-service backend with four microservices (`orders`, `payment`, `inventory`, `auth`), PostgreSQL, Prometheus, Grafana, and a headless Locust load generator.

Start the stack:
```bash
docker compose -f infra/docker-compose.yml up -d --build
```

View services and telemetry:
- Orders API: http://localhost:8001
- Payment API: http://localhost:8002
- Inventory API: http://localhost:8003
- Auth API: http://localhost:8004
- Prometheus: http://localhost:9090
- Grafana (telemetry dashboard): http://localhost:3000 (admin / admin)

Run tests:
```bash
pytest tests/test_target_app.py -v
```

Stop the stack:
```bash
docker compose -f infra/docker-compose.yml down -v
```
