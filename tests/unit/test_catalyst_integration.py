"""Tests for catalyst-signal integration: confidence haircut + profitability loop."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.profitability import (
    MIN_SAMPLE,
    RelationStats,
    format_report,
    measure_profitability,
)
from hermes_quant.catalyst.propagation import load_graph
from hermes_quant.catalyst.synthesize import (
    CONSUMER_TREND_CONFIDENCE_HAIRCUT,
    _consumer_trend_haircut,
    synthesize_packets,
)


def _item(title):
    return CatalystItem(title=title, published_at=datetime.now(UTC),
                        source="t", link="n/a")


# --- sizing: consumer-trend is haircut, established sectors are not ---
def test_consumer_trend_packet_is_haircut():
    g, a = load_graph()
    pk = synthesize_packets([_item("Celsius energy drink goes viral, sales soar")], graph=g, aliases=a)
    celh = [p for p in pk if p.asset == "CELH"]
    assert celh, "expected a CELH consumer-trend packet"
    d = celh[0].to_dict()
    # confidence == pre_haircut * 0.5
    assert abs(d["confidence"] - d["metadata"]["confidence_pre_haircut"] * CONSUMER_TREND_CONFIDENCE_HAIRCUT) < 1e-6
    assert d["metadata"]["confidence_haircut"] == CONSUMER_TREND_CONFIDENCE_HAIRCUT
    assert d["metadata"]["relations"] == ["brand_self"]


def test_established_sector_packet_not_haircut():
    g, a = load_graph()
    pk = synthesize_packets([_item("Blue Origin New Glenn explodes on launch")], graph=g, aliases=a)
    assert pk, "expected space-sector packets"
    for p in pk:
        d = p.to_dict()
        assert d["metadata"]["confidence_haircut"] == 1.0
        assert "brand_self" not in d["metadata"]["relations"]


def test_haircut_helper_keys_on_relation():
    class _R:
        contributions = [{"relation": "brand_self"}]
    assert _consumer_trend_haircut(_R()) == CONSUMER_TREND_CONFIDENCE_HAIRCUT

    class _S:
        contributions = [{"relation": "competitor"}, {"relation": "sector_member"}]
    assert _consumer_trend_haircut(_S()) == 1.0

    class _Mixed:  # a mixed packet is NOT all-consumer -> no haircut (conservative)
        contributions = [{"relation": "brand_self"}, {"relation": "competitor"}]
    assert _consumer_trend_haircut(_Mixed()) == 1.0


# --- profitability loop ---
def _write_log(tmp_path, rows):
    p = tmp_path / "propagation-log.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_profitability_scores_hits_by_relation(tmp_path):
    # brand_self: 2 correct (sign matches fwd), 1 wrong
    rows = [
        {"symbol": "CELH", "relation": "brand_self", "symbol_sign": 1, "asof": "2021-03-01T00:00:00+00:00"},
        {"symbol": "CROX", "relation": "brand_self", "symbol_sign": 1, "asof": "2020-06-01T00:00:00+00:00"},
        {"symbol": "TPR", "relation": "brand_self", "symbol_sign": 1, "asof": "2023-08-01T00:00:00+00:00"},
        {"symbol": "RKLB", "relation": "competitor", "symbol_sign": -1, "asof": "2026-05-28T00:00:00+00:00"},
    ]
    p = _write_log(tmp_path, rows)
    fwd = {"CELH": 14.0, "CROX": 82.8, "TPR": -25.3, "RKLB": -14.8}
    stats = measure_profitability(lambda s, d: fwd.get(s), path=p)
    bs = stats["brand_self"]
    assert bs.n_scored == 3
    assert bs.hits == 2  # CELH+CROX correct, TPR wrong
    assert abs(bs.hit_rate - 2 / 3) < 1e-6
    comp = stats["competitor"]
    assert comp.n_scored == 1 and comp.hits == 1


def test_verdict_requires_min_sample():
    s = RelationStats(relation="brand_self", n_scored=3, hits=3, sum_signed_return=30.0)
    assert s.verdict == "INSUFFICIENT_SAMPLE"  # n < MIN_SAMPLE
    s2 = RelationStats(relation="brand_self", n_scored=MIN_SAMPLE, hits=int(MIN_SAMPLE * 0.7), sum_signed_return=40.0)
    assert s2.verdict == "PROFITABLE"
    s3 = RelationStats(relation="brand_self", n_scored=MIN_SAMPLE, hits=int(MIN_SAMPLE * 0.4), sum_signed_return=-10.0)
    assert s3.verdict == "UNPROFITABLE_CONSIDER_PRUNE"


def test_verdict_boundary_hit_rate_060_is_profitable():
    """hr == MIN_HIT_RATE (0.60) with positive return is the PROFITABLE edge.

    This is the exact knife-edge B07 turns on: a brand_self class clears the
    consumer-trend haircut only when it reaches the 0.60 bar. n=20==MIN_SAMPLE,
    hits=12 -> hit_rate == 0.60 exactly (>= MIN_HIT_RATE).
    """
    from hermes_quant.catalyst.profitability import MIN_HIT_RATE

    s = RelationStats(relation="brand_self", n_scored=MIN_SAMPLE, hits=12, sum_signed_return=30.0)
    assert s.hit_rate == MIN_HIT_RATE  # 12/20 == 0.60
    assert s.mean_signed_return > 0
    assert s.verdict == "PROFITABLE"


def test_verdict_boundary_hit_rate_059_is_marginal_hold():
    """hr just under 0.60 (0.59) with positive return is MARGINAL_HOLD, not PROFITABLE.

    The B07 decision line: at hr=0.59 the class is in the 0.5<=hr<0.6 band with a
    non-negative mean return, so it holds the 0.5 haircut (accumulate more data),
    it does NOT clear the bar to raise the weight. n=100, hits=59 -> hr == 0.59.
    """
    s = RelationStats(relation="brand_self", n_scored=100, hits=59, sum_signed_return=15.0)
    assert s.hit_rate == 0.59  # strictly below MIN_HIT_RATE, at/above 0.5
    assert s.mean_signed_return > 0
    assert s.verdict == "MARGINAL_HOLD"


def test_profitability_empty_log_is_silent(tmp_path):
    assert measure_profitability(lambda s, d: 1.0, path=tmp_path / "none.jsonl") == {}
    assert "no scored propagations" in format_report({})


def test_format_report_recommends_action(tmp_path):
    stats = {"brand_self": RelationStats("brand_self", n_scored=MIN_SAMPLE, hits=int(MIN_SAMPLE*0.7), sum_signed_return=50.0)}
    rep = format_report(stats)
    assert "RAISING" in rep  # profitable -> suggest raising the haircut


# ---------------------------------------------------------------------------
# PDR-2 TrendVelocity — flag-OFF byte-identical (T2) + flag-ON magnitude-only (T3)
# ---------------------------------------------------------------------------
def _camillo_item():
    return CatalystItem(
        title="Celsius energy drink goes viral on TikTok as sales surge among Gen Z",
        published_at=datetime(2021, 3, 1, tzinfo=UTC),
        source="phase0-label",
        link="n/a",
    )


def test_velocity_flag_off_is_byte_identical(monkeypatch):
    """HERMES_QUANT_TREND_VELOCITY unset/0 -> magnitude stays severity-based and the
    synthesized packets are BYTE-IDENTICAL to the no-velocity baseline. The single most
    important rail (#1): the default path is bit-for-bit today's, even when a velocity
    map is PASSED. Asserted on the full dict INCLUDING the packet hash."""
    monkeypatch.delenv("HERMES_QUANT_TREND_VELOCITY", raising=False)
    g, a = load_graph()
    items = [_camillo_item()]
    base = synthesize_packets(items, graph=g, aliases=a)
    # passing a (loud) velocity map but with the flag OFF must change NOTHING:
    withv = synthesize_packets(
        items, graph=g, aliases=a, velocity_by_symbol={"CELH": {"baseline_z": 9.0}}
    )
    assert [p.to_dict(include_hash=True) for p in base] == [
        p.to_dict(include_hash=True) for p in withv
    ]
    # and no provenance keys leaked into metadata when the flag is OFF:
    for p in withv:
        assert "magnitude_source" not in p.metadata
        assert "velocity_score" not in p.metadata


def test_velocity_flag_off_explicit_zero_is_byte_identical(monkeypatch):
    """Same as above but with the flag EXPLICITLY set to '0' (not just unset)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "0")
    g, a = load_graph()
    items = [_camillo_item()]
    base = synthesize_packets(items, graph=g, aliases=a)
    withv = synthesize_packets(
        items, graph=g, aliases=a, velocity_by_symbol={"CELH": {"baseline_z": 9.0}}
    )
    assert [p.to_dict(include_hash=True) for p in base] == [
        p.to_dict(include_hash=True) for p in withv
    ]


