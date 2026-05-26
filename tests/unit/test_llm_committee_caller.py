"""Unit tests for the LLM-backed committee caller (ADR-0037).

Focus: failure-closed posture. The four failure modes mandated by the ADR
("Failure-closed posture") must each result in a dropped turn, never a
propagated exception.

  1. The LLM call itself raises (network/timeout).
  2. The LLM returns text that is not valid JSON.
  3. The LLM returns valid JSON that fails Pydantic validation
     (missing required field, out-of-range float, wrong literal).
  4. The LLM returns success — happy path, structured CommitteeTurn emitted.

Two consecutive drops in one tick must yield an empty list (the
deterministic aggregator's BMA fallback then handles the symbol).

All tests use a mock client; no live network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.aggregators.deliberative import (
    CommitteeTurn,
    DeliberativeConfig,
)
from hermes_quant.aggregators.llm_committee import (
    BullBearTurn,
    PortfolioDecision,
    ResearchPlan,
    RiskTurn,
    run_llm_committee,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx() -> MarketContext:
    ts = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100, 101],
            "high": [101, 102],
            "low": [99, 100],
            "close": [100, 101],
            "volume": [1000, 1000],
        }
    )
    return MarketContext(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.0,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view(name: str, direction: int, conf: float = 0.7) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=conf,
        confidence_raw=conf,
        horizon="1d",
        rationale=f"{name} rationale",
    )


def _baseline() -> AggregatedSignal:
    v = _view("ta", 1)
    return AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2024-01-01 01:00", tz="UTC"),
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.6,
        horizon="1d",
        components=(v,),
        aggregator="bma",
    )


def _config(**overrides: Any) -> DeliberativeConfig:
    base = dict(enable_llm_turns=True, max_debate_rounds=1)
    base.update(overrides)
    return DeliberativeConfig(**base)


def _mock_client_with_responses(contents: list[str | Exception]) -> Any:
    """Build a mock OpenAI-compatible client.

    ``contents`` is a list of either string responses (returned as the
    LLM's message.content) or Exception instances (raised on call).
    """
    client = MagicMock(name="openai_client")
    call_iter = iter(contents)

    def _create(**_kwargs: Any) -> Any:
        nxt = next(call_iter)
        if isinstance(nxt, Exception):
            raise nxt
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = nxt
        return resp

    client.chat.completions.create.side_effect = _create
    return client


def _bull_json(stance: str = "long the breakout", conf: float = 0.7) -> str:
    payload = BullBearTurn(
        role="bull_researcher",
        stance=stance,
        confidence=conf,
        rationale="Strong trend with rising volume; analyst ta confirms.",
        key_evidence=["ta"],
        counterarguments="Bear will note macro overhang.",
        metadata={"tier": "quick"},
    )
    return payload.model_dump_json()


def _bear_json(conf: float = 0.4) -> str:
    payload = BullBearTurn(
        role="bear_researcher",
        stance="cautious; macro headwinds",
        confidence=conf,
        rationale="Macro overhang, weak follow-through.",
        key_evidence=["ta"],
        counterarguments="Bull will cite breakout volume.",
        metadata={"tier": "quick"},
    )
    return payload.model_dump_json()


def _judge_json(rec: str = "Overweight", overrules: bool = False) -> str:
    payload = ResearchPlan(
        recommendation=rec,  # type: ignore[arg-type]
        confidence=0.65,
        rationale="Bull case stronger than bear case net of evidence.",
        overrules_baseline=overrules,
        strategic_actions="Enter on close above prior high; stop below day low.",
        horizon_emphasis="1d",
        metadata={"tier": "deep"},
    )
    return payload.model_dump_json()


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


def test_returns_empty_when_llm_disabled() -> None:
    cfg = DeliberativeConfig(enable_llm_turns=False)
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=MagicMock(),  # client provided but feature off
    )
    assert out == []


def test_drops_turn_on_llm_exception_then_bails_after_two_consecutive() -> None:
    """First and second turns both raise -> two consecutive failures -> bail."""
    cfg = _config()
    client = _mock_client_with_responses(
        [TimeoutError("network"), TimeoutError("network"), TimeoutError("never")]
    )
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert out == []
    # The second failure should trigger the bail; we should NOT have made
    # a third call (the bull and bear roles for round 1).
    assert client.chat.completions.create.call_count == 2


def test_drops_turn_on_invalid_json() -> None:
    """LLM returns garbage; turn is dropped, second turn succeeds, third
    succeeds — final list has 2 turns (bear + judge), not 3."""
    cfg = _config()
    client = _mock_client_with_responses(
        ["not json {{{ broken", _bear_json(), _judge_json()]
    )
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    # Bull dropped, bear OK, judge OK.
    assert len(out) == 2
    assert out[0].role == "bear_researcher"
    assert out[1].role == "portfolio_manager"  # judge maps onto pm slot
    assert out[1].metadata["logical_role"] == "research_manager"


def test_drops_turn_on_pydantic_validation_failure() -> None:
    """LLM returns valid JSON but with confidence > 1.0 (out of range)."""
    bad = '{"role":"bull_researcher","stance":"x","confidence":2.5,'
    bad += '"rationale":"r","key_evidence":[],"counterarguments":"c","metadata":{}}'
    cfg = _config()
    client = _mock_client_with_responses([bad, _bear_json(), _judge_json()])
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(out) == 2
    assert all(t.role != "bull_researcher" for t in out)


def test_pydantic_strict_rejects_missing_required_field() -> None:
    """Pydantic validation MUST be strict -- missing rationale -> drop."""
    bad = '{"role":"bull_researcher","stance":"x","confidence":0.5,'
    bad += '"key_evidence":[],"counterarguments":"c","metadata":{}}'
    cfg = _config()
    client = _mock_client_with_responses([bad, _bear_json(), _judge_json()])
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(out) == 2


def test_happy_path_emits_three_turns_with_prompt_hash() -> None:
    cfg = _config()
    client = _mock_client_with_responses([_bull_json(), _bear_json(), _judge_json()])
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(out) == 3
    bull, bear, judge = out
    assert bull.role == "bull_researcher" and bull.tier == "quick"
    assert bear.role == "bear_researcher" and bear.tier == "quick"
    assert judge.role == "portfolio_manager" and judge.tier == "deep"
    for t in out:
        assert isinstance(t.metadata, dict)
        assert "prompt_hash" in t.metadata
        assert len(t.metadata["prompt_hash"]) == 64  # sha256 hex
        assert t.metadata["model_id"]


def test_consecutive_failures_in_judge_does_not_propagate_exception() -> None:
    """Bull OK, bear OK, judge raises -- only one failure, but the run
    still succeeds with two turns and the judge dropped."""
    cfg = _config()
    client = _mock_client_with_responses(
        [_bull_json(), _bear_json(), RuntimeError("boom")]
    )
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(out) == 2
    assert {t.role for t in out} == {"bull_researcher", "bear_researcher"}


def test_strict_pydantic_rejects_wrong_role_literal() -> None:
    """LLM hallucinates a role that doesn't match the request -> dropped."""
    wrong = '{"role":"bear_researcher","stance":"x","confidence":0.5,'
    wrong += '"rationale":"r","key_evidence":[],"counterarguments":"c","metadata":{}}'
    cfg = _config()
    # First call asks bull_researcher; LLM returns bear_researcher payload.
    client = _mock_client_with_responses([wrong, _bear_json(), _judge_json()])
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    # Bull turn dropped because role mismatch.
    assert all(t.role != "bull_researcher" for t in out)


def test_returns_empty_when_no_api_key_and_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _config()
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=None,
    )
    assert out == []


