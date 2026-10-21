import json
import os
from pathlib import Path
from typing import Any, Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

from crewai import Agent, Crew, LLM, Task
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.propositions import Proposition, CheckableSignal
from surveyops.agents.surface_mapper.tools.graph_builder import (
    TargetAppGraph,
    build_target_app_graph,
)
from surveyops.agents.surface_mapper.tools.spec_parser import (
    SystemSpec,
    parse_system_specs,
)
from surveyops.agents.surface_mapper.guardrails import (
    ValidationReport,
    validate_propositions,
)
from surveyops.agents.surface_mapper.callbacks import SurfaceMapperCallbackHandler
from surveyops.agents.surface_mapper.storage import store_propositions


DEFAULT_CACHE_DIR = Path(__file__).parent / "cache"
DEFAULT_CACHE_FILE = DEFAULT_CACHE_DIR / "propositions_cache.json"


class PropositionList(BaseModel):
    propositions: list[Proposition] = Field(default_factory=list)


def build_surface_mapper_agent(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    callbacks: Optional[SurfaceMapperCallbackHandler] = None,
) -> Agent:
    model_name = model or os.getenv("MAPPER_MODEL")
    if not model_name:
        raise ValueError("MAPPER_MODEL environment variable is not configured")
    if "deepseek" in model_name.lower():
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
    else:
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    llm = LLM(model=model_name, api_key=key, temperature=0.1)

    return Agent(
        role="Distributed Systems Reliability Architect",
        goal=(
            "Statically inspect target microservices AST dependency graph and OpenAPI specifications "
            "to deduce plausible, falsifiable architectural failure modes per endpoint."
        ),
        backstory=(
            "You are an expert site reliability and distributed backend architect. "
            "You conduct static failure-mode and effects analysis (FMEA) purely on code structure: "
            "evaluating connection pool limits, database query patterns, pagination off-by-one risks, "
            "idempotency race conditions, and un-backed-off cross-service HTTP retry storms. "
            "You have NO access to runtime telemetry, logs, or metrics. "
            "Your output consists solely of unranked failure propositions referencing verified graph edges."
        ),
        llm=llm,
        verbose=False,
    )


def get_default_checkable_signal(failure_mode: FailureMode) -> CheckableSignal:
    """Returns the deterministic machine-checkable signal for a given failure mode."""
    if failure_mode == FailureMode.connection_pool_exhaustion:
        return CheckableSignal(
            type="metric",
            metric_name="pool_utilization_ratio",
            comparator=">=",
            threshold=0.80,
            log_pattern=r"QueuePool limit.*reached|connection timed out",
        )
    elif failure_mode == FailureMode.missing_index_n_plus_1:
        return CheckableSignal(
            type="log_pattern",
            log_pattern=r"slow query|seq scan|rows examined|full table scan",
        )
    elif failure_mode == FailureMode.race_condition_idempotency:
        return CheckableSignal(
            type="log_pattern",
            log_pattern=r"duplicate.*key|unique constraint.*idempotency|concurrent.*idempotency",
        )
    elif failure_mode == FailureMode.retry_storm_no_backoff:
        return CheckableSignal(
            type="metric",
            metric_name="request_rate",
            comparator=">=",
            threshold=50.0,
            log_pattern=r"retry storm|max retries exceeded",
        )
    elif failure_mode == FailureMode.upstream_dependency_failure:
        return CheckableSignal(
            type="metric",
            metric_name="error_rate",
            comparator=">=",
            threshold=0.05,
            log_pattern=r"502 Bad Gateway|Connection refused|Upstream.*failed",
        )
    elif failure_mode == FailureMode.pagination_off_by_one:
        return CheckableSignal(
            type="log_pattern",
            log_pattern=r"off-by-one|invalid offset|duplicate page item",
        )
    return CheckableSignal(
        type="metric",
        metric_name="error_rate",
        comparator=">=",
        threshold=0.05,
    )


