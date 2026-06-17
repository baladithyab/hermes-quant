"""Regression: semantic analyst freshest-packet selection must be asof-PARSED,
not a lexical string compare.

Family: time-ordering / latest-wins money bug. ``HermesSemanticAnalyst._select_packet``
sorts the valid packets by ``packet.asof`` (a *string*) and returns ``valid[-1]``
as the freshest. ``validate_semantic_packet`` accepts ANY ``pd.Timestamp``-parseable
format, so a packet log can legitimately hold MIXED tz formats: synthesize.py emits
``...+00:00`` (catalyst/synthesize.py:228) while a model/human-authored packet may
use ``Z`` or a non-UTC offset (e.g. ``-06:00``). A lexical compare on those mixed
strings mis-orders them, so the analyst can return a STALE packet as "freshest" and
emit the WRONG trading direction / confidence (a money-decision flip).

These tests fail RED on a lexical sort and pass GREEN once the selection parses the
asof before comparing. Single-format inputs (the common case + every existing test)
must remain byte-identical.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.analysts.semantic import HermesSemanticAnalyst
from hermes_quant.protocol import MarketContext
from hermes_quant.semantic import semantic_packet_from_dict


def _ctx(extras, *, asof="2024-01-02T00:00:00Z"):
    ts = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100] * 5,
            "high": [101] * 5,
            "low": [99] * 5,
            "close": [100] * 5,
            "volume": [1000] * 5,
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="kraken",
        bars=bars,
        last_close=100.0,
        last_volume=1000.0,
        asof=pd.Timestamp(asof),
        extras=extras,
    )


def _packet(**overrides):
    payload = {
        "schema_version": 1,
        "asset": "BTC/USDT",
        "asof": "2024-01-01T23:00:00Z",
        "horizon": "1h",
        "stance": "bullish",
        "confidence": 0.75,
        "magnitude": 0.012,
        "summary": "Hermes research packet.",
        "sources": [{"type": "note", "ref": "unit-test"}],
        "model": "hermes:test-model",
    }
    payload.update(overrides)
    return semantic_packet_from_dict(payload).to_dict()


def test_freshest_packet_when_offset_format_inverts_lexical_order():
    """Fresher packet carries a non-UTC offset that sorts BEFORE the older +00:00.

    old: 2024-01-01T10:30:00+00:00  == 10:30 UTC  (bearish, conf 0.9)
    new: 2024-01-01T05:00:00-06:00  == 11:00 UTC  (bullish, conf 0.7) -- TRULY fresher

    Lexically 'T05' < 'T10', so a string sort returns the older bearish packet as
    valid[-1] -> direction -1. Parsed order returns the bullish packet -> +1.
    """
    analyst = HermesSemanticAnalyst()
    old = _packet(asof="2024-01-01T10:30:00+00:00", stance="bearish", confidence=0.9)
    new = _packet(asof="2024-01-01T05:00:00-06:00", stance="bullish", confidence=0.7)

    # Sanity: the TRUTH is that `new` is the later instant.
    assert pd.Timestamp("2024-01-01T05:00:00-06:00").tz_convert("UTC") > pd.Timestamp(
        "2024-01-01T10:30:00+00:00"
    ).tz_convert("UTC")

    view = analyst.analyze(_ctx({"semantic_packets": [old, new]}))
    assert view.direction == 1, "must trade the genuinely-fresher bullish packet"
    assert view.confidence_raw == 0.7
    assert view.metadata["packet_asof"] == "2024-01-01T05:00:00-06:00"

    # Order-independence: shuffling the input list must not change the winner.
    view2 = analyst.analyze(_ctx({"semantic_packets": [new, old]}))
    assert view2.direction == 1
    assert view2.metadata["packet_asof"] == "2024-01-01T05:00:00-06:00"


def test_freshest_packet_space_vs_t_separator():
    """A space-separated asof and a 'T'-separated asof are both pd.Timestamp-parseable.

    ' ' (0x20) sorts before 'T' (0x54), so the older space-separated packet can win a
    lexical sort even when it is genuinely older.

    old: '2024-01-01 23:30:00+00:00'  (space sep) -> 23:30 UTC  (bearish)  -- lex-larger date part irrelevant; see below
    new: '2024-01-01T23:45:00+00:00'  ('T' sep)   -> 23:45 UTC  (bullish)  -- TRULY fresher
    """
    analyst = HermesSemanticAnalyst()
    old = _packet(asof="2024-01-01 23:45:00+00:00", stance="bearish", confidence=0.9)
    new = _packet(asof="2024-01-01T23:30:00+00:00", stance="bullish", confidence=0.7)
    # TRUTH: the space-separated one (23:45) is later, but it sorts BEFORE 'T' lexically,
    # so the lexical valid[-1] picks the 'T' (23:30, bullish) -> WRONG (should be bearish 23:45).
    assert pd.Timestamp("2024-01-01 23:45:00+00:00").tz_convert("UTC") > pd.Timestamp(
        "2024-01-01T23:30:00+00:00"
    ).tz_convert("UTC")

    view = analyst.analyze(_ctx({"semantic_packets": [old, new]}))
    assert view.direction == -1, "must trade the genuinely-fresher (space-sep, 23:45) bearish packet"
    assert view.confidence_raw == 0.9
    assert view.metadata["packet_asof"] == "2024-01-01 23:45:00+00:00"


def test_single_format_selection_unchanged():
    """Common case: all packets share one format. Behaviour must be byte-identical."""
    analyst = HermesSemanticAnalyst()
    old = _packet(asof="2024-01-01T01:00:00Z", stance="bearish", confidence=0.8)
    new = _packet(asof="2024-01-01T23:00:00Z", stance="bullish", confidence=0.7)
    view = analyst.analyze(_ctx({"semantic_packets": [old, new]}))
    assert view.direction == 1
    assert view.confidence_raw == 0.7
    assert view.metadata["packet_asof"] == "2024-01-01T23:00:00Z"
