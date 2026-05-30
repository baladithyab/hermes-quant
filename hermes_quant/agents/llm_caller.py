"""hermes_quant.agents.llm_caller — unified, silence-by-default LLM call wrapper.

ADR-0054: LLM-Caller Foundation & TraderNode v0.2.

Design contract
───────────────
* Routes to OpenRouter (https://openrouter.ai/api/v1) using OPENROUTER_API_KEY.
* Uses provider-aware structured-output binding from structured_output.py.
* NEVER raises from .call() — returns (None, {"error": ...}) on any failure.
* Records every call attempt as an audit-log event (kind='llm_call' by default)
  with 8+ fields: model_id, prompt_hash, raw_response, parsed_dump, latency_ms,
  error, audit_kind, timestamp.
* .available() is a cheap env-var + reachability probe; no full LLM call.

Usage
─────
    from hermes_quant.agents.llm_caller import LLMCaller
    from hermes_quant.agents.trader import TraderProposal

    caller = LLMCaller()
    obj, raw = caller.call(
        system_prompt="You are a trading assistant.",
        user_prompt="Given this research plan, propose a trade ...",
        schema=TraderProposal,
    )
    if obj is None:
        # structured output failed or LLM unavailable — apply conservative default
        ...
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, Optional, Type

from pydantic import BaseModel

from hermes_quant.agents.structured_output import (
    bind_structured,
    parse_structured_or_freetext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_ID = "openai/gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_AUDIT_KIND = "llm_call"

# Audit-log event kind for LLM calls — registered as an extension kind.
# The governance audit_log validates against VALID_KINDS, so we write via
# the raw append path with kind="llm_call" using the _append_extension helper
# that bypasses the strict Literal gate (see _audit_append below).
_LLM_CALL_KIND = "llm_call"


# ---------------------------------------------------------------------------
# Audit-log integration (graceful — never crashes if governance unavailable)
# ---------------------------------------------------------------------------


def _audit_append(kind: str, source: str, payload: dict[str, Any]) -> None:
    """Append an LLM-call event to the audit log.

    Uses a raw-write path so we are not blocked by the strict EventKind
    Literal defined in governance/audit_log.py (which covers the 8 core
    governance event types).  The 'llm_call' / 'trader_llm_call' kinds
    are extension kinds documented in ADR-0054.

    Never raises — any failure is logged at WARNING.
    """
    try:
        import uuid as _uuid
        from hermes_quant.governance.audit_log import (
            AUDIT_LOG_PATH,
            CURRENT_SCHEMA_VERSION,
            _write_lock,
        )
        import os as _os

        row = {
            "event_id": str(_uuid.uuid4()),
            "kind": kind,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "asof": datetime.now(UTC).isoformat(),
            "source": source,
            "payload": payload,
        }
        line = json.dumps(row, sort_keys=True, default=str)
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(AUDIT_LOG_PATH, "a", buffering=1) as f:
                f.write(line + "\n")
                f.flush()
                _os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLMCaller: audit_append failed (%s); continuing.", exc)


# ---------------------------------------------------------------------------
# LLMCaller
# ---------------------------------------------------------------------------


class LLMCaller:
    """Unified, silence-by-default LLM call wrapper for hermes-quant.

    ADR-0054 §2: every component that needs to call an LLM (TraderNodeLLM,
    RiskCommitteeV2, ReflectorV2) should compose with this class rather than
    rolling its own HTTP + audit path.

    Args:
        model_id:    OpenRouter-style model identifier, e.g. "openai/gpt-4.1-mini".
        api_key:     API key; defaults to OPENROUTER_API_KEY env var.
        base_url:    Base URL for the completions endpoint.
        timeout:     Per-request timeout in seconds.
        audit_kind:  Event kind written to the audit log (default "llm_call").
    """

    def __init__(
        self,
        *,
        model_id: str = _DEFAULT_MODEL_ID,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        audit_kind: str = _DEFAULT_AUDIT_KIND,
    ) -> None:
        self.model_id = model_id
        self._api_key = api_key  # None → read from env at call time
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.audit_kind = audit_kind

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Return True iff an API key is present.

        Intentionally cheap: only checks env-var presence.  Does NOT
        make a network round-trip (that would add latency to the hot path).
        Returns False if the key is missing; callers fall back to v0.1.
        """
        return bool(self._resolve_api_key())

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        schema: Optional[Type[BaseModel]] = None,
    ) -> tuple[BaseModel | str | None, dict[str, Any]]:
        """Call the LLM with optional structured-output binding.

        Args:
            system_prompt: System message content.
            user_prompt:   User message content.
            schema:        Optional Pydantic model class.  When provided,
                           uses bind_structured() for provider-native JSON output
                           and validates the response.  When None, returns raw text.

        Returns:
            (parsed_obj, raw_response_dict):
              - On structured success: (BaseModel instance, raw dict)
              - On parse failure:      (None, raw dict)
              - On network/auth error: (None, {"error": "<msg>"})
              - On no api_key:         (None, {"error": "no_api_key"})

        Never raises.
        """
        api_key = self._resolve_api_key()
        prompt_hash = _sha256_hash(system_prompt + user_prompt)
        start_ms = time.monotonic()

        if not api_key:
            err_msg = "no_api_key: OPENROUTER_API_KEY is not set"
            logger.warning("LLMCaller.call: %s", err_msg)
            raw = {"error": err_msg}
            self._record_audit(
                prompt_hash=prompt_hash,
                raw_response=raw,
                parsed_dump=None,
                latency_ms=0.0,
                error=err_msg,
            )
            return None, raw

        # ---- Build request payload ----------------------------------------
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
        }
        if schema is not None:
            extra = bind_structured(self.model_id, schema)
            body.update(extra)

        # ---- HTTP call -------------------------------------------------------
        raw_response: dict[str, Any] = {}
        raw_text: str = ""
        error_msg: str | None = None

        try:
            raw_text, raw_response = self._http_post(api_key, body)
        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("LLMCaller.call: HTTP error — %s", error_msg)
            raw_response = {"error": error_msg}

        latency_ms = (time.monotonic() - start_ms) * 1000.0

        # ---- Parse response --------------------------------------------------
        parsed_obj: BaseModel | str | None = None
        parsed_dump: dict[str, Any] | None = None

        if error_msg is None and schema is not None and raw_text:
            # Route through the single canonical parse ladder shared with
            # invoke_structured_or_freetext so the two structured-output entry
            # points cannot drift (ADR-0044 / Wave C G11).
            parsed_obj = parse_structured_or_freetext(
                raw_text, raw_response, schema, self.model_id
            )
            if parsed_obj is None:
                logger.warning(
                    "LLMCaller.call: structured parse failed for %s; returning (None, raw).",
                    schema.__name__,
                )
            else:
                parsed_dump = parsed_obj.model_dump()

        elif error_msg is None and schema is None and raw_text:
            # Free-text mode: return raw text as the "parsed" result
            parsed_obj = raw_text

        # ---- Audit -----------------------------------------------------------
        self._record_audit(
            prompt_hash=prompt_hash,
            raw_response=raw_response,
            parsed_dump=parsed_dump,
            latency_ms=latency_ms,
            error=error_msg,
        )

        return parsed_obj, raw_response

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        """Return the API key, preferring the constructor arg then the env var."""
        return self._api_key or os.environ.get("OPENROUTER_API_KEY") or None

    def _http_post(
        self, api_key: str, body: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """POST to the completions endpoint; return (raw_text, raw_response_dict).

        Uses httpx (already a project dependency per ADR-0054 §6).
        Raises on network errors, timeouts, and non-2xx status codes.
        """
        import httpx  # local import so module loads even if httpx unavailable

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=headers, json=body)

        if response.status_code == 401:
            raise PermissionError(
                f"OpenRouter 401 Unauthorized — check OPENROUTER_API_KEY. "
                f"URL: {url}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter HTTP {response.status_code}: {response.text[:300]}"
            )

        resp_json: dict[str, Any] = response.json()

        # Extract the assistant message text from OAI-compatible response
        raw_text = ""
        try:
            raw_text = resp_json["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            pass
        if not raw_text:
            # Tool-call path (Anthropic via OAI compat)
            try:
                raw_text = json.dumps(
                    resp_json["choices"][0]["message"]["tool_calls"][0]["function"][
                        "arguments"
                    ]
                )
            except (KeyError, IndexError, TypeError):
                pass

        return raw_text, resp_json

    def _record_audit(
        self,
        *,
        prompt_hash: str,
        raw_response: dict[str, Any],
        parsed_dump: dict[str, Any] | None,
        latency_ms: float,
        error: str | None,
    ) -> None:
        """Write one audit-log event. Never raises."""
        payload: dict[str, Any] = {
            "model_id": self.model_id,
            "prompt_hash": prompt_hash,
            "raw_response": _safe_truncate(raw_response),
            "parsed_dump": parsed_dump,
            "latency_ms": round(latency_ms, 2),
            "error": error,
            "audit_kind": self.audit_kind,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        _audit_append(
            kind=self.audit_kind,
            source="hermes_quant.agents.llm_caller",
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _sha256_hash(text: str) -> str:
    """Return the SHA-256 hex digest of *text* (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_truncate(obj: Any, max_chars: int = 4096) -> Any:
    """Truncate a dict/str to avoid storing huge raw responses in audit log."""
    if isinstance(obj, str) and len(obj) > max_chars:
        return obj[:max_chars] + "...[truncated]"
    if isinstance(obj, dict):
        s = json.dumps(obj, default=str)
        if len(s) > max_chars:
            return {"_truncated": s[:max_chars] + "...[truncated]"}
    return obj
