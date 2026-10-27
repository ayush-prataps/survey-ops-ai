"""
FastMCP Server for SurveyOps Remediator Tools.
Exposes:
- repo_read: Reads repository file with path allowlisting and untrusted comment delimitation
- apply_patch: Applies atomic patch with AST syntax validation
- write_test_file: Writes regression test file under tests/ with AST syntax validation
- run_tests: Executes pytest with sandboxing, timeout, and output parsing
- github_open_pr: Pushes fix branch to origin and opens GitHub PR (never merges)

Every tool return is validated into a Pydantic model before returning structured output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
import anyio
from mcp import ClientSession
from mcp.server.fastmcp import FastMCP

from surveyops.agents.remediator.schemas import (
    PatchResult,
    PRResult,
    RepoReadResult,
    TestRunResult,
)
from surveyops.agents.remediator.tools import (
    apply_patch as execute_apply_patch,
    github_open_pr as execute_github_open_pr,
    repo_read as execute_repo_read,
    run_tests as execute_run_tests,
    write_test_file as execute_write_test_file,
)

remediator_mcp_server = FastMCP(
    "surveyops-remediator-tools",
    instructions=(
        "MCP tools for SurveyOps Remediator agent. "
        "Provides repo_read, apply_patch, write_test_file, run_tests, and github_open_pr."
    ),
)


@remediator_mcp_server.tool(name="repo_read")
def mcp_repo_read(
    file_path: str,
    repo_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Reads a file from the repository with path allowlisting and untrusted comment delimitation.
    Allowed paths: service source files and tests under orders/, payment/, inventory/, auth/, tests/, db/.
    Forbidden paths: .github/, Dockerfiles, compose files, .env, secrets.
    """
    result: RepoReadResult = execute_repo_read(repo_dir=repo_dir, file_path=file_path)
    return result.model_dump()


@remediator_mcp_server.tool(name="apply_patch")
def mcp_apply_patch(
    file_path: str,
    original_snippet: str,
    replacement_snippet: str,
    repo_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Replaces original_snippet with replacement_snippet in file_path.
    Validates path allowlist and verifies Python syntax with AST before writing to disk.
    """
    result: PatchResult = execute_apply_patch(
        repo_dir=repo_dir,
        file_path=file_path,
        original_snippet=original_snippet,
        replacement_snippet=replacement_snippet,
    )
    return result.model_dump()


@remediator_mcp_server.tool(name="write_test_file")
def mcp_write_test_file(
    test_file_path: str,
    test_code: str,
    repo_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Writes a new regression test file to the repo under tests/.
    Validates that path is within tests/ and verifies Python syntax with AST before writing.
    """
    result: PatchResult = execute_write_test_file(
        repo_dir=repo_dir,
        test_file_path=test_file_path,
        test_code=test_code,
    )
    return result.model_dump()


@remediator_mcp_server.tool(name="run_tests")
def mcp_run_tests(
    test_file: Optional[str] = None,
    repo_dir: Optional[str] = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    Runs pytest in the target repository using sandboxed subprocess with timeout.
    Returns structured TestRunResult with pass/fail status, counts, and output.
    """
    result: TestRunResult = execute_run_tests(
        repo_dir=repo_dir,
        test_file=test_file,
        timeout_seconds=timeout_seconds,
    )
    return result.model_dump()


@remediator_mcp_server.tool(name="github_open_pr")
def mcp_github_open_pr(
    branch_name: str,
    title: str,
    body: str,
    base_branch: str = "main",
    repo_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Pushes the fix branch to GitHub and opens a pull request via the GitHub REST API.
    NEVER merges any pull request. Redacts tokens in errors and logs.
    """
    result: PRResult = execute_github_open_pr(
        repo_dir=repo_dir,
        branch_name=branch_name,
        title=title,
        body=body,
        base_branch=base_branch,
    )
    return result.model_dump()


async def _call_remediator_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Executes a Remediator tool call through an official MCP ClientSession over an in-memory stream
    connected to the remediator FastMCP server.
    """
    client_send, server_recv = anyio.create_memory_object_stream(20)
    server_send, client_recv = anyio.create_memory_object_stream(20)

    result_dict: dict[str, Any] = {}
    err_to_raise: Optional[RuntimeError] = None

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            remediator_mcp_server._mcp_server.run,
            server_recv,
            server_send,
            remediator_mcp_server._mcp_server.create_initialization_options(),
        )
        async with ClientSession(client_recv, client_send) as session:
            await session.initialize()
            mcp_res = await session.call_tool(tool_name, arguments=arguments)
            if mcp_res.isError:
                err_text = mcp_res.content[0].text if mcp_res.content else "Unknown MCP error"
                err_to_raise = RuntimeError(f"MCP tool '{tool_name}' returned error: {err_text}")
            else:
                raw_text = mcp_res.content[0].text if mcp_res.content else "{}"
                result_dict = json.loads(raw_text)
        tg.cancel_scope.cancel()

    if err_to_raise:
        raise err_to_raise

    return result_dict



def execute_remediator_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Synchronous helper to execute an MCP tool on remediator_mcp_server over in-memory ClientSession.
    """
    return anyio.run(_call_remediator_mcp_tool, tool_name, arguments)


if __name__ == "__main__":
    remediator_mcp_server.run()
