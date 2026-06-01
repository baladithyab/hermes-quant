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

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0

# No-key government-primary BLS news-release schedule (ADR-0084 D-4). Public
# iCal, no API key. SUMMARY text carries the release name (CPI/NFP/PPI/...).
_BLS_ICAL_URL = "https://www.bls.gov/schedule/news_release/bls.ics"

# FRED releases/dates endpoint (ADR-0084 D-4 keyed FALLBACK; needs FRED_API_KEY).
_FRED_RELEASES_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"

# Vendored no-key PRIMARY FOMC seed (ADR-0084 Option D, item 1). Ships WITH the
# code (next to this module, mirroring catalyst/propagation_graph.seed.yaml), so
# load_fomc_seed() reads it with ZERO network. The Fed publishes the FOMC
# schedule ~1+yr ahead -> every row's announced_at is a hard PAST fact.
_FOMC_SEED_PATH = Path(__file__).resolve().parent / "fomc_calendar.seed.yaml"

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
                        dt = dt.replace(tzinfo=UTC)
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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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


# Macro-release name -> kind. Substring match against the lowercased release
# title (BLS SUMMARY text / FRED release_name). Order matters: the FIRST match
# wins, so the more specific phrases are listed first. A title that matches none
# of these is NOT a recognized macro release and is dropped (we do not want to
# blanket-classify every government release as a Tier-1 macro event).
_MACRO_NAME_KINDS: tuple[tuple[str, str], ...] = (
    ("consumer price index", "cpi"),
    ("producer price index", "ppi"),
    ("employment situation", "nfp"),  # the NFP print
    ("gross domestic product", "gdp"),
    ("personal income and outlays", "pce"),  # core PCE lives here
    ("personal consumption expenditures", "pce"),
    ("unemployment insurance weekly claims", "jobless_claims"),
)


def _infer_macro_kind(title: str | None) -> str | None:
    """Map a macro-release title to a known macro kind, or None if unrecognized.

    Returns None (NOT 'other') for an unrecognized title so callers can DROP it:
    a BLS/FRED feed carries many low-impact releases we do not want to surface as
    macro events. ``None`` => not a tracked macro release.
    """
    t = (title or "").strip().lower()
    if not t:
        return None
    for needle, kind in _MACRO_NAME_KINDS:
        if needle in t:
            return kind
    return None


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


# ---------------------------------------------------------------------------
# vendored FOMC seed — the no-key PRIMARY (ADR-0084 Option D, item 1)
# ---------------------------------------------------------------------------


def load_fomc_seed(path: Path | None = None) -> list[CalendarEvent]:
    """Load the vendored FOMC seed YAML into CalendarEvents (ZERO network).

    The no-key government-primary path: a YAML that ships WITH the package
    (``catalyst/fomc_calendar.seed.yaml``), carrying the year's FOMC meeting
    windows and communications-blackout windows, EACH with both timestamps
    (ADR-0084 D-2). Because the Fed publishes the schedule ~1+yr ahead, every
    row's ``announced_at`` is a hard PAST fact (no fabricated provenance).

    Shape (mirrors propagation_graph.seed.yaml — operator-editable)::

        year: 2026
        announced_at: "2024-06-12T18:00:00Z"   # publication anchor (shared default)
        market: US
        impact: high
        source: federalreserve.gov/fomccalendars
        meetings:  [{scheduled_for: ..., title: ..., meeting_dates: ...}, ...]
        blackouts: [{scheduled_for: ..., title: ..., blackout_start/_end: ...}, ...]

    Top-level ``announced_at``/``market``/``impact``/``source`` are DEFAULTS each
    row inherits unless it overrides them; a row may carry its own ``announced_at``.
    Every event is ``kind="fomc"``. Parsing reuses :func:`parse_event_rows`, so the
    asof invariant (``announced_at <= scheduled_for``, tz-aware) is enforced at
    construction and any dishonest/unparseable row is SKIPPED (never defaulted to
    now()). Never raises — a missing/malformed seed returns ``[]``
    (silence-by-default; a stale seed must not break the daily run).

    Extra row keys (``meeting_dates``, ``blackout_start``, ``blackout_end``) are
    carried for human/guard provenance and ignored by the CalendarEvent contract.
    """
    p = path or _FOMC_SEED_PATH
    try:
        import yaml  # lazy import; PyYAML is already a project dep

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("calendar: FOMC seed not found at %s -> no FOMC events", p)
        return []
    except Exception as e:  # noqa: BLE001 - malformed seed is non-fatal (silence-by-default)
        logger.warning("calendar: FOMC seed load failed (%s) -> no FOMC events", e)
        return []
    if not isinstance(data, dict):
        return []

    default_announced = data.get("announced_at")
    default_market = data.get("market") or "US"
    default_impact = data.get("impact") or "high"
    default_source = data.get("source") or "federalreserve.gov"

    rows: list[dict] = []
    for section in ("meetings", "blackouts"):
        section_rows = data.get(section) or []
        if not isinstance(section_rows, list):
            continue
        for r in section_rows:
            if not isinstance(r, dict):
                continue
            # Every FOMC seed row is kind=fomc; inherit the file-level defaults
            # (a row may still override announced_at/market/impact/source).
            row = dict(r)
            row.setdefault("kind", "fomc")
            row.setdefault("announced_at", default_announced)
            row.setdefault("market", default_market)
            row.setdefault("impact", default_impact)
            rows.append(row)
    # source is applied uniformly via parse_event_rows (rows can still self-source).
    return parse_event_rows(rows, source=default_source)


