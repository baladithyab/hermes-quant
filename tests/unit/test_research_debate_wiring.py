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
# Test pollution fix (v0.6.2): isolate env-var state per test.
# Previously full-suite runs leaked HERMES_QUANT_RESEARCH_DEBATE / _ROUNDS
# from earlier tests, causing test_t4_run_research_manager_judge_happy_path
# to fail in the full suite while passing in isolation.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_research_debate_env(monkeypatch):
    """Ensure each test sees clean env state for HERMES_QUANT_RESEARCH_DEBATE flags."""
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE", raising=False)
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE_ROUNDS", raising=False)
    # W7 (ADR-0080): keep the red-team turn OFF so T1–T11 stay byte-identical
    # (off-state) regardless of suite ordering.
    monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)
    yield


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


# ---------------------------------------------------------------------------
# T7–T9: run_llm_committee dispatch integration tests
# ---------------------------------------------------------------------------


def _alternating_call_factory(judge_payload: str | None = None):
    """Build a fake _call_llm_json that returns bull/bear/judge JSON in order.

    Per the stage runner's loop, the call order under max_debate_rounds=1 is:
      1. bull_researcher (round 1)
      2. bear_researcher (round 1)
      3. research_manager (judge)
    """
    sequence: list[str] = []

    def factory(rounds: int = 1, judge: str | None = None) -> Any:
        nonlocal sequence
        sequence = []
        for r in range(1, rounds + 1):
            sequence.append(_bull_json(round_idx=r))
            sequence.append(_bear_json(round_idx=r))
        if judge is not None:
            sequence.append(judge)
        idx = {"i": 0}

        def fake_call(**_: Any) -> str | None:
            if idx["i"] >= len(sequence):
                return None
            out = sequence[idx["i"]]
            idx["i"] += 1
            return out

        return fake_call

    return factory(rounds=1, judge=judge_payload)


def test_t7_run_llm_committee_dispatches_to_research_debate_when_flag_on(
    monkeypatch,
):
    """T7: HERMES_QUANT_RESEARCH_DEBATE=1 → dispatch path runs end-to-end and
    emits bull_researcher + bear_researcher + portfolio_manager(judge) turns
    with the expected metadata shape.
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")
    fake_call = _alternating_call_factory(
        judge_payload=_judge_json("OVERWEIGHT", with_overrules=True)
    )
    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )

    roles = [t.role for t in turns]
    assert "bull_researcher" in roles
    assert "bear_researcher" in roles
    # The judge maps to the deep-tier portfolio_manager slot.
    assert "portfolio_manager" in roles

    bull = next(t for t in turns if t.role == "bull_researcher")
    bear = next(t for t in turns if t.role == "bear_researcher")
    judge = next(t for t in turns if t.role == "portfolio_manager")

    assert bull.tier == "quick"
    assert bear.tier == "quick"
    assert judge.tier == "deep"

    assert bull.metadata is not None
    assert bull.metadata.get("from_research_debate") is True
    assert bear.metadata is not None
    assert bear.metadata.get("from_research_debate") is True
    assert judge.metadata is not None
    assert judge.metadata.get("from_research_debate") is True
    assert judge.metadata.get("logical_role") == "research_manager"
    assert judge.metadata.get("recommendation") == "OVERWEIGHT"
    assert judge.stance == "judge:OVERWEIGHT"
    # OVERWEIGHT → +1 direction (verifies the extended _direction_from_recommendation).
    assert judge.direction == 1


def test_t8_run_llm_committee_falls_through_when_flag_off(monkeypatch):
    """T8: flag unset → legacy parallel-emit path runs (no research_debate
    metadata flag on emitted turns).
    """
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE", raising=False)
    # Legacy path also calls bull/bear/judge in order, so reuse the fixture.
    fake_call = _alternating_call_factory(
        judge_payload=_judge_json("HOLD", with_overrules=True)
    )
    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )

    # At least one turn should NOT carry from_research_debate.
    assert turns
    assert all(
        not (t.metadata or {}).get("from_research_debate", False) for t in turns
    )


def test_t9_run_llm_committee_falls_through_on_research_debate_exception(
    monkeypatch,
):
    """T9: flag ON but the stage raises → fall through to legacy emit path.

    We force the dispatch to raise by monkey-patching run_research_debate
    inside the lazily-imported stage module to a bomb. Since the import is
    inside the try-block, patching the module attribute *after* the first
    import works for subsequent calls.
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")

    # Force an exception inside the dispatch.
    from hermes_quant.agents.research_debate import stage as stage_mod

    def _bomb(*_a: Any, **_kw: Any):
        raise RuntimeError("synthetic dispatch failure")

    monkeypatch.setattr(stage_mod, "run_research_debate", _bomb)

    # Legacy path will then take over and call _run_one_turn for each role.
    fake_call = _alternating_call_factory(
        judge_payload=_judge_json("HOLD", with_overrules=True)
    )
    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )

    # We should have legacy turns (no from_research_debate flag) — the
    # dispatch crashed before it could append any debate turns.
    assert turns
    for t in turns:
        assert not (t.metadata or {}).get("from_research_debate", False)


