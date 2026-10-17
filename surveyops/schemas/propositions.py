from pydantic import BaseModel
from .failure_modes import FailureMode


class Proposition(BaseModel):
    """Surface Mapper output. UNRANKED, no confidence."""
    proposition_id: str
    endpoint: str
    failure_mode: FailureMode
    justification: str
    predicted_signal: str
    graph_edge_ref: str
