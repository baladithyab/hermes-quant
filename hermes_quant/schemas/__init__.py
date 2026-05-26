"""hermes_quant.schemas — Typed Pydantic schemas for daemon-internal state.

Per ADR-0038 §D.2, these models are the **internal** state representation
between pipeline stages (analysts → aggregator → risk-gate → emit). They
do NOT replace the existing Protocol dataclasses (MarketContext,
AnalystView, AggregatedSignal, Action) which remain the public contract.

Public re-exports.
"""

from __future__ import annotations

from hermes_quant.schemas.bar_snapshot import (
    AggregatedSignalSlot,
    AnalystViewSlot,
    BarSnapshot,
    FinalDecisionSlot,
    HaltSummary,
    IndicatorsSlot,
    MetaSlot,
    OHLCVSlot,
    RiskCheckSlot,
    SymbolStatus,
)

__all__ = [
    "AggregatedSignalSlot",
    "AnalystViewSlot",
    "BarSnapshot",
    "FinalDecisionSlot",
    "HaltSummary",
    "IndicatorsSlot",
    "MetaSlot",
    "OHLCVSlot",
    "RiskCheckSlot",
    "SymbolStatus",
]
