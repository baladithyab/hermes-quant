"""hermes_quant.catalyst.calendar — scheduled-event calendar adapter (ADR-0084, D74.5 shape).

A stdlib-only, asof-honest adapter for FORWARD scheduled events (FOMC, CPI, NFP,
earnings, ...). It clones the posture of ``catalyst/ingest.py``: pure ``parse_*``
functions, an ``ingest_*`` with an INJECTABLE fetcher (offline-testable), and
silence-by-default — any network/parse failure returns ``([], latency)`` and
NEVER raises (a dead feed must not break the daily run).

The load-bearing distinction from ``CatalystItem`` is the TWO-TIMESTAMP model
(ADR-0084 D-2):

  * ``announced_at`` — when the schedule became PUBLIC (the asof anchor; the
    equivalent of ``CatalystItem.published_at`` / EvidenceRecord ``available_at``).
    A consumer may not even know the event EXISTS before this instant.
  * ``scheduled_for`` — when the event will HAPPEN (the forward-looking payload).
    A consumer may not know the event's OUTCOME before this instant.

asof-honesty is enforced at construction:

  * both timestamps must be tz-aware,
  * ``announced_at <= scheduled_for`` (you cannot announce an event after it
    happened — that would be a fabricated schedule), and
  * an event whose ``announced_at`` is UNPARSEABLE is SKIPPED, never defaulted
    to ``now()`` (defaulting to now would manufacture lookahead-free provenance
    that does not exist).

This module is the ADAPTER + dataclass ONLY. The vendored FOMC seed YAML, the
``event_risk`` PerceptionFrame field, and the deterministic pre-event guard are
separate (blocked) seeds and are intentionally NOT in this file. Importing this
module has no effect on any existing path; it is purely additive.

No new hard dependency — events are parsed from generic stdlib representations
(dict rows and iCalendar text) with ``xml``/``email``-style stdlib parsers only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0

# Coarse impact tiers (config-driven hierarchy lives with the guard seed, not here).
# A frozen, ordered set so the adapter can normalize free-text vendor tiers.
_IMPACT_TIERS: frozenset[str] = frozenset({"low", "medium", "high"})

# Recognized event kinds. Unknown kinds are preserved verbatim (the adapter does
# not gatekeep semantics — that is the guard's job) but normalized to lowercase.
_KNOWN_KINDS: frozenset[str] = frozenset(
    {"earnings", "fomc", "cpi", "nfp", "gdp", "pce", "ppi", "jobless_claims", "other"}
)


@dataclass(frozen=True)
class CalendarEvent:
    """One scheduled, forward-looking event. Immutable; asof-honest by construction.

    Two timestamps (ADR-0084 D-2):
      * ``announced_at`` — when the schedule became public (asof anchor).
      * ``scheduled_for`` — when the event happens (forward payload).

    ``outcome`` is ALWAYS ``None`` at adapter time: the calendar carries the
    EXISTENCE of a forward event, never its result. A consumer that learns the
    outcome does so via the catalyst/evidence layers AFTER ``scheduled_for``.
    """

    kind: str  # earnings | fomc | cpi | nfp | ... (normalized lowercase)
    scheduled_for: datetime  # tz-aware UTC — when the event happens (forward)
    announced_at: datetime  # tz-aware UTC — when the schedule became public (asof anchor)
    symbol: str | None = None  # equity symbol for single-name (earnings); None for macro
    market: str | None = None  # market/region for macro (e.g. "US"); None for single-name
    impact: str = "medium"  # low | medium | high
    title: str = ""  # human-readable label (provenance/explainability)
    source: str = ""  # which feed surfaced it (provenance)
    outcome: None = None  # ALWAYS None — the adapter is outcome-free by contract

    def __post_init__(self) -> None:
        # Both timestamps MUST be tz-aware. A naive timestamp has no honest asof
        # anchor (its UTC offset is ambiguous), so it cannot be admitted.
        for fname in ("scheduled_for", "announced_at"):
            v = getattr(self, fname)
            if not isinstance(v, datetime) or v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
                raise ValueError(
                    f"CalendarEvent.{fname} must be a tz-aware datetime; got {v!r}"
                )
        # The asof invariant: a schedule cannot have become public AFTER the event
        # already happened. announced_at <= scheduled_for. (scheduled_for may be
        # far in the future — that is the whole point of a calendar.)
        if self.announced_at > self.scheduled_for:
            raise ValueError(
                f"CalendarEvent: announced_at ({self.announced_at.isoformat()}) > "
                f"scheduled_for ({self.scheduled_for.isoformat()}); a schedule cannot "
                f"be announced after the event happens (asof violation)."
            )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "scheduled_for": self.scheduled_for.isoformat(),
            "announced_at": self.announced_at.isoformat(),
            "symbol": self.symbol,
            "market": self.market,
            "impact": self.impact,
            "title": self.title,
            "source": self.source,
            "outcome": self.outcome,  # always None
        }


# ---------------------------------------------------------------------------
# timestamp + field parsers (pure)
# ---------------------------------------------------------------------------


def _parse_ts(s: str | datetime | None) -> datetime | None:
    """Parse a timestamp to tz-aware UTC. Returns None on failure.

    Accepts ISO-8601 (the calendar-native form), RFC-2822 (iCal/RSS), or an
    already-``datetime`` value. Mirrors ``ingest._parse_pubdate``'s posture:
    use stdlib purpose-built parsers, never silently misinterpret a zone. A
    NAIVE result is assumed UTC only as a last resort and converted explicitly.
    """
    if isinstance(s, datetime):
        dt: datetime | None = s
    else:
        s = (s or "").strip()
        if not s:
            return None
        dt = None
        # ISO-8601 first (the calendar-native + seed-YAML form). Normalize a
        # trailing 'Z' which fromisoformat rejected before 3.11 in some forms.
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = None
        # iCalendar basic form: YYYYMMDDTHHMMSSZ (no separators).
        if dt is None:
            for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    if fmt.endswith("Z"):
                        dt = dt.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        # RFC-2822 fallback (iCal DTSTAMP sometimes carries it; RSS shares it).
        if dt is None:
            try:
                dt = parsedate_to_datetime(s)
            except (TypeError, ValueError):
                dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:  # naive -> assume UTC (last resort, made explicit)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_kind(s: str | None) -> str:
    """Normalize an event kind to a known lowercase token (unknown -> 'other')."""
    k = (s or "").strip().lower().replace("-", "_").replace(" ", "_")
    return k if k in _KNOWN_KINDS else "other"


def _norm_impact(s: str | None) -> str:
    """Normalize a free-text impact tier to {low,medium,high} (default medium)."""
    i = (s or "").strip().lower()
    # vendor synonyms commonly seen on economic calendars
    if i in ("3", "***", "high", "tier1", "tier-1"):
        return "high"
    if i in ("2", "**", "medium", "med", "moderate", "tier2", "tier-2"):
        return "medium"
    if i in ("1", "*", "low", "tier3", "tier-3"):
        return "low"
    return i if i in _IMPACT_TIERS else "medium"


# ---------------------------------------------------------------------------
# row parser — generic {field: value} rows (seed YAML / vendor JSON shape)
# ---------------------------------------------------------------------------


def parse_event_rows(
    rows: Iterable[dict],
    *,
    source: str = "",
) -> list[CalendarEvent]:
    """Parse generic dict rows into CalendarEvents. Skips any unparseable row.

    A row is admitted only if BOTH ``scheduled_for`` and ``announced_at`` parse
    to tz-aware timestamps AND the asof invariant holds. A row missing a
    parseable ``announced_at`` is SKIPPED (ADR-0084 D-2: never defaulted to
    now()). Never raises.
    """
    events: list[CalendarEvent] = []
    for row in rows:
        try:
            scheduled = _parse_ts(row.get("scheduled_for") or row.get("date") or row.get("dtstart"))
            announced = _parse_ts(row.get("announced_at") or row.get("announced"))
            if scheduled is None:
                continue  # no event time -> cannot gate -> drop
            if announced is None:
                # asof anchor missing -> SKIP (never default to now()).
                logger.debug(
                    "calendar: skipping row with no parseable announced_at: %r",
                    row.get("title") or row.get("kind"),
                )
                continue
            ev = CalendarEvent(
                kind=_norm_kind(row.get("kind") or row.get("type")),
                scheduled_for=scheduled,
                announced_at=announced,
                symbol=(row.get("symbol") or None),
                market=(row.get("market") or row.get("region") or None),
                impact=_norm_impact(row.get("impact") or row.get("importance")),
                title=(row.get("title") or "").strip(),
                source=source or (row.get("source") or "").strip(),
            )
        except ValueError as e:  # asof / tz invariant violated -> drop, never raise
            logger.debug("calendar: skipping invalid event row: %s", e)
            continue
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# iCalendar (.ics) parser — the BLS / government no-key primary feed shape
# ---------------------------------------------------------------------------


def parse_ical(
    raw: bytes | str,
    *,
    kind: str = "other",
    market: str | None = None,
    impact: str = "high",
    source: str = "",
) -> list[CalendarEvent]:
    """Parse minimal iCalendar VEVENTs into CalendarEvents. Skips unparseable.

    A pragmatic stdlib parser (no `icalendar` dependency, mirroring ingest.py's
    no-feedparser stance): walks VEVENT blocks, reading DTSTART as
    ``scheduled_for`` and DTSTAMP (publication of the calendar entry) as
    ``announced_at``. A VEVENT with no parseable DTSTAMP is SKIPPED — the iCal
    spec mandates DTSTAMP, so its absence means the asof anchor is unknown and
    we must NOT default to now(). Never raises.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    rows: list[dict] = []
    cur: dict | None = None
    try:
        # RFC-5545 line unfolding: a leading space/tab continues the prior line.
        lines: list[str] = []
        for line in text.splitlines():
            if line[:1] in (" ", "\t") and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        for line in lines:
            up = line.strip().upper()
            if up == "BEGIN:VEVENT":
                cur = {"kind": kind, "market": market, "impact": impact}
            elif up == "END:VEVENT":
                if cur is not None:
                    rows.append(cur)
                cur = None
            elif cur is not None and ":" in line:
                prop, _, value = line.partition(":")
                name = prop.split(";", 1)[0].strip().upper()
                value = value.strip()
                if name == "DTSTART":
                    cur["scheduled_for"] = value
                elif name == "DTSTAMP":
                    cur["announced_at"] = value
                elif name == "SUMMARY":
                    cur["title"] = value
    except Exception as e:  # noqa: BLE001 - malformed iCal is non-fatal
        logger.warning("calendar: iCal parse error: %s", e)
        return []
    return parse_event_rows(rows, source=source)


