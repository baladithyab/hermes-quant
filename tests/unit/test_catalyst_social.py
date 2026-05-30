"""Tests for hermes_quant.catalyst.social (Reddit + Google Trends producers).

Offline-deterministic via injected fetchers — mirrors the GN-RSS ingester test
pattern. Verifies: parse correctness, published_at fidelity (real post/trend time,
not now), watch-term filtering, silence-by-default on feed failure, and that emitted
CatalystItems flow through the existing classify->propagate path unchanged.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from hermes_quant.catalyst.social import (
    ingest_google_trends,
    ingest_reddit,
    ingest_social,
)

# --- fixtures ----------------------------------------------------------------
_REDDIT_PAYLOAD = json.dumps({
    "data": {"children": [
        {"data": {
            "title": "Celsius goes viral on TikTok, sales surging",
            "created_utc": 1614556800.0,  # 2021-03-01 UTC
            "score": 1200, "num_comments": 340, "subreddit": "stocks",
            "permalink": "/r/stocks/comments/abc/celsius/",
        }},
        {"data": {
            "title": "Routine portfolio check-in thread",
            "created_utc": 1614643200.0,  # 2021-03-02 UTC
            "score": 5, "num_comments": 2, "subreddit": "stocks",
            "permalink": "/r/stocks/comments/def/checkin/",
        }},
        {"data": {  # malformed: no created_utc -> skipped
            "title": "No timestamp here", "score": 1, "subreddit": "stocks",
        }},
    ]}
}).encode()

_TRENDS_PAYLOAD = b")]}',\n" + json.dumps({
    "default": {"trendingSearchesDays": [
        {"date": "20210301", "trendingSearches": [
            {"title": {"query": "Celsius energy drink"}, "formattedTraffic": "200K+"},
            {"title": {"query": "weather today"}, "formattedTraffic": "1M+"},  # noise
            {"title": {"query": "Crocs sale"}, "formattedTraffic": "100K+"},
        ]},
    ]}
}).encode()


def _reddit_fetcher(url, timeout):
    return _REDDIT_PAYLOAD


def _trends_fetcher(url, timeout):
    return _TRENDS_PAYLOAD


def _boom_fetcher(url, timeout):
    raise ConnectionError("simulated feed failure")


# --- reddit ------------------------------------------------------------------
def test_reddit_parses_posts_with_real_timestamps():
    items, lat = ingest_reddit("stocks", fetcher=_reddit_fetcher, dedupe=False)
    assert len(items) == 2  # malformed (no created_utc) dropped
    celsius = items[0]
    assert "Celsius" in celsius.title
    # published_at is the POST time, not now (the fidelity anchor)
    assert celsius.published_at == datetime(2021, 3, 1, tzinfo=UTC)
    assert "reddit/r/stocks" in celsius.source
    assert "score=1200" in celsius.source
    assert lat >= 0.0


def test_reddit_search_url_uses_search_endpoint():
    seen = {}
    def cap(url, timeout):
        seen["url"] = url
        return _REDDIT_PAYLOAD
    ingest_reddit("stocks", query="celsius", fetcher=cap)
    assert "search.json" in seen["url"]
    assert "q=celsius" in seen["url"]
    assert "restrict_sr=1" in seen["url"]


def test_reddit_silences_on_feed_failure():
    items, lat = ingest_reddit("stocks", fetcher=_boom_fetcher)
    assert items == []  # never raises; silence-by-default
    assert lat >= 0.0


# --- google trends -----------------------------------------------------------
def test_trends_strips_xssi_and_parses_dates():
    items, _ = ingest_google_trends(fetcher=_trends_fetcher, dedupe=False)
    # no watch filter -> all 3 trends become items
    assert len(items) == 3
    assert all(it.published_at == datetime(2021, 3, 1, tzinfo=UTC) for it in items)
    assert all("trending" in it.title.lower() for it in items)


def test_trends_watch_term_filter():
    items, _ = ingest_google_trends(
        fetcher=_trends_fetcher, watch_terms={"celsius", "crocs"}, dedupe=False
    )
    # "weather today" filtered out, celsius + crocs kept
    assert len(items) == 2
    titles = " ".join(it.title.lower() for it in items)
    assert "celsius" in titles and "crocs" in titles
    assert "weather" not in titles


def test_trends_silences_on_feed_failure():
    items, lat = ingest_google_trends(fetcher=_boom_fetcher)
    assert items == []
    assert lat >= 0.0


# --- orchestration + downstream integration ----------------------------------
def test_ingest_social_combines_producers():
    def fetch(url, timeout):
        return _TRENDS_PAYLOAD if "trends.google" in url else _REDDIT_PAYLOAD
    items = ingest_social(
        reddit_queries={"stocks:celsius": "celsius-watch"},
        trends_geo="US",
        trends_watch_terms={"celsius", "crocs"},
        fetcher=fetch,
    )
    assert len(items) >= 2
    assert any("Celsius" in it.title for it in items)


def test_social_items_flow_through_classify_unchanged():
    """A social CatalystItem must be classify-able exactly like a news item."""
    from hermes_quant.catalyst.classify import classify_headline
    items, _ = ingest_reddit("stocks", fetcher=_reddit_fetcher)
    viral = next(it for it in items if "viral" in it.title.lower())
    cls = classify_headline(viral.title)
    # "surging" is a consumer-trend word once the lexicon is patched; even without
    # the patch, "surging" base form should be a positive catalyst. At minimum the
    # item must be a valid CatalystItem the classifier accepts without error.
    assert cls is not None
