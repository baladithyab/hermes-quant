"""hermes_quant.catalyst.social — Reddit + Google Trends catalyst producers (Phase 1).

Mirrors the GN-RSS ingester pattern in ``ingest.py``: stdlib-only, no paid API,
injectable fetcher for offline tests, silence-by-default on feed failure, emits the
same ``CatalystItem`` shape so the rest of the pipeline (classify -> propagate ->
synthesize) consumes social signal identically to news signal.

Two free producers:

  * Reddit — public Atom RSS feeds (``/r/<sub>/new.rss``,
    ``/r/<sub>/search.rss``). The unauthenticated ``.json`` endpoints now return
    HTTP 403 (Reddit closed anonymous JSON access), but the public Atom feeds still
    serve WITHOUT OAuth. A descriptive User-Agent is still sent (Reddit throttles
    bare UAs). We parse the Atom ``<entry>`` elements with ``xml.etree`` (same
    approach as the GN-RSS ingester): each post becomes a CatalystItem with
    published_at = the entry ``<published>`` time (fallback ``<updated>``), an
    ISO-8601 timestamp parsed to tz-aware UTC — the fidelity anchor. NOTE: the
    public RSS feed does NOT expose the post score/comment count the old JSON did,
    so we do not fabricate one; the source tag is just ``reddit/r/<sub> (rss)``.

  * Google Trends — the public ``trending/rss`` feed returns RSS 2.0 (the old
    ``trends/api/dailytrends`` JSON endpoint was removed and now 404s). We parse it
    with the same ``xml.etree`` + RFC-822 ``_parse_pubdate`` approach as the GN-RSS
    ingester and surface a *rising-interest* signal as a synthetic catalyst headline
    ("<term> trending / interest surging") timestamped at the trend's ``<pubDate>``
    observation time, so a velocity spike enters the same classify->propagate path
    as a news headline.

FIDELITY: every CatalystItem.published_at is a real observation timestamp (Reddit
post time / Trends ``<pubDate>``), NEVER wall-clock-now — same rule as GN pubDate.
This is what keeps any social-driven backtest lookahead-honest (ADR-0074 D74.4).
"""
from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from hermes_quant.catalyst.ingest import CatalystItem, _parse_pubdate, dedupe_items

logger = logging.getLogger(__name__)

