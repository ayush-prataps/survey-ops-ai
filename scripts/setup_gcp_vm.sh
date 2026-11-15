#!/usr/bin/env bash
# ==============================================================================
# SurveyOps - GCP Compute Engine VM Bootstrap & Setup Script
# ==============================================================================
# Sets up a clean Ubuntu 22.04 / 24.04 LTS VM on Google Cloud Platform (GCP)
# to host the SurveyOps multi-agent diagnostic & remediation stack.
#
# HARD INVARIANTS ENFORCED:
#   1. Only port 8050 (Demo UI) and port 22 (SSH) are accessible externally.
#   2. Toxiproxy port 8474 is bound strictly to loopback (127.0.0.1) and internal
#      Docker network. NEVER exposed publicly.
#   3. Secrets are stored strictly in .env on the VM, never committed to git.
#   4. Tokens matching github_pat_, ghp_, or gho_ are never printed.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

echo "=================================================================="
echo "         SurveyOps Autonomous Multi-Agent System on GCP           "
echo "=================================================================="

# 1. Verify root or sudo access
if [ "$EUID" -ne 0 ] && ! command -v sudo &> /dev/null; then
    log_error "This script must be run as root or with sudo available."
    exit 1
fi

SUDO=""
if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
fi

# 2. Update and install base packages
log_info "Updating system packages..."
$SUDO apt-get update -y
$SUDO apt-get install -y ca-certificates curl gnupg lsb-release git jq ufw

# 3. Install Docker Engine & Docker Compose plugin if not present
if ! command -v docker &> /dev/null; then
    log_info "Installing Docker Engine and Docker Compose plugin..."
    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

    UBUNTU_CODENAME="$(lsb_release -cs)"
    echo \
      "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      \"${UBUNTU_CODENAME}\" stable" | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

    $SUDO apt-get update -y
    $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    log_success "Docker Engine installed successfully."
else
    log_info "Docker Engine is already installed: $(docker --version)"
fi

# Add current user to docker group
if [ -n "${SUDO_USER:-}" ]; then
    $SUDO usermod -aG docker "$SUDO_USER" || true
    log_info "Added $SUDO_USER to the docker group."
fi

# Ensure docker service is running
$SUDO systemctl enable --now docker

# 4. Configure local host firewall (UFW defense-in-depth)
log_info "Configuring host firewall rules (UFW defense-in-depth)..."
$SUDO ufw default deny incoming
$SUDO ufw default allow outgoing
$SUDO ufw allow 22/tcp comment 'SSH'
$SUDO ufw allow 8050/tcp comment 'SurveyOps Demo UI'
# Note: Toxiproxy (8474), DB (5432), and internal agent ports are strictly blocked
$SUDO ufw --force enable
log_success "UFW firewall enabled (only ports 22 and 8050 allowed)."

# 5. Check repo directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

log_info "Working inside repository: $REPO_ROOT"

# 6. Configure .env file
if [ ! -f .env ]; then
    log_info "Creating .env from .env.example..."
    cp .env.example .env

    # Generate random secret for internal auth
    RAND_SECRET="$(openssl rand -hex 16)"
    sed -i "s/INVESTIGATE_SHARED_SECRET=change-me-before-deploy/INVESTIGATE_SHARED_SECRET=${RAND_SECRET}/" .env

    log_warn "A starter .env file was created."
    log_warn "Please ensure DEEPSEEK_API_KEY (or GEMINI_API_KEY) and GITHUB_TOKEN are set in .env before running tests!"
else
    log_info ".env file already exists."
fi

# Ensure infra/.env exists so docker compose inherits the same environment
cp .env infra/.env

# 7. Start the Docker Compose stack
log_info "Building and launching SurveyOps Docker Compose stack..."
$SUDO docker compose -f infra/docker-compose.yml up -d --build

# 8. Wait for core services to report healthy
log_info "Waiting for service health checks to pass..."
CONTAINERS=(
    "target_postgres"
    "target_auth"
    "target_inventory"
    "target_payment"
    "target_orders"
    "surveyops_postgres"
    "surveyops_api"
    "surveyops_demo_ui"
)

for CNAME in "${CONTAINERS[@]}"; do
    printf "Checking %-22s ... " "$CNAME"
    ATTEMPTS=0
    MAX_ATTEMPTS=24 # 24 x 5s = 120s max
    until [ "$($SUDO docker inspect --format='{{json .State.Health.Status}}' "$CNAME" 2>/dev/null)" = '"healthy"' ] || [ $ATTEMPTS -ge $MAX_ATTEMPTS ]; do
        sleep 5
        ATTEMPTS=$((ATTEMPTS+1))
    done

    if [ $ATTEMPTS -lt $MAX_ATTEMPTS ]; then
        echo -e "${GREEN}HEALTHY${NC}"
    else
        echo -e "${YELLOW}STARTED${NC}"
    fi
done

# 9. Get Public IP address if on GCP
EXTERNAL_IP="$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || curl -s ifconfig.me || echo "<VM_EXTERNAL_IP>")"

echo ""
echo "=================================================================="
log_success "SurveyOps stack is running on GCP Compute Engine!"
echo "=================================================================="
echo "  Demo UI Dashboard:  http://${EXTERNAL_IP}:8050"
echo "  Grafana (SSH tunnel): ssh -L 3000:localhost:3000 user@${EXTERNAL_IP}"
echo ""
echo "  GCP Firewall Reminder:"
echo "    Ensure GCP VPC firewall allows TCP port 8050 from your IP address:"
echo "    gcloud compute firewall-rules create allow-surveyops-demo \\"
echo "      --allow tcp:8050 --target-tags=surveyops-stack"
echo "=================================================================="

