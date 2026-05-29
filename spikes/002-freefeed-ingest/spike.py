#!/usr/bin/env python3
"""Spike 002 — free-feed ingest (no paid API).

QUESTION: Given Google News RSS + a curated RSS set, can we pull timestamped,
deduped, parseable catalyst items for a sector/symbol query at acceptable
latency/coverage with ZERO paid API?

Specifically: would a query-driven Google News RSS pull have surfaced the
Blue Origin explosion story (and thus let the butterfly engine fire) using
only free, public endpoints and Python stdlib?

Throwaway: stdlib only (urllib + xml.etree), no feedparser, no API keys.
"""
from __future__ import annotations

import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (catalyst-sense-spike; research)"

# Google News RSS query endpoint (free, public, no key).
GN_BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Queries a catalyst ingester would run: per-sector + per-watchlist-symbol.
QUERIES = {
    "space-sector":   '(Blue Origin OR Rocket Lab OR SpaceX OR "space stocks") when:7d',
    "rklb-symbol":    'Rocket Lab RKLB when:7d',
    "energy-sector":  '(OPEC OR "oil prices" OR crude) when:2d',
    "semis-sector":   '(TSMC OR Nvidia OR "chip supply") when:2d',
}


def fetch_gn(query: str, timeout: float = 15.0) -> tuple[list[dict], float]:
    """Fetch + parse a Google News RSS search. Returns (items, latency_s)."""
    url = GN_BASE.format(q=urllib.parse.quote(query))
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    latency = time.monotonic() - t0
    root = ET.fromstring(raw)
    items = []
    # RSS 2.0: channel/item/{title,link,pubDate,source}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        items.append({"title": title, "pubDate": pub, "link": link, "source": source})
    return items, latency


def parse_pubdate(s: str):
    # RFC 822: "Thu, 28 May 2026 22:14:00 GMT"
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def dedupe(items: list[dict], thresh: float = 0.6) -> list[dict]:
    kept = []
    for it in items:
        if any(jaccard(it["title"], k["title"]) >= thresh for k in kept):
            continue
        kept.append(it)
    return kept


def main() -> int:
    print("=" * 70)
    print("SPIKE 002 — free-feed ingest (Google News RSS, stdlib only)")
    print("=" * 70)
    total_items = 0
    blue_origin_hits = 0
    latencies = []
    for name, q in QUERIES.items():
        print(f"\n--- query: {name} ---")
        print(f"  q = {q}")
        try:
            items, lat = fetch_gn(q)
        except Exception as e:
            print(f"  FETCH FAILED: {e}")
            continue
        latencies.append(lat)
        deduped = dedupe(items)
        total_items += len(deduped)
        print(f"  fetched {len(items)} items ({len(deduped)} after dedupe) in {lat:.2f}s")
        # show top 4 with parsed timestamps
        for it in deduped[:4]:
            dt = parse_pubdate(it["pubDate"])
            ts = dt.isoformat() if dt else "UNPARSED"
            print(f"    [{ts}] {it['title'][:70]}  ({it['source']})")
        # count Blue Origin coverage in the space query
        if name == "space-sector":
            for it in deduped:
                t = it["title"].lower()
                if "blue origin" in t or "new glenn" in t:
                    blue_origin_hits += 1

    print("\n" + "=" * 70)
    print("SUMMARY")
    print(f"  total deduped items across queries: {total_items}")
    print(f"  Blue Origin / New Glenn items in space query: {blue_origin_hits}")
    if latencies:
        print(f"  latency: min={min(latencies):.2f}s avg={sum(latencies)/len(latencies):.2f}s max={max(latencies):.2f}s")
    print(f"  paid APIs used: 0")
    print("  VERDICT: see README.md")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
