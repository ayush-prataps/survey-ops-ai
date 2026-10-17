"""
Tests for:
1. Pydantic schema validation & roundtrip across all models in surveyops/schemas/
2. Malformed payload rejection
3. A2A transport utilities in surveyops/a2a_utils.py
"""

from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError

from a2a.types import Message, TextPart, Role

from surveyops.schemas import (
    FailureMode,
    Proposition,
    InvestigatePayload,
    ExperimentType,
    ExperimentRequest,
    Observation,
    LedgerEntry,
    Outcome,
    DiagnosisResult,
    RemediationResult,
    IncidentReport,
)
from surveyops.a2a_utils import (
    to_a2a_text_part,
    from_a2a_text,
    from_a2a_message,
    A2ADecodeResult,
)


# ── Sample model instances ───────────────────────────────────────────────────

@pytest.fixture
def sample_proposition() -> Proposition:
    return Proposition(
        proposition_id="SM-001",
        endpoint="POST /payment/charge",
        failure_mode=FailureMode.connection_pool_exhaustion,
        justification="High concurrency checkout pool exhaustion",
        predicted_signal="p99 latency > 0.5s",
        graph_edge_ref="payment.charge() -> db.connection_pool",
    )


@pytest.fixture
def sample_investigate_payload() -> InvestigatePayload:
    return InvestigatePayload(
        alert="HighP99Latency",
        service="payment",
        endpoint="/payment/charge",
        metric="http_request_duration_seconds",
        observed=None,
        threshold=0.5,
        window_start=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_experiment_request() -> ExperimentRequest:
    return ExperimentRequest(
        request_id="req-101",
        hypothesis_id="SM-001",
        experiment_type=ExperimentType.query_metrics,
        params={"query": "histogram_quantile(0.99, ...)", "range_s": 300},
        rationale="Check if p99 latency spiked during the incident window",
        disconfirming_if="p99 latency < 0.1s",
    )


@pytest.fixture
def sample_observation() -> Observation:
    return Observation(
        request_id="req-101",
        source="prometheus",
        data={"p99": 2.45},
        collected_at=datetime.now(timezone.utc),
        truncated=False,
        contains_untrusted_text=False,
    )


@pytest.fixture
def sample_ledger_entry() -> LedgerEntry:
    return LedgerEntry(
        iteration=1,
        hypothesis_id="SM-001",
        evidence_for=["p99 is 2.45s", "queue pool timeout in logs"],
        evidence_against=[],
        confidence=0.85,
        status="active",
    )


@pytest.fixture
def sample_diagnosis_result() -> DiagnosisResult:
    return DiagnosisResult(
        incident_id="inc-abc1234",
        outcome=Outcome.root_cause,
        root_cause=FailureMode.connection_pool_exhaustion,
        endpoint="/payment/charge",
        supporting_proposition_id="SM-001",
        graph_edge_ref="payment.charge() -> db.connection_pool",
        confidence=0.92,
        ruled_out=["missing_index_n_plus_1"],
        iterations_used=2,
    )


@pytest.fixture
def sample_remediation_result() -> RemediationResult:
    return RemediationResult(
        pr_url="https://github.com/ayush-prataps/survey-ops-ai/pull/42",
        branch="fix/payment-pool-size",
        files_changed=["services/target_app/payment/main.py"],
        tests_added=["tests/test_payment_pool.py"],
    )


@pytest.fixture
def sample_incident_report(
    sample_diagnosis_result,
    sample_ledger_entry,
    sample_remediation_result,
) -> IncidentReport:
    return IncidentReport(
        incident_id="inc-abc1234",
        diagnosis=sample_diagnosis_result,
        evidence_trail=[sample_ledger_entry],
        remediation=sample_remediation_result,
        timings={"detect": 45, "diagnose": 60, "pr": 30},
        usage={"tokens": 12500, "cost_usd": 0.015},
    )


# ── Model roundtrip tests ────────────────────────────────────────────────────

class TestModelRoundtrips:
    def test_proposition_roundtrip(self, sample_proposition):
        raw = sample_proposition.model_dump_json()
        loaded = Proposition.model_validate_json(raw)
        assert loaded == sample_proposition

    def test_investigate_payload_roundtrip(self, sample_investigate_payload):
        raw = sample_investigate_payload.model_dump_json()
        loaded = InvestigatePayload.model_validate_json(raw)
        assert loaded == sample_investigate_payload

    def test_experiment_request_roundtrip(self, sample_experiment_request):
        raw = sample_experiment_request.model_dump_json()
        loaded = ExperimentRequest.model_validate_json(raw)
        assert loaded == sample_experiment_request

    def test_observation_roundtrip(self, sample_observation):
        raw = sample_observation.model_dump_json()
        loaded = Observation.model_validate_json(raw)
        assert loaded == sample_observation

    def test_ledger_entry_roundtrip(self, sample_ledger_entry):
        raw = sample_ledger_entry.model_dump_json()
        loaded = LedgerEntry.model_validate_json(raw)
        assert loaded == sample_ledger_entry

    def test_diagnosis_result_roundtrip(self, sample_diagnosis_result):
        raw = sample_diagnosis_result.model_dump_json()
        loaded = DiagnosisResult.model_validate_json(raw)
        assert loaded == sample_diagnosis_result

    def test_remediation_result_roundtrip(self, sample_remediation_result):
        raw = sample_remediation_result.model_dump_json()
        loaded = RemediationResult.model_validate_json(raw)
        assert loaded == sample_remediation_result

    def test_incident_report_roundtrip(self, sample_incident_report):
        raw = sample_incident_report.model_dump_json()
        loaded = IncidentReport.model_validate_json(raw)
        assert loaded == sample_incident_report


# ── Malformed payload rejection tests ────────────────────────────────────────

class TestMalformedPayloadRejection:
    def test_invalid_failure_mode_rejected(self):
        with pytest.raises(ValidationError):
            Proposition(
                proposition_id="SM-001",
                endpoint="/payment/charge",
                failure_mode="non_existent_failure_mode",  # invalid
                justification="bad",
                predicted_signal="none",
                graph_edge_ref="none",
            )

    def test_missing_required_fields_rejected(self):
        with pytest.raises(ValidationError):
            DiagnosisResult(
                incident_id="inc-1",
                outcome=Outcome.root_cause,
                # missing endpoint, iterations_used
            )

    def test_invalid_experiment_type_rejected(self):
        with pytest.raises(ValidationError):
            ExperimentRequest(
                request_id="req-1",
                hypothesis_id="hyp-1",
                experiment_type="invalid_experiment",  # invalid
                rationale="test",
                disconfirming_if="test",
            )


# ── A2A Transport Utils tests ─────────────────────────────────────────────────

class TestA2AUtils:
    def test_to_a2a_text_part(self, sample_diagnosis_result):
        part = to_a2a_text_part(sample_diagnosis_result)
        assert isinstance(part, TextPart)
        parsed = json.loads(part.text)
        assert parsed["incident_id"] == sample_diagnosis_result.incident_id
        assert parsed["outcome"] == "root_cause"

    def test_from_a2a_text_valid(self, sample_diagnosis_result):
        json_str = sample_diagnosis_result.model_dump_json()
        result = from_a2a_text(json_str, DiagnosisResult)
        assert result.is_ok
        assert result.value == sample_diagnosis_result
        assert result.error is None

    def test_from_a2a_text_malformed(self):
        result = from_a2a_text("{not valid json", DiagnosisResult)
        assert not result.is_ok
        assert result.value is None
        assert "A2A validation error" in result.error

    def test_from_a2a_text_schema_mismatch(self):
        result = from_a2a_text('{"incident_id": "123"}', DiagnosisResult)
        assert not result.is_ok
        assert result.value is None
        assert "validation error" in result.error.lower()

    def test_from_a2a_message_with_text_part(self, sample_remediation_result):
        part = to_a2a_text_part(sample_remediation_result)
        msg = Message(role=Role.agent, parts=[part], message_id="msg-100")
        result = from_a2a_message(msg, RemediationResult)
        assert result.is_ok
        assert result.value == sample_remediation_result

    def test_from_a2a_message_with_dict(self, sample_experiment_request):
        data = sample_experiment_request.model_dump()
        result = from_a2a_message(data, ExperimentRequest)
        assert result.is_ok
        assert result.value == sample_experiment_request

    def test_from_a2a_message_none_or_empty(self):
        res1 = from_a2a_message(None, DiagnosisResult)
        assert not res1.is_ok
        assert "None" in res1.error

        res2 = from_a2a_text("", DiagnosisResult)
        assert not res2.is_ok
        assert "Empty" in res2.error
