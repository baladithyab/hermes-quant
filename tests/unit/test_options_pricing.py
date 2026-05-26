"""Validation tests for hermes_quant.options.pricing + .greeks.

Reference values are from Hull, *Options, Futures, and Other Derivatives*,
10th Edition (2017). Each test docstring cites the chapter and example #.

We aim for <0.005 absolute error against Hull's printed values; Hull rounds
to 2 decimals so anything tighter than that is below the noise floor of the
textbook itself.

Test groups:
    1. Hull textbook values (Ch 15, 16, 17, 18)
    2. Put–call parity at-the-money sanity
    3. Implied-volatility round-trip (European + American)
    4. Bjerksund–Stensland sanity (American >= European)
    5. Greeks numerical-derivative cross-check
"""

from __future__ import annotations

import math

import pytest

from hermes_quant.options.greeks import (
    OptionGreeks,
    american_greeks,
    covered_call_yield_per_period,
    european_greeks,
    implied_vol,
)
from hermes_quant.options.pricing import (
    american,
    asian_76,
    black_76,
    black_scholes,
    euro_implied_vol,
    merton,
)

# ---------------------------------------------------------------------------
# 1. Hull 10th Edition textbook values
# ---------------------------------------------------------------------------


def test_hull_15_6_call_no_dividend():
    """Hull 10e, Chapter 15, Example 15.6.

    European call. S=42, K=40, r=10 %, T=6 months, σ=20 %. Hull prints c=4.76.
    """
    c, *_ = black_scholes("c", 42.0, 40.0, 0.5, 0.10, 0.20)
    assert c == pytest.approx(4.76, abs=0.005)


def test_hull_15_6_put_no_dividend():
    """Hull 10e, Chapter 15, Example 15.6.

    Same params as the call test; Hull prints p=0.81.
    """
    p, *_ = black_scholes("p", 42.0, 40.0, 0.5, 0.10, 0.20)
    assert p == pytest.approx(0.81, abs=0.005)


def test_hull_16_delta_gamma_vega():
    """Hull 10e, Chapter 16 (Greeks).

    European call, S=49, K=50, r=5 %, T=20/52 yr, σ=20 %, no dividend.
    Hull's tabulated Greeks: delta≈0.522, gamma≈0.066, vega≈12.1
    (vega quoted per unit vol, i.e. per 1.00 increase in σ).
    """
    _, delta, gamma, _, vega, _ = black_scholes("c", 49.0, 50.0, 20 / 52, 0.05, 0.20)
    assert delta == pytest.approx(0.522, abs=0.005)
    assert gamma == pytest.approx(0.066, abs=0.005)
    assert vega == pytest.approx(12.1, abs=0.1)


def test_hull_17_1_index_call_with_dividend():
    """Hull 10e, Chapter 17 (Options on indices, currencies, futures).

    Index call: S=930, K=900, r=8 %, q=3 %, T=2 months, σ=20 %. Hull prints
    c=51.83. We use ``merton`` (cost-of-carry b=r-q).
    """
    c, *_ = merton("c", 930.0, 900.0, 2 / 12, 0.08, 0.03, 0.20)
    assert c == pytest.approx(51.83, abs=0.01)


def test_hull_17_1_index_put_with_dividend():
    """Hull 10e, Chapter 17 — companion put to Example 17.1.

    Same params as the index call; the corresponding put per put-call parity
    ought to be c - (S e^{-qT} - K e^{-rT}). We assert that parity here as a
    cross-check on the dividend-yield arm of the kernel.
    """
    c, *_ = merton("c", 930.0, 900.0, 2 / 12, 0.08, 0.03, 0.20)
    p, *_ = merton("p", 930.0, 900.0, 2 / 12, 0.08, 0.03, 0.20)
    # Put-call parity with continuous dividend yield:
    #   c - p = S e^{-qT} - K e^{-rT}
    rhs = 930.0 * math.exp(-0.03 * 2 / 12) - 900.0 * math.exp(-0.08 * 2 / 12)
    assert (c - p) == pytest.approx(rhs, abs=1e-6)


def test_hull_18_7_futures_call_black76():
    """Hull 10e, Chapter 18, Example 18.7 (Black-76).

    Futures call: F=20, K=20, r=12 %, T=4 months, σ=25 %. Hull prints c=1.12.
    """
    c, *_ = black_76("c", 20.0, 20.0, 4 / 12, 0.12, 0.25)
    assert c == pytest.approx(1.12, abs=0.02)


def test_hull_18_futures_put_black76_parity():
    """Hull 10e, Chapter 18 — futures put-call parity for Black-76.

    Black-76 parity:  c - p = e^{-rT} (F - K). At F=K this means c == p.
    """
    f = k = 20.0
    r = 0.12
    t = 4 / 12
    sigma = 0.25
    c, *_ = black_76("c", f, k, t, r, sigma)
    p, *_ = black_76("p", f, k, t, r, sigma)
    assert c == pytest.approx(p, abs=1e-6)


