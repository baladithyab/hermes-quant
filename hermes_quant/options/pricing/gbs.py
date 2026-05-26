# SPDX-License-Identifier: MIT
#
# Vendored from optlib (https://github.com/dbrojas/optlib), commit 2022-11-18.
# Original code: Copyright (c) 2017 Davis W. Edwards
#   "Energy Trading and Investing", "Risk Management in Trading",
#   "Energy Investing Demystified".
# Packaged into optlib by: Daniel Rojas (2020-2021).
# Vendored into hermes-quant 2026-05-26 (upstream effectively unmaintained).
#
# See LICENSE.optlib in this directory for the verbatim MIT license text.
# Modifications since vendoring:
#   - Removed `from __future__ import division` (Python 3.11+ target).
#   - Replaced .format() with f-strings; collapsed verbose debug logs.
#   - Reflowed for ruff line-length=100 + repo style (E, F, I, B, UP, N, W).
#   - Replaced deprecated `scipy.stats.mvn.mvndst` with the equivalent
#     `scipy.stats.multivariate_normal.cdf` (SciPy 1.13+ removed the F77 path).
#   - No algorithmic changes — pricing math is byte-identical to upstream.
#
# Public surface (re-exported from `hermes_quant.options.pricing`):
#   Pricers (8): black_scholes, merton, black_76, garman_kohlhagen,
#                asian_76, kirks_76, american, american_76
#   IV solvers (4): euro_implied_vol, euro_implied_vol_76,
#                   amer_implied_vol, amer_implied_vol_76
#   Each pricer returns (value, delta, gamma, theta, vega, rho).

"""Closed-form option pricing kernel (vendored from optlib).

Generalized Black–Scholes family + Bjerksund–Stensland (2002) American
approximation + Newton/bisection implied-volatility solvers.

This module is intentionally a thin port: the public function names, argument
order, and return tuples mirror upstream so existing optlib callers can
swap their import without code changes.
"""

import logging
import math

import numpy as np
from scipy.stats import multivariate_normal, norm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits + exceptions
# ---------------------------------------------------------------------------


class _GBS_Limits:  # noqa: N801 — name preserved from upstream
    """Numerical sanity bounds for GBS inputs (private)."""

    MAX32 = 2147483248.0

    MIN_T = 1.0 / 1000.0  # require some time left before expiration
    MIN_X = 0.01
    MIN_FS = 0.01

    # Volatility under 0.5% causes the American Option calc to blow up.
    # _gbs() is fine with anything positive; we apply this floor everywhere
    # so numerical issues surface as input errors, not silent NaNs.
    MIN_V = 0.005

    MAX_T = 100
    MAX_X = MAX32
    MAX_FS = MAX32

    # Asian option averaging-period limits.
    MIN_TA = 0

    # b, r, V capped at 200% — protects against fat-finger inputs like
    # passing 15 instead of 0.15.
    MIN_b = -1  # noqa: N815
    MIN_r = -1  # noqa: N815

    MAX_b = 1  # noqa: N815
    MAX_r = 2  # noqa: N815
    MAX_V = 2


class GBS_InputError(Exception):  # noqa: N801 — name preserved
    """Invalid input to a GBS pricing function."""

    def __init__(self, mismatch: str) -> None:
        super().__init__(mismatch)


class GBS_CalculationError(Exception):  # noqa: N801 — name preserved
    """Calculation failed to converge."""

    def __init__(self, mismatch: str) -> None:
        super().__init__(mismatch)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _test_option_type(option_type: str) -> None:
    if option_type not in ("c", "p"):
        raise GBS_InputError(
            f"Invalid Input option_type ({option_type}). Acceptable value are: c, p"
        )