# ---------------------------------------------------------------------------
# ingest — injectable fetcher, silence-by-default
# ---------------------------------------------------------------------------


def _fetch_raw(url: str, timeout: float) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "hermes-quant calendar; research"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - caller-fixed host
        return resp.read()


def ingest_ical(
    url: str,
    *,
    kind: str = "other",
    market: str | None = None,
    impact: str = "high",
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
    source: str = "",
) -> tuple[list[CalendarEvent], float]:
    """Ingest one iCalendar feed. Returns (events, latency_seconds).

    ``fetcher`` is injectable (``fetcher(url, timeout) -> bytes``) for offline
    tests. On any network/parse failure returns ``([], latency)`` — never raises
    (silence-by-default; a dead feed must not break the daily run).
    """
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 - feed failure is non-fatal
        logger.warning("calendar: fetch failed for %r: %s", url, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    events = parse_ical(raw, kind=kind, market=market, impact=impact, source=source or url)
    return events, latency


def ingest_rows(
    fetcher,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    source: str = "",
) -> tuple[list[CalendarEvent], float]:
    """Ingest dict-row events via an injectable ``fetcher(timeout) -> Iterable[dict]``.

    The no-key primary path (a vendored seed YAML loader or a keyed JSON API)
    supplies rows; this wrapper enforces the silence-by-default + latency
    contract uniformly. On any failure returns ``([], latency)`` — never raises.
    """
    t0 = time.monotonic()
    try:
        rows = fetcher(timeout)
    except Exception as e:  # noqa: BLE001 - feed failure is non-fatal
        logger.warning("calendar: row fetch failed: %s", e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    events = parse_event_rows(rows or [], source=source)
    return events, latency


def visible_at(events: Iterable[CalendarEvent], decision_asof: datetime) -> list[CalendarEvent]:
    """Filter to events whose schedule is PUBLIC at ``decision_asof`` (asof gate).

    An event is admissible only if ``announced_at <= decision_asof`` — the
    consumer may not know an event EXISTS before its schedule was announced.
    (``scheduled_for`` is intentionally NOT filtered: a forward event is exactly
    one whose ``scheduled_for`` is still in the future.) Pure; never raises.
    """
    if decision_asof.tzinfo is None:
        decision_asof = decision_asof.replace(tzinfo=timezone.utc)
    return [e for e in events if e.announced_at <= decision_asof]
