"""Tests for TradingAgents-style deliberative committee aggregator (ADR-0023)."""
from __future__ import annotations

import pandas as pd

from hermes_quant.aggregators.deliberative import CommitteeTurn, DeliberativeCommitteeAggregator
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx(extras=None):
    ts = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    bars = pd.DataFrame({
        "timestamp": ts,
        "open": [100, 101, 102, 103, 104],
        "high": [101, 102, 103, 104, 105],
        "low": [99, 100, 101, 102, 103],
        "close": [100, 101, 102, 103, 104],
        "volume": [1000] * 5,
    })
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


def test_deliberative_committee_accepts_aligned_views_and_records_trace():
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate([
        _view("classical_ta", 1, 0.8),
        _view("microstructure_lite", 1, 0.7),
    ], _ctx())
    assert signal.aggregator == "deliberative_committee"
    assert signal.direction == 1
    committee = signal.metadata["committee"]
    assert committee["decision"] == "accepted"
    assert committee["n_effective_views"] == 2
    assert "portfolio_manager" in committee["roles"]
    assert committee["safety"]["disagreement_reduces_confidence"] is True


def test_deliberative_committee_flats_on_insufficient_effective_views():
    agg = DeliberativeCommitteeAggregator(min_effective_views=2)
    signal = agg.aggregate([_view("classical_ta", 1, 0.8)], _ctx())
    assert signal.direction == 0
    assert signal.confidence == 0.0
    assert signal.metadata["committee"]["decision"] == "insufficient_effective_views"


def test_deliberative_committee_penalizes_disagreement():
    agg = DeliberativeCommitteeAggregator(disagreement_penalty=0.25)
    aligned = agg.aggregate([
        _view("a", 1, 0.8),
        _view("b", 1, 0.8),
    ], _ctx())
    split = agg.aggregate([
        _view("a", 1, 0.8),
        _view("b", -1, 0.6),
        _view("c", 1, 0.4),
    ], _ctx())
    assert split.metadata["committee"]["disagreement_score"] > 0
    assert split.confidence <= aligned.confidence


def test_deliberative_committee_includes_model_backed_turn_artifacts():
    turn = CommitteeTurn(
        role="portfolio_manager",
        stance="model_synthesis",
        direction=1,
        confidence=0.66,
        rationale="external model vote",
        model="openrouter:test-model",
        input_hash="abc123",
    )
    agg = DeliberativeCommitteeAggregator()
    signal = agg.aggregate([
        _view("classical_ta", 1, 0.8),
        _view("hermes_semantic", 1, 0.7),
    ], _ctx(extras={"committee_turns": [turn]}))
    model_turns = signal.metadata["committee"]["model_backed_turns"]
    assert len(model_turns) == 1
    assert model_turns[0]["model"] == "openrouter:test-model"


def test_deliberative_committee_semantic_alignment_can_add_small_bonus():
    agg = DeliberativeCommitteeAggregator(semantic_bonus_cap=0.05)
    no_semantic = agg.aggregate([
        _view("classical_ta", 1, 0.8),
        _view("microstructure_lite", 1, 0.8),
    ], _ctx())
    with_semantic = agg.aggregate([
        _view("classical_ta", 1, 0.8),
        _view("microstructure_lite", 1, 0.8),
        _view("hermes_semantic", 1, 0.7),
    ], _ctx())
    assert with_semantic.confidence >= no_semantic.confidence
