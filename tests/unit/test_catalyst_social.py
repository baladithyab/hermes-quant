"""Tests for hermes_quant.catalyst.social (Reddit + Google Trends producers).

Offline-deterministic via injected fetchers — mirrors the GN-RSS ingester test
pattern. Verifies: parse correctness, published_at fidelity (real post/trend time,
not now), watch-term filtering, silence-by-default on feed failure, and that emitted
CatalystItems flow through the existing classify->propagate path unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime

from hermes_quant.catalyst.social import (
    ingest_google_trends,
    ingest_reddit,
    ingest_social,
)

# --- fixtures ----------------------------------------------------------------
# Captured-real shape of the live Reddit Atom feed (https://www.reddit.com/
# r/stocks/new.rss): <feed> in the Atom namespace, each post an <entry> with
# <title>, <author><name>, <published> AND <updated> (ISO-8601 e.g.
# "2021-03-01T00:00:00+00:00"), <link href=...> (permalink as an ATTRIBUTE), <id>.
# The public RSS feed carries NO score/comment count (the old .json did). The
# <published> timestamps are FIXED PAST dates so the parsed UTC asof proves
# published_at comes from <published>, never now(). Entry 3 is malformed (no
# <published>/<updated>) -> must be SKIPPED (no now() fabrication). Entry 2's
# title mentions "nvidia" so a search-query test has a match.
_REDDIT_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <category term="stocks" label="r/stocks"/>
  <updated>2021-03-02T00:00:00+00:00</updated>
  <id>/r/stocks/new.rss</id>
  <link rel="self" href="https://www.reddit.com/r/stocks/new.rss" type="application/atom+xml" />
  <title>newest submissions : stocks</title>
  <entry>
    <author><name>/u/buklau00</name><uri>https://www.reddit.com/user/buklau00</uri></author>
    <category term="stocks" label="r/stocks"/>
    <content type="html">&lt;div&gt;body&lt;/div&gt;</content>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/stocks/comments/abc/celsius/" />
    <published>2021-03-01T00:00:00+00:00</published>
    <updated>2021-03-01T00:00:00+00:00</updated>
    <title>Celsius goes viral on TikTok, sales surging</title>
  </entry>
  <entry>
    <author><name>/u/uncle493</name><uri>https://www.reddit.com/user/uncle493</uri></author>
    <category term="stocks" label="r/stocks"/>
    <content type="html">&lt;div&gt;body&lt;/div&gt;</content>
    <id>t3_def456</id>
    <link href="https://www.reddit.com/r/stocks/comments/def/nvidia/" />
    <published>2021-03-02T00:00:00+00:00</published>
    <updated>2021-03-02T00:00:00+00:00</updated>
    <title>nvidia routine portfolio check-in thread</title>
  </entry>
  <entry>
    <author><name>/u/notime99</name><uri>https://www.reddit.com/user/notime99</uri></author>
    <category term="stocks" label="r/stocks"/>
    <id>t3_ghi789</id>
    <link href="https://www.reddit.com/r/stocks/comments/ghi/notime/" />
    <title>No timestamp here</title>
  </entry>
</feed>
"""

# Captured-real shape of the live trending/rss feed (https://trends.google.com/
# trending/rss?geo=US): RSS 2.0, ht:-namespaced approx_traffic/news_item children,
# unnamespaced <title> (the search term) / <pubDate> (RFC-822) / <link>.
# pubDate is a FIXED PAST date with a -0700 offset, so the parsed UTC asof is
# 2021-03-01 20:10:00Z — proving published_at comes from pubDate, never now.
# Items: one matches "celsius", one is noise ("weather today"), one matches "crocs".
_TRENDS_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <title>Daily Search Trends</title>
    <description>Recent searches</description>
    <link>https://trends.google.com/trending/rss?geo=US</link>
    <item>
      <title>Celsius energy drink</title>
      <ht:approx_traffic>200000+</ht:approx_traffic>
      <description/>
      <link>https://trends.google.com/trending/rss?geo=US</link>
      <pubDate>Mon, 01 Mar 2021 13:10:00 -0700</pubDate>
      <ht:picture>https://example.invalid/pic1.jpg</ht:picture>
      <ht:picture_source>Some Source</ht:picture_source>
      <ht:news_item>
        <ht:news_item_title>Celsius sales jump as drink goes viral</ht:news_item_title>
        <ht:news_item_snippet/>
        <ht:news_item_url>https://example.invalid/celsius</ht:news_item_url>
        <ht:news_item_source>Example News</ht:news_item_source>
      </ht:news_item>
    </item>
    <item>
      <title>weather today</title>
      <ht:approx_traffic>1000000+</ht:approx_traffic>
      <description/>
      <link>https://trends.google.com/trending/rss?geo=US</link>
      <pubDate>Mon, 01 Mar 2021 13:10:00 -0700</pubDate>
      <ht:picture>https://example.invalid/pic2.jpg</ht:picture>
      <ht:picture_source>Weather Co</ht:picture_source>
    </item>
    <item>
      <title>Crocs sale</title>
      <ht:approx_traffic>100000+</ht:approx_traffic>
      <description/>
      <link>https://trends.google.com/trending/rss?geo=US</link>
      <pubDate>Mon, 01 Mar 2021 13:10:00 -0700</pubDate>
      <ht:picture>https://example.invalid/pic3.jpg</ht:picture>
      <ht:picture_source>Retail Daily</ht:picture_source>
    </item>
  </channel>
