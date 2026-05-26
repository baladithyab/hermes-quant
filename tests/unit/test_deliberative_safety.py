"""Tests for TradingAgents safety patterns added to the deliberative committee.

Three patterns:
  1. Two-tier LLM split (quick / deep / deterministic) with rejection of
     quick-tier turns bound to deep-required roles (trader, portfolio_manager).
  2. Bull/bear deterministic turn cap (count >= 2 * max_debate_rounds).
  3. msg-clear at intake boundary: upstream agent-context keys (messages,
     tool_calls, context_messages, prior_messages) are stripped from inbound
     turn metadata.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.aggregators.deliberative import (
    CommitteeTurn,
    DeliberativeCommitteeAggregator,
)
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx(extras=None):
    ts = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100, 101, 102, 103, 104],
            "high": [101, 102, 103, 104, 105],
            "low": [99, 100, 101, 102, 103],
            "close": [100, 101, 102, 103, 104],
            "volume": [1000] * 5,
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="kraken",
        bars=bars,
        last_close=104.0,
        last_volume=1000.0,
        asof=ts[-1],
        extras=extras or {},
    )


def _view(name, direction, confidence=0.8, magnitude=0.01):
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence,
        horizon="1h",
        rationale=f"{name} rationale",
    )


def _aligned_views():
    return [
        _view("classical_ta", 1, 0.8),
        _view("microstructure_lite", 1, 0.7),
    ]


# ---------------------------------------------------------------------------
# 1. CommitteeTurn tier field defaults
# ---------------------------------------------------------------------------
def test_committee_turn_has_tier_field_default_deterministic():
    turn = CommitteeTurn(
        role="bull_researcher",
        stance="bull_case",
        direction=1,
        confidence=0.5,
        rationale="default",
    )
    assert turn.tier == "deterministic"


# ---------------------------------------------------------------------------
# 2. Tier inferred from role when omitted on inbound dict
# ---------------------------------------------------------------------------
def test_committee_turn_tier_inferred_from_role_when_omitted():
    raw_turn = {
        "role": "bull_researcher",
        "stance": "bull_case",
        "direction": 1,
        "confidence": 0.6,
        "rationale": "external bull",
        "model": "openrouter:test",
    }
    agg = DeliberativeCommitteeAggregator(max_debate_rounds=3)  # raise cap to admit
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": [raw_turn]}))
    model_turns = signal.metadata["committee"]["model_backed_turns"]
    bull_model_turns = [t for t in model_turns if t["role"] == "bull_researcher"]
    assert len(bull_model_turns) == 1
    assert bull_model_turns[0]["tier"] == "quick"


# ---------------------------------------------------------------------------
# 3. Quick-tier portfolio_manager turn rejected
# ---------------------------------------------------------------------------
def test_committee_turn_quick_tier_rejected_for_portfolio_manager():
    rejected = {
        "role": "portfolio_manager",
        "stance": "model_synthesis",
        "direction": 1,
        "confidence": 0.7,
        "rationale": "quick model trying to act as PM",
        "model": "openrouter:cheap",
        "tier": "quick",
    }
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": [rejected]}))
    pm_turns = [
        t for t in signal.metadata["committee"]["turns"] if t["role"] == "portfolio_manager"
    ]
    # Only the deterministic PM should remain.
    assert len(pm_turns) == 1
    assert pm_turns[0]["model"].startswith("deterministic:")
    assert pm_turns[0]["tier"] == "deterministic"
    # And the rejected one didn't sneak into model_backed_turns either.
    pm_model_turns = [
        t
        for t in signal.metadata["committee"]["model_backed_turns"]
        if t["role"] == "portfolio_manager"
    ]
    assert pm_model_turns == []


# ---------------------------------------------------------------------------
# 4. Quick-tier trader turn rejected
# ---------------------------------------------------------------------------
def test_committee_turn_quick_tier_rejected_for_trader():
    rejected = {
        "role": "trader",
        "stance": "provisional_signal",
        "direction": 1,
        "confidence": 0.65,
        "rationale": "quick model trying to act as trader",
        "model": "openrouter:cheap",
        "tier": "quick",
    }
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": [rejected]}))
    trader_turns = [t for t in signal.metadata["committee"]["turns"] if t["role"] == "trader"]
    assert len(trader_turns) == 1
    assert trader_turns[0]["model"].startswith("deterministic:")
    assert trader_turns[0]["tier"] == "deterministic"


# ---------------------------------------------------------------------------
# 5. Deep-tier portfolio_manager turn IS accepted; safety flag still asserted
# ---------------------------------------------------------------------------
def test_committee_turn_deep_tier_accepted_for_portfolio_manager():
    accepted = {
        "role": "portfolio_manager",
        "stance": "model_synthesis",
        "direction": 1,
        "confidence": 0.7,
        "rationale": "deep model is the right tier for PM",
        "model": "openrouter:deep",
        "tier": "deep",
    }
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": [accepted]}))
    pm_model_turns = [
        t
        for t in signal.metadata["committee"]["model_backed_turns"]
        if t["role"] == "portfolio_manager"
    ]
    assert len(pm_model_turns) == 1
    assert pm_model_turns[0]["tier"] == "deep"
    assert signal.metadata["committee"]["safety"]["tier_split_enforced"] is True


# ---------------------------------------------------------------------------
# 6. Bull/bear cap drops extra turns
# ---------------------------------------------------------------------------
def test_debate_turn_cap_drops_extra_bull_bear():
    # max_debate_rounds=2 -> cap = 4 total bull+bear turns.
    # Deterministic scaffold contributes 2 (1 bull + 1 bear).
    # Inbound: 4 bull/bear turns. Expect 2 accepted, 2 dropped.
    inbound = [
        {
            "role": "bull_researcher",
            "stance": "bull_case",
            "direction": 1,
            "confidence": 0.6,
            "rationale": f"inbound bull #{i}",
            "model": "openrouter:bull",
            "tier": "quick",
        }
        for i in range(2)
    ] + [
        {
            "role": "bear_researcher",
            "stance": "bear_case",
            "direction": -1,
            "confidence": 0.6,
            "rationale": f"inbound bear #{i}",
            "model": "openrouter:bear",
            "tier": "quick",
        }
        for i in range(2)
    ]
    agg = DeliberativeCommitteeAggregator(max_debate_rounds=2)
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": inbound}))
    committee = signal.metadata["committee"]
    bull_bear_total = sum(
        1 for t in committee["turns"] if t["role"] in ("bull_researcher", "bear_researcher")
    )
    # Cap = 4 total bull+bear turns.
    assert bull_bear_total == 4
    assert committee["safety"]["dropped_turns"] >= 2
    assert committee["safety"]["turn_cap_active"] is True
    assert committee["safety"]["max_debate_rounds"] == 2


# ---------------------------------------------------------------------------
# 7. Cap doesn't affect non-bull/bear roles
# ---------------------------------------------------------------------------
def test_debate_turn_cap_does_not_affect_neutral_or_risk_roles():
    inbound = [
        {
            "role": "risk_conservative",
            "stance": "extra_risk_view",
            "direction": 0,
            "confidence": 0.5,
            "rationale": f"inbound risk #{i}",
            "model": "openrouter:risk",
            "tier": "quick",
        }
        for i in range(5)
    ]
    agg = DeliberativeCommitteeAggregator(max_debate_rounds=1)
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": inbound}))
    committee = signal.metadata["committee"]
    risk_model_turns = [
        t for t in committee["model_backed_turns"] if t["role"] == "risk_conservative"
    ]
    assert len(risk_model_turns) == 5
    assert committee["safety"]["dropped_turns"] == 0


# ---------------------------------------------------------------------------
# 8. msg-clear strips upstream-context keys from inbound metadata
# ---------------------------------------------------------------------------
def test_msg_clear_strips_messages_from_inbound_metadata():
    inbound = {
        "role": "bull_researcher",
        "stance": "bull_case",
        "direction": 1,
        "confidence": 0.6,
        "rationale": "inbound bull with leaky metadata",
        "model": "openrouter:bull",
        "tier": "quick",
        "metadata": {
            "messages": [{"role": "user", "content": "leak"}],
            "tool_calls": [{"name": "search"}],
            "context_messages": ["upstream context"],
            "prior_messages": ["earlier"],
            "rationale_extra": "keep",
        },
    }
    agg = DeliberativeCommitteeAggregator(max_debate_rounds=2)
    signal = agg.aggregate(_aligned_views(), _ctx(extras={"committee_turns": [inbound]}))
    bull_model_turns = [
        t
        for t in signal.metadata["committee"]["model_backed_turns"]
        if t["role"] == "bull_researcher"
    ]
    assert len(bull_model_turns) == 1
    md = bull_model_turns[0]["metadata"] or {}
    assert "messages" not in md
    assert "tool_calls" not in md
    assert "context_messages" not in md
    assert "prior_messages" not in md
    assert md.get("rationale_extra") == "keep"


# ---------------------------------------------------------------------------
# 9. safety metadata block exposes the three new flags
# ---------------------------------------------------------------------------
def test_safety_metadata_block_includes_three_new_flags():
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate(_aligned_views(), _ctx())
    safety = signal.metadata["committee"]["safety"]
    assert safety["tier_split_enforced"] is True
    assert safety["turn_cap_active"] is True
    assert safety["msg_clear_enforced"] is True
