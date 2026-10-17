import json
import urllib.request
from pathlib import Path
import pytest
from surveyops.agents.surface_mapper.tools.graph_builder import build_target_app_graph

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_APP_DIR = REPO_ROOT / "services" / "target_app"

SERVICES = [
    ("orders", 8001),
    ("payment", 8002),
    ("inventory", 8003),
    ("auth", 8004),
]


def test_static_openapi_files_exist_and_match_ast_endpoints():
    """
    Offline drift test:
    Validates that each static openapi.json file exists, is valid JSON,
    and includes all route paths discovered by AST graph parsing of main.py.
    """
    graph = build_target_app_graph(TARGET_APP_DIR)
    ast_endpoints_by_svc: dict[str, set[str]] = {}
    for node in graph.nodes:
        if node.type == "endpoint":
            path = node.metadata.get("path")
            if path and path not in ("/health", "/metrics"):
                ast_endpoints_by_svc.setdefault(node.service, set()).add(path)

    for svc_name, _ in SERVICES:
        spec_file = TARGET_APP_DIR / svc_name / "openapi.json"
        assert spec_file.exists(), f"Static openapi.json missing for {svc_name}"

        with open(spec_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        spec_paths = set(data.get("paths", {}).keys())
        ast_paths = ast_endpoints_by_svc.get(svc_name, set())

        # All AST non-system endpoints must exist in openapi.json
        for p in ast_paths:
            assert p in spec_paths, f"Endpoint '{p}' discovered in {svc_name}/main.py is missing from openapi.json"


def test_live_openapi_diff_if_services_running():
    """
    Live drift test:
    If target app containers are running, fetches live openapi.json
    and asserts zero path drift against static repository files.
    """
    for svc_name, port in SERVICES:
        url = f"http://localhost:{port}/openapi.json"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                live_spec = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # Skip live check if container is not currently running
            continue

        spec_file = TARGET_APP_DIR / svc_name / "openapi.json"
        with open(spec_file, "r", encoding="utf-8") as f:
            saved_spec = json.load(f)

        live_paths = set(live_spec.get("paths", {}).keys())
        saved_paths = set(saved_spec.get("paths", {}).keys())

        assert live_paths == saved_paths, (
            f"OpenAPI path drift detected in {svc_name}! "
            f"Live: {live_paths} vs Saved: {saved_paths}. "
            f"Run python scripts/generate_openapi.py to update."
        )
