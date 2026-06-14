"""ft1 (2026-06-13): delta-normalizer REGIME STAMP guards a flag-flip phantom-sell.

The normalizer fold (ADR-0091 Option E, default-OFF behind
HERMES_QUANT_DELTA_NORMALIZER) reads the flag at fold time in BOTH the rebuild
(reconstruct_from) and incremental (apply_execution) paths. Flipping the flag ON
against a populated state.db built under the flag OFF differences NEW absolute
targets against an INFLATED running net => phantom sells.

The fix stamps the BUILD regime in PRAGMA user_version and hard-refuses an
incremental apply whose current flag regime disagrees with a populated db's stamp.

Rails proven here:
  (1) flag-OFF default: a legacy db (user_version 0) folds with NO refusal and the
      positions are byte-identical to today's legacy (inflated) result;
  (2) flag-flip ON against an OFF-built populated db RAISES a regime-mismatch
      RuntimeError BEFORE any fold (no phantom sell);
  (3) reconstruct_from under flag ON restamps user_version and a subsequent
      flag-ON apply succeeds.
"""

from __future__ import annotations

import json

import pytest

from hermes_quant.state.portfolio_state import (
    _REGIME_OFF,
    _REGIME_ON,
    PortfolioState,
)


def _rec(asset, target, *, pid, asof, price=100.0, acct="paper-default"):
    return {
        "proposal_id": pid,
        "signal_id": None,
        "asset": asset,
        "asset_class": "equity",
        "timeframe": "1d",
        "asof_decision": asof,
        "asof_execution": asof,
        "target_position_pct": target,
        "decision_price": price,
        "fill_price": price,
        "fill_size_pct": target,  # ABSOLUTE target every fire
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "account_id": acct,
    }


def _user_version(ps: PortfolioState) -> int:
    with ps._conn() as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_flag_off_legacy_db_no_refusal_byte_identical(tmp_path, monkeypatch):
    """Flag OFF (default): a legacy db never trips the guard, and the positions
    match today's legacy incremental (inflated) result byte-for-byte."""
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    recs = [
        _rec("AAPL", 0.05, pid=f"p{i}", asof=f"2026-06-06T10:{i:02d}:00Z")
        for i in range(12)
    ]
    ps = PortfolioState(state_db_path=tmp_path / "inc.db")
    for r in recs:
        ps.apply_execution(r)  # must NOT raise

    pos = ps.get_positions("paper-default")
    # Legacy incremental inflation (12 × 0.05) — unchanged by the stamp.
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.60, rel=1e-9)
    # Flag-OFF stamp is 0 == legacy never-stamped: byte-identical user_version.
    assert _user_version(ps) == _REGIME_OFF


def test_flag_flip_on_against_off_built_db_raises(tmp_path, monkeypatch):
    """Build a populated db under flag OFF, then flip the flag ON: the next apply
    must RAISE a regime-mismatch RuntimeError (no phantom sell)."""
    db = tmp_path / "inc.db"

    # Phase 1: build under flag OFF.
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    ps_off = PortfolioState(state_db_path=db)
    ps_off.apply_execution(_rec("AAPL", 0.05, pid="p0", asof="2026-06-06T10:00:00Z"))
    assert _user_version(ps_off) == _REGIME_OFF
    assert ps_off.get_positions("paper-default")  # populated

    # Phase 2: flip the flag ON and apply against the OFF-built populated db.
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    ps_on = PortfolioState(state_db_path=db)
    with pytest.raises(RuntimeError, match="regime mismatch"):
        ps_on.apply_execution(_rec("AAPL", 0.05, pid="p1", asof="2026-06-06T10:01:00Z"))

    # The db was NOT mutated by the refused apply (still OFF-built, still inflated-by-1).
    assert _user_version(ps_on) == _REGIME_OFF
    assert ps_on.get_positions("paper-default")[("equity", "AAPL")].quantity == pytest.approx(
        0.05, rel=1e-9
    )


def test_reconstruct_under_flag_on_restamps_and_apply_succeeds(tmp_path, monkeypatch):
    """A reconstruct_from under flag ON restamps user_version to ON, after which a
    flag-ON incremental apply succeeds (regimes agree)."""
    db = tmp_path / "inc.db"

    # Build a populated db under flag OFF first (the inflated legacy state).
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    bus = tmp_path / "executions.jsonl"
    recs = [
        _rec("AAPL", 0.05, pid=f"p{i}", asof=f"2026-06-06T10:{i:02d}:00Z")
        for i in range(3)
    ]
    with open(bus, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    ps_seed = PortfolioState(state_db_path=db)
    for r in recs:
        ps_seed.apply_execution(r)
    assert _user_version(ps_seed) == _REGIME_OFF

    # Now rebuild under flag ON — this restamps the regime to ON and deflates.
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    ps_on = PortfolioState(state_db_path=db)
    ps_on.reconstruct_from(bus)
    assert _user_version(ps_on) == _REGIME_ON
    # Option E: re-affirmed 0.05 target deflates to the single intended 0.05.
    assert ps_on.get_positions("paper-default")[("equity", "AAPL")].quantity == pytest.approx(
        0.05, rel=1e-9
    )

    # A subsequent flag-ON apply now agrees with the ON stamp — no refusal.
    ps_on.apply_execution(_rec("AAPL", 0.05, pid="p_after", asof="2026-06-06T10:09:00Z"))
    # Re-affirmation under ON is a no-op (delta 0): still 0.05.
    assert ps_on.get_positions("paper-default")[("equity", "AAPL")].quantity == pytest.approx(
        0.05, rel=1e-9
    )
