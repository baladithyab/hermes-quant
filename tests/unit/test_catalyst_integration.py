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


def test_profitability_empty_log_is_silent(tmp_path):
    assert measure_profitability(lambda s, d: 1.0, path=tmp_path / "none.jsonl") == {}
    assert "no scored propagations" in format_report({})


def test_format_report_recommends_action(tmp_path):
    stats = {"brand_self": RelationStats("brand_self", n_scored=MIN_SAMPLE, hits=int(MIN_SAMPLE*0.7), sum_signed_return=50.0)}
    rep = format_report(stats)
    assert "RAISING" in rep  # profitable -> suggest raising the haircut
