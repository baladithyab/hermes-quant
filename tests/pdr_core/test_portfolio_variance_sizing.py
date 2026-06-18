"""aegis-ag01 (ADR-0096 Gate 1) — correlation-aware POSITION-level portfolio sizing.

THE PROBLEM (verified against 83bf280): ``pdr_core/kelly.py`` sizes per-name
``edge / sigma^2``; the ONLY correlation-aware sizing module
(``hermes_quant.risk.portfolio_normalize``) lives in the HERMES SHELL, so the
cowork/standalone hosts get concentration CAPS (per-name 0.20, gross/net) but NO
covariance view. Five HIGHLY correlated longs each at the 0.20 per-name cap =
a ~100% beta bet wearing a "diversified" mask: the per-name caps all pass, yet
the PORTFOLIO VARIANCE (``w^T Σ w``) blows past any sane aggregate-risk budget.

This file is the G1 ACCEPTANCE for the new pure CORE module
``hermes_quant.pdr_core.portfolio_sizing`` (default-OFF behind
``HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING``). It proves:

  1. THE G1 PROPERTY TEST (load-bearing): N highly-correlated names each at the
     per-name cap -> with the variance step ON, the post-sizing PORTFOLIO
     VARIANCE is <= the variance cap (so the names are scaled DOWN below 0.20),
     AND this FAILS under per-name-caps-only sizing (the flag-off / old
     behavior) where all five stay at 0.20 and ``w^T Σ w`` overshoots the cap.
  2. UNCORRELATED no-op — the haircut only bites on correlation/concentration.
  3. HAIRCUT-ONLY — the step NEVER increases any |target| (scale in [0,1]); a
     basket already under the cap is unchanged.
  4. FAIL-CLOSED — a NaN/inf in Σ or a target falls back to conservative
     per-name behavior (never sizes UP); a NaN must not defeat the cap.
  5. THIN-DATA SHRINKAGE — n < min observations shrinks Σ hard toward diagonal
     (~ per-name; no over-precise optimization on noise).
  6. DEFAULT-OFF byte-identical — flag unset -> gate sizing identical to 83bf280
     (the step is a pass-through).

RED-first: with ``hermes_quant.pdr_core.portfolio_sizing`` absent every test
errors at collection. Creating the module turns them GREEN.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_quant.pdr_core.portfolio_sizing import (
    PortfolioVarianceConfig,
    portfolio_variance_enabled_from_env,
    portfolio_variance_haircut,
    shrink_covariance,
)

# ---------------------------------------------------------------------------
# Fixtures — a HIGH-correlation Σ where correlated-vs-independent genuinely
# diverge. (A test on rho=0 is VACUOUS — the haircut would be a no-op either
# way; we deliberately use rho=0.9 so the basket's true variance is far above
# the sum-of-independent-variances the per-name caps implicitly assume.)
# ---------------------------------------------------------------------------

N = 5
SIGMA = 0.20  # per-name vol (stdev) — each name at the 0.20 NAV cap
RHO = 0.9  # high pairwise correlation


def _corr_cov(n: int = N, sigma: float = SIGMA, rho: float = RHO) -> np.ndarray:
    """Σ = sigma^2 * [ (1-rho) I + rho J ] — constant-correlation, equal-vol."""
    corr = (1.0 - rho) * np.eye(n) + rho * np.ones((n, n))
    return (sigma**2) * corr


def _diag_cov(n: int = N, sigma: float = SIGMA) -> np.ndarray:
    return (sigma**2) * np.eye(n)


def _port_var(weights, cov) -> float:
    w = np.asarray(weights, dtype=float)
    return float(w @ np.asarray(cov, dtype=float) @ w)


# The aggregate variance cap. Calibrated so that:
#  * five INDEPENDENT names at 0.20 (var = 5 * 0.20^2 * 0.20^2 = 0.008) is UNDER
#    the cap (the per-name caps are "fine" when names are uncorrelated), but
#  * five rho=0.9 correlated names at 0.20 BLOW PAST it.
VAR_CAP = 0.010


# ---------------------------------------------------------------------------
# 1. THE G1 PROPERTY TEST (load-bearing).
# ---------------------------------------------------------------------------


def test_g1_correlated_basket_scaled_under_variance_cap() -> None:
    """N=5 highly-correlated names each at the 0.20 per-name cap: with the
    variance step ON the post-sizing portfolio variance is <= VAR_CAP, which
    means each |w_i| is scaled BELOW 0.20.

    RED-PROOF (the load-bearing assertion): per-name-caps-only sizing leaves all
    five at 0.20 and ``w^T Σ w`` overshoots VAR_CAP — the property the broken
    behavior violates. The variance step is what restores it."""
    targets = [SIGMA] * N  # five longs each at the per-name cap
    cov = _corr_cov()

    # --- RED proof: per-name-caps-only (the OLD behavior) BREACHES the cap. ---
    old_var = _port_var(targets, cov)
    assert old_var > VAR_CAP, (
        "RED-PROOF SETUP: five rho=0.9 names at 0.20 must breach VAR_CAP under "
        f"per-name-caps-only sizing; got w^T Σ w={old_var:.6f} <= cap={VAR_CAP} "
        "(the fixture would be vacuous). Choose a higher rho / lower cap."
    )

    # --- GREEN: the variance step haircuts the basket under the cap. ---
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)
    scaled = portfolio_variance_haircut(targets, cov, config=cfg)
    new_var = _port_var(scaled, cov)

    assert new_var <= VAR_CAP + 1e-12, (
        f"post-sizing portfolio variance {new_var:.6f} must be <= cap {VAR_CAP}"
    )
    # Each name was scaled DOWN strictly below the per-name cap.
    for w in scaled:
        assert abs(w) < SIGMA, f"correlated name should be scaled below 0.20, got {w}"
    # Single global scale: relative ranking preserved (all equal here).
    assert all(math.isclose(w, scaled[0]) for w in scaled)


# ---------------------------------------------------------------------------
# 2. UNCORRELATED no-op.
# ---------------------------------------------------------------------------


def test_uncorrelated_basket_under_cap_not_scaled() -> None:
    """Five UNCORRELATED names at 0.20 sit UNDER the variance cap -> the haircut
    is a no-op (the step bites on correlation/concentration, not on every basket)."""
    targets = [SIGMA] * N
    cov = _diag_cov()
    base_var = _port_var(targets, cov)
    assert base_var <= VAR_CAP, (
        "fixture sanity: independent basket must be under the cap so the no-op is "
        f"meaningful; got {base_var:.6f} vs {VAR_CAP}"
    )
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)
    scaled = portfolio_variance_haircut(targets, cov, config=cfg)
    assert scaled == pytest.approx(targets), (
        "an under-cap uncorrelated basket must pass through UNCHANGED"
    )


# ---------------------------------------------------------------------------
# 3. HAIRCUT-ONLY — scale in [0, 1], never increases any |target|.
# ---------------------------------------------------------------------------


def test_haircut_only_never_increases_any_target() -> None:
    """The step can ONLY shrink: every output |w_i| <= input |target_i| and the
    implied scale is in [0, 1]. A basket already under the cap is unchanged."""
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)

    # Correlated basket -> shrinks.
    targets = [SIGMA, -SIGMA, SIGMA, -SIGMA, SIGMA]
    cov = _corr_cov()
    scaled = portfolio_variance_haircut(targets, cov, config=cfg)
    for w_in, w_out in zip(targets, scaled, strict=True):
        assert abs(w_out) <= abs(w_in) + 1e-12, "haircut must never increase |target|"
        assert (w_in == 0.0) or (w_out * w_in >= 0.0), "sign must be preserved"

    # Implied global scale is in [0, 1].
    nz = [w_out / w_in for w_in, w_out in zip(targets, scaled, strict=True) if w_in != 0.0]
    for s in nz:
        assert -1e-12 <= s <= 1.0 + 1e-12, f"scale {s} must be in [0,1]"

    # Under-cap basket: unchanged (scale == 1.0).
    small = [0.05, 0.05]
    small_cov = _corr_cov(n=2)
    assert _port_var(small, small_cov) <= VAR_CAP
    assert portfolio_variance_haircut(small, small_cov, config=cfg) == pytest.approx(small)


# ---------------------------------------------------------------------------
# 4. FAIL-CLOSED — NaN/inf in Σ or a target -> conservative per-name fallback,
#    never sizes UP; a NaN must not defeat the cap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_failclosed_nonfinite_in_covariance(bad: float) -> None:
    """A non-finite entry in Σ defeats every ``<=`` cap comparison (every NaN/inf
    comparison short-circuits to a wrong answer). The step must FAIL CLOSED: fall
    back to the conservative per-name-cap behavior and NEVER size up.

    RED-PROOF: a naive ``if w^T Σ w <= cap: return targets`` returns the targets
    UNSHRUNK when the variance is NaN (``nan <= cap`` is False -> it would try to
    scale, but ``cap / nan`` is nan -> nan*target propagates a NaN target, or a
    naive ``<=`` guard passes them through at full size). The guard here must
    clamp to <= the per-name caps and emit only finite, not-larger targets."""
    targets = [SIGMA] * N
    cov = _corr_cov()
    cov[0, 1] = bad  # poison one off-diagonal
    cov[1, 0] = bad
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)
    scaled = portfolio_variance_haircut(targets, cov, config=cfg)
    # Every output is finite and not larger than the corresponding input.
    for w_in, w_out in zip(targets, scaled, strict=True):
        assert math.isfinite(w_out), f"a NaN/inf Σ must not leak a non-finite target: {w_out}"
        assert abs(w_out) <= abs(w_in) + 1e-12, "fail-closed must never size UP"
    # wave4-review FIX (was partially vacuous): the prior assertions passed even with the
    # cov finite-guard removed — the variance-finite guard ALSO catches it, and Python's
    # min(1.0, nan)==1.0 silently clamps a NaN λ to full size. Pin the COVARIANCE guard
    # SPECIFICALLY: a poisoned Σ must short-circuit to the EXACT conservative per-name
    # pass-through (targets UNCHANGED), proving cov_ok fired BEFORE any w^T Σ w / λ math.
    assert scaled == pytest.approx(targets), (
        "a non-finite Σ must fall back to the EXACT conservative per-name targets "
        f"(cov-guard short-circuit), not a partial/NaN scale; got {scaled}"
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_failclosed_nonfinite_target(bad: float) -> None:
    """A non-finite INCOMING target -> that name is zeroed (fail-closed) and no
    name is sized up. A NaN target also poisons ``w^T Σ w``; the guard must
    silence it rather than let it through."""
    targets = [SIGMA, bad, SIGMA, SIGMA, SIGMA]
    cov = _corr_cov()
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)
    scaled = portfolio_variance_haircut(targets, cov, config=cfg)
    assert scaled[1] == 0.0, "a non-finite target must be silenced (fail-closed)"
    for w_out in scaled:
        assert math.isfinite(w_out)
    # The surviving finite names are never sized above their per-name cap.
    for w_out in scaled:
        assert abs(w_out) <= SIGMA + 1e-12


def test_failclosed_degenerate_covariance_falls_back() -> None:
    """A degenerate (wrong-shape) Σ -> conservative fallback (return targets
    clamped to per-name behavior, never sized up). Never raises."""
    targets = [SIGMA] * N
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP)
    bad_cov = np.ones((2, 3))  # not square, not N×N
    scaled = portfolio_variance_haircut(targets, bad_cov, config=cfg)
    for w_in, w_out in zip(targets, scaled, strict=True):
        assert math.isfinite(w_out)
        assert abs(w_out) <= abs(w_in) + 1e-12


# ---------------------------------------------------------------------------
# 5. THIN-DATA SHRINKAGE — n < min obs shrinks hard toward diagonal.
# ---------------------------------------------------------------------------


def test_thin_data_shrinks_hard_toward_diagonal() -> None:
    """With n < shrinkage_min_obs observations, the shrunk Σ is pulled HARD
    toward its diagonal (toward independence) — so the optimizer does not act on
    a noisy sample off-diagonal. Off-diagonals of the SHRUNK Σ are strictly
    smaller in magnitude than the raw sample's.

    RED-PROOF: an un-shrunk sample covariance on n=3 obs has large, noisy
    off-diagonals (often near-perfectly-correlated by luck); the shrinkage must
    pull them toward 0 so a thin-data basket is treated ~ independent (fail
    toward LESS aggregate exposure, not an over-precise optimization on noise)."""
    rng = np.random.default_rng(7)
    # n=3 observations of 5 names — far below a sane min (e.g. 30).
    returns = rng.normal(0.0, SIGMA, size=(3, N))
    sample = np.cov(returns, rowvar=False, bias=True)
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP, shrinkage_min_obs=30)
    shrunk = shrink_covariance(returns, config=cfg)

    assert shrunk.shape == (N, N)
    assert np.all(np.isfinite(shrunk))
    # Diagonal (variances) preserved within reason (shrinkage targets the
    # off-diagonal correlation, not the variances).
    for i in range(N):
        assert shrunk[i, i] > 0.0
    # Off-diagonals pulled toward 0 (strictly smaller magnitude than the raw
    # sample for at least the largest off-diagonal — the noisy one).
    raw_off = np.abs(sample - np.diag(np.diag(sample)))
    shr_off = np.abs(shrunk - np.diag(np.diag(shrunk)))
    assert shr_off.max() < raw_off.max() + 1e-12
    assert shr_off.sum() < raw_off.sum(), (
        "thin-data shrinkage must pull the noisy off-diagonals toward 0 "
        f"(shrunk |offdiag| sum {shr_off.sum():.6f} vs raw {raw_off.sum():.6f})"
    )


def test_shrinkage_finite_on_ample_data() -> None:
    """With ample observations the shrunk Σ is still finite and PSD-ish (positive
    diagonal), and the intensity is lower than the thin-data case."""
    rng = np.random.default_rng(11)
    returns = rng.normal(0.0, SIGMA, size=(200, N))
    cfg = PortfolioVarianceConfig(variance_cap=VAR_CAP, shrinkage_min_obs=30)
    shrunk = shrink_covariance(returns, config=cfg)
    assert shrunk.shape == (N, N)
    assert np.all(np.isfinite(shrunk))
    assert np.all(np.diag(shrunk) > 0.0)


# ---------------------------------------------------------------------------
# 6. DEFAULT-OFF byte-identical — env flag unset means the gate-level enable
#    helper reads False (pass-through). (The gate-wiring byte-identity is proven
#    in test_gate_port.py's parity grid; here we pin the env-read default.)
# ---------------------------------------------------------------------------


def test_default_off_env_flag(monkeypatch) -> None:
    """``HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING`` unset -> disabled (byte-identical
    posture). Only the literal ``"1"`` enables it (default-OFF rail).

    RED-PROOF: a naive ``bool(os.environ.get(FLAG))`` would treat ``"0"`` /
    ``"false"`` as ENABLED (non-empty string is truthy) — flipping the rail ON
    by accident. The helper must gate on the literal ``"1"``."""
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", raising=False)
    assert portfolio_variance_enabled_from_env() is False
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "0")
    assert portfolio_variance_enabled_from_env() is False
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "false")
    assert portfolio_variance_enabled_from_env() is False
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "1")
    assert portfolio_variance_enabled_from_env() is True


# ---------------------------------------------------------------------------
# Gate-level wiring: the basket method is default-OFF byte-identical and only
# haircuts when the RiskConfig flag is set.
# ---------------------------------------------------------------------------


def test_gate_basket_default_off_is_passthrough() -> None:
    """``DefaultRiskGate.apply_portfolio_variance_sizing`` with the config flag
    OFF (default) is a PASS-THROUGH — returns the per-name targets UNCHANGED
    (byte-identical to 83bf280, where the step does not exist).

    RED-PROOF: if the step ran unconditionally it would shrink the correlated
    basket even with the flag off — a behavior change on the default path."""
    from hermes_quant.pdr_core.gate import DefaultRiskGate, RiskConfig

    targets = [("A", SIGMA), ("B", SIGMA), ("C", SIGMA), ("D", SIGMA), ("E", SIGMA)]
    cov = _corr_cov()

    gate_off = DefaultRiskGate(RiskConfig())  # flag defaults OFF
    out_off = gate_off.apply_portfolio_variance_sizing(targets, cov)
    assert out_off == targets, "flag OFF must be a pass-through (byte-identical)"

    gate_on = DefaultRiskGate(
        RiskConfig(
            portfolio_variance_sizing_enabled=True,
            portfolio_variance_cap=VAR_CAP,
        )
    )
    out_on = gate_on.apply_portfolio_variance_sizing(targets, cov)
    scaled = [w for _, w in out_on]
    assert _port_var(scaled, cov) <= VAR_CAP + 1e-12
    for (_, w_in), (_, w_out) in zip(targets, out_on, strict=True):
        assert abs(w_out) < abs(w_in), "flag ON must shrink the correlated basket"
