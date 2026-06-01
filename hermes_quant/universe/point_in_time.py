"""Point-in-time (survivorship-bias-safe) universe constructor — B36.

Backtests and as-of perception that draw their candidate symbols from the
*currently* listed universe are survivorship-biased: a 2019 backtest run today
would silently exclude every name that has since delisted (bankruptcies,
buyouts, going-private) and silently *include* names that hadn't IPO'd yet.
The honest universe at a date ``as_of`` is **listed-at-asof**: symbols that had
listed on/before ``as_of`` and had not yet delisted as of that date.

This module implements that filter against an *injectable listing-date table*.

Why injectable (and not a baked-in fetch)? Research finding: there is no free,
license-clean, point-in-time US listing/delisting source that we can ship.
(CRSP is the gold standard but paywalled; SEC/Nasdaq/NYSE feeds are
current-state only and don't preserve historical delistings cleanly.) So the
*code* is mechanical and ready, but the *data* is the gate. The constructor
takes the listing table as a parameter so it is fully testable today and will
work unchanged the moment a real PIT table lands at the documented seam.

Posture (RAILS):
  * **Default-OFF.** Gated behind ``HERMES_QUANT_PIT_UNIVERSE=1``, read at call
    time. When OFF, ``filter_listed_at_asof`` returns the input symbols
    UNCHANGED (order preserved) — the existing/default code path is
    byte-identical. No survivorship claim is made.
  * **Fail-open-to-current-behavior, never silently-safe.** If the flag is ON
    but no listing table is supplied (or it is empty), we DO NOT silently
    pretend the universe is survivorship-safe. We return the input unchanged
    and log a WARNING that the result is NOT point-in-time. The one and only
    way to get a survivorship-filtered universe is: flag ON *and* a non-empty
    listing table present.
  * Read-only. Touches no risk gate, no sizing ladder, no kill-switch.
  * As-of-honest / no-lookahead: a symbol is admitted iff it was tradable on
    ``as_of`` per the listing table.

The constructor is deterministic and pure given its inputs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default-OFF flag, read at call time (per RAILS). "1" enables PIT filtering.
_FLAG = "HERMES_QUANT_PIT_UNIVERSE"


def _pit_enabled() -> bool:
    return os.environ.get(_FLAG, "0") == "1"


@dataclass(frozen=True)
class ListingRecord:
    """Listing lifecycle for one symbol.

    ``listed_at`` is the first date the symbol traded on a primary exchange.
    ``delisted_at`` is the date it stopped trading (None == still listed).
    Both are inclusive of the boundary day as a *listed* day: a symbol that
    delisted on D is considered tradable on D-1 and earlier, not on D. (The
    convention "delisted strictly after as_of stays in" matches a same-day
    delist removing the name — see ``_is_listed_at``.)
    """

    symbol: str
    listed_at: date
    delisted_at: date | None = None


# A listing table maps SYMBOL -> ListingRecord (or a (listed, delisted) pair, or
# a {"listed_at":..., "delisted_at":...} mapping). All three shapes are accepted
# so callers can hand us whatever a future PIT source decodes into.
ListingTable = Mapping[str, "ListingRecord | tuple[Any, Any] | Mapping[str, Any]"]


def _coerce_date(value: Any) -> date | None:
    """Best-effort coerce a value to a ``date``. None/empty -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # Accept ISO date or ISO datetime; take the date component.
        text = value.strip()
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(text).date()
            except ValueError:
                logger.warning("pit_universe: unparseable date %r — treated as missing", value)
                return None
    logger.warning("pit_universe: unrecognized date type %r — treated as missing", type(value))
    return None


def _coerce_record(symbol: str, raw: Any) -> ListingRecord | None:
    """Normalize one listing-table entry into a ``ListingRecord``.

    Returns None (and logs) if there is no usable ``listed_at`` — a symbol with
    no known listing date cannot be point-in-time filtered.
    """
    if isinstance(raw, ListingRecord):
        return raw

    listed_raw: Any = None
    delisted_raw: Any = None
    if isinstance(raw, Mapping):
        listed_raw = raw.get("listed_at", raw.get("listed"))
        delisted_raw = raw.get("delisted_at", raw.get("delisted"))
    elif isinstance(raw, (tuple, list)):
        if len(raw) >= 1:
            listed_raw = raw[0]
        if len(raw) >= 2:
            delisted_raw = raw[1]
    else:
        listed_raw = raw  # bare date-ish value == listed_at, never delisted

    listed = _coerce_date(listed_raw)
    if listed is None:
        logger.warning("pit_universe: %s has no listed_at — cannot PIT-filter, excluded", symbol)
        return None
    delisted = _coerce_date(delisted_raw)
    return ListingRecord(symbol=symbol, listed_at=listed, delisted_at=delisted)


