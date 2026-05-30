"""Tests for hermes_quant.catalyst (ADR-0074 Catalyst Sense Phase 1).

Covers all five stages + the eval gate, fully offline (injected fetchers, no
network). The Blue Origin case (spike 001) is the precision fixture; benign
headlines are the negative control.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from hermes_quant.catalyst.classify import classify_headline, polarity_sign
from hermes_quant.catalyst.eval import EvalCase, eval_gate, run_negative_control, run_precision
from hermes_quant.catalyst.ingest import (
    CatalystItem,
    dedupe_items,
    ingest_query,
    parse_gn_rss,
)
from hermes_quant.catalyst.propagation import (
    PropagationEdge,
    extract_entities,
    load_graph,
    propagate,
)
from hermes_quant.catalyst.synthesize import (
    load_packets_for,
    synthesize_packets,
    write_packets,
)

UTC = timezone.utc
PUB = datetime(2026, 5, 28, 22, 14, tzinfo=UTC)


def _item(title: str, when: datetime = PUB, source="Test", link="http://x") -> CatalystItem:
    return CatalystItem(title=title, published_at=when, source=source, link=link)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

_SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Blue Origin's New Glenn rocket explodes during hotfire test</title>
    <link>https://news.google.com/x1</link>
    <pubDate>Thu, 28 May 2026 22:14:00 GMT</pubDate>
    <source url="http://reuters.com">Reuters</source>
  </item>
  <item>
    <title>Blue Origin New Glenn explodes during test</title>
    <link>https://news.google.com/x2</link>
    <pubDate>Thu, 28 May 2026 22:30:00 GMT</pubDate>
    <source url="http://barrons.com">Barron's</source>
  </item>
  <item>
    <title>Item with no date should be skipped</title>
    <link>https://news.google.com/x3</link>
  </item>
</channel></rss>"""


def test_parse_gn_rss_skips_undated():
    items = parse_gn_rss(_SAMPLE_RSS, query="space")
    assert len(items) == 2  # the dateless item is dropped
    assert items[0].published_at.tzinfo is not None
    assert items[0].source == "Reuters"


def test_dedupe_collapses_near_duplicates():
    items = parse_gn_rss(_SAMPLE_RSS)
    deduped = dedupe_items(items, thresh=0.6)
    assert len(deduped) == 1  # the two near-identical Blue Origin titles collapse


def test_ingest_query_injected_fetcher():
    def fake_fetch(url, timeout):
        assert "news.google.com" in url
        return _SAMPLE_RSS
    items, latency = ingest_query("anything", fetcher=fake_fetch)
    assert len(items) == 1  # deduped
    assert latency >= 0.0


def test_ingest_query_fetch_failure_is_silent():
    def boom(url, timeout):
        raise ConnectionError("network down")
    items, _ = ingest_query("q", fetcher=boom)
    assert items == []  # never raises


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_negative_catalyst():
    c = classify_headline("Blue Origin New Glenn rocket explodes during test")
    assert c.polarity == "negative"
    assert c.is_catalyst
    assert c.severity >= 0.04  # 'explodes' is critical-tier
    assert polarity_sign(c.polarity) == -1


def test_classify_positive_catalyst():
    c = classify_headline("Rocket Lab soars after record contract win")
    assert c.polarity == "positive"
    assert polarity_sign(c.polarity) == 1


def test_classify_neutral_non_catalyst():
    c = classify_headline("Rocket Lab reports quarterly results in line with estimates")
    assert c.polarity == "neutral"
    assert not c.is_catalyst
    assert c.severity == 0.0


def test_classify_severity_is_max_not_sum():
    # co-occurring synonyms must not inflate severity past the critical tier
    c = classify_headline("rocket explodes in massive blast, destroyed on pad")
    assert c.severity <= 0.06


# ---------------------------------------------------------------------------
# propagation (butterfly engine)
# ---------------------------------------------------------------------------

def test_extract_entities_longest_alias_wins():
    _, aliases = load_graph()
    ents = extract_entities("Jeff Bezos's Blue Origin suffers anomaly", aliases)
    assert "blue origin" in ents


