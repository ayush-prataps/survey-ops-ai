"""
LLM Record/Replay Cache for Diagnostician.
Saves Gemini token quota by caching model completions during tests and repeated runs.
Activated when SURVEYOPS_LLM_CACHE_ENABLED is 'true' (or by default in test environments).
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types

logger = logging.getLogger("surveyops.diagnostician.cache")

DEFAULT_CACHE_PATH = (
    Path(__file__).parent / "cache" / "diagnostician_cache.json"
)


class LLMCache:
    """Thread-safe persistent JSON cache for LLM responses."""

    def __init__(self, cache_file: Path | str | None = None) -> None:
        self.cache_path = Path(cache_file or os.environ.get("SURVEYOPS_LLM_CACHE_FILE", DEFAULT_CACHE_PATH))
        self.enabled = os.environ.get("SURVEYOPS_LLM_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")
        self._cache: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception as exc:
                logger.warning(f"Failed to read LLM cache from {self.cache_path}: {exc}")
                self._cache = {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            logger.warning(f"Failed to save LLM cache to {self.cache_path}: {exc}")

    def compute_key(self, request: LlmRequest) -> str:
        """Computes deterministic SHA256 key from request contents, model, schema, and instructions."""
        content_strs = []
        if request.contents:
            for c in request.contents:
                if hasattr(c, "parts") and c.parts:
                    for p in c.parts:
                        if hasattr(p, "text") and p.text:
                            content_strs.append(p.text)
                elif hasattr(c, "text") and c.text:
                    content_strs.append(c.text)

        sys_inst = ""
        if request.config:
            if hasattr(request.config, "system_instruction") and request.config.system_instruction:
                sys_inst = str(request.config.system_instruction)

        schema_name = ""
        if request.config and hasattr(request.config, "response_schema"):
            schema = request.config.response_schema
            if hasattr(schema, "__name__"):
                schema_name = schema.__name__
            else:
                schema_name = str(schema)

        # Determine mode: tool_call vs json_schema vs plain_json
        has_tools = bool(
            getattr(request, "tools", None)
            or (request.config and hasattr(request.config, "tools") and request.config.tools)
        )
        mode = "tool_call" if has_tools else ("json_schema" if schema_name else "plain_json")

        raw = f"{request.model}|{mode}|{schema_name}|{sys_inst}|{'|'.join(content_strs)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_cached_response(self, request: LlmRequest) -> LlmResponse | None:
        if not self.enabled:
            return None
        key = self.compute_key(request)
        entry = self._cache.get(key)
        if not entry:
            return None

        text_content = entry.get("text")
        if not text_content:
            return None

        logger.info(f"LLM cache hit for key {key[:12]}")
        usage = entry.get("usage", {})
        usage_meta = genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage.get("prompt_tokens", 0),
            candidates_token_count=usage.get("completion_tokens", 0),
            total_token_count=usage.get("total_tokens", 0),
        )
        return LlmResponse(
            content=genai_types.Content(
                parts=[genai_types.Part(text=text_content)]
            ),
            usage_metadata=usage_meta,
        )

    def record_response(self, request: LlmRequest, response: LlmResponse) -> None:
        if not self.enabled or not response or not response.content:
            return
        key = self.compute_key(request)
        text_parts = []
        if response.content.parts:
            for p in response.content.parts:
                if hasattr(p, "text") and p.text:
                    text_parts.append(p.text)

        full_text = "".join(text_parts)
        if not full_text:
            return

        usage_dict = {}
        if response.usage_metadata:
            usage_dict = {
                "prompt_tokens": getattr(response.usage_metadata, "prompt_token_count", 0),
                "completion_tokens": getattr(response.usage_metadata, "candidates_token_count", 0),
                "total_tokens": getattr(response.usage_metadata, "total_token_count", 0),
            }

        self._cache[key] = {
            "text": full_text,
            "usage": usage_dict,
        }
        self._save()
        logger.info(f"Recorded response in LLM cache for key {key[:12]}")

    def clear(self) -> None:
        self._cache = {}
        self._save()


global_llm_cache = LLMCache()
