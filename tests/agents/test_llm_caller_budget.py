"""tests/agents/test_llm_caller_budget.py — LLMCaller × LLMBudgetGuard wiring (B41-a).

ADR-4665 §7.1/§7b: the budget guard is a PRE-CALL gate inside LLMCaller.call().

Deliverables exercised here at the caller seam:
  (a) budget exhaustion → deterministic fallback (None, {error}), NOT a raise
  (d) corrupt spend file → fail-closed (the call is blocked, no network attempt)
  + clamp: the guard's allowed_max_tokens is what hits the wire
  + record: a successful call's actual usage is folded into the ledger
  + no-guard (default) → byte-identical request body to pre-B41-a (no budget keys)

All offline — httpx.Client mocked; ledger writes go to tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from hermes_quant.agents.llm_budget import BudgetCeilings, LLMBudgetGuard
from hermes_quant.agents.llm_caller import LLMCaller


class _SimpleSchema(BaseModel):
    value: int
    label: str


def _oai(content: str, *, prompt_tokens: int = 10, completion_tokens: int = 20) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "model": "test/model",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _mock_resp(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


def _drive(caller: LLMCaller, body_resp: dict, **call_kwargs):
    """Run caller.call through a mocked client; return (obj, raw, captured_body,
    post_called)."""
    captured: dict[str, Any] = {}
    post_called = {"n": 0}

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _post(url, headers=None, json=None):  # noqa: A002
                post_called["n"] += 1
                captured["json"] = json
                return _mock_resp(body_resp)

            mock_client.post.side_effect = _post
            mock_client_cls.return_value = mock_client

            obj, raw = caller.call("sys", "user", **call_kwargs)
    return obj, raw, captured.get("json", {}), post_called["n"]


def _guard(tmp_path: Path, **ceilings) -> LLMBudgetGuard:
    return LLMBudgetGuard(
        ceilings=BudgetCeilings(**ceilings),
        price_table={"test/model": (0.001, 0.001)},
        default_price=(1.0, 1.0),
        path=tmp_path / "spend.json",
    )


# ---------------------------------------------------------------------------
# (a) Budget exhaustion → deterministic fallback (None, {error}), no raise, no
# network call attempted.
# ---------------------------------------------------------------------------


def test_exhausted_budget_returns_none_error_and_skips_network(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    # Zero per-tick USD ceiling → kill-switch: the first call is blocked.
    guard = _guard(tmp_path, per_tick_usd=0.0)
    caller = LLMCaller(api_key="sk-test", model_id="test/model", budget_guard=guard)

    obj, raw, body, n_post = _drive(
        caller, _oai("{}"), schema=_SimpleSchema, decision_id="d1", tick_id="t1"
    )

    assert obj is None
    assert "error" in raw
    assert raw["error"].startswith("budget_")
    # No money/network spent: the HTTP client's post was never called.
    assert n_post == 0


def test_exhausted_budget_never_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    guard = _guard(tmp_path, per_tick_tokens=0)  # token kill-switch
    caller = LLMCaller(api_key="sk-test", model_id="test/model", budget_guard=guard)
    # Should return cleanly, not raise.
    obj, raw, body, n_post = _drive(
        caller, _oai("{}"), schema=_SimpleSchema, decision_id="d1", tick_id="t1"
    )
    assert obj is None
    assert n_post == 0


# ---------------------------------------------------------------------------
# (d) Corrupt spend file → fail-closed: call blocked, no network.
# ---------------------------------------------------------------------------


def test_corrupt_ledger_fails_closed_at_caller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    spend = tmp_path / "spend.json"
    spend.write_text("}}}not json", encoding="utf-8")
    guard = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=100.0),  # huge ceiling
        price_table={"test/model": (0.001, 0.001)},
        path=spend,
    )
    caller = LLMCaller(api_key="sk-test", model_id="test/model", budget_guard=guard)
    obj, raw, body, n_post = _drive(
        caller, _oai("{}"), schema=_SimpleSchema, decision_id="d1", tick_id="t1"
    )
    assert obj is None
    assert raw["error"] == "budget_fail_closed"
    assert n_post == 0


# ---------------------------------------------------------------------------
# Clamp: the guard's allowed_max_tokens is what hits the wire.
# ---------------------------------------------------------------------------


def test_guard_clamps_max_tokens_on_the_wire(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    guard = _guard(tmp_path, per_tick_tokens=300)
    # Pre-spend 100 tokens so only 200 remain. Prompt is tiny (~2 tokens), so
    # the completion ceiling clamps to ~198.
    guard.record(
        model_id="test/model",
        prompt_tokens=60,
        completion_tokens=40,
        decision_id="d0",
        tick_id="t1",
    )
    caller = LLMCaller(
        api_key="sk-test", model_id="test/model", budget_guard=guard, max_tokens=1000
    )
    obj, raw, body, n_post = _drive(
        caller, _oai("{}"), schema=_SimpleSchema, decision_id="d1", tick_id="t1"
    )
    assert n_post == 1
    # max_tokens on the request is clamped below the requested 1000.
    assert body["max_tokens"] < 1000
    assert body["max_tokens"] <= 200


# ---------------------------------------------------------------------------
# Record: a successful call's actual usage is folded into the durable ledger.
# ---------------------------------------------------------------------------


def test_successful_call_records_spend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    guard = _guard(tmp_path, per_tick_usd=10.0)
    caller = LLMCaller(api_key="sk-test", model_id="test/model", budget_guard=guard)

    obj, raw, body, n_post = _drive(
        caller,
        _oai(json.dumps({"value": 1, "label": "a"}), prompt_tokens=1000, completion_tokens=1000),
        schema=_SimpleSchema,
        decision_id="d1",
        tick_id="t1",
    )
    assert n_post == 1
    snap = guard.snapshot(decision_id="d1", tick_id="t1")
    # 1000 prompt + 1000 completion at $0.001/1k each = $0.002.
    assert snap["tick_usd"] == pytest.approx(0.002)
    assert snap["decision_calls"] == 1


def test_record_persists_for_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    path = tmp_path / "spend.json"
    guard = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=10.0),
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )
    caller = LLMCaller(api_key="sk-test", model_id="test/model", budget_guard=guard)
    _drive(
        caller,
        _oai(json.dumps({"value": 1, "label": "a"}), prompt_tokens=1000, completion_tokens=1000),
        schema=_SimpleSchema,
        decision_id="d1",
        tick_id="t1",
    )
    # New guard reading the same file (simulated restart) sees the spend.
    g2 = LLMBudgetGuard(
        ceilings=BudgetCeilings(per_tick_usd=10.0),
        price_table={"test/model": (0.001, 0.001)},
        path=path,
    )
    assert g2.snapshot(decision_id="d1", tick_id="t1")["tick_usd"] == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# No guard wired in (the DEFAULT) → request body carries NO budget keys and the
# call behaves exactly as pre-B41-a (byte-identity argument).
# ---------------------------------------------------------------------------


def test_no_guard_default_body_has_no_budget_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    caller = LLMCaller(api_key="sk-test", model_id="test/model")  # no budget_guard
    obj, raw, body, n_post = _drive(
        caller, _oai(json.dumps({"value": 1, "label": "a"}), ), schema=_SimpleSchema
    )
    assert n_post == 1
    # B41-f pins these; no budget-specific keys leak into the request.
    assert set(body.keys()) <= {
        "model", "messages", "temperature", "top_p", "max_tokens", "response_format",
        "tools", "tool_choice", "response_schema", "response_mime_type",
    }
    assert "decision_id" not in body and "tick_id" not in body
