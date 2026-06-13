"""Increment-0 follow-up i0b (seed i0b, ADR-0091 Option E): the carry-forward
normalizer's per-bucket delta derivation is order-dependent, so reconstruct_from
must fold records in asof order when the normalizer is active.

The risk (Inc-0 adversarial review, check b): delta = target - running_net is
order-sensitive. If executions.jsonl is NOT asof-ascending within a bucket (an
out-of-order append), the intermediate deltas and the carry-forward net come out
wrong. The final net is order-invariant only for a true append log; the read site
should not depend on that unverified invariant.

Fix: when HERMES_QUANT_DELTA_NORMALIZER=1, reconstruct_from stable-sorts records by
asof_execution before folding (stable so same-asof ties keep file order — the
reviewer's identical-delta-stream requirement). Flag OFF = raw file order, bit-for-bit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.state.portfolio_state import PortfolioState


def _rec(asset, target, *, pid, asof, price=100.0):
    return {
        "proposal_id": pid, "signal_id": None, "asset": asset, "asset_class": "equity",
        "timeframe": "1d", "asof_decision": asof, "asof_execution": asof,
        "target_position_pct": target, "decision_price": price, "fill_price": price,
        "fill_size_pct": target, "reactor_name": "paper", "human_in_the_loop": True,
        "account_id": "paper-default",
    }


def test_out_of_order_file_folds_correctly_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    # A genuine target progression 0.05 -> 0.10 -> 0.15, but written OUT OF ORDER
    # in the file (asof timestamps are correct; the append order is scrambled).
    in_order = [
        _rec("AAPL", 0.05, pid="a", asof="2026-06-06T10:00:00Z"),
        _rec("AAPL", 0.10, pid="b", asof="2026-06-06T10:01:00Z"),
        _rec("AAPL", 0.15, pid="c", asof="2026-06-06T10:02:00Z"),
    ]
    scrambled = [in_order[2], in_order[0], in_order[1]]  # c, a, b

    bus = tmp_path / "executions.jsonl"
    with open(bus, "w") as f:
        for r in scrambled:
            f.write(json.dumps(r) + "\n")

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.reconstruct_from(bus)
    pos = ps.get_positions("paper-default")
    # Correct final net is the last-by-asof target = 0.15. Without an asof sort the
    # scrambled carry-forward would mis-derive the intermediate deltas.
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.15, rel=1e-9)


def test_in_order_and_scrambled_agree_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    recs = [
        _rec("AAPL", 0.05, pid="a", asof="2026-06-06T10:00:00Z"),
        _rec("AAPL", 0.10, pid="b", asof="2026-06-06T10:01:00Z"),
        _rec("AAPL", 0.05, pid="c", asof="2026-06-06T10:02:00Z"),  # genuine REDUCE back to 0.05
    ]
    import random

    def _net(order):
        bus = tmp_path / f"bus_{order}.jsonl"
        seq = list(recs)
        if order == "scrambled":
            seq = [recs[1], recs[2], recs[0]]
        with open(bus, "w") as f:
            for r in seq:
                f.write(json.dumps(r) + "\n")
        ps = PortfolioState(state_db_path=tmp_path / f"state_{order}.db")
        ps.reconstruct_from(bus)
        return ps.get_positions("paper-default")[("equity", "AAPL")].quantity

    assert _net("in_order") == pytest.approx(_net("scrambled"), rel=1e-9)
    assert _net("in_order") == pytest.approx(0.05, rel=1e-9)
