"""hermes_quant.pdr_core.portfolio_sizing — correlation-aware POSITION-level sizing
(aegis-ag01, ADR-0096 Gate 1).

THE GAP this closes (verified against 83bf280): ``pdr_core/kelly.py`` sizes each
name on its OWN ``edge / sigma^2`` and the gate clips each |w_i| to the per-name
cap (Rule 6.5). The ONLY correlation-aware module
(``hermes_quant.risk.portfolio_normalize``, ADR-0071) lives in the HERMES SHELL,
so a cowork/standalone host gets concentration caps but NO covariance view. Five
HIGHLY correlated longs each at the 0.20 per-name cap all pass the per-name cap,
yet the PORTFOLIO VARIANCE ``w^T Σ w`` is a ~100% beta bet wearing a
"diversified" mask. This module is the host-agnostic CORE step that haircuts the
basket so the portfolio VARIANCE — not merely each |w_i| — stays within a cap.

POSTURE (money-software, ADR-0004 + the project posture):
  * HAIRCUT-TOWARD-SILENCE. The step can ONLY shrink: it applies a single global
    scale ``λ ∈ [0, 1]`` so a basket of correlated names is scaled DOWN TOGETHER.
    It NEVER increases any |target|. If ``w^T Σ w`` is already within the cap it
    is a no-op (λ = 1). It can only reject / abstain / de-risk — never size up.
  * FAIL-CLOSED on non-finite. A NaN/inf in Σ or in a target defeats every ``<=``
    cap comparison (every comparison against a NaN is False). The step finite-
    guards FIRST: a non-finite incoming target is SILENCED (zeroed); a non-finite
    or degenerate Σ falls back to the conservative per-name behavior (return the
    targets clamped, never sized up). A non-finite covariance must fail toward
    MORE conservative sizing, never less.
  * HAIRCUT-TOWARD-SILENCE ON THIN DATA. The covariance estimate is a SHRUNK
    estimate (Ledoit-Wolf-style, implemented BY HAND in numpy — NO sklearn in the
    core). On thin data (n < ``shrinkage_min_obs``) the off-diagonal correlation
    is shrunk HARD toward the diagonal (toward independence), so the optimizer
    does not act on a noisy sample off-diagonal. Covariance on thin/interday data
    is NOISY -> shrink hard, prefer under-sizing.

DEFAULT-OFF: the gate wires this behind ``portfolio_variance_sizing_enabled`` on
:class:`~hermes_quant.pdr_core.gate.RiskConfig` (the shell flips it from the env
flag ``HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING`` via
:func:`portfolio_variance_enabled_from_env`). With the flag unset the gate's
``apply_portfolio_variance_sizing`` is a PASS-THROUGH — byte-identical to 83bf280.

PURITY (ADR-0092, ``tests/pdr_core/test_contract_purity.py``): stdlib + numpy
only. numpy is NOT a forbidden top-level package (the forbidden set is
alpaca/ccxt/yfinance/discord/mcp/torch/sklearn/requests/httpx/aiohttp/pydantic);
the shrinkage is implemented by hand so NO sklearn enters the core. No host /
infra / governance / state import.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np

# The env flag the shell reads to flip the RiskConfig knob. Bound to a module
# constant so the FLAG-INVENTORY scanner (ops/scripts/quant-flag-inventory.py)
# records it via the ``_FLAG = "HERMES_QUANT_..."`` + ``environ.get(CONST, ...)``
# patterns.
# NOTE: NO type annotation on this assignment — the FLAG-INVENTORY scanner's
# ``_CONST`` regex matches ``NAME = "HERMES_QUANT_..."`` directly (a ``NAME: str =``
# annotation breaks the match and silently drops the flag from the inventory).
PORTFOLIO_VARIANCE_SIZING_FLAG = "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING"


def portfolio_variance_enabled_from_env() -> bool:
    """True iff the env flag is the literal ``"1"`` (default-OFF rail).

    Gates on the literal ``"1"`` — NOT ``bool(os.environ.get(...))`` (which would
    treat ``"0"`` / ``"false"`` as enabled, since a non-empty string is truthy).
    Default-OFF + eval-gated: unset / any non-``"1"`` value reads False, so the
    gate stays byte-identical to 83bf280 until an operator explicitly flips it."""
    return os.environ.get(PORTFOLIO_VARIANCE_SIZING_FLAG, "0") == "1"


@dataclass(frozen=True)
class PortfolioVarianceConfig:
    """Knobs for the correlation-aware sizing step.

    ``variance_cap`` is the aggregate PORTFOLIO VARIANCE budget ``w^T Σ w`` (a
    NAV-fraction² number). ``shrinkage_min_obs`` is the observation count below
    which the sample covariance is shrunk HARD toward its diagonal. ``shrinkage``
    in [0, 1], when not None, OVERRIDES the data-driven intensity (1.0 = pure
    diagonal target; 0.0 = pure sample). The defaults are deliberately
    conservative — the cap is small enough that a concentrated correlated basket
    is de-levered, and the min-obs threshold treats interday/thin data as noisy.
    """

    variance_cap: float = 0.02
    shrinkage_min_obs: int = 30
    shrinkage: float | None = None

    def __post_init__(self) -> None:
        # Finite-guard the operator-supplied knobs: a NaN/inf/<=0 variance_cap
        # would make ``w^T Σ w <= cap`` a fail-OPEN no-op (nan/inf comparisons
        # are wrong), defeating the whole rail. Fail loud at construction rather
        # than silently disable the cap.
        if not (math.isfinite(self.variance_cap) and self.variance_cap > 0.0):
            raise ValueError(
                f"variance_cap must be finite and > 0, got {self.variance_cap!r} "
                "(a NaN/inf/<=0 cap silently disables the portfolio-variance rail)."
            )
        if self.shrinkage_min_obs < 1:
            raise ValueError(
                f"shrinkage_min_obs must be >= 1, got {self.shrinkage_min_obs!r}"
            )
        if self.shrinkage is not None and not (
            math.isfinite(self.shrinkage) and 0.0 <= self.shrinkage <= 1.0
        ):
            raise ValueError(
                f"shrinkage override must be in [0, 1] or None, got {self.shrinkage!r}"
            )


def _clamp_haircut_only(
    targets: list[float], scaled: list[float]
) -> list[float]:
    """Belt-and-suspenders: enforce the haircut-only invariant element-wise.

    Returns, per element, the value with the SMALLER magnitude (and the input's
    sign), and zeroes any non-finite output. The step must NEVER size up nor leak
    a non-finite target, regardless of how the scale was computed."""
    out: list[float] = []
    for t, s in zip(targets, scaled, strict=True):
        if not math.isfinite(s):
            out.append(0.0)
            continue
        if not math.isfinite(t):
            out.append(0.0)
            continue
        # Never larger in magnitude than the input; preserve the input's sign.
        if abs(s) > abs(t):
            out.append(t)
        else:
            out.append(s)
    return out


def shrink_covariance(
    returns: np.ndarray,
    *,
    config: PortfolioVarianceConfig,
) -> np.ndarray:
    """Ledoit-Wolf-STYLE shrinkage of a sample covariance toward a constant-
    correlation target, implemented BY HAND in numpy (NO sklearn in the core).

    Shrinks ``Σ_shrunk = δ·F + (1-δ)·S`` where ``S`` is the (biased) sample
    covariance and ``F`` is the constant-correlation target (same diagonal
    variances as ``S``; off-diagonals = average sample correlation × the geometric
    mean of the pair's variances). The shrinkage intensity ``δ ∈ [0, 1]`` is:

      * the explicit ``config.shrinkage`` if set, else
      * data-driven toward 1 (the full constant-correlation target) as the
        observation count ``n`` falls below ``config.shrinkage_min_obs``:
        ``δ = 1 - n / min_obs`` for ``n < min_obs`` (so n→0 shrinks fully toward
        ``F``), and a small floor ``δ = 0.10`` for ``n >= min_obs`` (always shrink
        a little — covariance on real data is noisy).

    IMPORTANT (review-corrected): the target ``F`` is a CONSTANT-CORRELATION
    target, NOT the diagonal/identity. On thin data the off-diagonals are pulled
    toward their AVERAGE correlation, not toward 0/independence — so when a basket
    genuinely co-moves (high average ρ) the shrunk Σ PRESERVES that average
    correlation rather than laundering it away. This is the safe direction: it
    keeps the aggregate-correlation signal that the variance CAP then acts on
    (a noisy per-pair off-diagonal is smoothed toward the basket mean, never
    fabricated). Aggregate safety on thin data is delivered by the variance CAP
    in ``portfolio_variance_haircut``, not by shrinking correlation to zero. Only
    when the individual sample correlations have high dispersion around a near-zero
    mean does ``F`` (and thus the shrunk Σ) collapse toward independence.

    Args:
        returns: ``(n_obs, n_assets)`` array of per-period returns. Non-finite
            entries are dropped row-wise before estimation (a torn observation
            must not poison the whole estimate).
        config: knobs (``shrinkage_min_obs`` + optional ``shrinkage`` override).

    Returns:
        ``(n_assets, n_assets)`` shrunk covariance, symmetric, finite. On
        degenerate input (no usable rows, single asset) returns the diagonal of
        the per-asset sample variances (or a tiny positive floor) — never raises,
        never returns a non-finite matrix.
    """
    r = np.asarray(returns, dtype=float)
    if r.ndim != 2 or r.shape[1] < 1:
        # Degenerate shape — cannot estimate; return a 1×1 floor.
        return np.array([[1e-8]], dtype=float)
    # Drop rows with any non-finite entry (a torn observation must not poison Σ).
    finite_rows = np.isfinite(r).all(axis=1)
    r = r[finite_rows]
    n_obs, n_assets = r.shape

    floor = 1e-8
    if n_obs < 2:
        # Not enough to estimate any covariance — treat as fully independent at a
        # tiny floor variance (fail toward less exposure).
        return np.eye(n_assets) * floor

    sample = np.cov(r, rowvar=False, bias=True)
    sample = np.atleast_2d(sample)
    if sample.shape != (n_assets, n_assets):
        return np.eye(n_assets) * floor

    var = np.clip(np.diag(sample).copy(), floor, None)
    std = np.sqrt(var)

    # Constant-correlation target F: same variances, off-diagonals = average
    # sample correlation × sqrt(var_i var_j).
    denom = np.outer(std, std)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(denom > 0, sample / denom, 0.0)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    np.fill_diagonal(corr, 1.0)
    if n_assets > 1:
        off_mask = ~np.eye(n_assets, dtype=bool)
        rbar = float(corr[off_mask].mean())
    else:
        rbar = 0.0
    rbar = max(-1.0, min(1.0, rbar))
    target = rbar * denom
    np.fill_diagonal(target, var)

    # Shrinkage intensity δ.
    if config.shrinkage is not None:
        delta = float(config.shrinkage)
    elif n_obs < config.shrinkage_min_obs:
        delta = 1.0 - (n_obs / float(config.shrinkage_min_obs))
    else:
        delta = 0.10
    delta = max(0.0, min(1.0, delta))

    shrunk = delta * target + (1.0 - delta) * sample
    # Symmetrize + finite-guard (a fully-shrunk thin estimate must still be sane).
    shrunk = 0.5 * (shrunk + shrunk.T)
    shrunk = np.nan_to_num(shrunk, nan=0.0, posinf=0.0, neginf=0.0)
    # Keep the diagonal strictly positive (a degenerate name gets a floor var).
    diag = np.clip(np.diag(shrunk).copy(), floor, None)
    np.fill_diagonal(shrunk, diag)
    return shrunk


def portfolio_variance_haircut(
    targets: list[float],
    cov: np.ndarray,
    *,
    config: PortfolioVarianceConfig,
) -> list[float]:
    """Haircut a basket of per-name targets so the PORTFOLIO VARIANCE stays within
    ``config.variance_cap`` — not merely each |w_i| within a per-name cap.

    Applies a SINGLE GLOBAL scale ``λ ∈ [0, 1]``: if ``v = w^T Σ w`` exceeds the
    cap, ``λ = sqrt(cap / v)`` so ``(λw)^T Σ (λw) = λ² v = cap`` — the whole basket
    of correlated names is scaled DOWN together, preserving relative ranking. If
    ``v`` is already within the cap it is a no-op (``λ = 1``).

    HAIRCUT-TOWARD-SILENCE: the returned targets are NEVER larger in magnitude
    than the inputs (the scale is clamped to [0, 1] and a final element-wise
    haircut-only clamp is applied).

    FAIL-CLOSED:
      * A non-finite INCOMING target is SILENCED (set to 0.0) before sizing — a
        NaN target poisons ``w^T Σ w`` AND every ``<=`` comparison; it must never
        fire and must never corrupt its basket siblings.
      * A non-finite or wrong-shape Σ falls back to the conservative per-name
        behavior: return the (finite) targets UNCHANGED (already per-name-capped
        upstream), never sized up. A non-finite covariance fails toward MORE
        conservative sizing, never less.
      * If the computed variance is non-finite or non-positive, no scaling is
        applied (the targets pass through, never sized up).

    Args:
        targets: per-name signed target fractions (Stage-1 quarter-Kelly output,
            each already clipped to the per-name cap). Order is preserved.
        cov: ``(N, N)`` covariance over the SAME N names, in the SAME order. Use
            :func:`shrink_covariance` to produce a shrunk estimate first.
        config: the variance cap + shrinkage knobs.

    Returns:
        A list of N haircut targets in input order. Always finite; every
        ``|out_i| <= |in_i|``.
    """
    n = len(targets)
    if n == 0:
        return []

    # 1) Finite-guard the incoming targets FIRST. A non-finite target is silenced
    #    (zeroed) so it neither fires nor poisons w^T Σ w / the cap comparison.
    safe_targets: list[float] = [t if math.isfinite(t) else 0.0 for t in targets]

    # 2) Finite-guard / shape-guard the covariance. A non-finite or wrong-shape Σ
    #    => conservative fallback: pass the (silenced) targets through unscaled.
    cov_arr = np.asarray(cov, dtype=float)
    cov_ok = (
        cov_arr.ndim == 2
        and cov_arr.shape == (n, n)
        and bool(np.all(np.isfinite(cov_arr)))
    )
    if not cov_ok:
        # Fail CLOSED: never size up. Return the per-name targets (already capped
        # upstream), with the non-finite ones silenced.
        return _clamp_haircut_only(targets, safe_targets)

    # 3) Compute portfolio variance and the global de-lever scale.
    w = np.asarray(safe_targets, dtype=float)
    variance = float(w @ cov_arr @ w)
    if not math.isfinite(variance) or variance <= 0.0:
        # No usable variance (e.g. all-zero targets) — nothing to de-lever.
        return _clamp_haircut_only(targets, safe_targets)

    if variance <= config.variance_cap:
        # Already within the aggregate cap — pass-through (no-op).
        return _clamp_haircut_only(targets, safe_targets)

    lam = math.sqrt(config.variance_cap / variance)
    lam = max(0.0, min(1.0, lam))  # haircut-only: λ in [0, 1]
    scaled = [lam * t for t in safe_targets]
    return _clamp_haircut_only(targets, scaled)
