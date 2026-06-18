"""Ledger roundtrip, hash-chain integrity, and settlement math."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
UTC = timezone.utc

from quantcore.ledger import Ledger, new_proposal_id
from quantcore.schemas import Fill, Proposal
from quantcore.settle import calibration_report, parse_horizon, settle

from .conftest import ASOF, make_signal


def _propose_and_fill(ledger: Ledger, *, price=100.0, pct=0.10, asof=ASOF, direction=1) -> str:
    pid = new_proposal_id()
    proposal = Proposal(
        proposal_id=pid,
        signal=make_signal(confidence=0.7, direction=direction),
        target_position_pct=pct,
        current_position_pct=0.0,
        delta_pct=pct,
        gate_reason="test",
        created_at=asof,
    )
    ledger.record_proposal(proposal)
    ledger.record_decision_on_proposal(pid, "approval")
    ledger.record_fill(
        Fill(proposal_id=pid, asset="AAPL", fill_price=price, filled_position_pct=pct, filled_at=asof)
    )
    return pid


def test_chain_verifies_and_detects_tamper(tmp_path):
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger)
    ok, msg = ledger.verify_chain()
    assert ok, msg
    # tamper with a middle line
    lines = ledger.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True)
    ledger.path.write_text("\n".join(lines) + "\n")
    ok, msg = ledger.verify_chain()
    assert not ok


def test_pending_proposals_lifecycle(tmp_path):
    ledger = Ledger(tmp_path)
    pid = new_proposal_id()
    proposal = Proposal(
        proposal_id=pid,
        signal=make_signal(),
        target_position_pct=0.05,
        current_position_pct=0.0,
        delta_pct=0.05,
        gate_reason="test",
        created_at=ASOF,
    )
    ledger.record_proposal(proposal)
    assert [p.proposal_id for p in ledger.pending_proposals()] == [pid]
    ledger.record_decision_on_proposal(pid, "rejection", "too rich")
    assert ledger.pending_proposals() == []


def test_portfolio_reconstruction_from_fills(tmp_path):
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10)
    pf = ledger.portfolio()
    assert pf.current_position_pct("AAPL") == 0.10
    # flattening fill removes the position
    pid2 = _propose_and_fill(ledger, price=110.0, pct=0.0)
    pf = ledger.portfolio()
    assert pf.current_position_pct("AAPL") == 0.0


def test_proposal_ladder_validation_rejects_off_ladder():
    import pytest

    with pytest.raises(ValueError):
        Proposal(
            proposal_id="x" * 16,
            signal=make_signal(),
            target_position_pct=0.07,  # not on ladder
            current_position_pct=0.0,
            delta_pct=0.07,
            gate_reason="test",
            created_at=ASOF,
        )


def test_parse_horizon():
    assert parse_horizon("5d") == timedelta(days=5)
    assert parse_horizon("1h") == timedelta(hours=1)
    import pytest

    with pytest.raises(ValueError):
        parse_horizon("soon")


def test_settle_long_win_via_mark(tmp_path):
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, asof=ASOF)
    ledger.record_mark("AAPL", price=105.0, nav=100_500.0)
    now = ASOF + timedelta(days=6)  # past the 5d horizon
    events = settle(ledger, now=now)
    assert len(events) == 1
    ev = events[0]
    assert abs(ev["realized_return"] - 0.05) < 1e-9
    assert ev["direction_correct"] is True
    assert ev["exit_kind"] == "mark"
    # idempotent
    assert settle(ledger, now=now) == []
    # calibration updated for both committee analysts
    report = calibration_report(tmp_path)
    assert set(report) == {"analyst-0", "analyst-1"}
    assert report["analyst-0"]["n"] == 1


def test_settle_waits_for_horizon(tmp_path):
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, asof=ASOF)
    ledger.record_mark("AAPL", price=105.0, nav=100_500.0)
    assert settle(ledger, now=ASOF + timedelta(days=2)) == []


# --- R1-01: calibration judges the RAW move, never the direction-adjusted P&L


def test_settle_correct_short_tallies_correct(tmp_path):
    """100 -> 90 on a short (fill -0.10, views direction -1): the short
    profited AND the view called the raw move right — both must say so."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=-0.10, direction=-1)
    ledger.record_mark("AAPL", price=90.0, nav=101_000.0)
    events = settle(ledger, now=ASOF + timedelta(days=6))
    assert len(events) == 1
    ev = events[0]
    assert abs(ev["realized_return"] - 0.10) < 1e-9  # direction-adjusted P&L: +10%
    assert ev["direction_correct"] is True
    report = calibration_report(tmp_path)
    assert report["analyst-0"]["buckets"][0]["accuracy"] == 1.0
    assert report["analyst-0"]["n"] == 1


