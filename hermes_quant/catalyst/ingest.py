"""hermes_quant.catalyst.ingest — free-feed catalyst ingestion (ADR-0074, D74.5).

Stdlib-only Google News RSS ingester (spike 002 validated: 0 paid API, sub-second
latency, 83 Blue Origin items on the space query). Pulls query-driven catalyst
items with parseable publication timestamps.

No feedparser dependency — Google News RSS is RSS 2.0, parsed with xml.etree.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (hermes-quant catalyst-sense; research)"
_GN_BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class CatalystItem:
    """One ingested news item, normalized."""

    title: str
    published_at: datetime  # tz-aware UTC — the fidelity anchor (becomes packet.asof)
    source: str
    link: str
    query: str = ""  # which ingest query surfaced it (provenance)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "published_at": self.published_at.isoformat(),
            "source": self.source,
            "link": self.link,
            "query": self.query,
        }


def _parse_pubdate(s: str) -> datetime | None:
    """Parse an RFC-822 pubDate to tz-aware UTC. Returns None on failure."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def dedupe_items(items: list[CatalystItem], thresh: float = 0.6) -> list[CatalystItem]:
    """Collapse near-duplicate syndicated copies by title Jaccard similarity."""
    kept: list[CatalystItem] = []
    for it in items:
        if any(_jaccard(it.title, k.title) >= thresh for k in kept):
            continue
        kept.append(it)
    return kept


def _fetch_raw(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed GN host
        return resp.read()


def parse_gn_rss(raw: bytes, *, query: str = "") -> list[CatalystItem]:
    """Parse a Google News RSS payload into CatalystItems. Skips unparseable items."""
    items: list[CatalystItem] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("catalyst.ingest: RSS parse error: %s", e)
        return items
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = _parse_pubdate(item.findtext("pubDate") or "")
        if not title or pub is None:
            continue  # an item without a parseable timestamp can't anchor a packet
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        items.append(CatalystItem(title=title, published_at=pub, source=source,
                                  link=link, query=query))
    return items


def ingest_query(
    query: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest one Google News RSS query. Returns (items, latency_seconds).

    ``fetcher`` is injectable (``fetcher(url, timeout) -> bytes``) for offline
    tests. On any network/parse failure returns ([], latency) — never raises
    (silence-by-default; a dead feed must not break the daily run).
    """
    url = _GN_BASE.format(q=urllib.parse.quote(query))
    fetch = fetcher or _fetch_raw
    t0 = time.monotonic()
    try:
        raw = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 - feed failure is non-fatal
        logger.warning("catalyst.ingest: fetch failed for %r: %s", query, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    items = parse_gn_rss(raw, query=query)
    if dedupe:
        items = dedupe_items(items)
    return items, latency


def ingest_queries(
    queries: dict[str, str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    fetcher=None,
) -> list[CatalystItem]:
    """Ingest a {name: query} map, concatenate + cross-query dedupe. Never raises."""
    all_items: list[CatalystItem] = []
    for name, q in queries.items():
        items, lat = ingest_query(q, timeout=timeout, fetcher=fetcher, dedupe=True)
        logger.info("catalyst.ingest: %s -> %d items in %.2fs", name, len(items), lat)
        all_items.extend(items)
    return dedupe_items(all_items)
