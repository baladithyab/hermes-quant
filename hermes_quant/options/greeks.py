"""Ergonomic wrappers around the optlib pricing kernel.

This module provides a dataclass-friendly facade over
:mod:`hermes_quant.options.pricing`. It is the import everyone in the codebase
should reach for when they need a Greek; only reach into ``.pricing`` directly
if you need a non-equity model (Black-76, Kirk's, Asian, FX).

Conventions:
    - ``option_type``: ``"c"`` for call, ``"p"`` for put.
    - All rates and volatilities are decimals (``0.05`` = 5 %).
    - Time is in years. For days-to-expiry: ``dte_years = dte_days / 365.0``.
    - Pricers return ``OptionGreeks`` (frozen dataclass).
    - This module is **pure** — no I/O, no live API, no global state.
      Safe to call from analyst hot paths.

Example:
    >>> from hermes_quant.options.greeks import european_greeks
    >>> g = european_greeks("c", spot=185.0, strike=190.0,
    ...                     dte_years=30/365, rfr=0.05,
    ...                     dividend_yield=0.005, iv=0.25)
    >>> g.value, g.delta  # doctest: +SKIP
    (2.39..., 0.42...)
"""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import (
    amer_implied_vol,
    american,
    euro_implied_vol,
    merton,
)


@dataclass(frozen=True)
class OptionGreeks:
    """Frozen container for a price + first/second-order Greeks.

    Attributes:
        value: Option premium in the units of the underlying.
        delta: dValue / dSpot.
        gamma: d²Value / dSpot².
        theta: dValue / dt (per year; divide by 365 for per-calendar-day).
        vega: dValue / dVol (per unit vol; multiply by 0.01 for "per 1 % vol").
        rho: dValue / dRate (per unit rate; multiply by 0.01 for "per 1 % rate").
    """

    value: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _validate_option_type(option_type: str) -> None:
    if option_type not in ("c", "p"):
        raise ValueError(f"option_type must be 'c' or 'p', got {option_type!r}")


def european_greeks(
    option_type: str,
    spot: float,
    strike: float,
    dte_years: float,
    rfr: float,
    dividend_yield: float,
    iv: float,
) -> OptionGreeks:
    """Price a European option (Merton model) and return its Greeks.

    Wraps :func:`hermes_quant.options.pricing.merton`.

    Args:
        option_type: ``"c"`` or ``"p"``.
        spot: Current price of the underlying.
        strike: Option strike.
        dte_years: Time to expiry in years.
        rfr: Risk-free rate (decimal).
        dividend_yield: Continuous dividend yield (decimal; 0 if none).
        iv: Implied volatility (decimal, annualized).
    """
    _validate_option_type(option_type)
    value, delta, gamma, theta, vega, rho = merton(
        option_type, spot, strike, dte_years, rfr, dividend_yield, iv
    )
    return OptionGreeks(value, delta, gamma, theta, vega, rho)


def american_greeks(
    option_type: str,
    spot: float,
    strike: float,
    dte_years: float,
    rfr: float,
    dividend_yield: float,
    iv: float,
) -> OptionGreeks:
    """Price an American option (Bjerksund–Stensland 2002) and return Greeks.

    Wraps :func:`hermes_quant.options.pricing.american`. Greeks are inherited
    from the European GBS formula; for tight precision on deep-ITM Americans
    where early exercise is near-certain, bump-and-revalue numerically.
    """
    _validate_option_type(option_type)
    value, delta, gamma, theta, vega, rho = american(
        option_type, spot, strike, dte_years, rfr, dividend_yield, iv
    )
    return OptionGreeks(value, delta, gamma, theta, vega, rho)


def implied_vol(
    option_type: str,
    spot: float,
    strike: float,
    dte_years: float,
    rfr: float,
    dividend_yield: float,
    market_price: float,
    american: bool = False,
) -> float:
    """Recover implied volatility from an observed market premium.

    Wraps :func:`amer_implied_vol` when ``american=True``, else
    :func:`euro_implied_vol`. Falls back from Newton–Raphson to bisection
    automatically inside the pricing kernel.

    Args:
        option_type: ``"c"`` or ``"p"``.
        spot, strike, dte_years, rfr, dividend_yield: as for the pricers.
        market_price: Observed call/put premium.
        american: If ``True``, use the American IV solver (bisection).

    Returns:
        Implied volatility (decimal, annualized).

    Raises:
        GBS_CalculationError: If the solver can't converge.
    """
    _validate_option_type(option_type)
    if american:
        return amer_implied_vol(
            option_type, spot, strike, dte_years, rfr, dividend_yield, market_price
        )
    return euro_implied_vol(option_type, spot, strike, dte_years, rfr, dividend_yield, market_price)


def covered_call_yield_per_period(
    spot: float,
    strike: float,
    dte_years: float,
    rfr: float,
    dividend_yield: float,
    iv: float,
) -> float:
    """Per-period yield of writing a covered call: ``call_premium / spot``.

    This is the headline number for socalminh's covered-call methodology
    (ADR-0030): ``yield_per_period >= 0.10`` at ``dte_years ≈ 30/365`` is the
    "10 %/month" rule. We use the European Merton model — the difference
    between European and American call values is negligible for short-dated,
    near-the-money calls on dividend-paying equities, and using the European
    formula keeps this hot-path call cheap.

    Args:
        spot: Underlying price.
        strike: Call strike (typically OTM by some delta or % of spot).
        dte_years: Time to expiry, in years.
        rfr, dividend_yield, iv: as for the pricers.

    Returns:
        Premium received as a fraction of spot. ``0.10`` = 10 % / period.
    """
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    call_premium = merton("c", spot, strike, dte_years, rfr, dividend_yield, iv)[0]
    return call_premium / spot