def test_settle_wrong_short_tallies_incorrect(tmp_path):
    """100 -> 110 on a short: the view was WRONG even though `realized`
    (direction-adjusted) is what flips sign — calibration must record 0.0."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=-0.10, direction=-1)
    ledger.record_mark("AAPL", price=110.0, nav=99_000.0)
    events = settle(ledger, now=ASOF + timedelta(days=6))
    ev = events[0]
    assert abs(ev["realized_return"] + 0.10) < 1e-9  # short lost 10%
    assert ev["direction_correct"] is False
    report = calibration_report(tmp_path)
    assert report["analyst-0"]["buckets"][0]["accuracy"] == 0.0


def test_settle_long_loss_unchanged_by_short_fix(tmp_path):
    """Long behavior must be untouched: 100 -> 90 on a long is wrong."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, direction=1)
    ledger.record_mark("AAPL", price=90.0, nav=99_000.0)
    ev = settle(ledger, now=ASOF + timedelta(days=6))[0]
    assert abs(ev["realized_return"] + 0.10) < 1e-9
    assert ev["direction_correct"] is False
    assert calibration_report(tmp_path)["analyst-0"]["buckets"][0]["accuracy"] == 0.0


def test_settle_zero_move_excluded_from_calibration(tmp_path):
    """A zero raw move has no direction to be right about: settle records the
    outcome but calibration gets NO tally."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10)
    ledger.record_mark("AAPL", price=100.0, nav=100_000.0)
    ev = settle(ledger, now=ASOF + timedelta(days=6))[0]
    assert ev["realized_return"] == 0.0
    assert ev["direction_correct"] is False
    assert calibration_report(tmp_path) == {}


# --- R1-03: only a REDUCING later fill is an exit; adds fall through to mark


def test_increasing_fill_is_not_an_exit(tmp_path):
    """R1 scenario: 0.10 entry @100, position INCREASED to 0.15 @102 on day 2,
    mark 110 at expiry. The entry settles at the horizon mark (+10%), never at
    the add price (+2%)."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, asof=ASOF)
    _propose_and_fill(ledger, price=102.0, pct=0.15, asof=ASOF + timedelta(days=2))
    ledger.record_mark("AAPL", price=110.0, nav=101_000.0)
    events = settle(ledger, now=ASOF + timedelta(days=6))
    assert len(events) == 1  # the add's own 5d horizon is still running
    ev = events[0]
    assert ev["exit_kind"] == "mark"
    assert abs(ev["exit_price"] - 110.0) < 1e-9
    assert abs(ev["realized_return"] - 0.10) < 1e-9


def test_reducing_fill_is_still_an_exit(tmp_path):
    """A genuine reduce (0.10 -> 0.05) settles the entry at the reduce fill,
    even when a later mark exists."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, asof=ASOF)
    _propose_and_fill(ledger, price=105.0, pct=0.05, asof=ASOF + timedelta(days=2))
    ledger.record_mark("AAPL", price=120.0, nav=102_000.0)
    events = settle(ledger, now=ASOF + timedelta(days=6))
    assert len(events) == 1
    ev = events[0]
    assert ev["exit_kind"] == "fill"
    assert abs(ev["exit_price"] - 105.0) < 1e-9
    assert abs(ev["realized_return"] - 0.05) < 1e-9


def test_flattening_fill_is_an_exit(tmp_path):
    """A flatten-to-zero fill is the exit; no mark needed."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, asof=ASOF)
    _propose_and_fill(ledger, price=103.0, pct=0.0, asof=ASOF + timedelta(days=2))
    events = settle(ledger, now=ASOF + timedelta(days=6))
    assert len(events) == 1
    ev = events[0]
    assert ev["exit_kind"] == "fill"
    assert abs(ev["exit_price"] - 103.0) < 1e-9
    assert abs(ev["realized_return"] - 0.03) < 1e-9


def test_sign_flip_fill_is_an_exit(tmp_path):
    """A sign flip (long 0.10 -> short -0.05) closes the long: it is an exit."""
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, price=100.0, pct=0.10, asof=ASOF)
    _propose_and_fill(ledger, price=104.0, pct=-0.05, asof=ASOF + timedelta(days=2), direction=-1)
    events = settle(ledger, now=ASOF + timedelta(days=6))
    assert len(events) == 1
    ev = events[0]
    assert ev["exit_kind"] == "fill"
    assert abs(ev["exit_price"] - 104.0) < 1e-9


def test_settle_never_invents_exit_price(tmp_path):
    ledger = Ledger(tmp_path)
    _propose_and_fill(ledger, asof=ASOF)
    # no mark, no later fill -> stays unsettled
    assert settle(ledger, now=ASOF + timedelta(days=30)) == []


def test_fill_requires_approval(tmp_path):
    """CLI-level guard: fills only land on approved proposals."""
    from quantcore.cli import main

    ledger = Ledger(tmp_path)
    pid = new_proposal_id()
    proposal = Proposal(
        proposal_id=pid,
        signal=make_signal(),
        target_position_pct=0.05,
        current_position_pct=0.0,
        delta_pct=0.05,
        gate_reason="test",
        created_at=ASOF,
    )
    ledger.record_proposal(proposal)
    fill_file = tmp_path / "fill.json"
    fill_file.write_text(
        json.dumps(
            {
                "proposal_id": pid,
                "asset": "AAPL",
                "fill_price": 100.0,
                "filled_position_pct": 0.05,
                "filled_at": ASOF.isoformat(),
            }
        )
    )
    rc = main(["fill", "--state-dir", str(tmp_path), "--fill-json", str(fill_file)])
    assert rc == 1  # no approval recorded -> refused
