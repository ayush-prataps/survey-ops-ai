"""
Pydantic schemas for the Remediator (CrewAI).
Used for structured tool inputs/outputs, patch proposals, and localization results.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class PatchProposal(BaseModel):
    """
    LLM-proposed minimal patch and regression test.
    Validated in JSON mode via surveyops.llm_utils.
    """
    target_file: str = Field(..., description="Target file path relative to repo root (e.g. 'payment/main.py')")
    explanation: str = Field(..., description="Technical rationale for the patch explaining the fix")
    original_snippet: str = Field(..., description="Exact code snippet in the target file to be replaced")
    replacement_snippet: str = Field(..., description="New replacement code snippet")
    regression_test_file: str = Field(
        ...,
        description="Path to regression test file (e.g. 'tests/test_payment_pool_regression.py')"
    )
    regression_test_code: str = Field(
        ...,
        description="Deterministic pytest code that reproduces the fault on unpatched code and passes when patched"
    )


class RepoReadResult(BaseModel):
    """Result of reading a repository file."""
    file_path: str
    content: str
    line_count: int
    untrusted_delimited: str


class PatchResult(BaseModel):
    """Result of applying a patch."""
    success: bool
    file_path: str
    error: Optional[str] = None
    syntax_valid: bool = True


class TestRunResult(BaseModel):
    """Result of running tests in the target repository."""
    __test__ = False
    passed: bool
    total: int
    failed: int
    errors: int = 0
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""


class PRResult(BaseModel):
    """Result of creating a GitHub branch and opening a pull request."""
    success: bool
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    error: Optional[str] = None


class LocalizationResult(BaseModel):
    """Result of localizing the target file from graph edge and pgvector."""
    file_path: str
    signal_used: str  # "graph_edge_ref (confirmed by pgvector)", "graph_edge_ref", "pgvector semantic index"
    semantic_score: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
