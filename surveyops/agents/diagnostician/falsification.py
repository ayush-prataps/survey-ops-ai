"""
Falsification Guardrail & Request Validation.
Enforces Requirement 3 from AGENTS.md / docs/04-CONTRACTS.md:
- Every experiment request must state what result would disprove the leading hypothesis (disconfirming_if).
- Rejects empty or whitespace-only disconfirming_if.
- Rejects experiment types, services, templates, or endpoints outside the Runner's allowlist before sending.
"""

from typing import Any
from surveyops.schemas.experiments import ExperimentRequest, ExperimentType

ALLOWED_SERVICES = {"orders", "payment", "inventory", "auth"}
ALLOWED_ENDPOINTS = {
    "/orders",
    "/payment/charge",
    "/inventory/{item_id}",
    "/inventory/{item_id}/reserve",
    "/inventory/item-1",
    "/auth/login",
    "/auth/verify",
    "/health",
    "/metrics",
}
ALLOWED_TEMPLATES = {
    "p99_latency",
    "error_rate",
    "request_rate",
    "pool_in_use",
    "pool_capacity",
}


def validate_experiment_request(req: ExperimentRequest) -> None:
    """
    Validates an ExperimentRequest before dispatching to the Runner.
    Raises ValueError on falsification or allowlist violations.
    """
    # 1. Falsification rule: non-empty disconfirming_if condition
    if not req.disconfirming_if or not req.disconfirming_if.strip():
        raise ValueError(
            "Falsification rule violation: 'disconfirming_if' is empty. "
            "Every experiment request must explicitly state what observation would disprove the leading hypothesis."
        )

    if len(req.disconfirming_if.strip()) < 5:
        raise ValueError(
            f"Falsification rule violation: 'disconfirming_if' ('{req.disconfirming_if}') is too brief to be falsifiable."
        )

    # 2. Experiment Type Check
    if req.experiment_type not in (ExperimentType.query_metrics, ExperimentType.fetch_logs, ExperimentType.load_burst):
        raise ValueError(f"Disallowed experiment type: {req.experiment_type}")

    # 3. Parameter checks against Runner allowlist
    params = req.params or {}

    if req.experiment_type == ExperimentType.query_metrics:
        template = params.get("template")
        if not template or template not in ALLOWED_TEMPLATES:
            raise ValueError(
                f"Disallowed query template '{template}'. Allowed templates: {sorted(ALLOWED_TEMPLATES)}"
            )
        service = params.get("service")
        if service and service not in ALLOWED_SERVICES:
            raise ValueError(
                f"Disallowed service '{service}' for metrics. Allowed services: {sorted(ALLOWED_SERVICES)}"
            )
        endpoint = params.get("endpoint")
        if endpoint and endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(
                f"Disallowed endpoint '{endpoint}' for metrics. Allowed endpoints: {sorted(ALLOWED_ENDPOINTS)}"
            )

    elif req.experiment_type == ExperimentType.fetch_logs:
        service = params.get("service")
        if not service or service not in ALLOWED_SERVICES:
            raise ValueError(
                f"Disallowed or missing service '{service}' for log query. Allowed services: {sorted(ALLOWED_SERVICES)}"
            )

    elif req.experiment_type == ExperimentType.load_burst:
        endpoint = params.get("endpoint")
        if not endpoint or endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(
                f"Disallowed or missing endpoint '{endpoint}' for load burst. Allowed endpoints: {sorted(ALLOWED_ENDPOINTS)}"
            )
