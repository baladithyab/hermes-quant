"""hermes_quant.options — option pricing, Greeks, and (TODO) risk-gate helpers.

The closed-form pricing kernel lives under :mod:`hermes_quant.options.pricing`
(vendored from optlib, MIT license). The ergonomic wrappers live in
:mod:`hermes_quant.options.greeks` — start there for typical use.
"""

from .greeks import (
    OptionGreeks,
    american_greeks,
    covered_call_yield_per_period,
    european_greeks,
    implied_vol,
)

__all__ = [
    "OptionGreeks",
    "european_greeks",
    "american_greeks",
    "implied_vol",
    "covered_call_yield_per_period",
]