def _gbs_test_inputs(option_type, fs, x, t, r, b, v):
    _test_option_type(option_type)

    if (x < _GBS_Limits.MIN_X) or (x > _GBS_Limits.MAX_X):
        raise GBS_InputError(
            f"Invalid Input Strike Price (X={x}). "
            f"Acceptable range is {_GBS_Limits.MIN_X} to {_GBS_Limits.MAX_X}"
        )
    if (fs < _GBS_Limits.MIN_FS) or (fs > _GBS_Limits.MAX_FS):
        raise GBS_InputError(
            f"Invalid Input Forward/Spot Price (FS={fs}). "
            f"Acceptable range is {_GBS_Limits.MIN_FS} to {_GBS_Limits.MAX_FS}"
        )
    if (t < _GBS_Limits.MIN_T) or (t > _GBS_Limits.MAX_T):
        raise GBS_InputError(
            f"Invalid Input Time (T={t}). "
            f"Acceptable range is {_GBS_Limits.MIN_T} to {_GBS_Limits.MAX_T}"
        )
    if (b < _GBS_Limits.MIN_b) or (b > _GBS_Limits.MAX_b):
        raise GBS_InputError(
            f"Invalid Input Cost of Carry (b={b}). "
            f"Acceptable range is {_GBS_Limits.MIN_b} to {_GBS_Limits.MAX_b}"
        )
    if (r < _GBS_Limits.MIN_r) or (r > _GBS_Limits.MAX_r):
        raise GBS_InputError(
            f"Invalid Input Risk Free Rate (r={r}). "
            f"Acceptable range is {_GBS_Limits.MIN_r} to {_GBS_Limits.MAX_r}"
        )
    if (v < _GBS_Limits.MIN_V) or (v > _GBS_Limits.MAX_V):
        raise GBS_InputError(
            f"Invalid Input Implied Volatility (V={v}). "
            f"Acceptable range is {_GBS_Limits.MIN_V} to {_GBS_Limits.MAX_V}"
        )


# ---------------------------------------------------------------------------
# Generalized Black–Scholes (European, cost-of-carry parametrization)
# ---------------------------------------------------------------------------


def _gbs(option_type, fs, x, t, r, b, v):
    """Generalized Black–Scholes pricer.

    Returns ``(value, delta, gamma, theta, vega, rho)``.

    The cost-of-carry ``b`` parametrization lets the same engine price:
        b = r       Black–Scholes (no dividend)
        b = r - q   Merton (continuous dividend yield)
        b = 0       Black-76 (commodities / forwards)
        b = r - rf  Garman–Kohlhagen (FX)
    """
    _gbs_test_inputs(option_type, fs, x, t, r, b, v)

    t__sqrt = math.sqrt(t)
    d1 = (math.log(fs / x) + (b + (v * v) / 2) * t) / (v * t__sqrt)
    d2 = d1 - v * t__sqrt

    if option_type == "c":
        value = fs * math.exp((b - r) * t) * norm.cdf(d1) - x * math.exp(-r * t) * norm.cdf(d2)
        delta = math.exp((b - r) * t) * norm.cdf(d1)
        gamma = math.exp((b - r) * t) * norm.pdf(d1) / (fs * v * t__sqrt)
        theta = (
            -(fs * v * math.exp((b - r) * t) * norm.pdf(d1)) / (2 * t__sqrt)
            - (b - r) * fs * math.exp((b - r) * t) * norm.cdf(d1)
            - r * x * math.exp(-r * t) * norm.cdf(d2)
        )
        vega = math.exp((b - r) * t) * fs * t__sqrt * norm.pdf(d1)
        rho = x * t * math.exp(-r * t) * norm.cdf(d2)
    else:
        value = x * math.exp(-r * t) * norm.cdf(-d2) - (fs * math.exp((b - r) * t) * norm.cdf(-d1))
        delta = -math.exp((b - r) * t) * norm.cdf(-d1)
        gamma = math.exp((b - r) * t) * norm.pdf(d1) / (fs * v * t__sqrt)
        theta = (
            -(fs * v * math.exp((b - r) * t) * norm.pdf(d1)) / (2 * t__sqrt)
            + (b - r) * fs * math.exp((b - r) * t) * norm.cdf(-d1)
            + r * x * math.exp(-r * t) * norm.cdf(-d2)
        )
        vega = math.exp((b - r) * t) * fs * t__sqrt * norm.pdf(d1)
        rho = -x * t * math.exp(-r * t) * norm.cdf(-d2)

    return value, delta, gamma, theta, vega, rho


# ---------------------------------------------------------------------------
# American options — Bjerksund–Stensland (2002) closed-form approximation
# ---------------------------------------------------------------------------


def _american_option(option_type, fs, x, t, r, b, v):
    """Generalized American-option pricer (routes to BS-2002)."""
    _gbs_test_inputs(option_type, fs, x, t, r, b, v)

    if option_type == "c":
        return _bjerksund_stensland_2002(fs, x, t, r, b, v)

    # Put–call transformation: P(X, FS, T, r, b, V) = C(FS, X, T, -b, r-b, V).
    # WARNING — when reconciling against the B&S paper, variable order differs.
    put__x = fs
    put_fs = x
    put_b = -b
    put_r = r - b
    return _bjerksund_stensland_2002(put_fs, put__x, t, put_r, put_b, v)