def create_mapping_task(
    agent: Agent,
    graph: TargetAppGraph,
    spec: SystemSpec,
) -> Task:
    """Creates the CrewAI task for failure surface mapping."""
    # Summarize graph nodes and edges
    graph_summary = {
        "services": list(spec.services.keys()),
        "allowed_endpoints": spec.all_endpoints,
        "valid_graph_edge_refs": list(graph.get_edge_refs()),
        "http_dependencies": [
            {"from": e.source, "to": e.target, "ref": e.ref}
            for e in graph.edges
            if e.relation == "calls_http"
        ],
        "database_and_pool_edges": [
            {"endpoint": e.source, "resource": e.target, "relation": e.relation, "ref": e.ref}
            for e in graph.edges
            if e.relation in ("accesses_db", "uses_pool", "invokes")
        ],
    }

    allowed_failure_modes = [fm.value for fm in FailureMode]

    description = f"""
Analyze the static architecture of the target microservice application.

System Architecture and AST Dependency Summary:
{json.dumps(graph_summary, indent=2)}

Permitted Failure Modes (you MUST only use these exact values):
{json.dumps(allowed_failure_modes, indent=2)}

Analysis Instructions:
1. Examine every key business endpoint in the system (e.g. POST /orders, GET /orders, GET /orders/{{order_id}}, POST /payment/charge, POST /inventory/{{item_id}}/reserve, GET /inventory/{{item_id}}, POST /auth/login, GET /auth/verify).
2. For each endpoint, trace its downstream dependencies in the graph:
   - If it touches a database session or connection pool (`uses_pool`, `accesses_db`), analyze:
     * `connection_pool_exhaustion`: Can heavy latency or query load exhaust the pool?
     * `missing_index_n_plus_1`: Can queries scan full tables or lack index filters?
   - If it coordinates downstream HTTP calls to other services (`calls_http`), analyze:
     * `retry_storm_no_backoff`: Can downstream failures or timeouts trigger un-backed-off retries that cascade?
     * `upstream_dependency_failure`: Can downstream service failures or network disconnects break the endpoint?
   - If it checks idempotency or handles stateful financial operations, analyze:
     * `race_condition_idempotency`: Can concurrent identical requests race past non-atomic checks?
   - If it accepts pagination queries (e.g. page, page_size, offset), analyze:
     * `pagination_off_by_one`: Can boundary calculations lead to duplicate or missed items?

STRICT RULES:
- `endpoint`: MUST be one of the exact strings from `allowed_endpoints`.
- `graph_edge_ref`: MUST be an exact string from `valid_graph_edge_refs`.
- `failure_mode`: MUST be one of the permitted failure modes.
- `proposition_id`: Assign sequential IDs starting with 'SM-001', 'SM-002', etc.
- `justification`: Concise explanation grounded solely in the static code structure.
- `predicted_signal`: Falsifiable observation or metric that would manifest during this failure.
- `checkable_signal`: Machine-checkable verification rule object with `type` ("metric" or "log_pattern"):
  * For connection_pool_exhaustion: {{"type": "metric", "metric_name": "pool_utilization_ratio", "comparator": ">=", "threshold": 0.8}}
  * For missing_index_n_plus_1: {{"type": "log_pattern", "log_pattern": "slow query|seq scan|rows examined|full table scan"}}
  * For race_condition_idempotency: {{"type": "log_pattern", "log_pattern": "duplicate.*key|unique constraint|concurrent.*idempotency"}}
  * For retry_storm_no_backoff: {{"type": "metric", "metric_name": "request_rate", "comparator": ">=", "threshold": 50.0}}
  * For upstream_dependency_failure: {{"type": "metric", "metric_name": "error_rate", "comparator": ">=", "threshold": 0.05}}
  * For pagination_off_by_one: {{"type": "log_pattern", "log_pattern": "off-by-one|invalid offset|duplicate page item"}}
- UNRANKED: Do NOT include rankings or confidence values.
"""

    return Task(
        description=description + "\n\nOutput format:\nYou must output ONLY a valid JSON object matching the PropositionList schema:\n{\n  \"propositions\": [\n    {\n      \"proposition_id\": \"SM-001\",\n      \"endpoint\": \"POST /payment/charge\",\n      \"failure_mode\": \"connection_pool_exhaustion\",\n      \"justification\": \"...\",\n      \"predicted_signal\": \"...\",\n      \"checkable_signal\": {\n        \"type\": \"metric\",\n        \"metric_name\": \"pool_utilization_ratio\",\n        \"comparator\": \">=\",\n        \"threshold\": 0.8\n      },\n      \"graph_edge_ref\": \"...\"\n    }\n  ]\n}",
        expected_output="A JSON object conforming to PropositionList schema: {\"propositions\": [...]}.",
        agent=agent,
    )