def test_european_call_atm_zero_rate_zero_div():
    """Sanity: at-the-money European call with r=0, q=0 has the closed form
    c = S * (2 * Φ(σ√T/2) - 1).

    Take S=K=100, T=1, σ=0.20:  σ√T/2 = 0.10, Φ(0.10) ≈ 0.5398,
    so c ≈ 100 * (2*0.5398 - 1) = 7.97.
    """
    c, *_ = black_scholes("c", 100.0, 100.0, 1.0, 0.0, 0.20)
    assert c == pytest.approx(7.9656, abs=0.005)


def test_call_value_monotonic_in_vol():
    """Vega positive: a higher implied vol must produce a higher call value."""
    base, *_ = black_scholes("c", 100.0, 100.0, 0.5, 0.05, 0.20)
    higher, *_ = black_scholes("c", 100.0, 100.0, 0.5, 0.05, 0.30)
    assert higher > base


def test_call_value_monotonic_in_spot():
    """Delta positive for a call: a higher spot must produce a higher value."""
    low, *_ = black_scholes("c", 95.0, 100.0, 0.5, 0.05, 0.20)
    high, *_ = black_scholes("c", 105.0, 100.0, 0.5, 0.05, 0.20)
    assert high > low


def test_asian_lower_than_vanilla():
    """An average-price Asian call is worth less than the vanilla European
    counterpart with the same params, because averaging reduces effective vol.
    """
    vanilla, *_ = black_76("c", 100.0, 100.0, 1.0, 0.05, 0.30)
    avg_price, *_ = asian_76("c", 100.0, 100.0, 1.0, 0.0, 0.05, 0.30)
    assert avg_price < vanilla


