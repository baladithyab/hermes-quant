"""tests/agents/test_structured_output.py — G11 structured-output helper tests.

No network — the client is a callable/dict mock. Covers provider routing,
bind_structured shapes, the invoke wrapper's graceful-fallback ladder, and the
single-path proof that LLMCaller.call and invoke_structured_or_freetext both
route through parse_structured_or_freetext.
"""

from __future__ import annotations

import json

from hermes_quant.agents.structured_output import (
    _detect_provider,
    bind_structured,
    invoke_structured_or_freetext,
    parse_structured_or_freetext,
)
from hermes_quant.agents.trader import TraderAction, TraderProposal

_MODEL = "openai/gpt-4o"


def _valid_proposal_json() -> str:
    return json.dumps(
        {
            "action": "BUY",
            "size_fraction": 0.10,
            "confidence": 0.7,
            "rationale": "valid structured proposal",
        }
    )


def test_detect_provider_routing():
    assert _detect_provider("openai/gpt-4o") == "openai"
    assert _detect_provider("google/gemini-2.0-flash") == "google"
    assert _detect_provider("anthropic/claude-3-5-haiku") == "anthropic"
    assert _detect_provider("xai/grok-3") == "openai"
    assert _detect_provider("mystery-model") == "unknown"


def test_bind_structured_openai_shape():
    kw = bind_structured("openai/gpt-4o", TraderProposal)
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["name"] == "TraderProposal"


def test_bind_structured_anthropic_tool_choice_any():
    kw = bind_structured("anthropic/claude-3-5-haiku", TraderProposal)
    assert "tools" in kw
    assert kw["tool_choice"] == {"type": "any"}


def test_bind_structured_google_response_schema():
    kw = bind_structured("google/gemini-2.0-flash", TraderProposal)
    assert "response_schema" in kw
    assert kw["response_mime_type"] == "application/json"


def test_bind_structured_unknown_empty():
    assert bind_structured("mystery-model", TraderProposal) == {}


def test_invoke_happy_path_callable_client():
    def client(messages, model, **kw):  # noqa: ANN001
        return _valid_proposal_json()

    obj, raw = invoke_structured_or_freetext(
        client=client,
        prompt=[{"role": "user", "content": "propose a trade"}],
        schema=TraderProposal,
        model_id=_MODEL,
    )
    assert obj is not None
    assert obj.action == TraderAction.BUY


def test_invoke_freetext_fallback_fenced():
    fenced = "Here is the answer:\n```json\n" + _valid_proposal_json() + "\n```"

    def client(messages, model, **kw):  # noqa: ANN001
        return fenced

    obj, raw = invoke_structured_or_freetext(
        client=client,
        prompt=[{"role": "user", "content": "x"}],
        schema=TraderProposal,
        model_id=_MODEL,
    )
    assert obj is not None
    assert obj.action == TraderAction.BUY


def test_invoke_validation_failure_returns_none():
    def client(messages, model, **kw):  # noqa: ANN001
        return "this is definitely not json"

    obj, raw = invoke_structured_or_freetext(
        client=client,
        prompt=[{"role": "user", "content": "x"}],
        schema=TraderProposal,
        model_id=_MODEL,
    )
    assert obj is None
    assert isinstance(raw, dict)  # does not raise


def test_invoke_client_exception_graceful():
    def client(messages, model, **kw):  # noqa: ANN001
        raise RuntimeError("boom")

    obj, raw = invoke_structured_or_freetext(
        client=client,
        prompt=[{"role": "user", "content": "x"}],
        schema=TraderProposal,
        model_id=_MODEL,
    )
    assert obj is None
    assert "error" in raw


def test_parse_helper_shared_by_caller_and_invoke():
    # Same malformed-then-fenced raw text routed through the public parser and
    # through invoke_structured_or_freetext must agree (single-path proof).
    fenced = "prose then ```json\n" + _valid_proposal_json() + "\n``` trailing"
    raw_response = {"text": fenced}

    direct = parse_structured_or_freetext(fenced, raw_response, TraderProposal, _MODEL)

    def client(messages, model, **kw):  # noqa: ANN001
        return fenced

    via_invoke, _ = invoke_structured_or_freetext(
        client=client,
        prompt=[{"role": "user", "content": "x"}],
        schema=TraderProposal,
        model_id=_MODEL,
    )
    assert direct is not None
    assert via_invoke is not None
    assert direct.model_dump() == via_invoke.model_dump()