# ---------------------------------------------------------------------------
# source producers — best-effort, injectable, silence-by-default (ADR-0084 §D.1)
#
# Each producer:
#   * takes an INJECTABLE fetcher (offline-testable; no live network in tests),
#   * returns [] on key-absent / fetch-fail / parse-fail (silence-by-default;
#     a dead source must never break the daily run), and
#   * is asof-honest two-timestamp: announced_at from the feed's known-at
#     (iCal DTSTAMP / FRED realtime_start / observation asof), scheduled_for
#     from the row. announced_at is NEVER defaulted to now() inside the parser;
#     where a feed only exposes an observation instant (FRED realtime_start,
#     yfinance has none), that instant IS the honest "schedule became known to
#     us" anchor and is passed in explicitly.
# ---------------------------------------------------------------------------


def ingest_bls_ical(
    *,
    url: str = _BLS_ICAL_URL,
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
) -> tuple[list[CalendarEvent], float]:
    """Ingest the BLS news-release schedule (.ics, NO key) for CPI/NFP/PPI/... dates.

    The public BLS iCal carries one VEVENT per scheduled release; the SUMMARY text
    names the release ("Consumer Price Index", "Employment Situation", ...). We
    parse the calendar via the shared stdlib iCal parser (DTSTART => scheduled_for,
    DTSTAMP => announced_at) and then RE-KIND each event from its title, DROPPING
    any release we do not track as a macro event (so we surface CPI/NFP/PPI/etc.,
    not every BLS release). impact defaults to ``high`` (Tier-1 macro).

    ``fetcher`` is injectable (``fetcher(url, timeout) -> bytes``) for offline
    tests. On any fetch/parse failure returns ``([], latency)`` — never raises.
    """
    raw_events, latency = ingest_ical(
        url, kind="other", market="US", impact="high",
        timeout=timeout, fetcher=fetcher, source="bls",
    )
    out: list[CalendarEvent] = []
    for ev in raw_events:
        kind = _infer_macro_kind(ev.title)
        if kind is None:
            continue  # not a tracked macro release -> drop (do not over-surface)
        out.append(
            CalendarEvent(
                kind=kind,
                scheduled_for=ev.scheduled_for,
                announced_at=ev.announced_at,
                market="US",
                impact=ev.impact,
                title=ev.title,
                source="bls",
            )
        )
    return out, latency


def _fred_api_key() -> str | None:
    """Return FRED_API_KEY from the env, or None when absent/blank.

    Key-absent => the FRED producer is silent (returns []). The key is never
    logged. Mirrors the catalyst flag-reading posture (os.environ.get)."""
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    return key or None


