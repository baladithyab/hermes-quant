"""hermes_quant.agents.structured_output — provider-aware structured-output helpers.

ADR-0044: Wave 2 — provider-aware bind_structured + invoke_structured_or_freetext.

Design contract:
  - This module does NOT import any LLM SDK (openai, anthropic, google-ai).
    It constructs the kwargs/params dicts that callers pass to their SDK client.
    Tests mock the client entirely.
  - Provider routing is determined by model_id prefix (before the first '/').
  - Graceful fallback: on structured-output rejection or Pydantic validation
    failure, invoke_structured_or_freetext returns (None, raw_response) so
    callers can apply conservative defaults without crashing the pipeline.

Supported provider routing:
  Provider prefix   | Structured-output mechanism
  ──────────────────|──────────────────────────────
  openai/*          | response_format = {"type": "json_schema", ...}
  xai/*             | same as openai (Grok API is OAI-compatible)
  google/*          | response_schema (Gemini native structured output)
  anthropic/*       | tools[0] with tool_choice={"type": "any"} (forced)
  (unknown)         | free-text + manual json.loads() fallback

Usage example (illustrative — do NOT call in this module):
    from hermes_quant.agents.structured_output import bind_structured, invoke_structured_or_freetext
    from hermes_quant.agents.trader import TraderProposal

    kwargs = bind_structured("openai/gpt-4o", TraderProposal)
    # → {"response_format": {"type": "json_schema", "json_schema": {...}}}

    obj, raw = invoke_structured_or_freetext(
        client=my_openai_client,
        prompt=[{"role": "user", "content": "..."}],
        schema=TraderProposal,
        model_id="openai/gpt-4o",
    )
    if obj is None:
        # structured output failed; raw has the raw LLM text
        apply_conservative_defaults()
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

_OPENAI_PREFIXES = ("openai/", "xai/")
_GOOGLE_PREFIXES = ("google/",)
_ANTHROPIC_PREFIXES = ("anthropic/",)


def _detect_provider(model_id: str) -> str:
    """Return canonical provider name from model_id string.

    The hermes-quant convention is '<provider>/<model-name>' (OpenRouter
    style). We strip anything after the first '/' to get the provider.

    Returns one of: 'openai', 'google', 'anthropic', 'unknown'.
    """
    lower = (model_id or "").lower()
    for prefix in _OPENAI_PREFIXES:
        if lower.startswith(prefix):
            return "openai"
    for prefix in _GOOGLE_PREFIXES:
        if lower.startswith(prefix):
            return "google"
    for prefix in _ANTHROPIC_PREFIXES:
        if lower.startswith(prefix):
            return "anthropic"
    return "unknown"


# ---------------------------------------------------------------------------
# bind_structured — returns provider-specific extra kwargs for the LLM call
# ---------------------------------------------------------------------------


def bind_structured(model_id: str, schema: type[BaseModel]) -> dict[str, Any]:
    """Return provider-specific kwargs for structured output binding.

    These kwargs should be passed as **kwargs (or merged into the call dict)
    when invoking the LLM client. They do NOT include the prompt or model —
    only the structured-output-specific parameters.

    Args:
        model_id: Full model identifier, e.g. "openai/gpt-4o",
                  "anthropic/claude-3-5-haiku-20241022",
                  "google/gemini-2.0-flash", "xai/grok-3-mini".
        schema:   A Pydantic v2 BaseModel subclass whose JSON schema
                  describes the desired output structure.

    Returns:
        Dict of extra kwargs/params for the LLM call.
        For the 'unknown' provider, returns an empty dict (free-text mode).

    Examples:
        openai/gpt-4o   → {"response_format": {"type": "json_schema",
                            "json_schema": {"name": "TraderProposal",
                                            "schema": {...}, "strict": True}}}
        xai/grok-3-mini → same structure as openai/*
        anthropic/*     → {"tools": [...], "tool_choice": {"type": "any"}}
        google/*        → {"response_schema": {...}, "response_mime_type": "application/json"}
        unknown         → {}
    """
    provider = _detect_provider(model_id)
    schema_name = schema.__name__
    json_schema = schema.model_json_schema()

    if provider == "openai":
        return _bind_openai(schema_name, json_schema)
    if provider == "google":
        return _bind_google(json_schema)
    if provider == "anthropic":
        return _bind_anthropic(schema_name, json_schema)
    # unknown: free-text fallback — caller will json.loads() the response
    logger.debug(
        "bind_structured: unknown provider for %r; falling back to free-text.",
        model_id,
    )
    return {}


def _bind_openai(schema_name: str, json_schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI / xAI (Grok) — json_schema response_format."""
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": True,
            },
        }
    }


def _bind_google(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Google Gemini — response_schema + MIME type."""
    return {
        "response_schema": json_schema,
        "response_mime_type": "application/json",
    }


def _bind_anthropic(schema_name: str, json_schema: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Claude — forced tool-use (tool_choice=any).

    Anthropic does not support response_format; instead we define a single
    tool whose input_schema is the target schema and force the model to call
    it via tool_choice={"type": "any"}.
    """
    return {
        "tools": [
            {
                "name": schema_name,
                "description": f"Emit a structured {schema_name} response.",
                "input_schema": json_schema,
            }
        ],
        "tool_choice": {"type": "any"},
    }


# ---------------------------------------------------------------------------
# invoke_structured_or_freetext — graceful-fallback wrapper
# ---------------------------------------------------------------------------


def invoke_structured_or_freetext(
    client: Any,
    prompt: list[dict[str, str]],
    schema: type[T],
    model_id: str,
    *,
    system: str | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[T | None, dict[str, Any]]:
    """Invoke the LLM client and parse the structured response.

    Attempts provider-native structured output first; on any failure
    (SDK error, structured-output refusal, Pydantic validation error)
    falls back to free-text + manual json.loads().

    The function signature is intentionally generic: it accepts any
    callable ``client`` that has a ``chat.completions.create``-style
    interface OR a plain ``client(messages, **kwargs)`` interface.  In
    tests, pass a mock.

    Args:
        client:      LLM client instance.  Must support one of:
                       client.chat.completions.create(model, messages, **kw)
                       client(messages=..., model=..., **kw)
        prompt:      List of message dicts (role/content).
        schema:      Pydantic v2 BaseModel subclass to parse into.
        model_id:    Full model identifier for provider detection.
        system:      Optional system message (prepended to messages).
        extra_kwargs: Additional kwargs merged into the LLM call.

    Returns:
        (parsed_obj, raw_response) where:
          - parsed_obj is a validated schema instance, or None on failure.
          - raw_response is a dict with at least a 'text' key containing
            the raw LLM output (or an 'error' key on hard failure).
    """
    struct_kwargs = bind_structured(model_id, schema)
    if extra_kwargs:
        struct_kwargs.update(extra_kwargs)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(prompt)

    raw_text: str = ""
    raw_response: dict[str, Any] = {}

    try:
        raw_text, raw_response = _invoke_client(
            client=client,
            model_id=model_id,
            messages=messages,
            extra_kwargs=struct_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoke_structured_or_freetext: LLM call failed: %s", exc)
        raw_response["error"] = f"{type(exc).__name__}: {exc}"
        return None, raw_response

    # --- Parse via the single canonical ladder (shared with LLMCaller.call) ---
    parsed = parse_structured_or_freetext(
        raw_text=raw_text,
        raw_response=raw_response,
        schema=schema,
        model_id=model_id,
    )
    if parsed is None:
        logger.warning(
            "invoke_structured_or_freetext: all parse attempts failed for %s. "
            "Returning (None, raw_response) for graceful fallback.",
            schema.__name__,
        )
    return parsed, raw_response


# ---------------------------------------------------------------------------
# Canonical parse ladder — the ONE entry point both call sites route through
# ---------------------------------------------------------------------------


def parse_structured_or_freetext(
    raw_text: str,
    raw_response: dict[str, Any],
    schema: type[T],
    model_id: str,
) -> T | None:
    """The single canonical parse ladder for structured LLM output.

    provider-native parse → free-text JSON fallback. Returns the validated
    schema instance or None on total failure.

    ``invoke_structured_or_freetext`` and ``LLMCaller.call`` BOTH route through
    this helper so the two structured-output entry points cannot drift.

    Args:
        raw_text:     The raw assistant text extracted from the LLM response.
        raw_response: The full provider response dict (used for the provider's
                      tool-call path on Anthropic).
        schema:       Pydantic v2 BaseModel subclass to parse into.
        model_id:     Full model identifier for provider detection.

    Returns:
        A validated schema instance, or None if every parse attempt fails.
    """
    provider = _detect_provider(model_id)

    # 1) provider-native structured-output parse
    parsed = _parse_response(
        raw_text=raw_text,
        raw_response=raw_response,
        schema=schema,
        provider=provider,
    )
    if parsed is not None:
        return parsed

    # 2) graceful fallback: manual json.loads() on free-text (fenced/embedded)
    parsed = _parse_freetext_json(raw_text, schema)
    if parsed is not None:
        logger.info(
            "parse_structured_or_freetext: structured output failed; "
            "fell back to free-text JSON parse for %s.",
            schema.__name__,
        )
        return parsed

    return None


# ---------------------------------------------------------------------------
# Private parse helpers
# ---------------------------------------------------------------------------


def _invoke_client(
    client: Any,
    model_id: str,
    messages: list[dict[str, str]],
    extra_kwargs: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Call the client and return (raw_text, raw_response_dict).

    Supports:
      1. client.chat.completions.create(model=..., messages=..., **kw)
         (OpenAI-compatible SDK)
      2. client(messages=..., model=..., **kw)
         (callable / mock / custom)

    Returns the raw text and a dict representation of the response.
    """
    raw_response: dict[str, Any] = {}

    # Attempt OpenAI-style interface first
    if hasattr(client, "chat") and hasattr(client.chat, "completions"):
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            **extra_kwargs,
        )
        # OpenAI response object → extract text
        raw_text = _extract_text_from_oai_response(response)
        raw_response = _response_to_dict(response)
        return raw_text, raw_response

    # Fallback: treat client as a plain callable
    response = client(messages=messages, model=model_id, **extra_kwargs)
    if isinstance(response, str):
        raw_text = response
        raw_response = {"text": raw_text}
    elif isinstance(response, dict):
        raw_text = response.get("text") or response.get("content") or ""
        raw_response = response
    else:
        raw_text = str(response)
        raw_response = {"text": raw_text}
    return raw_text, raw_response


def _extract_text_from_oai_response(response: Any) -> str:
    """Extract plain text from an OpenAI-style response object."""
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        pass
    # tool-call path (Anthropic via OpenAI compat or native anthropic SDK)
    try:
        return json.dumps(response.choices[0].message.tool_calls[0].function.arguments)
    except (AttributeError, IndexError, TypeError):
        pass
    return str(response)


def _response_to_dict(response: Any) -> dict[str, Any]:
    """Convert an SDK response object to a plain dict."""
    if isinstance(response, dict):
        return response
    try:
        return response.model_dump()
    except AttributeError:
        pass
    try:
        return dict(vars(response))
    except TypeError:
        return {"raw": str(response)}


def _parse_response(
    raw_text: str,
    raw_response: dict[str, Any],
    schema: type[T],
    provider: str,
) -> T | None:
    """Try to parse provider-specific structured output into schema.

    Returns the parsed object or None on failure.
    """
    if not raw_text:
        return None
    try:
        data = json.loads(raw_text)
        return schema.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        pass

    # Anthropic tool-call path: arguments may be in a nested structure
    if provider == "anthropic":
        try:
            outer = json.loads(raw_text)
            # Anthropic tools response: {"type": "tool_use", "input": {...}}
            if isinstance(outer, dict) and "input" in outer:
                return schema.model_validate(outer["input"])
        except (json.JSONDecodeError, ValidationError):
            pass

    return None


def _parse_freetext_json(raw_text: str, schema: type[T]) -> T | None:
    """Try to extract and validate JSON from free-text LLM output.

    Handles common patterns:
    - Plain JSON string
    - ```json ... ``` fenced blocks
    - JSON embedded in prose (first { ... } block)
    """
    if not raw_text:
        return None

    # Try plain parse first
    try:
        return schema.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError):
        pass

    # Try ```json ... ``` fenced block
    import re
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw_text)
    if m:
        try:
            return schema.model_validate(json.loads(m.group(1).strip()))
        except (json.JSONDecodeError, ValidationError):
            pass

    # Try first { ... } block (greedy — handles prose-embedded JSON)
    m2 = re.search(r"\{[\s\S]+\}", raw_text)
    if m2:
        try:
            return schema.model_validate(json.loads(m2.group(0)))
        except (json.JSONDecodeError, ValidationError):
            pass

    return None
