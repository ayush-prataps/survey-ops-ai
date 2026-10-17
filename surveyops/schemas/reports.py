from typing import Any
from pydantic import BaseModel, Field
from .diagnosis import DiagnosisResult, LedgerEntry
from .remediation import RemediationResult


class IncidentReport(BaseModel):
    """Final structured output, rendered to Markdown by template."""
    incident_id: str
    diagnosis: DiagnosisResult
    evidence_trail: list[LedgerEntry] = Field(default_factory=list)
    remediation: RemediationResult | None = None
    timings: dict[str, Any] = Field(default_factory=dict)  # detect, diagnose, pr (seconds)
    usage: dict[str, Any] = Field(default_factory=dict)    # tokens, notional cost