def parse_fred_release_dates(
    payload: bytes | str | dict,
    *,
    impact: str = "high",
) -> list[CalendarEvent]:
    """Parse a FRED ``releases/dates`` JSON payload into macro CalendarEvents.

    Shape (file_type=json, include_release_dates_with_no_data=true)::

        {"realtime_start": "2026-05-31",
         "release_dates": [{"release_id": 10, "release_name": "Consumer Price Index",
                            "date": "2026-06-10"}, ...]}

    asof anchor (ADR-0084 D-2): a FUTURE scheduled release has no public
    "announced_at" timestamp in this feed, but ``realtime_start`` is the FRED
    realtime point — i.e. the date as of which this schedule is KNOWN to us. That
    observation instant is the honest "the schedule became known" anchor; we use
    it as ``announced_at`` rather than defaulting to now() inside the row parser.
    A row whose release name is not a tracked macro kind is DROPPED. Never raises.
    """
    try:
        obj = payload if isinstance(payload, dict) else json.loads(
            payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        )
    except (ValueError, AttributeError) as e:
        logger.warning("calendar: FRED JSON parse error: %s", e)
        return []
    if not isinstance(obj, dict):
        return []
    # realtime_start is the asof anchor for every row (when we observed the schedule).
    announced = _parse_ts(obj.get("realtime_start"))
    rows_in = obj.get("release_dates") or []
    if not isinstance(rows_in, list):
        return []
    rows: list[dict] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        kind = _infer_macro_kind(r.get("release_name"))
        if kind is None:
            continue  # untracked release -> drop
        # Per-row publication, if FRED exposes it (release_last_updated), is a
        # stronger asof anchor than the container realtime_start; prefer it.
        row_announced = _parse_ts(r.get("release_last_updated")) or announced
        if row_announced is None:
            continue  # no honest asof anchor -> SKIP (never default to now())
        rows.append(
            {
                "kind": kind,
                "scheduled_for": r.get("date"),
                "announced_at": row_announced,
                "market": "US",
                "impact": impact,
                "title": (r.get("release_name") or "").strip(),
            }
        )
    return parse_event_rows(rows, source="fred")


def ingest_fred_releases(
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
    api_key: str | None = None,
) -> tuple[list[CalendarEvent], float]:
    """Ingest FRED ``releases/dates`` for FUTURE scheduled macro dates (keyed).

    Requires ``FRED_API_KEY`` (passed or from the env). KEY ABSENT => returns
    ``([], 0.0)`` immediately — silence-by-default, never crash (ADR-0084 D-4).
    Queries with ``include_release_dates_with_no_data=true`` so future-scheduled
    dates (which have no data yet) are returned.

    ``fetcher`` is injectable (``fetcher(url, timeout) -> bytes``) for offline
    tests; the constructed URL (key redacted in logs) is passed through. On any
    fetch/parse failure returns ``([], latency)`` — never raises.
    """
    key = api_key or _fred_api_key()
    if not key:
        logger.debug("calendar: FRED_API_KEY absent -> FRED producer silent")
        return [], 0.0
    import urllib.parse

    params = urllib.parse.urlencode(
        {
            "api_key": key,
            "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
        }
    )
    url = f"{_FRED_RELEASES_DATES_URL}?{params}"
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 - feed failure is non-fatal
        logger.warning("calendar: FRED fetch failed: %s", e)  # key not in message
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    return parse_fred_release_dates(raw), latency


def ingest_yfinance_earnings(
    symbols: Iterable[str],
    *,
    asof: datetime | None = None,
    ticker_factory=None,
    impact: str = "high",
) -> tuple[list[CalendarEvent], float]:
    """Ingest next-earnings dates per symbol via yfinance ``Ticker.calendar``.

    The least-bad free option (ADR-0084 D-4 / Negative-risk note): ``.calendar``
    returns a date-only next-earnings date. NOTE: yfinance's ``.earnings_dates``
    FUTURE path is broken upstream, so we deliberately use ``.calendar`` and treat
    the date as a 00:00:00 UTC ``scheduled_for`` (date-only; yfinance forward
    dates may only WIDEN risk, never inform direction — ADR-0084 Negative).

    asof anchor (ADR-0084 D-2): yfinance exposes NO "when this earnings date was
    scheduled/announced" timestamp. The honest anchor is the OBSERVATION instant
    — when we fetched the schedule — supplied as ``asof`` (defaults to now()).
    This is the only producer where now() is an honest anchor: it is the instant
    the schedule became known TO US, not a fabricated event-publication time.

    ``ticker_factory`` is injectable (``ticker_factory(symbol) -> obj`` whose
    ``.calendar`` is the yfinance dict) for offline tests. A missing/empty/failed
    lookup for a symbol yields NO event (never fabricate a blackout). Never raises.
    """
    observed = asof or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    factory = ticker_factory or _default_ticker_factory
    t0 = time.monotonic()
    rows: list[dict] = []
    for sym in symbols:
        sym = (sym or "").strip().upper()
        if not sym:
            continue
        try:
            cal = getattr(factory(sym), "calendar", None)
        except Exception as e:  # noqa: BLE001 - per-symbol failure is non-fatal
            logger.warning("calendar: yfinance calendar fetch failed for %s: %s", sym, e)
            continue
        for dt in _earnings_dates_from_calendar(cal):
            # Only future-or-equal dates relative to the observation are forward
            # events worth gating; a past .calendar date is stale, drop it.
            if dt < observed:
                continue
            rows.append(
                {
                    "kind": "earnings",
                    "scheduled_for": dt,
                    "announced_at": observed,  # honest: when we observed the schedule
                    "symbol": sym,
                    "impact": impact,
                    "title": f"{sym} earnings",
                }
            )
    latency = time.monotonic() - t0
    return parse_event_rows(rows, source="yfinance"), latency