def test_velocity_flag_on_changes_only_magnitude(monkeypatch):
    """Flag ON: magnitude moves to velocity-sourced; stance + confidence UNCHANGED
    (D74.3 magnitude/confidence never conflated)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    g, a = load_graph()
    base = synthesize_packets([_camillo_item()], graph=g, aliases=a)
    on = synthesize_packets(
        [_camillo_item()], graph=g, aliases=a, velocity_by_symbol={"CELH": {"baseline_z": 9.0}}
    )
    assert base and on
    assert base[0].stance == on[0].stance
    assert base[0].confidence == on[0].confidence  # confidence is NOT re-sourced (D74.3)
    assert on[0].magnitude != base[0].magnitude  # magnitude (and ONLY magnitude) moved
    assert on[0].metadata["magnitude_source"] == "velocity"
    assert on[0].metadata["velocity_score"] == {"baseline_z": 9.0}
    # metadata otherwise identical (only the two provenance keys added):
    extra = set(on[0].metadata) - set(base[0].metadata)
    assert extra == {"magnitude_source", "velocity_score"}


def test_velocity_flag_on_no_score_for_symbol_falls_back_to_severity(monkeypatch):
    """Flag ON but the velocity map has no entry for the touched symbol -> magnitude
    falls back to severity and NO provenance is stamped (silence-by-default abstain)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    g, a = load_graph()
    base = synthesize_packets([_camillo_item()], graph=g, aliases=a)
    on = synthesize_packets(
        [_camillo_item()], graph=g, aliases=a, velocity_by_symbol={"NVDA": {"baseline_z": 9.0}}
    )
    assert on[0].magnitude == base[0].magnitude
    assert "magnitude_source" not in on[0].metadata


def test_velocity_flag_on_decelerating_z_floors_magnitude(monkeypatch):
    """A decelerating (z<=0) score floors magnitude to 0.0 — a flag flip on a fading
    trend does not inflate the packet (rail #2)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    g, a = load_graph()
    on = synthesize_packets(
        [_camillo_item()], graph=g, aliases=a, velocity_by_symbol={"CELH": {"baseline_z": -3.0}}
    )
    assert on[0].magnitude == 0.0
    assert on[0].metadata["magnitude_source"] == "velocity"
