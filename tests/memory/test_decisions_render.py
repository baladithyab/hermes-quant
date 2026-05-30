"""tests/memory/test_decisions_render.py — G3 markdown renderer (Wave C).

Pure, read-only renderer over decisions.jsonl. No network, deterministic.
"""

from __future__ import annotations

import json

from hermes_quant.memory.decisions import DecisionLog
from hermes_quant.memory.decisions_render import (
    render_decision_block,
    render_decisions_md,
)


def _log(tmp_path) -> DecisionLog:
    return DecisionLog(tmp_path / "decisions.jsonl")


def test_empty_log_renders_placeholder(tmp_path):
    log = _log(tmp_path)
    out = render_decisions_md(log=log)
    assert "no decisions recorded" in out
    # does not raise


def test_pending_decision_block_has_arrow_and_meta(tmp_path):
    log = _log(tmp_path)
    dec_id = log.record_decision(
        asof_decision="2026-05-01T00:00:00+00:00",
        ticker="AAPL",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.71,
        target_position_pct=0.10,
        thesis_summary="momentum + earnings beat",
    )
    out = render_decisions_md(log=log)
    assert "## " in out
    assert "↑" in out
    assert "Buy" in out
    assert dec_id in out
    assert "target_position_pct" in out


def test_resolved_decision_folds_resolution(tmp_path):
    log = _log(tmp_path)
    dec_id = log.record_decision(
        asof_decision="2026-05-01T00:00:00+00:00",
        ticker="MSFT",
        asset_class="equity",
        rating="Overweight",
        direction=1,
        confidence=0.6,
        target_position_pct=0.05,
        thesis_summary="cloud growth",
    )
    log.record_resolution(dec_id, "refl_x")
    out = render_decisions_md(log=log)
    assert "reflection_id: refl_x" in out
    assert "resolved" in out
    # exactly one block for the id (not two)
    assert out.count(f"decision_id: {dec_id}") == 1


def test_state_filter_pending_only(tmp_path):
    log = _log(tmp_path)
    d1 = log.record_decision(
        asof_decision="2026-05-01T00:00:00+00:00",
        ticker="AAA",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.6,
        target_position_pct=0.10,
        thesis_summary="t1",
    )
    d2 = log.record_decision(
        asof_decision="2026-05-02T00:00:00+00:00",
        ticker="BBB",
        asset_class="equity",
        rating="Sell",
        direction=-1,
        confidence=0.6,
        target_position_pct=-0.10,
        thesis_summary="t2",
    )
    log.record_resolution(d2, "refl_b")
    out = render_decisions_md(log=log, state_filter="pending")
    assert d1 in out
    assert d2 not in out


def test_limit_returns_most_recent(tmp_path):
    log = _log(tmp_path)
    for i, tk in enumerate(["OLD", "MID", "NEW"], 1):
        log.record_decision(
            asof_decision=f"2026-05-0{i}T00:00:00+00:00",
            ticker=tk,
            asset_class="equity",
            rating="Hold",
            direction=0,
            confidence=0.5,
            target_position_pct=0.0,
            thesis_summary=f"t{i}",
        )
    out = render_decisions_md(log=log, limit=1)
    assert "NEW" in out
    assert "OLD" not in out
    assert "MID" not in out


def test_render_is_pure_no_writes(tmp_path):
    log = _log(tmp_path)
    log.record_decision(
        asof_decision="2026-05-01T00:00:00+00:00",
        ticker="AAPL",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.7,
        target_position_pct=0.10,
        thesis_summary="x",
    )
    p = tmp_path / "decisions.jsonl"
    mtime_before = p.stat().st_mtime
    size_before = p.stat().st_size
    render_decisions_md(log=log)
    render_decisions_md(path=p)
    assert p.stat().st_mtime == mtime_before
    assert p.stat().st_size == size_before


def test_missing_optional_keys_tolerated(tmp_path):
    # Hand-write a minimal decision row missing many optional keys.
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps(
            {
                "kind": "decision",
                "decision_id": "dec_minimal",
                "asof_decision": "2026-05-01T00:00:00+00:00",
                "ticker": "ZZZ",
                "rating": "Hold",
                "direction": 0,
                "thesis_summary": "minimal",
            }
        )
        + "\n"
    )
    out = render_decisions_md(path=p)
    block = render_decision_block(
        {
            "decision_id": "dec_minimal",
            "ticker": "ZZZ",
            "rating": "Hold",
            "direction": 0,
        }
    )
    assert "dec_minimal" in out
    assert "ZZZ" in block  # no raise on missing risk_debate_summary/trader_proposal
