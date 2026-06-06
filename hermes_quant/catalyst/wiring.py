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

This module also hosts the sibling :func:`calendar_market_extras` seam (ADR-0084):
the SINGLE scheduled-event (calendar) -> advisor wiring point. It is gated by the
NEW default-OFF flag ``HERMES_QUANT_CALENDAR_ENABLED`` and is asof-honest — it
NEVER surfaces an event before its schedule was public, and NEVER an outcome.
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
    if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "1") != "1":
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


def _load_seed_events(timeout: float | None = None) -> list[Any]:
    """Default calendar loader: the vendored no-key FOMC seed (ADR-0084 Option D).

    Delegates to :func:`hermes_quant.catalyst.calendar.load_fomc_seed`, the SINGLE
    canonical seed parser (so this seam never schema-drifts from the seed file). It
    is silence-by-default already (a missing/malformed seed returns ``[]``, never
    raises), reading ``catalyst/fomc_calendar.seed.yaml`` with ZERO network. Returns
    a list of :class:`~hermes_quant.catalyst.calendar.CalendarEvent`. The ``timeout``
    arg exists only for signature-parity with an injected ``ingest_*`` fetcher.
    """
    from hermes_quant.catalyst.calendar import load_fomc_seed

    return load_fomc_seed()


def calendar_market_extras(
    symbol: str,
    *,
    decision_asof: datetime | None = None,
    base_extras: dict[str, Any] | None = None,
    events_loader: Any = None,
) -> dict[str, Any] | None:
    """Return market_extras carrying asof-honest, outcome-free scheduled-event risk
    for ``symbol``, or ``None`` when calendar is OFF / no visible events / any error
    (silence-by-default). Sibling of :func:`semantic_market_extras` (ADR-0084).

    Gated by the NEW default-OFF flag ``HERMES_QUANT_CALENDAR_ENABLED``: absent/``0``
    => ``None`` => the adapter stamps NOTHING => the default path is byte-identical.

    asof-honest by construction (ADR-0084 D-2): events are filtered to
    ``announced_at <= decision_asof`` via :func:`calendar.visible_at` — the consumer
    may not even know an event EXISTS before its schedule became public, so a
    future-announced event is EXCLUDED (no lookahead). The exposed payload carries
    ONLY ``scheduled_for`` + ``kind`` + ``impact`` (+ provenance ``title``/``source``)
    — NEVER an ``outcome`` (the calendar is outcome-free; an outcome arrives via the
    catalyst/evidence layers AFTER ``scheduled_for``).

    ``decision_asof`` defaults to wall-clock now (live path). Pass an explicit asof
    for backtests so the asof gate holds against decision time, not wall clock.
    ``events_loader`` is injectable (``loader(timeout) -> Iterable[CalendarEvent]``)
    for offline tests; it defaults to the vendored-seed loader.

    The returned mapping is the analysts' READ surface (``ctx.extras['event_risk']``);
    bull/bear/judge MAY weigh it. It carries NO authority — the deterministic gate
    remains the final, immutable backstop (ADR-0004/ADR-0084 D-1/D-5).
    """
    if os.environ.get("HERMES_QUANT_CALENDAR_ENABLED", "0") != "1":
        return None
    try:
        from hermes_quant.catalyst.calendar import visible_at

        asof = decision_asof or datetime.now(UTC)
        loader = events_loader or _load_seed_events
        events = list(loader(None) or [])
        # asof gate: drop any event whose SCHEDULE was not yet public at asof
        # (announced_at > asof). This is the no-lookahead guarantee.
        visible = visible_at(events, asof)
        # Scope: macro events (no symbol) apply to every symbol; single-name events
        # apply only to their own symbol. Match case-insensitively.
        sym_u = (symbol or "").strip().upper()
        scoped = [
            e for e in visible
            if e.symbol is None or (e.symbol or "").strip().upper() == sym_u
        ]
        if not scoped:
            return None
        # Forward, deterministic, outcome-free projection. Sort by (scheduled_for,
        # kind) so the emitted list is stable regardless of loader/seed ordering.
        scoped.sort(key=lambda e: (e.scheduled_for, e.kind))
        emitted = [
            {
                "kind": e.kind,
                "scheduled_for": e.scheduled_for.isoformat(),
                "impact": e.impact,
                "title": e.title,
                "source": e.source,
            }
            for e in scoped
        ]
        out = dict(base_extras or {})
        out["event_risk"] = {
            "decision_asof": asof.isoformat(),
            "events": emitted,
        }
        return out
    except Exception:  # noqa: BLE001 — never block a recommend on calendar loading
        return None