def _bjerksund_stensland_1993(fs, x, t, r, b, v):
    """Bjerksund–Stensland 1993 single-boundary approximation (kept for tests)."""
    e_value, delta, gamma, theta, vega, rho = _gbs("c", fs, x, t, r, b, v)

    # If b >= r, never optimal to early-exercise an American call → European value.
    if b >= r:
        return e_value, delta, gamma, theta, vega, rho

    v2 = v**2
    sqrt_t = math.sqrt(t)

    beta = (0.5 - b / v2) + math.sqrt(((b / v2 - 0.5) ** 2) + 2 * r / v2)
    b_infinity = (beta / (beta - 1)) * x
    b_zero = max(x, (r / (r - b)) * x)

    h1 = -(b * t + 2 * v * sqrt_t) * (b_zero / (b_infinity - b_zero))
    i = b_zero + (b_infinity - b_zero) * (1 - math.exp(h1))
    alpha = (i - x) * (i ** (-beta))

    if fs >= i:
        value = fs - x
    else:
        value = (
            alpha * (fs**beta)
            - alpha * _phi(fs, t, beta, i, i, r, b, v)
            + _phi(fs, t, 1, i, i, r, b, v)
            - _phi(fs, t, 1, x, i, r, b, v)
            - x * _phi(fs, t, 0, i, i, r, b, v)
            + x * _phi(fs, t, 0, x, i, r, b, v)
        )

    # Approximation can break down at boundary conditions — floor at European.
    value = max(value, e_value)
    return value, delta, gamma, theta, vega, rho


def _bjerksund_stensland_2002(fs, x, t, r, b, v):
    """Bjerksund–Stensland 2002 two-boundary approximation."""
    e_value, delta, gamma, theta, vega, rho = _gbs("c", fs, x, t, r, b, v)

    # If b >= r, never optimal to early-exercise → European value.
    if b >= r:
        return e_value, delta, gamma, theta, vega, rho

    v2 = v**2
    t1 = 0.5 * (math.sqrt(5) - 1) * t
    t2 = t

    beta_inside = ((b / v2 - 0.5) ** 2) + 2 * r / v2
    # Force the inside of the sqrt to be positive (numerical guard).
    beta_inside = abs(beta_inside)
    beta = (0.5 - b / v2) + math.sqrt(beta_inside)
    b_infinity = (beta / (beta - 1)) * x
    b_zero = max(x, (r / (r - b)) * x)

    h1 = -(b * t1 + 2 * v * math.sqrt(t1)) * ((x**2) / ((b_infinity - b_zero) * b_zero))
    h2 = -(b * t2 + 2 * v * math.sqrt(t2)) * ((x**2) / ((b_infinity - b_zero) * b_zero))

    i1 = b_zero + (b_infinity - b_zero) * (1 - math.exp(h1))
    i2 = b_zero + (b_infinity - b_zero) * (1 - math.exp(h2))

    alpha1 = (i1 - x) * (i1 ** (-beta))
    alpha2 = (i2 - x) * (i2 ** (-beta))

    if fs >= i2:
        value = fs - x
    else:
        value = (
            alpha2 * (fs**beta)
            - alpha2 * _phi(fs, t1, beta, i2, i2, r, b, v)
            + _phi(fs, t1, 1, i2, i2, r, b, v)
            - _phi(fs, t1, 1, i1, i2, r, b, v)
            - x * _phi(fs, t1, 0, i2, i2, r, b, v)
            + x * _phi(fs, t1, 0, i1, i2, r, b, v)
            + alpha1 * _phi(fs, t1, beta, i1, i2, r, b, v)
            - alpha1 * _psi(fs, t2, beta, i1, i2, i1, t1, r, b, v)
            + _psi(fs, t2, 1, i1, i2, i1, t1, r, b, v)
            - _psi(fs, t2, 1, x, i2, i1, t1, r, b, v)
            - x * _psi(fs, t2, 0, i1, i2, i1, t1, r, b, v)
            + x * _psi(fs, t2, 0, x, i2, i1, t1, r, b, v)
        )

    # Floor at European value (boundary-condition guard).
    value = max(value, e_value)
    return value, delta, gamma, theta, vega, rho


