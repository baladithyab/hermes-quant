"""tests/agents/test_llm_caller.py — LLMCaller unit tests (ADR-0054).

All tests use mocks — no real network calls.

Coverage:
  - available() with/without OPENROUTER_API_KEY
  - .call() with no api_key → (None, {"error": ...}) + audit event
  - Mock httpx.Client: success path (valid JSON schema), timeout,
    401 Unauthorized, malformed JSON, freetext fallback
  - Each path produces a deterministic audit-log event
  - prompt_hash is stable for identical prompts
  - audit_kind customisation
"""

from __future__ import annotations

import json
import os
import hashlib
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
from pydantic import BaseModel

from hermes_quant.agents.llm_caller import LLMCaller, _sha256_hash, _safe_truncate


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _SimpleSchema(BaseModel):
    """Minimal schema used in tests."""
    value: int
    label: str


def _make_oai_response(content: str) -> dict[str, Any]:
    """Build a minimal OpenAI-style response dict."""
    return {
        "choices": [
            {"message": {"content": content, "role": "assistant"}}
        ],
        "model": "openai/gpt-4.1-mini",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


def _mock_http_response(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    """Return a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    if body is not None:
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.side_effect = json.JSONDecodeError("bad json", "", 0)
        resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Test 1: available() → False when no API key
# ---------------------------------------------------------------------------


def test_available_false_when_no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    caller = LLMCaller()
    assert caller.available() is False


# ---------------------------------------------------------------------------
# Test 2: available() → True when env var is set
# ---------------------------------------------------------------------------


def test_available_true_when_key_in_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-123")
    caller = LLMCaller()
    assert caller.available() is True


# ---------------------------------------------------------------------------
# Test 3: available() → True when key passed in constructor
# ---------------------------------------------------------------------------


def test_available_true_when_key_in_constructor(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    caller = LLMCaller(api_key="sk-constructor-key")
    assert caller.available() is True


# ---------------------------------------------------------------------------
# Test 4: .call() with no api_key → returns (None, {"error": ...})
# ---------------------------------------------------------------------------


def test_call_no_api_key_returns_none_error(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_quant.agents.llm_caller.AUDIT_LOG_PATH",
        tmp_path / "audit.jsonl",
        raising=False,
    )

    audit_events = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda **kw: audit_events.append(kw)
        caller = LLMCaller()
        obj, raw = caller.call("system", "user", schema=_SimpleSchema)

    assert obj is None
    assert "error" in raw
    assert "no_api_key" in raw["error"]


# ---------------------------------------------------------------------------
# Test 5: audit event is recorded on no-key call
# ---------------------------------------------------------------------------


def test_call_no_api_key_records_audit_event(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(
            {"kind": kind, "source": source, "payload": payload}
        )
        caller = LLMCaller()
        caller.call("sys", "user")

    assert len(audit_calls) == 1
    evt = audit_calls[0]
    assert evt["kind"] == "llm_call"
    assert evt["payload"]["model_id"] == "openai/gpt-4.1-mini"
    assert evt["payload"]["error"] is not None
    assert "prompt_hash" in evt["payload"]
    assert "latency_ms" in evt["payload"]
    assert "audit_kind" in evt["payload"]
    assert "timestamp" in evt["payload"]


# ---------------------------------------------------------------------------
# Test 6: success path — valid structured JSON response
# ---------------------------------------------------------------------------


def test_call_success_structured_output(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    valid_payload = {"value": 42, "label": "alpha"}
    oai_resp = _make_oai_response(json.dumps(valid_payload))
    mock_resp = _mock_http_response(200, body=oai_resp)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test")
            obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert isinstance(obj, _SimpleSchema)
    assert obj.value == 42
    assert obj.label == "alpha"
    assert len(audit_calls) == 1
    assert audit_calls[0]["parsed_dump"] == valid_payload
    assert audit_calls[0]["error"] is None


# ---------------------------------------------------------------------------
# Test 7: timeout → returns (None, {"error": ...})
# ---------------------------------------------------------------------------


def test_call_timeout_returns_none_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    import httpx as _httpx

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _httpx.TimeoutException("timed out")
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test")
            obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert obj is None
    assert "error" in raw
    assert "TimeoutException" in raw["error"] or "timed out" in raw["error"].lower()
    assert len(audit_calls) == 1
    assert audit_calls[0]["error"] is not None
    assert audit_calls[0]["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# Test 8: 401 Unauthorized → returns (None, {"error": ...})
# ---------------------------------------------------------------------------


def test_call_401_returns_none_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-bad-key")

    mock_resp = _mock_http_response(401, text="Unauthorized")

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-bad-key")
            obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert obj is None
    assert "error" in raw
    assert len(audit_calls) == 1
    assert "PermissionError" in audit_calls[0]["error"] or "401" in audit_calls[0]["error"]


# ---------------------------------------------------------------------------
# Test 9: malformed JSON response → parse fails, returns (None, raw)
# ---------------------------------------------------------------------------


def test_call_malformed_json_returns_none(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    # LLM returns gibberish instead of JSON
    oai_resp = _make_oai_response("this is not JSON at all!!")
    mock_resp = _mock_http_response(200, body=oai_resp)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test")
            obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert obj is None
    # raw_response is still the parsed OAI envelope
    assert isinstance(raw, dict)
    assert len(audit_calls) == 1
    # parse failed → parsed_dump is None
    assert audit_calls[0]["parsed_dump"] is None
    # no network error
    assert audit_calls[0]["error"] is None


# ---------------------------------------------------------------------------
# Test 10: prompt_hash is deterministic for identical prompts
# ---------------------------------------------------------------------------


def test_prompt_hash_deterministic():
    h1 = _sha256_hash("hello" + "world")
    h2 = _sha256_hash("hello" + "world")
    h3 = _sha256_hash("hello" + "WORLD")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Test 11: audit_kind customisation
# ---------------------------------------------------------------------------


def test_custom_audit_kind(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(
            {"kind": kind, "payload": payload}
        )
        caller = LLMCaller(audit_kind="trader_llm_call")
        caller.call("sys", "user")

    assert len(audit_calls) == 1
    assert audit_calls[0]["kind"] == "trader_llm_call"
    assert audit_calls[0]["payload"]["audit_kind"] == "trader_llm_call"


# ---------------------------------------------------------------------------
# Test 12: free-text mode (no schema) returns raw text string
# ---------------------------------------------------------------------------


def test_call_freetext_mode_returns_string(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    oai_resp = _make_oai_response("Hello from the LLM!")
    mock_resp = _mock_http_response(200, body=oai_resp)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test")
            obj, raw = caller.call("sys", "user")  # no schema

    assert obj == "Hello from the LLM!"
    assert isinstance(raw, dict)


# ---------------------------------------------------------------------------
# Test 13: _safe_truncate behaviour
# ---------------------------------------------------------------------------


def test_safe_truncate_long_string():
    long_str = "x" * 5000
    result = _safe_truncate(long_str, max_chars=100)
    assert result.endswith("[truncated]")
    assert len(result) < 200


def test_safe_truncate_short_string():
    short = "hello"
    assert _safe_truncate(short) == "hello"


# ---------------------------------------------------------------------------
# Test 14: 500 server error → error in raw
# ---------------------------------------------------------------------------


def test_call_500_returns_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    mock_resp = _mock_http_response(500, text="Internal Server Error")

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test")
            obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert obj is None
    assert "error" in raw
    assert len(audit_calls) == 1
    assert audit_calls[0]["error"] is not None


# ---------------------------------------------------------------------------
# Test 15: audit event contains all 8 required fields
# ---------------------------------------------------------------------------


def test_audit_event_has_required_fields(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        caller = LLMCaller(model_id="openai/gpt-4o")
        caller.call("system prompt", "user prompt", schema=_SimpleSchema)

    payload = audit_calls[0]
    required_fields = {
        "model_id", "prompt_hash", "raw_response", "parsed_dump",
        "latency_ms", "error", "audit_kind", "timestamp",
    }
    assert required_fields.issubset(set(payload.keys()))
    assert payload["model_id"] == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# G11 (Wave C): consolidated parse path — LLMCaller.call routes through the
# single shared parse_structured_or_freetext ladder.
# ---------------------------------------------------------------------------


def test_caller_uses_shared_parser(monkeypatch):
    """A fenced-JSON HTTP body is parsed via the consolidated free-text ladder."""
    from hermes_quant.agents.trader import TraderAction, TraderProposal

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    proposal_json = json.dumps(
        {
            "action": "BUY",
            "size_fraction": 0.10,
            "confidence": 0.7,
            "rationale": "shared-parser path",
        }
    )
    fenced = "Here you go:\n```json\n" + proposal_json + "\n```"
    oai_resp = _make_oai_response(fenced)
    mock_resp = _mock_http_response(200, body=oai_resp)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test", model_id="openai/gpt-4o")
            obj, raw = caller.call("sys", "user", schema=TraderProposal)

    assert isinstance(obj, TraderProposal)
    assert obj.action == TraderAction.BUY


def test_caller_no_api_key_silent(monkeypatch):
    """With OPENROUTER_API_KEY unset, .call returns (None, {error}) and never raises."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        caller = LLMCaller()  # no constructor key either
        obj, raw = caller.call("sys", "user", schema=_SimpleSchema)

    assert obj is None
    assert "error" in raw
    assert "no_api_key" in raw["error"]
