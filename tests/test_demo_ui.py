"""
Unit and Integration Tests for SurveyOps Demo UI, Events, Controller & Scorer Reveal.
Tests cover:
1. Event redaction (github_pat_, ghp_, gho_, sensitive keys, and forbidden ground-truth fields).
2. SSE event stream formatting and delivery.
3. Ground truth masking (never exposed before reveal, exposed only via /demo/reveal).
4. Controller authentication (403 on missing/invalid secret, 200 on valid secret).
5. Replay mode saving and streaming with is_replay tag.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from surveyops.events import (
    record_event,
    fetch_events_since,
    redact_payload,
    get_all_events_for_incident,
    export_replay_file,
)
from surveyops.controller.app import app as controller_app
from surveyops.demo_ui.app import app as demo_ui_app


class TestEventRedaction:
    """Verifies that secrets are redacted and ground truth is never recorded in events."""

    def test_github_token_patterns_redacted(self):
        raw_payload = {
            "token_pat": "github_pat_11ABX890123456789_abcdefghijklmnopqrstuvwxyz",
            "token_ghp": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
            "token_gho": "gho_OAuthSecretString9876543210",
            "safe_text": "Normal observation message",
        }
        clean = redact_payload(raw_payload)
        assert "[REDACTED_TOKEN]" in clean["token_pat"]
        assert "github_pat_" not in clean["token_pat"]
        assert "[REDACTED_TOKEN]" in clean["token_ghp"]
        assert "ghp_" not in clean["token_ghp"]
        assert "[REDACTED_TOKEN]" in clean["token_gho"]
        assert "gho_" not in clean["token_gho"]
        assert clean["safe_text"] == "Normal observation message"

    def test_sensitive_keys_masked(self):
        raw_payload = {
            "password": "super_secret_db_password",
            "api_key": "sk-deepseek-123456",
            "shared_secret": "dev-secret-token",
            "auth_header": "Bearer abcdef123",
            "checkable_metric": "pool_in_use",  # checkable_ prefix preserved
        }
        clean = redact_payload(raw_payload)
        assert clean["password"] == "[REDACTED_SECRET]"
        assert clean["api_key"] == "[REDACTED_SECRET]"
        assert clean["shared_secret"] == "[REDACTED_SECRET]"
        assert clean["auth_header"] == "[REDACTED_SECRET]"
        assert clean["checkable_metric"] == "pool_in_use"

    def test_forbidden_ground_truth_keys_stripped(self):
        raw_payload = {
            "incident_id": "inc-test-123",
            "ground_truth": "connection_pool_exhaustion",
            "ground_truth_failure_mode": "connection_pool_exhaustion",
            "fault_label": "fault-af11c960",
            "symptom": "p99 latency > 1.0s",
        }
        clean = redact_payload(raw_payload)
        assert "ground_truth" not in clean
        assert "ground_truth_failure_mode" not in clean
        assert "fault_label" not in clean
        assert clean["symptom"] == "p99 latency > 1.0s"

    def test_record_event_end_to_end_sanitization(self):
        ev = record_event(
            incident_id="inc-audit-999",
            component="test_component",
            event_type="test_event",
            payload={
                "github_token": "ghp_superSecretToken12345",
                "ground_truth": "leak_attempt",
                "status": "ok",
            },
        )
        assert ev["incident_id"] == "inc-audit-999"
        assert ev["component"] == "test_component"
        assert "ground_truth" not in ev["payload"]
        assert "ghp_" not in str(ev["payload"])


class TestControllerAuth:
    """Verifies that the controller requires the shared secret and isolates Toxiproxy."""

    def test_inject_rejects_missing_secret(self):
        client = TestClient(controller_app)
        resp = client.post("/inject", json={"latency_ms": 1000})
        assert resp.status_code == 403
        assert "Forbidden" in resp.json()["detail"]

    def test_inject_rejects_wrong_secret(self):
        client = TestClient(controller_app)
        resp = client.post(
            "/inject",
            json={"latency_ms": 1000},
            headers={"X-Shared-Secret": "wrong-secret-value"},
        )
        assert resp.status_code == 403

    def test_reset_rejects_missing_secret(self):
        client = TestClient(controller_app)
        resp = client.post("/reset")
        assert resp.status_code == 403

    def test_health_is_public(self):
        client = TestClient(controller_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "controller"


class TestDemoUIEndpointsAndGroundTruthMasking:
    """Verifies SSE streaming, incident querying, and ground truth disclosure restrictions."""

    def test_index_page_serves_html(self):
        client = TestClient(demo_ui_app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "SURVEYOPS" in resp.text
        assert "Autonomous Multi-Agent" in resp.text
        assert "btn-inject" in resp.text
        assert "btn-reset" in resp.text
        assert "btn-options" in resp.text
        assert "health-row" in resp.text
        assert "step-strip" in resp.text
        assert "log-area" in resp.text
        # Task 1: SVG map and node inspector removed
        assert "arch-svg" not in resp.text
        assert "node-inspector" not in resp.text

    def test_sse_event_stream_format(self):
        client = TestClient(demo_ui_app)
        # Push an event to buffer
        test_inc = "inc-sse-test-01"
        record_event(test_inc, "api", "incident_opened", {"alert": "HighLatency", "threshold": 1.0})

        with client.stream("GET", f"/events/stream?incident_id={test_inc}&once=true") as stream:
            lines = []
            for line in stream.iter_lines():
                if line:
                    lines.append(line)
                if len(lines) >= 3:
                    break

            text = "\n".join(lines)
            assert "event:" in text or "data:" in text or "connected" in text

    def test_ground_truth_never_exposed_in_incident_query(self):
        client = TestClient(demo_ui_app)
        inc_id = "inc-trial-1793044393"
        resp = client.get(f"/incident/{inc_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["incident_id"] == inc_id
        # has_ground_truth is a boolean flag only
        assert isinstance(data["has_ground_truth"], bool)
        # Verify no ground truth failure mode leaked in top-level or payload
        assert "ground_truth_failure_mode" not in data
        assert "fault_id" not in data

    def test_reveal_endpoint_runs_scorer_and_returns_verdict(self):
        client = TestClient(demo_ui_app)
        # Call reveal on existing trial incident
        resp = client.post("/demo/reveal", json={"incident_id": "inc-trial-1793044393"})
        if resp.status_code == 404:
            pytest.skip("Report not present on this machine")
        assert resp.status_code == 200
        score = resp.json()
        assert "ground_truth_failure_mode" in score
        assert "predicted_root_cause" in score
        assert "is_correct" in score
        assert isinstance(score["is_correct"], bool)
        assert score["outcome"] == "root_cause"
        assert score["confidence"] == 0.80

    def test_replays_list_and_stream(self):
        client = TestClient(demo_ui_app)
        # Check list
        resp = client.get("/demo/replays")
        assert resp.status_code == 200
        replays = resp.json()
        assert isinstance(replays, list)
        assert any(r["name"] == "pool_exhaustion_demo" for r in replays)

        # Stream replay with fast=true for non-blocking test execution
        with client.stream("GET", "/demo/replays/pool_exhaustion_demo/stream?fast=true") as stream:
            chunks = []
            for chunk in stream.iter_lines():
                if chunk:
                    chunks.append(chunk)
                if len(chunks) >= 5:
                    break
            joined = "\n".join(chunks)
            assert "REPLAY" in joined or "is_replay" in joined

    def test_controller_metrics_endpoint_exposes_named_queries_and_rejects_promql(self):
        client = TestClient(controller_app)
        headers = {"X-Shared-Secret": "dev-secret"}

        # 1. Valid request to named metrics summary
        resp = client.get("/metrics/summary", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "payment_p99_ms" in data
        assert "pool_in_use" in data
        assert "pool_capacity" in data
        assert "alert_state" in data

        # 2. Reject arbitrary PromQL parameters
        bad_resp = client.get("/metrics/summary?query=up", headers=headers)
        assert bad_resp.status_code == 400
        assert "Arbitrary PromQL queries are rejected" in bad_resp.json()["detail"]

        bad_resp2 = client.get("/metrics/summary?promql=histogram_quantile(0.99,...)", headers=headers)
        assert bad_resp2.status_code == 400

    def test_demo_ui_metrics_proxy_and_rejects_promql(self):
        client = TestClient(demo_ui_app)
        # 1. Valid request
        resp = client.get("/demo/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "payment_p99_ms" in data
        assert "pool_in_use" in data
        assert "pool_capacity" in data
        assert "alert_state" in data

        # 2. Reject arbitrary PromQL queries
        bad_resp = client.get("/demo/metrics?query=rate(http_requests_total[1m])")
        assert bad_resp.status_code == 400
        assert "Arbitrary PromQL queries are rejected" in bad_resp.json()["detail"]

    def test_architecture_config_schema(self):
        cfg_path = Path(__file__).parent.parent / "surveyops" / "demo_ui" / "static" / "architecture_config.json"
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "zones" in cfg
        assert "nodes" in cfg
        assert "edges" in cfg
        assert "forbidden_edges" in cfg
        zone_ids = {z["id"] for z in cfg["zones"]}
        assert {"target_net", "data_net", "ops_net", "controller_net", "external"}.issubset(zone_ids)
        assert len(cfg["nodes"]) >= 18
        assert len(cfg["forbidden_edges"]) >= 5

    def test_get_architecture_config_endpoint(self):
        client = TestClient(demo_ui_app)
        # Test /static/architecture_config.json
        resp = client.get("/static/architecture_config.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "zones" in data
        assert len(data["nodes"]) >= 18

        # Test fallback /architecture_config.json
        resp2 = client.get("/architecture_config.json")
        assert resp2.status_code == 200
        assert "nodes" in resp2.json()

    def test_trial_reset_endpoint(self):
        from surveyops.api.main import app as api_app
        client = TestClient(api_app)
        headers = {"X-Shared-Secret": "dev-secret"}
        resp = client.post("/trial/reset", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reset"
        assert "open_incidents_cleared" in data

    def test_replay_plays_once_and_emits_completed(self):
        client = TestClient(demo_ui_app)
        with client.stream("GET", "/demo/replays/pool_exhaustion_demo/stream?fast=true") as stream:
            all_lines = list(stream.iter_lines())
            text = "\n".join(all_lines)
            assert "event: replay_completed" in text
            assert '"status": "completed"' in text

    def test_events_postgres_single_source_of_truth_fails_loudly(self, monkeypatch):
        from surveyops import events
        monkeypatch.delenv("SURVEYOPS_TEST_EVENTS_MEMORY", raising=False)
        monkeypatch.setenv("SURVEYOPS_POSTGRES_HOST", "invalid-host-definitely-does-not-exist.local")
        monkeypatch.setenv("SURVEYOPS_POSTGRES_PORT", "5432")
        with pytest.raises(RuntimeError) as exc_info:
            events.record_event("inc-fail-loud", "test", "test", {})
        assert "PostgreSQL events table unavailable in surveyops_postgres" in str(exc_info.value)

    def test_health_endpoints_controller_and_demo_ui(self):
        ctrl_client = TestClient(controller_app)
        ctrl_resp = ctrl_client.get("/health/services")
        assert ctrl_resp.status_code == 200
        ctrl_data = ctrl_resp.json()
        assert "services" in ctrl_data
        assert isinstance(ctrl_data["services"], list)

        ui_client = TestClient(demo_ui_app)
        ui_resp = ui_client.get("/demo/health")
        assert ui_resp.status_code == 200
        ui_data = ui_resp.json()
        assert "services" in ui_data

    def test_event_summary_plain_english_storage(self):
        inc_id = "inc-summary-test-99"
        ev = record_event(
            inc_id,
            "api",
            "incident_opened",
            {"alert": "HighLatency", "endpoint": "/payment/charge"},
            summary="Alert received: HighLatency on /payment/charge — starting investigation",
        )
        assert ev["payload"]["summary"] == "Alert received: HighLatency on /payment/charge — starting investigation"
        assert ev["payload"]["alert"] == "HighLatency"