# ---------------------------------------------------------------------------
# Bjerksund–Stensland intermediate functions
# ---------------------------------------------------------------------------


def _psi(fs, t2, gamma, h, i2, i1, t1, r, b, v):
    """Psi() helper used by the BS-2002 model."""
    vsqrt_t1 = v * math.sqrt(t1)
    vsqrt_t2 = v * math.sqrt(t2)

    bgamma_t1 = (b + (gamma - 0.5) * (v**2)) * t1
    bgamma_t2 = (b + (gamma - 0.5) * (v**2)) * t2

    d1 = (math.log(fs / i1) + bgamma_t1) / vsqrt_t1
    d3 = (math.log(fs / i1) - bgamma_t1) / vsqrt_t1

    d2 = (math.log((i2**2) / (fs * i1)) + bgamma_t1) / vsqrt_t1
    d4 = (math.log((i2**2) / (fs * i1)) - bgamma_t1) / vsqrt_t1

    e1 = (math.log(fs / h) + bgamma_t2) / vsqrt_t2
    e2 = (math.log((i2**2) / (fs * h)) + bgamma_t2) / vsqrt_t2
    e3 = (math.log((i1**2) / (fs * h)) + bgamma_t2) / vsqrt_t2
    e4 = (math.log((fs * (i1**2)) / (h * (i2**2))) + bgamma_t2) / vsqrt_t2

    tau = math.sqrt(t1 / t2)
    lambda1 = -r + gamma * b + 0.5 * gamma * (gamma - 1) * (v**2)
    kappa = (2 * b) / (v**2) + (2 * gamma - 1)

    psi = (
        math.exp(lambda1 * t2)
        * (fs**gamma)
        * (
            _cbnd(-d1, -e1, tau)
            - ((i2 / fs) ** kappa) * _cbnd(-d2, -e2, tau)
            - ((i1 / fs) ** kappa) * _cbnd(-d3, -e3, -tau)
            + ((i1 / i2) ** kappa) * _cbnd(-d4, -e4, -tau)
        )
    )
    return psi


def _phi(fs, t, gamma, h, i, r, b, v):
    """Phi() helper used by the BS-2002 and BS-1993 models."""
    d1 = -(math.log(fs / h) + (b + (gamma - 0.5) * (v**2)) * t) / (v * math.sqrt(t))
    d2 = d1 - 2 * math.log(i / fs) / (v * math.sqrt(t))

    lambda1 = -r + gamma * b + 0.5 * gamma * (gamma - 1) * (v**2)
    kappa = (2 * b) / (v**2) + (2 * gamma - 1)

    phi = math.exp(lambda1 * t) * (fs**gamma) * (norm.cdf(d1) - ((i / fs) ** kappa) * norm.cdf(d2))
    return phi


def _cbnd(a, b, rho):
    """Cumulative bivariate normal: ``P(X <= a, Y <= b)`` with correlation ``rho``.

    Upstream optlib used ``scipy.stats.mvn.mvndst`` (Genz F77 routine), which
    SciPy deprecated in favor of the OO ``multivariate_normal`` interface.
    Mathematically equivalent — the CDF of a zero-mean bivariate normal with
    unit variances and correlation ``rho`` evaluated at the point ``(a, b)``.
    """
    cov = np.array([[1.0, rho], [rho, 1.0]])
    return float(multivariate_normal.cdf([a, b], mean=[0.0, 0.0], cov=cov))


# ---------------------------------------------------------------------------
# Implied-volatility solvers
# ---------------------------------------------------------------------------


def _approx_implied_vol(option_type, fs, x, t, r, b, cp):
    """Brenner & Subrahmanyam (1988) starting-point approximation."""
    _test_option_type(option_type)

    ebrt = math.exp((b - r) * t)
    ert = math.exp(-r * t)

    a = math.sqrt(2 * math.pi) / (fs * ebrt + x * ert)

    if option_type == "c":
        payoff = fs * ebrt - x * ert
    else:
        payoff = x * ert - fs * ebrt

    b = cp - payoff / 2
    c = (payoff**2) / math.pi

    v = (a * (b + math.sqrt(b**2 + c))) / math.sqrt(t)
    return v


def _gbs_implied_vol(option_type, fs, x, t, r, b, cp, precision=0.00001, max_steps=100):
    """European IV via Newton–Raphson (vega-driven)."""
    return _newton_implied_vol(_gbs, option_type, x, fs, t, b, r, cp, precision, max_steps)


