# SurveyOps

An autonomous multi-agent system that diagnoses API failures and proposes fixes as pull requests. Agents are separated by authority, and the diagnostic loop tries to disprove its own hypotheses before acting.

Built with Google ADK and CrewAI, connected over A2A, with tools exposed via MCP.

## Status
Early development. See `docs/STATUS.md`.

## Docs
Start with `AGENTS.md`, then `docs/00-OVERVIEW.md`.

## Quick start
TBD. Will be: copy `.env.example` to `.env`, then `docker compose -f infra/docker-compose.yml up -d`.
