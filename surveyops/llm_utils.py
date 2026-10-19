"""
Reusable Structured Output Adapter for SurveyOps.
Provides:
1. Tool-Call Mode for Google ADK agents:
   - Dynamic submission tools carrying Pydantic models (validated arguments).
   - Session state storage.
   - Plain-text answer detection treated as a retryable error.
2. JSON Mode for CrewAI agents (and direct LLM completion):
   - Code fence stripping and robust JSON extraction.
   - Pydantic model validation (`model_validate_json`).
   - One-shot retry mechanism feeding validation errors back to the model.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Callable, Optional, TypeVar
from pydantic import BaseModel, ValidationError

try:
    from google.adk.tools import ToolContext
except ImportError:
    ToolContext = Any  # type: ignore

logger = logging.getLogger("surveyops.llm_utils")

T = TypeVar("T", bound=BaseModel)


# ------------------------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------------------------
class StructuredOutputError(Exception):
    """Base error for structured output failures."""
    pass


class PlainTextReplyError(StructuredOutputError):
    """Raised when an agent replies with plain text instead of invoking the required submission tool."""
    pass


class SchemaValidationError(StructuredOutputError):
    """Raised when output fails Pydantic schema validation."""
    pass


# ------------------------------------------------------------------------------
# JSON Utilities
# ------------------------------------------------------------------------------
def extract_json_from_text(text: str) -> str:
    """
    Extracts raw JSON string from text, stripping Markdown code fences and surrounding text.
    Handles ```json ... ```, ``` ... ```, and un-fenced JSON objects or arrays.
    """
    if not text or not text.strip():
        raise StructuredOutputError("Cannot extract JSON from empty text.")

    trimmed = text.strip()

    # 1. Match code block ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", trimmed, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()

    # 2. Match outermost JSON object { ... }
    obj_match = re.search(r"(\{[\s\S]*\})", trimmed)
    if obj_match:
        candidate = obj_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 3. Match outermost JSON array [ ... ]
    arr_match = re.search(r"(\[[\s\S]*\])", trimmed)
    if arr_match:
        candidate = arr_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return trimmed


def parse_structured_json(text: str, model_cls: type[T]) -> T:
    """
    Strips code fences, parses JSON, and validates against the provided Pydantic model.
    Raises SchemaValidationError on syntax or validation errors.
    """
    cleaned = extract_json_from_text(text)
    try:
        return model_cls.model_validate_json(cleaned)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"Failed to validate {model_cls.__name__}: {exc}") from exc


# ------------------------------------------------------------------------------
# (a) Tool-Call Mode for Google ADK Agents
# ------------------------------------------------------------------------------
def build_adk_submission_tool(
    tool_name: str,
    description: str,
    model_cls: type[T],
    state_key: str,
    validator: Optional[Callable[[T, Optional[ToolContext]], Optional[str]]] = None,
) -> Callable[..., str]:
    """
    Creates an ADK tool function that dynamically exposes parameters matching model_cls.
    When invoked by the LLM, arguments are validated into an instance of model_cls and
    recorded directly into tool_context.session.state[state_key].
    ToolContext is injected automatically by ADK and omitted from the function schema.
    If a validator is provided, it is invoked with (validated_model, tool_context).
    If it returns an error string, the tool returns that error string so the LLM can correct and retry.
    """

    def tool_func(*args: Any, tool_context: Optional[ToolContext] = None, **kwargs: Any) -> str:
        data: dict[str, Any] = dict(kwargs)
        if args and len(args) == 1 and isinstance(args[0], dict):
            data.update(args[0])

        try:
            validated = model_cls.model_validate(data)
        except ValidationError as exc:
            logger.warning(f"Tool {tool_name} validation error: {exc}")
            return f"Validation error: {exc}. Please correct the parameters and retry."

        if validator is not None:
            val_err = validator(validated, tool_context)
            if val_err:
                logger.warning(f"Tool {tool_name} domain validation rejection: {val_err}")
                return f"Validation rejected: {val_err}. Please correct the parameters and call {tool_name} again."

        if tool_context and hasattr(tool_context, "session") and tool_context.session:
            tool_context.session.state[state_key] = validated
        if tool_context and hasattr(tool_context, "state") and tool_context.state is not None:
            tool_context.state[state_key] = validated

        logger.info(f"Tool {tool_name} successfully recorded {model_cls.__name__} in state['{state_key}'].")
        return f"Successfully validated and recorded {model_cls.__name__}."

    # Build dynamic function signature preserving model_cls field annotations
    req_params = []
    def_params = []
    for name, info in model_cls.model_fields.items():
        if info.is_required():
            req_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=info.annotation,
                )
            )
        else:
            def_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=info.annotation,
                    default=info.default,
                )
            )

    params = (
        req_params
        + def_params
        + [
            inspect.Parameter(
                name="tool_context",
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=ToolContext,
                default=None,
            )
        ]
    )

    tool_func.__signature__ = inspect.Signature(params)
    tool_func.__name__ = tool_name
    tool_func.__doc__ = (
        f"{description}\n"
        f"You must invoke this tool with arguments matching {model_cls.__name__}."
    )
    return tool_func


def extract_adk_structured_output(
    state: dict[str, Any],
    state_key: str,
    model_cls: type[T],
    last_text_reply: Optional[str] = None,
) -> T:
    """
    Extracts and validates the structured model from ADK session state.
    If the state key is missing or None, treats plain-text replies as a retryable error.
    """
    val = state.get(state_key)
    if val is not None:
        if isinstance(val, model_cls):
            return val
        if isinstance(val, dict):
            try:
                return model_cls.model_validate(val)
            except ValidationError as exc:
                raise SchemaValidationError(f"Invalid dict in state['{state_key}']: {exc}") from exc

    # If tool wasn't called but the model replied with valid JSON in text, attempt to recover it
    if last_text_reply and last_text_reply.strip():
        try:
            parsed = parse_structured_json(last_text_reply, model_cls)
            state[state_key] = parsed
            return parsed
        except Exception:
            pass

    err_detail = f" Plain text response was: '{last_text_reply}'" if last_text_reply else ""
    raise PlainTextReplyError(
        f"Agent did not invoke the submission tool to store '{state_key}' ({model_cls.__name__}).{err_detail}"
    )


# ------------------------------------------------------------------------------
# (b) JSON Mode for CrewAI Agents & Direct LLM Completions (with 1 Retry)
# ------------------------------------------------------------------------------
def execute_with_json_retry(
    execute_fn: Callable[[str], str],
    prompt: str,
    model_cls: type[T],
    max_retries: int = 1,
) -> T:
    """
    Executes an LLM or CrewAI task expecting JSON.
    If Pydantic validation fails, executes up to max_retries retry attempts feeding
    back the specific validation error to the model.
    """
    raw_output = execute_fn(prompt)
    try:
        return parse_structured_json(raw_output, model_cls)
    except SchemaValidationError as first_err:
        if max_retries <= 0:
            raise

        logger.warning(f"First-try JSON validation failed for {model_cls.__name__}: {first_err}. Retrying once...")
        retry_prompt = (
            f"{prompt}\n\n"
            f"[SYSTEM FEEDBACK]: Your previous response failed schema validation for {model_cls.__name__}.\n"
            f"Validation Error: {first_err}\n"
            f"Please fix the error and output ONLY valid JSON matching the schema. Do not include markdown preamble."
        )
        retry_output = execute_fn(retry_prompt)
        try:
            return parse_structured_json(retry_output, model_cls)
        except SchemaValidationError as retry_err:
            logger.error(f"Retry also failed for {model_cls.__name__}: {retry_err}")
            raise


async def async_execute_with_json_retry(
    async_execute_fn: Callable[[str], Any],
    prompt: str,
    model_cls: type[T],
    max_retries: int = 1,
) -> T:
    """Async variant of execute_with_json_retry."""
    raw_output = await async_execute_fn(prompt)
    if hasattr(raw_output, "raw"):
        raw_text = raw_output.raw
    elif isinstance(raw_output, str):
        raw_text = raw_output
    else:
        raw_text = str(raw_output)

    try:
        return parse_structured_json(raw_text, model_cls)
    except SchemaValidationError as first_err:
        if max_retries <= 0:
            raise

        logger.warning(f"First-try JSON validation failed for {model_cls.__name__}: {first_err}. Retrying once...")
        retry_prompt = (
            f"{prompt}\n\n"
            f"[SYSTEM FEEDBACK]: Your previous response failed schema validation for {model_cls.__name__}.\n"
            f"Validation Error: {first_err}\n"
            f"Please fix the error and output ONLY valid JSON matching the schema. Do not include markdown preamble."
        )
        retry_res = await async_execute_fn(retry_prompt)
        retry_text = retry_res.raw if hasattr(retry_res, "raw") else str(retry_res)
        try:
            return parse_structured_json(retry_text, model_cls)
        except SchemaValidationError as retry_err:
            logger.error(f"Retry also failed for {model_cls.__name__}: {retry_err}")
            raise
