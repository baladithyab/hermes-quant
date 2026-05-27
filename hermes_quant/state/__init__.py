"""hermes_quant.state — Durable portfolio state (ADR-0039 wave 1c).

The state package provides:
  - PortfolioState: materialized-view projection of executions.jsonl
    into state.db positions + cash tables.
  - Position / CashState: typed read-views over those tables.

Design: state.db is a *derived cache* of executions.jsonl.
  - Never edit executions.jsonl.
  - rebuild_state() (or PortfolioState.reconstruct_from) is idempotent.
  - The executions_replayed watermark table makes subsequent replays O(delta).
"""

from __future__ import annotations

from .portfolio_state import PortfolioState, ReconstructionResult
from .positions import CashState, Position

__all__ = [
    "CashState",
    "PortfolioState",
    "Position",
    "ReconstructionResult",
]