def _is_listed_at(rec: ListingRecord, as_of: date) -> bool:
    """True iff the symbol was tradable on ``as_of`` per its listing record.

    Listed: ``listed_at <= as_of``. Still trading: no delist, or delist strictly
    *after* ``as_of`` (a same-day delist removes the name).
    """
    if rec.listed_at > as_of:
        return False
    if rec.delisted_at is not None and rec.delisted_at <= as_of:
        return False
    return True


def filter_listed_at_asof(
    symbols: Iterable[str],
    as_of: date | datetime | str,
    listing_table: ListingTable | None = None,
    *,
    force: bool | None = None,
) -> list[str]:
    """Return only symbols listed-and-not-yet-delisted at ``as_of``.

    Default-OFF: unless ``HERMES_QUANT_PIT_UNIVERSE=1`` (or ``force=True``) AND a
    non-empty ``listing_table`` is supplied, the input ``symbols`` are returned
    UNCHANGED (order and identity preserved) so the existing code path is
    byte-identical. When the gate is enabled but no usable table is present, a
    WARNING is logged stating the result is NOT survivorship-safe — we never
    silently claim point-in-time safety we don't have.

    Args:
        symbols: candidate symbols (e.g. today's live universe). Order preserved
            in the survivorship-safe output for symbols that survive.
        as_of: the as-of date (``date``, ``datetime``, or ISO string).
        listing_table: SYMBOL -> listing record / (listed, delisted) / mapping.
            Symbols absent from the table are EXCLUDED when filtering is active
            (we cannot prove they were listed → fail-closed on the per-symbol
            admission decision).
        force: test/override seam. ``True`` forces filtering on regardless of
            the env flag; ``False`` forces it off; ``None`` (default) consults
            the flag. The default path is the env flag.

    Returns:
        A list of symbols. Same membership+order as input when the gate is off
        or no table is present; a survivorship-filtered subset otherwise.
    """
    syms = list(symbols)

    enabled = _pit_enabled() if force is None else bool(force)
    if not enabled:
        # Default / existing path — byte-identical passthrough.
        return syms

    if not listing_table:
        logger.warning(
            "pit_universe: %s enabled but no listing table supplied — returning "
            "CURRENT universe unchanged. Result is NOT survivorship-safe (no PIT data).",
            _FLAG,
        )
        return syms

    as_of_date = _coerce_date(as_of)
    if as_of_date is None:
        logger.warning(
            "pit_universe: as_of=%r could not be parsed — returning CURRENT universe "
            "unchanged. Result is NOT survivorship-safe.",
            as_of,
        )
        return syms

    kept: list[str] = []
    n_unknown = 0
    n_premature = 0
    n_delisted = 0
    for sym in syms:
        raw = listing_table.get(sym)
        if raw is None:
            n_unknown += 1
            continue
        rec = _coerce_record(sym, raw)
        if rec is None:
            n_unknown += 1
            continue
        if rec.listed_at > as_of_date:
            n_premature += 1
            continue
        if rec.delisted_at is not None and rec.delisted_at <= as_of_date:
            n_delisted += 1
            continue
        kept.append(sym)

    logger.info(
        "pit_universe: as_of=%s kept %d/%d (excluded: %d not-yet-listed, %d delisted, "
        "%d unknown-listing)",
        as_of_date.isoformat(),
        len(kept),
        len(syms),
        n_premature,
        n_delisted,
        n_unknown,
    )
    return kept


def is_point_in_time_active(listing_table: ListingTable | None = None) -> bool:
    """True iff a call right now would actually apply PIT filtering.

    Convenience for callers/diagnostics: PIT is *active* only when the flag is
    enabled AND a non-empty listing table is available. Mirrors the gate inside
    ``filter_listed_at_asof`` so a caller can log honestly whether the universe
    it is about to use is survivorship-safe.
    """
    return _pit_enabled() and bool(listing_table)


__all__ = [
    "ListingRecord",
    "ListingTable",
    "filter_listed_at_asof",
    "is_point_in_time_active",
]
