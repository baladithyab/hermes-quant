"""tests/catalyst/test_social_reach_ob5.py — seed aegis-ob5.

Agent-Reach social-sentiment/velocity PERCEPTION provider tests. ALL offline:
the agent-reach CLI is operator-installed and absent in CI, so every test injects
a fake fetcher that returns canned bytes (NO network, NO subprocess).

The contract mirrors ``ingest_reddit`` exactly:
  * injectable-fetcher producer (``fetcher=None`` defaults to the real shell-out)
  * returns ``(list[CatalystItem], latency_seconds)``
  * NEVER raises — a dead CLI returns ``([], latency)``
  * anchors each item's ``published_at`` on the post's REAL ``created_at``
    (tz-aware UTC) and SKIPS any post with no parseable timestamp (no-lookahead
    anchor — never fabricates a now() asof)

It also covers the load-bearing perception enhancement: registering ``twitter``
and ``stocktwits`` as DISTINCT independent source families so that
reddit + twitter + stocktwits can form the >=2 independent origins
``validate_convergence`` requires.
"""
from __future__ import annotations

import datetime as dt
import json

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.social import ingest_stocktwits, ingest_twitter
from hermes_quant.perception.convergence import source_family, validate_convergence

_NOW = dt.datetime(2026, 6, 17, 18, 0, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# canned agent-reach CLI payloads (the shape the injected fetcher returns)
# --------------------------------------------------------------------------- #
def _twitter_payload() -> bytes:
    """3 tweets in the REAL twitter-cli serialization shape (codex P2 fix): the
    timestamp lands under camelCase ``createdAt`` / ``createdAtISO`` (see
    twitter_cli/serialization.py::tweet_to_dict), NOT snake_case ``created_at``.
    The fixture used to use ``created_at`` which masked that the live backend
    would drop every post — the test now exercises the real field names."""
    return json.dumps(
        {
            "results": [
                {
                    "id": "1",
                    "text": "$AAPL ripping today, calls printing",
                    "createdAtISO": "2026-06-17T15:30:00+00:00",
                    "url": "https://x.com/u/status/1",
                },
                {
                    "id": "2",
                    "text": "loading up on $AAPL again",
                    "createdAt": "2026-06-17T16:00:00Z",
                    "url": "https://x.com/u/status/2",
                },
                {
                    "id": "3",
                    "text": "market-wide bounce, $AAPL leading",
                    "createdAtISO": "2026-06-17T16:45:00+00:00",
                    "url": "https://x.com/u/status/3",
                },
            ]
        }
    ).encode("utf-8")


def _stocktwits_payload() -> bytes:
    """2 StockTwits messages on AAPL with real created_at."""
    return json.dumps(
        {
            "messages": [
                {
                    "id": 100,
                    "body": "AAPL breaking out, watching $AAPL closely",
                    "created_at": "2026-06-17T15:45:00Z",
                    "symbol": "AAPL",
                },
                {
                    "id": 101,
                    "body": "added more AAPL",
                    "created_at": "2026-06-17T16:20:00+00:00",
                    "symbol": "AAPL",
                },
            ]
        }
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# ingest_twitter — happy path
# --------------------------------------------------------------------------- #
def test_twitter_three_posts_real_timestamps():
    payload = _twitter_payload()

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, latency = ingest_twitter("AAPL", limit=10, fetcher=fake_fetch)
    assert len(items) == 3
    assert all(isinstance(it, CatalystItem) for it in items)
    # source tag is "twitter/<cashtag>" (prefix matters for source_family)
    assert all(it.source == "twitter/AAPL" for it in items)
    # published_at is the REAL created_at, tz-aware UTC — NOT now()
    pubs = sorted(it.published_at for it in items)
    assert pubs[0] == dt.datetime(2026, 6, 17, 15, 30, tzinfo=dt.UTC)
    assert pubs[-1] == dt.datetime(2026, 6, 17, 16, 45, tzinfo=dt.UTC)
    assert all(it.published_at.tzinfo is not None for it in items)
    assert latency >= 0.0


def test_twitter_reads_camelcase_createdat_fields_codex_p2():
    """codex P2 regression: the REAL twitter-cli backend serializes the timestamp
    as camelCase ``createdAt`` / ``createdAtISO`` (twitter_cli/serialization.py),
    NOT snake_case ``created_at``. The first cut read only created_at/created, so
    EVERY real tweet dropped (pub=None -> skipped). Pin both camelCase forms.
    RED-proof: revert _reach_created_at to read only created_at/created -> 0 items."""
    payload = json.dumps(
        {
            "results": [
                {"id": "1", "text": "$AAPL a", "createdAt": "2026-06-17T15:30:00Z"},
                {"id": "2", "text": "$AAPL b", "createdAtISO": "2026-06-17T16:00:00+00:00"},
            ]
        }
    ).encode("utf-8")

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, _lat = ingest_twitter("AAPL", limit=10, fetcher=fake_fetch)
    # BOTH camelCase forms parse -> 2 items survive (0 if the reader only knew snake_case).
    assert len(items) == 2
    assert {it.published_at for it in items} == {
        dt.datetime(2026, 6, 17, 15, 30, tzinfo=dt.UTC),
        dt.datetime(2026, 6, 17, 16, 0, tzinfo=dt.UTC),
    }


def test_stocktwits_real_timestamps():
    payload = _stocktwits_payload()

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, _lat = ingest_stocktwits("AAPL", limit=10, fetcher=fake_fetch)
    assert len(items) == 2
    assert all(it.source == "stocktwits/AAPL" for it in items)
    pubs = sorted(it.published_at for it in items)
    assert pubs[0] == dt.datetime(2026, 6, 17, 15, 45, tzinfo=dt.UTC)
    assert pubs[-1] == dt.datetime(2026, 6, 17, 16, 20, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# no-lookahead anchor: a post with NO/garbage timestamp is SKIPPED (not now())
# --------------------------------------------------------------------------- #
def test_twitter_skips_post_with_no_timestamp():
    payload = json.dumps(
        {
            "results": [
                {"id": "1", "text": "$AAPL good", "createdAt": "2026-06-17T15:30:00Z"},
                {"id": "2", "text": "$AAPL no ts"},  # MISSING timestamp entirely
                {"id": "3", "text": "$AAPL garbage ts", "createdAt": "not-a-date"},
            ]
        }
    ).encode("utf-8")

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, _lat = ingest_twitter("AAPL", limit=10, fetcher=fake_fetch)
    # only the ONE post with a parseable created_at survives — the missing and the
    # garbage timestamp posts are SKIPPED, never fabricated to now(). If the
    # producer defaulted a missing/garbage ts to now(), this would be 3, not 1.
    assert len(items) == 1
    assert items[0].published_at == dt.datetime(2026, 6, 17, 15, 30, tzinfo=dt.UTC)


def test_stocktwits_skips_post_with_no_timestamp():
    payload = json.dumps(
        {
            "messages": [
                {"id": 1, "body": "AAPL up", "created_at": "2026-06-17T15:45:00Z"},
                {"id": 2, "body": "AAPL no ts"},  # MISSING
            ]
        }
    ).encode("utf-8")

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, _lat = ingest_stocktwits("AAPL", limit=10, fetcher=fake_fetch)
    assert len(items) == 1


def test_twitter_never_fabricates_now_for_any_item():
    """Belt-and-suspenders: NO surviving item may carry a wall-clock-now asof.

    Every emitted published_at must equal one of the canned created_at values.
    """
    payload = _twitter_payload()

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    before = dt.datetime.now(dt.UTC)
    items, _lat = ingest_twitter("AAPL", limit=10, fetcher=fake_fetch)
    allowed = {
        dt.datetime(2026, 6, 17, 15, 30, tzinfo=dt.UTC),
        dt.datetime(2026, 6, 17, 16, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 6, 17, 16, 45, tzinfo=dt.UTC),
    }
    for it in items:
        assert it.published_at in allowed
        # none of the canned timestamps is "now" (they are 2026-06-17 fixed)
        assert it.published_at < before


# --------------------------------------------------------------------------- #
# silence-by-default: a dead fetcher (raises) -> ([], latency), never propagates
# --------------------------------------------------------------------------- #
def test_twitter_dead_fetcher_returns_empty():
    def dead_fetch(args, timeout):  # noqa: ARG001
        raise RuntimeError("agent-reach CLI not installed")

    items, latency = ingest_twitter("AAPL", limit=10, fetcher=dead_fetch)
    assert items == []
    assert latency >= 0.0


def test_stocktwits_dead_fetcher_returns_empty():
    def dead_fetch(args, timeout):  # noqa: ARG001
        raise FileNotFoundError("twitter: command not found")

    items, latency = ingest_stocktwits("AAPL", limit=10, fetcher=dead_fetch)
    assert items == []
    assert latency >= 0.0


def test_twitter_garbage_payload_returns_empty():
    """A CLI that returns non-JSON garbage -> ([], latency), never raises."""

    def garbage_fetch(args, timeout):  # noqa: ARG001
        return b"<<<not json at all>>>"

    items, _lat = ingest_twitter("AAPL", limit=10, fetcher=garbage_fetch)
    assert items == []


# --------------------------------------------------------------------------- #
# the convergence enhancement: twitter + stocktwits are DISTINCT origins
# --------------------------------------------------------------------------- #
def test_source_family_registers_twitter_and_stocktwits():
    assert source_family("twitter/AAPL") == "twitter"
    assert source_family("stocktwits/AAPL") == "stocktwits"
    # they are NOT collapsed into news/unknown
    assert source_family("twitter/AAPL") not in {"news_rss", "unknown"}
    assert source_family("stocktwits/AAPL") not in {"news_rss", "unknown"}


def _reddit_item() -> CatalystItem:
    return CatalystItem(
        title="AAPL chatter on r/stocks",
        published_at=_NOW,
        source="reddit/r/stocks (rss)",
        link="n/a",
        query="reddit",
    )


def _twitter_item() -> CatalystItem:
    return CatalystItem(
        title="$AAPL ripping", published_at=_NOW, source="twitter/AAPL", link="n/a", query="twitter"
    )


def _stocktwits_item() -> CatalystItem:
    return CatalystItem(
        title="AAPL breakout",
        published_at=_NOW,
        source="stocktwits/AAPL",
        link="n/a",
        query="stocktwits",
    )


def test_convergence_reddit_twitter_stocktwits_three_independent():
    """reddit + twitter + stocktwits => 3 INDEPENDENT origins => validated.

    RED-prove: WITHOUT the source_family registration, twitter/stocktwits map to
    news_rss (one family) -> only reddit + news_rss = 2 origins at best, and the
    distinct twitter/stocktwits independence would be lost.
    """
    items = [_reddit_item(), _twitter_item(), _stocktwits_item()]
    r = validate_convergence(items)
    assert r.validated
    assert r.n_independent >= 2
    assert r.n_independent == 3
    assert set(r.families) == {"reddit", "twitter", "stocktwits"}


def test_convergence_twitter_alone_does_not_collapse_to_news():
    """twitter + stocktwits ALONE (no reddit) are still 2 independent origins."""
    items = [_twitter_item(), _stocktwits_item()]
    r = validate_convergence(items)
    assert r.validated and r.n_independent == 2


def test_twitter_two_posts_same_origin_not_two_families():
    """Ten twitter posts != convergence (one origin)."""
    items = [_twitter_item() for _ in range(5)]
    r = validate_convergence(items)
    assert not r.validated and r.n_independent == 1


# --------------------------------------------------------------------------- #
# default-OFF: HERMES_QUANT_SOCIAL_REACH unset -> live path never calls ingesters
# --------------------------------------------------------------------------- #
def test_social_reach_flag_default_off(monkeypatch):
    """With the flag unset, the live catalyst path must NOT invoke the new
    ingesters (byte-identical to the news/reddit path)."""
    import hermes_quant.catalyst.social as social

    monkeypatch.delenv("HERMES_QUANT_SOCIAL_REACH", raising=False)
    assert social._social_reach_on() is False
    # the gate helper is the single decision point; OFF => ingesters never called.
    monkeypatch.setenv("HERMES_QUANT_SOCIAL_REACH", "0")
    assert social._social_reach_on() is False
    monkeypatch.setenv("HERMES_QUANT_SOCIAL_REACH", "1")
    assert social._social_reach_on() is True


def test_social_reach_ingesters_not_called_when_flag_off(monkeypatch):
    """Drive the orchestration helper with the flag OFF and assert the new
    twitter/stocktwits ingesters are never invoked (no item, no fetch)."""
    import hermes_quant.catalyst.social as social

    monkeypatch.delenv("HERMES_QUANT_SOCIAL_REACH", raising=False)
    called = {"n": 0}

    def tripwire(*a, **k):  # noqa: ARG001
        called["n"] += 1
        return [], 0.0

    monkeypatch.setattr(social, "ingest_twitter", tripwire)
    monkeypatch.setattr(social, "ingest_stocktwits", tripwire)
    items = social.ingest_social_reach({"AAPL"})
    assert items == []
    assert called["n"] == 0


def test_social_reach_ingesters_called_when_flag_on(monkeypatch):
    """With the flag ON, ingest_social_reach pulls twitter + stocktwits per symbol.

    Twitter + StockTwits have DIFFERENT fetcher shapes (CLI argv vs HTTP url) and
    DIFFERENT real field names (twitter createdAt camelCase; stocktwits messages/
    created_at), so the orchestrator takes separate injectable fetchers. Each
    fixture uses its REAL backend shape (codex P2 fix)."""
    import hermes_quant.catalyst.social as social

    monkeypatch.setenv("HERMES_QUANT_SOCIAL_REACH", "1")

    def fake_twitter(argv, timeout):  # noqa: ARG001 — CLI-shaped: (argv, timeout)
        return json.dumps(
            {"results": [{"id": "1", "text": "$AAPL up", "createdAt": "2026-06-17T15:30:00Z"}]}
        ).encode("utf-8")

    def fake_stocktwits(url, timeout):  # noqa: ARG001 — HTTP-shaped: (url, timeout)
        return json.dumps(
            {"messages": [{"id": 1, "body": "AAPL up", "created_at": "2026-06-17T15:31:00Z"}]}
        ).encode("utf-8")

    items = social.ingest_social_reach(
        {"AAPL"}, twitter_fetcher=fake_twitter, stocktwits_fetcher=fake_stocktwits
    )
    srcs = {it.source for it in items}
    assert "twitter/AAPL" in srcs
    assert "stocktwits/AAPL" in srcs


# --------------------------------------------------------------------------- #
# dedupe + cashtag/symbol extraction
# --------------------------------------------------------------------------- #
def test_twitter_dedupe_collapses_near_duplicates():
    payload = json.dumps(
        {
            "results": [
                {"id": "1", "text": "$AAPL ripping today", "created_at": "2026-06-17T15:30:00Z"},
                {"id": "2", "text": "$AAPL ripping today", "created_at": "2026-06-17T16:00:00Z"},
            ]
        }
    ).encode("utf-8")

    def fake_fetch(args, timeout):  # noqa: ARG001
        return payload

    items, _ = ingest_twitter("AAPL", limit=10, fetcher=fake_fetch, dedupe=True)
    assert len(items) == 1  # identical titles collapse; earliest survives
    assert items[0].published_at == dt.datetime(2026, 6, 17, 15, 30, tzinfo=dt.UTC)
