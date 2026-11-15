#!/usr/bin/env bash
# ==============================================================================
# SurveyOps - Automated GitHub Webhook Deployment Setup
# ==============================================================================
# Installs and configures the webhook deployment server as a systemd service on
# the GCP Compute Engine VM.
#
# When your partner pushes to GitHub:
# 1. GitHub sends a POST event to http://<VM_IP>:9000
# 2. Webhook server validates the secret signature
# 3. Runs: git pull origin main && docker compose up -d --build
# ==============================================================================

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ] && ! command -v sudo &> /dev/null; then
    log_error "This script must be run as root or with sudo."
    exit 1
fi
SUDO=""
[ "$EUID" -ne 0 ] && SUDO="sudo"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
WEBHOOK_PORT="9000"
CONFIG_FILE="/etc/surveyops-webhook.env"
SERVICE_FILE="/etc/systemd/system/surveyops-webhook.service"

log_info "Setting up auto-deploy webhook in: $REPO_DIR"

# 1. Generate or reuse webhook secret
if [ ! -f "$CONFIG_FILE" ]; then
    GENERATED_SECRET="$(openssl rand -hex 20)"
    $SUDO bash -c "cat <<EOF > $CONFIG_FILE
WEBHOOK_SECRET=${GENERATED_SECRET}
REPO_DIR=${REPO_DIR}
DEPLOY_BRANCH=main
WEBHOOK_PORT=${WEBHOOK_PORT}
COMPOSE_FILE=infra/docker-compose.yml
EOF"
    $SUDO chmod 600 "$CONFIG_FILE"
    log_info "Generated new secret in $CONFIG_FILE"
else
    log_info "Reusing existing configuration in $CONFIG_FILE"
fi

# Load the secret to display to user
SECRET_VAL="$($SUDO grep WEBHOOK_SECRET "$CONFIG_FILE" | cut -d'=' -f2)"

# 2. Configure UFW firewall for port 9000
if command -v ufw &> /dev/null; then
    $SUDO ufw allow ${WEBHOOK_PORT}/tcp comment 'SurveyOps GitHub Webhook' >/dev/null || true
    log_info "Allowed port ${WEBHOOK_PORT} in UFW firewall."
fi

# 3. Create systemd service
log_info "Creating systemd service: $SERVICE_FILE"
$SUDO bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=SurveyOps Auto-Deploy GitHub Webhook Server
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${CONFIG_FILE}
ExecStart=/usr/bin/python3 ${REPO_DIR}/scripts/webhook_deploy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF"

# 4. Reload and start systemd service
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now surveyops-webhook
log_success "surveyops-webhook service is enabled and running!"

# 5. Get VM external IP
EXTERNAL_IP="$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || curl -s ifconfig.me || echo "<VM_EXTERNAL_IP>")"

echo ""
echo "=================================================================="
log_success "Auto-Deploy Webhook Setup Complete!"
echo "=================================================================="
echo ""
echo "  Payload URL:  http://${EXTERNAL_IP}:${WEBHOOK_PORT}"
echo "  Content type: application/json"
echo "  Secret:       ${SECRET_VAL}"
echo "  Events:       Just the push event"
echo ""
echo "  To add this webhook to your GitHub repository:"
echo "    1. Go to your repo on GitHub -> Settings -> Webhooks -> Add webhook"
echo "    2. Set Payload URL:  http://${EXTERNAL_IP}:${WEBHOOK_PORT}"
echo "    3. Set Content type: application/json"
echo "    4. Paste the Secret: ${SECRET_VAL}"
echo "    5. Click 'Add webhook'"
echo ""
echo "  Ensure GCP VPC firewall allows port ${WEBHOOK_PORT}:"
echo "    gcloud compute firewall-rules create allow-surveyops-webhook \\"
echo "        --direction=INGRESS --action=ALLOW --rules=tcp:${WEBHOOK_PORT} \\"
echo "        --target-tags=surveyops-host"
echo "=================================================================="
