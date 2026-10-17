import pytest
from pathlib import Path
from surveyops.agents.surface_mapper.tools.graph_builder import (
    build_target_app_graph,
    TargetAppGraph,
)
from surveyops.agents.surface_mapper.tools.spec_parser import (
    parse_system_specs,
    parse_openapi_dict,
    SystemSpec,
)

TARGET_APP_DIR = Path(__file__).parent.parent / "services" / "target_app"


class TestGraphBuilder:
    def test_build_graph_structure(self):
        graph = build_target_app_graph(TARGET_APP_DIR)
        assert isinstance(graph, TargetAppGraph)
        assert len(graph.nodes) >= 20
        assert len(graph.edges) >= 15

    def test_endpoints_discovered(self):
        graph = build_target_app_graph(TARGET_APP_DIR)
        node_ids = {n.id for n in graph.nodes if n.type == "endpoint"}
        assert "orders:POST /orders" in node_ids
        assert "orders:GET /orders" in node_ids
        assert "orders:GET /orders/{order_id}" in node_ids
        assert "payment:POST /payment/charge" in node_ids
        assert "inventory:GET /inventory/{item_id}" in node_ids
        assert "inventory:POST /inventory/{item_id}/reserve" in node_ids
        assert "auth:POST /auth/login" in node_ids
        assert "auth:GET /auth/verify" in node_ids

    def test_cross_service_http_edges(self):
        graph = build_target_app_graph(TARGET_APP_DIR)
        http_edges = [e for e in graph.edges if e.relation == "calls_http"]
        assert len(http_edges) >= 3

        # orders calls auth, inventory, payment
        calls = {(e.source, e.target) for e in http_edges}
        assert ("orders:POST /orders", "payment:POST /payment/charge") in calls
        assert ("orders:POST /orders", "auth:GET /auth/verify") in calls
        assert ("orders:POST /orders", "inventory:POST /inventory/{item_id}/reserve") in calls

    def test_database_and_pool_edges(self):
        graph = build_target_app_graph(TARGET_APP_DIR)
        db_edges = [e for e in graph.edges if e.relation == "accesses_db"]
        pool_edges = [e for e in graph.edges if e.relation == "uses_pool"]

        assert len(db_edges) > 0
        assert len(pool_edges) > 0

        edge_refs = graph.get_edge_refs()
        assert "payment.charge() -> db.connection_pool" in edge_refs
        assert "orders.create_order() -> db.session" in edge_refs

    def test_internal_invocations(self):
        graph = build_target_app_graph(TARGET_APP_DIR)
        invokes = [e for e in graph.edges if e.relation == "invokes"]
        assert any("check_idempotency" in e.target for e in invokes)


class TestSpecParser:
    def test_parse_system_specs(self):
        specs = parse_system_specs(TARGET_APP_DIR)
        assert isinstance(specs, SystemSpec)
        assert "orders" in specs.services
        assert "payment" in specs.services
        assert "inventory" in specs.services
        assert "auth" in specs.services

    def test_endpoint_validation(self):
        specs = parse_system_specs(TARGET_APP_DIR)
        assert specs.is_valid_endpoint("POST /orders") is True
        assert specs.is_valid_endpoint("GET /orders") is True
        assert specs.is_valid_endpoint("POST /payment/charge") is True
        assert specs.is_valid_endpoint("GET /inventory/{item_id}") is True
        assert specs.is_valid_endpoint("POST /auth/login") is True
        assert specs.is_valid_endpoint("POST /nonexistent/endpoint") is False

    def test_parameters_and_responses_parsed(self):
        raw_openapi = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0"},
            "paths": {
                "/test/{id}": {
                    "get": {
                        "parameters": [{"name": "id", "in": "path", "required": True}],
                        "responses": {"200": {"description": "OK"}, "404": "Not Found"},
                    }
                }
            },
        }
        parsed = parse_openapi_dict("test_svc", raw_openapi)
        assert len(parsed.endpoints) == 1
        ep = parsed.endpoints[0]
        assert ep.method == "GET"
        assert ep.path == "/test/{id}"
        assert ep.full_endpoint == "GET /test/{id}"
        assert ep.parameters[0]["name"] == "id"
        assert "200" in ep.status_codes
        assert "404" in ep.status_codes