def test_propagate_negative_catalyst_bearish_basket():
    graph, aliases = load_graph()
    ents = extract_entities("Blue Origin New Glenn explodes", aliases)
    results = propagate(ents, catalyst_sign=-1, graph=graph)
    # spike 001 basket
    for sym in ("RKLB", "LUNR", "ASTS"):
        assert sym in results
        assert results[sym].stance == "bearish"
        assert 0.0 < results[sym].confidence <= 1.0


def test_propagate_neutral_catalyst_no_results():
    graph, aliases = load_graph()
    ents = extract_entities("Blue Origin New Glenn", aliases)
    assert propagate(ents, catalyst_sign=0, graph=graph) == {}


def test_propagate_logs_every_edge():
    graph, aliases = load_graph()
    ents = extract_entities("Blue Origin explodes", aliases)
    log: list[dict] = []
    propagate(ents, -1, graph, log=log)
    assert len(log) >= 3
    assert all("symbol" in e and "effect_sign" in e for e in log)


def test_propagate_positive_catalyst_flips_sign():
    graph, aliases = load_graph()
    ents = extract_entities("Blue Origin New Glenn soars after breakthrough", aliases)
    # positive catalyst on Blue Origin -> competitors bullish-flip (effect_sign negated)
    results = propagate(ents, catalyst_sign=1, graph=graph)
    assert results["RKLB"].stance == "bullish"


# ---------------------------------------------------------------------------
# expanded multi-sector graph (ADR-0074 graph expansion) + false-correlation guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headline,expect_symbol", [
    ("Boeing 737 grounded after mid-air door blowout", "BA"),
    ("Tesla recalls 2 million vehicles over safety defect", "TSLA"),
    ("Regional bank collapse sparks contagion fears", "BAC"),
    ("TSMC fab halted after Taiwan earthquake", "TSM"),
])
def test_expanded_sectors_fire(headline, expect_symbol):
    pkts = synthesize_packets([_item(headline)])
    assert any(p.asset == expect_symbol and p.stance == "bearish" for p in pkts), \
        f"{expect_symbol} not bearish-flagged: {[(p.asset, p.stance) for p in pkts]}"


def test_no_person_alias_cross_sector_contamination():
    # A SpaceX/Musk rocket event must NOT bleed into Tesla/EV — person-aliases
    # were removed precisely to avoid this false correlation.
    pkts = synthesize_packets([_item("Elon Musk's SpaceX Starship explodes during launch test")])
    syms = {p.asset for p in pkts}
    assert not (syms & {"TSLA", "RIVN", "LCID"}), f"EV contamination: {sorted(syms)}"
    # it SHOULD still tag the space basket via the 'spacex' alias
    assert "RKLB" in syms


def test_expanded_negative_control():
    benign = [
        _item("Tesla holds annual shareholder meeting"),
        _item("Boeing delivers planes on schedule this quarter"),
        _item("JPMorgan reports earnings in line with estimates"),
        _item("TSMC opens new training center"),
    ]
    res = run_negative_control(benign)
    assert res.passed, f"spurious: {res.spurious}"


# ---------------------------------------------------------------------------
# synthesize -> SemanticPacket (asof = pub time)
# ---------------------------------------------------------------------------

def test_synthesize_sets_asof_to_publication_time():
    packets = synthesize_packets([_item("Blue Origin New Glenn explodes")])
    assert packets
    for p in packets:
        # asof MUST equal publication time, never now()
        assert pd.Timestamp(p.asof) == pd.Timestamp(PUB)


def test_synthesize_confidence_vs_magnitude_separation():
    packets = synthesize_packets([_item("Blue Origin New Glenn explodes")])
    rklb = next(p for p in packets if p.asset == "RKLB")
    # confidence from linkage; magnitude from severity — distinct sources
    assert 0.0 < rklb.confidence <= 1.0
    assert 0.0 < rklb.magnitude <= 0.06
    assert rklb.stance == "bearish"


def test_synthesize_neutral_headline_no_packets():
    packets = synthesize_packets([_item("Blue Origin reports quarterly results in line")])
    assert packets == []


