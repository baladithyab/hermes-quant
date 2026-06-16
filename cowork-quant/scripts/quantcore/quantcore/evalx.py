"""quantcore.evalx — anti-overfitting evaluation harness (backlog B-06).

Three guards against the cardinal sin of backtesting — selection bias under
multiple trials — implemented stdlib-only (math/itertools, no numpy/scipy):

1. CPCV — Combinatorially Purged Cross-Validation.
   N contiguous groups, all C(N, k) combinations of k test groups per split,
   with purging (drop train observations within ``purge`` of any test-group
   boundary) and embargo (drop train observations in the ``embargo`` window
   AFTER each test group, since serial correlation leaks information forward).
   Source: M. Lopez de Prado, "Advances in Financial Machine Learning",
   Wiley 2018, Ch. 7 (purged k-fold + embargo) and Ch. 12 (CPCV).

2. PSR / DSR — Probabilistic and Deflated Sharpe Ratios.
   PSR(SR*) = Phi[ (SR_hat - SR*) * sqrt(n - 1)
                   / sqrt(1 - skew*SR_hat + ((kurt - 1)/4)*SR_hat^2) ]
   with ``kurt`` the Pearson (raw, non-excess) kurtosis: Normal => 3.
   Source: Bailey & Lopez de Prado, "The Sharpe Ratio Efficient Frontier",
   Journal of Risk 15(2), 2012. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643
   The DSR is the PSR evaluated against the expected maximum Sharpe ratio of
   ``n_trials`` unskilled trials (False Strategy Theorem):
   E[max SR] ~= sqrt(V[SR]) * ((1-gamma)*PhiInv(1 - 1/N) + gamma*PhiInv(1 - 1/(N*e)))
   with gamma the Euler-Mascheroni constant (~0.5772).
   Source: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for
   Selection Bias, Backtest Overfitting and Non-Normality", Journal of
   Portfolio Management 40(5), 2014, pp. 94-107.
   https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
   https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf

3. PBO — Probability of Backtest Overfitting via CSCV.
   Split the T x N performance matrix into S even row-blocks; for each of the
   C(S, S/2) ways to pick half the blocks as in-sample (IS), select the
   IS-best strategy, compute its RELATIVE RANK omega-bar among all strategies
   out-of-sample (OOS = complementary blocks), map to a logit
   lambda = ln(omega/(1-omega)), and report PBO = fraction of logits <= 0
   (i.e. how often the IS winner is a below-median OOS performer).
   Source: Bailey, Borwein, Lopez de Prado & Zhu, "The Probability of
   Backtest Overfitting", Journal of Computational Finance 20(4), 2015.
   https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
   https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf

Normal CDF uses math.erf; the inverse normal CDF (PhiInv) uses Peter Acklam's
rational approximation (relative error < 1.15e-9) plus one Halley refinement:
https://web.archive.org/web/20151110174102/http://home.online.no/~pjacklam/notes/invnorm/

Deterministic throughout: no randomness, combination order is
itertools.combinations order.
"""

from __future__ import annotations

import math
from itertools import combinations

__all__ = [
    "cpcv_splits",
    "dsr",
    "expected_max_sharpe",
    "norm_cdf",
    "norm_ppf",
    "pbo",
    "psr",
    "sharpe",
]

#: Euler-Mascheroni constant (False Strategy Theorem weighting).
EULER_GAMMA = 0.5772156649015329


# --------------------------------------------------------------------------
# Standard normal CDF and inverse CDF (stdlib only)
# --------------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    """Standard normal CDF: Phi(x) = (1 + erf(x / sqrt(2))) / 2."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Acklam's coefficients (see module docstring for the citation).
_ACKLAM_A = (
    -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
    1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
    6.680131188771972e01, -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
    -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03, 3.224671290700398e-01,
    2.445134137142996e00, 3.754408661907416e00,
)
_ACKLAM_P_LOW = 0.02425


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF via Acklam's algorithm + Halley polish.

    Raises ValueError outside the open interval (0, 1).
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p}")
    a, b, c, d = _ACKLAM_A, _ACKLAM_B, _ACKLAM_C, _ACKLAM_D
    if p < _ACKLAM_P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    elif p <= 1.0 - _ACKLAM_P_LOW:
        q = p - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    # One Halley step against the erf-based CDF -> near machine precision.
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# --------------------------------------------------------------------------
# 1. CPCV — Combinatorially Purged Cross-Validation
# --------------------------------------------------------------------------


