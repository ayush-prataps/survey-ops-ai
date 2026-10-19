from typing import Any
from pydantic import BaseModel, Field


class PrometheusQueryReturn(BaseModel):
    template: str
    service: str | None = None
    endpoint: str | None = None
    range_s: int
    promql: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    request_rate: float | None = None
    request_count: float | None = None
    pool_in_use: float | None = None
    pool_max_in_use: float | None = None
    pool_capacity: float | None = None
    pool_utilization_ratio: float | None = None
    status: str = "success"


class FetchLogsReturn(BaseModel):
    service: str
    lines_count: int
    logs: str
    truncated: bool = False
    contains_untrusted_text: bool = True


class LoadBurstReturn(BaseModel):
    endpoint: str
    concurrency: int
    duration_s: int
    total_requests: int
    successful_requests: int
    error_requests: int
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
