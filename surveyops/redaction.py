"""
Shared token-redaction helper (requirement 8: never log/report GitHub PATs).

Lives outside surveyops.agents.remediator because that package's __init__.py
eagerly imports agent.py (CrewAI), and crewai is not installed in every
process that needs to redact text (e.g. surveyops-api's deterministic report
renderer, which runs ADK-only and never touches the Remediator).
"""

from __future__ import annotations

import re

TOKEN_PATTERN = re.compile(r"(?:github_pat_|ghp_|gho_)[A-Za-z0-9_]+")


def redact_tokens(text: str) -> str:
    """Redacts GitHub personal access tokens matching github_pat_, ghp_, or gho_."""
    if not text:
        return text
    return TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
