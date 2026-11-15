#!/usr/bin/env python3
"""
SurveyOps Auto-Deploy Webhook Server
=====================================
Listens on port 9000 for GitHub push webhook events.
On a push to 'main', validates the HMAC signature, then runs:
    git pull origin main
    docker compose -f infra/docker-compose.yml up -d --build

Usage (runs automatically as a systemd service via setup_webhook.sh):
    python3 webhook_deploy.py

Environment variables:
    WEBHOOK_SECRET   - GitHub webhook secret (required)
    REPO_DIR         - Path to survey-ops-ai repo (default: ~/survey-ops-ai)
    DEPLOY_BRANCH    - Branch to deploy on push (default: main)
    WEBHOOK_PORT     - Port to listen on (default: 9000)
    COMPOSE_FILE     - Relative path to docker-compose.yml (default: infra/docker-compose.yml)
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Configuration ──────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
REPO_DIR = os.path.expanduser(os.environ.get("REPO_DIR", "~/survey-ops-ai"))
DEPLOY_BRANCH = os.environ.get("DEPLOY_BRANCH", "main")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "9000"))
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "infra/docker-compose.yml")
LOG_FILE = os.path.join(REPO_DIR, "deploy.log")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger("webhook")


def verify_signature(payload: bytes, signature_header: str) -> bool:
    """Validates GitHub HMAC-SHA256 webhook signature."""
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET not set - accepting all requests (insecure!)")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def run_deploy():
    """Pulls latest code and rebuilds changed containers in the background."""
    log.info("=" * 60)
    log.info(f"Deploy triggered at {datetime.utcnow().isoformat()}Z")

    commands = [
        ["git", "-C", REPO_DIR, "pull", "origin", DEPLOY_BRANCH],
        ["docker", "compose", "-f", os.path.join(REPO_DIR, COMPOSE_FILE),
         "up", "-d", "--build", "--remove-orphans"],
    ]

    for cmd in commands:
        log.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            log.info(result.stdout.strip())
        if result.stderr:
            log.info(result.stderr.strip())
        if result.returncode != 0:
            log.error(f"Command failed with exit code {result.returncode}")
            return

    log.info("Deploy completed successfully!")
    log.info("=" * 60)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"surveyops-webhook"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """GitHub push event handler."""
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)

        # Validate HMAC signature
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(payload, sig):
            log.warning(f"Invalid webhook signature from {self.client_address[0]}")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid signature"}')
            return

        # Only handle push events to the deploy branch
        event = self.headers.get("X-GitHub-Event", "")
        if event != "push":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ignored","reason":"not a push event"}')
            return

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return

        ref = data.get("ref", "")
        pusher = data.get("pusher", {}).get("name", "unknown")
        commit_msg = data.get("head_commit", {}).get("message", "")[:60]

        if ref != f"refs/heads/{DEPLOY_BRANCH}":
            log.info(f"Ignoring push to non-deploy branch: {ref}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ignored","reason":"not deploy branch"}')
            return

        log.info(f"Push event from {pusher} on {ref}: {commit_msg!r}")

        # Acknowledge webhook immediately, then deploy in background
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b'{"status":"deploying"}')

        thread = threading.Thread(target=run_deploy, daemon=True)
        thread.start()

    def log_message(self, format, *args):
        """Suppress default HTTP access logs (we use our own)."""
        pass


if __name__ == "__main__":
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET is not set! Set it in /etc/surveyops-webhook.env")

    log.info(f"SurveyOps Webhook Deploy Server starting on port {WEBHOOK_PORT}")
    log.info(f"Repo: {REPO_DIR} | Branch: {DEPLOY_BRANCH}")
    log.info(f"Log file: {LOG_FILE}")

    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Webhook server stopped.")

