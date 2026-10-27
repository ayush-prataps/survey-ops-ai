"""
Workspace manager for the Remediator agent.
Handles repository cloning, branch management, and git operations for surveyops-target-app.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("surveyops.remediator.workspace")

TOKEN_PATTERN = re.compile(r"(?:github_pat_|ghp_|gho_)[A-Za-z0-9_]+")


def redact_tokens(text: str) -> str:
    """Redacts GitHub personal access tokens matching github_pat_, ghp_, or gho_."""
    if not text:
        return text
    return TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)


def get_authenticated_repo_url(repo_url: Optional[str] = None, token: Optional[str] = None) -> str:
    """Builds an authenticated GitHub clone URL if token is available."""
    url = repo_url or os.getenv("TARGET_REPO_URL", "https://github.com/ayush-prataps/surveyops-target-app.git")
    gh_token = token or os.getenv("GITHUB_TOKEN")
    if gh_token and url.startswith("https://") and "@" not in url:
        return url.replace("https://", f"https://x-access-token:{gh_token}@")
    return url


def resolve_target_repo_dir(workspace_root: Optional[Path] = None) -> Path:
    """
    Resolves or prepares the target application repository directory.
    Priority:
    1. TARGET_REPO_DIR environment variable (if exists)
    2. Sibling directory ../surveyops-target-app (if exists)
    3. Cloned repo in workspace volume /tmp/surveyops_workspace/target_repo or {workspace_root}/target_repo
    """
    env_dir = os.getenv("TARGET_REPO_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists() and (p / ".git").exists():
            return p

    # Check sibling repository in dev environment
    sibling = Path(__file__).resolve().parent.parent.parent.parent.parent / "surveyops-target-app"
    if sibling.exists() and (sibling / ".git").exists():
        return sibling

    # Workspace volume
    base_dir = workspace_root or Path(os.getenv("WORKSPACE_DIR", "/tmp/surveyops_workspace"))
    target_dir = base_dir / "target_repo"

    if not target_dir.exists() or not (target_dir / ".git").exists():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        auth_url = get_authenticated_repo_url()
        logger.info(f"Cloning target repository into workspace volume {target_dir}...")
        res = subprocess.run(
            ["git", "clone", auth_url, str(target_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            clean_err = redact_tokens(res.stderr)
            logger.error(f"Failed to clone target repository: {clean_err}")
            raise RuntimeError(f"Failed to clone target repository: {clean_err}")

    return target_dir


def find_test_runner(repo_dir: Path) -> list[str]:
    """Finds appropriate test runner executable inside target repository or environment."""
    # Check local virtual environment within target repo
    venv_pytest = repo_dir / ".venv" / "bin" / "pytest"
    if venv_pytest.exists() and os.access(venv_pytest, os.X_OK):
        return [str(venv_pytest)]

    alt_venv_pytest = repo_dir / "venv" / "bin" / "pytest"
    if alt_venv_pytest.exists() and os.access(alt_venv_pytest, os.X_OK):
        return [str(alt_venv_pytest)]

    # Fall back to pytest on PATH or python -m pytest
    custom_pytest = os.getenv("TARGET_PYTEST_BIN")
    if custom_pytest and Path(custom_pytest).exists():
        return [custom_pytest]

    return ["pytest"]


def create_fix_branch(repo_dir: Path, branch_name: str) -> None:
    """Creates and checks out a new fix branch from main."""
    # Ensure working tree is clean or discard unstaged edits
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, check=False)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_dir, capture_output=True, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir, capture_output=True, check=False)

    # Pull latest from origin main if remote is accessible
    subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=repo_dir, capture_output=True, check=False)

    # Create and checkout branch
    res = subprocess.run(["git", "checkout", "-B", branch_name], cwd=repo_dir, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create branch {branch_name}: {res.stderr}")
    logger.info(f"Checked out new branch {branch_name} in {repo_dir}")


def commit_and_push_changes(repo_dir: Path, branch_name: str, commit_message: str) -> None:
    """Stages changes, commits, and pushes branch to origin."""
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)

    # Configure identity if not present
    subprocess.run(
        ["git", "config", "user.name", "SurveyOps Remediator"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.email", "surveyops-agent@users.noreply.github.com"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )

    commit_res = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_res.returncode != 0 and "nothing to commit" not in commit_res.stdout:
        raise RuntimeError(f"Git commit failed: {commit_res.stderr or commit_res.stdout}")

    # Set up remote with token if needed
    auth_url = get_authenticated_repo_url()
    subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=repo_dir, capture_output=True, check=False)

    push_res = subprocess.run(
        ["git", "push", "-u", "origin", branch_name, "--force"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if push_res.returncode != 0:
        clean_push_err = redact_tokens(push_res.stderr)
        raise RuntimeError(f"Git push failed for {branch_name}: {clean_push_err}")
    logger.info(f"Pushed branch {branch_name} to origin.")


def delete_branch_cleanly(repo_dir: Path, branch_name: str, remote: bool = True) -> None:
    """Checks out main, deletes local branch, and optionally deletes remote branch."""
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, check=False)
    subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir, capture_output=True, check=False)
    subprocess.run(["git", "branch", "-D", branch_name], cwd=repo_dir, capture_output=True, check=False)

    if remote:
        auth_url = get_authenticated_repo_url()
        subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=repo_dir, capture_output=True, check=False)
        subprocess.run(["git", "push", "origin", "--delete", branch_name], cwd=repo_dir, capture_output=True, check=False)
    logger.info(f"Cleaned up branch {branch_name}")
