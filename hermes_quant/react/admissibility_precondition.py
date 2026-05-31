"""hermes_quant.react.admissibility_precondition — shared ADR-0077/0079 equity-leg seam.

Extracts the core of ``PaperReactor._admissibility_reject`` (the ``admit_or_reject``
call + the no-fill ``ExecutionRecord`` construction) into a module-level helper so
BOTH ``PaperReactor`` and ``MultiLegPaperReactor`` call the SAME pre-trade
admissibility precondition for a SHORT equity leg — without ``multileg.py`` importing
``paper.py``.

Rails (identical to ADR-0077, inherited from ``admissibility.gate_order.admit_or_reject``):
  * REJECT-only / fail-closed. It can ONLY refuse to fill a SHORT equity order; it
    never amplifies, widens, forces, or flips a side.
  * DEFAULT-OFF behind ``HERMES_QUANT_ADMISSIBILITY=1``. With the flag absent this is
    a bit-for-bit no-op (returns None without consulting the oracle / NAV).
  * Constrains opening SHORT EQUITY only. Longs, flats, and non-equity legs are out
    of scope (the CC's +100-long equity leg is admissible by construction; the guard
    exists so a future short-stock collar leg is covered — plan §2.4 step 4).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .base import ExecutionRecord

logger = logging.getLogger(__name__)


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC string to an aware datetime, or None (fail-closed)."""
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def admissibility_reject_equity(
    *,
    symbol: str,
    asset_class: str,
    fill_size_pct: float,
    decision_price: float,
    nav_provider: Callable[[], float | None],
    asof_decision: str,
    asof_execution: str,
    reactor_name: str,
    proposal_id: str,
    signal_id: str | None,
    timeframe: str,
    bar_ts: str | None,
    approver_user_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> ExecutionRecord | None:
    """Pre-trade admissibility precondition for a SHORT equity leg (ADR-0077/0079).

    Returns:
        None  => proceed (flag OFF, long/flat order, non-equity, or ADMITTED).
                 With ``HERMES_QUANT_ADMISSIBILITY`` unset this ALWAYS returns None
                 and never touches the oracle / NAV — bit-for-bit pre-ADR-0077.
        ExecutionRecord (fill_size_pct=0.0, NOT written to the bus by this helper)
                 => the short was inadmissible; the caller records the rejection in
                 the audit trail and skips the fill.
    """
    if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") != "1":
        return None
    # Admissibility constrains opening SHORT EQUITY only.
    if fill_size_pct >= 0 or asset_class != "equity":
        return None

    from hermes_quant.admissibility import admit_or_reject

    # NAV is resolved lazily, ONLY after the flag + side/asset-class short-circuit
    # above (callers assert the NAV lookup never runs for flag-off / long / non-equity
    # orders — keep the seam cheap, matching the pre-refactor PaperReactor behavior).
    nav = nav_provider()
    asof_dt = _parse_utc(asof_decision) or datetime.now(tz=UTC)
    price = decision_price if decision_price > 0 else None

    # For the paper account, equity == NAV (`equity_total`), so plumb it as
    # `account_equity` to clear the live oracle's < $2,000 floor (step 5). `available_bp`
    # is left None (not tracked in the materialized paper state — needs a live broker
    # fetch), so a short still fails-closed on the BP hard check (step 8b): documented
    # gap, never a fabricated pass. Bit-for-bit no-op when the flag is OFF (short-circuited above).
    verdict = admit_or_reject(
        symbol, "short", fill_size_pct, nav, price, asof_dt, account_equity=nav
    )
    if verdict.admitted:
        return None

    logger.warning(
        "%s: ADMISSIBILITY REJECT %s asset=%s target=%+.4f state=%s reason=%s "
        "qty_shares=%d — NO FILL written",
        reactor_name,
        proposal_id,
        symbol,
        fill_size_pct,
        verdict.state.value,
        verdict.reason,
        verdict.qty_shares,
    )
    metadata: dict[str, Any] = {
        "paper": True,
        "admissibility_rejected": True,
        "admissibility_state": verdict.state.value,
        "admissibility_reason": verdict.reason,
        "admissibility_qty_shares": verdict.qty_shares,
        "requested_target_pct": fill_size_pct,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return ExecutionRecord(
        proposal_id=proposal_id,
        signal_id=signal_id,
        asset=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        asof_decision=asof_decision,
        asof_execution=asof_execution,
        target_position_pct=fill_size_pct,
        decision_price=decision_price,
        fill_price=0.0,
        fill_size_pct=0.0,
        reactor_name=reactor_name,
        human_in_the_loop=True,
        approver_user_id=approver_user_id,
        reactor_metadata=metadata,
        bar_ts=bar_ts,
    )
