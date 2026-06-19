"""Tests for hermes_quant.backtest (ADR-0020).

Three lenses:
1. PaperPortfolio mark-to-market accounting (lot math, slippage, fees)
2. replay() end-to-end: synthetic bars -> BacktestResult shape
3. Reproducibility (same input -> same config_hash + same equity curve)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest import BacktestResult, PaperPortfolio, replay

# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------


def _bars(n: int = 200, *, seed: int = 42, drift: float = 0.0, vol: float = 0.5):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": 1000.0,
        }
    )


# ===========================================================================
# PaperPortfolio
# ===========================================================================


def test_portfolio_fresh_starts_with_full_cash():
    p = PaperPortfolio.fresh(10_000.0)
    assert p.cash == 10_000.0
    assert p.position_qty == 0.0
    assert p.realized_pnl == 0.0
    assert p.fees_paid == 0.0
    assert p.equity(100.0) == 10_000.0


def test_portfolio_open_long_position():
    p = PaperPortfolio.fresh(10_000.0)
    trade = p.apply_target(
        target_position_pct=0.10, bar_close=100.0, commission=0.001, slippage=0.0005
    )
    # 10% of NAV at close=100 means $1,000 long, ~10 qty
    assert p.position_qty > 9 and p.position_qty < 11
    assert p.avg_entry_price > 100  # slippage on the buy side
    assert p.fees_paid > 0
    assert trade["delta_qty"] > 0
    assert trade["commission_paid"] > 0
    assert trade["realized_pnl_delta"] == 0.0


def test_portfolio_close_realizes_pnl():
    p = PaperPortfolio.fresh(10_000.0)
    # Open +5% NAV at 100
    p.apply_target(0.05, bar_close=100.0, commission=0.0, slippage=0.0)
    # Close at 110 (10% gain)
    trade = p.apply_target(0.0, bar_close=110.0, commission=0.0, slippage=0.0)
    assert trade["realized_pnl_delta"] > 0
    assert p.realized_pnl > 0
    assert p.position_qty == 0.0


def test_portfolio_flip_long_to_short():
    p = PaperPortfolio.fresh(10_000.0)
    p.apply_target(0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    assert p.position_qty > 0
    # Flip
    trade = p.apply_target(-0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    assert p.position_qty < 0
    # Realized P&L close to 0 (price unchanged)
    assert abs(p.realized_pnl) < 1.0
    assert trade["delta_qty"] < 0


def test_portfolio_zero_change_skipped():
    p = PaperPortfolio.fresh(10_000.0)
    p.apply_target(0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    qty_before = p.position_qty
    trades_before = p.n_trades
    # Same target -> skip
    trade = p.apply_target(0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    assert trade["skipped"] is True
    assert p.position_qty == qty_before
    assert p.n_trades == trades_before


def test_portfolio_fees_deduct_from_cash():
    p = PaperPortfolio.fresh(10_000.0)
    p.apply_target(0.10, bar_close=100.0, commission=0.001, slippage=0.0)
    # Position ~ 10 qty * $100 = $1000 notional; commission 0.001 = $1
    assert p.fees_paid > 0.5 and p.fees_paid < 2.0


def test_portfolio_unrealized_pnl_when_long_and_price_rises():
    p = PaperPortfolio.fresh(10_000.0)
    p.apply_target(0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    upnl_at_100 = p.unrealized_pnl(100.0)
    upnl_at_110 = p.unrealized_pnl(110.0)
    assert upnl_at_100 == pytest.approx(0.0)
    assert upnl_at_110 > 0


def test_portfolio_equity_includes_position_mtm():
    p = PaperPortfolio.fresh(10_000.0)
    p.apply_target(0.10, bar_close=100.0, commission=0.0, slippage=0.0)
    e_at_100 = p.equity(100.0)
    e_at_110 = p.equity(110.0)
    # 10% gain on 10% position = 1% NAV gain
    assert e_at_110 - e_at_100 == pytest.approx(p.position_qty * 10.0)


# ===========================================================================
# replay() — basic shape
# ===========================================================================


def test_replay_returns_backtest_result():
    r = replay(
        _bars(200),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        initial_equity=10_000,
        warmup_bars=60,
    )
    assert isinstance(r, BacktestResult)
    assert r.symbol == "TEST"
    assert r.timeframe == "1h"
    assert r.n_bars == 140  # 200 - 60 warmup
    assert r.initial_equity == 10_000


def test_replay_too_few_bars_raises():
    with pytest.raises(ValueError, match="at least"):
        replay(_bars(50), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)


def test_replay_equity_curve_length_matches_bars_processed():
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    assert len(r.equity_curve) == 140
    assert len(r.bh_equity_curve) == 140
    assert len(r.positions) == 140


def test_replay_initial_equity_starts_curve_close_to_input():
    r = replay(
        _bars(200),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        initial_equity=10_000,
        warmup_bars=60,
    )
    # First equity value is post-first-bar-trade; should be close to 10k
    assert abs(r.equity_curve.iloc[0] - 10_000) < 200


def test_replay_buy_and_hold_baseline_computed():
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    # Buy-hold equity curve must match qty*close exactly
    bh_curve = r.bh_equity_curve
    assert bh_curve.iloc[0] > 0
    # First buy-hold = initial_equity (anchor close) -> approximately initial_equity
    assert abs(bh_curve.iloc[0] - r.initial_equity) < r.initial_equity * 0.05


def test_replay_excess_return_is_strategy_minus_buy_hold():
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    assert r.excess_return_vs_buy_hold_pct == pytest.approx(
        r.total_return_pct - r.buy_hold_total_return_pct,
        rel=1e-9,
    )


# ===========================================================================
# Reproducibility (charter Reproducibility invariant)
# ===========================================================================


def test_replay_same_input_same_config_hash():
    r1 = replay(
        _bars(200, seed=42), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60
    )
    r2 = replay(
        _bars(200, seed=42), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60
    )
    assert r1.config_hash == r2.config_hash


def test_replay_different_warmup_changes_config_hash():
    r1 = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    r2 = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=80)
    assert r1.config_hash != r2.config_hash


def test_replay_same_bars_same_equity_curve():
    """Charter Reproducibility: two runs with identical bars produce
    byte-identical equity curves."""
    bars = _bars(200, seed=42)
    r1 = replay(bars.copy(), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    r2 = replay(bars.copy(), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    assert r1.total_return_pct == r2.total_return_pct
    assert r1.n_trades == r2.n_trades
    pd.testing.assert_series_equal(r1.equity_curve, r2.equity_curve)


# ===========================================================================
# Markdown report
# ===========================================================================


def test_replay_markdown_report_includes_charter_headline():
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    md = r.to_markdown_report()
    assert "Backtest report" in md
    assert "Excess return vs buy-and-hold" in md
    assert "Sharpe" in md
    assert r.config_hash in md


def test_replay_negative_excess_marked_as_charter_block():
    """When strategy LOSES vs buy-hold, the report flags it per charter
    'fix analysts before RL aggregator'."""
    # Use a strong-trend up market — strategy with discrete-position
    # actuator and slippage almost always loses to buy-and-hold.
    bars = _bars(200, seed=1, drift=0.5, vol=0.1)
    r = replay(bars, symbol="UPONLY", asset_class="equity", timeframe="1h", warmup_bars=60)
    md = r.to_markdown_report()
    if r.excess_return_vs_buy_hold_pct < 0:
        assert "NEGATIVE" in md
        assert "RL aggregator" in md


# ===========================================================================
# JSON serialization
# ===========================================================================


def test_replay_to_dict_is_json_serializable():
    import json

    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    d = r.to_dict()
    s = json.dumps(d)  # no exception
    assert "config_hash" in s
    assert "excess_return_vs_buy_hold_pct" in s


def test_replay_to_dict_excludes_pd_series():
    """to_dict must not contain pd.Series (not JSON-serializable)."""
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    d = r.to_dict()
    for v in d.values():
        assert not isinstance(v, pd.Series)


# ===========================================================================
# DSR computed when n_observations >= 30
# ===========================================================================


def test_replay_dsr_computed_for_long_runs():
    r = replay(_bars(200), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    # 140 bars >> 30, so DSR should be a finite float
    assert not np.isnan(r.deflated_sharpe)
    assert 0.0 <= r.deflated_sharpe <= 1.0


def test_replay_dsr_nan_for_short_runs():
    """With < 30 observations DSR is undefined; result reports NaN."""
    # 50 bars with warmup 35 -> 15 observations, < 30
    r = replay(_bars(50), symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=35)
    assert np.isnan(r.deflated_sharpe)


# ===========================================================================
# cs56 (sibling of cs48 on the replay path): a non-finite Sharpe must not
# propagate a NaN deflated_sharpe into BacktestResult / result.json
# ===========================================================================


def _advisor_full_long(**kwargs):
    """Inject a deterministic full-long signal so replay() walks every bar."""
    return {
        "aggregated_signal": {"direction": 1, "magnitude": 1.0, "confidence": 1.0},
        "risk_gate": {"pass": True, "kelly_fraction": 0.5},
        "analyst_views": [],
    }


@pytest.mark.parametrize("degenerate_sharpe", [float("inf"), float("-inf")])
def test_replay_dsr_finite_conservative_on_nonfinite_sharpe(monkeypatch, degenerate_sharpe):
    """cs56 (sibling of cs48): a zero-variance OOS strategy series (bit-identical
    per-bar returns, e.g. a flat-but-marked position or a synthetic
    geometric-doubling instrument) makes replay._sharpe return ±inf (std==0,
    mean!=0 branch). dsr.deflated_sharpe then forms
    ``variance_term = 1 - skew*SR + (kurt-1)/4*SR**2``; for a constant series
    skew==0, so ``skew*inf == nan`` -> variance_term is NaN, the
    ``variance_term <= 0`` guard (NaN<=0 == False) is bypassed, and
    ``Φ(sr_diff*sqrt(n-1)/sqrt(NaN))`` collapses to NaN WITHOUT raising.
    replay()'s try/except only catches ValueError/ZeroDivisionError, so pre-fix
    the NaN escaped into BacktestResult.deflated_sharpe and rendered as ``null``
    in result.json — INDISTINGUISHABLE from the legitimate n<30 low-power
    omission, silently erasing the false-discovery hedge.

    The degenerate ±inf Sharpe is awkward to reach through PaperPortfolio's
    mark-at-trade accounting, so we monkeypatch replay._sharpe to emit the
    documented degenerate output; the assertion targets replay()'s guard.

    After the fix the deflated Sharpe is a FINITE, conservative 0.0 (fails any
    ``dsr >= floor`` gate), never a NaN.
    """
    import sys

    rpmod = sys.modules["hermes_quant.backtest.replay"]
    monkeypatch.setattr(rpmod, "_sharpe", lambda *a, **k: degenerate_sharpe)

    # 120 bars, warmup 60 -> ~59 observations >= 30 so the DSR block runs.
    bars = _bars(120, drift=0.01)
    r = replay(
        bars,
        symbol="DEGEN",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=False,
        advisor_recommend=_advisor_full_long,
    )
    # The bug: deflated_sharpe is NaN. The fix: a finite conservative 0.0.
    assert np.isfinite(r.deflated_sharpe), "deflated_sharpe must be finite, not NaN (cs56)"
    assert r.deflated_sharpe == 0.0
    # The JSON artifact now carries a real number, not the null a NaN renders as
    # (which masquerades as the n<30 low-power omission).
    assert r.to_dict()["deflated_sharpe"] == 0.0


def test_replay_dsr_unchanged_for_finite_variance_series(monkeypatch):
    """cs56: a normal finite-variance run never trips the non-finite guard, so
    the deflated Sharpe is byte-identical to the bare dsr.deflated_sharpe call on
    the same observed Sharpe / skew / kurtosis. The guard fires ONLY on the
    degenerate (non-finite) input."""
    bars = _bars(200, drift=0.02, vol=0.4)
    r = replay(bars, symbol="TEST", asset_class="equity", timeframe="1h", warmup_bars=60)
    assert np.isfinite(r.deflated_sharpe)
    assert 0.0 <= r.deflated_sharpe <= 1.0

    # Reconstruct the expected DSR through the exact same inputs replay() feeds
    # dsr.deflated_sharpe, proving the guard left the finite path untouched.
    from hermes_quant.evaluation.dsr import deflated_sharpe

    strat_returns = r.equity_curve.pct_change().dropna()
    n_obs = len(strat_returns)
    assert n_obs >= 30
    expected = deflated_sharpe(
        observed_sharpe=r.sharpe,
        n_trials=1,
        n_observations=n_obs,
        skew=float(strat_returns.skew()) if n_obs >= 3 else 0.0,
        kurtosis=float(strat_returns.kurtosis() + 3.0) if n_obs >= 4 else 3.0,
    )
    assert r.deflated_sharpe == expected


# ===========================================================================
# Advisor failure does not crash the backtest
# ===========================================================================


def test_advisor_exception_treated_as_flat():
    """If advisor raises mid-replay, that bar is treated as no-op."""
    bars = _bars(200)
    n_calls = {"x": 0}

    def flaky_advisor(**kwargs):
        n_calls["x"] += 1
        if n_calls["x"] == 5:
            raise RuntimeError("simulated transient error")
        # Otherwise return a no-trade signal
        return {
            "as_of": kwargs["as_of"].isoformat()
            if hasattr(kwargs["as_of"], "isoformat")
            else str(kwargs["as_of"]),
            "aggregated_signal": {"direction": 0, "magnitude": 0.0, "confidence": 0.0},
            "risk_gate": {"pass": False, "kelly_fraction": 0.0},
            "analyst_views": [],
        }

    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=flaky_advisor,
    )
    # No crash; result is structurally valid
    assert isinstance(r, BacktestResult)
    assert r.n_decisions == 0  # advisor never returned a non-flat signal
