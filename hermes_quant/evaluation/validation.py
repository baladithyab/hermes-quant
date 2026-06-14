"""hermes_quant.evaluation.validation — B32 validation suite.

A pure, deterministic, default-OFF evidence layer over the already-realized
per-bar return series produced by the no-lookahead replay / walk-forward
path. It produces a ``ValidationReport`` (serializable to ``validation.json``)
combining three complementary lines of evidence:

1. **Monte-Carlo permutation test** (Masters 2020; quantpylib timing-skill
   null): Fisher-Yates shuffle of the realized return sequence holding the
   implicit position/weight vector fixed, recompute an ORDER-SENSITIVE timing
   statistic (lag-1 momentum-following PnL), and form the Laplace-smoothed
   one-sided p-value ``(1 + #{perm >= obs}) / (M + 1)``. This isolates *timing
   skill* (temporal structure) from static exposure / market drift and mirrors
   the convention in ``evaluation/lookahead.py``. Plain Sharpe / total-return
   are permutation-INVARIANT set-functions (a permutation leaves them
   unchanged → degenerate p == 1.0) and so are assessed via the bootstrap CI,
   never the permutation test.

2. **Stationary block bootstrap CI** (Politis & Romano 1994) with the
   Politis-White (2004) automatic block-length selection, corrected per
   Patton, Politis & White (2009) ``D_SB = 2·g²(0)``. This is the PRIMARY CI
   because it preserves serial dependence (autocorrelated returns violate the
   IID assumption of PSR/DSR). When scipy is available a SECONDARY BCa CI on
   the IID-resampled series is added for cross-reference (``scipy.stats.bootstrap``);
   when scipy is absent the secondary CI gracefully degrades to a percentile
   CI from the stationary-bootstrap distribution with a warning.

3. **Deflated / Probabilistic Sharpe Ratio** via
   ``evaluation.dsr.deflated_sharpe`` (Bailey & López de Prado 2014) — NOT
   reimplemented here; called directly. DSR assumes IID returns, so it is
   complementary to (not a substitute for) the block-bootstrap CI.

NO-LOOKAHEAD: the suite only ever resamples / permutes a *fixed, already
realized* outcome vector. It never re-runs the strategy on resampled bars
(which would risk re-introducing lookahead). For walk-forward inputs the
caller concatenates non-overlapping OOS test folds chronologically.

Determinism: a single integer ``seed`` (default 42, matching lookahead.py)
seeds one ``np.random.default_rng(seed)``; independent streams for the
permutation vs bootstrap stages are derived via ``rng.spawn(...)`` so the
whole report is one-seed reproducible.

This module is import-only until explicitly invoked behind the default-OFF
``--validate`` CLI flag. It produces evidence, never a trade or a promotion:
the deterministic risk gate / promotion machinery (ADR-0004, ADR-0029)
remains the final authority. ``validation.json`` is an *input* to that gate
(the CI lower bound), not a decision.

References:
- Politis, D. & Romano, J. (1994). "The Stationary Bootstrap." JASA 89:1303-1313.
- Politis, D. & White, H. (2004). "Automatic Block-Length Selection for the
  Dependent Bootstrap." Econometric Reviews 23(1):53-70.
- Patton, A., Politis, D. & White, H. (2009). Correction. Econometric Reviews
  28(4):372-375. (D_SB = 2·g²(0).)
- Masters, T. (2020). "Permutation and Randomization Tests for Trading System
  Development."
- Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio." JPM 40(5).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Scipy optional import — graceful fallback (mirror factors/ic_metrics.py)
# ---------------------------------------------------------------------------
try:
    from scipy.stats import bootstrap as _scipy_bootstrap  # type: ignore[import-untyped]

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False


_MIN_OBS_FOR_DSR = 30  # mirrors dsr.py:56 guard


# ---------------------------------------------------------------------------
# Frozen dataclasses (public API)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BootstrapCI:
    """A bootstrap confidence interval for one statistic."""

    statistic: str  # "sharpe" | "excess_return"
    point: float
    ci_low: float  # 2-sided low (also the 1-sided lower at same level)
    ci_high: float
    confidence_level: float  # e.g. 0.95
    n_resamples: int
    block_length: float  # Politis-White expected block length (1.0 == IID)
    method: str  # "stationary_block" | "bca_iid" | "percentile_iid"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PermutationResult:
    """A Monte-Carlo permutation (timing-skill null) test for one statistic."""

    statistic: str  # "sharpe" | "total_return" | "excess_return"
    observed: float
    p_value: float  # (1 + #{perm >= obs}) / (M + 1), one-sided right tail
    n_permutations: int
    perm_mean: float
    perm_std: float
    alpha: float

    @property
    def significant(self) -> bool:
        """True if observed statistic beats the timing-skill null at alpha."""
        return self.p_value <= self.alpha

    def to_dict(self) -> dict:
        d = asdict(self)
        d["significant"] = self.significant
        return d


@dataclass(frozen=True)
class ValidationReport:
    """Full B32 validation report — serializable to validation.json."""

    seed: int
    n_observations: int
    bars_per_year: float
    deflated_sharpe: float  # from evaluation.dsr.deflated_sharpe (NaN if low power)
    permutation: list[PermutationResult]
    bootstrap: list[BootstrapCI]
    walk_forward: dict | None  # positive_excess_fold_rate, mean_sharpe_delta, n_splits
    warnings: list[str]

    def to_dict(self) -> dict:
        """JSON-serializable, sort_keys-friendly dict.

        NaN floats are rendered as ``None`` so the artifact round-trips
        through ``json.dumps`` / ``json.loads`` without producing the
        non-standard ``NaN`` token.
        """
        return {
            "seed": self.seed,
            "n_observations": self.n_observations,
            "bars_per_year": self.bars_per_year,
            "deflated_sharpe": _json_float(self.deflated_sharpe),
            "permutation": [_clean_floats(p.to_dict()) for p in self.permutation],
            "bootstrap": [_clean_floats(c.to_dict()) for c in self.bootstrap],
            "walk_forward": (_clean_floats(self.walk_forward) if self.walk_forward else None),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Internal helpers — numpy only
# ---------------------------------------------------------------------------
def _json_float(x: float) -> float | None:
    """Render NaN/inf as None so json.dumps produces valid JSON."""
    if x is None:
        return None
    xf = float(x)
    if math.isnan(xf) or math.isinf(xf):
        return None
    return xf


def _clean_floats(obj):
    """Recursively replace NaN/inf floats with None for JSON-safety."""
    if isinstance(obj, dict):
        return {k: _clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_floats(v) for v in obj]
    if isinstance(obj, float):
        return _json_float(obj)
    return obj


def _to_array(returns) -> np.ndarray:
    """Coerce a pd.Series | np.ndarray | sequence to a 1-D float ndarray.

    Drops non-finite ELEMENTS (compresses the array). Correct for the
    per-series, order-only statistics (Sharpe, DSR, timing permutation) where
    a dropped bar carries no cross-series pairing. For the PAIRED excess
    computation use :func:`_paired_finite` instead — independent per-series
    compression here would shift the two series relative to one another and
    pair bars from different dates (cs39).
    """
    arr = np.asarray(getattr(returns, "values", returns), dtype=float).ravel()
    return arr[np.isfinite(arr)]


def _paired_finite(a, b) -> tuple[np.ndarray, np.ndarray]:
    """Coerce two return series and align them on a SHARED finite mask.

    Truncates both to the common length, then keeps only the positions where
    BOTH series are finite (``isfinite(a) & isfinite(b)`` elementwise). A
    non-finite element in EITHER series drops that bar from BOTH, so the i-th
    kept element of ``a`` always pairs the SAME-DATE i-th kept element of
    ``b`` (cs39). Independently compressing each series (the old ``_to_array``
    path) shifts them relative to one another when a NaN sits at different
    indices, silently pairing bars from different dates in the excess subtract.

    When both series are fully finite the mask is all-True and the result is
    byte-identical to a naive positional ``a[:m] - b[:m]``.
    """
    arr_a = np.asarray(getattr(a, "values", a), dtype=float).ravel()
    arr_b = np.asarray(getattr(b, "values", b), dtype=float).ravel()
    m = min(arr_a.size, arr_b.size)
    arr_a = arr_a[:m]
    arr_b = arr_b[:m]
    mask = np.isfinite(arr_a) & np.isfinite(arr_b)
    return arr_a[mask], arr_b[mask]


def _sharpe(returns: np.ndarray, *, bars_per_year: float) -> float:
    """Annualized Sharpe ratio.

    Mirrors hermes_quant/backtest/replay.py:_sharpe (source of truth) so the
    validation Sharpe equals the reported backtest Sharpe. Duplicated here as
    a tiny local helper because evaluation/ must not depend on backtest/.
    """
    if returns.size < 2:
        return float("nan")
    mean = float(returns.mean())
    std = float(returns.std(ddof=1))
    if std == 0 or math.isnan(std):
        return 0.0 if mean == 0 else math.inf * (1.0 if mean > 0 else -1.0)
    return float(mean / std * math.sqrt(bars_per_year))


def _politis_white_block_length(x: np.ndarray) -> float:
    """Politis-White (2004) automatic expected block length with the
    Patton-Politis-White (2009) ``D_SB = 2·g²(0)`` correction.

    Implements the flat-top-lag-window spectral plug-in (PWSD) exactly as
    documented by ``arch.bootstrap.optimal_block_length`` (the spec
    authority — arch is NOT a runtime dependency):

    - tuning lag ``m`` = first lag after which ``k_n`` consecutive sample
      autocorrelations all fall inside ``±2·sqrt(log10(n)/n)``, with
      ``k_n = max(5, log10(n))`` and ``m_max = ceil(sqrt(n)) + k_n``;
    - ``M = 2·m``; flat-top kernel ``lam(t) = 1 (|t|<=1/2), 2(1-|t|)
      (1/2<|t|<=1), 0 else``;
    - ``g = Σ_{k=-M..M} lam(k/M)·|k|·γ_k``; ``σ̂² = Σ lam(k/M)·γ_k``;
      ``d_SB = 2·(σ̂²)²`` (the 2009 correction);
    - ``b_opt = (2·g²/d_SB · n)^(1/3)``, clamped to
      ``[1, ceil(min(3·sqrt(n), n/3))]``.

    For a Sharpe-ratio CI the caller should run this on the SQUARED returns
    (variance autocorrelation dominates) and take the max of the level and
    squared estimates — see ``_block_length_for_sharpe``.

    A serially-independent series collapses to ``b == 1`` (an IID bootstrap),
    which is the correct degenerate behavior.
    """
    x = np.asarray(x, dtype=float).ravel()
    n = x.size
    if n < 4:
        return 1.0
    x = x - x.mean()
    gamma0 = float(np.dot(x, x) / n)
    if gamma0 <= 0:
        return 1.0  # zero variance -> IID

    def autocov(k: int) -> float:
        if k <= 0:
            return gamma0
        if k >= n:
            return 0.0
        return float(np.dot(x[: n - k], x[k:]) / n)

    def autocorr(k: int) -> float:
        return autocov(k) / gamma0

    k_n = max(5, int(math.ceil(math.log10(n))))
    m_max = int(math.ceil(math.sqrt(n))) + k_n
    m_max = min(m_max, n - 1)

    crit = 2.0 * math.sqrt(math.log10(n) / n)
    rho = np.array([autocorr(k) for k in range(0, m_max + 1)])

    # Find the smallest lag m such that the next k_n autocorrelations are all
    # insignificant. If none qualifies, fall back to m_max.
    m = m_max
    for lag in range(1, m_max + 1):
        window = rho[lag : lag + k_n]
        if window.size < k_n:
            break  # not enough lags left to confirm insignificance
        if np.all(np.abs(window) < crit):
            m = lag - 1
            break
    if m < 1:
        m = 1

    big_m = min(2 * m, n - 1)

    def lam(t: float) -> float:
        a = abs(t)
        if a <= 0.5:
            return 1.0
        if a <= 1.0:
            return 2.0 * (1.0 - a)
        return 0.0

    g = 0.0
    sigma2 = 0.0  # g(0) = Σ lam(k/M)·γ_k
    for k in range(-big_m, big_m + 1):
        gk = autocov(abs(k))
        w = lam(k / big_m) if big_m > 0 else (1.0 if k == 0 else 0.0)
        g += w * abs(k) * gk
        sigma2 += w * gk

    d_sb = 2.0 * sigma2**2
    if d_sb <= 0 or not math.isfinite(d_sb):
        return 1.0
    b = (2.0 * g**2 / d_sb * n) ** (1.0 / 3.0)
    b_max = math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    return float(min(max(b, 1.0), max(b_max, 1.0)))


def _block_length_for_sharpe(x: np.ndarray) -> float:
    """Conservative block length for a Sharpe CI: max of the level and
    squared-return estimates (arch guidance — variance autocorrelation
    dominates the Sharpe statistic).
    """
    b_level = _politis_white_block_length(x)
    b_sq = _politis_white_block_length(x**2)
    return max(b_level, b_sq)


def _stationary_bootstrap_indices(
    n: int, b: float, n_resamples: int, rng: np.random.Generator
) -> np.ndarray:
    """Stationary (Politis-Romano 1994) bootstrap index matrix.

    Geometric-length blocks (mean = b, so ``p = 1/b``) that wrap circularly,
    producing a stationary resampled series. Returns an ``(n_resamples, n)``
    integer index array into the original series. A single shared seeded
    ``rng`` is threaded through for full determinism.
    """
    if n <= 0:
        return np.zeros((n_resamples, 0), dtype=np.intp)
    b = max(float(b), 1.0)
    p = 1.0 / b
    indices = np.empty((n_resamples, n), dtype=np.intp)
    for r in range(n_resamples):
        idx = np.empty(n, dtype=np.intp)
        i = 0
        while i < n:
            start = int(rng.integers(0, n))
            # geometric block length (>= 1); when b == 1, p == 1 -> length 1
            if p >= 1.0:
                length = 1
            else:
                length = int(rng.geometric(p))
            length = max(length, 1)
            for j in range(length):
                if i >= n:
                    break
                idx[i] = (start + j) % n  # circular wrap
                i += 1
        indices[r] = idx
    return indices


def _percentile_ci(
    samples: np.ndarray, confidence_level: float
) -> tuple[float, float]:
    """Two-sided percentile CI from a bootstrap distribution.

    cs46: filter NON-FINITE bootstrap samples (BOTH NaN and ±inf) before the
    percentile, then percentile only the finite tail. ``np.nanpercentile``
    drops NaN but KEEPS inf; a single inf in the tail-interpolation window
    makes numpy's ``subtract(b, a)`` on inf yield NaN, corrupting ci_low /
    ci_high to NaN. A degenerate / zero-variance resample (a stationary block
    drawing a single repeated block -> constant series) makes ``_sharpe``
    return ±inf, so this is reachable on a real low-variance OOS series.

    A NaN lower bound is the worst failure mode for the promotion gate: the
    gate is ``sharpe_95ci_lower < 1.0`` (governance/promotion.py:274;
    react/live.py:38), and ``NaN < 1.0`` is ``False`` in Python — a NaN CI
    silently PASSES a gate that should fail-closed. We therefore (a) drop the
    non-finite samples, and (b) when NO finite samples remain (a fully
    degenerate zero-variance distribution), return a CONSERVATIVE finite
    ``0.0`` lower bound so the gate fails-closed rather than reading a NaN.

    A finite-variance series leaves all samples finite -> the finite mask is
    all-True and the result is byte-identical to the old percentile.
    """
    # Coerce to ndarray first: the production caller (_bootstrap_ci) passes an
    # np.array, but a list/tuple caller would TypeError on the boolean mask
    # below. np.nanpercentile used to coerce implicitly; preserve that contract.
    samples = np.asarray(samples, dtype=float)
    finite = samples[np.isfinite(samples)]
    if finite.size == 0:
        # Fully degenerate (every resample non-finite, e.g. zero-variance
        # constant series -> ±inf Sharpe). The CI cannot be estimated; return
        # a conservative finite bound that fails the >=1.0 promotion gate.
        return 0.0, 0.0
    alpha = 1.0 - confidence_level
    lo = float(np.percentile(finite, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(finite, 100.0 * (1.0 - alpha / 2.0)))
    return lo, hi


def _timing_pnl(x: np.ndarray) -> float:
    """Order-SENSITIVE timing statistic: lag-1 momentum-following PnL.

    The timing-skill null asks whether the *temporal ordering* of the
    realized return series carries information. A position taken in the
    direction of the previous bar's return, ``pos(t) = sign(x[t-1])``, earns
    ``Σ pos(t)·x(t)``. This is large only when returns are positively
    autocorrelated (genuine momentum/timing structure) and collapses toward
    zero under a random permutation of the sequence — exactly the property a
    permutation test needs.

    Plain Sharpe / total-return are permutation-INVARIANT set-functions
    (shuffling the same returns leaves them unchanged), so they are useless as
    a permutation statistic (degenerate p == 1.0). They are tested via the
    bootstrap CI instead. The timing statistic is the order-sensitive
    counterpart used for the Monte-Carlo permutation test. The continuous
    magnitude (not sign-only) is used to avoid Masters' tie inflation.
    """
    if x.size < 3:
        return 0.0
    pos = np.sign(x[:-1])
    return float(np.sum(pos * x[1:]))


def _permutation_pvalue(
    returns: np.ndarray,
    statistic_fn,
    *,
    n_permutations: int,
    rng: np.random.Generator,
) -> tuple[float, float, float, float]:
    """Monte-Carlo permutation p-value under the timing-skill null.

    Fisher-Yates shuffle of the realized return sequence (the implicit
    position/weight vector is held fixed), recompute the order-sensitive
    statistic, and form the Laplace-smoothed one-sided right-tail p-value
    ``(1 + #{perm >= obs}) / (M + 1)`` — mirroring lookahead.py.

    NOTE: the statistic MUST be order-sensitive (e.g. ``_timing_pnl``);
    permutation-invariant set-functions (Sharpe, total return) yield a
    degenerate p == 1.0 and must be assessed via the bootstrap CI instead.

    Returns ``(observed, p_value, perm_mean, perm_std)``.
    """
    observed = float(statistic_fn(returns))
    if not math.isfinite(observed) or returns.size < 2:
        return observed, 1.0, float("nan"), float("nan")
    perm_stats = np.empty(n_permutations, dtype=float)
    for i in range(n_permutations):
        shuffled = rng.permutation(returns)  # Fisher-Yates
        perm_stats[i] = statistic_fn(shuffled)
    finite = perm_stats[np.isfinite(perm_stats)]
    n_at_or_above = int(np.sum(perm_stats >= observed))
    p_value = (1 + n_at_or_above) / (n_permutations + 1)  # Laplace smoothing
    perm_mean = float(np.mean(finite)) if finite.size else float("nan")
    perm_std = float(np.std(finite)) if finite.size else float("nan")
    return observed, p_value, perm_mean, perm_std


def _scipy_bca_ci(
    x: np.ndarray,
    statistic_fn,
    *,
    confidence_level: float,
    n_resamples: int,
    seed: int,
) -> tuple[float, float] | None:
    """Secondary IID BCa CI via scipy.stats.bootstrap (ADR-0029 pattern).

    Returns ``(ci_low, ci_high)`` or ``None`` if scipy is absent or the
    series is degenerate (DegenerateDataWarning -> NaN bounds). The result is
    explicitly IID-resampled (serial dependence destroyed) and is reported
    only as a cross-reference to the primary stationary-block CI.
    """
    if not _HAS_SCIPY:
        return None

    def _vec_stat(data: np.ndarray, axis: int = -1) -> np.ndarray:
        # data may be (n,) or (resamples, n); reduce along `axis`.
        return np.apply_along_axis(lambda row: statistic_fn(row), axis, data)

    try:
        res = _scipy_bootstrap(
            (x,),
            _vec_stat,
            n_resamples=n_resamples,
            method="BCa",
            confidence_level=confidence_level,
            rng=np.random.default_rng(seed),
            vectorized=True,
        )
    except Exception:  # noqa: BLE001 — fail-closed; downgrade to percentile
        return None
    lo = float(res.confidence_interval.low)
    hi = float(res.confidence_interval.high)
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    return lo, hi


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def validate_returns(
    strat_returns,
    *,
    bars_per_year: float,
    bh_returns=None,
    n_permutations: int = 1000,
    n_resamples: int = 9999,
    confidence_level: float = 0.95,
    alpha: float = 0.05,
    seed: int = 42,
    walk_forward_summary: dict | None = None,
) -> ValidationReport:
    """Run the B32 validation suite on an already-realized return series.

    Args:
        strat_returns: Realized per-bar strategy returns (pd.Series | ndarray).
            For walk-forward, the caller passes the chronological concatenation
            of non-overlapping OOS test-fold returns.
        bars_per_year: Annualization factor (e.g. 252 for daily). Use
            ``hermes_quant.backtest.replay._bars_per_year(timeframe)``.
        bh_returns: Optional buy-and-hold per-bar returns. When provided, an
            excess-return permutation + bootstrap is added.
        n_permutations: Monte-Carlo permutation count (default 1000).
        n_resamples: Bootstrap resample count (default 9999, ADR-0029).
        confidence_level: Bootstrap CI level (default 0.95).
        alpha: Permutation significance threshold (default 0.05).
        seed: Single integer seed (default 42). Sub-streams derived via spawn.
        walk_forward_summary: Optional dict carried through verbatim
            (n_splits, positive_excess_fold_rate, mean_sharpe_delta).

    Returns:
        ValidationReport. Fail-closed: low power / degenerate series are
        flagged in ``warnings`` rather than raised.
    """
    warnings: list[str] = []
    r = _to_array(strat_returns)
    n = r.size

    # Deterministic, one-seed reproducibility: spawn independent streams.
    root = np.random.default_rng(seed)
    perm_rng, boot_rng = root.spawn(2)
    # Distinct child seeds for the (separate) scipy BCa generators.
    bca_seed_sharpe = seed + 1
    bca_seed_excess = seed + 2

    if n < 2:
        warnings.append(
            f"n_observations={n} < 2: insufficient data for any statistic; "
            "report is empty / low-power."
        )
        return ValidationReport(
            seed=seed,
            n_observations=n,
            bars_per_year=float(bars_per_year),
            deflated_sharpe=float("nan"),
            permutation=[],
            bootstrap=[],
            walk_forward=walk_forward_summary,
            warnings=warnings,
        )

    if n < _MIN_OBS_FOR_DSR:
        warnings.append(
            f"n_observations={n} < {_MIN_OBS_FOR_DSR}: low statistical power. "
            "Deflated Sharpe omitted (NaN); permutation/bootstrap still run but "
            "should be treated as indicative only."
        )

    # ---- Statistics (continuous magnitude — never sign-only) ----
    def sharpe_stat(x: np.ndarray) -> float:
        return _sharpe(x, bars_per_year=bars_per_year)

    def total_return_stat(x: np.ndarray) -> float:
        # compounded total return over the (resampled) window
        return float(np.prod(1.0 + x) - 1.0)

    observed_sharpe = sharpe_stat(r)

    # ---- Deflated / Probabilistic Sharpe Ratio (call dsr, do not reimplement) ----
    deflated = float("nan")
    if n >= _MIN_OBS_FOR_DSR:
        from hermes_quant.evaluation.dsr import deflated_sharpe

        # Sample skew / (non-excess) kurtosis via numpy (no scipy required).
        skew = _sample_skew(r)
        kurt = _sample_kurtosis(r)  # non-excess (normal == 3.0)

        # cs48 (sibling of cs46): a zero-variance OOS series makes _sharpe
        # return ±inf (see _sharpe above). dsr.deflated_sharpe then forms
        # ``variance_term = 1 - skew*SR + (kurt-1)/4*SR**2``; for a constant
        # series skew==0, so ``skew*inf == NaN`` -> variance_term is NaN, the
        # ``variance_term <= 0`` guard (NaN<=0 == False) is bypassed, and
        # ``Φ(sr_diff·sqrt(n-1)/sqrt(NaN))`` collapses to NaN WITHOUT raising —
        # the try/except below only catches ValueError/ZeroDivisionError, so
        # the NaN escapes and renders as ``null`` in validation.json,
        # INDISTINGUISHABLE from the legitimate n<_MIN_OBS_FOR_DSR omission and
        # silently erasing the false-discovery hedge. Mirror cs46's
        # _percentile_ci guard: when any DSR input is non-finite the deflated
        # Sharpe is not estimable; report a CONSERVATIVE finite 0.0 (zero
        # probability the Sharpe is real — fails any ``dsr >= floor`` gate)
        # plus a warning that distinguishes this from a low-power omission. A
        # finite-variance series leaves every input finite, this guard never
        # fires, and the result is byte-identical to the bare dsr call.
        if not (
            math.isfinite(observed_sharpe)
            and math.isfinite(skew)
            and math.isfinite(kurt)
        ):
            deflated = 0.0
            warnings.append(
                "deflated_sharpe: non-finite Sharpe/skew/kurtosis "
                f"(observed_sharpe={observed_sharpe}, skew={skew}, kurtosis={kurt}); "
                "degenerate (likely zero-variance) OOS series. Reporting a "
                "conservative 0.0 (fails the DSR floor) rather than a NaN that "
                "would render as null and masquerade as a low-power omission."
            )
        else:
            try:
                deflated = deflated_sharpe(
                    observed_sharpe=observed_sharpe,
                    n_trials=1,
                    n_observations=n,
                    skew=skew,
                    kurtosis=kurt,
                )
            except (ValueError, ZeroDivisionError):
                deflated = float("nan")
                warnings.append("deflated_sharpe: degenerate inputs; reported as NaN.")
            else:
                # Defensive: dsr.deflated_sharpe can in principle return a
                # non-finite probability if a future input combination escapes
                # its internal guards. Never let a NaN/inf DSR reach the
                # artifact; collapse to the conservative 0.0.
                if not math.isfinite(deflated):
                    warnings.append(
                        "deflated_sharpe: non-finite result; reporting a "
                        "conservative 0.0 (fails the DSR floor)."
                    )
                    deflated = 0.0

    # ---- Monte-Carlo permutation tests (timing-skill null) ----
    # The permutation statistic is order-sensitive (_timing_pnl); plain
    # Sharpe / total-return are permutation-invariant and so are assessed via
    # the bootstrap CI below, not the permutation test.
    permutation: list[PermutationResult] = []

    obs, p, pmean, pstd = _permutation_pvalue(
        r, _timing_pnl, n_permutations=n_permutations, rng=perm_rng
    )
    permutation.append(
        PermutationResult(
            statistic="timing",
            observed=obs,
            p_value=p,
            n_permutations=n_permutations,
            perm_mean=pmean,
            perm_std=pstd,
            alpha=alpha,
        )
    )

    # ---- Excess-return series (optional) ----
    # cs39: pair strat/bh on a SHARED finite mask BEFORE subtracting so every
    # excess element pairs SAME-DATE strat/bh bars. We re-coerce strat_returns
    # here (rather than reuse the independently-compressed `r`) because a
    # non-finite in EITHER series must drop that bar from BOTH; the per-series
    # `r` above is still correct for the strat-only Sharpe/DSR/timing stats.
    excess = None
    if bh_returns is not None:
        r_paired, bh_paired = _paired_finite(strat_returns, bh_returns)
        m = r_paired.size
        if m >= 2:
            excess = r_paired - bh_paired
            obs, p, pmean, pstd = _permutation_pvalue(
                excess, _timing_pnl, n_permutations=n_permutations, rng=perm_rng
            )
            permutation.append(
                PermutationResult(
                    statistic="timing_excess",
                    observed=obs,
                    p_value=p,
                    n_permutations=n_permutations,
                    perm_mean=pmean,
                    perm_std=pstd,
                    alpha=alpha,
                )
            )
        else:
            warnings.append(
                "bh_returns provided but too short to align with strat_returns; "
                "excess-return stats omitted."
            )

    # ---- Stationary block bootstrap CIs (PRIMARY) ----
    bootstrap: list[BootstrapCI] = []
    bootstrap.append(
        _bootstrap_ci(
            r,
            sharpe_stat,
            statistic_name="sharpe",
            block_length=_block_length_for_sharpe(r),
            n_resamples=n_resamples,
            confidence_level=confidence_level,
            rng=boot_rng,
            bca_seed=bca_seed_sharpe,
            warnings=warnings,
        )
    )

    if excess is not None:
        bootstrap.append(
            _bootstrap_ci(
                excess,
                total_return_stat,
                statistic_name="excess_return",
                block_length=_politis_white_block_length(excess),
                n_resamples=n_resamples,
                confidence_level=confidence_level,
                rng=boot_rng,
                bca_seed=bca_seed_excess,
                warnings=warnings,
            )
        )

    return ValidationReport(
        seed=seed,
        n_observations=n,
        bars_per_year=float(bars_per_year),
        deflated_sharpe=deflated,
        permutation=permutation,
        bootstrap=bootstrap,
        walk_forward=walk_forward_summary,
        warnings=warnings,
    )


def _bootstrap_ci(
    x: np.ndarray,
    statistic_fn,
    *,
    statistic_name: str,
    block_length: float,
    n_resamples: int,
    confidence_level: float,
    rng: np.random.Generator,
    bca_seed: int,
    warnings: list[str],
) -> BootstrapCI:
    """Build the PRIMARY stationary-block-bootstrap percentile CI for one
    statistic, plus emit a SECONDARY scipy BCa CI as a warning cross-reference
    (or a percentile-fallback warning when scipy is absent / degenerate).
    """
    point = float(statistic_fn(x))
    n = x.size
    idx = _stationary_bootstrap_indices(n, block_length, n_resamples, rng)
    samples = np.array([statistic_fn(x[row]) for row in idx], dtype=float)
    ci_low, ci_high = _percentile_ci(samples, confidence_level)

    # Secondary IID BCa cross-reference (or fallback warning).
    bca = _scipy_bca_ci(
        x,
        statistic_fn,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        seed=bca_seed,
    )
    if bca is None:
        if _HAS_SCIPY:
            warnings.append(
                f"{statistic_name}: scipy BCa CI degenerate; primary stationary-block "
                "percentile CI used."
            )
        else:
            warnings.append(
                "scipy unavailable: BCa CI omitted, percentile CI from stationary "
                f"bootstrap used for {statistic_name}."
            )
    else:
        warnings.append(
            f"{statistic_name}: secondary IID BCa CI (scipy) "
            f"[{bca[0]:.6g}, {bca[1]:.6g}] (cross-reference; primary CI is "
            "stationary_block)."
        )

    return BootstrapCI(
        statistic=statistic_name,
        point=point,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence_level=confidence_level,
        n_resamples=n_resamples,
        block_length=float(block_length),
        method="stationary_block",
    )


def _sample_skew(x: np.ndarray) -> float:
    """Sample skewness (bias-uncorrected, population definition). 0.0 when n<3."""
    n = x.size
    if n < 3:
        return 0.0
    m = x.mean()
    s = x.std(ddof=0)
    if s == 0:
        return 0.0
    return float(np.mean(((x - m) / s) ** 3))


def _sample_kurtosis(x: np.ndarray) -> float:
    """Sample kurtosis, NON-excess (normal == 3.0). 3.0 when n<4."""
    n = x.size
    if n < 4:
        return 3.0
    m = x.mean()
    s = x.std(ddof=0)
    if s == 0:
        return 3.0
    return float(np.mean(((x - m) / s) ** 4))
