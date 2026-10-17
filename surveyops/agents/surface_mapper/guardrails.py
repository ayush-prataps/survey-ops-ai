from typing import Any
from pydantic import BaseModel, Field
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.propositions import Proposition
from surveyops.agents.surface_mapper.tools.graph_builder import TargetAppGraph
from surveyops.agents.surface_mapper.tools.spec_parser import SystemSpec


class DroppedProposition(BaseModel):
    proposition_id: str
    endpoint: str
    failure_mode: str
    graph_edge_ref: str
    reason: str
    details: str


class ValidationReport(BaseModel):
    valid_propositions: list[Proposition] = Field(default_factory=list)
    dropped_propositions: list[DroppedProposition] = Field(default_factory=list)
    total_received: int = 0
    total_valid: int = 0
    total_dropped: int = 0
    drop_reasons: dict[str, int] = Field(default_factory=dict)


def validate_propositions(
    propositions: list[Proposition],
    graph: TargetAppGraph,
    spec: SystemSpec,
) -> ValidationReport:
    """
    Deterministic guardrail to filter propositions returned by LLM:
    1. Endpoint must exist in the parsed OpenAPI spec.
    2. graph_edge_ref must exist in the AST dependency graph.
    3. failure_mode must be an allowed FailureMode enum member.

    Any hallucinated endpoints, invented edges, or invalid failure modes are dropped.
    """
    valid: list[Proposition] = []
    dropped: list[DroppedProposition] = []
    drop_reasons: dict[str, int] = {}

    valid_edge_refs = graph.get_edge_refs()
    # Also include raw source -> target node references as acceptable edge references
    for e in graph.edges:
        valid_edge_refs.add(f"{e.source} -> {e.target}")
        valid_edge_refs.add(f"{e.source}->{e.target}")

    for prop in propositions:
        # Check 1: failure mode validity
        if not isinstance(prop.failure_mode, FailureMode) and prop.failure_mode not in FailureMode:
            reason = "invalid_failure_mode"
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            dropped.append(
                DroppedProposition(
                    proposition_id=prop.proposition_id,
                    endpoint=prop.endpoint,
                    failure_mode=str(prop.failure_mode),
                    graph_edge_ref=prop.graph_edge_ref,
                    reason=reason,
                    details=f"Failure mode '{prop.failure_mode}' not in FailureMode enum",
                )
            )
            continue

        # Check 2: endpoint exists in spec
        if not spec.is_valid_endpoint(prop.endpoint):
            reason = "endpoint_not_in_spec"
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            dropped.append(
                DroppedProposition(
                    proposition_id=prop.proposition_id,
                    endpoint=prop.endpoint,
                    failure_mode=str(prop.failure_mode),
                    graph_edge_ref=prop.graph_edge_ref,
                    reason=reason,
                    details=f"Endpoint '{prop.endpoint}' not found in OpenAPI specifications",
                )
            )
            continue

        # Check 3: graph_edge_ref exists in AST graph
        edge_ref = prop.graph_edge_ref.strip()
        edge_matched = edge_ref in valid_edge_refs
        if not edge_matched:
            # Check normalized match without spaces around ->
            normalized_ref = edge_ref.replace(" ", "")
            for ref in valid_edge_refs:
                if ref.replace(" ", "") == normalized_ref:
                    edge_matched = True
                    break

        if not edge_matched:
            reason = "edge_not_in_graph"
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            dropped.append(
                DroppedProposition(
                    proposition_id=prop.proposition_id,
                    endpoint=prop.endpoint,
                    failure_mode=str(prop.failure_mode),
                    graph_edge_ref=prop.graph_edge_ref,
                    reason=reason,
                    details=f"Graph edge reference '{prop.graph_edge_ref}' does not exist in the AST dependency graph",
                )
            )
            continue

        valid.append(prop)

    return ValidationReport(
        valid_propositions=valid,
        dropped_propositions=dropped,
        total_received=len(propositions),
        total_valid=len(valid),
        total_dropped=len(dropped),
        drop_reasons=drop_reasons,
    )
