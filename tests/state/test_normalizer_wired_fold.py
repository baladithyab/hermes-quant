"""Increment-0 §0.3 (seed ra01, ADR-0091 Option E): the shared normalizer wired
into the rebuild fold, behind HERMES_QUANT_DELTA_NORMALIZER (default-OFF).

- Flag OFF (default): reconstruct_from is bit-for-bit legacy — re-affirmations still
  inflate (this is the state cr09's strict-xfail documents).
- Flag ON: re-affirmations of an unchanged target fold to the single intended
  position, cost basis unchanged, cash moved once — the ADR-0091 fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.state.portfolio_state import PortfolioState


def _exec(asset, target, *, pid, asof, price=100.0):
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
        "fill_size_pct": target,  # ABSOLUTE target written every fire (the bug shape)
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "account_id": "paper-default",
    }


def _write_reaffirmations(path: Path, asset, target, n) -> None:
    with open(path, "w") as f:
        for i in range(n):
            asof = f"2026-06-06T10:{i:02d}:00Z"  # distinct asof per fire, ascending
            f.write(json.dumps(_exec(asset, target, pid=f"p_{asset}_{i}", asof=asof)) + "\n")


def test_flag_off_is_legacy_inflation(tmp_path, monkeypatch):
    # Default (flag absent) MUST be bit-for-bit legacy: 12 re-affirmations inflate.
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    bus = tmp_path / "executions.jsonl"
    _write_reaffirmations(bus, "AAPL", 0.05, 12)

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.reconstruct_from(bus)
    pos = ps.get_positions("paper-default")
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.60, rel=1e-9), (
        "flag OFF must preserve the legacy inflation (12 x 0.05) bit-for-bit"
    )


def test_flag_on_reaffirmation_does_not_inflate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    bus = tmp_path / "executions.jsonl"
    _write_reaffirmations(bus, "AAPL", 0.05, 12)

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.reconstruct_from(bus)
    pos = ps.get_positions("paper-default")
    # The single intended 0.05 — NOT 12 x 0.05.
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.05, rel=1e-9)


def test_flag_on_genuine_change_accumulates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    bus = tmp_path / "executions.jsonl"
    with open(bus, "w") as f:
        # 5% -> 7% -> 7% (reaffirm): net should be 0.07, not 0.05+0.07+0.07.
        f.write(json.dumps(_exec("AAPL", 0.05, pid="a", asof="2026-06-06T10:00:00Z")) + "\n")
        f.write(json.dumps(_exec("AAPL", 0.07, pid="b", asof="2026-06-06T10:01:00Z")) + "\n")
        f.write(json.dumps(_exec("AAPL", 0.07, pid="c", asof="2026-06-06T10:02:00Z")) + "\n")

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.reconstruct_from(bus)
    pos = ps.get_positions("paper-default")
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.07, rel=1e-9)


def test_flag_on_cash_moves_once_for_reaffirmations(tmp_path, monkeypatch):
    # Re-affirmations trade nothing, so cash must move only on the opening fire.
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    bus = tmp_path / "executions.jsonl"
    _write_reaffirmations(bus, "AAPL", 0.05, 6)

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.reconstruct_from(bus)
    cash_on = ps.get_cash("paper-default")

    # Compare with a single fire: cash impact must be identical (re-affirm = no-op).
    bus2 = tmp_path / "executions_single.jsonl"
    _write_reaffirmations(bus2, "AAPL", 0.05, 1)
    ps2 = PortfolioState(state_db_path=tmp_path / "state2.db")
    ps2.reconstruct_from(bus2)
    cash_single = ps2.get_cash("paper-default")

    assert cash_on.balance_usd == pytest.approx(cash_single.balance_usd, rel=1e-9), (
        "re-affirmations must not move cash beyond the single opening fire"
    )
