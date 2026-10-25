#!/usr/bin/env bash
# run_trial.sh — Single-command full trial execution for SurveyOps.
# Actions:
#   1. Reset target system (Toxiproxy toxics, database records)
#   2. Inject fault on payment-postgres proxy
#   3. Wait for alert & launch diagnosis
#   4. Remediator localizes, synthesizes patch, verifies Rule 5 behavior test, opens PR
#   5. Wait for report (deterministic renderer writes IncidentReport JSON & Markdown)
#   6. Print report path
#   7. Run evaluation scorer separately
#   8. Close GitHub PR and delete branch cleanly
#
# Credentials:
#   Never reads or modifies .env or credential files.
#   Uses only GITHUB_TOKEN already exported in environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -d "${REPO_ROOT}/.venv" ]; then
    source "${REPO_ROOT}/.venv/bin/activate"
fi

exec python3 "${REPO_ROOT}/scripts/run_trial_orchestrator.py" "$@"
