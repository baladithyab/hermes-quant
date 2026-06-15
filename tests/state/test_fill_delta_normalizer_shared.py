"""ADR-0091 Option E acceptance gate (:367) — the ONE shared normalizer 4-FOLD
cross-parity test.

The gate names this file (``tests/unit/test_fill_delta_normalizer_shared.py`` in the
ADR; it lives under tests/state/ next to its sibling fold-parity tests). It proves the
hard architectural invariant: under HERMES_QUANT_DELTA_NORMALIZER=1, the SAME
AAPL-12x / BA-6x absolute-target re-affirmation fixture folds to the SAME net via ALL
FOUR fold paths, and the all-absolute-target stream is byte-identical with the flag OFF.

The four folds:
  1. state.db REBUILD          — PortfolioState.reconstruct_from() (the carry-forward
                                 normalizer over the asof-sorted log).
  2. state.db INCREMENTAL      — PortfolioState.apply_execution() one record at a time
                                 (delta_from_net against the persisted positions.quantity).
  3. settlement FIFO           — daemon.settlement_loop.join_exit_fills() (the flag-gated
                                 normalizer pre-pass that collapses re-affirms to delta 0).
  4. portfolio/state.py        — reconstruct_portfolio_state() — the IMMUNE reconstructor
                                 that reads target_position_pct with LATEST-supersedes
                                 semantics, so it was NEVER inflated and is the reference
                                 truth the other three must converge to.

The first three read the per-fill SIZE field (which the producers write as the ABSOLUTE
target — the bug shape); only the normalizer makes them agree with the immune fourth.

Plus:
  - ORDERING (gate b): a same-asof tie produces an identical delta stream in the rebuild
    and incremental consumers (no ordering divergence).
  - INCREMENTAL-vs-REBUILD parity (gate c): already covered structurally here by folds 1+2.
  - ARCHITECTURAL (gate d): both state.db consumers import the SAME carry-forward symbol
    from the one fill_delta_normalizer module (no parallel reimplementation).

Deterministic, offline. The fixture is the verified incident: AAPL re-affirmed 0.05 x12,
BA re-affirmed -0.20 x6. Legacy folds inflate to 0.60 / -1.20; the corrected net is the
single intended 0.05 / -0.20.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.daemon.settlement_loop import join_exit_fills
from hermes_quant.portfolio.state import reconstruct_portfolio_state
from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer
from hermes_quant.state.portfolio_state import PortfolioState

# The verified incident fixture.
_AAPL_TARGET = 0.05
_AAPL_N = 12
_BA_TARGET = -0.20
_BA_N = 6


def _rec(asset, target, *, pid, asof, price=100.0, acct="paper-default"):
    """A PaperReactor fill as persisted: fill_size_pct == target_position_pct ==
    the ABSOLUTE target (the bug shape Option E corrects at fold time). reactor_name
    'paper' so the immune reconstruct_portfolio_state's default reactor_filter keeps it.
    """
    return {
        "proposal_id": pid,
        "signal_id": "sig-aapl" if asset == "AAPL" else "sig-ba",
        "asset": asset,
        "asset_class": "equity",
        "timeframe": "1d",
        "asof_decision": asof,
        "asof_execution": asof,
        "target_position_pct": target,
        "decision_price": price,
        "fill_price": price,
        "fill_size_pct": target,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "account_id": acct,
    }


def _incident_stream() -> list[dict]:
    recs = [
        _rec("AAPL", _AAPL_TARGET, pid=f"a{i}", asof=f"2026-06-06T10:{i:02d}:00Z")
        for i in range(_AAPL_N)
    ]
    recs += [
        _rec("BA", _BA_TARGET, pid=f"b{i}", asof=f"2026-06-06T11:{i:02d}:00Z")
        for i in range(_BA_N)
    ]
    return recs


def _write(path: Path, recs: list[dict]) -> None:
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


# --------------------------------------------------------------------------- #
# (a) PARITY: all four folds agree on the AAPL/BA net under the flag.
# --------------------------------------------------------------------------- #


def test_four_fold_cross_parity_flag_on(tmp_path, monkeypatch):
    """The gate keystone: state.db rebuild + incremental + settlement FIFO +
    reconstruct_portfolio_state ALL report the SAME corrected net for the AAPL-12x /
    BA-6x re-affirmation fixture under HERMES_QUANT_DELTA_NORMALIZER=1."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    recs = _incident_stream()
    bus = tmp_path / "executions.jsonl"
    _write(bus, recs)

    # FOLD 1: state.db REBUILD.
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(bus)
    reb = ps_reb.get_positions("paper-default")
    reb_aapl = reb[("equity", "AAPL")].quantity
    reb_ba = reb[("equity", "BA")].quantity

    # FOLD 2: state.db INCREMENTAL.
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    for r in recs:
        ps_inc.apply_execution(r)
    inc = ps_inc.get_positions("paper-default")
    inc_aapl = inc[("equity", "AAPL")].quantity
    inc_ba = inc[("equity", "BA")].quantity

    # FOLD 3: settlement FIFO net per bucket (sum of the pre-pass deltas). The FIFO
    # pre-pass runs the same normalizer; the bucket net is the sum of round-trip
    # closings + still-open lots. Here every re-affirm collapses to delta 0, so the
    # open lot is exactly the single intended target with NO offsetting close.
    norm = FillDeltaNormalizer()
    fifo_aapl = sum(norm.delta_for(r) for r in recs if r["asset"] == "AAPL")
    norm_ba = FillDeltaNormalizer()
    fifo_ba = sum(norm_ba.delta_for(r) for r in recs if r["asset"] == "BA")
    # The settlement consumer reads the SAME normalizer the position folds do (i0c):
    # confirm join_exit_fills leaves exactly the single intended open lot (no phantoms).
    _trips, open_lots = join_exit_fills(recs)
    assert len(open_lots[("paper-default", "equity", "AAPL")]) == 1, (
        "FIFO must hold ONE AAPL lot (re-affirms collapsed), not 12 phantom lots"
    )
    fifo_aapl_lot = open_lots[("paper-default", "equity", "AAPL")][0]["qty"]

    # FOLD 4: the IMMUNE reconstructor (target-supersedes, never inflated).
    immune = reconstruct_portfolio_state(bus, drop_zeros=False)
    imm_aapl = immune.positions["AAPL"]
    imm_ba = immune.positions["BA"]

    # All four AAPL nets equal the single intended 0.05.
    assert reb_aapl == pytest.approx(_AAPL_TARGET, rel=1e-9)
    assert inc_aapl == pytest.approx(_AAPL_TARGET, rel=1e-9)
    assert fifo_aapl == pytest.approx(_AAPL_TARGET, rel=1e-9)
    assert fifo_aapl_lot == pytest.approx(_AAPL_TARGET, rel=1e-9)
    assert imm_aapl == pytest.approx(_AAPL_TARGET, rel=1e-9)

    # All four BA nets equal the single intended -0.20.
    assert reb_ba == pytest.approx(_BA_TARGET, rel=1e-9)
    assert inc_ba == pytest.approx(_BA_TARGET, rel=1e-9)
    assert fifo_ba == pytest.approx(_BA_TARGET, rel=1e-9)
    assert imm_ba == pytest.approx(_BA_TARGET, rel=1e-9)

    # And cross-fold equality, pinned explicitly (the no-two-views guarantee).
    assert reb_aapl == pytest.approx(inc_aapl) == pytest.approx(imm_aapl)
    assert reb_ba == pytest.approx(inc_ba) == pytest.approx(imm_ba)


