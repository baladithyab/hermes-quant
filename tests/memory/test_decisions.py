"""tests/memory/test_decisions.py — Layer 1 decision log tests (ADR-0042)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant.memory.decisions import AppendOnlyViolation, DecisionLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log(tmp_path: Path) -> DecisionLog:
    return DecisionLog(path=tmp_path / "decisions.jsonl")


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


def test_record_decision_writes_row(log: DecisionLog, tmp_path: Path) -> None:
    dec_id = log.record_decision(
        asof_decision="2026-05-27T16:50:47Z",
        ticker="MRNA",
        asset_class="equity",
        rating="Underweight",
        direction=-1,
        confidence=0.85,
        target_position_pct=-0.10,
        thesis_summary="Pipeline attrition risk outweighs near-term catalyst.",
        thesis_evidence_ids=["ev_001", "ev_002"],
    )
    assert dec_id.startswith("dec_")
    assert "MRNA" in dec_id

    rows = list(log.read_all())
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "MRNA"
    assert row["rating"] == "Underweight"
    assert row["direction"] == -1
    assert row["schema_version"] == 1
    assert row["kind"] == "decision"
    assert row["state"] == "pending"
    assert row["tau_observable"] is None
    assert row["resolution"] is None


def test_schema_version_on_every_row(log: DecisionLog) -> None:
    log.record_decision(
        asof_decision=datetime(2026, 5, 27, tzinfo=UTC),
        ticker="AAPL",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.7,
        target_position_pct=0.05,
        thesis_summary="Strong services growth.",
    )
    for row in log.read_all():
        assert row["schema_version"] == 1


def test_record_resolution_writes_separate_row(log: DecisionLog) -> None:
    dec_id = log.record_decision(
        asof_decision="2026-05-27T16:50:47Z",
        ticker="TSLA",
        asset_class="equity",
        rating="Sell",
        direction=-1,
        confidence=0.6,
        target_position_pct=-0.05,
        thesis_summary="Valuation stretched.",
    )
    log.record_resolution(dec_id, "ref_20260612T140000_TSLA_abc123")

    rows = list(log.read_all())
    assert len(rows) == 2
    kinds = {r["kind"] for r in rows}
    assert kinds == {"decision", "resolution"}

    # original decision row must NOT be mutated
    decision_row = next(r for r in rows if r["kind"] == "decision")
    assert decision_row["state"] == "pending"
    assert decision_row["resolution"] is None


def test_read_pending_excludes_resolved(log: DecisionLog) -> None:
    dec1 = log.record_decision(
        asof_decision="2026-05-01T10:00:00Z",
        ticker="SPY",
        asset_class="equity",
        rating="Hold",
        direction=0,
        confidence=0.5,
        target_position_pct=0.0,
        thesis_summary="Neutral stance.",
    )
    dec2 = log.record_decision(
        asof_decision="2026-05-02T10:00:00Z",
        ticker="QQQ",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.8,
        target_position_pct=0.10,
        thesis_summary="Tech momentum.",
    )
    log.record_resolution(dec1, "ref_xyz")

    pending = list(log.read_pending())
    assert len(pending) == 1
    assert pending[0]["decision_id"] == dec2


def test_read_resolved_returns_pairs(log: DecisionLog) -> None:
    dec_id = log.record_decision(
        asof_decision="2026-05-27T16:50:47Z",
        ticker="NVDA",
        asset_class="equity",
        rating="Overweight",
        direction=1,
        confidence=0.9,
        target_position_pct=0.15,
        thesis_summary="AI compute supercycle.",
    )
    log.record_resolution(dec_id, "ref_nvda_001")

    resolved = list(log.read_resolved())
    assert len(resolved) == 1
    dec_row, res_row = resolved[0]
    assert dec_row["ticker"] == "NVDA"
    assert res_row["reflection_id"] == "ref_nvda_001"
    assert res_row["decision_id"] == dec_id


def test_multiple_decisions_round_trip(log: DecisionLog) -> None:
    ids = []
    for i in range(5):
        did = log.record_decision(
            asof_decision=f"2026-05-{i+1:02d}T10:00:00Z",
            ticker=f"TICK{i}",
            asset_class="equity",
            rating="Hold",
            direction=0,
            confidence=0.5,
            target_position_pct=0.0,
            thesis_summary=f"Thesis {i}",
        )
        ids.append(did)
    rows = list(log.read_all())
    assert len(rows) == 5
    assert all(r["schema_version"] == 1 for r in rows)


# ---------------------------------------------------------------------------
# Append-only enforcement
# ---------------------------------------------------------------------------


def test_truncate_raises(log: DecisionLog) -> None:
    with pytest.raises(AppendOnlyViolation):
        log.truncate()


def test_update_raises(log: DecisionLog) -> None:
    with pytest.raises(AppendOnlyViolation):
        log.update()


def test_jsonl_is_valid_json_every_row(log: DecisionLog, tmp_path: Path) -> None:
    """Every line in the JSONL file must be valid JSON."""
    log.record_decision(
        asof_decision="2026-06-01T00:00:00Z",
        ticker="META",
        asset_class="equity",
        rating="Buy",
        direction=1,
        confidence=0.75,
        target_position_pct=0.07,
        thesis_summary="Metaverse pivot paying off.",
        signal_provenance={"source": "kronos", "signal_id": "sig_001"},
    )
    path = log._path
    for line in path.read_text().splitlines():
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
