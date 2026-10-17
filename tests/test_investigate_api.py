"""
Tests for surveyops/api/main.py:
- Payload parsing from Alertmanager webhook bodies
- Shared-secret authentication (X-Shared-Secret and Authorization: Bearer)
- Incident deduplication by (service, endpoint)
- Incident close function and endpoint
"""

from datetime import datetime, timezone
import os
import sys

import pytest
from fastapi.testclient import TestClient

# Add surveyops/api and repo root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "surveyops", "api"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as api_main

# Patch shared secret for testing
api_main.SHARED_SECRET = "test-secret"
client = TestClient(api_main.app)


@pytest.fixture(autouse=True)
def clean_dedup_store():
    """Ensure incident store is clean before and after each test."""
    api_main.dedup_store.clear()
    yield
    api_main.dedup_store.clear()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _alertmanager_body(
    alertname: str = "HighP99Latency",
    handler: str = "/payment/charge",
    status: str = "firing",
) -> dict:
    return {
        "version": "4",
        "status": status,
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
        # Reconciled: observed is None (omitted by Alertmanager, filled by Diagnostician)
        assert payload["observed"] is None
        assert "incident_id" in payload

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


class TestIncidentDeduplication:
    HEADERS = {"X-Shared-Secret": "test-secret"}

    def test_duplicate_alert_ignored_for_open_incident(self):
        # 1. First alert creates incident
        resp1 = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["accepted"] == 1
        assert len(data1["ignored_duplicates"]) == 0
        incident_id = data1["payloads"][0]["incident_id"]

        # 2. Second alert for same endpoint ignored as duplicate
        resp2 = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["accepted"] == 0
        assert len(data2["ignored_duplicates"]) == 1
        assert data2["ignored_duplicates"][0]["incident_id"] == incident_id
        assert data2["ignored_duplicates"][0]["service"] == "payment"
        assert data2["ignored_duplicates"][0]["endpoint"] == "/payment/charge"

    def test_distinct_endpoints_create_distinct_incidents(self):
        resp1 = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        resp2 = client.post(
            "/investigate",
            json=_alertmanager_body("HighErrorRate", "/orders"),
            headers=self.HEADERS,
        )
        assert resp1.json()["accepted"] == 1
        assert resp2.json()["accepted"] == 1
        inc1 = resp1.json()["payloads"][0]["incident_id"]
        inc2 = resp2.json()["payloads"][0]["incident_id"]
        assert inc1 != inc2

    def test_close_incident_by_function_allows_new_incident(self):
        # Open incident
        resp = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        inc1 = resp.json()["payloads"][0]["incident_id"]

        # Close incident via explicit function
        closed_id = api_main.close_incident("payment", "/payment/charge")
        assert closed_id == inc1

        # Re-investigating opens a new incident
        resp_after = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        assert resp_after.json()["accepted"] == 1
        inc2 = resp_after.json()["payloads"][0]["incident_id"]
        assert inc2 != inc1

    def test_close_incident_via_api_endpoint(self):
        # Open incident
        resp = client.post(
            "/investigate",
            json=_alertmanager_body("HighP99Latency", "/payment/charge"),
            headers=self.HEADERS,
        )
        inc_id = resp.json()["payloads"][0]["incident_id"]

        # Verify listed in /incidents/open (with auth)
        open_list = client.get("/incidents/open", headers=self.HEADERS).json()["open_incidents"]
        assert len(open_list) == 1
        assert open_list[0]["incident_id"] == inc_id

        # Close via POST /incidents/close (with auth)
        close_resp = client.post("/incidents/close", json={"incident_id": inc_id}, headers=self.HEADERS)
        assert close_resp.status_code == 200
        assert close_resp.json()["status"] == "closed"

        # Verify no longer open
        open_list_after = client.get("/incidents/open", headers=self.HEADERS).json()["open_incidents"]
        assert len(open_list_after) == 0

    def test_incidents_endpoints_require_auth(self):
        # Unauthenticated calls must be rejected
        get_resp = client.get("/incidents/open")
        assert get_resp.status_code == 403

        post_resp = client.post("/incidents/close", json={"service": "payment", "endpoint": "/payment/charge"})
        assert post_resp.status_code == 403

    def test_expired_incident_auto_closes_and_allows_reopen(self):
        import time

        # Set temporary very short TTL (10ms)
        original_ttl = api_main.dedup_store.expiry_seconds
        try:
            api_main.dedup_store.expiry_seconds = 0.05

            resp1 = client.post(
                "/investigate",
                json=_alertmanager_body("HighP99Latency", "/payment/charge"),
                headers=self.HEADERS,
            )
            inc1 = resp1.json()["payloads"][0]["incident_id"]

            # Wait for incident to expire
            time.sleep(0.1)

            # Subsequent alert should not be blocked; new incident opened
            resp2 = client.post(
                "/investigate",
                json=_alertmanager_body("HighP99Latency", "/payment/charge"),
                headers=self.HEADERS,
            )
            assert resp2.json()["accepted"] == 1
            inc2 = resp2.json()["payloads"][0]["incident_id"]
            assert inc2 != inc1
        finally:
            api_main.dedup_store.expiry_seconds = original_ttl