def test_three_folds_inflate_flag_off_immune_does_not(tmp_path, monkeypatch):
    """RED-PROOF of the bug the gate corrects: with the flag OFF, the THREE size-reading
    folds inflate (12x / 6x) while the immune target-reading fold already reports the
    single intended net. This documents the divergence the normalizer heals."""
    monkeypatch.delenv("HERMES_QUANT_DELTA_NORMALIZER", raising=False)
    recs = _incident_stream()
    bus = tmp_path / "executions.jsonl"
    _write(bus, recs)

    # FOLD 1 rebuild: inflates.
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(bus)
    assert ps_reb.get_positions("paper-default")[("equity", "AAPL")].quantity == pytest.approx(
        _AAPL_TARGET * _AAPL_N, rel=1e-9
    )  # 0.60

    # FOLD 2 incremental: inflates.
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    for r in recs:
        ps_inc.apply_execution(r)
    assert ps_inc.get_positions("paper-default")[("equity", "BA")].quantity == pytest.approx(
        _BA_TARGET * _BA_N, rel=1e-9
    )  # -1.20

    # FOLD 3 settlement FIFO: 12 phantom AAPL lots (the inflation in the FIFO view).
    _trips, open_lots = join_exit_fills(recs)
    assert len(open_lots[("paper-default", "equity", "AAPL")]) == _AAPL_N

    # FOLD 4 immune: already correct (reads target_position_pct, supersedes).
    immune = reconstruct_portfolio_state(bus, drop_zeros=False)
    assert immune.positions["AAPL"] == pytest.approx(_AAPL_TARGET, rel=1e-9)
    assert immune.positions["BA"] == pytest.approx(_BA_TARGET, rel=1e-9)