def _american_implied_vol(option_type, fs, x, t, r, b, cp, precision=0.00001, max_steps=100):
    """American IV via bisection (vega is unreliable for early-exercise)."""
    return _bisection_implied_vol(
        _american_option, option_type, fs, x, t, r, b, cp, precision, max_steps
    )


def _newton_implied_vol(val_fn, option_type, x, fs, t, b, r, cp, precision=0.00001, max_steps=100):
    """Newton–Raphson IV search; falls back to bisection if it doesn't converge."""
    _test_option_type(option_type)

    v = _approx_implied_vol(option_type, fs, x, t, r, b, cp)
    v = max(_GBS_Limits.MIN_V, v)
    v = min(_GBS_Limits.MAX_V, v)

    value, _delta, _gamma, _theta, vega, _rho = val_fn(option_type, fs, x, t, r, b, v)
    min_diff = abs(cp - value)

    countr = 0
    while precision <= abs(cp - value) <= min_diff and countr < max_steps:
        v = v - (value - cp) / vega
        if (v > _GBS_Limits.MAX_V) or (v < _GBS_Limits.MIN_V):
            logger.debug("    Volatility out of bounds")
            break

        value, _delta, _gamma, _theta, vega, _rho = val_fn(option_type, fs, x, t, r, b, v)
        min_diff = min(abs(cp - value), min_diff)
        countr += 1

    if abs(cp - value) < precision:
        return v
    # Newton didn't converge — try bisection.
    return _bisection_implied_vol(val_fn, option_type, fs, x, t, r, b, cp, precision, max_steps)


def _bisection_implied_vol(
    val_fn, option_type, fs, x, t, r, b, cp, precision=0.00001, max_steps=100
):
    """Bisection IV search (no vega required)."""
    v_mid = _approx_implied_vol(option_type, fs, x, t, r, b, cp)

    if (v_mid <= _GBS_Limits.MIN_V) or (v_mid >= _GBS_Limits.MAX_V):
        v_low = _GBS_Limits.MIN_V
        v_high = _GBS_Limits.MAX_V
        v_mid = (v_low + v_high) / 2
    else:
        v_low = max(_GBS_Limits.MIN_V, v_mid * 0.5)
        v_high = min(_GBS_Limits.MAX_V, v_mid * 1.5)

    cp_mid = val_fn(option_type, fs, x, t, r, b, v_mid)[0]

    current_step = 0
    diff = abs(cp - cp_mid)

    while (diff > precision) and (current_step < max_steps):
        current_step += 1

        if cp_mid < cp:
            v_low = v_mid
        else:
            v_high = v_mid

        cp_low = val_fn(option_type, fs, x, t, r, b, v_low)[0]
        cp_high = val_fn(option_type, fs, x, t, r, b, v_high)[0]

        v_mid = v_low + (cp - cp_low) * (v_high - v_low) / (cp_high - cp_low)
        v_mid = max(_GBS_Limits.MIN_V, v_mid)
        v_mid = min(_GBS_Limits.MAX_V, v_mid)

        cp_mid = val_fn(option_type, fs, x, t, r, b, v_mid)[0]
        diff = abs(cp - cp_mid)

    if abs(cp - cp_mid) < precision:
        return v_mid
    raise GBS_CalculationError(
        f"Implied Vol did not converge. Best Guess={v_mid}, "
        f"Price diff={diff}, Required Precision={precision}"
    )


# ---------------------------------------------------------------------------
# Public pricer interface
# ---------------------------------------------------------------------------


def black_scholes(option_type, fs, x, t, r, v):
    """Black–Scholes (1973) — European option on non-dividend-paying stock.

    Args:
        option_type: ``"c"`` for call, ``"p"`` for put.
        fs: Spot price of the underlying.
        x: Strike price.
        t: Time to expiration in years (1.0 = one year).
        r: Risk-free rate (continuously compounded).
        v: Implied volatility (annualized, decimal).

    Returns:
        Tuple ``(value, delta, gamma, theta, vega, rho)``.
    """
    b = r
    return _gbs(option_type, fs, x, t, r, b, v)


