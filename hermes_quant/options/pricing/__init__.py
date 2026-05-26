"""Closed-form option pricing kernel — public surface for hermes-quant.

Vendored from optlib (https://github.com/dbrojas/optlib, MIT license).
See ``LICENSE.optlib`` and ``README.md`` in this directory for provenance.

Pricers (8) — all return ``(value, delta, gamma, theta, vega, rho)``:
    black_scholes(option_type, fs, x, t, r, v)            European, no dividend
    merton(option_type, fs, x, t, r, q, v)                European + cont. div yield
    black_76(option_type, fs, x, t, r, v)                 Forward / futures
    garman_kohlhagen(option_type, fs, x, t, r, rf, v)     FX
    asian_76(option_type, fs, x, t, t_a, r, v)            Average-price commodity
    kirks_76(option_type, f1, f2, x, t, r, v1, v2, corr)  Spread option (Kirk's)
    american(option_type, fs, x, t, r, q, v)              Bjerksund–Stensland 2002
    american_76(option_type, fs, x, t, r, v)              American on futures

Implied-volatility solvers (4):
    euro_implied_vol(option_type, fs, x, t, r, q, cp)     Newton–Raphson
    euro_implied_vol_76(option_type, fs, x, t, r, cp)
    amer_implied_vol(option_type, fs, x, t, r, q, cp)     Bisection
    amer_implied_vol_76(option_type, fs, x, t, r, cp)

Convention: ``option_type`` is ``"c"`` (call) or ``"p"`` (put). Time ``t`` is
in years (1.0 = 1y; 30/365 ≈ 0.0822 for monthly). Rates and vols are decimals.

For a higher-level dataclass-friendly API (``OptionGreeks``, multi-leg helpers),
see :mod:`hermes_quant.options.greeks`.
"""

from .gbs import (
    GBS_CalculationError,
    GBS_InputError,
    amer_implied_vol,
    amer_implied_vol_76,
    american,
    american_76,
    asian_76,
    black_76,
    black_scholes,
    euro_implied_vol,
    euro_implied_vol_76,
    garman_kohlhagen,
    kirks_76,
    merton,
)

__all__ = [
    # Pricers
    "black_scholes",
    "merton",
    "black_76",
    "garman_kohlhagen",
    "asian_76",
    "kirks_76",
    "american",
    "american_76",
    # IV solvers
    "euro_implied_vol",
    "euro_implied_vol_76",
    "amer_implied_vol",
    "amer_implied_vol_76",
    # Exceptions
    "GBS_InputError",
    "GBS_CalculationError",
]
