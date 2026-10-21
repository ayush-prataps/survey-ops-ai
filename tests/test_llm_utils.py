"""
Unit tests for SurveyOps LLM structured-output adapter (surveyops/llm_utils.py).
Tests:
- Code fence stripping & JSON extraction
- Successful Pydantic validation
- Malformed JSON handling & error raising
- Missing required fields
- ADK submission tool execution & state persistence
- Plain-text reply detection (PlainTextReplyError)
- JSON retry mechanism (first-try, retry recovery, exhausted retries)
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from surveyops.llm_utils import (
    PlainTextReplyError,
    SchemaValidationError,
    StructuredOutputError,
    build_adk_submission_tool,
    extract_adk_structured_output,
    extract_json_from_text,
    parse_structured_json,
    execute_with_json_retry,
)


class MockResult(BaseModel):
    id: str = Field(description="Unique ID")
    score: float = Field(description="Confidence score")
    summary: str = Field(description="Brief summary")


class MockToolContext:
    def __init__(self) -> None:
        self.state = {}
        self.session = self
        self.actions = SimpleNamespace(skip_summarization=False)

    def get(self, k, default=None):
        return self.state.get(k, default)


# ------------------------------------------------------------------------------
# 1. Extraction and JSON Parsing Tests
# ------------------------------------------------------------------------------
def test_extract_json_from_code_fences():
    fenced = '```json\n{"id": "test-1", "score": 0.9, "summary": "ok"}\n```'
    extracted = extract_json_from_text(fenced)
    assert extracted == '{"id": "test-1", "score": 0.9, "summary": "ok"}'


def test_extract_json_from_generic_fence():
    fenced = 'Here is the result:\n```\n{"id": "test-2", "score": 0.85, "summary": "done"}\n```\nHope that helps!'
    extracted = extract_json_from_text(fenced)
    assert extracted == '{"id": "test-2", "score": 0.85, "summary": "done"}'


def test_extract_json_empty_text():
    with pytest.raises(StructuredOutputError):
        extract_json_from_text("   \n  ")


def test_parse_structured_json_success():
    raw = '{"id": "test-3", "score": 0.95, "summary": "validated"}'
    model = parse_structured_json(raw, MockResult)
    assert model.id == "test-3"
    assert model.score == 0.95
    assert model.summary == "validated"


def test_parse_structured_json_malformed():
    raw = '{"id": "test-4", "score": 0.95, "summary": invalid_json_here...}'
    with pytest.raises(SchemaValidationError) as exc_info:
        parse_structured_json(raw, MockResult)
    assert "Failed to validate" in str(exc_info.value)


def test_parse_structured_json_missing_fields():
    # Missing required field 'summary'
    raw = '{"id": "test-5", "score": 0.7}'
    with pytest.raises(SchemaValidationError) as exc_info:
        parse_structured_json(raw, MockResult)
    assert "summary" in str(exc_info.value)


# ------------------------------------------------------------------------------
# 2. ADK Tool-Call Mode Tests
# ------------------------------------------------------------------------------
def test_adk_submission_tool_success():
    tool = build_adk_submission_tool(
        tool_name="submit_mock_result",
        description="Submits mock result",
        model_cls=MockResult,
        state_key="mock_key",
    )
    ctx = MockToolContext()
    res = tool(id="res-101", score=0.99, summary="Perfect run", tool_context=ctx)
    assert "Successfully validated" in res
    assert "mock_key" in ctx.state
    assert isinstance(ctx.state["mock_key"], MockResult)
    assert ctx.state["mock_key"].id == "res-101"
    assert ctx.actions.skip_summarization is True


def test_adk_submission_tool_validation_error():
    tool = build_adk_submission_tool(
        tool_name="submit_mock_result",
        description="Submits mock result",
        model_cls=MockResult,
        state_key="mock_key",
    )
    ctx = MockToolContext()
    # Missing 'summary' field
    res = tool(id="res-102", score=0.5, tool_context=ctx)
    assert "Validation error" in res
    assert "mock_key" not in ctx.state
    assert ctx.actions.skip_summarization is False


def test_adk_plain_text_reply_error():
    ctx = MockToolContext()
    # Agent did not call the submission tool, state is empty, plain text returned
    plain_reply = "I think the failure was due to timeout."
    with pytest.raises(PlainTextReplyError) as exc_info:
        extract_adk_structured_output(
            state=ctx.state,
            state_key="mock_key",
            model_cls=MockResult,
            last_text_reply=plain_reply,
        )
    assert "PlainTextReplyError" in exc_info.type.__name__
    assert "plain text" in str(exc_info.value).lower()
    assert plain_reply in str(exc_info.value)


def test_adk_extract_structured_output_success():
    ctx = MockToolContext()
    ctx.state["mock_key"] = MockResult(id="ok-1", score=1.0, summary="done")
    res = extract_adk_structured_output(ctx.state, "mock_key", MockResult)
    assert res.id == "ok-1"


def test_adk_submission_tool_custom_validator():
    def custom_val(res: MockResult, tool_context: MockToolContext) -> str | None:
        if res.score < 0.7:
            return "Score too low"
        return None

    tool = build_adk_submission_tool(
        tool_name="submit_mock",
        description="Submit mock",
        model_cls=MockResult,
        state_key="mock_key",
        validator=custom_val,
    )
    ctx = MockToolContext()
    # Fails custom validation
    msg = tool(id="test-val", score=0.5, summary="low score", tool_context=ctx)
    assert "Validation rejected: Score too low" in msg
    assert "mock_key" not in ctx.state
    assert ctx.actions.skip_summarization is False

    # Passes custom validation
    msg2 = tool(id="test-val", score=0.85, summary="high score", tool_context=ctx)
    assert "Successfully validated" in msg2
    assert "mock_key" in ctx.state
    assert ctx.state["mock_key"].score == 0.85
    assert ctx.actions.skip_summarization is True


# ------------------------------------------------------------------------------
# 3. JSON Retry Mode Tests
# ------------------------------------------------------------------------------
def test_execute_with_json_retry_first_try():
    calls = []

    def mock_exec(prompt: str) -> str:
        calls.append(prompt)
        return '```json\n{"id": "first-1", "score": 0.8, "summary": "first try success"}\n```'

    res = execute_with_json_retry(mock_exec, "Analyze something", MockResult)
    assert res.id == "first-1"
    assert len(calls) == 1


def test_execute_with_json_retry_recovers_on_retry():
    calls = []

    def mock_exec(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            # First try returns missing field 'summary'
            return '{"id": "retry-1", "score": 0.5}'
        # Second try fixes the error after feedback
        assert "[SYSTEM FEEDBACK]" in prompt
        return '{"id": "retry-1", "score": 0.9, "summary": "fixed on retry"}'

    res = execute_with_json_retry(mock_exec, "Initial prompt", MockResult, max_retries=1)
    assert res.id == "retry-1"
    assert res.summary == "fixed on retry"
    assert len(calls) == 2


def test_execute_with_json_retry_exhausted():
    calls = []

    def mock_exec(prompt: str) -> str:
        calls.append(prompt)
        # Keeps returning malformed JSON
        return "This is not valid json at all!"

    with pytest.raises(SchemaValidationError):
        execute_with_json_retry(mock_exec, "Initial prompt", MockResult, max_retries=1)
    assert len(calls) == 2
