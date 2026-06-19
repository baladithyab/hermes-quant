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


# --- ar50: operator recipe-YAML max_age_minutes (NaN/inf) must NOT silently
# disable the staleness gate. A non-finite ceiling makes `age_minutes > ceiling`
# always-False, admitting arbitrarily stale catalyst data into the live
# committee. Finite-guard the operator-supplied ceiling (threshold-side sibling
# of ar33 data-side / ar41 governance-side). ---

def _stale_packet():
    """A 30-day-old packet relative to the 2024-01-02 decision context."""
    return _packet(asof="2023-12-03T00:00:00Z")


def test_validate_rejects_stale_packet_with_nan_ceiling():
    packet = semantic_packet_from_dict(_stale_packet(), attach_hash=True)
    ok, reason = validate_semantic_packet(
        packet,
        asset="BTC/USDT",
        asof=pd.Timestamp("2024-01-02T00:00:00Z"),
        max_age_minutes=float("nan"),
    )
    assert ok is False
    assert reason == "stale_packet"


def test_validate_rejects_stale_packet_with_inf_ceiling():
    packet = semantic_packet_from_dict(_stale_packet(), attach_hash=True)
    ok, reason = validate_semantic_packet(
        packet,
        asset="BTC/USDT",
        asof=pd.Timestamp("2024-01-02T00:00:00Z"),
        max_age_minutes=float("inf"),
    )
    assert ok is False
    assert reason == "stale_packet"


def test_validate_rejects_stale_packet_with_negative_ceiling():
    packet = semantic_packet_from_dict(_stale_packet(), attach_hash=True)
    ok, reason = validate_semantic_packet(
        packet,
        asset="BTC/USDT",
        asof=pd.Timestamp("2024-01-02T00:00:00Z"),
        max_age_minutes=-1.0,
    )
    assert ok is False
    assert reason == "stale_packet"


def test_semantic_analyst_abstains_on_stale_packet_with_nan_ceiling():
    # End-to-end: operator wrote analyst_config.hermes_semantic.max_age_minutes=.nan
    analyst = HermesSemanticAnalyst(max_age_minutes=float("nan"))
    view = analyst.analyze(_ctx(extras={"semantic_packets": [_stale_packet()]}))
    assert view.direction == 0
    assert view.confidence == 0.0
    assert view.metadata["abstain_reason"] == "stale_packet"


def test_validate_still_admits_fresh_packet_with_finite_ceiling():
    # Happy path must stay byte-identical: a fresh packet with the documented
    # 1-day ceiling is still admitted.
    packet = semantic_packet_from_dict(_packet(), attach_hash=True)
    ok, reason = validate_semantic_packet(
        packet,
        asset="BTC/USDT",
        asof=pd.Timestamp("2024-01-02T00:00:00Z"),
        max_age_minutes=1440.0,
    )
    assert ok is True
    assert reason == "ok"
