# `hermes_quant.options.pricing`

Closed-form option pricing kernel — vendored from
[optlib](https://github.com/dbrojas/optlib).

## Provenance

| | |
|---|---|
| Original code | Davis W. Edwards, 2017. Companion to *Energy Trading and Investing*, *Risk Management in Trading*, *Energy Investing Demystified*. |
| Packaged as `optlib` | Daniel Rojas, 2020–2021. |
| Upstream repo | https://github.com/dbrojas/optlib |
| Last upstream commit | 2022-11-18 (effectively unmaintained). |
| License | MIT — see [`LICENSE.optlib`](LICENSE.optlib). |
| Vendored | hermes-quant 2026-05-26. |

We vendor (rather than depend) because optlib is a single-file pricing kernel
with no maintainer; pinning a Git URL leaves us exposed if the upstream repo
disappears. The vendored copy is *byte-identical* in algorithm, ruff-cleaned
to repo style, with `from __future__` and verbose debug logs trimmed. Every
change is annotated in the header of `gbs.py`.

The TDAmeritrade-facing modules from upstream (`api.py`, `instruments.py`)
are **not** vendored — Schwab sunset the TDAmeritrade API in May 2024.

## Public surface

All pricers return a 6-tuple `(value, delta, gamma, theta, vega, rho)`.

### Pricers

| Function | Use for | Distinguishing arg |
|---|---|---|
| `black_scholes(option_type, fs, x, t, r, v)` | European, no dividend | — |
| `merton(option_type, fs, x, t, r, q, v)` | European + continuous dividend yield | `q` |
| `black_76(option_type, fs, x, t, r, v)` | Forward / futures | `b = 0` |
| `garman_kohlhagen(option_type, fs, x, t, r, rf, v)` | FX | `rf` (foreign rate) |
| `asian_76(option_type, fs, x, t, t_a, r, v)` | Average-price commodity | `t_a` (avg start) |
| `kirks_76(option_type, f1, f2, x, t, r, v1, v2, corr)` | Spread option | two underlyings |
| `american(option_type, fs, x, t, r, q, v)` | American (Bjerksund–Stensland 2002) | `q` |
| `american_76(option_type, fs, x, t, r, v)` | American on futures | `b = 0` |

### Implied-vol solvers

| Function | Solver |
|---|---|
| `euro_implied_vol(option_type, fs, x, t, r, q, cp)` | Newton–Raphson → bisection fallback |
| `euro_implied_vol_76(option_type, fs, x, t, r, cp)` | same |
| `amer_implied_vol(option_type, fs, x, t, r, q, cp)` | Bisection (vega unreliable for Americans) |
| `amer_implied_vol_76(option_type, fs, x, t, r, cp)` | same |

### Argument conventions

- `option_type`: `"c"` for call, `"p"` for put.
- Time `t`, `t_a` is in years. For days-to-expiry: `t = dte_days / 365.0`.
- Rates and vols are decimals (`0.05` = 5 %).
- `q` = continuous dividend yield.
- `cp` = observed market premium (call or put).

## Higher-level wrapper

For most uses prefer
[`hermes_quant.options.greeks`](../greeks.py), which returns an
`OptionGreeks` dataclass instead of a 6-tuple and adds covered-call
yield helpers.

## Worked example: covered-call yield for AAPL

socalminh's covered-call methodology (ADR-0030) writes a 30-day OTM call
each month and harvests the premium. The headline screening number is
`monthly_premium_yield = call_premium / spot`. Computed inline:

```python
from hermes_quant.options.greeks import (
    OptionGreeks,
    european_greeks,
    covered_call_yield_per_period,
)

# Hypothetical AAPL snapshot — replace with live quote in production:
spot          = 185.00      # AAPL spot
strike        = 195.00      # ~5 % OTM
dte_years     = 30 / 365    # ~30 calendar days
rfr           = 0.045       # 3-month T-bill
dividend_yield = 0.005      # AAPL ~0.5 %
iv            = 0.25        # observed/forecast IV

# 1. Greeks for the short-call leg.
short_call: OptionGreeks = european_greeks(
    option_type="c",
    spot=spot,
    strike=strike,
    dte_years=dte_years,
    rfr=rfr,
    dividend_yield=dividend_yield,
    iv=iv,
)
# short_call.delta is positive (long-call delta); the *position* delta is -delta
# because we wrote (sold) the call.
position_delta = 1.0 - short_call.delta   # 1 share long + short call

# 2. Per-period premium yield — feeds ADR-0030's daily picker:
yield_per_period = covered_call_yield_per_period(
    spot=spot, strike=strike, dte_years=dte_years,
    rfr=rfr, dividend_yield=dividend_yield, iv=iv,
)
passes_socalminh_rule = yield_per_period >= 0.10   # "10 % / month"

# 3. Net Greeks across the covered-call (1 long share + 1 short call):
net_delta = 1.0 - short_call.delta
net_gamma = -short_call.gamma
net_theta = -short_call.theta   # we earn theta as the writer
net_vega  = -short_call.vega    # we are short vol
```

This is the building block for ADR-0027 (options-aware risk gate, net-Greeks
aggregation across legs) and ADR-0029 (multi-leg paper reactor).

## When *not* to use this kernel

- **Exotic options with path-dependence** (barriers, lookbacks): not
  implemented. Use a Monte-Carlo or PDE engine.
- **Vol surface fitting**: this prices off a single IV input; it is not a
  surface model.
- **Sub-second hot paths in C-extensions**: every call goes through Python +
  scipy.stats. For a tick loop pricing thousands of strikes in a millisecond,
  vectorise with `scipy.stats.norm.cdf` directly.

## License

This package is dual-licensed:

- The vendored `gbs.py` retains its upstream **MIT** license — see
  [`LICENSE.optlib`](LICENSE.optlib).
- The hermes-quant wrappers (`__init__.py`, `greeks.py`, this README) are
  **Apache-2.0** to match the rest of hermes-quant.
