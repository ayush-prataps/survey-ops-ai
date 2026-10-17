#!/usr/bin/env bash
# reset_trial.sh — Reset the system to a clean state between evaluation trials.
#
# Actions:
#   1. Clear all Toxiproxy toxics on payment-postgres proxy
#   2. Truncate payment and orders rows in Postgres (leaves schema and inventory intact)
#   3. Print confirmation
#
# Usage: bash scripts/reset_trial.sh
# Requires: stack running (docker compose up -d)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TOXI_API="${TOXIPROXY_API:-http://localhost:8474}"
PROXY_NAME="payment-postgres"
PG_CONTAINER="target_postgres"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-target_app}"

echo "=== SurveyOps trial reset ==="

# ── 1. Clear Toxiproxy toxics ────────────────────────────────────────────────
echo "[1/2] Clearing Toxiproxy toxics on '${PROXY_NAME}'..."
python3 "${REPO_ROOT}/eval/faults/clear_faults.py"

# ── 2. Truncate DB rows ───────────────────────────────────────────────────────
echo "[2/2] Truncating payments and orders tables in '${PG_DB}'..."
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" \
  -c "TRUNCATE TABLE payments, orders RESTART IDENTITY CASCADE;" \
  -c "SELECT 'payments rows: ' || COUNT(*) FROM payments;" \
  -c "SELECT 'orders rows:   ' || COUNT(*) FROM orders;"

echo ""
echo "=== Reset complete. System is clean for next trial. ==="
