"""tests/agents/test_llm_caller_fingerprint.py — B41-f reproducibility tests.

ADR-4665 §7.6 (Gate 1, reproducibility):
  * Pin model snapshot, temperature=0, top_p=1, max_tokens on EVERY call.
  * Record system_fingerprint (when the provider returns one) in the audit event.
  * Golden-response idempotency: the deterministic projection of a fixed logged
    LLM output is byte-stable (same input → same downstream numeric projection).

All tests are offline — httpx.Client is mocked, no real network.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from hermes_quant.agents.llm_caller import LLMCaller


class _SimpleSchema(BaseModel):
    value: int
    label: str


def _make_oai_response(content: str, *, system_fingerprint: str | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "model": "openai/gpt-4o-2024-08-06",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    if system_fingerprint is not None:
        resp["system_fingerprint"] = system_fingerprint
    return resp


def _mock_http_response(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if body is not None:
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.side_effect = json.JSONDecodeError("bad json", "", 0)
        resp.text = text
    return resp


def _run_call(monkeypatch, oai_resp: dict, *, schema=None, **caller_kwargs):
    """Drive one LLMCaller.call through a mocked httpx client; return (obj, raw,
    captured_body, audit_payloads)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    mock_resp = _mock_http_response(200, body=oai_resp)
    captured: dict[str, Any] = {}
    audit_payloads: list[dict[str, Any]] = []

    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_payloads.append(payload)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _post(url, headers=None, json=None):  # noqa: A002 — match httpx sig
                captured["url"] = url
                captured["json"] = json
                return mock_resp

            mock_client.post.side_effect = _post
            mock_client_cls.return_value = mock_client

            caller = LLMCaller(api_key="sk-test", **caller_kwargs)
            obj, raw = caller.call("sys", "user", schema=schema)

    return obj, raw, captured.get("json", {}), audit_payloads


# ---------------------------------------------------------------------------
# (e) Every call pins temperature=0, top_p=1, max_tokens, model snapshot.
# ---------------------------------------------------------------------------


def test_call_body_pins_temperature_top_p_and_max_tokens(monkeypatch) -> None:
    oai_resp = _make_oai_response(json.dumps({"value": 1, "label": "a"}))
    obj, raw, body, _ = _run_call(monkeypatch, oai_resp, schema=_SimpleSchema)

    assert body["temperature"] == 0.0
    assert body["top_p"] == 1.0
    assert "max_tokens" in body
    assert isinstance(body["max_tokens"], int)
    assert body["max_tokens"] > 0
    # Model snapshot is pinned on the request.
    assert body["model"] == "openai/gpt-4.1-mini"


def test_call_body_uses_explicit_max_tokens(monkeypatch) -> None:
    oai_resp = _make_oai_response(json.dumps({"value": 1, "label": "a"}))
    obj, raw, body, _ = _run_call(
        monkeypatch, oai_resp, schema=_SimpleSchema, max_tokens=256
    )
    assert body["max_tokens"] == 256


def test_freetext_call_also_pins_config(monkeypatch) -> None:
    """The pinning applies even without a schema (free-text mode)."""
    oai_resp = _make_oai_response("hello world")
    obj, raw, body, _ = _run_call(monkeypatch, oai_resp, schema=None)
    assert body["temperature"] == 0.0
    assert body["top_p"] == 1.0
    assert "max_tokens" in body


# ---------------------------------------------------------------------------
# (f) system_fingerprint recorded in the audit event when provider returns it.
# ---------------------------------------------------------------------------


def test_system_fingerprint_recorded_when_present(monkeypatch) -> None:
    oai_resp = _make_oai_response(
        json.dumps({"value": 7, "label": "b"}), system_fingerprint="fp_abc123"
    )
    obj, raw, body, audit = _run_call(monkeypatch, oai_resp, schema=_SimpleSchema)
    assert len(audit) == 1
    assert audit[0]["system_fingerprint"] == "fp_abc123"
    # The pinned generation config is also recorded for replay.
    assert audit[0]["temperature"] == 0.0
    assert audit[0]["top_p"] == 1.0
    assert audit[0]["max_tokens"] > 0
    assert audit[0]["model_id"] == "openai/gpt-4.1-mini"


def test_system_fingerprint_none_when_absent(monkeypatch) -> None:
    oai_resp = _make_oai_response(json.dumps({"value": 7, "label": "b"}))
    obj, raw, body, audit = _run_call(monkeypatch, oai_resp, schema=_SimpleSchema)
    assert len(audit) == 1
    # Field present in every audit row for schema stability, but None when the
    # provider returned no fingerprint.
    assert "system_fingerprint" in audit[0]
    assert audit[0]["system_fingerprint"] is None


# ---------------------------------------------------------------------------
# (g) Golden-response idempotency: a fixed logged LLM output projects to the
# same downstream numeric value every time.
# ---------------------------------------------------------------------------


def test_golden_response_projection_is_stable(monkeypatch) -> None:
    """Same logged raw response → identical parsed numeric projection twice."""
    from hermes_quant.agents.trader import TraderProposal

    # A FIXED logged LLM output (the "golden" response).
    golden = json.dumps(
        {
            "action": "BUY",
            "size_fraction": 0.10,
            "confidence": 0.73,
            "rationale": "golden fixed response",
        }
    )

    def _project() -> tuple[str, float, float]:
        oai_resp = _make_oai_response(golden)
        obj, raw, body, _ = _run_call(
            monkeypatch, oai_resp, schema=TraderProposal, model_id="openai/gpt-4o"
        )
        assert isinstance(obj, TraderProposal)
        return (obj.action.value, float(obj.size_fraction), float(obj.confidence))

    first = _project()
    second = _project()
    assert first == second
    assert first == ("BUY", 0.10, 0.73)


def test_golden_response_audit_dump_is_byte_stable(monkeypatch) -> None:
    """The parsed_dump serialized into the audit event is byte-identical across
    runs for a fixed logged response — the reproducibility invariant Gate 1
    relies on."""
    from hermes_quant.agents.trader import TraderProposal

    golden = json.dumps(
        {
            "action": "SELL",
            "size_fraction": 0.05,
            "confidence": 0.42,
            "rationale": "stable dump",
        }
    )

    def _dump() -> str:
        oai_resp = _make_oai_response(golden)
        obj, raw, body, audit = _run_call(
            monkeypatch, oai_resp, schema=TraderProposal, model_id="openai/gpt-4o"
        )
        return json.dumps(audit[0]["parsed_dump"], sort_keys=True, default=str)

    assert _dump() == _dump()