def merton(option_type, fs, x, t, r, q, v):
    """Merton (1973) — European option with continuous dividend yield ``q``.

    Args:
        option_type: ``"c"`` or ``"p"``.
        fs, x, t, r, v: as for ``black_scholes``.
        q: Continuous dividend yield (decimal).

    Returns:
        Tuple ``(value, delta, gamma, theta, vega, rho)``.
    """
    b = r - q
    return _gbs(option_type, fs, x, t, r, b, v)


def black_76(option_type, fs, x, t, r, v):
    """Black-76 — European option on a forward/futures price.

    The underlying is a forward, so cost-of-carry ``b = 0``.
    """
    b = 0
    return _gbs(option_type, fs, x, t, r, b, v)


def garman_kohlhagen(option_type, fs, x, t, r, rf, v):
    """Garman–Kohlhagen — European FX option.

    Args:
        rf: Foreign risk-free rate.
    """
    b = r - rf
    return _gbs(option_type, fs, x, t, r, b, v)


def asian_76(option_type, fs, x, t, t_a, r, v):
    """Average-price (Asian) commodity option, Levy approximation.

    Args:
        t_a: Time to start of averaging period (years), with ``0 <= t_a <= t``.
    """
    if (t_a < _GBS_Limits.MIN_TA) or (t_a > t):
        raise GBS_InputError(
            f"Invalid Input Averaging Time (TA={t_a}). "
            f"Acceptable range is {_GBS_Limits.MIN_TA} to <T"
        )

    b = 0
    if t_a == t:
        # No averaging window → vanilla Black-76.
        v_a = v
    else:
        m = (2 * math.exp((v**2) * t) - 2 * math.exp((v**2) * t_a) * (1 + (v**2) * (t - t_a))) / (
            (v**4) * ((t - t_a) ** 2)
        )
        v_a = math.sqrt(math.log(m) / t)

    return _gbs(option_type, fs, x, t, r, b, v_a)


def kirks_76(option_type, f1, f2, x, t, r, v1, v2, corr):
    """Spread option pricer via Kirk's approximation.

    Greeks are returned as zeros — Kirk's approximation does not have
    closed-form Greeks; compute them by finite-differences if you need them.
    """
    b = 0
    fs = f1 / (f2 + x)
    f_temp = f2 / (f2 + x)
    v = math.sqrt((v1**2) + ((v2 * f_temp) ** 2) - (2 * corr * v1 * v2 * f_temp))
    my_values = _gbs(option_type, fs, 1.0, t, r, b, v)
    return my_values[0] * (f2 + x), 0, 0, 0, 0, 0


def american(option_type, fs, x, t, r, q, v):
    """American option on stock with continuous dividend yield ``q``.

    Uses Bjerksund–Stensland (2002) closed-form approximation.
    Returns ``(value, delta, gamma, theta, vega, rho)``.

    Note: Greeks are inherited from the European GBS formula. They are
    accurate when early-exercise is not optimal (b >= r) and approximations
    when it is. For tight Greek precision on deep-ITM American options,
    bump-and-revalue numerically.
    """
    b = r - q
    return _american_option(option_type, fs, x, t, r, b, v)


def american_76(option_type, fs, x, t, r, v):
    """American option on a futures contract (Bjerksund–Stensland 2002, b=0)."""
    b = 0
    return _american_option(option_type, fs, x, t, r, b, v)


# ---------------------------------------------------------------------------
# Public implied-volatility interface
# ---------------------------------------------------------------------------


def euro_implied_vol(option_type, fs, x, t, r, q, cp):
    """European implied vol from observed market price ``cp``.

    Args:
        q: Continuous dividend yield (set to 0 for non-dividend-paying assets).
        cp: Observed call/put market premium.

    Returns:
        Implied volatility (decimal, annualized).
    """
    b = r - q
    return _gbs_implied_vol(option_type, fs, x, t, r, b, cp)


def euro_implied_vol_76(option_type, fs, x, t, r, cp):
    """European implied vol on a futures option (Black-76)."""
    b = 0
    return _gbs_implied_vol(option_type, fs, x, t, r, b, cp)


def amer_implied_vol(option_type, fs, x, t, r, q, cp):
    """American implied vol from observed market price ``cp`` (bisection)."""
    b = r - q
    return _american_implied_vol(option_type, fs, x, t, r, b, cp)


def amer_implied_vol_76(option_type, fs, x, t, r, cp):
    """American implied vol on a futures option (Black-76, bisection)."""
    b = 0
    return _american_implied_vol(option_type, fs, x, t, r, b, cp)
