from datetime import datetime
from pydantic import BaseModel


class InvestigatePayload(BaseModel):
    """
    Alertmanager -> /investigate. SYMPTOM ONLY.
    NEVER add a root-cause or fault label here.
    """
    alert: str
    service: str
    endpoint: str
    metric: str
    observed: float | None = None
    threshold: float
    window_start: datetime
