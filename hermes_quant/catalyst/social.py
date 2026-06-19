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

import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from hermes_quant.catalyst.ingest import CatalystItem, _parse_pubdate, dedupe_items

logger = logging.getLogger(__name__)

# Reddit requires a unique descriptive UA or it 429s/403s aggressively.
_UA = "python:hermes-quant.catalyst-social:v0.1 (research; by /u/hermes-quant)"
_REDDIT_TIMEOUT = 15.0
_TRENDS_TIMEOUT = 15.0
_REACH_TIMEOUT = 20.0


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
    max_age_days: float | None = None,
    now: datetime | None = None,
) -> list[CatalystItem]:
    """Pull configured Reddit subs + Google Trends, concat, cross-query dedupe.

    ``reddit_queries`` maps ``"sub"`` or ``"sub:search terms"`` -> a provenance
    label. Never raises; producers that fail contribute zero items.

    ``now`` — INJECTABLE clock for the ``max_age_days`` recency gate (default
    ``None`` => wall-clock ``datetime.now(UTC)``, byte-identical to the live path).
    Pass a fixed tz-aware UTC ``now`` to make the recency cut deterministic in
    tests/backtests (it only affects which items survive the cutoff; it never
    shifts an item's real ``published_at``).

    ``max_age_days`` — RECENCY GATE (ADR-0074, PDR-3 enabler). If given, DROP any
    item whose ``published_at`` is older than ``now_utc - max_age_days``. This is
    the durable fix for stale social packets: Reddit relevance-ranked search.rss
    returns posts with a median age of ~62 days, so a "reddit packet" never
    co-occurred with fresh news inside PDR-3's 24h cross-source convergence
    window. A 7-day cutoff is fresh enough to overlap that window when a name is
    active, lenient enough to catch a multi-day trend. asof HONESTY: this filter
    ONLY excludes items by comparing each item's REAL ``published_at`` (already
    parsed from ``<published>``/``<pubDate>`` — never wall-clock-now) against a
    tz-aware UTC cutoff; it NEVER fabricates or shifts a timestamp. Items with a
    naive/missing ``published_at`` are already dropped upstream by the producers
    (``_parse_atom_dt``/``_parse_pubdate`` return None -> entry skipped), so every
    surviving item here has a tz-aware UTC anchor safe to compare. ``None`` (the
    default) => no filtering (backward compatible for library callers).
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
    all_items = _filter_by_recency(all_items, max_age_days=max_age_days, now=now)
    return dedupe_items(all_items)


def _filter_by_recency(
    items: list[CatalystItem],
    *,
    max_age_days: float | None,
    now: datetime | None = None,
) -> list[CatalystItem]:
    """Drop items older than ``now_utc - max_age_days`` by their real published_at.

    ``None`` => no-op (returns items unchanged). The cutoff uses a tz-aware UTC
    ``now`` so the comparison against each item's tz-aware UTC ``published_at`` is
    well-defined. NEVER mutates a timestamp — it only includes/excludes. Belt-and-
    suspenders: an item whose ``published_at`` is somehow naive is kept (it would
    raise on a naive-vs-aware compare; producers already exclude such items, but we
    refuse to let one bad item drop the batch — silence-by-default).

    ``now`` is an INJECTABLE clock (default ``None`` => ``datetime.now(UTC)`` read
    at call time, byte-identical to the wall-clock path). Tests pass a fixed
    tz-aware UTC ``now`` for determinism (no dependency on real wall-clock); a
    naive ``now`` is localized to UTC so the cutoff comparison stays well-defined.
    """
    if max_age_days is None:
        return items
    base_now = now if now is not None else datetime.now(UTC)
    if base_now.tzinfo is None:  # accept a naive injected clock -> treat as UTC
        base_now = base_now.replace(tzinfo=UTC)
    cutoff = base_now - timedelta(days=max_age_days)
    kept: list[CatalystItem] = []
    for it in items:
        pub = it.published_at
        if pub.tzinfo is None:  # defensive — should not happen (producers skip these)
            kept.append(it)
            continue
        if pub >= cutoff:
            kept.append(it)
    return kept


# =========================================================================== #
# aegis-ob5 — Agent-Reach social-sentiment/velocity PERCEPTION provider
# =========================================================================== #
# Twitter/X (cashtag) and StockTwits (symbol) producers backed by the operator-
# installed agent-reach CLI (the `twitter` / `stocktwits` subcommands). Each
# mirrors ``ingest_reddit`` exactly: an injectable-fetcher producer that returns
# (items, latency_seconds), NEVER raises (a dead/absent CLI -> ([], latency)),
# and anchors each item's ``published_at`` on the post's REAL ``created_at``
# (tz-aware UTC). A post with no parseable timestamp is SKIPPED — never
# fabricated to now(). The emitted ``source`` tags ("twitter/<cashtag>",
# "stocktwits/<symbol>") are what PDR-3's source_family keys on to count these as
# DISTINCT independent origins (perception/convergence.py).
#
# LIVE-DATA money rails honored here:
#   * default-OFF: the live catalyst run only CALLS these behind
#     HERMES_QUANT_SOCIAL_REACH=1 (read at call time). Unset => byte-identical
#     (the ingesters are never invoked; nothing emits a twitter/stocktwits source).
#   * no-lookahead: published_at = the post's real created_at, tz-aware UTC; an
#     unparseable/future-looking timestamp is dropped downstream by
#     velocity_source's <= asof cut + the lookahead_gate. We NEVER stamp now().
#   * silence-by-default / fail-soft: the agent-reach CLI is operator-installed;
#     if it is absent the default fetcher raises FileNotFoundError, which the
#     producer catches -> ([], latency). A dead CLI never blocks the run.


def _social_reach_on() -> bool:
    """DEFAULT-OFF Agent-Reach gate, read at call time.

    OFF (unset or anything but "1") => the live catalyst path never invokes the
    twitter/stocktwits ingesters, so the run is byte-identical to the news/reddit
    path. This is a LIVE-DATA money-path provider; it stays dark until an operator
    explicitly arms HERMES_QUANT_SOCIAL_REACH=1.
    """
    return os.environ.get("HERMES_QUANT_SOCIAL_REACH", "0") == "1"


def _reach_cli(argv: list[str], timeout: float) -> bytes:
    """Default fetcher: shell out to the operator-installed agent-reach CLI.

    ``argv`` is the fully-formed command vector (e.g. ``["twitter", "search",
    "$AAPL", "-n", "50", "--json"]``). Returns the CLI's stdout bytes. Raises on a
    missing binary (FileNotFoundError) or a non-zero exit / timeout — the CALLER
    (``ingest_twitter`` / ``ingest_stocktwits``) catches every exception and
    returns ([], latency), so a missing or failing CLI never blocks the run. We
    never run a shell (``shell=False``); ``argv`` is a list, no string interpolation.
    """
    proc = subprocess.run(  # noqa: S603 — fixed argv, shell=False, operator-installed CLI
        argv,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    return proc.stdout


def _norm_cashtag(s: str) -> str:
    """Normalize a cashtag/symbol to a bare upper-case ticker ('$AAPL'->'AAPL')."""
    return (s or "").strip().lstrip("$").upper()


def _parse_reach_created_at(value: object) -> datetime | None:
    """Parse an agent-reach post timestamp to tz-aware UTC. None on failure.

    Accepts the ISO-8601 forms the Twitter/StockTwits APIs emit
    ("2026-06-17T15:30:00Z" / "...+00:00") via ``_parse_atom_dt`` (fromisoformat,
    handles the trailing 'Z'), and a numeric Unix epoch (int/float or a numeric
    string) as a fallback. Anything unparseable returns None so the caller SKIPS
    the post rather than fabricating a now() asof — the no-lookahead anchor.
    """
    if value is None:
        return None
    # numeric epoch (seconds) — some CLIs emit created_at as a unix timestamp.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # ISO-8601 first (handles 'Z' suffix via _parse_atom_dt's fromisoformat path).
    dt = _parse_atom_dt(s)
    if dt is not None:
        return dt
    # numeric string epoch fallback ("1750000000").
    try:
        epoch = float(s)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _reach_records(raw: bytes, *, keys: tuple[str, ...]) -> list[dict]:
    """Extract the list-of-post dicts from an agent-reach JSON payload.

    Tolerant of several shapes: a bare top-level list, or a dict carrying the
    posts under one of ``keys`` ("results"/"data"/"messages"/"posts"/"tweets").
    Returns [] on any decode error or unrecognized shape (silence-by-default —
    the caller never raises).
    """
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return []
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        for k in keys:
            v = doc.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _reach_created_at(rec: dict) -> datetime | None:
    """Extract a post's REAL creation timestamp from any backend field name.

    The agent-reach backends serialize the timestamp under different keys: the
    documented Twitter backend (``twitter-cli``) emits ``createdAt`` /
    ``createdAtISO`` (camelCase — see twitter_cli/serialization.py); the
    StockTwits public stream API emits ``created_at`` (RFC form); generic CLIs
    sometimes emit ``created`` or a numeric epoch. Reading ONLY ``created_at`` /
    ``created`` (as the first cut did) silently dropped every real twitter-cli
    post (codex P2). Probe all known names IN ORDER and parse the first present
    value; missing/unparseable across all of them -> None so the caller SKIPS
    the post (never fabricates a now() asof — the no-lookahead anchor).
    """
    for k in ("createdAtISO", "createdAt", "created_at", "created_at_iso", "created", "timestamp"):
        if k in rec:
            pub = _parse_reach_created_at(rec.get(k))
            if pub is not None:
                return pub
    return None


def _reach_text(rec: dict) -> str:
    """Pull the post body from any of the common field names."""
    for k in ("text", "body", "title", "content", "message"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _reach_link(rec: dict) -> str:
    for k in ("url", "link", "permalink", "href"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ingest_twitter(
    cashtag: str,
    *,
    limit: int = 50,
    timeout: float = _REACH_TIMEOUT,
    fetcher=None,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest recent Twitter/X posts for a cashtag via the agent-reach CLI.

    ``cashtag`` is a ticker ('AAPL' or '$AAPL'). ``fetcher`` is injectable
    (``fetcher(argv, timeout) -> bytes``) for offline tests; the default shells
    out to ``twitter search "$<TAG>" -n <limit> --json``. Returns
    (items, latency_seconds). NEVER raises — a missing/dead CLI returns
    ([], latency) (silence-by-default).

    Each emitted CatalystItem has source ``"twitter/<TAG>"`` and published_at =
    the post's REAL ``created_at`` (tz-aware UTC). A post with a missing/
    unparseable created_at is SKIPPED — never fabricated to now() (no-lookahead
    anchor; the same rule as ``ingest_reddit``).
    """
    tag = _norm_cashtag(cashtag)
    argv = ["twitter", "search", f"${tag}", "-n", str(int(limit)), "--json"]
    fetch = fetcher or _reach_cli
    t0 = time.monotonic()
    try:
        raw = fetch(argv, timeout)
    except Exception as e:  # noqa: BLE001 — absent/dead CLI is non-fatal
        logger.warning("catalyst.social: twitter agent-reach failed for $%s: %s", tag, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    src = f"twitter/{tag}"
    items: list[CatalystItem] = []
    for rec in _reach_records(raw, keys=("results", "tweets", "data", "posts")):
        title = _reach_text(rec)
        pub = _reach_created_at(rec)  # camelCase (twitter-cli createdAt/createdAtISO) + snake_case + epoch
        if not title or pub is None:
            continue  # no body, or no parseable timestamp to anchor packet.asof
        items.append(CatalystItem(
            title=title,
            published_at=pub,
            source=src,
            link=_reach_link(rec),
            query=f"twitter:${tag}",
        ))
    if dedupe:
        items = dedupe_items(items)
    return items, latency


# StockTwits public streams JSON endpoint. codex P2: Agent-Reach ships NO
# stocktwits channel (its documented channels are web/twitter/reddit/rss/youtube/
# bilibili/xiaohongshu/linkedin/v2ex/xueqiu/...), so a `stocktwits ...` CLI would
# always FileNotFoundError and silently yield nothing. Use the documented public
# stream API directly (an HTTP GET, like the reddit RSS path), so the StockTwits
# half of the independent-origin convergence evidence can actually appear when
# armed. No API key for the public symbol stream; rate-limited (fail-soft on 429).
_STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"


def ingest_stocktwits(
    symbol: str,
    *,
    limit: int = 50,
    timeout: float = _REACH_TIMEOUT,
    fetcher=None,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest recent StockTwits messages for a symbol via the public stream API.

    Mirrors ``ingest_reddit`` (HTTP GET), NOT a CLI — Agent-Reach has no
    stocktwits channel. ``fetcher`` is injectable (``fetcher(url, timeout) ->
    bytes``) for offline tests; the default ``_fetch_raw`` GETs the public
    ``streams/symbol/<SYM>.json`` endpoint. source = ``"stocktwits/<SYM>"``;
    published_at = the message's REAL ``created_at`` (tz-aware UTC) — StockTwits
    nests messages under ``messages`` with a top-level ``created_at`` RFC form. A
    message with no parseable timestamp is SKIPPED (never now()). NEVER raises
    (a 429 / dead endpoint -> ([], latency), silence-by-default).
    """
    sym = _norm_cashtag(symbol)
    url = _STOCKTWITS_STREAM.format(sym=urllib.parse.quote(sym))
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 — rate-limit / dead endpoint is non-fatal
        logger.warning("catalyst.social: stocktwits stream failed for %s: %s", sym, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    src = f"stocktwits/{sym}"
    items: list[CatalystItem] = []
    # StockTwits stream nests posts under "messages"; tolerate the generic shapes too.
    for rec in _reach_records(raw, keys=("messages", "results", "data", "posts"))[: int(limit)]:
        title = _reach_text(rec)
        pub = _reach_created_at(rec)
        if not title or pub is None:
            continue
        items.append(CatalystItem(
            title=title,
            published_at=pub,
            source=src,
            link=_reach_link(rec),
            query=f"stocktwits:{sym}",
        ))
    if dedupe:
        items = dedupe_items(items)
    return items, latency


def ingest_social_reach(
    symbols,
    *,
    limit: int = 50,
    timeout: float = _REACH_TIMEOUT,
    twitter_fetcher=None,
    stocktwits_fetcher=None,
    max_age_days: float | None = None,
    now: datetime | None = None,
) -> list[CatalystItem]:
    """Orchestrate the Agent-Reach social producers across a symbol set.

    DEFAULT-OFF: returns ``[]`` immediately unless HERMES_QUANT_SOCIAL_REACH=1
    (read at call time via ``_social_reach_on``). With the flag OFF the
    twitter/stocktwits ingesters are NEVER invoked — the live catalyst run is
    byte-identical to the news/reddit path. ON => pulls Twitter + StockTwits per
    symbol, concatenates, applies the same recency gate as ``ingest_social``
    (asof honesty: filters by each item's real published_at, never shifts one),
    and cross-source dedupes. Never raises (each producer fails soft to []).

    The two producers have DIFFERENT fetcher shapes — Twitter shells out to the
    agent-reach CLI (``fetcher(argv, timeout)``) while StockTwits HTTP-GETs the
    public stream (``fetcher(url, timeout)``) — so they take SEPARATE injectable
    fetchers (default None => each picks its own correct default). The Twitter
    CLI runs ``shell=False`` with a fixed argv vector (no string interpolation).
    """
    if not _social_reach_on():
        return []
    all_items: list[CatalystItem] = []
    for raw_sym in symbols or ():
        sym = _norm_cashtag(raw_sym)
        if not sym:
            continue
        tw, lat_tw = ingest_twitter(sym, limit=limit, timeout=timeout, fetcher=twitter_fetcher, dedupe=True)
        st, lat_st = ingest_stocktwits(sym, limit=limit, timeout=timeout, fetcher=stocktwits_fetcher, dedupe=True)
        logger.info(
            "catalyst.social: agent-reach %s -> %d twitter (%.2fs) + %d stocktwits (%.2fs)",
            sym, len(tw), lat_tw, len(st), lat_st,
        )
        all_items.extend(tw)
        all_items.extend(st)
    all_items = _filter_by_recency(all_items, max_age_days=max_age_days, now=now)
    return dedupe_items(all_items)
