"""B41-e ResearchDebate stage-level budget envelope tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from hermes_quant.agents.llm_budget import BudgetCeilings, LLMBudgetGuard
from hermes_quant.agents.research_debate import stage as stage_mod
from hermes_quant.agents.research_debate.schemas import (
    BullBearTurn,
    PortfolioRating,
    ResearchPlan,
)
from hermes_quant.agents.research_debate.stage import run_research_debate
from hermes_quant.aggregators.deliberative import CommitteeTurn, DeliberativeConfig
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext


@pytest.fixture(autouse=True)
def _isolate_env_and_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_LLM_BUDGET", raising=False)
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)
    monkeypatch.setattr(stage_mod, "_audit_append", lambda **_: None)


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


def _view() -> AnalystView:
    return AnalystView(
        analyst="ta",
        direction=1,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="ta rationale",
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
        enable_llm_turns=True,
        max_debate_rounds=3,
        max_tokens_per_turn=10,
    )


def _guard(tmp_path: Path, *, per_decision_tokens: int) -> LLMBudgetGuard:
    return LLMBudgetGuard(
        ceilings=BudgetCeilings(per_decision_tokens=per_decision_tokens),
        price_table={
            "anthropic/claude-haiku-4.5": (0.001, 0.001),
            "anthropic/claude-sonnet-4.6": (0.001, 0.001),
        },
        path=tmp_path / "spend.json",
    )


def _structured(role: str, idx: int) -> BullBearTurn:
    return BullBearTurn(
        role=role,  # type: ignore[arg-type]
        stance=f"{role}-{idx}",
        confidence=0.7 if role == "bull_researcher" else 0.4,
        rationale=f"{role} rationale {idx}",
        key_evidence=["ta"],
        counterarguments="counter",
        metadata={"tier": "quick"},
    )


def _committee_turn(role: str, idx: int) -> CommitteeTurn:
    structured = _structured(role, idx)
    return CommitteeTurn(
        role=role,  # type: ignore[arg-type]
        stance=structured.stance,
        direction=1 if role == "bull_researcher" else -1,  # type: ignore[arg-type]
        confidence=structured.confidence,
        rationale=structured.rationale,
        model="llm:test",
        input_hash=None,
        metadata={
            "tier": "quick",
            "model_id": "test",
            "structured": structured.model_dump(),
        },
        tier="quick",
    )


def _run_one_tracker(seen: list[tuple[str, int]]):
    def _run_one(*, role: str, config: DeliberativeConfig, **_: Any) -> CommitteeTurn:
        seen.append((role, config.max_tokens_per_turn))
        return _committee_turn(role, len(seen))

    return _run_one


def _judge_tracker(seen: list[int]):
    def _judge(*, config: DeliberativeConfig, **_: Any) -> ResearchPlan:
        seen.append(config.max_tokens_per_turn)
        return ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            confidence=0.65,
            rationale="judge rationale",
            strategic_actions="hold the plan",
            metadata={},
        )

    return _judge


def test_whole_debate_shares_one_budget_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(stage_mod, "_budget_prompt_tokens", lambda **_: 1)
    seen_turns: list[tuple[str, int]] = []
    seen_judge: list[int] = []
    guard = _guard(tmp_path, per_decision_tokens=3 * (1 + 10))

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=object(),
        max_rounds=3,
        proposal_id="debate-budget-shared",
        run_one_turn=_run_one_tracker(seen_turns),
        run_judge=_judge_tracker(seen_judge),
        budget_guard=guard,
    )

    assert state.count == 3
    assert len(state.bull_turns) + len(state.bear_turns) == 3
    assert seen_judge == []
    assert state.terminated_reason.startswith("budget_exhausted:")
    snap = guard.snapshot(
        decision_id="debate-budget-shared",
        tick_id=stage_mod._budget_tick_id(_ctx()),
    )
    assert snap["decision_calls"] == 3
    assert snap["decision_tokens"] <= 33


def test_budget_exhaustion_terminates_mid_debate_with_partial_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(stage_mod, "_budget_prompt_tokens", lambda **_: 1)
    seen_turns: list[tuple[str, int]] = []
    guard = _guard(tmp_path, per_decision_tokens=2 * (1 + 10))

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=object(),
        max_rounds=3,
        proposal_id="debate-budget-partial",
        run_one_turn=_run_one_tracker(seen_turns),
        run_judge=_judge_tracker([]),
        budget_guard=guard,
    )

    assert state.count == 2
    assert len(state.bull_turns) == 1
    assert len(state.bear_turns) == 1
    assert state.judge_decision is None
    assert state.terminated_reason.startswith("budget_exhausted:")
    snap = guard.snapshot(
        decision_id="debate-budget-partial",
        tick_id=stage_mod._budget_tick_id(_ctx()),
    )
    assert snap["decision_calls"] == 2
    assert snap["decision_tokens"] <= 22


def test_zero_ceiling_kill_switch_executes_zero_turns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(stage_mod, "_budget_prompt_tokens", lambda **_: 1)
    seen_turns: list[tuple[str, int]] = []
    seen_judge: list[int] = []
    guard = _guard(tmp_path, per_decision_tokens=0)

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=object(),
        max_rounds=1,
        proposal_id="debate-budget-kill",
        run_one_turn=_run_one_tracker(seen_turns),
        run_judge=_judge_tracker(seen_judge),
        budget_guard=guard,
    )

    assert state.count == 0
    assert seen_turns == []
    assert seen_judge == []
    assert state.judge_decision is None
    assert state.terminated_reason.startswith("budget_exhausted:")
    snap = guard.snapshot(
        decision_id="debate-budget-kill",
        tick_id=stage_mod._budget_tick_id(_ctx()),
    )
    assert snap["decision_calls"] == 0
    assert snap["decision_tokens"] == 0


def test_budget_guard_none_is_byte_identical_to_current_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_QUANT_LLM_BUDGET", raising=False)

    def _run_once():
        return run_research_debate(
            ctx=_ctx(),
            baseline_signal=_baseline(),
            analyst_views=[_view()],
            config=_config(),
            client=object(),
            max_rounds=2,
            proposal_id="debate-budget-off",
            run_one_turn=_run_one_tracker([]),
            run_judge=_judge_tracker([]),
            budget_guard=None,
        )

    baseline = _run_once()
    explicit_none = _run_once()

    assert baseline.count == 4
    assert explicit_none.count == 4
    assert baseline.model_dump(mode="json") == explicit_none.model_dump(mode="json")
