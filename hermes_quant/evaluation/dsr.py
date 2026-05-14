"""hermes_quant.evaluation.dsr — Deflated Sharpe Ratio (Bailey & López de Prado 2014).

Per ADR-0019 §D4. The Deflated Sharpe Ratio adjusts an observed Sharpe
for the multiple-comparisons + non-normality bias inherent to backtest-
mined strategies.

For v0.3 paper-book Sharpe reporting, n_trials=1 (we're not searching);
DSR ≈ probabilistic-Sharpe-ratio of observed Sharpe. Multi-strategy
multiple-comparisons usage is the v0.4 hook for RL training.

Reference: Bailey, D. and López de Prado, M. (2014). "The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting, and
Non-Normality." Journal of Portfolio Management, 40(5), 94-107.
"""
from __future__ import annotations

import math


def deflated_sharpe(
    observed_sharpe: float,
    *,
    n_trials: int = 1,
    n_observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Probability that the observed Sharpe is not a false discovery.

    For n_trials=1 (single-strategy, no search), this collapses to the
    Probabilistic Sharpe Ratio. For n_trials > 1 (e.g., during RL
    hyperparameter search), the threshold tightens via the expected
    maximum order statistic of i.i.d. Sharpes.

    Args:
        observed_sharpe: The observed Sharpe ratio (e.g., 1.5).
        n_trials: How many strategies were searched/evaluated. Default 1
            (single strategy). v0.4 RL training uses n_trials = number
            of grid-search points or PPO epochs.
        n_observations: Number of return observations (e.g., daily returns
            count, hourly returns count).
        skew: Sample skewness of returns. Default 0.0 (normal).
        kurtosis: Sample kurtosis of returns. Default 3.0 (normal — note
            this is NOT excess kurtosis; normal distribution has
            kurtosis=3).

    Returns:
        Probability in [0, 1] that the observed Sharpe is statistically
        significant. Higher = more confident the strategy has real edge.

    Raises:
        ValueError: n_observations < 30 (DSR is meaningless on tiny samples)
        ValueError: n_trials < 1
    """
    if n_observations < 30:
        raise ValueError(
            f"n_observations must be >= 30 for DSR to be meaningful, "
            f"got {n_observations}"
        )
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")

    # ------------------------------------------------------------------
    # Step 1: Compute the expected maximum Sharpe under the null
    # (i.e., what's the highest Sharpe you'd see under n_trials random
    # strategies that all have true Sharpe = 0?)
    # ------------------------------------------------------------------
    # Bailey & López de Prado eq. 7
    if n_trials == 1:
        sharpe_threshold = 0.0
    else:
        # Expected max of N standard-normal: approximated by Bailey & LdP
        # using the Euler-Mascheroni constant
        gamma = 0.5772156649   # Euler–Mascheroni
        z_e = math.sqrt(2.0 * math.log(n_trials))
        sharpe_threshold = (
            (1.0 - gamma) * z_e
            + gamma * _inverse_normal_cdf(1.0 - 1.0 / (n_trials * math.e))
        )

    # ------------------------------------------------------------------
    # Step 2: Compute the probabilistic Sharpe ratio
    # PSR(SR*) = Φ((SR_obs - SR*) * sqrt(n - 1) /
    #             sqrt(1 - skew*SR_obs + (kurtosis - 1)/4 * SR_obs²))
    # ------------------------------------------------------------------
    n = float(n_observations)
    sr_diff = observed_sharpe - sharpe_threshold

    # Variance of sample Sharpe under non-normality (López de Prado 2014 eq. 4)
    variance_term = (
        1.0
        - skew * observed_sharpe
        + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2
    )
    if variance_term <= 0:
        # Degenerate case (extreme non-normality). Return 0.5 (no info).
        return 0.5

    z = sr_diff * math.sqrt(n - 1.0) / math.sqrt(variance_term)

    return float(_normal_cdf(z))


def _normal_cdf(x: float) -> float:
    """Standard normal CDF Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _inverse_normal_cdf(p: float) -> float:
    """Inverse standard normal CDF (probit). Beasley-Springer-Moro approximation.

    Adequate for DSR's purposes (we only need ~3 sig figs of accuracy
    in the [0.5, 0.999] range).
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")

    # Beasley-Springer-Moro coefficients
    a = [
        -3.969683028665376e+01,
         2.209460984245205e+02,
        -2.759285104469687e+02,
         1.383577518672690e+02,
        -3.066479806614716e+01,
         2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01,
         1.615858368580409e+02,
        -1.556989798598866e+02,
         6.680131188771972e+01,
        -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e+00,
        -2.549732539343734e+00,
         4.374664141464968e+00,
         2.938163982698783e+00,
    ]
    d = [
         7.784695709041462e-03,
         3.224671290700398e-01,
         2.445134137142996e+00,
         3.754408661907416e+00,
    ]
    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / (
                (((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / (
                (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0))
    # p > p_high
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / (
             (((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