def test_synthesize_unknown_entity_no_packets():
    packets = synthesize_packets([_item("Some unrelated company explodes in popularity")])
    # 'explodes' fires polarity but no known entity -> no propagation
    assert packets == []


# ---------------------------------------------------------------------------
# packet store roundtrip + lookahead via load_packets_for
# ---------------------------------------------------------------------------

def test_load_packets_for_respects_lookahead(tmp_path):
    store = tmp_path / "packets.jsonl"
    packets = synthesize_packets([_item("Blue Origin New Glenn explodes")])
    write_packets(packets, path=store)

    # BEFORE publication -> nothing returned (future_packet rejected)
    pre = load_packets_for("RKLB", "2026-05-28T20:00:00+00:00", path=store)
    assert pre == []

    # AFTER publication, fresh -> returned
    post = load_packets_for("RKLB", "2026-05-29T13:30:00+00:00", path=store)
    assert len(post) == 1
    assert post[0]["asset"] == "RKLB"

    # stale (3 days later) -> rejected
    stale = load_packets_for("RKLB", "2026-05-31T22:14:00+00:00", path=store)
    assert stale == []


def test_load_packets_for_missing_store(tmp_path):
    assert load_packets_for("RKLB", "2026-05-29T13:30:00+00:00", path=tmp_path / "nope.jsonl") == []


def test_load_packets_for_collapses_duplicates(tmp_path):
    # Many syndicated headlines about one event -> many packets for RKLB.
    store = tmp_path / "packets.jsonl"
    items = [
        _item("Blue Origin New Glenn explodes during test"),
        _item("Blue Origin rocket explodes on launchpad"),
        _item("Bezos Blue Origin suffers anomaly, rocket destroyed"),
    ]
    write_packets(synthesize_packets(items), path=store)
    # collapse (default) -> at most one packet per stance for RKLB
    collapsed = load_packets_for("RKLB", "2026-05-29T13:30:00+00:00", path=store)
    stances = [p["stance"] for p in collapsed]
    assert len(collapsed) == len(set(stances))  # one per stance
    assert all(s == "bearish" for s in stances)
    # uncollapsed -> all of them
    raw = load_packets_for("RKLB", "2026-05-29T13:30:00+00:00", path=store, collapse=False)
    assert len(raw) >= len(collapsed)


# ---------------------------------------------------------------------------
# decision_asof: live news validates against decision time, not bar time
# ---------------------------------------------------------------------------

def test_semantic_analyst_decision_asof_vs_bar_asof():
    """A packet published today must be REJECTED against a yesterday bar-asof
    (backtest safety) but ACCEPTED when decision_asof=now is supplied (live)."""
    from datetime import timedelta
    from hermes_quant.analysts.semantic import HermesSemanticAnalyst
    from hermes_quant.protocol import MarketContext

    now = datetime(2026, 5, 29, 18, 0, tzinfo=UTC)
    packet = synthesize_packets([_item("Blue Origin New Glenn explodes",
                                        when=datetime(2026, 5, 29, 14, 0, tzinfo=UTC))])
    rklb = next(p.to_dict(include_hash=True) for p in packet if p.asset == "RKLB")
    analyst = HermesSemanticAnalyst()

    bar_asof = now - timedelta(days=1)  # last daily bar = yesterday
    _bars = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    # bar-time only -> packet is "future" relative to yesterday -> abstain
    ctx_bar = MarketContext(asset="RKLB", timeframe="1d", asset_class="equity",
                            exchange=None, bars=_bars, last_close=10.0, last_volume=1.0,
                            asof=pd.Timestamp(bar_asof),
                            extras={"semantic_packets": [rklb]})
    v_bar = analyst.analyze(ctx_bar)
    assert v_bar.direction == 0  # abstained (future_packet vs bar time)

    # decision_asof=now -> packet is in the past relative to decision -> used
    ctx_live = MarketContext(asset="RKLB", timeframe="1d", asset_class="equity",
                             exchange=None, bars=_bars, last_close=10.0, last_volume=1.0,
                             asof=pd.Timestamp(bar_asof),
                             extras={"semantic_packets": [rklb], "decision_asof": now.isoformat()})
    v_live = analyst.analyze(ctx_live)
    assert v_live.direction == -1  # bearish semantic view now contributes


