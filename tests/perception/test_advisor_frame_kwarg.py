"""T5 — recommend(perception_frame=...) branch + None-default no-op + precedence.

Pins (plan §3.4 / Acceptance §3):
  - perception_frame=None is the byte-identical no-op (additivity, no flag).
  - frame-wins-with-caveat when BOTH perception_frame and market_extras passed.
  - the kwarg threads through default-None for callers that always pass it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.advisor import recommend
from hermes_quant.perception.builder import build_perception_frame


def _make_bars(n: int = 120, *, trend: float = 0.5, seed: int = 42):
    rng = np.random.default_rng(seed=seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * trend + rng.normal(0, 0.5, n)
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


def _frame(bars, asof):
    return build_perception_frame(
        "TEST",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(asof, tz="UTC"),
        lookback_bars=200,
    )


def test_none_default_is_noop():
    """Not passing perception_frame == passing None."""
    bars = _make_bars(120, seed=42)
    asof = bars["timestamp"].iloc[-1].isoformat()
    r_implicit = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False,
    )
    r_explicit_none = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False, perception_frame=None,
    )
    for key in ["as_of", "aggregated_signal", "risk_gate", "decision_price", "analyst_views"]:
        assert r_implicit.get(key) == r_explicit_none.get(key)


def test_frame_branch_produces_signal():
    bars = _make_bars(120, seed=42)
    asof = bars["timestamp"].iloc[-1].isoformat()
    frame = _frame(bars, asof)
    assert frame is not None
    r = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False, perception_frame=frame,
    )
    assert r["as_of"] == frame.asof.isoformat()
    assert r["data_quality"]["bars_received"] == len(frame.bars)


def test_frame_wins_with_caveat_when_both_passed():
    """When BOTH perception_frame and market_extras are passed, the frame wins
    (it already absorbed the semantic slice) and a caveat is appended — never raise."""
    bars = _make_bars(120, seed=42)
    asof = bars["timestamp"].iloc[-1].isoformat()
    frame = _frame(bars, asof)
    r = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False,
        perception_frame=frame,
        market_extras={"semantic_packets": [{"asof": "ignored"}]},
    )
    assert any("market_extras ignored" in c for c in r.get("caveats", [])), (
        "expected the frame-wins-with-caveat posture"
    )
    # The ignored market_extras must NOT have leaked a semantic packet into the
    # projected ctx: build the same frame and confirm the result matches the
    # no-market_extras frame path.
    r_frame_only = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False,
        perception_frame=_frame(bars, asof),
    )
    for key in ["aggregated_signal", "risk_gate", "decision_price"]:
        assert r.get(key) == r_frame_only.get(key)


def test_kwarg_threads_default_none_for_always_pass_callers():
    """A caller that always passes perception_frame=None (the M17 'always build,
    forward possibly-None' contract) gets the byte-identical default path."""
    bars = _make_bars(120, seed=7)
    asof = bars["timestamp"].iloc[-1].isoformat()
    r = recommend(
        symbol="TEST", asset_class="equity", as_of=asof,
        provider=_RecordingProvider(bars), include_lessons=False, perception_frame=None,
    )
    assert r["risk_gate"] is not None
