# Contracts

This is the interface spec. Code in `surveyops/schemas/` must match it. Change here first, then in code.

## Pydantic models (shape, not final code)
```python
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

class FailureMode(str, Enum):
    connection_pool_exhaustion = "connection_pool_exhaustion"
    missing_index_n_plus_1 = "missing_index_n_plus_1"
    race_condition_idempotency = "race_condition_idempotency"
    pagination_off_by_one = "pagination_off_by_one"
    retry_storm_no_backoff = "retry_storm_no_backoff"
    # extend as the fault set grows

class Proposition(BaseModel):          # Surface Mapper output. UNRANKED, no confidence.
    proposition_id: str                # e.g. "SM-014"
    endpoint: str                      # e.g. "POST /payment/charge"
    failure_mode: FailureMode
    justification: str
    predicted_signal: str
    graph_edge_ref: str                # e.g. "payment.charge() -> db.connection_pool"

class InvestigatePayload(BaseModel):   # Alertmanager -> /investigate. SYMPTOM ONLY.
    alert: str
    service: str
    endpoint: str
    metric: str
    observed: float
    threshold: float
    window_start: datetime
    # NEVER add a root-cause or fault label here.

class ExperimentType(str, Enum):
    query_metrics = "query_metrics"
    fetch_logs = "fetch_logs"
    load_burst = "load_burst"

class ExperimentRequest(BaseModel):    # Diagnostician -> Runner (A2A)
    request_id: str
    hypothesis_id: str
    experiment_type: ExperimentType
    params: dict
    rationale: str
    disconfirming_if: str              # what result would disprove the hypothesis

class Observation(BaseModel):          # Runner -> Diagnostician (A2A). RAW ONLY.
    request_id: str
    source: str
    data: dict
    collected_at: datetime
    truncated: bool = False
    contains_untrusted_text: bool = False   # true for log content

class LedgerEntry(BaseModel):
    iteration: int
    hypothesis_id: str
    evidence_for: list[str]
    evidence_against: list[str]
    confidence: float
    status: str                        # active | ruled_out | dominant

class Outcome(str, Enum):
    root_cause = "root_cause"
    budget_exhausted = "budget_exhausted"
    abstained = "abstained"

class DiagnosisResult(BaseModel):      # Diagnostician -> Remediator (A2A)
    incident_id: str
    outcome: Outcome
    root_cause: FailureMode | None
    endpoint: str
    supporting_proposition_id: str | None
    graph_edge_ref: str | None
    confidence: float | None
    ruled_out: list[str]
    iterations_used: int

class RemediationResult(BaseModel):    # Remediator output
    pr_url: str | None
    branch: str | None
    files_changed: list[str]
    tests_added: list[str]

class IncidentReport(BaseModel):       # final structured output, rendered to Markdown by template
    incident_id: str
    diagnosis: DiagnosisResult
    evidence_trail: list[LedgerEntry]
    remediation: RemediationResult | None
    timings: dict                      # detect, diagnose, pr (seconds)
    usage: dict                        # tokens, notional cost
```

## ADK session state keys (Diagnostician and Runner)
`incident` (InvestigatePayload), `propositions` (list[Proposition] for the endpoint), `hypotheses`, `ledger` (list[LedgerEntry]), `iteration`, `max_iterations`, `pending_request`, `last_observation`, `diagnosis`.

## A2A messages
- Diagnostician -> Runner: `ExperimentRequest`. Runner -> Diagnostician: `Observation`.
- Diagnostician -> Remediator: `DiagnosisResult`. Remediator -> caller: `RemediationResult`.
- Remediator is exposed behind an A2A server wrapper (verify CrewAI native support first).

## MCP tools
| Tool | Held by | Signature (draft) |
|---|---|---|
| `prometheus_query` | Runner | (promql: str, range_s: int) -> data. Read-only, allowlisted queries |
| `fetch_logs` | Runner | (service: str, since_s: int, limit: int) -> lines. Output sanitized and delimited |
| `run_load_burst` | Runner | (endpoint: str, concurrency: int, duration_s: int) -> stats. Capped by env limits |
| `repo_read` | Remediator | (path: str) -> content |
| `github_open_pr` | Remediator | (branch, title, body, files) -> pr_url. Never merge |

Surface Mapper tools (graph builder, spec parser) are plain code tools, wrapped as MCP only if it adds value.

## /investigate
`POST /investigate`, header `X-Shared-Secret`, body `InvestigatePayload`. Creates an incident record, starts the Diagnostician. Lives on the SurveyOps backend, not the target app.

## A2A transport rules (from spike 2, 2026-10-14)
- Payloads travel as `TextPart(model_dump_json())`, not `DataPart`. Applies to every A2A link.
- Every receiver validates with Pydantic. On `ValidationError` it returns a failed task status (via `new_agent_text_message`), never crashes.
- Every sender catches `ValidationError` on the reply and treats it as a failed experiment or failed remediation.
- Session state does not cross A2A. The message must carry everything the receiver needs.
- Deterministic code builds and sends the payload. An LLM must never retype the JSON.
- Encode/decode lives in one helper module (`surveyops/a2a_utils.py`).