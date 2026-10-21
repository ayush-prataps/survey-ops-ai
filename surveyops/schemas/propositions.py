from typing import Literal, Optional
from pydantic import BaseModel
from .failure_modes import FailureMode


class CheckableSignal(BaseModel):
    """
    Machine-checkable signal per proposition.
    Evaluated deterministically against cited Observations in the exit gate.
    """
    type: Literal["metric", "log_pattern"]
    metric_name: Optional[str] = None
    comparator: Optional[Literal[">", ">=", "<", "<=", "=="]] = None
    threshold: Optional[float] = None
    log_pattern: Optional[str] = None


class Proposition(BaseModel):
    """Surface Mapper output. UNRANKED, no confidence."""
    proposition_id: str
    endpoint: str
    failure_mode: FailureMode
    justification: str
    predicted_signal: str
    graph_edge_ref: str
    checkable_signal: Optional[CheckableSignal] = None
