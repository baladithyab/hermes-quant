"""Tests for quantcore.evalx — CPCV splits, PSR/DSR, PBO (CSCV).

Citations live in the module under test; here we pin the BEHAVIOR:
splits never leak, purging/embargo actually remove indices, deflation
bites when n_trials > 1, and PBO separates noise-picked winners from a
genuinely dominant strategy.
"""

from __future__ import annotations

import math
import random

import pytest

from quantcore.evalx import (
    cpcv_splits,
    dsr,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
    pbo,
    psr,
    sharpe,
)

# --- CPCV splits -------------------------------------------------------------


def test_cpcv_split_count_is_n_choose_k():
    assert len(cpcv_splits(60, n_groups=6, k_test=2)) == math.comb(6, 2)
    assert len(cpcv_splits(60, n_groups=5, k_test=1)) == 5


def test_cpcv_no_train_test_overlap_even_with_purge_embargo():
    for purge, embargo in [(0, 0), (2, 0), (0, 3), (2, 3)]:
        for train, test in cpcv_splits(60, 6, 2, purge=purge, embargo=embargo):
            assert not set(train) & set(test)
            assert train == sorted(set(train))  # sorted, no dupes
            assert test == sorted(set(test))


def test_cpcv_all_obs_covered_across_splits():
    n_obs = 61  # uneven on purpose: first group takes the extra observation
    covered = set()
    for _, test in cpcv_splits(n_obs, 6, 2):
        covered.update(test)
    assert covered == set(range(n_obs))


def test_cpcv_test_groups_are_contiguous_blocks():
    for _, test in cpcv_splits(60, 6, 1):
        assert test == list(range(test[0], test[-1] + 1))


def test_cpcv_purge_removes_boundary_train_obs():
    n_obs, purge = 60, 2
    plain = cpcv_splits(n_obs, 6, 2, purge=0)
    purged = cpcv_splits(n_obs, 6, 2, purge=purge)
    for (train0, test0), (train1, test1) in zip(plain, purged):
        assert test0 == test1  # purge never touches the test set
        s, e = test0[0], test0[-1] + 1
        for i in range(max(0, s - purge), s):
            assert i in train0 and i not in train1
        for i in range(e, min(n_obs, e + purge)):
            if i not in test0:  # second test group may sit right after
                assert i not in train1
        assert set(train1) <= set(train0)


def test_cpcv_embargo_removes_post_test_window_only():
    n_obs, embargo = 60, 5
    plain = cpcv_splits(n_obs, 6, 1, purge=0, embargo=0)
    embargoed = cpcv_splits(n_obs, 6, 1, purge=0, embargo=embargo)
    for (train0, test0), (train1, test1) in zip(plain, embargoed):
        assert test0 == test1
        s, e = test0[0], test0[-1] + 1
        # window AFTER the test group is embargoed...
        for i in range(e, min(n_obs, e + embargo)):
            assert i not in train1
        # ...but observations BEFORE the test group are untouched
        if s > 0:
            assert (s - 1) in train1


def test_cpcv_deterministic():
    a = cpcv_splits(50, 5, 2, purge=1, embargo=2)
    b = cpcv_splits(50, 5, 2, purge=1, embargo=2)
    assert a == b


def test_cpcv_input_validation():
    with pytest.raises(ValueError):
        cpcv_splits(0)
    with pytest.raises(ValueError):
        cpcv_splits(60, n_groups=1)
    with pytest.raises(ValueError):
        cpcv_splits(5, n_groups=6)  # more groups than observations
    with pytest.raises(ValueError):
        cpcv_splits(60, n_groups=6, k_test=6)  # k_test must be < n_groups
    with pytest.raises(ValueError):
        cpcv_splits(60, n_groups=6, k_test=0)
    with pytest.raises(ValueError):
        cpcv_splits(60, purge=-1)
    with pytest.raises(ValueError):
        cpcv_splits(60, embargo=-1)
    with pytest.raises(ValueError):
        cpcv_splits(60.0)  # type: ignore[arg-type]


# --- sharpe ------------------------------------------------------------------


def test_sharpe_constant_positive_is_large_positive():
    s = sharpe([0.01] * 20)
    assert s > 100 and math.isinf(s)


def test_sharpe_alternating_mean_zero_is_zero():
    assert abs(sharpe([0.01, -0.01] * 10)) < 1e-12


def test_sharpe_sign_follows_mean():
    assert sharpe([-0.01, -0.02, -0.01, -0.03]) < 0
    assert sharpe([0.01, 0.02, 0.01, 0.03]) > 0


def test_sharpe_needs_two_obs():
    with pytest.raises(ValueError):
        sharpe([0.01])


# --- PSR ---------------------------------------------------------------------


def test_psr_zero_sr_vs_zero_benchmark_is_half():
    assert psr(0.0, 100, 0.0, 3.0) == pytest.approx(0.5, abs=1e-12)


def test_psr_monotonic_in_sr():
    vals = [psr(sr, 252, 0.0, 3.0) for sr in (0.0, 0.05, 0.1, 0.2)]
    assert vals == sorted(vals) and vals[0] < vals[-1]


def test_psr_monotonic_in_n():
    vals = [psr(0.1, n, 0.0, 3.0) for n in (10, 50, 252, 1000)]
    assert vals == sorted(vals) and vals[0] < vals[-1]


