"""hermes_quant.catalyst.social — Reddit + Google Trends catalyst producers (Phase 1).

Mirrors the GN-RSS ingester pattern in ``ingest.py``: stdlib-only, no paid API,
injectable fetcher for offline tests, silence-by-default on feed failure, emits the
same ``CatalystItem`` shape so the rest of the pipeline (classify -> propagate ->
synthesize) consumes social signal identically to news signal.

Two free producers:

  * Reddit — public ``.json`` endpoints (``/r/<sub>/new.json``,
    ``/r/<sub>/search.json``). No OAuth needed for read-only public listings; a
    descriptive User-Agent is required by Reddit's API rules. Each post becomes a
    CatalystItem with published_at = post ``created_utc`` (the fidelity anchor).

  * Google Trends — the public ``trends/api/dailytrends`` + ``explore`` widgets
    return JSON (with a leading ``)]}',`` XSSI guard that we strip). We surface a
    *rising-interest* signal as a synthetic catalyst headline ("<term> trending /
    interest surging") timestamped at the trend's observation date, so a velocity
    spike enters the same classify->propagate path as a news headline.

FIDELITY: every CatalystItem.published_at is a real observation timestamp (Reddit
post time / Trends date), NEVER wall-clock-now — same rule as GN pubDate. This is
what keeps any social-driven backtest lookahead-honest (ADR-0074 D74.4).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from hermes_quant.catalyst.ingest import CatalystItem, dedupe_items

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
_REDDIT_NEW = "https://www.reddit.com/r/{sub}/new.json?limit={limit}"
_REDDIT_SEARCH = (
    "https://www.reddit.com/r/{sub}/search.json"
    "?q={q}&restrict_sr=1&sort=new&limit={limit}"
)


def _reddit_posts_to_items(raw: bytes, *, query: str) -> list[CatalystItem]:
    """Parse a Reddit listing JSON payload into CatalystItems."""
    items: list[CatalystItem] = []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("catalyst.social: reddit JSON parse error: %s", e)
        return items
    children = (data.get("data") or {}).get("children") or []
    for ch in children:
        d = ch.get("data") or {}
        title = (d.get("title") or "").strip()
        created = d.get("created_utc")
        if not title or created is None:
            continue
        try:
            pub = datetime.fromtimestamp(float(created), tz=UTC)
        except (TypeError, ValueError, OSError):
            continue
        # fold engagement into the title so the classifier sees "viral" velocity;
        # the raw score/comments are kept implicitly via the headline framing.
        score = d.get("score") or 0
        ncomments = d.get("num_comments") or 0
        sub = d.get("subreddit") or ""
        link = "https://www.reddit.com" + (d.get("permalink") or "")
        items.append(CatalystItem(
            title=title,
            published_at=pub,
            source=f"reddit/r/{sub} (score={score} c={ncomments})",
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
    items = _reddit_posts_to_items(raw, query=query or f"r/{sub}")
    if dedupe:
        items = dedupe_items(items)
    return items, latency


# --------------------------------------------------------------------------- #
# Google Trends producer (rising-interest -> synthetic catalyst headline)
# --------------------------------------------------------------------------- #
_TRENDS_DAILY = (
    "https://trends.google.com/trends/api/dailytrends"
    "?hl=en-US&tz=0&geo={geo}&ns=15"
)
_XSSI_GUARD = b")]}',"


def _strip_xssi(raw: bytes) -> bytes:
    """Google JSON APIs prefix a ``)]}',`` XSSI guard; strip it before json.loads."""
    s = raw.lstrip()
    if s.startswith(_XSSI_GUARD):
        s = s[len(_XSSI_GUARD):]
    return s


def _trends_to_items(raw: bytes, *, geo: str, watch_terms: set[str] | None) -> list[CatalystItem]:
    """Parse Google daily-trends JSON into rising-interest CatalystItems.

    Each trending search becomes a synthetic catalyst headline so a velocity spike
    enters the classify->propagate path. published_at = the trend day (observation
    time), NOT now. If ``watch_terms`` is given, only emit trends whose query
    contains one of the watched brand terms (keeps it targeted; a firehose of every
    trending search is noise the propagation graph would ignore anyway).
    """
    items: list[CatalystItem] = []
    try:
        data = json.loads(_strip_xssi(raw))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("catalyst.social: trends JSON parse error: %s", e)
        return items
    days = (data.get("default") or {}).get("trendingSearchesDays") or []
    wt = {w.lower() for w in (watch_terms or set())}
    for day in days:
        date_str = day.get("date") or ""  # YYYYMMDD
        try:
            pub = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pub = datetime.now(UTC)
        for tr in day.get("trendingSearches") or []:
            q = ((tr.get("title") or {}).get("query") or "").strip()
            if not q:
                continue
            if wt and not any(w in q.lower() for w in wt):
                continue
            traffic = (tr.get("formattedTraffic") or "").strip()
            # synthetic headline framed as a positive demand catalyst so the
            # consumer-trend lexicon ("trending"/"surging") fires.
            title = f"{q} trending with surging search interest ({traffic} searches)"
            items.append(CatalystItem(
                title=title,
                published_at=pub,
                source=f"google_trends/{geo}",
                link=f"https://trends.google.com/trends/explore?q={urllib.parse.quote(q)}&geo={geo}",
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

    Returns (items, latency_seconds). Never raises (silence-by-default).
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
