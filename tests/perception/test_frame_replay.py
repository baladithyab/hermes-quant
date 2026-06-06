"""T3 — THE byte-identical-replay proof (ADR-0079 PDR-1 eval gate, plan §5.1).

The PDR-1 promotion gate (ADR-0079 Rollout PDR-1; worked example ADR-0079:209-215):

    r_today = recommend(symbol, ...)                       # no perception_frame
    frame   = build_perception_frame(...)
    r_frame = recommend(symbol, ..., perception_frame=frame)
    assert r_today == r_frame                              # on the load-bearing keys

Covers: synthetic fixtures (_make_bars via _RecordingProvider), BOTH flag states
(HERMES_QUANT_SEMANTIC_ENABLED OFF and ON with a fixture packet store), and the
degenerate (no-bars / all-still-forming-dropped) paths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from hermes_quant.advisor import recommend
from hermes_quant.perception.builder import build_perception_frame

# Keys the existing replay test pins (test_no_lookahead.py:268) PLUS analyst_views
# (proves the analysts saw an identical ctx).
_REPLAY_KEYS = ["as_of", "aggregated_signal", "risk_gate", "decision_price", "analyst_views"]


def _make_bars(n: int = 120, *, base: float = 100.0, trend: float = 0.5, seed: int = 42):
    rng = np.random.default_rng(seed=seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = base + np.arange(n) * trend + rng.normal(0, 0.5, n)
    opens = closes - rng.uniform(0, 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, 0.4, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _RecordingProvider:
    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _assert_replay_identical(symbol, asset_class, asof, bars, *, lookback_bars=200):
    """Core eval-gate assertion: no-frame == frame-built on the load-bearing keys."""
    r_today = recommend(
        symbol=symbol,
        asset_class=asset_class,
        as_of=asof,
        provider=_RecordingProvider(bars),
        include_lessons=False,
    )
    frame = build_perception_frame(
        symbol,
        timeframe="1d",
        asset_class=asset_class,
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(asof, tz="UTC"),
        lookback_bars=lookback_bars,
    )
    r_frame = recommend(
        symbol=symbol,
        asset_class=asset_class,
        as_of=asof,
        provider=_RecordingProvider(bars),
        include_lessons=False,
        perception_frame=frame,
    )
    for key in _REPLAY_KEYS:
        assert r_today.get(key) == r_frame.get(key), (
            f"key {key!r} differs between no-frame and frame-built recommend "
            f"(PDR-1 byte-identity violation): today={r_today.get(key)} "
            f"frame={r_frame.get(key)}"
        )
    return r_today, r_frame, frame


# ---------------------------------------------------------------------------
# Flag OFF (the default) — no packets either way -> trivially identical
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [42, 7, 123])
@pytest.mark.parametrize("trend", [0.5, -0.4, 0.0])
def test_replay_byte_identical_flag_off(monkeypatch, seed, trend):
    # Off-switch: SEMANTIC_ENABLED=0 (FLAGS.md Tier A promoted the default to ON,
    # so the inert path must be requested explicitly).
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    bars = _make_bars(120, trend=trend, seed=seed)
    asof = bars["timestamp"].iloc[60].isoformat()
    _assert_replay_identical("TEST", "equity", asof, bars)


@pytest.mark.parametrize("trend", [0.5, -0.4, 0.0])
def test_replay_byte_identical_convergence_off(monkeypatch, trend):
    """PDR-3 (HERMES_QUANT_CONVERGENCE) absent => the full-pipeline recommend()
    replay is byte-identical no-frame vs frame-built (no live-path divergence from
    the builder's Step 5c convergence stamp when the flag is OFF)."""
    # Off-switch: SEMANTIC_ENABLED=0 (default promoted to ON, FLAGS.md Tier A).
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    bars = _make_bars(120, trend=trend, seed=42)
    asof = bars["timestamp"].iloc[60].isoformat()
    _, _, frame = _assert_replay_identical("TEST", "equity", asof, bars)
    # Convergence slot stays empty when the flag is OFF (container, not authority).
    assert frame is None or frame.convergence is None


def test_replay_byte_identical_full_history(monkeypatch):
    # Off-switch: SEMANTIC_ENABLED=0 (default promoted to ON, FLAGS.md Tier A).
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    bars = _make_bars(120, trend=0.5, seed=42)
    asof = bars["timestamp"].iloc[-1].isoformat()
    r_today, r_frame, frame = _assert_replay_identical("TEST", "equity", asof, bars)
    # Sanity: the frame path actually produced a signal (not a degenerate gate).
    assert frame is not None
    assert r_frame["as_of"] == r_today["as_of"]


# ---------------------------------------------------------------------------
# Flag ON with a fixture packet store — proves the absorbed semantic_market_extras
# produces the identical ctx.extras["semantic_packets"] + decision_asof.
# ---------------------------------------------------------------------------


def _seed_packet(store, *, asset, asof, stance, confidence, magnitude, horizon="1d"):
    from hermes_quant.semantic import semantic_packet_from_dict

    pkt = semantic_packet_from_dict(
        {
            "schema_version": 1,
            "asset": asset,
            "asof": asof,
            "horizon": horizon,
            "stance": stance,
            "confidence": confidence,
            "magnitude": magnitude,
            "summary": f"replay-fixture {asset} {stance} {asof}",
            "sources": [{"type": "note", "ref": "frame-replay-fence"}],
            "model": "hermes:frame-replay-test",
        }
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")


def test_replay_byte_identical_flag_on_with_packets(monkeypatch, tmp_path):
    """With the flag ON, the cron path built market_extras via
    semantic_market_extras(symbol, horizon=tf). The frame path must produce the
    identical ctx — so r_today (built with the SAME helper via market_extras) ==
    r_frame (built via build_perception_frame, which absorbed the helper).

    Both branches use the SAME explicit decision_asof so the wall-clock default
    is pinned (deterministic, no network)."""
    from hermes_quant.catalyst import synthesize
    from hermes_quant.catalyst.wiring import semantic_market_extras

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)

    bars = _make_bars(120, trend=0.5, seed=42)
    asof = bars["timestamp"].iloc[-1].isoformat()
    decision = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    # A past packet (admissible) for AAPL.
    _seed_packet(
        store, asset="AAPL", asof="2026-05-30T09:00:00Z",
        stance="bullish", confidence=0.70, magnitude=0.01,
    )

    # Today path: the cron's bespoke market_extras (the pre-PDR-1 shape).
    me = semantic_market_extras("AAPL", decision_asof=decision, horizon="1d")
    assert me is not None, "expected the past packet to be injected"
    r_today = recommend(
        symbol="AAPL",
        asset_class="equity",
        as_of=asof,
        provider=_RecordingProvider(bars),
        include_lessons=False,
        market_extras=me,
    )

    # Frame path: build_perception_frame absorbs the helper (same decision_asof).
    frame = build_perception_frame(
        "AAPL",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(asof, tz="UTC"),
        lookback_bars=200,
        decision_asof=decision,
    )
    assert frame is not None
    assert frame.semantic_packets, "frame absorbed no packets despite flag ON"
    assert frame.extras["decision_asof"] == decision.isoformat()
    r_frame = recommend(
        symbol="AAPL",
        asset_class="equity",
        as_of=asof,
        provider=_RecordingProvider(bars),
        include_lessons=False,
        perception_frame=frame,
    )

    for key in _REPLAY_KEYS:
        assert r_today.get(key) == r_frame.get(key), (
            f"key {key!r} differs flag-ON: today={r_today.get(key)} "
            f"frame={r_frame.get(key)}"
        )


# ---------------------------------------------------------------------------
# Degenerate paths — builder returns None -> advisor's None branch -> identical
# gated_no_data result.
# ---------------------------------------------------------------------------


def test_replay_no_bars_degenerate_identical():
    """Provider yields zero bars: builder returns None, the cron forwards None,
    and recommend's None branch produces the identical _gated_no_data result."""
    empty = pd.DataFrame(
        {"timestamp": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    )
    asof = "2026-03-15T00:00:00Z"

    frame = build_perception_frame(
        "TEST",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(empty),
        asof_ts=pd.Timestamp(asof, tz="UTC"),
        lookback_bars=200,
    )
    assert frame is None, "builder must return None on no-bars (degenerate)"

    r_today = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=_RecordingProvider(empty),
        include_lessons=False,
    )
    r_frame = recommend(
        symbol="TEST",
        asset_class="equity",
        as_of=asof,
        provider=_RecordingProvider(empty),
        include_lessons=False,
        perception_frame=frame,  # None -> identical to not passing one
    )
    for key in _REPLAY_KEYS:
        assert r_today.get(key) == r_frame.get(key)
    assert r_today["risk_gate"]["pass"] is False


def test_replay_as_of_before_all_bars_degenerate_identical():
    """as_of earlier than every bar -> empty after filter -> builder None ->
    identical gated result."""
    bars = _make_bars(50, seed=42)
    asof = "2025-01-01T00:00:00Z"  # before the 2026 start
    frame = build_perception_frame(
        "TEST",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(asof, tz="UTC"),
        lookback_bars=200,
    )
    assert frame is None
    r_today = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False,
    )
    r_frame = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False, perception_frame=frame,
    )
    for key in _REPLAY_KEYS:
        assert r_today.get(key) == r_frame.get(key)
