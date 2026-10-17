import pytest
from pathlib import Path
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.propositions import Proposition
from surveyops.agents.surface_mapper.agent import run_surface_mapper
from surveyops.agents.surface_mapper.guardrails import validate_propositions
from surveyops.agents.surface_mapper.tools.graph_builder import build_target_app_graph
from surveyops.agents.surface_mapper.tools.spec_parser import parse_system_specs
from surveyops.agents.surface_mapper.callbacks import SurfaceMapperCallbackHandler
from surveyops.agents.surface_mapper.storage import (
    store_propositions,
    load_propositions,
    clear_propositions,
    get_surveyops_db_url,
)

TARGET_APP_DIR = Path(__file__).parent.parent / "services" / "target_app"


@pytest.fixture(scope="module")
def graph_and_spec():
    graph = build_target_app_graph(TARGET_APP_DIR)
    spec = parse_system_specs(TARGET_APP_DIR)
    return graph, spec


class TestGuardrailValidation:
    def test_valid_proposition_passes(self, graph_and_spec):
        graph, spec = graph_and_spec
        prop = Proposition(
            proposition_id="SM-VALID-1",
            endpoint="POST /payment/charge",
            failure_mode=FailureMode.connection_pool_exhaustion,
            justification="Database latency exhausts connection pool",
            predicted_signal="Pool exhaustion timeout",
            graph_edge_ref="payment.charge() -> db.connection_pool",
        )
        report = validate_propositions([prop], graph, spec)
        assert report.total_valid == 1
        assert report.total_dropped == 0
        assert len(report.valid_propositions) == 1

    def test_hallucinated_endpoint_dropped(self, graph_and_spec):
        graph, spec = graph_and_spec
        prop = Proposition(
            proposition_id="SM-INVALID-EP",
            endpoint="POST /nonexistent/charge",
            failure_mode=FailureMode.connection_pool_exhaustion,
            justification="Invalid endpoint",
            predicted_signal="Error",
            graph_edge_ref="payment.charge() -> db.connection_pool",
        )
        report = validate_propositions([prop], graph, spec)
        assert report.total_valid == 0
        assert report.total_dropped == 1
        assert report.dropped_propositions[0].reason == "endpoint_not_in_spec"

    def test_invented_edge_dropped(self, graph_and_spec):
        graph, spec = graph_and_spec
        prop = Proposition(
            proposition_id="SM-INVALID-EDGE",
            endpoint="POST /payment/charge",
            failure_mode=FailureMode.connection_pool_exhaustion,
            justification="Invented redis dependency",
            predicted_signal="Error",
            graph_edge_ref="payment.charge() -> redis.cluster_cache",
        )
        report = validate_propositions([prop], graph, spec)
        assert report.total_valid == 0
        assert report.total_dropped == 1
        assert report.dropped_propositions[0].reason == "edge_not_in_graph"


class TestSurfaceMapperStorage:
    def test_db_roundtrip(self):
        try:
            url = get_surveyops_db_url()
            clear_propositions(url)
        except Exception as e:
            pytest.skip(f"SurveyOps Postgres not accessible: {e}")

        test_prop = Proposition(
            proposition_id="SM-STORE-TEST",
            endpoint="POST /orders",
            failure_mode=FailureMode.retry_storm_no_backoff,
            justification="Un-backed-off retry storm to payment service",
            predicted_signal="High 5xx rate on downstream",
            graph_edge_ref="orders.create_order() -> payment.charge()",
        )

        stored = store_propositions([test_prop], url)
        assert stored == 1

        loaded = load_propositions(endpoint="POST /orders", db_url=url)
        assert len(loaded) == 1
        assert loaded[0].proposition_id == "SM-STORE-TEST"
        assert loaded[0].failure_mode == FailureMode.retry_storm_no_backoff

        clear_propositions(url)


class TestSurfaceMapperCallbacks:
    def test_callback_logging(self):
        handler = SurfaceMapperCallbackHandler()

        class DummyOutput:
            description = "Test surface mapping task"
            summary = "Generated 5 failure propositions"
            raw = "raw propositions output"
            error = None

        handler.task_callback(DummyOutput())
        assert len(handler.logs) == 1
        log = handler.logs[0]
        assert log["event"] == "task_completion"
        assert "Test surface mapping" in log["task"]
        assert log["latency_ms"] >= 0
        assert "5 failure propositions" in log["summary"]