def _earnings_dates_from_calendar(cal) -> list[datetime]:
    """Extract earnings dates (00:00 UTC) from a yfinance ``.calendar`` dict.

    ``.calendar`` is ``{"Earnings Date": [datetime.date, ...], ...}`` (date-only).
    Accepts ``date``/``datetime``/ISO-string members. Returns [] on any shape
    mismatch — never raises (best-effort, silence-by-default)."""
    if not isinstance(cal, dict):
        return []
    raw = cal.get("Earnings Date")
    if raw is None:
        return []
    members = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[datetime] = []
    for m in members:
        dt = _parse_ts(m if isinstance(m, (str, datetime)) else getattr(m, "isoformat", lambda: "")())
        if dt is not None:
            out.append(dt)
    return out


def _default_ticker_factory(symbol: str):
    """Default yfinance Ticker factory (imported lazily; no hard dependency)."""
    import yfinance  # noqa: PLC0415 - lazy: keeps yfinance an optional, soft dep

    return yfinance.Ticker(symbol)


def ingest_calendar(
    *,
    earnings_symbols: Iterable[str] | None = None,
    asof: datetime | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    bls_fetcher=None,
    fred_fetcher=None,
    fred_api_key: str | None = None,
    ticker_factory=None,
    enable_bls: bool = True,
    enable_fred: bool = True,
    enable_earnings: bool = True,
) -> list[CalendarEvent]:
    """Aggregate all best-effort source producers into one CalendarEvent list.

    Runs the no-key BLS .ics primary, the keyed FRED fallback (silent when
    FRED_API_KEY absent), and best-effort yfinance earnings (only if symbols are
    supplied). Each producer is silence-by-default; one dead source does not stop
    the others. Returns the concatenated events (unfiltered by asof — callers
    apply ``visible_at`` at decision time). Never raises.

    All fetchers/factory are injectable for offline-deterministic tests; the
    enable_* flags let a caller disable an individual producer. This is the
    SOURCE-WIRING entry point (data layer); it does NOT read the
    HERMES_QUANT_EVENT_RISK flag — that gates the downstream perception field and
    deterministic guard (separate seeds), not whether the data may be fetched.
    """
    events: list[CalendarEvent] = []
    if enable_bls:
        bls_events, lat = ingest_bls_ical(timeout=timeout, fetcher=bls_fetcher)
        logger.info("calendar: BLS .ics -> %d events in %.2fs", len(bls_events), lat)
        events.extend(bls_events)
    if enable_fred:
        fred_events, lat = ingest_fred_releases(
            timeout=timeout, fetcher=fred_fetcher, api_key=fred_api_key
        )
        logger.info("calendar: FRED -> %d events in %.2fs", len(fred_events), lat)
        events.extend(fred_events)
    if enable_earnings and earnings_symbols:
        earn_events, lat = ingest_yfinance_earnings(
            earnings_symbols, asof=asof, ticker_factory=ticker_factory
        )
        logger.info("calendar: yfinance earnings -> %d events in %.2fs", len(earn_events), lat)
        events.extend(earn_events)
    return events


def visible_at(events: Iterable[CalendarEvent], decision_asof: datetime) -> list[CalendarEvent]:
    """Filter to events whose schedule is PUBLIC at ``decision_asof`` (asof gate).

    An event is admissible only if ``announced_at <= decision_asof`` — the
    consumer may not know an event EXISTS before its schedule was announced.
    (``scheduled_for`` is intentionally NOT filtered: a forward event is exactly
    one whose ``scheduled_for`` is still in the future.) Pure; never raises.
    """
    if decision_asof.tzinfo is None:
        decision_asof = decision_asof.replace(tzinfo=UTC)
    return [e for e in events if e.announced_at <= decision_asof]
