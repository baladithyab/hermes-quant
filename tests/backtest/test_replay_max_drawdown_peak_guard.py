"""ar121 — backtest/replay.py max-drawdown must guard the peak denominator.

The replay max-drawdown computed `(equity - cummax) / cummax` with NO guard on the
running peak. A signed-position book (`PaperPortfolio.equity = cash + qty*mark` with
shorts) can drive NAV through 0 — a short that rallies hard — so the running peak can be
<= 0; the bare `/running_max` then divides by zero and the whole-series max_dd_pct becomes
-inf/NaN. The canonical sibling copies (eval/stockbench.py:212, backtest/engine.py:646)
divide by `where(peak > 0, peak, 1.0)`; replay was the un-guarded copy (a duplicated-metric
divergence, sibling of the ar120 Sortino case).

This is a reporting/ablation-path defect (the live promotion gate reads stockbench's
guarded drawdown; ablation._decide already finite-guards so an -inf forces a fail-closed
HOLD, not a spurious PROMOTE), but -inf in a reported/ablation metric is still wrong.

Tests pin the peak-guarded helper directly.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from hermes_quant.backtest.replay import _max_drawdown_pct


def test_zero_crossing_short_blowup_is_finite_not_neg_inf():
    """RED before fix: a NAV path whose running peak is <= 0 (a short blow-up crossing
    zero) divided by the raw peak → -inf. The peak-guarded form returns the finite,
    correctly-scaled drawdown.
    """
    # Running peak: [0, 0, 0, 2]. At indices 0-2 the bare /running_max divides by 0.
    equity = pd.Series([0.0, -5.0, -10.0, 2.0], dtype=float)
    got = _max_drawdown_pct(equity)

    assert math.isfinite(got), (
        f"ar121: max drawdown must be finite even when the running peak is <= 0 "
        f"(short blow-up through zero); got {got} (the un-guarded /running_max yields -inf)"
    )
    # Guarded form: worst (equity - peak) / max(peak, 1) = (-10 - 0)/1 = -10.0.
    assert got == pytest.approx(-10.0)


def test_negative_only_book_is_finite():
    """An all-negative NAV book (peak never positive) must not yield -inf/NaN."""
    equity = pd.Series([-1.0, -2.0, -3.0, -1.5], dtype=float)
    got = _max_drawdown_pct(equity)
    assert math.isfinite(got)


def test_positive_book_byte_identical_to_unguarded():
    """A positive-NAV book never has a non-positive peak, so the guard is a no-op —
    the result is identical to the bare /running_max form (no behavior change)."""
    equity = pd.Series([100.0, 110.0, 90.0, 120.0, 95.0], dtype=float)
    running_max = equity.cummax()  # [100,110,110,120,120]
    unguarded = float(((equity - running_max) / running_max).min())  # legacy form
    got = _max_drawdown_pct(equity)
    assert got == pytest.approx(unguarded), (
        "the guard must be a no-op on a positive-NAV book (where(peak>0) never rewrites "
        f"a positive peak); got {got} vs unguarded {unguarded}"
    )
    # The worst dd is the 120 -> 95 leg = (95-120)/120, not the 110 -> 90 leg.
    assert got == pytest.approx((95.0 - 120.0) / 120.0)


def test_legacy_unguarded_formula_was_neg_inf_on_zero_crossing():
    """Pins the BUG the fix closed (the helper is new, so this RED-proves the legacy
    inline formula directly): the pre-ar121 `(equity - cummax) / cummax` yields -inf on a
    zero-crossing peak, while the guarded helper returns a finite value. Were the guard
    ever removed, replay's reported max_drawdown_pct would regress to -inf here."""
    equity = pd.Series([0.0, -5.0, -10.0, 2.0], dtype=float)
    running_max = equity.cummax()  # [0, 0, 0, 2]
    legacy = float(((equity - running_max) / running_max).min())  # the OLD replay form
    assert legacy == float("-inf"), (
        "the legacy unguarded /running_max must blow up to -inf on a zero-crossing peak "
        f"(this is the bug ar121 fixes); got {legacy}"
    )
    # The shipped helper does NOT regress to that.
    assert math.isfinite(_max_drawdown_pct(equity))


def test_monotonic_up_is_zero_drawdown():
    assert _max_drawdown_pct(pd.Series([100.0, 110.0, 120.0], dtype=float)) == 0.0


def test_empty_series_is_zero():
    assert _max_drawdown_pct(pd.Series([], dtype=float)) == 0.0
