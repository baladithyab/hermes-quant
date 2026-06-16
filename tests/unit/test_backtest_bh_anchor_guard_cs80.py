"""cs80 — fail-CLOSED on a non-finite/zero buy-and-hold anchor close.

The buy-and-hold reference leg divides ``initial_equity`` by the close of the
bar at the warmup boundary (``bars["close"].iloc[warmup_bars]``). If that anchor
is 0.0 the quantity becomes +inf and ``excess_return_vs_buy_hold_pct`` becomes
-inf; if it is NaN the whole bh leg is NaN-poisoned. Both silently corrupt the
HONESTY metrics (buy_hold_total_return_pct / buy_hold_sharpe /
excess_return_vs_buy_hold_pct) an operator reads to judge a strategy — a
fail-open in the cs02 reporting-honesty family.

Money-software posture: a 0/NaN anchor at the very first priced bar means the
price series is corrupt at the decision boundary, so the whole backtest is
untrustworthy. replay() must raise ValueError (consistent with the
insufficient-bars guard), never hand the operator a silent -inf/NaN.

Non-vacuity: a NORMAL positive anchor still produces a finite excess-return.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest import replay


def _bars(n: int, *, anchor_override=None, warmup_bars: int):
    """A clean upward-drifting bar series; optionally corrupt the bar AT the
    warmup boundary (the buy-and-hold anchor) to ``anchor_override``."""
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    closes = np.linspace(100.0, 120.0, n)
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes.astype(float),
            "volume": 1000.0,
        }
    )
    if anchor_override is not None:
        df.loc[warmup_bars, "close"] = anchor_override
    return df


def _hold_advisor(**_kwargs):
    """Trivial no-op advisor: never fires (risk gate fails). Keeps the test
    self-contained and free of heavy advisor deps. The bh-anchor guard fires
    BEFORE the loop reaches any advisor call, so this only matters for the
    happy-path non-vacuity assertion."""
    return {"risk_gate": {"pass": False}, "aggregated_signal": {"direction": 0}}


def _replay(bars):
    return replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        initial_equity=10_000.0,
        warmup_bars=60,
        learn_from_fills=False,  # no aggregator deps
        advisor_recommend=_hold_advisor,
    )


def test_zero_anchor_close_raises_not_silent_minus_inf():
    """A 0.0 anchor close at the warmup boundary must raise, not produce a
    silent -inf excess-return."""
    bars = _bars(80, anchor_override=0.0, warmup_bars=60)
    with pytest.raises(ValueError, match="anchor close"):
        _replay(bars)


def test_nan_anchor_close_raises_not_silent_nan():
    """A NaN anchor close at the warmup boundary must raise, not NaN-poison the
    buy-and-hold honesty metrics."""
    bars = _bars(80, anchor_override=float("nan"), warmup_bars=60)
    with pytest.raises(ValueError, match="anchor close"):
        _replay(bars)


def test_negative_anchor_close_raises():
    """A negative anchor close is also a corrupt price at the decision
    boundary and must fail-closed (guard is `> 0`, not just finite)."""
    bars = _bars(80, anchor_override=-5.0, warmup_bars=60)
    with pytest.raises(ValueError, match="anchor close"):
        _replay(bars)


def test_normal_positive_anchor_produces_finite_excess_return():
    """Non-vacuity: the guard does NOT break the happy path. A normal positive
    anchor still yields a finite buy-and-hold leg + finite excess-return."""
    bars = _bars(80, warmup_bars=60)  # no override -> clean positive anchor
    r = _replay(bars)
    assert math.isfinite(r.excess_return_vs_buy_hold_pct)
    assert math.isfinite(r.buy_hold_total_return_pct)
    assert math.isfinite(r.buy_hold_sharpe)
    # bh leg actually computed something (upward-drifting series -> positive bh return)
    assert r.buy_hold_total_return_pct > 0.0