# ---------------------------------------------------------------------------
# eval gate — negative control + precision
# ---------------------------------------------------------------------------

_BENIGN = [
    _item("Rocket Lab reports quarterly results in line with estimates"),
    _item("Blue Origin schedules routine maintenance window"),
    _item("Analysts discuss the space sector outlook for next year"),
    _item("Market opens flat as investors await data"),
]


def test_negative_control_no_spurious_packets():
    res = run_negative_control(_BENIGN)
    assert res.passed, f"spurious flags: {res.spurious}"
    assert res.n_spurious_packets == 0


def test_precision_blue_origin_case():
    # The real spike-001 outcome: explosion -> these symbols fell.
    cases = [
        EvalCase(_item("Blue Origin New Glenn rocket explodes"), "RKLB", -3.07),
        EvalCase(_item("Blue Origin New Glenn rocket explodes"), "LUNR", -4.09),
        EvalCase(_item("Blue Origin New Glenn rocket explodes"), "ASTS", -14.79),
    ]
    res = run_precision(cases, min_hit_rate=0.6)
    assert res.passed
    assert res.hit_rate == 1.0  # 3/3 directional


def test_eval_gate_combined():
    cases = [
        EvalCase(_item("Blue Origin New Glenn rocket explodes"), "RKLB", -3.07),
        EvalCase(_item("Blue Origin New Glenn rocket explodes"), "LUNR", -4.09),
    ]
    passed, neg, prec = eval_gate(_BENIGN, cases, min_hit_rate=0.6)
    assert passed
    assert neg.passed and prec.passed


# ---------------------------------------------------------------------------
# analyst loadout wiring (ADR-0074): env-gated, default OFF
# ---------------------------------------------------------------------------

def test_semantic_analyst_off_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_SEMANTIC_ENABLED", raising=False)
    from hermes_quant.advisor import _build_default_analysts
    names = [type(a).__name__ for a in _build_default_analysts()]
    assert "HermesSemanticAnalyst" not in names


def test_semantic_analyst_on_when_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    from hermes_quant.advisor import _build_default_analysts
    names = [type(a).__name__ for a in _build_default_analysts()]
    assert "HermesSemanticAnalyst" in names
    # still a PEER — the numerical analysts are present alongside it
    assert "ClassicalTAAnalyst" in names


# ===========================================================================
# Deep-review regression suite (2026-05-29) — four latent bugs found by audit.
# Each test fails against the pre-fix code and passes after the fix.
# ===========================================================================

# --- BUG B: substring false-positives in classify (word-boundary fix) ---

import pytest as _pytest


@_pytest.mark.parametrize("headline", [
    "SpaceX completes successful mission to ISS",   # 'miss' in 'mission'
    "Company dismisses CEO amid restructuring",     # 'miss' in 'dismisses'
    "New emissions standards announced for autos",  # 'miss' in 'emissions'
    "Apple unveils ideal new product lineup",       # 'deal' in 'ideal'
])
def test_classify_no_substring_false_positive(headline):
    """Short catalyst words must not match as substrings of benign words."""
    c = classify_headline(headline)
    assert not c.is_catalyst, f"{headline!r} wrongly classified {c.polarity} via {c.matched_terms}"


def test_classify_word_boundary_still_matches_real_tokens():
    """The boundary fix must not break legitimate standalone-token matches."""
    assert classify_headline("Stock misses earnings estimates").polarity == "negative"
    assert classify_headline("Company strikes deal with partner").polarity == "positive"
    assert classify_headline("Tesla recall affects 50000 vehicles").polarity == "negative"
    assert classify_headline("Boeing 737 grounded after inspection").polarity == "negative"