# ---------------------------------------------------------------------------
# T10: dispatch preserves prompt_hash + round_index (C1+C3 regression).
# ---------------------------------------------------------------------------


def test_t10_dispatch_preserves_prompt_hash_and_round_index(monkeypatch):
    """T10 (v0.6.2-fix-C1): dispatch path must carry prompt_hash + round_index
    from the stage helper through state.bull_turns into CommitteeTurn metadata.

    Pre-fix: state.bull_turns retained only the BullBearTurn (no helper
    forensics), so the dispatch-side CommitteeTurn metadata had no
    prompt_hash and no round_index — committee_turn audit rows lost both.
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")
    fake_call = _alternating_call_factory(
        judge_payload=_judge_json("OVERWEIGHT", with_overrules=True)
    )
    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )

    assert turns
    # Dispatch order: all bull_turns first, then bear_turns, then judge.
    bull = next(t for t in turns if t.role == "bull_researcher")
    bear = next(t for t in turns if t.role == "bear_researcher")

    assert bull.metadata is not None
    assert bear.metadata is not None

    # prompt_hash is a non-empty 64-char hex per _prompt_hash convention.
    bull_hash = bull.metadata.get("prompt_hash")
    bear_hash = bear.metadata.get("prompt_hash")
    assert isinstance(bull_hash, str) and len(bull_hash) == 64
    assert isinstance(bear_hash, str) and len(bear_hash) == 64

    # round_index reflects the stage-controlled value (1 with max_debate_rounds=1).
    assert bull.metadata.get("round_index") == 1
    assert bear.metadata.get("round_index") == 1

    # Forge-resistance sanity: the structured payload's metadata.round (set by
    # the helper) matches the round_index — even if the LLM stub returned a
    # different value, the helper must overwrite it.
    structured_meta = (bull.metadata.get("structured") or {}).get("metadata") or {}
    assert structured_meta.get("round") == 1


# ---------------------------------------------------------------------------
# T11: judge sees bull/bear context (H2 regression).
# ---------------------------------------------------------------------------


def test_t11_judge_sees_debate_context(monkeypatch):
    """T11 (v0.6.2-fix-H2): _run_research_manager_judge must thread bull/bear
    rationales into the rendered prompt (via synthesized prior_turns).

    Pre-fix: prior_turns=[] discarded all debate context; the judge ran blind.
    Post-fix: bull/bear rationales appear verbatim in the user_text of the
    rendered research_manager prompt (folded in via _serialize_prior_turns).
    """
    captured: dict[str, Any] = {}

    def fake_call(**kw: Any) -> str:
        captured["system"] = kw["system_text"]
        captured["user"] = kw["user_text"]
        return _judge_json("HOLD", with_overrules=True)

    monkeypatch.setattr(committee_mod, "_call_llm_json", fake_call)

    bull_turns = [
        BullBearTurn(
            role="bull_researcher",
            stance="long the breakout",
            confidence=0.7,
            rationale="bull-says-this",
            key_evidence=["ta"],
            counterarguments="bear noise",
        )
    ]
    bear_turns = [
        BullBearTurn(
            role="bear_researcher",
            stance="cautious",
            confidence=0.4,
            rationale="bear-says-that",
            key_evidence=["macro"],
            counterarguments="bull noise",
        )
    ]

    plan = _run_research_manager_judge(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        bull_turns=bull_turns,
        bear_turns=bear_turns,
    )

    assert plan is not None  # judge returned a valid plan

    # The bull/bear rationales must appear in the rendered user_text.
    assert "user" in captured
    user_text = captured["user"]
    assert "bull-says-this" in user_text, (
        "bull rationale not threaded into research_manager prompt"
    )
    assert "bear-says-that" in user_text, (
        "bear rationale not threaded into research_manager prompt"
    )