def run_surface_mapper(
    target_app_dir: str | Path = "services/target_app",
    force_refresh: bool = False,
    cache_path: Optional[Path] = None,
    store_in_db: bool = True,
    model: Optional[str] = None,
) -> tuple[list[Proposition], ValidationReport, dict[str, Any]]:
    """
    Executes the Surface Mapper workflow:
    1. Deterministically builds AST graph and parses OpenAPI specs.
    2. Checks disk cache; if found and not force_refresh, returns cached validated propositions.
    3. Otherwise runs CrewAI agent in JSON mode with retry adapter.
    4. Deterministically validates all propositions with guardrails.
    5. Caches validated propositions on disk.
    6. Persists propositions in SurveyOps internal PostgreSQL.
    """
    from surveyops.llm_utils import execute_with_json_retry

    model_name = model or os.getenv("MAPPER_MODEL")
    if not model_name:
        raise ValueError("MAPPER_MODEL environment variable is not configured")
    app_dir = Path(target_app_dir)
    graph = build_target_app_graph(app_dir)
    spec = parse_system_specs(app_dir)

    target_cache_file = cache_path or DEFAULT_CACHE_FILE
    target_cache_file.parent.mkdir(parents=True, exist_ok=True)

    # Compute deterministic cache key including model name, mode, and checkable signal schema version
    import hashlib
    cache_key = hashlib.sha256(
        f"{model_name}|json_mode|v2_checkable_signals|{graph.model_dump_json()}|{spec.model_dump_json()}".encode("utf-8")
    ).hexdigest()

    metadata: dict[str, Any] = {
        "graph_nodes": len(graph.nodes),
        "graph_edges": len(graph.edges),
        "cross_service_edges": len([e for e in graph.edges if e.relation == "calls_http"]),
        "from_cache": False,
        "cache_key": cache_key,
        "token_usage": {},
    }

    # 1. Check disk cache matching current content hash
    if not force_refresh and target_cache_file.exists():
        try:
            with open(target_cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            if cached_data.get("cache_key") == cache_key:
                raw_props = [Proposition(**p) for p in cached_data.get("propositions", [])]
                for p in raw_props:
                    if not p.checkable_signal:
                        p.checkable_signal = get_default_checkable_signal(p.failure_mode)
                report = validate_propositions(raw_props, graph, spec)
                metadata["from_cache"] = True
                metadata["token_usage"] = cached_data.get("token_usage", {})

                if store_in_db and report.valid_propositions:
                    try:
                        store_propositions(report.valid_propositions)
                    except Exception as e:
                        metadata["db_storage_error"] = str(e)

                return report.valid_propositions, report, metadata
        except Exception:
            # Fall back to live generation if cache is corrupted or key mismatch
            pass

    # 2. Run CrewAI agent in JSON mode
    callbacks = SurfaceMapperCallbackHandler()
    agent = build_surface_mapper_agent(model=model_name, callbacks=callbacks)
    task = create_mapping_task(agent, graph, spec)

    def _execute_crew(prompt_text: str) -> str:
        current_task = Task(
            description=prompt_text,
            expected_output="A JSON object conforming to PropositionList schema: {\"propositions\": [...]}.",
            agent=agent,
        )
        crew = Crew(
            agents=[agent],
            tasks=[current_task],
            step_callback=callbacks.step_callback,
            task_callback=callbacks.task_callback,
            verbose=False,
        )
        res = crew.kickoff()
        if hasattr(res, "token_usage") and res.token_usage:
            tu = res.token_usage
            metadata["token_usage"] = {
                "total_tokens": getattr(tu, "total_tokens", None),
                "prompt_tokens": getattr(tu, "prompt_tokens", None),
                "completion_tokens": getattr(tu, "completion_tokens", None),
            }
        return res.raw

    parsed_list = execute_with_json_retry(
        execute_fn=_execute_crew,
        prompt=task.description,
        model_cls=PropositionList,
        max_retries=1,
    )
    raw_propositions = parsed_list.propositions
    for p in raw_propositions:
        if not p.checkable_signal:
            p.checkable_signal = get_default_checkable_signal(p.failure_mode)

    # 3. Guardrail validation
    report = validate_propositions(raw_propositions, graph, spec)

    # 4. Save to cache
    cache_payload = {
        "cache_key": cache_key,
        "propositions": [p.model_dump() for p in report.valid_propositions],
        "dropped": [d.model_dump() for d in report.dropped_propositions],
        "token_usage": metadata.get("token_usage", {}),
        "metadata": {
            "graph_nodes": metadata["graph_nodes"],
            "graph_edges": metadata["graph_edges"],
            "cross_service_edges": metadata["cross_service_edges"],
        },
    }
    with open(target_cache_file, "w", encoding="utf-8") as f:
        json.dump(cache_payload, f, indent=2)

    # 5. Store in Postgres
    if store_in_db and report.valid_propositions:
        try:
            store_propositions(report.valid_propositions)
        except Exception as e:
            metadata["db_storage_error"] = str(e)

    return report.valid_propositions, report, metadata
