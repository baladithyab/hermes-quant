"""hermes_quant.memory._paper_reflection_hook — internal helper for PaperReactor.

Called only when HERMES_QUANT_REFLECTION=1. Two symmetric side-effects, both
default-OFF (importing this module does nothing on its own):

  * ``maybe_record_decision_on_open`` — writes the ``pending`` decision row when
    an OPENING fill is recorded. This is the W1 keystone (capability-map O1): the
    reflection chain's required input was never produced in production, so the one
    closed feedback edge (reflection→retriever→PM prompt) was dark. This open-side
    recorder ignites it. Without it, ``read_pending()`` is always empty and
    ``maybe_reflect_on_close`` below finds nothing to resolve.
  * ``maybe_reflect_on_close`` — resolves that pending row + triggers the Reflector
    when a position is closed by a paper fill.

Together they form the per-trade self-improvement loop: open → pending decision →
close → reflection → (retriever, Oracle-guarded) → PM prompt on a future tick.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def maybe_record_decision_on_open(record: Any, proposal: Any) -> None:
    """Write a ``pending`` decision row when an OPENING paper fill is recorded.

    W1 (capability-map O1): ``DecisionLog.record_decision`` had ZERO production
    callers, so the reflection loop was starved at the source. This records the
    decision the reactor just acted on — keyed to the asset + direction the
    close-side hook looks for — so the loop can close on a later closing fill.

    Idempotent + best-effort: skips closing fills (direction inferred from
    fill sign vs any existing pending decision), skips direction=0, and never
    raises (reflection plumbing must never break a fill). Default-OFF: only
    invoked from the reactor under HERMES_QUANT_REFLECTION=1.

    Parameters
    ----------
    record:
        The ExecutionRecord just written to the bus.
    proposal:
        The Proposal object that produced the record (carries advisor_result).
    """
    try:
        from hermes_quant.memory.decisions import DecisionLog

        asset = (getattr(record, "asset", None) or "").upper()
        if not asset:
            return
        fill_size = float(getattr(record, "fill_size_pct", 0) or 0)
        if fill_size == 0.0:
            return  # admissibility/zero-fill — nothing was opened
        direction = 1 if fill_size > 0 else -1

        adv = (getattr(proposal, "advisor_result", None) or {})
        agg = adv.get("aggregated_signal") or {}
        rg = adv.get("risk_gate") or {}

        dlog = DecisionLog()

        # If the most-recent pending decision for this asset is in the OPPOSITE
        # direction, this fill is a CLOSE of it — let maybe_reflect_on_close handle
        # that; do NOT open a new pending decision for a closing fill.
        existing = [
            row for row in dlog.read_pending()
            if str(row.get("ticker", "")).upper() == asset
        ]
        if existing:
            existing.sort(key=lambda r: str(r.get("asof_decision", "")), reverse=True)
            open_dir = int(existing[0].get("direction", 0))
            if open_dir != 0 and open_dir != direction:
                logger.debug(
                    "decision-open-hook: fill (%+.4f) closes the open %s decision; "
                    "deferring to the close hook", fill_size, asset,
                )
                return

        asof = (
            adv.get("decision_wall_clock")
            or adv.get("as_of")
            or str(getattr(record, "asof_decision", "") or "")
            or str(getattr(record, "asof_execution", "") or "")
        )
        dec_id = dlog.record_decision(
            asof_decision=asof,
            ticker=asset,
            asset_class=str(getattr(record, "asset_class", "") or adv.get("asset_class", "equity")),
            rating=str(rg.get("recommended_action") or agg.get("rating") or "fired"),
            direction=direction,
            confidence=float(agg.get("confidence", 0.0) or 0.0),
            target_position_pct=fill_size,
            thesis_summary=str(
                agg.get("rationale")
                or adv.get("thesis_summary")
                or f"{asset} {'long' if direction > 0 else 'short'} paper fill"
            )[:500],
            thesis_evidence_ids=adv.get("evidence_ids") or None,
            signal_provenance=agg.get("provenance") or rg.get("provenance") or None,
            risk_debate_summary=adv.get("risk_debate_summary"),
        )
        logger.info("decision-open-hook: recorded pending decision %s for %s", dec_id, asset)

    except Exception:
        logger.exception("decision-open-hook: unexpected error (non-blocking)")


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
