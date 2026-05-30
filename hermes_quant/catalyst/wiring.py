"""hermes_quant.catalyst.wiring — the SINGLE catalyst -> advisor wiring seam (C2-2).

There are three live decision paths (daily-interim, autonomous-tick, playbook-tick).
Each must inject lookahead-honest semantic packets into the advisor's ``market_extras``
so that flipping ``HERMES_QUANT_SEMANTIC_ENABLED=1`` actually takes effect on EVERY
path — not just the daily-interim brief, which was the only wired path before this
module existed (gap G3). Rather than copy-paste the try/except packet-loading block
into two more scripts, all three call :func:`semantic_market_extras`.

This mirrors the "ONLY coupling point to the advisor" comment at
``synthesize.py:176`` (``load_packets_for``): one lookahead-honest packet-injection
seam, silence-by-default on every error path (returns ``None``, never raises).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any


def semantic_market_extras(
    symbol: str,
    *,
    decision_asof: datetime | None = None,
    horizon: str = "1d",
    base_extras: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return market_extras carrying lookahead-honest semantic packets for ``symbol``,
    or ``None`` when semantic is OFF / no packets / any error (silence-by-default).

    This is the SINGLE catalyst->advisor wiring seam. All live decision paths
    (daily-interim, autonomous-tick, playbook-tick) call this so flipping
    ``HERMES_QUANT_SEMANTIC_ENABLED=1`` takes effect on EVERY path, not just one.

    ``decision_asof`` defaults to wall-clock now (live path): packets validate against
    decision time, not the stale last-daily-bar close (ADR-0068/0074). Pass an
    explicit asof for backtests so the strict bar-time clamp holds.
    """
    if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") != "1":
        return None
    try:
        from hermes_quant.catalyst.synthesize import load_packets_for

        asof = decision_asof or datetime.now(UTC)
        packets = load_packets_for(symbol, asof, horizon=horizon)
        if not packets:
            return None
        out = dict(base_extras or {})
        out["semantic_packets"] = packets
        out["decision_asof"] = asof.isoformat()
        return out
    except Exception:  # noqa: BLE001 — never block a recommend on packet loading
        return None
