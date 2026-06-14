"""Tests for hermes_quant.evaluation.validation (B32 validation suite).

Offline-deterministic (no live network, seeded RNG). Covers the test matrix
from the B32 research note (docs/research/2026-05-31-r-B32.md §3.3):

- block length ≈ 1 for IID, ∈ [7, 13] for AR(1) ρ=0.5
- byte-deterministic to_dict() under a fixed seed
- permutation p high for noise, low for genuine timing signal
- bootstrap CI ordering (ci_low < point < ci_high)
- scipy-absent fallback to percentile CI + warning, no crash
- < 30-obs low-power guard (DSR omitted, suite still runs)
- validation.json schema round-trips through json.dumps(sort_keys=True)
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from hermes_quant.evaluation import validation as validation_mod
from hermes_quant.evaluation.validation import (
    BootstrapCI,
    PermutationResult,
    ValidationReport,
    validate_returns,
)


# --- fixtures ----------------------------------------------------------------
def _ar1(n: int, rho: float, *, scale: float = 1.0, seed: int = 0) -> np.ndarray:
    """Deterministic AR(1) series x[t] = rho*x[t-1] + e[t]."""
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = rho * x[t - 1] + e[t]
    return x * scale


def _noise(n: int, *, drift: float = 0.0003, vol: float = 0.01, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n) * vol + drift


# --- block-length selection --------------------------------------------------
def test_block_length_iid_is_one():
    """White noise has no serial dependence → block length collapses to 1."""
    iid = _noise(500, drift=0.0, vol=1.0, seed=11)
    b = validation_mod._politis_white_block_length(iid)
    assert b == pytest.approx(1.0, abs=0.5)


def test_block_length_ar1_matches_pwsd():
    """AR(1) ρ=0.5 → block length in the PWSD reference band [7, 13]."""
    ar = _ar1(1000, 0.5, seed=3)
    b = validation_mod._politis_white_block_length(ar)
    assert 7.0 <= b <= 13.0, b


def test_block_length_for_sharpe_is_conservative():
    """Sharpe block length = max(level, squared) ≥ the level estimate."""
    ar = _ar1(1000, 0.5, seed=3)
    b_level = validation_mod._politis_white_block_length(ar)
    b_sharpe = validation_mod._block_length_for_sharpe(ar)
    assert b_sharpe >= b_level


# --- determinism -------------------------------------------------------------
def test_validation_report_deterministic():
    """Same seed twice → byte-identical to_dict()."""
    r = _noise(120, seed=7)
    rep1 = validate_returns(r, bars_per_year=252, n_permutations=200, n_resamples=400, seed=42)
    rep2 = validate_returns(r, bars_per_year=252, n_permutations=200, n_resamples=400, seed=42)
    d1 = json.dumps(rep1.to_dict(), sort_keys=True)
    d2 = json.dumps(rep2.to_dict(), sort_keys=True)
    assert d1 == d2


def test_stationary_bootstrap_indices_deterministic():
    rng1 = np.random.default_rng(42)
    rng2 = np.random.default_rng(42)
    a = validation_mod._stationary_bootstrap_indices(50, 5.0, 10, rng1)
    b = validation_mod._stationary_bootstrap_indices(50, 5.0, 10, rng2)
    assert np.array_equal(a, b)
    assert a.shape == (10, 50)
    assert a.min() >= 0 and a.max() < 50


# --- permutation null --------------------------------------------------------
def test_permutation_pvalue_high_for_noise():
    """Random returns → timing permutation p-value not significant."""
    r = _noise(150, seed=21)
    rep = validate_returns(r, bars_per_year=252, n_permutations=500, n_resamples=300, seed=42)
    timing = next(p for p in rep.permutation if p.statistic == "timing")
    assert timing.p_value > 0.05
    assert not timing.significant


def test_permutation_pvalue_low_for_signal():
    """Positively-autocorrelated returns (momentum timing structure) → p < alpha."""
    sig = _ar1(200, 0.6, scale=0.01, seed=5)
    rep = validate_returns(sig, bars_per_year=252, n_permutations=500, n_resamples=300, seed=42)
    timing = next(p for p in rep.permutation if p.statistic == "timing")
    assert timing.p_value < 0.05
    assert timing.significant


def test_sharpe_is_not_permutation_tested():
    """Sharpe / total-return are permutation-invariant → not in the MC test
    (they live in the bootstrap block instead)."""
    r = _noise(120, seed=7)
    rep = validate_returns(r, bars_per_year=252, n_permutations=100, n_resamples=200, seed=42)
    perm_stats = {p.statistic for p in rep.permutation}
    assert "sharpe" not in perm_stats
    assert "total_return" not in perm_stats
    assert "timing" in perm_stats
    boot_stats = {c.statistic for c in rep.bootstrap}
    assert "sharpe" in boot_stats


# --- bootstrap CI ------------------------------------------------------------
def test_bootstrap_lower_bound_below_point():
    """CI ordering: ci_low < point < ci_high for a positive-Sharpe series."""
    rng = np.random.default_rng(2)
    r = rng.standard_normal(150) * 0.01 + 0.002  # positive drift
    rep = validate_returns(r, bars_per_year=252, n_permutations=100, n_resamples=800, seed=42)
    sharpe_ci = next(c for c in rep.bootstrap if c.statistic == "sharpe")
    assert sharpe_ci.ci_low < sharpe_ci.point < sharpe_ci.ci_high
    assert sharpe_ci.method == "stationary_block"
    assert sharpe_ci.confidence_level == 0.95
    assert sharpe_ci.block_length >= 1.0


def test_excess_return_stats_present_with_bh():
    """Providing bh_returns adds excess-return permutation + bootstrap."""
    r = _noise(120, drift=0.001, seed=4)
    bh = _noise(120, drift=0.0002, seed=9)
    rep = validate_returns(
        r, bars_per_year=252, bh_returns=bh, n_permutations=100, n_resamples=200, seed=42
    )
    perm_stats = {p.statistic for p in rep.permutation}
    boot_stats = {c.statistic for c in rep.bootstrap}
    assert "timing_excess" in perm_stats
    assert "excess_return" in boot_stats


def test_excess_pairs_same_date_bars_under_misaligned_nan():
    """cs39: a non-finite in ONE series at an index the OTHER series lacks must
    drop that bar from BOTH (joint mask) so excess pairs SAME-DATE strat/bh
    bars. The old code compressed each series independently then subtracted
    positionally — pairing strat[i] with the wrong (shifted) bh bar.

    strat = [.01, .02, .03, .04, .05]
    bh    = [.01, NaN, .02, .03, .04]
    The NaN is at index 1 of bh only. Correct same-date pairing drops index 1
    from BOTH -> excess = [.01-.01, .03-.02, .04-.03, .05-.04] = [0, .01, .01, .01].
    The buggy independent-compress yields bh_compressed[1:] shifted left, so
    excess collapses to [0, 0, 0, 0] (strategy looks like it never outperforms).
    The excess_return bootstrap point is the compounded total return of the
    PAIRED excess series; assert it matches the same-date-aligned hand value.
    """
    strat = np.array([0.01, 0.02, 0.03, 0.04, 0.05])
    bh = np.array([0.01, np.nan, 0.02, 0.03, 0.04])

    # Hand-computed SAME-DATE-aligned excess (joint mask drops index 1 from both).
    expected_excess = np.array([0.0, 0.01, 0.01, 0.01])
    expected_point = float(np.prod(1.0 + expected_excess) - 1.0)

    rep = validate_returns(
        strat,
        bars_per_year=252,
        bh_returns=bh,
        n_permutations=50,
        n_resamples=50,
        seed=42,
    )
    excess_ci = next(c for c in rep.bootstrap if c.statistic == "excess_return")
    # The buggy positional-subtract collapses excess to all-zeros -> point == 0.0;
    # the same-date pairing yields a strictly positive compounded excess.
    assert excess_ci.point == pytest.approx(expected_point)
    assert excess_ci.point > 0.0


def test_excess_all_finite_is_byte_identical():
    """The all-finite common case must be unchanged by the cs39 joint-mask fix
    (joint mask == all True), so the excess_return point equals the naive
    positional subtract of the two fully-finite series."""
    r = _noise(120, drift=0.001, seed=4)
    bh = _noise(120, drift=0.0002, seed=9)
    m = min(r.size, bh.size)
    expected_point = float(np.prod(1.0 + (r[:m] - bh[:m])) - 1.0)
    rep = validate_returns(
        r, bars_per_year=252, bh_returns=bh, n_permutations=50, n_resamples=50, seed=42
    )
    excess_ci = next(c for c in rep.bootstrap if c.statistic == "excess_return")
    assert excess_ci.point == pytest.approx(expected_point)


# --- cs46: non-finite bootstrap samples must not corrupt the promotion-gate CI
def test_degenerate_series_yields_finite_ci_low_not_nan():
    """cs46: a zero-variance (constant) return series makes _sharpe return ±inf
    on EVERY stationary-bootstrap resample (a single repeated block is still
    constant). np.nanpercentile drops NaN but KEEPS inf, and the tail
    interpolation subtract(b, a) on inf yields NaN -> a NaN ci_low.

    sharpe_95ci_lower is the promotion-gate threshold (react/live.py:38,
    governance/promotion.py:274 require >= 1.0). ``NaN < 1.0`` is False in
    Python, so a NaN lower bound silently PASSES a gate that must fail-closed.
    After the fix the lower bound is FINITE and conservative (does not satisfy
    the >= 1.0 gate)."""
    r = np.full(42, 0.001)  # perfectly constant -> zero variance, +inf Sharpe
    rep = validate_returns(r, bars_per_year=252, n_permutations=50, n_resamples=400, seed=42)
    sharpe_ci = next(c for c in rep.bootstrap if c.statistic == "sharpe")
    # The bug: ci_low/ci_high == NaN. The fix: a finite conservative bound.
    assert np.isfinite(sharpe_ci.ci_low), "ci_low must be finite, not NaN (cs46)"
    assert np.isfinite(sharpe_ci.ci_high), "ci_high must be finite, not NaN (cs46)"
    # Fail-closed: the conservative lower bound does NOT clear the >=1.0 gate.
    assert not (sharpe_ci.ci_low >= 1.0)
    # And it round-trips through json.dumps(allow_nan=False) as a real number,
    # not the None that a leaked NaN would render as.
    rep_dict = rep.to_dict()
    json.dumps(rep_dict, allow_nan=False)  # raises if any NaN/inf leaked
    sharpe_d = next(b for b in rep_dict["bootstrap"] if b["statistic"] == "sharpe")
    assert sharpe_d["ci_low"] is not None and np.isfinite(sharpe_d["ci_low"])


def test_percentile_ci_filters_inf_samples():
    """cs46 (unit): _percentile_ci must drop ±inf samples before the percentile
    so an inf in the tail-interpolation window cannot poison ci_low/ci_high to
    NaN. A bootstrap distribution mixing finite values with a few +inf samples
    must yield the SAME finite CI as the all-finite subset."""
    finite = np.linspace(-2.0, 3.0, 200)
    contaminated = np.concatenate([finite, np.full(7, np.inf)])
    lo_c, hi_c = validation_mod._percentile_ci(contaminated, 0.95)
    lo_f, hi_f = validation_mod._percentile_ci(finite, 0.95)
    assert np.isfinite(lo_c) and np.isfinite(hi_c)
    assert lo_c == pytest.approx(lo_f)
    assert hi_c == pytest.approx(hi_f)
    # All-non-finite distribution -> conservative finite bound, never NaN.
    all_inf = np.full(50, np.inf)
    lo_z, hi_z = validation_mod._percentile_ci(all_inf, 0.95)
    assert np.isfinite(lo_z) and np.isfinite(hi_z)
    assert not (lo_z >= 1.0)  # fails-closed against the promotion gate


def test_normal_variance_ci_unchanged_by_cs46_filter():
    """A finite-variance series leaves every bootstrap sample finite, so the
    finite mask is all-True and the percentile is byte-identical to the old
    np.nanpercentile path. Asserts _percentile_ci == np.nanpercentile on a
    fully-finite distribution."""
    rng = np.random.default_rng(11)
    samples = rng.standard_normal(5000) * 1.5 + 0.4  # all finite
    lo, hi = validation_mod._percentile_ci(samples, 0.95)
    # Equal to the old nanpercentile path to within floating-point (the only
    # difference is which numpy percentile kernel runs; on all-finite input the
    # finite mask is all-True so the statistic is the same percentile).
    assert lo == pytest.approx(float(np.nanpercentile(samples, 2.5)))
    assert hi == pytest.approx(float(np.nanpercentile(samples, 97.5)))


# --- cs48: a non-finite observed Sharpe must not propagate a NaN DSR ---------
def test_degenerate_series_dsr_is_finite_conservative_not_nan():
    """cs48 (sibling of cs46): a zero-variance OOS series with n >= 30 makes
    _sharpe return +inf (positive mean) or -inf (negative mean). The DSR path
    (dsr.deflated_sharpe) then computes
    ``variance_term = 1 - skew*SR + (kurt-1)/4*SR**2``; with a constant series
    skew==0, so ``skew*inf == nan`` -> variance_term is NaN, the
    ``variance_term <= 0`` guard is NaN<=0 == False, and
    ``Φ(sr_diff*sqrt(n-1)/sqrt(NaN))`` collapses to NaN. validation.py's
    try/except only catches ValueError/ZeroDivisionError, so the NaN is NOT
    caught and NO warning is emitted: the artifact reports deflated_sharpe NaN
    (-> null in JSON) INDISTINGUISHABLE from the legitimate n<30 low-power
    omission, silently erasing the false-discovery hedge.

    After the fix the DSR is a FINITE, conservative value that fails any
    ``dsr >= floor`` gate (mirrors cs46 returning a conservative finite bound),
    and a warning records the degeneracy."""
    r = np.full(42, 0.001)  # n>=30, zero variance -> +inf observed Sharpe
    rep = validate_returns(r, bars_per_year=252, n_permutations=50, n_resamples=200, seed=42)
    # The bug: deflated_sharpe is NaN. The fix: a finite conservative value.
    assert np.isfinite(rep.deflated_sharpe), "deflated_sharpe must be finite, not NaN (cs48)"
    # Conservative = does NOT clear a DSR floor (e.g. dsr_floor 0.5 / 0.95);
    # a probability of 0.0 is the maximally-conservative "no confidence" value.
    assert rep.deflated_sharpe == 0.0
    # n>=30 so the legitimate low-power warning must NOT be the explanation;
    # instead a DSR-degeneracy warning distinguishes this from a true omission.
    assert not any("low statistical power" in w for w in rep.warnings)
    assert any("deflated_sharpe" in w.lower() for w in rep.warnings)
    # And the JSON artifact now carries a real number, not the null that a NaN
    # would render as (which masquerades as the n<30 omission).
    rep_dict = rep.to_dict()
    json.dumps(rep_dict, allow_nan=False)  # raises if any NaN/inf leaked
    assert rep_dict["deflated_sharpe"] == 0.0


def test_degenerate_negative_mean_series_dsr_is_finite():
    """cs48: the -inf branch (zero variance, NEGATIVE mean) must also yield a
    finite conservative DSR, not NaN. A losing constant strategy should report
    0.0 confidence, never a null that the gate cannot fail-close on."""
    r = np.full(42, -0.002)  # n>=30, zero variance -> -inf observed Sharpe
    rep = validate_returns(r, bars_per_year=252, n_permutations=50, n_resamples=200, seed=42)
    assert np.isfinite(rep.deflated_sharpe)
    assert rep.deflated_sharpe == 0.0


def test_dsr_unchanged_for_finite_variance_series():
    """cs48: a normal finite-variance series with n>=30 never triggers the
    non-finite guard, so the DSR is byte-identical to the un-guarded
    dsr.deflated_sharpe computed directly from the same observed Sharpe / skew /
    kurtosis. The guard fires ONLY on the degenerate (non-finite) input."""
    r = _noise(120, drift=0.001, seed=12)
    rep = validate_returns(r, bars_per_year=252, n_permutations=50, n_resamples=200, seed=42)
    # Recompute the expected DSR through the exact same inputs validate_returns
    # feeds dsr.deflated_sharpe.
    from hermes_quant.evaluation.dsr import deflated_sharpe

    arr = validation_mod._to_array(r)
    obs = validation_mod._sharpe(arr, bars_per_year=252)
    skew = validation_mod._sample_skew(arr)
    kurt = validation_mod._sample_kurtosis(arr)
    expected = deflated_sharpe(
        observed_sharpe=obs, n_trials=1, n_observations=arr.size, skew=skew, kurtosis=kurt
    )
    assert rep.deflated_sharpe == expected
    assert np.isfinite(rep.deflated_sharpe)
    assert 0.0 <= rep.deflated_sharpe <= 1.0


# --- scipy-absent fallback ---------------------------------------------------
def test_scipy_absent_falls_back_to_percentile(monkeypatch):
    """With scipy unavailable: percentile CI from the stationary bootstrap +
    a warning, no crash."""
    monkeypatch.setattr(validation_mod, "_HAS_SCIPY", False)
    r = _noise(120, drift=0.001, seed=8)
    rep = validate_returns(r, bars_per_year=252, n_permutations=100, n_resamples=300, seed=42)
    # Still produces a CI...
    sharpe_ci = next(c for c in rep.bootstrap if c.statistic == "sharpe")
    assert np.isfinite(sharpe_ci.ci_low) and np.isfinite(sharpe_ci.ci_high)
    assert sharpe_ci.method == "stationary_block"
    # ...and warns about the missing BCa path.
    assert any("scipy unavailable" in w for w in rep.warnings)


# --- low-power guard ---------------------------------------------------------
def test_min_observations_guard():
    """< 30 obs → DSR omitted (NaN) + low-power warning; suite still runs."""
    r = _noise(20, seed=6)
    rep = validate_returns(r, bars_per_year=252, n_permutations=100, n_resamples=200, seed=42)
    assert np.isnan(rep.deflated_sharpe)
    assert any("low statistical power" in w for w in rep.warnings)
    # permutation + bootstrap still produced
    assert rep.permutation
    assert rep.bootstrap


def test_dsr_present_for_adequate_sample():
    """>= 30 obs → DSR is a finite probability in [0, 1]."""
    r = _noise(120, drift=0.001, seed=12)
    rep = validate_returns(r, bars_per_year=252, n_permutations=50, n_resamples=200, seed=42)
    assert np.isfinite(rep.deflated_sharpe)
    assert 0.0 <= rep.deflated_sharpe <= 1.0


def test_empty_series_does_not_crash():
    """Degenerate (< 2 obs) input → empty report with a warning, no raise."""
    rep = validate_returns(np.array([0.01]), bars_per_year=252, seed=42)
    assert rep.permutation == []
    assert rep.bootstrap == []
    assert rep.warnings


# --- JSON schema -------------------------------------------------------------
def test_validation_json_schema():
    """to_dict() round-trips through json.dumps(sort_keys=True); keys present."""
    r = _noise(120, drift=0.001, seed=7)
    bh = _noise(120, drift=0.0002, seed=9)
    rep = validate_returns(
        r, bars_per_year=252, bh_returns=bh, n_permutations=100, n_resamples=300, seed=42
    )
    d = rep.to_dict()
    blob = json.dumps(d, sort_keys=True, default=str)
    parsed = json.loads(blob)  # standard JSON: NaN rendered as None, no token errors
    for key in ("seed", "n_observations", "bars_per_year", "deflated_sharpe",
                "permutation", "bootstrap", "walk_forward", "warnings"):
        assert key in parsed
    assert parsed["seed"] == 42
    # frozen dataclasses are immutable
    assert isinstance(rep, ValidationReport)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.seed = 1  # type: ignore[misc]


def test_dataclass_to_dict_shapes():
    ci = BootstrapCI(
        statistic="sharpe", point=1.0, ci_low=0.1, ci_high=2.0,
        confidence_level=0.95, n_resamples=10, block_length=3.0, method="stationary_block",
    )
    assert ci.to_dict()["statistic"] == "sharpe"
    pr = PermutationResult(
        statistic="timing", observed=1.0, p_value=0.01, n_permutations=10,
        perm_mean=0.0, perm_std=1.0, alpha=0.05,
    )
    assert pr.to_dict()["significant"] is True


def test_nan_rendered_as_null_in_json():
    """NaN floats (e.g. low-power DSR) become JSON null, not the bare NaN token.

    Note: the literal word "NaN" may legitimately appear inside human-readable
    warning strings; what matters is that no *numeric* NaN token leaks, which
    we assert via strict-JSON parsing (allow_nan=False) and the null DSR.
    """
    r = _noise(20, seed=6)
    rep = validate_returns(r, bars_per_year=252, n_permutations=20, n_resamples=50, seed=42)
    # allow_nan=False raises ValueError if any float is NaN/inf — proves none leak.
    blob = json.dumps(rep.to_dict(), sort_keys=True, allow_nan=False)
    assert json.loads(blob)["deflated_sharpe"] is None
