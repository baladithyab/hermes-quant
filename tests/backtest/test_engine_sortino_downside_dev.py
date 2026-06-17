"""ar120 — backtest engine Sortino must use downside deviation about MAR=0 (RMS),
NOT std about the losers' own mean.

The prior `_annualised_sortino` computed downside dispersion as `np.std(neg, ddof=1)` —
the standard deviation of the negative days about THEIR OWN mean. That is the ar85
fail-open trap (the sibling eval.stockbench._compute_sortino was already fixed and its
docstring flags this exact form): a strategy whose losing days are all the SAME magnitude
(a fixed stop-loss, or steady down-drift at constant size) has ~zero dispersion about its
own mean → std_down collapses to 0 → the helper returned 0.0, masking a real net loss
from the d_sortino ablation comparison (backtest/ablation.py → cli/ablate.py C2a). And
clustered-but-unequal losses inflate the ratio many-fold.

Fix: downside deviation = sqrt(mean(min(r,0)²)) about MAR=0, across ALL days — so the
deviation stays proportional to the loss magnitude and the net-losing strategy scores a
finite, correctly-NEGATIVE Sortino the ablation can see.

(This reporting/ablation helper keeps its 0.0 sentinel for the no-data and no-downside
cases — its consumer differences two finite Sortinos; the legitimate +inf no-downside
signal lives in eval.stockbench, which feeds the live promotion gate.)
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_quant.backtest.engine import _annualised_sortino


def _rms_about_mar_sortino(rets: list[float]) -> float:
    """Reference: annualised mean / sqrt(mean(min(r,0)²))."""
    arr = np.array(rets, dtype=float)
    downside = np.minimum(arr, 0.0)
    dd = math.sqrt(float(np.mean(downside ** 2)))
    if dd < 1e-12:
        return float(np.mean(arr) * math.sqrt(252)) if np.mean(arr) > 0 else 0.0
    return float(np.mean(arr) / dd * math.sqrt(252))


def test_equal_magnitude_losses_net_loser_scores_negative_not_zero():
    """RED before fix: equal-magnitude losing days have ~zero std about their own mean,
    so std_down→0 returned 0.0 — masking a clear net loss. The RMS-about-MAR form yields
    the correct strongly-NEGATIVE Sortino.
    """
    # Net-losing book: small gains, four equal -5% loss days. Net cumulative < 0.
    rets = [0.005, 0.004, -0.05, -0.05, -0.05, -0.05, 0.003]
    got = _annualised_sortino(rets)
    expected = _rms_about_mar_sortino(rets)  # ≈ -11.28

    assert expected < 0  # sanity: the reference is negative for a net loser
    assert got == pytest.approx(expected, rel=1e-9), (
        f"ar120: equal-magnitude losses must score the RMS-about-MAR Sortino "
        f"({expected:.4f}), not the std-about-own-mean 0.0; got {got}"
    )
    assert got < 0, (
        "a net-losing strategy must score a NEGATIVE Sortino (the std-about-own-mean "
        f"form returned 0.0 here, the fail-open); got {got}"
    )
    # Explicitly NOT the masked-zero the bug produced.
    assert got != pytest.approx(0.0)


def test_clustered_losses_not_inflated():
    """Near-equal (but unequal) losses: the std-about-own-mean form makes the denominator
    tiny → a wildly inflated Sortino. RMS-about-MAR keeps it sane.
    """
    rets = [0.02, 0.03, 0.02, 0.025, -0.01, -0.0101, -0.0099, 0.018]
    got = _annualised_sortino(rets)
    expected = _rms_about_mar_sortino(rets)  # ≈ 26.89, NOT ~1647

    assert got == pytest.approx(expected, rel=1e-9), (
        f"ar120: clustered losses must score ~{expected:.2f}, not the inflated "
        f"std-about-own-mean ~1647; got {got}"
    )
    assert got < 100.0, (
        f"the std-about-own-mean form inflated this to ~1647; RMS-about-MAR keeps it "
        f"~27; got {got}"
    )


def test_no_downside_positive_book_scores_annualised_mean():
    """No negative day → downside risk zero. This reporting helper keeps its finite
    sentinel (annualised mean for a positive book), NOT +inf (that lives in stockbench).
    """
    rets = [0.01, 0.02, 0.015]
    got = _annualised_sortino(rets)
    assert math.isfinite(got)
    assert got == pytest.approx(float(np.mean(rets)) * math.sqrt(252))
    assert got > 0


def test_flat_book_is_zero():
    """A flat (all-zero) book has no downside and non-positive mean → 0.0 sentinel."""
    assert _annualised_sortino([0.0, 0.0, 0.0]) == 0.0


def test_insufficient_data_is_zero():
    assert _annualised_sortino([0.01]) == 0.0
    assert _annualised_sortino([]) == 0.0


def test_winning_book_with_some_losses_is_positive_and_finite():
    """A genuinely winning book (positive mean, some drawdown days) scores a positive,
    finite Sortino — non-vacuity that the fix did not just zero everything."""
    rets = [0.03, 0.04, -0.01, 0.02, -0.005, 0.025]
    got = _annualised_sortino(rets)
    assert math.isfinite(got) and got > 0
    assert got == pytest.approx(_rms_about_mar_sortino(rets), rel=1e-9)
