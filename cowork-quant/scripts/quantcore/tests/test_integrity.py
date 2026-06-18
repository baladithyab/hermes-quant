"""Tests for Wave-4 integrity: verify_ledger (B-30), manifest + replay (B-32)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quantcore.config import RiskConfig
from quantcore.gate import RiskGate
from quantcore.ledger import Ledger
from quantcore.manifest import (
    canonical_json,
    gate_manifest,
    manifest_digest,
    write_session_manifest,
)
from quantcore.replay import DecisionRecord, assert_replayable
from quantcore.schemas import (
    AnalystView,
    CommitteeSignal,
    Fill,
    MarketCosts,
    PortfolioState,
    Proposal,
)
from quantcore.verify_ledger import verify_ledger

UTC = timezone.utc
ASOF = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)


def _signal(direction=1):
    return CommitteeSignal(
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=0.05,
        confidence=0.70,
        horizon="5d",
        asof_decision=ASOF,
        views=[
            AnalystView(
                analyst="classical-ta", asset="AAPL", asset_class="equity",
                direction=direction, magnitude=0.05, confidence=0.7,
                horizon="5d", asof_decision=ASOF,
            ),
            AnalystView(
                analyst="fundamentals", asset="AAPL", asset_class="equity",
                direction=direction, magnitude=0.04, confidence=0.65,
                horizon="5d", asof_decision=ASOF,
            ),
        ],
    )


def _proposal(pid="proposal_0001"):
    return Proposal(
        proposal_id=pid, signal=_signal(), target_position_pct=0.05,
        current_position_pct=0.0, delta_pct=0.05, gate_reason="rule6_kelly",
        created_at=ASOF,
    )


def _fill(pid="proposal_0001", pct=0.05):
    return Fill(
        proposal_id=pid, asset="AAPL", fill_price=187.25,
        filled_position_pct=pct, filled_at=ASOF, source="manual",
    )


# -- verify_ledger ---------------------------------------------------------


def test_verify_clean_ledger(tmp_path):
    led = Ledger(tmp_path)
    led.record_proposal(_proposal())
    led.record_decision_on_proposal("proposal_0001", "approval")
    led.record_mark("AAPL", 187.25, 100_000.0)
    led.record_fill(_fill())
    rep = verify_ledger(tmp_path)
    assert rep.ok, rep.summary()
    assert rep.chain_ok


def test_orphan_fill_detected(tmp_path):
    led = Ledger(tmp_path)
    # inject a fill with NO prior proposal (poisoning / orphan)
    led.append("fill", {"fill": _fill(pid="ghost_9999").model_dump(mode="json")})
    rep = verify_ledger(tmp_path)
    assert not rep.ok
    assert "ghost_9999" in rep.orphan_fills
    assert "AAPL" in rep.untraceable_positions


def test_unapproved_fill_detected(tmp_path):
    led = Ledger(tmp_path)
    led.record_proposal(_proposal())  # proposed but NOT approved
    led.record_fill(_fill())
    rep = verify_ledger(tmp_path)
    assert not rep.ok
    assert "proposal_0001" in rep.unapproved_fills


def test_chain_tamper_detected(tmp_path):
    led = Ledger(tmp_path)
    led.record_proposal(_proposal())
    led.record_decision_on_proposal("proposal_0001", "approval")
    led.record_fill(_fill())
    # tamper a middle line so the next line's prev_hash no longer matches
    lines = led.path.read_text().splitlines()
    lines[1] = lines[1].replace("approval", "approvaI")  # capital-i typo
    led.path.write_text("\n".join(lines) + "\n")
    rep = verify_ledger(tmp_path)
    assert not rep.ok
    assert not rep.chain_ok


# -- manifest --------------------------------------------------------------


def test_manifest_digest_stable_and_sensitive():
    d1 = manifest_digest(RiskConfig.conservative())
    d2 = manifest_digest(RiskConfig.conservative())
    assert d1 == d2 and len(d1) == 64
    assert d1 != manifest_digest(RiskConfig.aggressive())


def test_canonical_json_determinism():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    # float drift is rounded away; -0.0 normalized
    assert canonical_json(0.1 + 0.2) == canonical_json(0.3)
    assert canonical_json(-0.0) == canonical_json(0.0)


def test_write_session_manifest(tmp_path):
    led = Ledger(tmp_path)
    rec = write_session_manifest(led, RiskConfig.conservative())
    assert rec["event"] == "session_manifest"
    assert rec["manifest_digest"] == manifest_digest(RiskConfig.conservative())
    assert led.verify_chain()[0]


# -- replay ----------------------------------------------------------------


def _record():
    cfg = RiskConfig(paper_zero_costs=True)
    sig = _signal()
    costs = MarketCosts(commission=0.0, spread=0.0, slippage_estimate=0.0,
                        volatility=0.02, tz="UTC")
    pf = PortfolioState(nav=100_000.0, peak_nav=100_000.0, day_start_nav=100_000.0,
                        positions=[], asof=ASOF)
    decision = RiskGate(cfg).gate(sig, costs, pf)
    return DecisionRecord(config=cfg, signal=sig, costs=costs, portfolio=pf,
                          decision=decision)


def test_gate_decision_replayable():
    rec = _record()
    h = assert_replayable(rec)  # must not raise
    assert len(h) == 64


def test_record_roundtrip_replayable():
    rec = _record()
    rebuilt = DecisionRecord.from_dict(rec.to_dict())
    assert assert_replayable(rebuilt)


def test_tampered_decision_fails_replay():
    rec = _record()
    # mutate the recorded decision; re-running the gate must disagree
    bad = rec.decision.model_copy(update={"target_position_pct": 0.99})
    rec.decision = bad
    with pytest.raises(AssertionError):
        assert_replayable(rec)
