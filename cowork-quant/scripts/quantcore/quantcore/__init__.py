"""quantcore — deterministic core of cowork-quant.

Everything money-adjacent lives here: risk gate, Kelly sizing, discrete
ladder, paper ledger, settlement. Claude proposes; this package disposes.

Ported (lean) from hermes-quant v0.6.4:
  - risk/kelly.py        -> quantcore.kelly   (verbatim math)
  - risk/gate.py         -> quantcore.gate    (rules 0-7, datetime-native)
  - ADR-0004/0009/0084 semantics preserved; see AGENTS.md rails.

No torch, no pandas. stdlib + pydantic only.
"""

__version__ = "0.1.0"

from quantcore.config import PROFILES, RiskConfig
from quantcore.gate import GateDecision, RiskGate
from quantcore.schemas import (
    AnalystView,
    CommitteeSignal,
    Fill,
    MarketCosts,
    PortfolioState,
    Proposal,
)

__all__ = [
    "PROFILES",
    "AnalystView",
    "CommitteeSignal",
    "Fill",
    "GateDecision",
    "MarketCosts",
    "PortfolioState",
    "Proposal",
    "RiskConfig",
    "RiskGate",
]
