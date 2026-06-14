"""Increment-0 §0.3 follow-up i0a (seed ra01, ADR-0091 Option E): the normalizer
wired into the INCREMENTAL apply_execution path, in agreement with the rebuild fold.

The reviewer's gap: §0.3 wired only reconstruct_from (rebuild). apply_execution
(live PaperReactor path) still folded the raw absolute target, so flag-ON would make
a live session inflate incrementally while a rebuild deflated — live state.db vs a
rebuild diverging mid-session. i0a closes that: apply_execution derives the same
carry-forward delta, sourcing the running net from the PERSISTED positions row, so
the two folds converge by construction.

- Flag OFF: apply_execution is bit-for-bit legacy (still inflates incrementally).
- Flag ON: incremental apply == rebuild for the same record stream (the parity gate).
"""

from __future__ import annotations

import pytest

from hermes_quant.state.fill_delta_normalizer import delta_from_net
from hermes_quant.state.portfolio_state import PortfolioState


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


# ---- the shared derivation helper (one place computes target - net) ----

def test_delta_from_net_absolute_target():
    # absolute-target record: delta = target - current_net
    assert delta_from_net(_rec("AAPL", 0.05, pid="a", asof="t"), 0.0) == 0.05
    assert abs(delta_from_net(_rec("AAPL", 0.05, pid="b", asof="t"), 0.05)) < 1e-12  # reaffirm
    assert abs(delta_from_net(_rec("AAPL", 0.07, pid="c", asof="t"), 0.05) - 0.02) < 1e-12  # ADD


def test_delta_from_net_quantity_lane():
    r = _rec("AAPL", 0.05, pid="q", asof="t")
    r["reactor_metadata"] = {"quantity": 33.33}
    assert delta_from_net(r, 0.0) == 33.33  # shares lane, derived in shares
    assert abs(delta_from_net(r, 33.33)) < 1e-12  # reaffirm in shares


def test_delta_from_net_true_delta_passthrough():
    r = _rec("AAPL", 0.05, pid="d", asof="t")
    r["schema_version"] = "true-delta-v1"
    assert delta_from_net(r, 0.05) == 0.05  # already a delta — net ignored, passthrough


# ---- the parity gate: incremental apply == rebuild, flag ON ----

def _apply_stream(ps, recs):
    for r in recs:
        ps.apply_execution(r)


def test_incremental_matches_rebuild_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    recs = [
        _rec("AAPL", 0.05, pid=f"p{i}", asof=f"2026-06-06T10:{i:02d}:00Z") for i in range(12)
    ] + [
        _rec("BA", -0.2, pid=f"b{i}", asof=f"2026-06-06T11:{i:02d}:00Z") for i in range(6)
    ]

    # Incremental path: apply one record at a time.
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    _apply_stream(ps_inc, recs)
    inc = ps_inc.get_positions("paper-default")

    # Rebuild path: write the same stream and reconstruct.
    import json
    bus = tmp_path / "executions.jsonl"
    with open(bus, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(bus)
    reb = ps_reb.get_positions("paper-default")

    # Both must agree on the single intended positions (NOT inflated).
    assert inc[("equity", "AAPL")].quantity == pytest.approx(0.05, rel=1e-9)
    assert inc[("equity", "BA")].quantity == pytest.approx(-0.2, rel=1e-9)
    assert inc[("equity", "AAPL")].quantity == pytest.approx(reb[("equity", "AAPL")].quantity, rel=1e-9)
    assert inc[("equity", "BA")].quantity == pytest.approx(reb[("equity", "BA")].quantity, rel=1e-9)


@pytest.mark.xfail(
    reason=(
        "ms1 (2026-06-13) DORMANT divergence: in a bucket that MIXES absolute-target "
        "and true-delta records, FillDeltaNormalizer.delta_for() returns a true-delta "
        "record's size WITHOUT advancing its in-memory running_net (rebuild), but "
        "apply_execution DOES advance the persisted state.db qty by that delta and "
        "then differences the NEXT absolute target against it (incremental). So the "
        "two folds seed the next target-difference from different bases and diverge "
        "(incremental 0.05 vs rebuild 0.07 for [abs 0.05, true-delta +0.02, abs 0.05]). "
        "Dormant today: no producer emits schema_version='true-delta-v1' (every record "
        "defaults schema_version=None == absolute-target), so the mixed stream cannot "
        "occur in production. This test pins the desired parity (it WILL fail until the "
        "advance rule is reconciled); flipping it to pass is the deferred fix's gate."
    ),
    strict=True,
)
def test_mixed_schema_bucket_incremental_vs_rebuild_parity(tmp_path, monkeypatch):
    """RED (xfail): a single bucket mixing absolute-target + true-delta fills folds
    to DIFFERENT final positions via the incremental vs rebuild paths.

    The desired (asserted) invariant is parity. It currently FAILS because the
    true-delta record advances the persisted net in the incremental fold but NOT the
    normalizer's in-memory net in the rebuild fold — the ms1 divergence.
    """
    import json

    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")

    def _delta_rec(asset, size, *, pid, asof):
        r = _rec(asset, size, pid=pid, asof=asof)
        r["schema_version"] = "true-delta-v1"  # NOT absolute-target -> passthrough
        return r

    recs = [
        _rec("AAPL", 0.05, pid="m0", asof="2026-06-06T10:00:00Z"),
        _delta_rec("AAPL", 0.02, pid="m1", asof="2026-06-06T10:01:00Z"),
        _rec("AAPL", 0.05, pid="m2", asof="2026-06-06T10:02:00Z"),
    ]

    # Incremental fold.
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    _apply_stream(ps_inc, recs)
    inc = ps_inc.get_positions("paper-default")[("equity", "AAPL")].quantity

    # Rebuild fold over the same stream.
    bus = tmp_path / "executions.jsonl"
    with open(bus, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(bus)
    reb = ps_reb.get_positions("paper-default")[("equity", "AAPL")].quantity

    # Desired invariant (currently violated => xfail): the two folds agree.
    assert inc == pytest.approx(reb, rel=1e-9)


def test_incremental_flag_off_is_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    recs = [_rec("AAPL", 0.05, pid=f"p{i}", asof=f"2026-06-06T10:{i:02d}:00Z") for i in range(12)]
    ps = PortfolioState(state_db_path=tmp_path / "inc.db")
    _apply_stream(ps, recs)
    pos = ps.get_positions("paper-default")
    # Flag OFF = legacy incremental inflation.
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.60, rel=1e-9)
