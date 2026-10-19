"""
A2A Transport Utilities (from docs/04-CONTRACTS.md):
- Payloads travel as TextPart(model.model_dump_json()).
- Every receiver decodes and validates into a Pydantic model.
- Validation failures return typed error results, never crashing or raising uncaught exceptions.
"""

from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from pydantic import BaseModel, ValidationError
from a2a.types import Message, TextPart

T = TypeVar("T", bound=BaseModel)


@dataclass
class A2ADecodeResult(Generic[T]):
    """Typed result of decoding an A2A message into a Pydantic model."""
    value: T | None = None
    error: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.value is not None and self.error is None


def to_a2a_text_part(model: BaseModel) -> TextPart:
    """Encode any Pydantic model into an A2A TextPart carrying JSON."""
    return TextPart(text=model.model_dump_json())


def from_a2a_text(text: str, model_cls: type[T]) -> A2ADecodeResult[T]:
    """Decode and validate a JSON string into model_cls without raising unhandled ValidationError."""
    if not text or not text.strip():
        return A2ADecodeResult(error=f"Empty text payload for {model_cls.__name__}")
    try:
        from surveyops.llm_utils import extract_json_from_text
        cleaned = extract_json_from_text(text)
        val = model_cls.model_validate_json(cleaned)
        return A2ADecodeResult(value=val)
    except (ValidationError, Exception) as exc:
        return A2ADecodeResult(error=f"A2A validation error for {model_cls.__name__}: {exc}")


def from_a2a_message(message: Any, model_cls: type[T]) -> A2ADecodeResult[T]:
    """
    Extract payload from an A2A Message (or TextPart, raw string, dict) and validate into model_cls.
    Returns A2ADecodeResult containing either the validated model or a typed error description.
    Never raises uncaught exceptions.
    """
    if message is None:
        return A2ADecodeResult(error=f"Cannot decode {model_cls.__name__} from None message")

    if isinstance(message, str):
        return from_a2a_text(message, model_cls)

    if isinstance(message, TextPart):
        return from_a2a_text(message.text, model_cls)

    if isinstance(message, dict):
        if "parts" in message and isinstance(message["parts"], list):
            for part in message["parts"]:
                if isinstance(part, dict):
                    if "text" in part and part["text"]:
                        res = from_a2a_text(part["text"], model_cls)
                        if res.is_ok:
                            return res
                    if "data" in part and part["data"] is not None:
                        try:
                            return A2ADecodeResult(value=model_cls.model_validate(part["data"]))
                        except (ValidationError, Exception) as exc:
                            return A2ADecodeResult(error=f"A2A validation error for {model_cls.__name__}: {exc}")
        try:
            return A2ADecodeResult(value=model_cls.model_validate(message))
        except (ValidationError, Exception) as exc:
            return A2ADecodeResult(error=f"A2A validation error for {model_cls.__name__}: {exc}")

    # Inspect Message object parts
    parts = getattr(message, "parts", None)
    if parts:
        for part in parts:
            # Direct TextPart
            if hasattr(part, "text") and part.text:
                return from_a2a_text(part.text, model_cls)
            # RootModel wrapper around TextPart or DataPart
            root = getattr(part, "root", None)
            if root is not None:
                if hasattr(root, "text") and root.text:
                    return from_a2a_text(root.text, model_cls)
                if hasattr(root, "data") and root.data is not None:
                    try:
                        return A2ADecodeResult(value=model_cls.model_validate(root.data))
                    except (ValidationError, Exception) as exc:
                        return A2ADecodeResult(error=f"A2A validation error for {model_cls.__name__}: {exc}")

    return A2ADecodeResult(error=f"No compatible parts found in A2A message for {model_cls.__name__}")
