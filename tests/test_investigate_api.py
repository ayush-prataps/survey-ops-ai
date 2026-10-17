"""
Tests for surveyops/api/main.py:
- Payload parsing from Alertmanager webhook bodies
- X-Shared-Secret enforcement
- Edge cases (missing fields, resolved alerts)

These are unit tests using TestClient — no Docker required.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

# Add surveyops/api to path so we can import main directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "surveyops", "api"))

import main as api_main
from fastapi.testclient import TestClient


# Patch the shared secret before creating the client
api_main.SHARED_SECRET = "test-secret"
client = TestClient(api_main.app)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _alertmanager_body(
    alertname: str = "HighP99Latency",
    handler: str = "/payment/charge",
    status: str = "firing",
) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "receiver": "surveyops-webhook",
        "groupLabels": {"alertname": alertname, "handler": handler},
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": alertname,
                    "handler": handler,
                    "severity": "warning",
                },
                "annotations": {
                    "summary": f"High latency on {handler}",
                    "description": f"p99 latency is 1.2s on handler {handler}",
                },
                "startsAt": "2026-10-17T10:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
            }
        ],
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestInvestigateAuth:
    def test_correct_secret_accepted(self):
        resp = client.post(
            "/investigate",
            json=_alertmanager_body(),
            headers={"X-Shared-Secret": "test-secret"},
        )
        assert resp.status_code == 200

    def test_bearer_authorization_accepted(self):
        resp = client.post(
            "/investigate",
            json=_alertmanager_body(),
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 200

    def test_wrong_secret_rejected(self):
        resp = client.post(
            "/investigate",
            json=_alertmanager_body(),
            headers={"X-Shared-Secret": "wrong-secret"},
        )
        assert resp.status_code == 403

    def test_missing_secret_rejected(self):
        resp = client.post("/investigate", json=_alertmanager_body())
        assert resp.status_code == 403


class TestPayloadParsing:
    HEADERS = {"X-Shared-Secret": "test-secret"}

    def test_high_p99_latency_alert_parsed(self):
        resp = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        payload = data["payloads"][0]
        assert payload["alert"] == "HighP99Latency"
        assert payload["endpoint"] == "/payment/charge"
        assert payload["service"] == "payment"
        assert payload["metric"] == "http_request_duration_seconds"
        assert payload["threshold"] == 0.5
        # observed is -1.0 (sentinel) — Alertmanager doesn't carry the numeric value
        assert payload["observed"] == -1.0

    def test_high_error_rate_alert_parsed(self):
        resp = client.post(
            "/investigate",
            json=_alertmanager_body("HighErrorRate", "/orders"),
            headers=self.HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        payload = data["payloads"][0]
        assert payload["alert"] == "HighErrorRate"
        assert payload["metric"] == "http_requests_total"
        assert payload["threshold"] == 0.05
        assert payload["service"] == "orders"

    def test_resolved_alert_skipped(self):
        """Resolved alerts must NOT produce InvestigatePayload entries."""
        resp = client.post(
            "/investigate",
            json=_alertmanager_body(status="resolved"),
            headers=self.HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 0

    def test_multiple_alerts_all_parsed(self):
        body = {
            "version": "4",
            "status": "firing",
            "receiver": "surveyops-webhook",
            "groupLabels": {},
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighP99Latency", "handler": "/payment/charge"},
                    "annotations": {},
                    "startsAt": "2026-10-17T10:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "",
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "HighErrorRate", "handler": "/orders"},
                    "annotations": {},
                    "startsAt": "2026-10-17T10:01:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "",
                },
            ],
        }
        resp = client.post("/investigate", json=body, headers=self.HEADERS)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 2

    def test_alert_without_handler_label_skipped(self):
        body = {
            "version": "4",
            "status": "firing",
            "receiver": "surveyops-webhook",
            "groupLabels": {},
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighP99Latency"},  # no handler
                    "annotations": {},
                    "startsAt": "2026-10-17T10:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "",
                }
            ],
        }
        resp = client.post("/investigate", json=body, headers=self.HEADERS)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 0

    def test_health_endpoint(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
