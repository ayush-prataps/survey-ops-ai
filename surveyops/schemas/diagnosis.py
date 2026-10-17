from enum import Enum
from pydantic import BaseModel, Field
from .failure_modes import FailureMode


class LedgerEntry(BaseModel):
    iteration: int
    hypothesis_id: str
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    confidence: float
    status: str  # active | ruled_out | dominant


class Outcome(str, Enum):
    root_cause = "root_cause"
    budget_exhausted = "budget_exhausted"
    abstained = "abstained"


class DiagnosisResult(BaseModel):
    """Diagnostician -> Remediator (A2A)."""
    incident_id: str
    outcome: Outcome
    root_cause: FailureMode | None = None
    endpoint: str
    supporting_proposition_id: str | None = None
    graph_edge_ref: str | None = None
    confidence: float | None = None
    ruled_out: list[str] = Field(default_factory=list)
    iterations_used: int
