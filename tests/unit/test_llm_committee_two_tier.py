"""Two-tier LLM split tests (ADR-0037 §"Two-tier LLM split is hard").

The LLM committee must use:
  * config.quick_model for bull/bear/risk_* roles
  * config.deep_model for research_manager and portfolio_manager

A quick-tier model bound (by configuration error) to a deep-required role
must surface as a config-validation problem at the deterministic-aggregator
intake (it rejects the turn at runtime), and the LLM caller must NOT
silently downgrade.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd

from hermes_quant.aggregators.deliberative import (
    DeliberativeCommitteeAggregator,
    DeliberativeConfig,
)
from hermes_quant.aggregators.llm_committee import (
    BullBearTurn,
    ResearchPlan,
    _expected_tier_for_role,
    _model_for_role,
    run_llm_committee,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext


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


def _view(name: str, direction: int) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.7,
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


def _bull_json() -> str:
    return BullBearTurn(
        role="bull_researcher",
        stance="x",
        confidence=0.6,
        rationale="r",
        key_evidence=["ta"],
        counterarguments="c",
        metadata={},
    ).model_dump_json()


def _bear_json() -> str:
    return BullBearTurn(
        role="bear_researcher",
        stance="x",
        confidence=0.4,
        rationale="r",
        key_evidence=["ta"],
        counterarguments="c",
        metadata={},
    ).model_dump_json()


def _judge_json() -> str:
    return ResearchPlan(
        recommendation="Overweight",
        confidence=0.65,
        rationale="r",
        overrules_baseline=False,
        strategic_actions="s",
        horizon_emphasis="1d",
        metadata={},
    ).model_dump_json()


def _mock_recording_client(contents: list[str]) -> tuple[Any, list[str]]:
    """Build a mock client that records the ``model`` argument of every call."""
    client = MagicMock(name="openai_client")
    call_iter = iter(contents)
    models_called: list[str] = []

    def _create(*, model: str, **_kwargs: Any) -> Any:
        models_called.append(model)
        nxt = next(call_iter)
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = nxt
        return resp

    client.chat.completions.create.side_effect = _create
    return client, models_called


def test_quick_model_used_for_bull_and_bear_deep_for_judge() -> None:
    cfg = DeliberativeConfig(
        enable_llm_turns=True,
        quick_model="quick-model-X",
        deep_model="deep-model-Y",
    )
    client, models_called = _mock_recording_client(
        [_bull_json(), _bear_json(), _judge_json()]
    )
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    assert len(out) == 3
    # Order of calls is bull, bear, judge.
    assert models_called == [
        "quick-model-X",  # bull
        "quick-model-X",  # bear
        "deep-model-Y",   # judge
    ]


def test_emitted_turn_tier_matches_role_split() -> None:
    cfg = DeliberativeConfig(
        enable_llm_turns=True,
        quick_model="q",
        deep_model="d",
    )
    client, _ = _mock_recording_client([_bull_json(), _bear_json(), _judge_json()])
    out = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view("ta", 1)],
        baseline_signal=_baseline(),
        config=cfg,
        client=client,
    )
    bull, bear, judge = out
    assert bull.tier == "quick"
    assert bear.tier == "quick"
    assert judge.tier == "deep"
    # Metadata mirrors tier and records the model_id used.
    assert bull.metadata["tier"] == "quick" and bull.metadata["model_id"] == "q"
    assert bear.metadata["tier"] == "quick" and bear.metadata["model_id"] == "q"
    assert judge.metadata["tier"] == "deep" and judge.metadata["model_id"] == "d"


def test_role_to_model_mapping_helpers_are_consistent() -> None:
    cfg = DeliberativeConfig(
        enable_llm_turns=True,
        quick_model="QM",
        deep_model="DM",
    )
    assert _expected_tier_for_role("bull_researcher") == "quick"
    assert _expected_tier_for_role("bear_researcher") == "quick"
    assert _expected_tier_for_role("risk_aggressive") == "quick"
    assert _expected_tier_for_role("risk_conservative") == "quick"
    assert _expected_tier_for_role("risk_neutral") == "quick"
    assert _expected_tier_for_role("research_manager") == "deep"
    assert _expected_tier_for_role("portfolio_manager") == "deep"

    assert _model_for_role("bull_researcher", cfg) == "QM"
    assert _model_for_role("bear_researcher", cfg) == "QM"
    assert _model_for_role("risk_neutral", cfg) == "QM"
    assert _model_for_role("research_manager", cfg) == "DM"
    assert _model_for_role("portfolio_manager", cfg) == "DM"


def test_deterministic_aggregator_rejects_quick_tier_judge() -> None:
    """If a judge turn is mistakenly emitted with tier='quick' (e.g. by a
    buggy alternative caller), the deterministic aggregator's intake
    rejects it. This test confirms that the safety net downstream of the
    LLM caller is intact -- the LLM caller cannot silently downgrade by
    construction (it always sets tier='deep' on the judge), and even if it
    DID, the aggregator would catch it.
    """
    from dataclasses import replace as _replace

    from hermes_quant.aggregators.deliberative import CommitteeTurn

    bad_judge = CommitteeTurn(
        role="portfolio_manager",
        stance="judge:Overweight",
        direction=1,
        confidence=0.6,
        rationale="should be deep but isn't",
        model="llm:quick-model-X",
        tier="quick",  # <- intentional violation
        metadata={"prompt_hash": "x" * 64, "model_id": "quick-model-X"},
    )
    ctx = _ctx()
    ctx_with = _replace(ctx, extras={"committee_turns": [bad_judge]})
    agg = DeliberativeCommitteeAggregator()
    out = agg.aggregate([_view("ta", 1), _view("ms", 1)], ctx_with)
    # The bad judge should be absent from model_backed_turns (rejected at
    # intake by the tier-split filter).
    model_backed = out.metadata["committee"]["model_backed_turns"]
    assert all(
        not (t["role"] == "portfolio_manager" and t.get("tier") == "quick")
        for t in model_backed
    )


def test_quick_eq_deep_warns_but_does_not_raise(caplog: Any) -> None:
    """Operator may set quick_model == deep_model intentionally (single-model
    deployment). It is wasteful but allowed -- a warning is emitted, no
    raise."""
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = DeliberativeConfig(
            enable_llm_turns=True,
            quick_model="same-model",
            deep_model="same-model",
        )
    assert cfg.quick_model == "same-model"
    assert cfg.deep_model == "same-model"
    assert any("two-tier split provides no cost benefit" in r.message for r in caplog.records)