def test_psr_punishes_fat_tails_and_negative_skew():
    base = psr(0.1, 252, 0.0, 3.0)
    assert psr(0.1, 252, 0.0, 9.0) < base       # fatter tails
    assert psr(0.1, 252, -1.0, 3.0) < base      # negative skew
    assert psr(0.1, 252, 0.0, 3.0, sr_benchmark=0.05) < base


def test_psr_validation():
    with pytest.raises(ValueError):
        psr(0.1, 1, 0.0, 3.0)


# --- E[max SR] / DSR -----------------------------------------------------------


def test_expected_max_sharpe_grows_with_trials_and_variance():
    vals = [expected_max_sharpe(n, 0.04) for n in (2, 10, 100, 1000)]
    assert vals == sorted(vals) and vals[0] < vals[-1]
    assert expected_max_sharpe(10, 0.16) > expected_max_sharpe(10, 0.04)
    assert expected_max_sharpe(1, 0.04) == 0.0
    assert expected_max_sharpe(10, 0.0) == 0.0


def test_dsr_below_psr_when_multiple_trials():
    # deflation bites: the benchmark moves from 0 to E[max SR] > 0
    assert dsr(0.1, 252, 0.0, 3.0, n_trials=10, var_sr=0.04) < psr(0.1, 252, 0.0, 3.0)


def test_dsr_equals_psr_for_single_trial():
    assert dsr(0.1, 252, 0.0, 3.0, n_trials=1, var_sr=0.04) == pytest.approx(
        psr(0.1, 252, 0.0, 3.0), abs=1e-15
    )


def test_dsr_decreases_with_more_trials():
    vals = [dsr(0.1, 252, 0.0, 3.0, n_trials=n, var_sr=0.04) for n in (1, 5, 25, 125)]
    assert vals == sorted(vals, reverse=True) and vals[0] > vals[-1]


# --- inverse normal CDF ---------------------------------------------------------


def test_norm_ppf_known_value():
    assert abs(norm_ppf(0.975) - 1.959964) < 1e-3


def test_norm_ppf_roundtrip_and_symmetry():
    for p in (0.001, 0.01, 0.02425, 0.1, 0.5, 0.9, 0.99, 0.999):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-9)
    assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
    assert norm_ppf(0.25) == pytest.approx(-norm_ppf(0.75), abs=1e-9)


def test_norm_ppf_domain():
    for bad in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(ValueError):
            norm_ppf(bad)


# --- PBO (CSCV) ------------------------------------------------------------------


def _noise_matrix(rng: random.Random, t: int, n: int) -> list[list[float]]:
    """Pure noise, column-demeaned: every variant's full-sample mean is exactly
    0, so any IS edge is overfit by construction (the canonical false-strategy
    setup — IS-best is ALWAYS noise, and its OOS performance mirrors negative)."""
    m = [[rng.gauss(0.0, 0.01) for _ in range(n)] for _ in range(t)]
    means = [math.fsum(m[i][j] for i in range(t)) / t for j in range(n)]
    return [[m[i][j] - means[j] for j in range(n)] for i in range(t)]


def test_pbo_high_for_pure_noise():
    m = _noise_matrix(random.Random(7), t=64, n=30)
    out = pbo(m, n_partitions=8)
    assert out["pbo"] > 0.6


def test_pbo_low_when_one_strategy_dominates_every_block():
    rng = random.Random(7)
    t, n_alt = 64, 9
    m = [
        [0.01 + 0.002 * rng.gauss(0.0, 1.0)] + [rng.gauss(0.0, 0.01) for _ in range(n_alt)]
        for _ in range(t)
    ]
    out = pbo(m, n_partitions=8)
    assert out["pbo"] < 0.3


def test_pbo_output_shape_and_default_partitions():
    rng = random.Random(7)
    m = [[rng.gauss(0.0, 0.01) for _ in range(3)] for _ in range(32)]
    out = pbo(m)  # default n_partitions=16
    assert set(out) == {"pbo", "logits"}
    assert len(out["logits"]) == math.comb(16, 8)
    assert 0.0 <= out["pbo"] <= 1.0
    assert out["pbo"] == sum(1 for x in out["logits"] if x <= 0) / len(out["logits"])
    assert all(math.isfinite(x) for x in out["logits"])


def test_pbo_deterministic():
    rng = random.Random(11)
    m = [[rng.gauss(0.0, 0.01) for _ in range(4)] for _ in range(32)]
    assert pbo(m, n_partitions=4) == pbo(m, n_partitions=4)


def test_pbo_validation():
    good = [[0.01 * ((i + j) % 3 - 1) for j in range(3)] for i in range(32)]
    with pytest.raises(ValueError):
        pbo([], n_partitions=4)
    with pytest.raises(ValueError):
        pbo(good, n_partitions=7)  # odd
    with pytest.raises(ValueError):
        pbo(good, n_partitions=0)
    with pytest.raises(ValueError):
        pbo([[0.1], [0.2]] * 16, n_partitions=4)  # single strategy column
    with pytest.raises(ValueError):
        pbo([[0.1, 0.2], [0.1]] + good, n_partitions=4)  # ragged rows
    with pytest.raises(ValueError):
        pbo(good[:6], n_partitions=4)  # too few rows for per-half Sharpe
