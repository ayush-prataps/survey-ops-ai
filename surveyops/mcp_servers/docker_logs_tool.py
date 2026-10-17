import os
import re
import time
import unicodedata
from typing import Any

from .schemas import FetchLogsReturn

ALLOWED_SERVICES = {"orders", "payment", "inventory", "auth"}
CONTAINER_NAMES = {
    "orders": "target_orders",
    "payment": "target_payment",
    "inventory": "target_inventory",
    "auth": "target_auth",
}

# Caps from env
MAX_LOGS_SINCE_S = int(os.environ.get("MAX_LOGS_SINCE_S", "600"))
MAX_LOGS_LIMIT = int(os.environ.get("MAX_LOGS_LIMIT", "100"))
MAX_LOGS_BYTES = int(os.environ.get("MAX_LOGS_BYTES", "50000"))
MAX_LINE_LENGTH = 1000

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _sanitize_line(line: str) -> str:
    """Strip ANSI sequences and control characters (except newline/tab); truncate line."""
    no_ansi = ANSI_ESCAPE_RE.sub("", line)
    clean = "".join(
        ch for ch in no_ansi
        if ch in ("\n", "\t") or unicodedata.category(ch)[0] != "C"
    )
    if len(clean) > MAX_LINE_LENGTH:
        return clean[:MAX_LINE_LENGTH] + "... [TRUNCATED_LINE]"
    return clean


def execute_fetch_logs(
    service: str,
    since_s: int = 300,
    limit: int = 50,
) -> FetchLogsReturn:
    """
    Fetches and sanitizes JSON logs from Docker for an allowlisted target service.
    Rejects any service not explicitly allowlisted.
    Enforces line length and total byte caps.
    Wraps log text in untrusted delimiters.
    """
    if service not in ALLOWED_SERVICES:
        raise ValueError(
            f"Disallowed log service '{service}'. Allowed services: {sorted(ALLOWED_SERVICES)}"
        )

    container_name = CONTAINER_NAMES[service]
    since_s = min(max(int(since_s), 1), MAX_LOGS_SINCE_S)
    limit = min(max(int(limit), 1), MAX_LOGS_LIMIT)

    try:
        import docker
        docker_host = os.environ.get("DOCKER_HOST")
        if docker_host:
            client = docker.DockerClient(base_url=docker_host)
        else:
            client = docker.from_env()
        container = client.containers.get(container_name)
        since_ts = int(time.time() - since_s)
        raw_logs_bytes = container.logs(since=since_ts, tail=limit)
        raw_logs = raw_logs_bytes.decode(errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch docker logs for {service} ({container_name}): {exc}")

    # Sanitize and truncate
    raw_lines = raw_logs.splitlines()
    sanitized_lines: list[str] = []
    total_bytes = 0
    truncated = False

    for line in raw_lines:
        clean = _sanitize_line(line)
        line_bytes = len(clean.encode("utf-8"))
        if total_bytes + line_bytes > MAX_LOGS_BYTES:
            sanitized_lines.append("... [LOG_OUTPUT_TRUNCATED_DUE_TO_SIZE_CAP]")
            truncated = True
            break
        sanitized_lines.append(clean)
        total_bytes += line_bytes

    formatted_body = "\n".join(sanitized_lines)

    # Delimit content so LLMs treat it strictly as untrusted data
    delimited_logs = (
        f'<UNTRUSTED_SERVICE_LOGS service="{service}" lines="{len(sanitized_lines)}" truncated="{truncated}">\n'
        f"{formatted_body}\n"
        f"</UNTRUSTED_SERVICE_LOGS>"
    )

    return FetchLogsReturn(
        service=service,
        lines_count=len(sanitized_lines),
        logs=delimited_logs,
        truncated=truncated,
        contains_untrusted_text=True,
    )
