"""CLI seam guards — per AGENTS.md, refusal/silence paths get tested FIRST.

Covers the R1 fixes at the command surface:
  R1-02  cmd_fill re-validates the last seam into the book
  R1-04  flatten_halt verdicts persist as 'halt' events; `resume` is human-only
  R1-05  deterministic proposal TTL: stale approvals refused; `expire` sweeps
  R1-06  cmd_mark sanity guards (lean mitigation; --allow-jump documented out)
  R1-07  the sizing ladder is an immutable rail at the RiskConfig boundary
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
UTC = timezone.utc

import pytest

from quantcore.cli import main
from quantcore.config import RiskConfig
from quantcore.ledger import Ledger, new_proposal_id
from quantcore.schemas import SIZING_LADDER, Proposal

from .conftest import ASOF, make_signal


def run(capsys, *argv: str):
    """Invoke the CLI exactly as a slash command would; parse the JSON reply."""
    rc = main(list(argv))
    out = json.loads(capsys.readouterr().out)
    return rc, out


def record_proposal(ledger: Ledger, *, target=0.10, created_at=None, direction=1) -> str:
    pid = new_proposal_id()
    ledger.record_proposal(
        Proposal(
            proposal_id=pid,
            signal=make_signal(direction=direction),
            target_position_pct=target,
            current_position_pct=0.0,
            delta_pct=target,
            gate_reason="test",
            created_at=created_at or datetime.now(UTC),
        )
    )
    return pid


def approved_proposal(tmp_path, *, target=0.10) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path)
    pid = record_proposal(ledger, target=target)
    ledger.record_decision_on_proposal(pid, "approval")
    return ledger, pid


def fill_json(tmp_path, name, *, pid, asset="AAPL", price=100.0, pct=0.10) -> str:
    f = tmp_path / name
    f.write_text(
        json.dumps(
            {
                "proposal_id": pid,
                "asset": asset,
                "fill_price": price,
                "filled_position_pct": pct,
                "filled_at": ASOF.isoformat(),
            }
        )
    )
    return str(f)


def signal_json(
    tmp_path,
    name="signal.json",
    *,
    direction=1,
    confidence=0.75,
    magnitude=0.03,
    volatility=0.02,
    asof=None,
) -> str:
    asof = (asof or ASOF).isoformat()
    sig = {
        "asset": "AAPL",
        "asset_class": "equity",
        "direction": direction,
        "magnitude": magnitude,
        "confidence": confidence,
        "horizon": "5d",
        "asof_decision": asof,
        "views": [
            {
                "analyst": f"analyst-{i}",
                "asset": "AAPL",
                "asset_class": "equity",
                "direction": direction,
                "magnitude": magnitude,
                "confidence": confidence,
                "horizon": "5d",
                "asof_decision": asof,
            }
            for i in range(2)
        ],
    }
    costs = {
        "commission": 0.0,
        "spread": 0.0005,
        "slippage_estimate": 0.0005,
        "volatility": volatility,
    }
    f = tmp_path / name
    f.write_text(json.dumps({"signal": sig, "costs": costs}))
    return str(f)


# === R1-02: cmd_fill is the last seam — every refusal path ====================


def test_fill_refused_for_unknown_proposal(tmp_path, capsys):
    fj = fill_json(tmp_path, "f.json", pid="feedfacefeedface")
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "not found" in out["error"]


def test_double_fill_refused(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=0.10)
    rc, _ = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 0
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "double-fill" in out["error"]
    # exactly one fill in the chain
    assert sum(1 for e in Ledger(tmp_path).events() if e["event"] == "fill") == 1


def test_fill_asset_mismatch_refused(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path)
    fj = fill_json(tmp_path, "f.json", pid=pid, asset="MSFT")
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "does not match" in out["error"]


def test_fill_size_up_refused(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path, target=0.10)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=0.15)
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "never up" in out["error"]


def test_fill_off_ladder_refused(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path, target=0.10)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=0.07)
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "ladder" in out["error"]


def test_fill_sign_flip_refused(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path, target=0.10)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=-0.05)
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 1 and "direction" in out["error"]


def test_fill_size_down_allowed(tmp_path, capsys):
    """The human may take LESS risk than approved (R1-09 sized-down flow)."""
    _, pid = approved_proposal(tmp_path, target=0.10)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=0.05)
    rc, out = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 0 and out["recorded"]["fill"]["filled_position_pct"] == 0.05


def test_fill_flatten_allowed(tmp_path, capsys):
    _, pid = approved_proposal(tmp_path, target=0.10)
    fj = fill_json(tmp_path, "f.json", pid=pid, pct=0.0)
    rc, _ = run(capsys, "fill", "--state-dir", str(tmp_path), "--fill-json", fj)
    assert rc == 0


# === R1-04: halt persistence + human-only resume ==============================


def test_drawdown_propose_persists_halt_then_silences_then_resume(tmp_path, capsys):
    d = str(tmp_path)
    # First mark is unconstrained: 20% drawdown vs paper NAV (conservative max 10%)
    rc, _ = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "80000")
    assert rc == 0
    sj = signal_json(tmp_path)
    rc, out = run(capsys, "propose", "--state-dir", d, "--signal-json", sj)
    assert rc == 0 and out["decision"]["verdict"] == "flatten_halt"
    halts = [e for e in Ledger(tmp_path).events() if e["event"] == "halt"]
    assert len(halts) == 1
    assert "drawdown" in halts[0]["reason"]
    assert halts[0]["halt_until"] is None  # durable: explicit resume only
    # The breaker is now LIVE state: the next propose is silenced by rule 0
    rc, out = run(capsys, "propose", "--state-dir", d, "--signal-json", sj)
    assert rc == 0 and out["decision"]["rule"] == "rule0_halt"
    # rule0 silence is not flatten_halt: no second halt event piles up
    assert sum(1 for e in Ledger(tmp_path).events() if e["event"] == "halt") == 1
    # Human-confirmed resume clears it
    rc, _ = run(capsys, "resume", "--state-dir", d, "--note", "human confirmed resume")
    assert rc == 0
    assert Ledger(tmp_path).portfolio().halted is False


def test_daily_loss_propose_persists_timed_halt_that_auto_clears(tmp_path, capsys):
    d = str(tmp_path)
    # 4% same-day loss (> 3% conservative) but only 4% drawdown (< 10%)
    rc, _ = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "96000")
    assert rc == 0
    # asof far in the past => halt_until (next session) is already past
    sj = signal_json(tmp_path, asof=datetime.now(UTC) - timedelta(days=30))
    rc, out = run(capsys, "propose", "--state-dir", d, "--signal-json", sj)
    assert rc == 0 and out["decision"]["rule"] == "rule2_daily_loss"
    halts = [e for e in Ledger(tmp_path).events() if e["event"] == "halt"]
    assert len(halts) == 1 and halts[0]["halt_until"] is not None
    # timed halt auto-clears once halt_until passes — no resume needed
    assert Ledger(tmp_path).portfolio().halted is False


def test_timed_halt_blocks_while_active(tmp_path):
    ledger = Ledger(tmp_path)
    future = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    ledger.append("halt", {"reason": "daily_loss_test", "halt_until": future})
    assert ledger.portfolio().halted is True


def test_resume_refused_when_not_halted(tmp_path, capsys):
    rc, out = run(capsys, "resume", "--state-dir", str(tmp_path), "--note", "oops")
    assert rc == 1 and "no active halt" in out["error"]


# === R1-05: deterministic proposal TTL ========================================


def test_stale_approval_refused_but_rejection_allowed(tmp_path, capsys):
    ledger = Ledger(tmp_path)
    stale = record_proposal(ledger, created_at=datetime.now(UTC) - timedelta(hours=48))
    rc, out = run(
        capsys, "decide", "--state-dir", str(tmp_path), "--proposal-id", stale, "--decision", "approval"
    )
    assert rc == 1 and "stale" in out["error"]
    # closing stale state (rejection) is still allowed
    rc, _ = run(
        capsys, "decide", "--state-dir", str(tmp_path), "--proposal-id", stale, "--decision", "rejection"
    )
    assert rc == 0


def test_fresh_approval_unaffected_by_ttl(tmp_path, capsys):
    ledger = Ledger(tmp_path)
    pid = record_proposal(ledger)  # created now
    rc, _ = run(
        capsys, "decide", "--state-dir", str(tmp_path), "--proposal-id", pid, "--decision", "approval"
    )
    assert rc == 0


def test_expire_sweeps_stale_only_and_is_idempotent(tmp_path, capsys):
    ledger = Ledger(tmp_path)
    stale = record_proposal(ledger, created_at=datetime.now(UTC) - timedelta(hours=48))
    fresh = record_proposal(ledger)
    rc, out = run(capsys, "expire", "--state-dir", str(tmp_path))
    assert rc == 0 and out["n_expired"] == 1
    assert out["expired"][0]["proposal_id"] == stale
    # idempotent: nothing left to expire
    rc, out = run(capsys, "expire", "--state-dir", str(tmp_path))
    assert rc == 0 and out["n_expired"] == 0
    # the fresh proposal is untouched and still pending
    assert [p.proposal_id for p in Ledger(tmp_path).pending_proposals()] == [fresh]


def test_pending_proposals_ttl_filter(tmp_path):
    ledger = Ledger(tmp_path)
    stale = record_proposal(ledger, created_at=datetime.now(UTC) - timedelta(hours=48))
    fresh = record_proposal(ledger)
    assert {p.proposal_id for p in ledger.pending_proposals()} == {stale, fresh}
    assert [p.proposal_id for p in ledger.pending_proposals(ttl_hours=24.0)] == [fresh]


def test_proposal_ttl_default_in_all_profiles():
    for ctor in (RiskConfig.conservative, RiskConfig.moderate, RiskConfig.aggressive):
        assert ctor().proposal_ttl_hours == 24.0


# === R1-07: the sizing ladder is an immutable rail ============================


def test_riskconfig_rejects_oversized_max_position():
    with pytest.raises(ValueError, match="rail #3"):
        RiskConfig(max_position_pct=0.4)


def test_riskconfig_rejects_off_ladder_action_step():
    with pytest.raises(ValueError, match="rail #3"):
        RiskConfig(action_step=0.10)


def test_aggressive_profile_is_on_the_ladder():
    cfg = RiskConfig.aggressive()
    assert cfg.max_position_pct <= max(SIZING_LADDER) + 1e-12
    assert abs(cfg.action_step - 0.05) < 1e-12


def test_aggressive_profile_proposes_ladder_target_end_to_end(tmp_path, capsys):
    """The R1-07 crash: aggressive used to size 0.30/0.40 and blow up Proposal
    validation inside cmd_propose. Now it must emit a valid ladder proposal."""
    (tmp_path / "config.json").write_text(json.dumps({"profile": "aggressive"}))
    sj = signal_json(tmp_path, confidence=0.9, magnitude=0.05, volatility=0.02)
    rc, out = run(capsys, "propose", "--state-dir", str(tmp_path), "--signal-json", sj)
    assert rc == 0 and out["proposal"] is not None
    target = out["proposal"]["target_position_pct"]
    assert any(abs(abs(target) - r) < 1e-9 for r in SIZING_LADDER)
    assert abs(target) <= 0.20 + 1e-9


# === R1-06 (lean): mark sanity guards =========================================


def test_first_mark_unconstrained(tmp_path, capsys):
    rc, _ = run(
        capsys, "mark", "--state-dir", str(tmp_path), "--asset", "AAPL", "--price", "100", "--nav", "50000"
    )
    assert rc == 0


def test_mark_price_jump_refused_then_allowed_with_flag(tmp_path, capsys):
    d = str(tmp_path)
    run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "100000")
    rc, out = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "160", "--nav", "100000")
    assert rc == 1 and "50%" in out["error"]
    rc, _ = run(
        capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "160", "--nav", "100000", "--allow-jump"
    )
    assert rc == 0


def test_mark_nav_jump_refused_then_allowed_with_flag(tmp_path, capsys):
    d = str(tmp_path)
    run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "100000")
    rc, out = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "101", "--nav", "60000")
    assert rc == 1 and "30%" in out["error"]
    rc, _ = run(
        capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "101", "--nav", "60000", "--allow-jump"
    )
    assert rc == 0


def test_mark_price_jump_checked_per_asset(tmp_path, capsys):
    """A first mark on a NEW asset is unconstrained on price (no history),
    even when other assets have marks; NAV continuity still applies."""
    d = str(tmp_path)
    run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "100000")
    rc, _ = run(capsys, "mark", "--state-dir", d, "--asset", "BTC", "--price", "70000", "--nav", "100500")
    assert rc == 0


def test_mark_nonpositive_or_nonfinite_refused_even_with_flag(tmp_path, capsys):
    d = str(tmp_path)
    rc, out = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "-5", "--nav", "100000", "--allow-jump")
    assert rc == 1 and "price" in out["error"]
    rc, out = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "100", "--nav", "0", "--allow-jump")
    assert rc == 1 and "nav" in out["error"]
    rc, out = run(capsys, "mark", "--state-dir", d, "--asset", "AAPL", "--price", "nan", "--nav", "100000", "--allow-jump")
    assert rc == 1 and "price" in out["error"]
    # nothing slipped into the chain
    assert all(e["event"] != "mark" for e in Ledger(tmp_path).events())
