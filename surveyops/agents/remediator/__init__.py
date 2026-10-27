"""
Remediator agent package (CrewAI).
Autonomous agent that diagnoses root causes, localizes files, proposes patches and regression tests,
and opens GitHub PRs without human intervention (never merges).
"""

try:
    from surveyops.agents.remediator.agent import run_remediation, build_remediator_agent
    from surveyops.agents.remediator.server import create_remediator_app
except ImportError:
    pass
from surveyops.agents.remediator.schemas import (
    PatchProposal,
    RepoReadResult,
    PatchResult,
    TestRunResult,
    PRResult,
    LocalizationResult,
)

__all__ = [
    "run_remediation",
    "build_remediator_agent",
    "create_remediator_app",
    "PatchProposal",
    "RepoReadResult",
    "PatchResult",
    "TestRunResult",
    "PRResult",
    "LocalizationResult",
]
