"""
Tools for the Remediator agent.
Includes path allowlisting, untrusted text delimitation, patch application with AST validation,
sandboxed test execution, and GitHub PR creation (never merge).
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
import urllib.request
import json
from dotenv import load_dotenv

from surveyops.agents.remediator.schemas import (
    PatchResult,
    PRResult,
    RepoReadResult,
    TestRunResult,
)
from surveyops.agents.remediator.workspace import (
    commit_and_push_changes,
    find_test_runner,
    redact_tokens,
    resolve_target_repo_dir,
)

load_dotenv()

logger = logging.getLogger("surveyops.remediator.tools")

# Strict path allowlist: only service source files and tests are allowed
ALLOWED_PREFIXES = ("orders/", "payment/", "inventory/", "auth/", "tests/", "db/")
FORBIDDEN_PATTERNS = (
    r"\.github/",
    r"dockerfile",
    r"compose",
    r"\.env",
    r"secret",
    r"credential",
    r"id_rsa",
    r"\.git/",
    r"\.pytest_cache",
)


class ForbiddenPathError(Exception):
    """Raised when an operation attempts to access a path outside the allowlist."""
    pass


def validate_path_allowlist(repo_dir: Path | str | None, rel_path: str) -> Path:
    """
    Validates that rel_path is strictly within the allowed service/test directories
    and does not touch forbidden config/CI/secrets files or escape the repo directory.
    """
    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    cleaned = rel_path.strip().lstrip("/")
    lowered = cleaned.lower()

    # Reject path traversal
    if ".." in cleaned:
        raise ForbiddenPathError(f"Path traversal detected: {rel_path}")

    # Reject forbidden files/patterns
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered):
            raise ForbiddenPathError(f"Access to forbidden pattern '{pattern}' is denied: {rel_path}")

    # Check allowlist
    if not any(cleaned.startswith(pfx) for pfx in ALLOWED_PREFIXES):
        raise ForbiddenPathError(
            f"Path '{rel_path}' is not in the allowed service or test directories: {ALLOWED_PREFIXES}"
        )

    resolved = (target_repo / cleaned).resolve()
    repo_resolved = target_repo.resolve()
    if not str(resolved).startswith(str(repo_resolved)):
        raise ForbiddenPathError(f"Path escaped repository boundary: {rel_path}")

    return resolved


def delimit_untrusted_content(file_path: str, content: str) -> str:
    """
    Wraps source file content in untrusted delimiters to prevent code comments
    from executing prompt injection attacks against LLMs.
    """
    return (
        f"<<<BEGIN UNTRUSTED SOURCE FILE: {file_path}>>>\n"
        f"{content}\n"
        f"<<<END UNTRUSTED SOURCE FILE: {file_path}>>>"
    )


def repo_read(
    repo_dir: Path | str | None = None,
    file_path: str = "",
) -> RepoReadResult:
    """Reads a file from the repository with path allowlisting and untrusted text delimitation."""
    if not file_path and isinstance(repo_dir, str):
        file_path, repo_dir = repo_dir, None

    if not file_path:
        raise ValueError("file_path is required")

    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    target = validate_path_allowlist(target_repo, file_path)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = target.read_text(encoding="utf-8")
    line_count = len(content.splitlines())
    delimited = delimit_untrusted_content(file_path, content)

    return RepoReadResult(
        file_path=file_path,
        content=content,
        line_count=line_count,
        untrusted_delimited=delimited,
    )


def apply_patch(
    repo_dir: Path | str | None = None,
    file_path: str = "",
    original_snippet: str = "",
    replacement_snippet: str = "",
) -> PatchResult:
    """
    Replaces original_snippet with replacement_snippet in file_path.
    Validates that the resulting file is syntactically valid Python before writing to disk.
    """
    if not replacement_snippet and isinstance(repo_dir, str) and not Path(str(repo_dir)).is_dir():
        replacement_snippet, original_snippet, file_path, repo_dir = original_snippet, file_path, repo_dir, None

    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    target = validate_path_allowlist(target_repo, file_path)
    if not target.exists():
        return PatchResult(success=False, file_path=file_path, error=f"File not found: {file_path}")

    content = target.read_text(encoding="utf-8")
    if original_snippet not in content:
        return PatchResult(
            success=False,
            file_path=file_path,
            error=f"Original snippet not found in {file_path}",
        )

    patched_content = content.replace(original_snippet, replacement_snippet, 1)

    # Validate Python syntax
    try:
        ast.parse(patched_content, filename=str(target))
    except SyntaxError as syn_err:
        return PatchResult(
            success=False,
            file_path=file_path,
            syntax_valid=False,
            error=f"Syntax error in patched code: {syn_err}",
        )

    target.write_text(patched_content, encoding="utf-8")
    logger.info(f"Successfully applied patch to {file_path}")
    return PatchResult(success=True, file_path=file_path, syntax_valid=True)


def write_test_file(
    repo_dir: Path | str | None = None,
    test_file_path: str = "",
    test_code: str = "",
) -> PatchResult:
    """Writes a new regression test file to the repo under tests/."""
    if not test_code and isinstance(repo_dir, str) and not Path(str(repo_dir)).is_dir():
        test_code, test_file_path, repo_dir = test_file_path, repo_dir, None

    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    target = validate_path_allowlist(target_repo, test_file_path)
    if not test_file_path.startswith("tests/"):
        return PatchResult(
            success=False,
            file_path=test_file_path,
            error="Regression test file must be inside 'tests/' directory",
        )

    # Validate syntax
    try:
        ast.parse(test_code, filename=str(target))
    except SyntaxError as syn_err:
        return PatchResult(
            success=False,
            file_path=test_file_path,
            syntax_valid=False,
            error=f"Syntax error in test code: {syn_err}",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(test_code, encoding="utf-8")
    logger.info(f"Successfully wrote test file {test_file_path}")
    return PatchResult(success=True, file_path=test_file_path, syntax_valid=True)


def run_tests(
    repo_dir: Path | str | None = None,
    test_file: Optional[str] = None,
    timeout_seconds: int = 60,
) -> TestRunResult:
    """
    Runs pytest in the target repository using sandboxed subprocess with timeout.
    Returns structured TestRunResult.
    """
    if isinstance(repo_dir, str) and not Path(str(repo_dir)).is_dir() and test_file is None:
        test_file, repo_dir = repo_dir, None

    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    runner_cmd = find_test_runner(target_repo)
    cmd = list(runner_cmd)
    if test_file:
        cmd.append(test_file)
    else:
        cmd.append("tests")

    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=target_repo,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        duration = time.time() - start_time
        stdout = proc.stdout
        stderr = proc.stderr
        passed = proc.returncode == 0

        # Parse test counts from output
        # e.g. "21 passed, 1 failed in 0.15s" or "collected 5 items ... 5 passed"
        passed_count = 0
        failed_count = 0
        error_count = 0

        m_pass = re.search(r"(\d+)\s+passed", stdout)
        if m_pass:
            passed_count = int(m_pass.group(1))
        m_fail = re.search(r"(\d+)\s+failed", stdout)
        if m_fail:
            failed_count = int(m_fail.group(1))
        m_err = re.search(r"(\d+)\s+error", stdout)
        if m_err:
            error_count = int(m_err.group(1))

        total = passed_count + failed_count + error_count
        return TestRunResult(
            passed=passed,
            total=total,
            failed=failed_count,
            errors=error_count,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - start_time
        return TestRunResult(
            passed=False,
            total=0,
            failed=1,
            errors=1,
            duration_seconds=duration,
            stdout=exc.stdout or "",
            stderr=f"Test run timed out after {timeout_seconds} seconds",
        )
    except Exception as exc:
        duration = time.time() - start_time
        return TestRunResult(
            passed=False,
            total=0,
            failed=1,
            errors=1,
            duration_seconds=duration,
            stderr=str(exc),
        )


def github_open_pr(
    repo_dir: Path | str | None = None,
    branch_name: str = "",
    title: str = "",
    body: str = "",
    base_branch: str = "main",
) -> PRResult:
    """
    Pushes the fix branch to GitHub and opens a pull request via the GitHub REST API.
    NEVER merges any pull request.
    """
    if not body and isinstance(repo_dir, str) and not Path(str(repo_dir)).is_dir():
        body, title, branch_name, repo_dir = title, branch_name, repo_dir, None

    target_repo = Path(repo_dir) if repo_dir else resolve_target_repo_dir()
    token = os.getenv("GITHUB_TOKEN")
    repo_url = os.getenv("TARGET_REPO_URL", "https://github.com/ayush-prataps/surveyops-target-app.git")

    # Extract owner and repo from TARGET_REPO_URL
    # e.g. https://github.com/ayush-prataps/surveyops-target-app.git -> ayush-prataps/surveyops-target-app
    match = re.search(r"github\.com[/:]([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+?)(?:\.git)?$", repo_url)
    if not match:
        return PRResult(success=False, error=f"Could not parse owner and repo from {repo_url}")
    owner, repo_name = match.group(1), match.group(2)

    # First push branch to remote
    try:
        commit_and_push_changes(target_repo, branch_name, title)
    except Exception as exc:
        return PRResult(success=False, branch=branch_name, error=f"Failed to push branch: {exc}")

    # Now open PR via GitHub API
    if not token:
        return PRResult(success=False, branch=branch_name, error="GITHUB_TOKEN is missing")

    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "SurveyOps-Remediator",
    }
    payload = {
        "title": title,
        "head": branch_name,
        "base": base_branch,
        "body": body,
    }

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            pr_url = data.get("html_url")
            pr_num = data.get("number")
            logger.info(f"Successfully opened GitHub PR: {pr_url} (#{pr_num})")
            return PRResult(
                success=True,
                pr_url=pr_url,
                pr_number=pr_num,
                branch=branch_name,
            )
    except urllib.error.HTTPError as err:
        err_body = redact_tokens(err.read().decode("utf-8"))
        err_msg = f"GitHub API error {err.code} at {api_url}: {err_body}"
        logger.error(err_msg)
        return PRResult(
            success=False,
            branch=branch_name,
            error=err_msg,
        )
    except Exception as exc:
        clean_err = redact_tokens(str(exc))
        logger.error(f"Failed to open GitHub PR: {clean_err}")
        return PRResult(success=False, branch=branch_name, error=clean_err)


def execute_remediator_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Executes a Remediator tool via MCP ClientSession connected to remediator_mcp_server."""
    from surveyops.mcp_servers.remediator_server import execute_remediator_mcp_tool as _exec
    return _exec(tool_name, arguments)


async def call_remediator_mcp_tool_async(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Executes a Remediator tool asynchronously via MCP ClientSession."""
    from surveyops.mcp_servers.remediator_server import _call_remediator_mcp_tool as _call_async
    return await _call_async(tool_name, arguments)

