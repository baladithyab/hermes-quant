"""ar19/ar20/ar21 — close three fail-OPEN holes on the always-on ADR-0016 kill-switch rail.

Found by the convergence review (wf wi06keswe), all RED-verified:

  ar19 — settlement_loop._normalize_exec_record had NO cs44 multi_leg family-PARENT skip
         (portfolio_state DOES skip it). The parent record (asset_class="multi_leg",
         nonzero fill_size_pct) produced a PHANTOM round-trip in join_exit_fills ON TOP of
         the real per-leg children -> the family's realized P&L was double-counted into
         compute_cumulative_realized_pnl_pct (the live kill-switch basis). A phantom gain
         masks a real loss -> the rail fails to trip.
  ar20 — compute_cumulative_realized_pnl_pct returned a bare 0.0 when NAV was unreadable
         (state.db corrupt/locked while executions.jsonl readable) even with a NONZERO
         realized loss -> the D9 drawdown rail silently disarmed. The ar02 fix only
         covered the OUTER except; this NAV-None branch was the sibling hole.
  ar21 — _read_kill_switch returned tripped=False on a corrupt/torn EXISTING file ->
         a previously-TRIPPED rail silently RE-ARMED trading. An existing-but-unreadable
         flag must fail CLOSED (tripped=True); an ABSENT file stays not-tripped.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto


def _write_bus(tmp_path: Path, fills: list[dict]) -> Path:
    p = tmp_path / "executions.jsonl"
    p.write_text("\n".join(json.dumps(f) for f in fills) + "\n", encoding="utf-8")
    return p


def _equity_fill(asset, pct, price, asof, pid, asset_class="equity", **extra):
    rec = {
        "asset": asset,
        "asset_class": asset_class,
        "timeframe": "1d",
        "fill_size_pct": pct,
        "fill_price": price,
        "decision_price": price,
        "asof_execution": asof,
        "asof_decision": asof,
        "bar_ts": asof,
        "proposal_id": pid,
        "signal_id": None,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "play_tag": "autonomous",
        "reactor_metadata": {"paper": True},
        "target_position_pct": pct,
        "approver_user_id": "test",
    }
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
# ar19 — multi-leg family-PARENT must NOT double-count into the kill-switch basis
# --------------------------------------------------------------------------- #
def test_ar19_multileg_parent_does_not_double_count_realized_pnl(tmp_path, monkeypatch):
    """A full open+close multi-leg family: the parent rollup (asset_class='multi_leg')
    plus its per-leg child. The realized P&L must come from the CHILD round-trip ONLY —
    the parent must be skipped, not produce a phantom second round-trip."""
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100000.0)

    # Per-leg us_option child: open +0.2 @ 100, close -0.2 @ 80 (a -20% lot).
    child_open = _equity_fill(
        "NVDA260626P00130000", 0.2, 100.0, "2026-06-01T15:00:00Z", "fam1",
        asset_class="us_option",
        reactor_metadata={"paper": True, "quantity": 1, "leg_index": 0, "role": "leg"},
    )
    child_close = _equity_fill(
        "NVDA260626P00130000", -0.2, 80.0, "2026-06-02T15:00:00Z", "fam2",
        asset_class="us_option",
        reactor_metadata={"paper": True, "quantity": -1, "leg_index": 0, "role": "leg"},
    )
    # Family-PARENT rollups (asset_class="multi_leg", nonzero fill_size_pct, role=parent).
    # These must be SKIPPED by _normalize_exec_record (ar19) — folding them produces a
    # phantom round-trip that double-counts the family's realized P&L.
    parent_open = _equity_fill(
        "NVDA", 0.2, 100.0, "2026-06-01T15:00:00Z", "fam1",
        asset_class="multi_leg",
        reactor_metadata={"paper": True, "role": "parent"},
    )
    parent_close = _equity_fill(
        "NVDA", -0.2, 80.0, "2026-06-02T15:00:00Z", "fam2",
        asset_class="multi_leg",
        reactor_metadata={"paper": True, "role": "parent"},
    )

    bus_with_parent = _write_bus(
        tmp_path, [parent_open, child_open, parent_close, child_close]
    )
    with_parent = auto.compute_cumulative_realized_pnl_pct(bus_with_parent)

    # The SAME family without the parent rollup rows — the true realized P&L.
    child_only_path = tmp_path / "child_only.jsonl"
    child_only_path.write_text(
        "\n".join(json.dumps(f) for f in [child_open, child_close]) + "\n",
        encoding="utf-8",
    )
    child_only = auto.compute_cumulative_realized_pnl_pct(child_only_path)

    assert with_parent == child_only, (
        "ar19: the multi_leg family-PARENT rollup double-counted realized P&L into the "
        f"kill-switch basis (with-parent={with_parent} vs child-only={child_only}); the "
        "parent must be skipped exactly like portfolio_state's cs44 fold-skip."
    )
    assert with_parent < 0.0  # the -20% lot is a real loss; sign must survive the skip


def test_ar19_normalize_skips_multileg_parent_directly(tmp_path):
    """Unit-level: _normalize_exec_record returns None for a multi_leg parent record."""
    from hermes_quant.daemon.settlement_loop import _normalize_exec_record

    parent = _equity_fill(
        "NVDA", 0.2, 100.0, "2026-06-01T15:00:00Z", "fam1",
        asset_class="multi_leg",
        reactor_metadata={"paper": True, "role": "parent"},
    )
    assert _normalize_exec_record(parent) is None
    # A real equity child is still normalized (not over-skipped).
    child = _equity_fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1")
    assert _normalize_exec_record(child) is not None


# --------------------------------------------------------------------------- #
# ar20/ar25 — the basis no longer reads NAV, so NAV-None cannot disarm it.
# (ar25 made the contribution realized_return × qty, where qty is already a
# NAV-fraction. NAV is never read for the basis, which structurally subsumes the
# old ar20 NAV-None fail-open — there is no NAV-None branch left to fail.)
# --------------------------------------------------------------------------- #
def test_ar25_basis_independent_of_nav_none(tmp_path, monkeypatch):
    """A 20%-NAV position down 20% (= -4% of NAV realized) computes the SAME fraction
    whether NAV is readable or None — the ar25 basis does not divide by NAV, so a
    momentarily-unreadable NAV can no longer disarm the D9 rail (the old ar20 hole)."""
    bus = _write_bus(tmp_path, [
        _equity_fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _equity_fill("ASTS", -0.2, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: None)  # NAV unreadable
    frac_navnone = auto.compute_cumulative_realized_pnl_pct(bus)
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100000.0)
    frac_nav = auto.compute_cumulative_realized_pnl_pct(bus)
    # 0.2 NAV-fraction lot, -20% return -> -0.04 of NAV; identical regardless of NAV.
    assert frac_navnone == pytest.approx(-0.04)
    assert frac_navnone == pytest.approx(frac_nav)


def test_ar25_realistic_drawdown_trips_threshold(tmp_path, monkeypatch):
    """ar25 RED→GREEN: a -10%-of-NAV realized drawdown reads -0.10 (was ~-0.0001 ~1000x
    understated) so the default 10% kill threshold actually trips."""
    bus = _write_bus(tmp_path, [
        _equity_fill("BIG", 0.5, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _equity_fill("BIG", -0.5, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100000.0)
    frac = auto.compute_cumulative_realized_pnl_pct(bus)
    assert frac == pytest.approx(-0.10), (
        f"ar25: a 50%-NAV position down 20% must read -0.10 of NAV (got {frac}); "
        "the pre-fix ×entry_price ÷nav double-discount read ~-0.0001 and never tripped."
    )
    assert frac <= -0.10  # trips the default kill_switch_pct=0.10


def test_ar25_empty_book_is_zero(tmp_path):
    """An empty/absent book legitimately returns 0.0 (no realized P&L yet)."""
    assert auto.compute_cumulative_realized_pnl_pct(_write_bus(tmp_path, [])) == 0.0


# --------------------------------------------------------------------------- #
# ar34 — the kill-switch basis must NOT pool cross-account (freqtrade raw-coin) fills
# --------------------------------------------------------------------------- #
def _freqtrade_fill(asset, side, qty, price, asof, eid):
    """A freqtrade consumer execution record: account_id='freqtrade', explicit side/qty
    where qty is a RAW COIN COUNT (NOT a NAV fraction), no fill_size_pct/target."""
    return {
        "asset": asset,
        "asset_class": "crypto",
        "side": side,
        "qty": qty,
        "fill_price": price,
        "decision_price": price,
        "asof_execution": asof,
        "asof": asof,
        "exec_id": eid,
        "signal_id": None,
        "account_id": "freqtrade",
        "reactor_metadata": {"account_id": "freqtrade"},
    }


def test_ar34_killswitch_excludes_freqtrade_account(tmp_path, monkeypatch):
    """The paper-default autonomous lane's realized-drawdown basis must IGNORE a
    cross-account freqtrade crypto round-trip whose qty is a raw coin count. Pooling a
    0.5-coin qty as if it were a 50%-NAV fraction would corrupt (here, swamp) the rail."""
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    # paper-default: a 20%-NAV position down 20% = -4% of NAV realized.
    paper = [
        _equity_fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _equity_fill("ASTS", -0.2, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ]
    # freqtrade: a 0.5-coin round-trip up 50% (raw coins, NOT NAV) — would read as a huge
    # spurious gain (+0.25) if pooled, masking the paper loss.
    ft = [
        _freqtrade_fill("ETH/USDT", "buy", 0.5, 100.0, "2026-06-01T16:00:00Z", "f1"),
        _freqtrade_fill("ETH/USDT", "sell", 0.5, 150.0, "2026-06-02T16:00:00Z", "f2"),
    ]
    bus_mixed = _write_bus(tmp_path, paper + ft)
    paper_only_path = tmp_path / "paper_only.jsonl"
    paper_only_path.write_text("\n".join(json.dumps(f) for f in paper) + "\n", encoding="utf-8")

    frac_mixed = auto.compute_cumulative_realized_pnl_pct(bus_mixed)
    frac_paper = auto.compute_cumulative_realized_pnl_pct(paper_only_path)
    assert frac_mixed == pytest.approx(frac_paper), (
        f"ar34: the freqtrade cross-account round-trip polluted the paper-default kill-switch "
        f"basis (mixed={frac_mixed} vs paper-only={frac_paper}); the basis must exclude non-"
        f"paper-default accounts (raw-coin qty != NAV fraction)."
    )
    assert frac_mixed == pytest.approx(-0.04)  # the real paper loss, undistorted


# --------------------------------------------------------------------------- #
# ar21 — corrupt EXISTING kill-switch file fails CLOSED (tripped=True)
# --------------------------------------------------------------------------- #
def test_ar21_corrupt_existing_killswitch_file_fails_closed(tmp_path, monkeypatch):
    ks = tmp_path / "autonomous_kill_switch.json"
    # A torn/half write of a TRIPPED flag.
    ks.write_text('{"tripped": tru', encoding="utf-8")
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", ks, raising=False)
    state = auto._read_kill_switch()
    assert state.tripped is True, (
        "ar21: a corrupt EXISTING kill-switch file must fail CLOSED (tripped=True) so a "
        "previously-tripped rail cannot silently re-arm."
    )
    assert "unreadable" in (state.reason or "")


def test_ar21_absent_killswitch_file_stays_not_tripped(tmp_path, monkeypatch):
    ks = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", ks, raising=False)
    state = auto._read_kill_switch()
    assert state.tripped is False, "an ABSENT file is a legit cold start (not tripped)"


def test_ar21_valid_tripped_file_reads_tripped(tmp_path, monkeypatch):
    ks = tmp_path / "autonomous_kill_switch.json"
    ks.write_text(json.dumps({
        "tripped": True, "tripped_at": "2026-06-15T00:00:00Z",
        "cumulative_pnl_pct": -0.2, "threshold_pct": 0.1, "reason": "drawdown",
    }), encoding="utf-8")
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", ks, raising=False)
    assert auto._read_kill_switch().tripped is True


def test_ar21_valid_untripped_file_reads_not_tripped(tmp_path, monkeypatch):
    ks = tmp_path / "autonomous_kill_switch.json"
    ks.write_text(json.dumps({
        "tripped": False, "tripped_at": None,
        "cumulative_pnl_pct": 0.0, "threshold_pct": 0.1, "reason": None,
    }), encoding="utf-8")
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", ks, raising=False)
    assert auto._read_kill_switch().tripped is False
