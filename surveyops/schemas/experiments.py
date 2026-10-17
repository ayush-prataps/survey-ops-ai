from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class ExperimentType(str, Enum):
    query_metrics = "query_metrics"
    fetch_logs = "fetch_logs"
    load_burst = "load_burst"


class ExperimentRequest(BaseModel):
    """Diagnostician -> Runner (A2A)."""
    request_id: str
    hypothesis_id: str
    experiment_type: ExperimentType
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    disconfirming_if: str


class Observation(BaseModel):
    """Runner -> Diagnostician (A2A). RAW ONLY."""
    request_id: str
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime
    truncated: bool = False
    contains_untrusted_text: bool = False
