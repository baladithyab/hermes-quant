"""Tests for hermes_quant.catalyst.openbb_news (aegis-ob4, ADR-0100).

Offline / deterministic — NO live network and NO openbb installed. ``OpenBBNews``
takes an injected ``obb`` seam (a fake exposing ``.news.company`` / ``.news.world``)
so the cardinal no-lookahead, default-OFF, and pipeline-feed tests run WITHOUT
the ``openbb`` SDK being importable.

OpenBBNews is an ALTERNATIVE headline source: it maps ``obb.news.*`` rows into
the SAME ``CatalystItem`` shape ``catalyst.ingest`` produces, so the downstream
``classify_headline`` -> ``propagate`` -> ``synthesize_packets`` path consumes
OpenBB news identically to the Google-News RSS feed (it does NOT replace it).

Covers (per the ob4 seed):
  1. NO-LOOKAHEAD WINDOW (cardinal): a story published > asof is DROPPED; a
     story published exactly AT asof is KEPT; a row with no parseable timestamp
     is DROPPED (can't anchor a packet — never defaulted to now()).
  2. DEFAULT-OFF byte-identical: HERMES_QUANT_OPENBB unset -> no openbb import
     (poisoned-import sentinel proves the lazy import is not reached); the
     catalyst pipeline run over an EMPTY OpenBB feed is unchanged.
  3. PIPELINE FEED: OpenBBNews items flow through the real classify -> propagate
     -> synthesize_packets path and produce the same packets a hand-built RSS
     CatalystItem would.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.openbb_news import (
    NEWS_LOOKBACK_DAYS,
    OPENBB_ENABLE_FLAG,
    OpenBBNews,
    ingest_openbb_news,
)
from hermes_quant.catalyst.propagation import PropagationEdge
from hermes_quant.catalyst.synthesize import synthesize_packets
from hermes_quant.protocol import DataProviderError


# ---------------------------------------------------------------------------
# Synthetic OpenBB news fixtures.
#
# obb.news.company(symbol=...) / obb.news.world() returns an OBBject whose
# .to_dataframe()/.results yield rows with `title`, `date` (publish instant),
# `url`, `source`. Tests inject a fake exposing both endpoints.
# ---------------------------------------------------------------------------
class _FakeOBBject:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    @property
    def results(self) -> list[dict]:
        return self._rows


class _FakeNewsObb:
    def __init__(self, rows: list[dict]):
        self.company_calls: list[dict] = []
        self.world_calls: list[dict] = []
        outer = self

        class _News:
            def company(self, **kwargs: Any) -> _FakeOBBject:
                outer.company_calls.append(kwargs)
                return _FakeOBBject(rows)

            def world(self, **kwargs: Any) -> _FakeOBBject:
                outer.world_calls.append(kwargs)
                return _FakeOBBject(rows)

        self.news = _News()


def _news(rows: list[dict]) -> OpenBBNews:
    return OpenBBNews(obb=_FakeNewsObb(rows), require_flag=False)


# ===========================================================================
# 1. NO-LOOKAHEAD WINDOW (cardinal)
# ===========================================================================
def test_news_published_after_asof_dropped():
    """A story published AFTER asof is DROPPED (no-lookahead window).

    RED-proof: without the published <= asof filter the 2026-06-20 story leaks
    into a 2026-06-15 read — len would be 2 not 1, and the future title would
    surface.
    """
    rows = [
        {"title": "Rocket Lab launch succeeds", "date": "2026-06-12T14:00:00Z",
         "url": "http://x/1", "source": "Reuters"},
        {"title": "FUTURE leak headline", "date": "2026-06-20T09:00:00Z",
         "url": "http://x/2", "source": "Reuters"},  # after asof
    ]
    news = _news(rows)
    items = news.fetch("rocket lab", symbol="RKLB",
                       as_of=datetime(2026, 6, 15, tzinfo=UTC))

    titles = {it.title for it in items}
    assert "FUTURE leak headline" not in titles
    assert titles == {"Rocket Lab launch succeeds"}
    assert len(items) == 1


def test_news_published_at_asof_kept():
    """A story published EXACTLY at asof is KEPT (boundary <=)."""
    rows = [
        {"title": "boundary headline", "date": "2026-06-15T00:00:00Z",
         "url": "http://x/1", "source": "Reuters"},
    ]
    news = _news(rows)
    items = news.fetch("q", symbol="RKLB",
                       as_of=datetime(2026, 6, 15, tzinfo=UTC))
    assert len(items) == 1
    assert items[0].published_at == datetime(2026, 6, 15, tzinfo=UTC)


def test_news_unparseable_timestamp_dropped():
    """A row with no parseable publish timestamp is DROPPED (can't anchor a
    packet — never defaulted to now())."""
    rows = [
        {"title": "no date", "date": "", "url": "http://x/1", "source": "Reuters"},
        {"title": "bad date", "date": "not-a-date", "url": "http://x/2", "source": "Reuters"},
        {"title": "good", "date": "2026-06-10T12:00:00Z", "url": "http://x/3", "source": "Reuters"},
    ]
    news = _news(rows)
    items = news.fetch("q", as_of=datetime(2026, 6, 15, tzinfo=UTC))
    assert {it.title for it in items} == {"good"}


def test_news_company_vs_world_routing():
    """symbol -> news.company; no symbol -> news.world."""
    rows = [{"title": "x", "date": "2026-06-10T00:00:00Z", "url": "u", "source": "s"}]
    fake = _FakeNewsObb(rows)
    news = OpenBBNews(obb=fake, require_flag=False)

    news.fetch("q", symbol="RKLB", as_of=datetime(2026, 6, 15, tzinfo=UTC))
    assert fake.company_calls and fake.company_calls[0]["symbol"] == "RKLB"
    assert not fake.world_calls

    news.fetch("q", as_of=datetime(2026, 6, 15, tzinfo=UTC))
    assert fake.world_calls


def test_news_outbound_call_is_asof_window_pinned():
    """4430: OpenBB news calls include start_date/end_date, not just post-filtering."""
    rows = [{"title": "x", "date": "2026-06-10T00:00:00Z", "url": "u", "source": "s"}]
    fake = _FakeNewsObb(rows)
    news = OpenBBNews(obb=fake, require_flag=False)

    asof = datetime(2026, 6, 15, 18, 30, tzinfo=UTC)
    news.fetch("q", symbol="RKLB", as_of=asof)

    assert fake.company_calls == [
        {
            "symbol": "RKLB",
            "start_date": "2026-05-16",
            "end_date": "2026-06-15",
        }
    ]
    assert NEWS_LOOKBACK_DAYS == 30


def test_news_world_call_is_asof_window_pinned():
    rows = [{"title": "x", "date": "2026-06-10T00:00:00Z", "url": "u", "source": "s"}]
    fake = _FakeNewsObb(rows)
    news = OpenBBNews(obb=fake, require_flag=False)

    news.fetch("q", as_of=datetime(2026, 6, 15, tzinfo=UTC))

    assert fake.world_calls == [{"start_date": "2026-05-16", "end_date": "2026-06-15"}]


def test_news_asof_less_read_hard_rejected():
    """4430: no as_of means latest-only semantics and must not touch OpenBB."""
    rows = [{"title": "x", "date": "2026-06-10T00:00:00Z", "url": "u", "source": "s"}]
    fake = _FakeNewsObb(rows)
    news = OpenBBNews(obb=fake, require_flag=False)

    with pytest.raises(DataProviderError) as ei:
        news.fetch("q")

    assert "latest-only" in str(ei.value).lower()
    assert fake.company_calls == []
    assert fake.world_calls == []


# ===========================================================================
# 2. DEFAULT-OFF byte-identical (no openbb import; pipeline unchanged)
# ===========================================================================
def test_default_off_does_not_import_openbb(monkeypatch):
    """With HERMES_QUANT_OPENBB unset, a fetch fails-closed at the flag gate
    BEFORE the lazy openbb import.

    RED-proof: poison the `openbb` import. Constructing OpenBBNews must NOT
    trigger it; a flag-off fetch raises the FLAG error ("disabled"), NOT the
    poisoned-import error — proving the import was never reached.
    """
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    class _Poison:
        def __getattr__(self, name):  # pragma: no cover - any access explodes
            raise ImportError("SENTINEL: openbb import was triggered")

    monkeypatch.setitem(sys.modules, "openbb", _Poison())

    news = OpenBBNews()  # require_flag defaults True
    with pytest.raises(DataProviderError) as ei:
        news.fetch("q", as_of=datetime(2026, 6, 15, tzinfo=UTC))
    msg = str(ei.value)
    assert "disabled" in msg.lower()
    assert OPENBB_ENABLE_FLAG in msg
    assert "SENTINEL" not in msg
    assert "not installed" not in msg.lower()


def test_ingest_wrapper_non_fatal_when_flag_off(monkeypatch):
    """The public ingest_openbb_news wrapper is NON-FATAL: flag-off yields no
    items (the existing feed + pipeline run unchanged), never crashes.

    RED-proof: a poisoned openbb import would explode if the wrapper let the
    DataProviderError escape; instead it returns ([], latency)."""
    monkeypatch.delenv(OPENBB_ENABLE_FLAG, raising=False)

    class _Poison:
        def __getattr__(self, name):  # pragma: no cover
            raise ImportError("SENTINEL: openbb import was triggered")

    monkeypatch.setitem(sys.modules, "openbb", _Poison())

    items, latency = ingest_openbb_news(
        "q", as_of=datetime(2026, 6, 15, tzinfo=UTC)
    )
    assert items == []
    assert latency >= 0.0


def test_pipeline_unchanged_over_empty_openbb_feed():
    """Synthesizing over the RSS items + an EMPTY OpenBB feed == RSS items
    alone (the alternative source ADDS nothing when empty)."""
    graph = {"rocket lab": [PropagationEdge("rocket lab", "RKLB", "self", -1, 0.9)]}
    aliases = {"rocket lab": "rocket lab"}
    rss_item = CatalystItem(
        title="Rocket Lab plunges on launch failure",
        published_at=datetime(2026, 6, 10, 12, tzinfo=UTC),
        source="GoogleNews",
        link="http://rss/1",
        query="rss",
    )

    base = synthesize_packets([rss_item], graph=graph, aliases=aliases)

    # Empty OpenBB feed merged in -> identical packets.
    empty_openbb, _ = ingest_openbb_news(
        "rocket lab", as_of=datetime(2026, 6, 15, tzinfo=UTC),
        obb=_FakeNewsObb([]), require_flag=False,
    )
    merged = synthesize_packets([rss_item, *empty_openbb], graph=graph, aliases=aliases)

    assert empty_openbb == []
    assert [(p.asset, p.stance, p.asof) for p in base] == [
        (p.asset, p.stance, p.asof) for p in merged
    ]
    assert base, "the RSS catalyst should produce at least one packet"


# ===========================================================================
# 3. PIPELINE FEED — OpenBBNews items flow through classify->propagate->synth
# ===========================================================================
def test_openbb_news_feeds_synthesize_like_rss():
    """An OpenBBNews CatalystItem produces the SAME packets a hand-built RSS
    item with the identical (title, published_at) would — proving the OpenBB
    feed plugs into the existing classify -> propagate -> synthesize path."""
    graph = {"rocket lab": [
        PropagationEdge("rocket lab", "RKLB", "self", -1, 0.9),
        PropagationEdge("rocket lab", "LUNR", "sector_member", -1, 0.4),
    ]}
    aliases = {"rocket lab": "rocket lab"}

    pub = "2026-06-10T12:00:00Z"
    rows = [{"title": "Rocket Lab plunges on launch failure",
             "date": pub, "url": "http://obb/1", "source": "Reuters"}]

    # OpenBBNews path.
    obb_items, _ = ingest_openbb_news(
        "rocket lab", symbol="RKLB",
        as_of=datetime(2026, 6, 15, tzinfo=UTC),
        obb=_FakeNewsObb(rows), require_flag=False,
    )
    assert len(obb_items) == 1
    obb_packets = synthesize_packets(obb_items, graph=graph, aliases=aliases)

    # Equivalent hand-built RSS item.
    rss_item = CatalystItem(
        title="Rocket Lab plunges on launch failure",
        published_at=datetime(2026, 6, 10, 12, tzinfo=UTC),
        source="Reuters", link="http://obb/1", query="rss",
    )
    rss_packets = synthesize_packets([rss_item], graph=graph, aliases=aliases)

    assert obb_packets, "OpenBB news should drive at least one packet through the pipeline"
    # The two feeds produce equivalent (asset, stance, asof) packets.
    obb_key = sorted((p.asset, p.stance, p.asof) for p in obb_packets)
    rss_key = sorted((p.asset, p.stance, p.asof) for p in rss_packets)
    assert obb_key == rss_key
    # asof is the publication instant (the fidelity anchor), not now().
    assert all(p.asof == "2026-06-10T12:00:00+00:00" for p in obb_packets)
