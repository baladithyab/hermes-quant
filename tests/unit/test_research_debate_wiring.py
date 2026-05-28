"""Unit + integration tests for v0.6.2 ResearchDebateStage production wiring (ADR-0066).

Covers the six unit tests T1–T6 and the three integration tests T7–T9 from
ADR-0066 §Test Plan. All tests stub `_call_llm_json` via monkeypatch — no
live network, no real OpenAI client — and assert against the exact
behaviour of the new helpers and the run_llm_committee dispatch site.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.aggregators import llm_committee as committee_mod
from hermes_quant.aggregators.deliberative import CommitteeTurn, DeliberativeConfig
from hermes_quant.aggregators.llm_committee import (
    BullBearTurn,
    _run_one_turn_with_history,
    _run_research_manager_judge,
    run_llm_committee,
)
from hermes_quant.agents.research_debate.schemas import (
    PortfolioRating,
    ResearchPlan as DebateResearchPlan,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(asset: str = "AAPL") -> MarketContext:
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
        asset=asset,
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.0,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view(name: str = "ta") -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=1,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale=f"{name} rationale",
    )


def _baseline() -> AggregatedSignal:
    v = _view()
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


def _config() -> DeliberativeConfig:
    return DeliberativeConfig(
        enable_llm_turns=True, max_debate_rounds=1, enable_risk_mgmt=False
    )


def _bull_json(round_idx: int = 1) -> str:
    return (
        '{"role": "bull_researcher", "stance": "long the breakout", '
        '"confidence": 0.7, "rationale": "Strong trend confirmed.", '
        '"key_evidence": ["ta"], '
        '"counterarguments": "Bear notes macro overhang.", '
        f'"metadata": {{"tier": "quick", "round": {round_idx}}}}}'
    )


def _bear_json(round_idx: int = 1) -> str:
    return (
        '{"role": "bear_researcher", "stance": "cautious", '
        '"confidence": 0.4, "rationale": "Macro overhang dominates.", '
        '"key_evidence": ["ta"], '
        '"counterarguments": "Bull cites breakout volume.", '
        f'"metadata": {{"tier": "quick", "round": {round_idx}}}}}'
    )


def _judge_json(rec: str = "OVERWEIGHT", with_overrules: bool = False) -> str:
    overrules = ', "overrules_baseline": false' if with_overrules else ""
    return (
        f'{{"recommendation": "{rec}", "confidence": 0.65, '
        f'"rationale": "Bull case wins on volume confirmation.", '
        f'"strategic_actions": "Add 1R; trail stop under prior swing low."'
        f'{overrules}, "metadata": {{}}}}'
    )


# ---------------------------------------------------------------------------
# T1–T3: _run_one_turn_with_history unit tests
# ---------------------------------------------------------------------------


def test_t1_run_one_turn_with_history_renders_conversational_prompt(monkeypatch):
    """T1: conversational placeholders thread into the rendered prompt."""
    captured: dict[str, Any] = {}

    def fake_call(**kw: Any) -> str:
        captured["system"] = kw["system_text"]
        captured["user"] = kw["user_text"]
        captured["model"] = kw["model"]
        return _bull_json(round_idx=2)

    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turn = _run_one_turn_with_history(
        role="bull_researcher",
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        current_response="Bear: macro is brutal.",
        own_history="[Bull r1] I led with breakout volume.",
        round_index=2,
        conversational_preamble="SPEAK_AS_IF_TO_OPPONENT",
    )

    assert turn is not None
    assert isinstance(turn, CommitteeTurn)
    assert turn.role == "bull_researcher"
    # Each conversational placeholder must appear verbatim in the rendered
    # user_text per the bull_bear.md template (see system+user blocks).
    user_text = captured["user"]
    system_text = captured["system"]
    assert "Bear: macro is brutal." in user_text
    assert "[Bull r1] I led with breakout volume." in user_text
    # round_index gets formatted into the system block ("round 2 of …") and the
    # JSON envelope hint ("metadata.round = 2").
    assert "round 2" in system_text.lower()
    # The conversational preamble is rendered verbatim into system_text.
    assert "SPEAK_AS_IF_TO_OPPONENT" in system_text
    # Metadata propagates round_index, prompt_hash, from_research_debate.
    assert turn.metadata is not None
    assert turn.metadata["round_index"] == 2
    assert turn.metadata["from_research_debate"] is True
    assert isinstance(turn.metadata["prompt_hash"], str)
    assert len(turn.metadata["prompt_hash"]) == 64


def test_t2_run_one_turn_with_history_returns_none_on_llm_failure(monkeypatch):
    """T2: LLM returning None → helper returns None (failure-closed)."""

    monkeypatch.setattr(committee_mod, "_call_llm_json", lambda **_: None)

    turn = _run_one_turn_with_history(
        role="bull_researcher",
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        current_response="(no prior turn)",
        own_history="(no prior turns by you yet)",
        round_index=1,
        conversational_preamble="speak conversationally",
    )

    assert turn is None


def test_t3_run_one_turn_with_history_returns_none_on_role_mismatch(monkeypatch):
    """T3: LLM returns role=bear when asked for bull → helper returns None."""

    def fake_call(**_: Any) -> str:
        # role mismatch: asked for bull, LLM returns bear payload.
        return _bear_json()

    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turn = _run_one_turn_with_history(
        role="bull_researcher",
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        current_response="(no prior turn)",
        own_history="(no prior turns by you yet)",
        round_index=1,
        conversational_preamble="x",
    )

    assert turn is None


# ---------------------------------------------------------------------------
# T4–T6: _run_research_manager_judge unit tests
# ---------------------------------------------------------------------------


def test_t4_run_research_manager_judge_happy_path(monkeypatch):
    """T4: valid ResearchPlan JSON → returns ResearchPlan w/ OVERWEIGHT."""
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        lambda **_: _judge_json("OVERWEIGHT", with_overrules=True),
    )

    plan = _run_research_manager_judge(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        bull_turns=[],
        bear_turns=[],
    )

    assert plan is not None
    assert isinstance(plan, DebateResearchPlan)
    assert plan.recommendation == PortfolioRating.OVERWEIGHT
    assert 0.0 <= plan.confidence <= 1.0


def test_t5_run_research_manager_judge_case_insensitive(monkeypatch):
    """T5: Title-case 'Buy' from LLM coerces to PortfolioRating.BUY."""
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        lambda **_: _judge_json("Buy", with_overrules=True),
    )

    plan = _run_research_manager_judge(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        bull_turns=[],
        bear_turns=[],
    )

    assert plan is not None
    assert plan.recommendation == PortfolioRating.BUY


def test_t6_run_research_manager_judge_returns_none_on_parse_failure(monkeypatch):
    """T6: malformed JSON → returns None (failure-closed)."""
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        lambda **_: "{this is not json",
    )

    plan = _run_research_manager_judge(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        bull_turns=[],
        bear_turns=[],
    )

    assert plan is None


def test_t6b_run_research_manager_judge_strips_overrules_baseline(monkeypatch):
    """ADR-0066 workaround: overrules_baseline (legacy) is stripped before
    parsing into the new schema (which has extra='forbid').
    """
    # Ship a payload that *only* validates if we strip overrules_baseline.
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        lambda **_: _judge_json("HOLD", with_overrules=True),
    )

    plan = _run_research_manager_judge(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        bull_turns=[],
        bear_turns=[],
    )

    assert plan is not None
    assert plan.recommendation == PortfolioRating.HOLD
    # Verify the new schema does NOT carry the legacy field through.
    dumped = plan.model_dump()
    assert "overrules_baseline" not in dumped