def cpcv_splits(
    n_obs: int,
    n_groups: int = 6,
    k_test: int = 2,
    purge: int = 0,
    embargo: int = 0,
) -> list[tuple[list[int], list[int]]]:
    """All C(n_groups, k_test) purged/embargoed (train_idx, test_idx) splits.

    Observations 0..n_obs-1 are partitioned into ``n_groups`` CONTIGUOUS
    groups (first n_obs % n_groups groups take one extra observation). Each
    split takes ``k_test`` groups as the test set; the train set is everything
    else MINUS:

    - purge:   train observations within ``purge`` of a test-group boundary,
               i.e. [start - purge, start) and [end, end + purge) for each
               test group [start, end);
    - embargo: train observations in the window AFTER each test group,
               i.e. [end, end + embargo) — leakage flows forward in time.

    Deterministic: groups and combinations are enumerated in natural order
    (itertools.combinations). AFML Ch. 7 & 12 (see module docstring).
    """
    if not all(isinstance(v, int) for v in (n_obs, n_groups, k_test, purge, embargo)):
        raise ValueError("cpcv_splits: all arguments must be integers")
    if n_obs < 2:
        raise ValueError(f"cpcv_splits: n_obs must be >= 2, got {n_obs}")
    if not 2 <= n_groups <= n_obs:
        raise ValueError(f"cpcv_splits: need 2 <= n_groups <= n_obs, got n_groups={n_groups}")
    if not 1 <= k_test < n_groups:
        raise ValueError(f"cpcv_splits: need 1 <= k_test < n_groups, got k_test={k_test}")
    if purge < 0 or embargo < 0:
        raise ValueError("cpcv_splits: purge and embargo must be >= 0")

    base, rem = divmod(n_obs, n_groups)
    bounds: list[tuple[int, int]] = []
    start = 0
    for g in range(n_groups):
        size = base + (1 if g < rem else 0)
        bounds.append((start, start + size))
        start += size

    splits: list[tuple[list[int], list[int]]] = []
    for combo in combinations(range(n_groups), k_test):
        test_idx: list[int] = []
        excluded: set[int] = set()
        for g in combo:
            s, e = bounds[g]
            test_idx.extend(range(s, e))
            excluded.update(range(max(0, s - purge), s))           # purge before
            excluded.update(range(e, min(n_obs, e + purge)))       # purge after
            excluded.update(range(e, min(n_obs, e + embargo)))     # embargo after
        test_set = set(test_idx)
        train_idx = [i for i in range(n_obs) if i not in test_set and i not in excluded]
        splits.append((train_idx, sorted(test_idx)))
    return splits


# --------------------------------------------------------------------------
# 2. Sharpe / PSR / DSR
# --------------------------------------------------------------------------


def sharpe(returns: list[float]) -> float:
    """Per-period Sharpe ratio: mean(returns) / sample stdev(returns).

    Zero-variance series degenerate to +/-inf in the sign of the mean
    (0.0 if the mean is also zero). Requires at least 2 observations.
    """
    n = len(returns)
    if n < 2:
        raise ValueError(f"sharpe: need >= 2 observations, got {n}")
    mean = math.fsum(returns) / n
    var = math.fsum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0.0:
        return 0.0 if mean == 0.0 else math.copysign(math.inf, mean)
    return mean / math.sqrt(var)


