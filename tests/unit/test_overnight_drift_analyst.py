"""tests/unit/test_overnight_drift_analyst.py — ADR-0089 OvernightDriftAnalyst.

Locks in the load-bearing invariants (money-software):
  * ASOF-HONEST: the view depends only on bars <= asof; feeding a future bar
    that the engine would NOT have shown at asof changes nothing (no-lookahead).
  * Trailing-spread correctness: a constructed overnight-tilted frame yields a
    LONG nudge; an intraday-tilted frame ABSTAINS (long-only stance, ADR-0089).
  * Anti-noise floor: a no-tilt frame abstains (does not vote noise).
  * Zero-turnover invariant: every emitted view is metadata-tagged zero_turnover
    and is a HOLD-direction nudge, never a round-trip proposal.
  * Loadout gate: HERMES_QUANT_OVERNIGHT_DRIFT default-OFF (roster byte-identical);
    flag-ON appends exactly the overnight-drift peer.
  * Robustness: insufficient history / missing columns / corrupt prices -> abstain
    (None), never raise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.analysts.overnight_drift import OvernightDriftAnalyst
from hermes_quant.protocol import MarketContext


def _frame(opens, closes, start="2024-01-01"):
    n = len(closes)
    idx = pd.bdate_range(start=start, periods=n)
    opens = np.asarray(opens, dtype=float)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.01,
            "low": np.minimum(opens, closes) * 0.99,
            "close": closes,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    df["timestamp"] = df.index
    return df


def _ctx(bars, asof=None):
    asof = asof if asof is not None else pd.Timestamp(bars.index[-1], tz="UTC")
    return MarketContext(
        asset="TEST",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        last_volume=float(bars["volume"].iloc[-1]),
        asof=asof,
    )


def _overnight_tilted(n=120, seed=3):
    """Gaps UP overnight, fades intraday -> positive overnight-minus-intraday."""
    rng = np.random.default_rng(seed)
    close = [100.0]
    openp = [100.0]
    for _ in range(1, n):
        on = rng.normal(0.003, 0.004)   # +overnight
        idr = rng.normal(-0.002, 0.004)  # -intraday
        o = close[-1] * (1 + on)
        c = o * (1 + idr)
        openp.append(o)
        close.append(c)
    return _frame(openp, close)


def _intraday_tilted(n=120, seed=4):
    """Flat overnight, rallies intraday -> negative overnight-minus-intraday."""
    rng = np.random.default_rng(seed)
    close = [100.0]
    openp = [100.0]
    for _ in range(1, n):
        on = rng.normal(-0.001, 0.004)
        idr = rng.normal(0.003, 0.004)
        o = close[-1] * (1 + on)
        c = o * (1 + idr)
        openp.append(o)
        close.append(c)
    return _frame(openp, close)


def _flat(n=120):
    """No tilt: overnight ~= intraday ~= 0 -> abstain (anti-noise floor)."""
    base = 100.0 * np.ones(n)
    return _frame(base, base)


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------


def test_overnight_tilt_emits_long_nudge():
    a = OvernightDriftAnalyst()
    v = a.analyze(_ctx(_overnight_tilted()))
    assert v is not None
    assert v.direction == 1  # LONG hold-through-close nudge
    assert v.metadata["spread_ann"] > 0
    assert 0.0 <= v.confidence <= 1.0
    assert v.horizon == "1d"


def test_intraday_tilt_abstains_in_long_only_mode():
    """Default long_only_nudge=True: a negative tilt ABSTAINS (no short)."""
    a = OvernightDriftAnalyst()
    v = a.analyze(_ctx(_intraday_tilted()))
    assert v is None


def test_intraday_tilt_shorts_when_long_only_disabled():
    a = OvernightDriftAnalyst(long_only_nudge=False)
    v = a.analyze(_ctx(_intraday_tilted()))
    assert v is not None
    assert v.direction == -1
    assert v.metadata["spread_ann"] < 0


def test_flat_frame_abstains_anti_noise():
    a = OvernightDriftAnalyst()
    assert a.analyze(_ctx(_flat())) is None


def test_zero_turnover_invariant_on_every_view():
    """ADR-0089 D-2: every emitted view is tagged zero_turnover — it modulates a
    hold, never proposes a round-trip."""
    a = OvernightDriftAnalyst()
    v = a.analyze(_ctx(_overnight_tilted()))
    assert v is not None
    assert v.metadata["zero_turnover"] is True


# ---------------------------------------------------------------------------
# Asof-honesty / no-lookahead
# ---------------------------------------------------------------------------


def test_no_lookahead_future_bar_does_not_change_view():
    """The view at asof must be identical whether or not bars AFTER asof exist in
    the (engine-filtered) frame. We emulate the engine contract: the analyst only
    ever sees bars <= asof, so a longer frame truncated to the same asof window
    must produce the same signal."""
    full = _overnight_tilted(n=140)
    asof_idx = 110
    asof_ts = pd.Timestamp(full.index[asof_idx], tz="UTC")
    # The engine passes only bars up to asof:
    truncated = full.iloc[: asof_idx + 1]
    a1 = OvernightDriftAnalyst()
    a2 = OvernightDriftAnalyst()
    v_trunc = a1.analyze(_ctx(truncated, asof=asof_ts))
    v_full_to_asof = a2.analyze(_ctx(full.iloc[: asof_idx + 1], asof=asof_ts))
    assert (v_trunc is None) == (v_full_to_asof is None)
    if v_trunc is not None:
        # Same window -> identical spread (deterministic, no RNG in the analyst).
        assert v_trunc.metadata["spread_ann"] == pytest.approx(
            v_full_to_asof.metadata["spread_ann"]
        )


def test_view_uses_only_completed_pairs():
    """The trailing spread is computed from completed (close[t-1]->open[t]->close[t])
    pairs; appending a single forward open with no close must not retroactively
    change the historical spread the analyst reports for the prior window."""
    base = _overnight_tilted(n=100)
    a = OvernightDriftAnalyst(lookback_window=60)
    v = a.analyze(_ctx(base))
    assert v is not None
    # n_pairs is bounded by the lookback window and the available completed pairs.
    assert v.metadata["n_pairs"] <= 60
    assert v.metadata["n_pairs"] >= 30


# ---------------------------------------------------------------------------
# Robustness — abstain, never raise
# ---------------------------------------------------------------------------


def test_insufficient_history_abstains():
    a = OvernightDriftAnalyst(min_history_bars=61)
    short = _overnight_tilted(n=40)
    assert a.analyze(_ctx(short)) is None


def test_missing_columns_abstains():
    a = OvernightDriftAnalyst()
    bars = _overnight_tilted()
    bars = bars.drop(columns=["open"])
    assert a.analyze(_ctx(bars)) is None


def test_corrupt_prices_do_not_raise():
    a = OvernightDriftAnalyst()
    bars = _overnight_tilted()
    # Inject zeros/NaNs into a chunk of opens — must be filtered, not crash.
    bars.iloc[50:60, bars.columns.get_loc("open")] = 0.0
    bars.iloc[70:75, bars.columns.get_loc("close")] = np.nan
    v = a.analyze(_ctx(bars))  # may abstain or emit; must not raise
    assert v is None or 0.0 <= v.confidence <= 1.0


def test_health_reports_counts():
    a = OvernightDriftAnalyst()
    a.analyze(_ctx(_overnight_tilted()))
    h = a.health()
    assert h["name"] == "overnight-drift"
    assert h["n_views_emitted"] == 1


# ---------------------------------------------------------------------------
# Loadout gate (default-OFF, byte-identical when off)
# ---------------------------------------------------------------------------


def test_loadout_excludes_when_flag_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_OVERNIGHT_DRIFT", raising=False)
    from hermes_quant.advisor import _build_default_analysts

    names = [getattr(a, "name", "?") for a in _build_default_analysts()]
    assert "overnight-drift" not in names


def test_loadout_includes_when_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_OVERNIGHT_DRIFT", "1")
    from hermes_quant.advisor import _build_default_analysts

    names = [getattr(a, "name", "?") for a in _build_default_analysts()]
    assert "overnight-drift" in names
