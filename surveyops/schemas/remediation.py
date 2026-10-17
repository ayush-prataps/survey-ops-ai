from pydantic import BaseModel, Field


class RemediationResult(BaseModel):
    """Remediator output."""
    pr_url: str | None = None
    branch: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
