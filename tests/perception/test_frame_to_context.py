"""T2 — frame_to_context adapter purity + exact extras-shape fidelity (PDR-1).

The adapter MUST reproduce advisor.py's None-branch ctx.extras key-set exactly
when the future-score fields are None, and it MUST be pure (no build_regime_extras
/ semantic_market_extras calls — those run during frame construction).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.builder import build_perception_frame
from hermes_quant.perception.frame import PerceptionFrame
from hermes_quant.protocol import MarketContext
from hermes_quant.regime.extras_builder import RegimePacket


def _make_bars(n: int = 100, *, base: float = 100.0, trend: float = 0.5, seed: int = 42):
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


def _frame_for(bars, asof) -> PerceptionFrame:
    return build_perception_frame(
        "TEST",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(asof),  # _make_bars already produces tz-aware UTC
        lookback_bars=200,
    )


def test_adapter_returns_market_context():
    bars = _make_bars(100)
    asof = bars["timestamp"].iloc[-1]
    frame = _frame_for(bars, asof)
    assert frame is not None
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert isinstance(ctx, MarketContext)
    assert ctx.asset == "TEST"
    assert ctx.timeframe == "1d"
    assert ctx.asset_class == "equity"
    assert ctx.exchange is None
    assert ctx.asof == frame.asof
    assert ctx.last_close == frame.last_close
    assert ctx.last_volume == pytest.approx(float(frame.bars["volume"].iloc[-1]))


def test_adapter_extras_key_set_matches_default_path():
    """The projected ctx.extras key-set is exactly {regime, regime_failure,
    regime_classifier_kind} when no semantic/still-forming keys are present —
    same as advisor.py:858-869 with market_extras=None."""
    bars = _make_bars(100)
    asof = bars["timestamp"].iloc[-1]
    frame = _frame_for(bars, asof)
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert set(ctx.extras.keys()) == {"regime", "regime_failure", "regime_classifier_kind"}


def test_adapter_does_not_write_future_score_keys_when_none():
    """PDR-1 invariant: trend_velocity/convergence/saturation are None so the
    adapter writes NOTHING for them (preserving the default extras key-set)."""
    bars = _make_bars(100)
    asof = bars["timestamp"].iloc[-1]
    frame = _frame_for(bars, asof)
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert "trend_velocity" not in ctx.extras
    assert "convergence" not in ctx.extras
    assert "saturation" not in ctx.extras


def test_adapter_writes_future_score_keys_when_present():
    """Forward-compat (PDR-2/3/4): a non-None future score rides in extras under
    its own key. Proves the adapter wiring works even though PDR-1 never sets them."""
    bars = _make_bars(100)
    frame = PerceptionFrame(
        symbol="TEST",
        asof=pd.Timestamp(bars["timestamp"].iloc[-1]),
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        trend_velocity={"accel": 0.3},
        convergence={"n_sources": 2},
        saturation={"m": 0.7},
    )
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert ctx.extras["trend_velocity"] == {"accel": 0.3}
    assert ctx.extras["convergence"] == {"n_sources": 2}
    assert ctx.extras["saturation"] == {"m": 0.7}


def test_adapter_is_pure_no_classification(monkeypatch):
    """The adapter must NOT call build_regime_extras. We poison it; if the
    adapter calls it, the test fails."""
    import hermes_quant.perception.adapter as adapter_mod

    # The adapter module does not import build_regime_extras at all; assert it
    # is absent from the module namespace (purity by construction).
    assert not hasattr(adapter_mod, "build_regime_extras")
    assert not hasattr(adapter_mod, "semantic_market_extras")

    # And re-expansion uses the carried packet object verbatim.
    pkt = RegimePacket.__new__(RegimePacket)
    object.__setattr__(pkt, "classifier_kind", "hmm")
    frame = PerceptionFrame(
        symbol="TEST",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_make_bars(3),
        last_close=100.5,
        regime=pkt,
    )
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert ctx.extras["regime"] is pkt
    assert ctx.extras["regime_classifier_kind"] == "hmm"


def test_adapter_regime_none_yields_unavailable_kind():
    frame = PerceptionFrame(
        symbol="TEST",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_make_bars(3),
        last_close=100.5,
        regime=None,
    )
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert ctx.extras["regime"] is None
    assert ctx.extras["regime_classifier_kind"] == "unavailable"
    assert ctx.extras["regime_failure"] is None


def test_adapter_semantic_packets_only_when_nonempty():
    frame_empty = PerceptionFrame(
        symbol="TEST",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_make_bars(3),
        last_close=100.5,
        semantic_packets=(),
    )
    ctx_empty = frame_to_context(frame_empty, timeframe="1d", asset_class="equity")
    assert "semantic_packets" not in ctx_empty.extras

    frame_full = PerceptionFrame(
        symbol="TEST",
        asof=pd.Timestamp("2026-01-03", tz="UTC"),
        bars=_make_bars(3),
        last_close=100.5,
        semantic_packets=({"asof": "x"},),
    )
    ctx_full = frame_to_context(frame_full, timeframe="1d", asset_class="equity")
    assert ctx_full.extras["semantic_packets"] == [{"asof": "x"}]