# ---------------------------------------------------------------------------
# 2. Put-call parity (no-dividend equity) at multiple spots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spot", [80.0, 95.0, 100.0, 110.0, 130.0])
def test_put_call_parity_equity(spot):
    """For European options on a non-dividend-paying stock:
        c - p = S - K e^{-rT}
    Verified across a range of spots from deep-ITM to deep-OTM.
    """
    strike = 100.0
    t = 0.5
    r = 0.05
    sigma = 0.25
    c, *_ = black_scholes("c", spot, strike, t, r, sigma)
    p, *_ = black_scholes("p", spot, strike, t, r, sigma)
    rhs = spot - strike * math.exp(-r * t)
    assert (c - p) == pytest.approx(rhs, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. Implied-volatility round-trip
# ---------------------------------------------------------------------------


def test_iv_round_trip_european_call():
    """Price a European call at σ=0.30, recover σ from the price → < 1e-3."""
    spot, strike, t, r, q = 100.0, 100.0, 0.25, 0.05, 0.02
    target_iv = 0.30
    price, *_ = merton("c", spot, strike, t, r, q, target_iv)
    recovered = euro_implied_vol("c", spot, strike, t, r, q, price)
    assert abs(recovered - target_iv) < 1e-3


def test_iv_round_trip_european_put_otm():
    """OTM put at σ=0.30 round-trips cleanly via the Newton–Raphson path."""
    spot, strike, t, r, q = 100.0, 90.0, 0.5, 0.05, 0.0
    target_iv = 0.30
    price, *_ = merton("p", spot, strike, t, r, q, target_iv)
    recovered = euro_implied_vol("p", spot, strike, t, r, q, price)
    assert abs(recovered - target_iv) < 1e-3


def test_iv_round_trip_american_via_wrapper():
    """High-level wrapper round-trip for American options (bisection)."""
    spot, strike, t, r, q = 100.0, 100.0, 0.5, 0.05, 0.05
    target_iv = 0.30
    pricer_value = american_greeks(
        "p", spot=spot, strike=strike, dte_years=t, rfr=r, dividend_yield=q, iv=target_iv
    ).value
    recovered = implied_vol(
        "p",
        spot=spot,
        strike=strike,
        dte_years=t,
        rfr=r,
        dividend_yield=q,
        market_price=pricer_value,
        american=True,
    )
    assert abs(recovered - target_iv) < 1e-3


# ---------------------------------------------------------------------------
# 4. Bjerksund–Stensland: American >= European
# ---------------------------------------------------------------------------


def test_american_call_at_least_european_with_dividend():
    """When q > 0 the American call has positive early-exercise premium."""
    ec, *_ = merton("c", 100.0, 100.0, 1.0, 0.05, 0.05, 0.30)
    ac, *_ = american("c", 100.0, 100.0, 1.0, 0.05, 0.05, 0.30)
    assert ac >= ec - 1e-9
    # When dividend is non-trivial we expect a strict premium:
    assert ac > ec


def test_american_put_at_least_european():
    """American puts always carry an early-exercise premium when r > 0."""
    ep, *_ = merton("p", 100.0, 100.0, 1.0, 0.05, 0.0, 0.30)
    ap, *_ = american("p", 100.0, 100.0, 1.0, 0.05, 0.0, 0.30)
    assert ap >= ep - 1e-9
    assert ap > ep


def test_american_call_no_dividend_equals_european():
    """Without dividends an American call should not be early-exercised, so
    its value collapses to the European call (Hull 15.5 — the intuitive
    no-arbitrage result).
    """
    ec, *_ = merton("c", 100.0, 100.0, 1.0, 0.05, 0.0, 0.30)
    ac, *_ = american("c", 100.0, 100.0, 1.0, 0.05, 0.0, 0.30)
    assert ac == pytest.approx(ec, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Greeks numerical-derivative cross-check
# ---------------------------------------------------------------------------


def test_delta_matches_central_difference():
    """Central-difference approximation:
        delta ≈ (V(S+ε) - V(S-ε)) / (2 ε)
    Bump spot by ε=0.01 and verify the analytic delta matches to 3 dp.
    """
    spot, strike, t, r, q, sigma = 100.0, 100.0, 0.5, 0.05, 0.0, 0.25
    g = european_greeks("c", spot, strike, t, r, q, sigma)
    eps = 0.01
    up, *_ = merton("c", spot + eps, strike, t, r, q, sigma)
    down, *_ = merton("c", spot - eps, strike, t, r, q, sigma)
    numerical_delta = (up - down) / (2 * eps)
    assert g.delta == pytest.approx(numerical_delta, abs=5e-4)


def test_delta_central_difference_put():
    """Same cross-check for a put — delta should be negative and match."""
    spot, strike, t, r, q, sigma = 100.0, 105.0, 0.5, 0.05, 0.0, 0.25
    g = european_greeks("p", spot, strike, t, r, q, sigma)
    eps = 0.01
    up, *_ = merton("p", spot + eps, strike, t, r, q, sigma)
    down, *_ = merton("p", spot - eps, strike, t, r, q, sigma)
    numerical_delta = (up - down) / (2 * eps)
    assert g.delta < 0
    assert g.delta == pytest.approx(numerical_delta, abs=5e-4)


def test_gamma_matches_second_difference():
    """Central second-difference for gamma:
    gamma ≈ (V(S+ε) - 2 V(S) + V(S-ε)) / ε²
    """
    spot, strike, t, r, q, sigma = 100.0, 100.0, 0.5, 0.05, 0.0, 0.25
    g = european_greeks("c", spot, strike, t, r, q, sigma)
    eps = 0.5
    up, *_ = merton("c", spot + eps, strike, t, r, q, sigma)
    mid, *_ = merton("c", spot, strike, t, r, q, sigma)
    down, *_ = merton("c", spot - eps, strike, t, r, q, sigma)
    numerical_gamma = (up - 2 * mid + down) / (eps * eps)
    # Wider tolerance: second-difference is noisier than first-difference.
    assert g.gamma == pytest.approx(numerical_gamma, abs=1e-3)


# ---------------------------------------------------------------------------
# 6. greeks.py wrapper smoke + covered-call yield
# ---------------------------------------------------------------------------


def test_option_greeks_is_frozen_dataclass():
    """The OptionGreeks dataclass must be frozen — risk-gate logic relies on
    this for safe sharing across threads / cached hashing."""
    g = european_greeks("c", 100.0, 100.0, 0.5, 0.05, 0.0, 0.25)
    assert isinstance(g, OptionGreeks)
    with pytest.raises((AttributeError, TypeError)):
        g.delta = 0.99  # type: ignore[misc]


def test_european_greeks_matches_low_level_pricer():
    """The wrapper must not transform values — every field maps 1:1 to
    ``merton``'s tuple ordering."""
    spot, strike, t, r, q, sigma = 100.0, 100.0, 0.5, 0.05, 0.0, 0.25
    raw = merton("c", spot, strike, t, r, q, sigma)
    g = european_greeks("c", spot, strike, t, r, q, sigma)
    assert (g.value, g.delta, g.gamma, g.theta, g.vega, g.rho) == raw


def test_invalid_option_type_raises():
    """Wrapper validation: option_type must be 'c' or 'p'."""
    with pytest.raises(ValueError, match="option_type"):
        european_greeks("call", 100.0, 100.0, 0.5, 0.05, 0.0, 0.25)


def test_covered_call_yield_basic_shape():
    """Yield must be positive, < 1, and increase with vol (vega-positive)."""
    base = covered_call_yield_per_period(
        spot=185.0,
        strike=195.0,
        dte_years=30 / 365,
        rfr=0.045,
        dividend_yield=0.005,
        iv=0.25,
    )
    high_iv = covered_call_yield_per_period(
        spot=185.0,
        strike=195.0,
        dte_years=30 / 365,
        rfr=0.045,
        dividend_yield=0.005,
        iv=0.50,
    )
    assert 0.0 < base < 1.0
    assert high_iv > base


def test_covered_call_yield_socalminh_threshold():
    """A spike-vol regime (σ=1.20, deep meme stock) crosses the 10 %/month
    threshold from ADR-0030 / socalminh's covered-call screener.
    """
    spike = covered_call_yield_per_period(
        spot=20.0,
        strike=22.0,
        dte_years=30 / 365,
        rfr=0.045,
        dividend_yield=0.0,
        iv=1.20,
    )
    assert spike >= 0.10
