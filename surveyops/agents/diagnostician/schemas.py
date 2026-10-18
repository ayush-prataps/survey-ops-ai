from typing import Any
from pydantic import BaseModel, Field
from surveyops.schemas.failure_modes import FailureMode
from surveyops.schemas.experiments import ExperimentRequest, ExperimentType
from surveyops.schemas.diagnosis import LedgerEntry, Outcome, DiagnosisResult


class HypothesisItem(BaseModel):
    """A working failure hypothesis evaluated during the diagnostic loop."""
    hypothesis_id: str
    failure_mode: FailureMode
    description: str
    status: str = "active"  # "active" | "ruled_out" | "dominant"
    is_novel: bool = False
    justification: str | None = None
    supporting_proposition_id: str | None = None
    graph_edge_ref: str | None = None
    confidence: float = 0.50


class ExperimentParams(BaseModel):
    """Typed experiment parameters avoiding untyped dicts for Gemini schema compliance."""
    template: str | None = None
    service: str | None = None
    endpoint: str | None = None
    range_s: int | None = None
    since_s: int | None = None
    limit: int | None = None
    concurrency: int | None = None
    duration_s: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class ChosenExperiment(BaseModel):
    """Experiment selected by the LLM step, adhering to Gemini Developer API schema constraints."""
    request_id: str
    hypothesis_id: str
    experiment_type: ExperimentType
    params: ExperimentParams = Field(default_factory=ExperimentParams)
    rationale: str
    disconfirming_if: str

    def to_experiment_request(self) -> ExperimentRequest:
        return ExperimentRequest(
            request_id=self.request_id,
            hypothesis_id=self.hypothesis_id,
            experiment_type=self.experiment_type,
            params=self.params.to_dict(),
            rationale=self.rationale,
            disconfirming_if=self.disconfirming_if,
        )


class ExperimentStepOutput(BaseModel):
    """Output schema for the hypothesis generation & experiment selection LLM step."""
    hypotheses: list[HypothesisItem] = Field(
        ...,
        description="Updated list of candidate hypotheses. Must include at least one rival.",
    )
    chosen_experiment: ChosenExperiment = Field(
        ...,
        description="The ONE chosen experiment to execute against the target system.",
    )
    disconfirming_if: str = Field(
        ...,
        description="Falsification condition: what observation outcome would disprove the leading hypothesis.",
    )


class LedgerUpdateOutput(BaseModel):
    """Output schema for the ledger evaluation LLM step."""
    ledger_entries: list[LedgerEntry] = Field(
        ...,
        description="Updated ledger entries recording evidence_for, evidence_against, and confidence.",
    )
    llm_recommendation: str = Field(
        default="continue",
        description="Recommendation: 'continue', 'conclude', or 'abstain'. Code makes the final exit decision.",
    )
    reasoning: str = Field(
        ...,
        description="Concise rationale explaining the evidence synthesis and confidence adjustments.",
    )


class ExitDecision(BaseModel):
    """Deterministic exit decision determined by code, not LLM."""
    should_exit: bool
    outcome: Outcome
    top_hypothesis: HypothesisItem | None = None
    confidence: float | None = None
    reason: str
