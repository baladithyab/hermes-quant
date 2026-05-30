"""Tests for hermes_quant.catalyst.onboarding (C2-4, ADR-0075).

Default-OFF: catalyst_admissions returns [] unless BOTH flags are on. When on, it
admits <=3 strong, fresh, tradeable, out-of-universe catalyst names from the
dead_on_arrival set. The tradeability gate is fail-closed. No network: the catalyst
store is a tmp_path JSONL, the graph is monkeypatched, tradeable() is injected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant.catalyst import onboarding, propagation, synthesize
from hermes_quant.catalyst.propagation import PropagationEdge


def _graph_with_targets(*symbols: str) -> dict[str, list[PropagationEdge]]:
    """A tiny graph whose single source touches the given target symbols."""
    return {
        "test source": [
            PropagationEdge("test source", s, "sector_member", -1, 0.80) for s in symbols
        ]
    }


def _write_packet(
    store: Path,
    *,
    asset: str,
    asof: str,
    stance: str = "bullish",
    confidence: float = 0.7,
    magnitude: float = 0.05,
    horizon: str = "1d",
) -> None:
    from hermes_quant.semantic import semantic_packet_from_dict

    pkt = semantic_packet_from_dict(
        {
            "schema_version": 1,
            "asset": asset,
            "asof": asof,
            "horizon": horizon,
            "stance": stance,
            "confidence": confidence,
            "magnitude": magnitude,
            "summary": f"onboarding test {asset} {stance}",
            "sources": [{"type": "note", "ref": "onboarding-test"}],
            "model": "hermes:onboarding-test",
        }
    )
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(pkt.to_dict(include_hash=True), default=str) + "\n")


@pytest.fixture
def both_flags_on(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_CATALYST_ONBOARDING", "1")
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")


@pytest.fixture
def asof():
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _arm(monkeypatch, tmp_path, *targets: str):
    """Point the store + graph at test fixtures; return the store path."""
    store = tmp_path / "packets.jsonl"
    monkeypatch.setattr(synthesize, "_DEFAULT_STORE", store)
    monkeypatch.setattr(
        propagation, "load_graph", lambda *a, **k: (_graph_with_targets(*targets), {})
    )
    return store


def test_admissions_empty_when_flag_off(monkeypatch, asof):
    monkeypatch.delenv("HERMES_QUANT_CATALYST_ONBOARDING", raising=False)
    monkeypatch.delenv("HERMES_QUANT_SEMANTIC_ENABLED", raising=False)
    assert onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof) == []


def test_admissions_empty_when_only_semantic_on(monkeypatch, asof):
    monkeypatch.delenv("HERMES_QUANT_CATALYST_ONBOARDING", raising=False)
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "1")
    assert onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof) == []


def test_admissions_skips_in_universe_symbols(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "LUNR")
    _write_packet(store, asset="LUNR", asof="2026-01-01T09:00:00Z")
    # LUNR IS in the universe -> covered, not dead_on_arrival -> never admitted.
    out = onboarding.catalyst_admissions({"LUNR"}, tradeable=lambda s: True, asof=asof)
    assert out == []


def test_admissions_threshold_gate(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "LUNR", "RKLB")
    # LUNR below TAU_CONF; RKLB above both -> only RKLB admitted.
    _write_packet(store, asset="LUNR", asof="2026-01-01T09:00:00Z", confidence=0.50, magnitude=0.10)
    _write_packet(store, asset="RKLB", asof="2026-01-01T09:00:00Z", confidence=0.80, magnitude=0.06)
    out = onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof)
    assert [a.symbol for a in out] == ["RKLB"]


def test_admissions_magnitude_floor(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "RKLB")
    # Strong confidence but magnitude below TAU_MAG -> not admitted.
    _write_packet(store, asset="RKLB", asof="2026-01-01T09:00:00Z", confidence=0.90, magnitude=0.01)
    out = onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof)
    assert out == []


def test_admissions_tradeability_fail_closed(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "RKLB")
    _write_packet(store, asset="RKLB", asof="2026-01-01T09:00:00Z", confidence=0.80, magnitude=0.06)
    # tradeable=False -> rejected.
    assert onboarding.catalyst_admissions(set(), tradeable=lambda s: False, asof=asof) == []

    # tradeable raising -> rejected (not an exception).
    def _boom(_s):
        raise RuntimeError("broker down")

    assert onboarding.catalyst_admissions(set(), tradeable=_boom, asof=asof) == []


def test_admissions_cap_to_three(monkeypatch, tmp_path, both_flags_on, asof):
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    store = _arm(monkeypatch, tmp_path, *syms)
    # 5 eligible dead symbols with varying confidence*magnitude -> exactly 3, ranked.
    for i, s in enumerate(syms):
        _write_packet(
            store, asset=s, asof="2026-01-01T09:00:00Z",
            confidence=0.70 + i * 0.05, magnitude=0.05,
        )
    out = onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof)
    assert len(out) == 3
    # Ranked by confidence*magnitude desc: EEE(0.90), DDD(0.85), CCC(0.80).
    assert [a.symbol for a in out] == ["EEE", "DDD", "CCC"]


def test_admissions_neutral_stance_dropped(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "RKLB")
    _write_packet(store, asset="RKLB", asof="2026-01-01T09:00:00Z",
                  stance="neutral", confidence=0.80, magnitude=0.06)
    assert onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof) == []


def test_admission_carries_admitted_via_tag_and_direction(monkeypatch, tmp_path, both_flags_on, asof):
    store = _arm(monkeypatch, tmp_path, "RKLB", "LUNR")
    _write_packet(store, asset="RKLB", asof="2026-01-01T09:00:00Z",
                  stance="bullish", confidence=0.80, magnitude=0.06)
    _write_packet(store, asset="LUNR", asof="2026-01-01T09:00:00Z",
                  stance="bearish", confidence=0.85, magnitude=0.06)
    out = {a.symbol: a for a in onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof)}
    assert out["RKLB"].admitted_via == "catalyst"
    assert out["RKLB"].direction == 1
    assert out["LUNR"].direction == -1


def test_admissions_future_packet_not_admitted(monkeypatch, tmp_path, both_flags_on, asof):
    """A packet published AFTER asof is dropped by load_packets_for -> no admission."""
    store = _arm(monkeypatch, tmp_path, "RKLB")
    _write_packet(store, asset="RKLB", asof="2026-01-01T15:00:00Z",  # after the 12:00 asof
                  confidence=0.90, magnitude=0.06)
    assert onboarding.catalyst_admissions(set(), tradeable=lambda s: True, asof=asof) == []


def test_default_tradeable_fail_closed_without_oracle(monkeypatch):
    """ADR-0077 oracle absent/raising -> default_tradeable returns False."""
    import hermes_quant.admissibility.oracle as oracle_mod

    class _BoomOracle:
        def __init__(self, *a, **k):
            pass

        def is_tradeable_long(self, symbol):
            raise RuntimeError("no creds")

    monkeypatch.setattr(oracle_mod, "AlpacaShortabilityOracle", _BoomOracle)
    assert onboarding.default_tradeable("RKLB") is False


def test_default_tradeable_uses_long_read(monkeypatch):
    import hermes_quant.admissibility.oracle as oracle_mod

    class _StubOracle:
        def __init__(self, *a, **k):
            pass

        def is_tradeable_long(self, symbol):
            return symbol == "RKLB"

    monkeypatch.setattr(oracle_mod, "AlpacaShortabilityOracle", _StubOracle)
    assert onboarding.default_tradeable("RKLB") is True
    assert onboarding.default_tradeable("NOPE") is False