@_pytest.mark.parametrize("headline,expect", [
    ("Hyundai is recalling nearly 600,000 vehicles", "negative"),   # recalling
    ("Volkswagen recalled 44,000 electric vehicles", "negative"),   # recalled
    ("Blue Origin rocket mishap during launch", "negative"),        # mishap outweighs launch
    ("Stock plunged on weak guidance", "negative"),                 # plunged
    ("Shares tumbled after the report", "negative"),                # tumbled
    ("Firm downgraded by analysts", "negative"),                    # downgraded
    ("Earnings missed expectations badly", "negative"),             # missed
    ("Company soared on the news", "positive"),                     # soared
    ("Bank stock sank on contagion fears", "negative"),             # sank + contagion
])
def test_classify_inflected_catalyst_forms_fire(headline, expect):
    """Inflected forms (recalling/plunged/missed/soared/mishap) must classify.

    Regression for the 2026-05-29 live finding: the word-boundary fix initially
    dropped 'recalling'/'recalled' (vehicle recalls — exactly the EV catalysts we
    want) and a rocket 'mishap' read POSITIVE because only 'launch' fired. The
    fix enumerates inflections explicitly rather than reverting to substring.
    """
    assert classify_headline(headline).polarity == expect


# --- BUG C: _parse_pubdate mishandled named timezones ---

def test_parse_pubdate_named_timezones():
    """PST/EST must convert correctly (not be silently treated as UTC or dropped)."""
    from hermes_quant.catalyst.ingest import _parse_pubdate
    # 14:30 PST == 22:30 UTC (pre-fix: wrongly 14:30 UTC)
    pst = _parse_pubdate("Wed, 27 May 2026 14:30:00 PST")
    assert pst is not None and pst.hour == 22 and pst.minute == 30
    # 14:30 EST == 19:30 UTC (pre-fix: returned None -> item dropped)
    est = _parse_pubdate("Wed, 27 May 2026 14:30:00 EST")
    assert est is not None and est.hour == 19
    # numeric offset
    off = _parse_pubdate("Sat, 31 May 2026 09:00:00 -0400")
    assert off is not None and off.hour == 13
    # GMT (the common GN form) unchanged
    gmt = _parse_pubdate("Thu, 28 May 2026 21:05:00 GMT")
    assert gmt is not None and gmt.hour == 21
    # garbage still None
    assert _parse_pubdate("garbage") is None


# --- BUG A: dedup must be deterministic (earliest-published survives) ---

def test_dedupe_keeps_earliest_published_deterministically():
    """Near-dup cluster -> earliest published_at survives regardless of input order."""
    early = _item("Blue Origin New Glenn explodes during hotfire test",
                  when=datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc))
    late = _item("Blue Origin New Glenn explodes during hotfire test at Cape",
                 when=datetime(2026, 5, 28, 22, 0, tzinfo=timezone.utc))
    for order in ([early, late], [late, early]):
        kept = dedupe_items(order, thresh=0.6)
        assert len(kept) == 1
        assert kept[0].published_at.hour == 21, "earliest copy must survive in both orders"


# --- BUG D: confidence must reflect directional agreement, not just linkage ---

def test_propagate_conflicting_edges_collapse_confidence():
    """Opposing-sign edges that near-cancel must NOT emit a high-confidence packet."""
    graph = {
        "entA": [PropagationEdge("entA", "SYM", "competitor", -1, 0.80)],
        "entB": [PropagationEdge("entB", "SYM", "competitor", +1, 0.75)],
    }
    res = propagate({"entA", "entB"}, -1, graph)["SYM"]
    # signed = -0.05 (coin-flip net); confidence must collapse toward 0, not stay ~0.95
    assert res.confidence < 0.10, f"near-cancel net emitted conf={res.confidence}"


def test_propagate_agreeing_edges_preserve_confidence():
    """All-agree edges keep the noisy-OR linkage (agreement factor == 1.0)."""
    # single edge: confidence == weight
    g1 = {"e": [PropagationEdge("e", "SYM", "x", -1, 0.85)]}
    assert propagate({"e"}, -1, g1)["SYM"].confidence == _pytest.approx(0.85, abs=1e-3)
    # two agreeing edges: noisy-OR(0.6, 0.5) = 0.80, agreement 1.0
    g2 = {"a": [PropagationEdge("a", "SYM", "x", -1, 0.6)],
          "b": [PropagationEdge("b", "SYM", "x", -1, 0.5)]}
    assert propagate({"a", "b"}, -1, g2)["SYM"].confidence == _pytest.approx(0.80, abs=1e-3)