def test_deterministic_aggregator_consumes_emitted_turns() -> None:
    """Smoke test: emitted turns survive the deterministic aggregator's
    intake validators (tier-split, msg-clear, turn-cap).

    Note: the deterministic skeleton already emits one bull + one bear turn
    (count = 2 = 2 * max_debate_rounds=1), so additional bull/bear LLM
    turns are dropped by the cap. We bump max_debate_rounds=3 here so the
    LLM bull/bear turns survive the cap. The deep-tier judge always
    survives (different role).
    """
    from hermes_quant.aggregators.deliberative import DeliberativeCommitteeAggregator

    cfg = _config()
    client = _mock_client_with_responses([_bull_json(), _bear_json(), _judge_json()])
    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(turns) == 3

    # Push them through the deterministic aggregator's intake.
    ctx = _ctx()
    from dataclasses import replace as _replace

    ctx_with_turns = _replace(ctx, extras={"committee_turns": list(turns)})
    # Bump max_debate_rounds so the LLM bull/bear turns survive the cap
    # (the skeleton already emits 1 bull + 1 bear toward the cap).
    agg = DeliberativeCommitteeAggregator(max_debate_rounds=3)
    out = agg.aggregate([_view("ta", 1), _view("ms", 1)], ctx_with_turns)
    model_backed = out.metadata["committee"]["model_backed_turns"]
    assert any(t["role"] == "bull_researcher" for t in model_backed)
    assert any(t["role"] == "bear_researcher" for t in model_backed)
    # Judge survives because it occupies the deep-tier portfolio_manager slot
    # (no quick-tier rejection, no bull/bear cap).
    judge_turns = [t for t in model_backed if t["role"] == "portfolio_manager"]
    assert len(judge_turns) >= 1
    assert all("prompt_hash" in (t["metadata"] or {}) for t in model_backed)
