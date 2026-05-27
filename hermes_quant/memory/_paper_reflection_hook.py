"""hermes_quant.memory._paper_reflection_hook — internal helper for PaperReactor.

Called only when HERMES_QUANT_REFLECTION=1. Triggers the Reflector when a
position is fully closed by a paper fill.  Default OFF — importing this
module has no effect; only ``maybe_reflect_on_close`` has side-effects.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def maybe_reflect_on_close(record: Any, proposal: Any) -> None:
    """Trigger a Reflection when a paper fill closes a position.

    Detection heuristic: the fill_size_pct returned in the ExecutionRecord
    has the opposite sign to the last entry fill for the same asset tracked
    in the decision log.  This is a best-effort heuristic; the authoritative
    close signal is daemon/settlement_loop.py.

    Parameters
    ----------
    record:
        The ExecutionRecord just written to the bus.
    proposal:
        The Proposal object that produced the record (carries advisor_result).
    """
    try:
        from hermes_quant.memory.decisions import DecisionLog
        from hermes_quant.memory.reflector import Reflector

        asset = getattr(record, "asset", None) or ""
        fill_size = float(getattr(record, "fill_size_pct", 0) or 0)
        decision_price = float(getattr(record, "decision_price", 0) or 0)
        fill_price = float(getattr(record, "fill_price", 0) or 0)
        asof_execution = str(getattr(record, "asof_execution", "") or "")

        dlog = DecisionLog()
        # Find the most recent pending decision for this asset
        pending = [
            row for row in dlog.read_pending()
            if str(row.get("ticker", "")).upper() == asset.upper()
        ]
        if not pending:
            logger.debug("reflection-hook: no pending decision found for %s; skipping", asset)
            return

        # Use the most recent pending decision
        pending.sort(key=lambda r: str(r.get("asof_decision", "")), reverse=True)
        decision = pending[0]

        # Heuristic close detection: fill is in the opposite direction to open
        decision_direction = int(decision.get("direction", 0))
        if decision_direction == 0:
            logger.debug("reflection-hook: direction=0 decision; skipping")
            return
        fill_is_close = (decision_direction > 0 and fill_size < 0) or (
            decision_direction < 0 and fill_size > 0
        )
        if not fill_is_close:
            logger.debug(
                "reflection-hook: fill (size=%+.4f) doesn't look like a close for "
                "direction=%d; skipping",
                fill_size,
                decision_direction,
            )
            return

        # Build a minimal exit record
        entry_price = float(decision_price or fill_price or 1.0)
        exit_record = {
            "asof_resolution": asof_execution,
            "entry_price": entry_price,
            "exit_price": fill_price,
            "benchmark_return": 0.0,  # no live benchmark fetch in hook; upgrading in Wave 5
        }

        reflector = Reflector()
        reflection = reflector.reflect_on_close(decision, exit_record)
        logger.info(
            "reflection-hook: reflected %s → %s",
            decision["decision_id"],
            reflection.reflection_id,
        )

        dlog.record_resolution(decision["decision_id"], reflection.reflection_id)

    except Exception:
        logger.exception("reflection-hook: unexpected error (non-blocking)")
