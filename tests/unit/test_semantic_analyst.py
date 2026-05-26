"""Tests for Hermes semantic perception packets (ADR-0022)."""

from __future__ import annotations

import pandas as pd

from hermes_quant.analysts.semantic import HermesSemanticAnalyst
from hermes_quant.protocol import MarketContext
from hermes_quant.semantic import semantic_packet_from_dict, validate_semantic_packet


def _ctx(*, extras=None, asof="2024-01-02T00:00:00Z"):
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
        asof=pd.Timestamp(asof),
        extras=extras or {},
    )


def _packet(**overrides):
    payload = {
        "schema_version": 1,
        "asset": "BTC/USDT",
        "asof": "2024-01-01T23:00:00Z",
        "horizon": "1h",
        "stance": "bullish",
        "confidence": 0.75,
        "magnitude": 0.012,
        "summary": "Hermes research packet is bullish.",
        "sources": [{"type": "note", "ref": "unit-test"}],
        "model": "hermes:test-model",
    }
    payload.update(overrides)
    return semantic_packet_from_dict(payload).to_dict()


def test_semantic_packet_hash_validates():
    packet = semantic_packet_from_dict(_packet(), attach_hash=True)
    ok, reason = validate_semantic_packet(
        packet,
        asset="BTC/USDT",
        asof=pd.Timestamp("2024-01-02T00:00:00Z"),
    )
    assert ok is True
    assert reason == "ok"
    assert packet.packet_hash == packet.computed_hash


def test_semantic_analyst_emits_view_from_fresh_packet():
    analyst = HermesSemanticAnalyst()
    view = analyst.analyze(_ctx(extras={"semantic_packets": [_packet()]}))
    assert view.analyst == "hermes_semantic"
    assert view.direction == 1
    assert view.confidence == 0.55
    assert view.confidence_raw == 0.75
    assert view.metadata["packet_hash"]
    assert view.metadata["packet_model"] == "hermes:test-model"


def test_semantic_analyst_abstains_when_packet_missing():
    analyst = HermesSemanticAnalyst()
    view = analyst.analyze(_ctx())
    assert view.direction == 0
    assert view.confidence == 0.0
    assert view.metadata["abstain_reason"] == "no_semantic_packets"


def test_semantic_analyst_rejects_future_packet():
    analyst = HermesSemanticAnalyst()
    view = analyst.analyze(
        _ctx(extras={"semantic_packets": [_packet(asof="2024-01-03T00:00:00Z")]})
    )
    assert view.direction == 0
    assert view.metadata["abstain_reason"] == "future_packet"


def test_semantic_analyst_rejects_hash_mismatch():
    analyst = HermesSemanticAnalyst()
    packet = _packet()
    packet["summary"] = "tampered after hash"
    view = analyst.analyze(_ctx(extras={"semantic_packets": [packet]}))
    assert view.direction == 0
    assert view.metadata["abstain_reason"] == "packet_hash_mismatch"


def test_semantic_analyst_selects_latest_valid_packet():
    analyst = HermesSemanticAnalyst()
    old = _packet(asof="2024-01-01T01:00:00Z", stance="bearish", confidence=0.8)
    new = _packet(asof="2024-01-01T23:00:00Z", stance="bullish", confidence=0.7)
    view = analyst.analyze(_ctx(extras={"semantic_packets": [old, new]}))
    assert view.direction == 1
    assert view.confidence_raw == 0.7
