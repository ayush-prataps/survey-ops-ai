"""
Unified FastMCP Server for Experiment Runner.
Exposes:
- prometheus_query
- fetch_logs
- run_load_burst

Validates every tool return into a Pydantic model before returning.
"""

from mcp.server.fastmcp import FastMCP

from .schemas import PrometheusQueryReturn, FetchLogsReturn, LoadBurstReturn
from .prometheus_tool import execute_prometheus_query
from .docker_logs_tool import execute_fetch_logs
from .load_burst_tool import execute_run_load_burst_async

mcp_server = FastMCP(
    "surveyops-runner-tools",
    instructions="MCP tools for SurveyOps Experiment Runner. Provides Prometheus queries, Docker logs, and synthetic load bursts.",
)


@mcp_server.tool(name="prometheus_query")
def prometheus_query(
    template: str,
    service: str | None = None,
    endpoint: str | None = None,
    range_s: int = 300,
    window_start: str | None = None,
) -> dict:
    """
    Executes a vetted named Prometheus query template.
    Allowed templates: p99_latency, error_rate, request_rate, pool_in_use, pool_capacity.
    """
    result: PrometheusQueryReturn = execute_prometheus_query(
        template=template,
        service=service,
        endpoint=endpoint,
        range_s=range_s,
        window_start=window_start,
    )
    return result.model_dump()


@mcp_server.tool(name="fetch_logs")
def fetch_logs(
    service: str,
    since_s: int = 300,
    limit: int = 50,
    window_start: str | None = None,
) -> dict:
    """
    Fetches and sanitizes Docker container stdout logs for an allowlisted service.
    Allowed services: orders, payment, inventory, auth.
    Output is wrapped in untrusted data delimiters and contains_untrusted_text is set to True.
    """
    result: FetchLogsReturn = execute_fetch_logs(
        service=service,
        since_s=since_s,
        limit=limit,
        window_start=window_start,
    )
    return result.model_dump()


@mcp_server.tool(name="run_load_burst")
async def run_load_burst(
    endpoint: str,
    concurrency: int = 5,
    duration_s: int = 5,
) -> dict:
    """
    Runs a bounded synthetic load burst against an allowlisted target app endpoint.
    Concurrency and duration are strictly capped.
    Returns latency percentiles (p50, p95, p99) and request counts.
    """
    result: LoadBurstReturn = await execute_run_load_burst_async(
        endpoint=endpoint,
        concurrency=concurrency,
        duration_s=duration_s,
    )
    return result.model_dump()


if __name__ == "__main__":
    mcp_server.run()