def psr(
    sr_hat: float,
    n: int,
    skew: float,
    kurt: float,
    sr_benchmark: float = 0.0,
) -> float:
    """Probabilistic Sharpe Ratio: P[true SR > sr_benchmark | SR_hat].

        PSR = Phi[ (SR_hat - SR*) * sqrt(n - 1)
                   / sqrt(1 - skew*SR_hat + ((kurt - 1)/4)*SR_hat^2) ]

    sr_hat:  observed per-period (NON-annualized) Sharpe ratio.
    n:       number of return observations (>= 2).
    skew:    skewness of the returns (Normal => 0).
    kurt:    Pearson (raw) kurtosis of the returns (Normal => 3).

    Bailey & Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier",
    Journal of Risk 15(2). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643
    """
    if n < 2:
        raise ValueError(f"psr: need n >= 2 observations, got {n}")
    denom_sq = 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * sr_hat * sr_hat
    if denom_sq <= 0.0 or not math.isfinite(denom_sq):
        raise ValueError(
            f"psr: degenerate SR variance (1 - skew*SR + (kurt-1)/4*SR^2 = {denom_sq})"
        )
    z = (sr_hat - sr_benchmark) * math.sqrt(n - 1.0) / math.sqrt(denom_sq)
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of n_trials unskilled (true SR = 0) strategies — the
    False Strategy Theorem benchmark used by the DSR:

        E[max SR] ~= sqrt(V[SR]) * ( (1 - gamma) * PhiInv(1 - 1/N)
                                     + gamma     * PhiInv(1 - 1/(N*e)) )

    gamma = Euler-Mascheroni. Degenerate cases (n_trials <= 1 or var_sr <= 0)
    return 0.0 — a single trial deflates against the plain 0 benchmark.

    Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio", JPM 40(5).
    """
    if n_trials < 1:
        raise ValueError(f"expected_max_sharpe: n_trials must be >= 1, got {n_trials}")
    if var_sr < 0.0:
        raise ValueError(f"expected_max_sharpe: var_sr must be >= 0, got {var_sr}")
    if n_trials == 1 or var_sr == 0.0:
        return 0.0
    g = EULER_GAMMA
    return math.sqrt(var_sr) * (
        (1.0 - g) * norm_ppf(1.0 - 1.0 / n_trials)
        + g * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def dsr(
    sr_hat: float,
    n: int,
    skew: float,
    kurt: float,
    n_trials: int,
    var_sr: float,
) -> float:
    """Deflated Sharpe Ratio: PSR evaluated against E[max SR of n_trials].

        DSR = PSR(SR*),  SR* = E[max SR]  (False Strategy Theorem)

    i.e. P[true SR > expected best-of-N-unskilled | SR_hat]. With
    n_trials > 1 and var_sr > 0 the benchmark is positive, so DSR < PSR:
    deflation bites exactly when many trials were run.

    n_trials: number of effectively independent strategy trials behind sr_hat.
    var_sr:   cross-sectional variance of the trials' Sharpe ratios.

    Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio: Correcting for
    Selection Bias, Backtest Overfitting and Non-Normality", JPM 40(5), 94-107.
    https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
    """
    return psr(sr_hat, n, skew, kurt, sr_benchmark=expected_max_sharpe(n_trials, var_sr))


# --------------------------------------------------------------------------
# 3. PBO — Probability of Backtest Overfitting (CSCV)
# --------------------------------------------------------------------------


def _block_sharpe(n: int, s1: float, s2: float) -> float:
    """Sharpe from aggregated count / sum / sum-of-squares."""
    mean = s1 / n
    var = (s2 - n * mean * mean) / (n - 1)
    if var <= 0.0:
        return 0.0 if mean == 0.0 else math.copysign(math.inf, mean)
    return mean / math.sqrt(var)


def pbo(perf_matrix: list[list[float]], n_partitions: int = 16) -> dict:
    """Probability of Backtest Overfitting via CSCV (Bailey et al. 2015).

    perf_matrix: T rows (time observations, oldest first) x N columns
                 (strategy variants) of per-period performance (returns).
    n_partitions: S, the EVEN number of contiguous row-blocks.

    Procedure (Bailey, Borwein, Lopez de Prado & Zhu 2015, §4 — see module
    docstring for URLs):
      1. Slice the rows into S contiguous blocks.
      2. For each of the C(S, S/2) ways to choose S/2 blocks as in-sample
         (IS; complement is out-of-sample, OOS), compute each strategy's
         Sharpe ratio on IS and on OOS.
      3. Let n* = argmax IS Sharpe (first index on ties). Its OOS relative
         rank is omega = rank(OOS_{n*}) / (N + 1) in (0, 1), where rank
         counts strategies with OOS performance <= OOS_{n*}.
      4. Logit lambda = ln(omega / (1 - omega)); lambda <= 0 means the IS
         winner was at or below the OOS median.
      5. PBO = (# logits <= 0) / (# combinations).

    Returns {"pbo": float, "logits": list[float]} with logits in
    combination order. Deterministic.
    """
    t = len(perf_matrix)
    if t == 0:
        raise ValueError("pbo: perf_matrix must be non-empty")
    n_strat = len(perf_matrix[0])
    if n_strat < 2:
        raise ValueError(f"pbo: need >= 2 strategy columns, got {n_strat}")
    if any(len(row) != n_strat for row in perf_matrix):
        raise ValueError("pbo: perf_matrix rows must all have the same length")
    if n_partitions < 2 or n_partitions % 2 != 0:
        raise ValueError(f"pbo: n_partitions must be an even integer >= 2, got {n_partitions}")
    if t < 2 * n_partitions:
        raise ValueError(
            f"pbo: need >= 2*n_partitions rows ({2 * n_partitions}) for per-half "
            f"Sharpe ratios, got {t}"
        )

    # Contiguous row-blocks (first t % S blocks take one extra row), with
    # per-strategy sufficient statistics so each combination is O(S*N).
    base, rem = divmod(t, n_partitions)
    blocks: list[list[tuple[int, float, float]]] = []  # blocks[b][j] = (n, sum, sumsq)
    start = 0
    for b in range(n_partitions):
        size = base + (1 if b < rem else 0)
        rows = perf_matrix[start : start + size]
        start += size
        blocks.append(
            [
                (
                    size,
                    math.fsum(row[j] for row in rows),
                    math.fsum(row[j] * row[j] for row in rows),
                )
                for j in range(n_strat)
            ]
        )

    all_blocks = frozenset(range(n_partitions))
    logits: list[float] = []
    for is_combo in combinations(range(n_partitions), n_partitions // 2):
        oos_combo = sorted(all_blocks.difference(is_combo))

        def half_perf(combo: tuple[int, ...] | list[int]) -> list[float]:
            perf = []
            for j in range(n_strat):
                n = sum(blocks[b][j][0] for b in combo)
                s1 = math.fsum(blocks[b][j][1] for b in combo)
                s2 = math.fsum(blocks[b][j][2] for b in combo)
                perf.append(_block_sharpe(n, s1, s2))
            return perf

        is_perf = half_perf(is_combo)
        oos_perf = half_perf(oos_combo)
        n_star = max(range(n_strat), key=lambda j: (is_perf[j], -j))
        rank = sum(1 for v in oos_perf if v <= oos_perf[n_star])  # 1..N
        omega = rank / (n_strat + 1.0)
        logits.append(math.log(omega / (1.0 - omega)))

    n_combos = len(logits)
    return {"pbo": sum(1 for lam in logits if lam <= 0.0) / n_combos, "logits": logits}