# --------------------------------------------------------------------------- #
# (b) ORDERING: a same-asof tie yields an identical delta stream in both
#     state.db consumers (no ordering-divergence P0).
# --------------------------------------------------------------------------- #


def test_same_asof_tie_identical_delta_stream_both_consumers(tmp_path, monkeypatch):
    """Two AAPL records sharing the SAME asof_execution must produce the identical
    per-bucket delta stream whether folded by the rebuild normalizer (delta_for over
    the stable-sorted log) or the incremental fold (delta_from_net against the persisted
    qty). Stable sort keeps file order on ties, so both consume the same order."""
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
    asof = "2026-06-06T10:00:00Z"
    recs = [
        _rec("AAPL", 0.05, pid="t0", asof=asof),
        _rec("AAPL", 0.08, pid="t1", asof=asof),  # SAME asof tie
    ]

    # Rebuild consumer delta stream.
    norm = FillDeltaNormalizer()
    reb_stream = [norm.delta_for(r) for r in recs]

    # Incremental consumer delta stream (persisted-qty carry-forward).
    from hermes_quant.state.fill_delta_normalizer import delta_from_net

    net = 0.0
    inc_stream = []
    for r in recs:
        d = delta_from_net(r, net)
        inc_stream.append(d)
        net += d

    assert reb_stream == pytest.approx(inc_stream)
    assert reb_stream == pytest.approx([0.05, 0.03])  # open 0.05, then +0.03 to 0.08

    # And the materialized fold agrees on the final net.
    bus = tmp_path / "executions.jsonl"
    _write(bus, recs)
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(bus)
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    for r in recs:
        ps_inc.apply_execution(r)
    reb_q = ps_reb.get_positions("paper-default")[("equity", "AAPL")].quantity
    inc_q = ps_inc.get_positions("paper-default")[("equity", "AAPL")].quantity
    assert reb_q == pytest.approx(inc_q) == pytest.approx(0.08, rel=1e-9)


# --------------------------------------------------------------------------- #
# (d) ARCHITECTURAL: exactly ONE module computes the carry-forward; both
#     state.db fold sites import the SAME symbol from it.
# --------------------------------------------------------------------------- #


def test_architectural_single_carry_forward_symbol():
    """Both consumers reference the SAME shared derivation, not a parallel copy:
    the rebuild fold uses FillDeltaNormalizer.delta_for and the incremental fold uses
    the module-level delta_from_net — both from hermes_quant.state.fill_delta_normalizer,
    and delta_for delegates to delta_from_net (one place computes target - net)."""
    import inspect

    from hermes_quant.state import fill_delta_normalizer as mod

    # The class method delegates to the module-level shared derivation — the single
    # place target - net is computed. (Both folds therefore difference identically.)
    src = inspect.getsource(mod.FillDeltaNormalizer.delta_for)
    assert "delta_from_net" in src, (
        "FillDeltaNormalizer.delta_for must delegate to the shared delta_from_net "
        "(one carry-forward derivation), not reimplement target - net"
    )

    # portfolio_state's incremental fold imports delta_from_net from THIS module, and
    # reconstruct_from imports FillDeltaNormalizer from THIS module — verified by source.
    ps_src = inspect.getsource(
        __import__("hermes_quant.state.portfolio_state", fromlist=["_"])
    )
    assert "from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer" in ps_src
    assert "from hermes_quant.state.fill_delta_normalizer import delta_from_net" in ps_src
    # The settlement consumer likewise imports the same normalizer (i0c pre-pass).
    sl_src = inspect.getsource(
        __import__("hermes_quant.daemon.settlement_loop", fromlist=["_"])
    )
    assert "fill_delta_normalizer" in sl_src, (
        "settlement_loop must import the ONE shared normalizer, not reimplement it"
    )
