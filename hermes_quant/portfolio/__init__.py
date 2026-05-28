"""hermes_quant.portfolio — portfolio-state reconstruction (ADR-0071 side-product).

Until ADR-0035 wave-4 lands a queryable `state.db.positions` table, the canonical
source of truth for "what positions am I holding" is `executions.jsonl`. This
module provides a helper that walks the executions log and reconstructs a
PortfolioState snapshot at a given point in time.

PaperReactor semantics: each fill writes a `target_position_pct` that is the
NEW intended position size for that symbol, NOT a delta. So the current position
for a symbol is simply the LATEST `target_position_pct` from `executions.jsonl`
filtered by symbol. (When ADR-0029 multi-leg arrives, this changes.)
"""

from __future__ import annotations
