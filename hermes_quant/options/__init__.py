"""hermes_quant.options — option pricing, Greeks, and (TODO) risk-gate helpers.

The closed-form pricing kernel lives under :mod:`hermes_quant.options.pricing`
(vendored from optlib, MIT license). The ergonomic wrappers live in
:mod:`hermes_quant.options.greeks` — start there for typical use.
"""

from .data import (
    ChainSnapshotReader,
    NetGreeks,
    OptionChain,
    OptionGreeksSnapshot,
    OptionLeg,
    OptionSnapshot,
    StockLeg,
    aggregate_net_greeks,
)
from .greeks import (
    OptionGreeks,
    american_greeks,
    covered_call_yield_per_period,
    european_greeks,
    implied_vol,
)
from .multileg import MultiLegProposal
from .occ import (
    OccComponents,
    OccParseError,
    format_occ,
    parse_occ,
)
from .structure_select import (
    Direction,
    IVRegime,
    classify_iv_regime,
    direction_from_rating,
    select_structure,
    select_structure_for_plan,
    structure_select_enabled,
)

__all__ = [
    # pricing / greeks (optlib facade)
    "OptionGreeks",
    "european_greeks",
    "american_greeks",
    "implied_vol",
    "covered_call_yield_per_period",
    # OCC-21 (ADR-0029 D1)
    "format_occ",
    "parse_occ",
    "OccComponents",
    "OccParseError",
    # data layer (ADR-0028)
    "OptionLeg",
    "StockLeg",
    "NetGreeks",
    "OptionGreeksSnapshot",
    "OptionSnapshot",
    "OptionChain",
    "ChainSnapshotReader",
    "aggregate_net_greeks",
    # multi-leg proposal carrier (ADR-0029 D5)
    "MultiLegProposal",
    # deterministic structure-selection table (ADR-0082 Part B)
    "Direction",
    "IVRegime",
    "classify_iv_regime",
    "direction_from_rating",
    "select_structure",
    "select_structure_for_plan",
    "structure_select_enabled",
]
