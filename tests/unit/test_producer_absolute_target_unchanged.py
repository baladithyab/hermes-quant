"""ADR-0091 Option E acceptance gate (:366) — PRODUCERS UNCHANGED.

Option E fixes the re-affirmation inflation at FOLD time, NOT at the producer. The
rejected Option B made the reactor read derived state and emit a per-fill DELTA (a
read-modify-write race into the immutable log). The Option-E contract is the inverse:
the producer keeps writing the ABSOLUTE signed post-fill TARGET into the per-fill size
field, and the shared fill_delta_normalizer derives the delta downstream.

These are the inverse of the deleted B-era "emits delta" tests:
  - test_paper_reactor_still_writes_absolute_target: the persisted fill_size_pct ==
    target_position_pct == the absolute target (NOT a target - current delta).
  - test_record_stamps_schema_version: the schema_version field round-trips through
    _record_to_dict (None on a legacy-shaped record reads as absolute-target; an
    explicitly-stamped SCHEMA_ABSOLUTE_TARGET round-trips and classifies correctly),
    so the fold can tell an absolute-target record from a future true-delta record.

Deterministic, offline (no normalizer, no state.db dependency on the producer side).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.proposals import Proposal
from hermes_quant.react.base import (
    SCHEMA_ABSOLUTE_TARGET,
    ExecutionRecord,
    is_absolute_target_record,
)
from hermes_quant.react.paper import PaperReactor, _record_to_dict


@pytest.fixture(autouse=True)
def _producer_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the slippage seam at legacy passthrough so fill_price == decision_price and
    # the producer's fill_size_pct is asserted on the unscaled record (the cap/slippage
    # seams are separate concerns; this test is about the absolute-target contract).
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")


def _proposal(*, kelly: float = 0.05) -> Proposal:
    return Proposal(
        proposal_id="prop_2026-06-04T000000_AAPL_abc123",
        state="pending",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-06-04T00:00:00Z",
        expires_at="2026-06-04T00:15:00Z",
        advisor_result={
            "as_of": "2026-06-04T00:00:00Z",
            "decision_price": 200.0,
            "signal_id": "sig-abs-target",
            "risk_gate": {
                "pass": True,
                "kelly_fraction": kelly,
                "recommended_action": "long_with_stop",
            },
            "caveats": [],
        },
    )


def _bus_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Producer writes the ABSOLUTE target (the Option-E contract), NOT a delta.
# --------------------------------------------------------------------------- #


def test_paper_reactor_still_writes_absolute_target(tmp_path: Path) -> None:
    """The persisted record's per-fill size field is the ABSOLUTE target on EVERY fire,
    including a re-affirmation of an unchanged target — proving the producer never reads
    derived state to emit a delta (the rejected Option B). Two consecutive 0.05 fires
    each persist fill_size_pct == 0.05 (== target_position_pct), NOT 0.05 then 0.0."""
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)

    rec1 = reactor.execute(_proposal(kelly=0.05), fill_size_pct=0.05)
    rec2 = reactor.execute(_proposal(kelly=0.05), fill_size_pct=0.05)  # RE-AFFIRM

    # In-memory contract: fill_size_pct == target_position_pct == the absolute target.
    for rec in (rec1, rec2):
        assert rec.fill_size_pct == pytest.approx(0.05)
        assert rec.target_position_pct == pytest.approx(0.05)
        assert rec.fill_size_pct == rec.target_position_pct

    # Persisted contract: BOTH records carry the absolute 0.05 — the re-affirmation is
    # NOT collapsed to a 0.0 delta at the producer (Option B would have written 0.0).
    persisted = _bus_records(bus)
    assert len(persisted) == 2
    assert persisted[0]["fill_size_pct"] == pytest.approx(0.05)
    assert persisted[1]["fill_size_pct"] == pytest.approx(0.05), (
        "the re-affirmation must persist the ABSOLUTE 0.05 target, not a 0.0 delta "
        "(Option E keeps the producer absolute; the delta is derived at fold time)"
    )
    # The persisted size field IS the target on both records.
    assert persisted[0]["fill_size_pct"] == persisted[0]["target_position_pct"]
    assert persisted[1]["fill_size_pct"] == persisted[1]["target_position_pct"]


def test_record_stamps_schema_version(tmp_path: Path) -> None:
    """schema_version round-trips through _record_to_dict, and the fold classifier reads
    it correctly:
      - a legacy-shaped record (schema_version unset -> None) serializes None and
        classifies as ABSOLUTE-TARGET (every historical record is one);
      - an explicitly-stamped SCHEMA_ABSOLUTE_TARGET record round-trips verbatim and
        also classifies as absolute-target.
    This is the field the fold keys off to tell an absolute-target record from a future
    true-delta record — the nullable + defaulted back-compat pattern (bar_ts / play_tag)."""
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)

    # The PaperReactor builds its ExecutionRecord without setting schema_version, so it
    # defaults to None — the legacy absolute-target shape that serializes verbatim.
    rec = reactor.execute(_proposal(kelly=0.05), fill_size_pct=0.05)
    assert rec.schema_version is None, "PaperReactor leaves schema_version default (None)"

    persisted = _bus_records(bus)[0]
    assert "schema_version" in persisted, "schema_version must be serialized to the log"
    assert persisted["schema_version"] is None
    # A None / absent schema_version classifies as absolute-target.
    assert is_absolute_target_record(persisted) is True
    assert is_absolute_target_record({k: v for k, v in persisted.items()
                                      if k != "schema_version"}) is True

    # An explicitly-stamped record round-trips the sentinel verbatim AND classifies as
    # absolute-target (the new-record stamp path the contract documents).
    stamped = ExecutionRecord(
        proposal_id="p_stamp",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-04T00:00:00Z",
        asof_execution="2026-06-04T00:00:00Z",
        target_position_pct=0.05,
        decision_price=200.0,
        fill_price=200.0,
        fill_size_pct=0.05,
        reactor_name="paper",
        human_in_the_loop=True,
        schema_version=SCHEMA_ABSOLUTE_TARGET,
    )
    d = _record_to_dict(stamped)
    assert d["schema_version"] == SCHEMA_ABSOLUTE_TARGET
    assert is_absolute_target_record(d) is True
    # The size field on the stamped record is STILL the absolute target.
    assert d["fill_size_pct"] == d["target_position_pct"] == pytest.approx(0.05)


def test_det_equity_quantity_lane_is_absolute_target(tmp_path: Path) -> None:
    """The det-equity producer carries the ABSOLUTE backend filled_qty under
    reactor_metadata.quantity (a true-share anchor), NOT a delta — the contract docstring
    (react/base.py:50-52) calls this 'likewise absolute not delta'. A record carrying a
    quantity lane and no explicit schema_version classifies as absolute-target, so the
    normalizer's quantity-path carry-forward derives the share delta downstream."""
    rec = {
        "proposal_id": "det1",
        "asset": "AAPL",
        "asset_class": "equity",
        "asof_execution": "2026-06-04T00:00:00Z",
        "fill_price": 100.0,
        "fill_size_pct": 0.05,
        "reactor_metadata": {"account_id": "paper-default", "quantity": 33.33},
    }
    # No schema_version => absolute-target => the normalizer must derive the share delta,
    # NOT pass the absolute 33.33 through as a per-fill traded delta.
    assert is_absolute_target_record(rec) is True