class TestSurfaceMapperCategoryCoverage:
    """
    Evaluates that the Surface Mapper covers the failure categories defined in FailureMode
    (docs/07-EVALUATION.md) by mapping them to valid endpoints and existing AST graph edges:
    1. connection_pool_exhaustion (payment or orders)
    2. retry_storm_no_backoff (orders)
    3. missing_index_n_plus_1 (orders)
    4. race_condition_idempotency (payment)
    5. pagination_off_by_one (orders)

    Note: These propositions represent plausible failure hypotheses generated by the mapper
    over the static topology. They are not discoveries of active bugs, as the target app
    orders service does not currently implement a retry loop.
    """

    def test_category_coverage(self):
        props, report, meta = run_surface_mapper(
            target_app_dir=TARGET_APP_DIR,
            force_refresh=False,
            store_in_db=False,
        )

        assert report.total_valid > 0
        assert report.total_dropped == 0

        # Check each planned fault from 07-EVALUATION.md
        found_pool_exhaustion = any(
            p.failure_mode == FailureMode.connection_pool_exhaustion
            and p.endpoint in ("POST /payment/charge", "POST /orders")
            for p in props
        )
        found_retry_storm = any(
            p.failure_mode == FailureMode.retry_storm_no_backoff
            and p.endpoint == "POST /orders"
            for p in props
        )
        found_missing_index = any(
            p.failure_mode == FailureMode.missing_index_n_plus_1
            and p.endpoint == "GET /orders"
            for p in props
        )
        found_idempotency_race = any(
            p.failure_mode == FailureMode.race_condition_idempotency
            and p.endpoint == "POST /payment/charge"
            for p in props
        )
        found_pagination_off_by_one = any(
            p.failure_mode == FailureMode.pagination_off_by_one
            and p.endpoint == "GET /orders"
            for p in props
        )

        results = {
            "connection_pool_exhaustion": found_pool_exhaustion,
            "retry_storm_no_backoff": found_retry_storm,
            "missing_index_n_plus_1": found_missing_index,
            "race_condition_idempotency": found_idempotency_race,
            "pagination_off_by_one": found_pagination_off_by_one,
        }

        matched_count = sum(1 for v in results.values() if v)
        coverage_ratio = matched_count / len(results)

        print(f"\n--- Surface Mapper Defined Category Coverage: {coverage_ratio:.2f} ({matched_count}/{len(results)}) ---")
        for fault, matched in results.items():
            print(f"  {fault}: {'COVERED' if matched else 'UNCOVERED'}")

        assert coverage_ratio >= 0.8, f"Category coverage {coverage_ratio} below 80% threshold: {results}"
        assert matched_count == 5, f"Expected 5/5 categories covered, got {matched_count}/5: {results}"

    def test_cache_keys_on_content_hash(self, tmp_path, monkeypatch):
        """Verifies that cache invalidates when graph/spec content changes."""
        from unittest import mock
        from crewai.crews.crew_output import CrewOutput

        dummy_cache = tmp_path / "test_cache.json"
        # Write dummy cache with an obsolete cache_key
        dummy_cache.write_text('{"cache_key": "old_obsolete_hash", "propositions": []}')

        mock_output = mock.MagicMock(spec=CrewOutput)
        mock_output.pydantic = None
        mock_output.raw = '{"propositions": []}'
        mock_output.token_usage = None
        monkeypatch.setattr("crewai.Crew.kickoff", lambda self: mock_output)

        props, report, meta = run_surface_mapper(
            target_app_dir=TARGET_APP_DIR,
            cache_path=dummy_cache,
            force_refresh=False,
            store_in_db=False,
        )
        # Obsolete cache_key was detected and bypassed
        assert meta["from_cache"] is False
        assert meta.get("cache_key") != "old_obsolete_hash"

    def test_unranked_invariant(self):
        """Propositions must be unranked with no confidence scores."""
        props, _, _ = run_surface_mapper(
            target_app_dir=TARGET_APP_DIR,
            force_refresh=False,
            store_in_db=False,
        )
        for p in props:
            # Pydantic model Proposition has no score or rank field
            assert not hasattr(p, "confidence")
            assert not hasattr(p, "rank")
            assert not hasattr(p, "probability")
