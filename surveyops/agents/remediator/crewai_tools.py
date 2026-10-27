"""
CrewAI Tool wrappers for Remediator MCP tools.
Enables CrewAI agents to call Remediator tools via the Model Context Protocol.
"""

from __future__ import annotations

from typing import Any, Optional
from crewai.tools import tool

from surveyops.mcp_servers.remediator_server import execute_remediator_mcp_tool


@tool("repo_read")
def mcp_tool_repo_read(file_path: str, repo_dir: Optional[str] = None) -> str:
    """Reads a file from the repository via MCP with path allowlisting and untrusted text delimitation."""
    args: dict[str, Any] = {"file_path": file_path}
    if repo_dir:
        args["repo_dir"] = repo_dir
    res = execute_remediator_mcp_tool("repo_read", args)
    return res.get("untrusted_delimited") or res.get("content", "")


@tool("apply_patch")
def mcp_tool_apply_patch(
    file_path: str,
    original_snippet: str,
    replacement_snippet: str,
    repo_dir: Optional[str] = None,
) -> str:
    """Replaces original_snippet with replacement_snippet in file_path via MCP with AST syntax validation."""
    args: dict[str, Any] = {
        "file_path": file_path,
        "original_snippet": original_snippet,
        "replacement_snippet": replacement_snippet,
    }
    if repo_dir:
        args["repo_dir"] = repo_dir
    res = execute_remediator_mcp_tool("apply_patch", args)
    return f"Success: {res.get('success')}. Syntax Valid: {res.get('syntax_valid')}. Error: {res.get('error')}"


@tool("write_test_file")
def mcp_tool_write_test_file(
    test_file_path: str,
    test_code: str,
    repo_dir: Optional[str] = None,
) -> str:
    """Writes a regression test file under tests/ via MCP with AST syntax validation."""
    args: dict[str, Any] = {
        "test_file_path": test_file_path,
        "test_code": test_code,
    }
    if repo_dir:
        args["repo_dir"] = repo_dir
    res = execute_remediator_mcp_tool("write_test_file", args)
    return f"Success: {res.get('success')}. Syntax Valid: {res.get('syntax_valid')}. Error: {res.get('error')}"


@tool("run_tests")
def mcp_tool_run_tests(test_file: Optional[str] = None, repo_dir: Optional[str] = None) -> str:
    """Runs pytest in the target repository via MCP and returns test outcome summary."""
    args: dict[str, Any] = {}
    if test_file:
        args["test_file"] = test_file
    if repo_dir:
        args["repo_dir"] = repo_dir
    res = execute_remediator_mcp_tool("run_tests", args)
    return f"Passed: {res.get('passed')}, Total: {res.get('total')}, Failed: {res.get('failed')}, Errors: {res.get('errors')}"


def get_remediator_crewai_tools() -> list[Any]:
    """Returns list of CrewAI tools wrapping Remediator MCP server tools."""
    return [mcp_tool_repo_read, mcp_tool_apply_patch, mcp_tool_write_test_file, mcp_tool_run_tests]