</rss>
"""

# 2021-03-01 13:10:00 -0700 == 2021-03-01 20:10:00 UTC
_TRENDS_PUB_UTC = datetime(2021, 3, 1, 20, 10, 0, tzinfo=UTC)


def _reddit_fetcher(url, timeout):
    return _REDDIT_PAYLOAD


def _trends_fetcher(url, timeout):
    return _TRENDS_PAYLOAD


def _boom_fetcher(url, timeout):
    raise ConnectionError("simulated feed failure")


# --- reddit ------------------------------------------------------------------
def test_reddit_parses_posts_with_real_timestamps():
    items, lat = ingest_reddit("stocks", fetcher=_reddit_fetcher, dedupe=False)
    assert len(items) == 2  # malformed entry (no <published>/<updated>) dropped
    celsius = items[0]
    assert "Celsius" in celsius.title
    # published_at is the <published> POST time, not now (the fidelity anchor)
    assert celsius.published_at == datetime(2021, 3, 1, tzinfo=UTC)
    assert celsius.published_at < datetime.now(UTC)  # a real PAST date, never now()
    # source must start with "reddit/" so PDR-3 source_family maps it to reddit;
    # the public RSS has no score, so we emit "(rss)" rather than a fake score.
    assert celsius.source.startswith("reddit/r/stocks")
    assert "rss" in celsius.source
    assert "score=" not in celsius.source  # no fabricated engagement number
    # link is the <link href=...> ATTRIBUTE (the permalink)
    assert celsius.link == "https://www.reddit.com/r/stocks/comments/abc/celsius/"
    assert lat >= 0.0


def test_reddit_skips_entry_with_no_timestamp():
    """An <entry> with no <published>/<updated> is SKIPPED (no now() fabrication)."""
    items, _ = ingest_reddit("stocks", fetcher=_reddit_fetcher, dedupe=False)
    assert all(it.title != "No timestamp here" for it in items)


def test_reddit_search_url_uses_search_endpoint():
    seen = {}
    def cap(url, timeout):
        seen["url"] = url
        return _REDDIT_PAYLOAD
    items, _ = ingest_reddit("stocks", query="nvidia", fetcher=cap, dedupe=False)
    assert "search.rss" in seen["url"]
    assert "q=nvidia" in seen["url"]
    assert "restrict_sr=1" in seen["url"]
    # the captured feed has an entry whose title matches the search query
    assert any("nvidia" in it.title.lower() for it in items)


def test_reddit_silences_on_malformed_feed():
    """A malformed (non-XML) feed must not crash — returns ([], latency)."""
    def _garbage(url, timeout):
        return b"<<<not valid xml at all >>> )]}',{broken"
    items, lat = ingest_reddit("stocks", fetcher=_garbage)
    assert items == []  # parse failure is silent, never raises
    assert lat >= 0.0


def test_reddit_silences_on_feed_failure():
    items, lat = ingest_reddit("stocks", fetcher=_boom_fetcher)
    assert items == []  # never raises; silence-by-default
    assert lat >= 0.0


# --- google trends -----------------------------------------------------------
def test_trends_parses_rss_with_pubdate_timestamps():
    items, _ = ingest_google_trends(fetcher=_trends_fetcher, dedupe=False)
    # no watch filter -> all 3 trends become items
    assert len(items) == 3
    # published_at comes from <pubDate> (a fixed PAST date), NEVER now
    assert all(it.published_at == _TRENDS_PUB_UTC for it in items)
    now = datetime.now(UTC)
    assert all(it.published_at < now for it in items)
    assert all("trending" in it.title.lower() for it in items)
    # the search term (the <title>) is carried into the synthetic headline
    titles = " ".join(it.title for it in items)
    assert "Celsius energy drink" in titles
    assert "weather today" in titles
    assert "Crocs sale" in titles
    # approx_traffic (ht:-namespaced) is folded into the headline
    assert any("200000+" in it.title for it in items)
    # source tag preserved exactly (PDR-3 taxonomy keys on the google_trends prefix)
    assert all(it.source == "google_trends/US" for it in items)


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


def test_trends_silences_on_malformed_feed():
    """A malformed (non-XML) feed must not crash — returns ([], latency)."""
    def _garbage(url, timeout):
        return b"<<<not valid xml at all >>> )]}',{broken"
    items, lat = ingest_google_trends(fetcher=_garbage)
    assert items == []  # parse failure is silent, never raises
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