# Reddit requires a unique descriptive UA or it 429s/403s aggressively.
_UA = "python:hermes-quant.catalyst-social:v0.1 (research; by /u/hermes-quant)"
_REDDIT_TIMEOUT = 15.0
_TRENDS_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# Shared fetch helper (injectable, never raises — silence-by-default)
# --------------------------------------------------------------------------- #
def _fetch_raw(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# --------------------------------------------------------------------------- #
# Reddit producer
# --------------------------------------------------------------------------- #
# Public Atom RSS feeds — serve WITHOUT OAuth (the unauthenticated ``.json``
# endpoints now 403). Same query semantics as the old JSON URLs.
_REDDIT_NEW = "https://www.reddit.com/r/{sub}/new.rss?limit={limit}"
_REDDIT_SEARCH = (
    "https://www.reddit.com/r/{sub}/search.rss"
    "?q={q}&restrict_sr=1&sort=new&limit={limit}"
)

# Atom namespace — every element (<entry>, <title>, <published>, <link>...) lives in
# this namespace, so we address them with Clark-notation "{ns}tag" names (the form
# ElementTree expands a default xmlns into). This is the public Atom namespace URI
# and is stable across Reddit's new/search feeds.
_ATOM_NS = "http://www.w3.org/2005/Atom"


def _parse_atom_dt(s: str) -> datetime | None:
    """Parse an Atom ISO-8601 timestamp to tz-aware UTC. Returns None on failure.

    Atom ``<published>``/``<updated>`` are ISO-8601 (e.g. "2026-05-31T20:24:50+00:00"
    or a "...Z" form), NOT RFC-822 — so we use ``datetime.fromisoformat`` rather than
    ``ingest._parse_pubdate`` (which is the RFC-2822 ``parsedate_to_datetime`` path).
    A naive value (no offset) is treated as UTC; anything unparseable returns None so
    the caller SKIPS the entry rather than fabricating a now() asof.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _reddit_posts_to_items(raw: bytes, *, sub: str, query: str) -> list[CatalystItem]:
    """Parse a Reddit Atom RSS feed into CatalystItems.

    For each ``<entry>``: title = ``<title>``; published_at = ``<published>``
    (fallback ``<updated>``) parsed to tz-aware UTC — NEVER wall-clock now. An entry
    with a missing/unparseable timestamp is SKIPPED (defaulting to now would fabricate
    freshness and corrupt packet.asof). link = the ``<link href=...>`` attribute. The
    public RSS feed carries no post score, so the source tag is just
    ``reddit/r/<sub> (rss)`` (no fabricated score) — it still starts with "reddit/"
    so PDR-3's ``source_family`` maps it to the reddit family.
    """
    items: list[CatalystItem] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("catalyst.social: reddit RSS parse error: %s", e)
        return items
    src = f"reddit/r/{sub} (rss)"
    for entry in root.iter(f"{{{_ATOM_NS}}}entry"):
        title = (entry.findtext(f"{{{_ATOM_NS}}}title") or "").strip()
        pub = _parse_atom_dt(entry.findtext(f"{{{_ATOM_NS}}}published") or "")
        if pub is None:  # fall back to <updated> before giving up
            pub = _parse_atom_dt(entry.findtext(f"{{{_ATOM_NS}}}updated") or "")
        if not title or pub is None:
            continue  # no title, or no parseable timestamp to anchor packet.asof
        link_el = entry.find(f"{{{_ATOM_NS}}}link")
        link = link_el.get("href", "") if link_el is not None else ""
        items.append(CatalystItem(
            title=title,
            published_at=pub,
            source=src,
            link=link,
            query=query,
        ))
    return items


def ingest_reddit(
    sub: str,
    *,
    query: str | None = None,
    limit: int = 50,
    timeout: float = _REDDIT_TIMEOUT,
    fetcher=None,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest recent posts from a subreddit (optionally search-filtered).

    Returns (items, latency_seconds). Never raises — a dead feed returns
    ([], latency) so the daily run is not broken by Reddit rate-limits.
    """
    if query:
        url = _REDDIT_SEARCH.format(sub=urllib.parse.quote(sub),
                                    q=urllib.parse.quote(query), limit=limit)
    else:
        url = _REDDIT_NEW.format(sub=urllib.parse.quote(sub), limit=limit)
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 — feed failure is non-fatal
        logger.warning("catalyst.social: reddit fetch failed for r/%s: %s", sub, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    items = _reddit_posts_to_items(raw, sub=sub, query=query or f"r/{sub}")
    if dedupe:
        items = dedupe_items(items)
    return items, latency


# --------------------------------------------------------------------------- #
# Google Trends producer (rising-interest -> synthetic catalyst headline)
# --------------------------------------------------------------------------- #
# The legacy trends/api/dailytrends JSON endpoint was removed (404s). The public
# trending/rss feed returns RSS 2.0 with the ht: namespace; we parse it like GN-RSS.
_TRENDS_DAILY = "https://trends.google.com/trending/rss?geo={geo}"


def _trends_to_items(raw: bytes, *, geo: str, watch_terms: set[str] | None) -> list[CatalystItem]:
    """Parse Google trending-searches RSS into rising-interest CatalystItems.

    Each trending search becomes a synthetic catalyst headline so a velocity spike
    enters the classify->propagate path. published_at = the trend's ``<pubDate>``
    (observation time), parsed via the same RFC-822 parser the GN-RSS ingester uses
    — NEVER wall-clock now. An item with a missing/unparseable pubDate is SKIPPED
    (defaulting to now would fabricate freshness and corrupt packet.asof). If
    ``watch_terms`` is given, only emit trends whose term (the ``<title>``) contains
    one of the watched brand terms (keeps it targeted; a firehose of every trending
    search is noise the propagation graph would ignore anyway).
    """
    items: list[CatalystItem] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("catalyst.social: trends RSS parse error: %s", e)
        return items
    wt = {w.lower() for w in (watch_terms or set())}
    for item in root.iter("item"):
        term = (item.findtext("title") or "").strip()
        pub = _parse_pubdate(item.findtext("pubDate") or "")
        if not term or pub is None:
            continue  # no term, or no parseable timestamp to anchor packet.asof
        if wt and not any(w in term.lower() for w in wt):
            continue
        # approx_traffic is ht:-namespaced (xmlns:ht=trending/rss). Match by local
        # tag name so we don't depend on the namespace URI string staying stable.
        traffic = ""
        for child in item:
            if child.tag.rsplit("}", 1)[-1] == "approx_traffic":
                traffic = (child.text or "").strip()
                break
        # synthetic headline framed as a positive demand catalyst so the
        # consumer-trend lexicon ("trending"/"surging") fires.
        title = f"{term} trending with surging search interest ({traffic} searches)"
        items.append(CatalystItem(
            title=title,
            published_at=pub,
            source=f"google_trends/{geo}",
            link=f"https://trends.google.com/trends/explore?q={urllib.parse.quote(term)}&geo={geo}",
            query="google-trends-daily",
        ))
    return items


def ingest_google_trends(
    *,
    geo: str = "US",
    watch_terms: set[str] | None = None,
    timeout: float = _TRENDS_TIMEOUT,
    fetcher=None,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest Google daily trending searches, filtered to watched brand terms.

    Returns (items, latency_seconds). Never raises (silence-by-default) — any fetch
    OR parse failure (incl. a malformed feed) returns ([], latency).
    """
    url = _TRENDS_DAILY.format(geo=urllib.parse.quote(geo))
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001
        logger.warning("catalyst.social: trends fetch failed (geo=%s): %s", geo, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    items = _trends_to_items(raw, geo=geo, watch_terms=watch_terms)
    if dedupe:
        items = dedupe_items(items)
    return items, latency


# --------------------------------------------------------------------------- #
# Orchestration: pull a set of social producers, concat + cross-dedupe
# --------------------------------------------------------------------------- #
def ingest_social(
    reddit_queries: dict[str, str] | None = None,
    *,
    trends_geo: str | None = "US",
    trends_watch_terms: set[str] | None = None,
    timeout: float = 15.0,
    fetcher=None,
) -> list[CatalystItem]:
    """Pull configured Reddit subs + Google Trends, concat, cross-query dedupe.

    ``reddit_queries`` maps ``"sub"`` or ``"sub:search terms"`` -> a provenance
    label. Never raises; producers that fail contribute zero items.
    """
    all_items: list[CatalystItem] = []
    for spec in (reddit_queries or {}):
        sub, _, q = spec.partition(":")
        items, lat = ingest_reddit(sub.strip(), query=(q.strip() or None),
                                   timeout=timeout, fetcher=fetcher, dedupe=True)
        logger.info("catalyst.social: reddit %s -> %d items in %.2fs", spec, len(items), lat)
        all_items.extend(items)
    if trends_geo is not None:
        items, lat = ingest_google_trends(geo=trends_geo, watch_terms=trends_watch_terms,
                                          timeout=timeout, fetcher=fetcher, dedupe=True)
        logger.info("catalyst.social: google-trends %s -> %d items in %.2fs", trends_geo, len(items), lat)
        all_items.extend(items)
    return dedupe_items(all_items)
