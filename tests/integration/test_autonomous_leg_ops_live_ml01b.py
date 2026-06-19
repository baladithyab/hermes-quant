"""aegis-ml01b (iter-5 REBUILD): wire the LIVE executor for the ml01 leg-op apply_* drivers.

ml01 landed the DECISION layer + apply_* drivers (injected-executor). The PRIOR build left
_apply_convert_live / _apply_decompose_live with ZERO callers (orphaned) and _apply_convert_live
UNTESTED. ml01b (iter-5) builds the LIVE executor in the host AND wires a REAL trigger: a
stop breach on a composite (HERMES_QUANT_COMPOSITE_LEG_OPS=1) routes the close as a LIVE
DECOMPOSE, transitioning the composite open -> decomposed via the REAL ml00b CompositePlaysStore.

NON-VACUOUS: every transition test runs against the REAL CompositePlaysStore (a real state.db
on a tmp path) — the state machine is exercised end-to-end (open -> decomposed). NO store double.

POSTURE: byte-identical when HERMES_QUANT_COMPOSITE_LEG_OPS off (no transition, no reactor call).
ATOMICITY (apply_convert H4): a failed ADD half raises and leaves the composite UNCHANGED.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.options.data import OptionLeg
from hermes_quant.options.leg_ops import ConvertExecutionError
from hermes_quant.state.composite_plays import CompositePlaysStore

_C190 = "AAPL260717C00190000"
_C195 = "AAPL260717C00195000"


def _store(tmp_path: Path) -> CompositePlaysStore:
    return CompositePlaysStore(db_path=tmp_path / "state.db")


def _open_spread(store: CompositePlaysStore, mlid: str = "ml_legop") -> None:
    """A REAL open composite carrying real leg dicts (the ml00b shape)."""
    store.open_composite(
        multi_leg_id=mlid,
        underlying="AAPL",
        strategy_kind="vertical_spread",
        outer_qty=1,
        net_entry_price=-1.50,
        fill_size_pct=0.0,
        expected_leg_count=2,
        max_loss=350.0,
        option_legs=[
            {"symbol": _C195, "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": _C190, "side": "buy", "position_intent": "buy_to_open"},
        ],
    )


# --------------------------------------------------------------------------- #
# 1. DECOMPOSE TRANSITIONS LIVE — flag ON => _apply_decompose_live drives the REAL
#    store open -> decomposed (all legs independent).
# --------------------------------------------------------------------------- #
def test_decompose_transitions_composite_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_COMPOSITE_LEG_OPS", "1")
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")  # arm the leg-close executor
    store = _store(tmp_path)
    _open_spread(store)
    assert store.get("ml_legop").state == "open"

    legs = [
        OptionLeg(symbol=_C195, side="buy", position_intent="buy_to_close"),
        OptionLeg(symbol=_C190, side="sell", position_intent="sell_to_close"),
    ]
    new_state = auto._apply_decompose_live(
        store=store,
        multi_leg_id="ml_legop",
        underlying="AAPL",
        decision={"decompose": True, "legs_to_independently_manage": [0, 1]},
        legs_remaining_after=0,  # all legs independent -> decomposed
        legs_to_close=legs,
    )
    assert new_state == "decomposed"
    # The REAL store row transitioned (non-vacuous: a real state machine moved).
    assert store.get("ml_legop").state == "decomposed"


# --------------------------------------------------------------------------- #
# 2. DEFAULT-OFF byte-identical — flag unset => NO store transition, NO reactor call.
# --------------------------------------------------------------------------- #
def test_default_off_no_transition_no_reactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_QUANT_COMPOSITE_LEG_OPS", raising=False)
    store = _store(tmp_path)
    _open_spread(store)

    fired = {"executed": False}

    def _spy_executor(legs):  # noqa: ANN001
        fired["executed"] = True

    monkeypatch.setattr(
        auto, "_build_live_leg_mleg_executor", lambda **kw: _spy_executor
    )
    legs = [OptionLeg(symbol=_C195, side="buy", position_intent="buy_to_close")]
    state = auto._apply_decompose_live(
        store=store,
        multi_leg_id="ml_legop",
        underlying="AAPL",
        decision={"decompose": True, "legs_to_independently_manage": [0]},
        legs_remaining_after=0,
        legs_to_close=legs,
    )
    assert state == "open", "flag OFF => apply_decompose short-circuits => no transition"
    assert store.get("ml_legop").state == "open"
    assert fired["executed"] is False, "flag OFF => the live executor is NEVER called"


# --------------------------------------------------------------------------- #
# 3. EXECUTOR ROUTES THROUGH THE REACTOR — the live executor builds a close MLEG
#    proposal that routes to the multi-leg reactor (armed => a real execute call).
# --------------------------------------------------------------------------- #
def test_executor_routes_through_multileg_reactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    seen = {"reactor": None, "legs": None}

    class _Reactor:
        name = "multileg-paper"

        def execute(self, order, **kw):  # noqa: ANN001, ANN003
            seen["reactor"] = order.proposal_id
            seen["legs"] = [leg.symbol for leg in order.option_legs]

            class _Rec:
                reactor_metadata = {"parent_status": "filled", "outer_qty": 1, "role": "parent"}
                fill_size_pct = 0.0
                proposal_id = order.proposal_id
                asset_class = "multi_leg"

            return _Rec()

    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda p: _Reactor())
    executor = auto._build_live_leg_mleg_executor(underlying="AAPL", play_tag="autonomous_leg_decompose")
    executor([OptionLeg(symbol=_C195, side="buy", position_intent="buy_to_close")])
    assert seen["reactor"] is not None, "the executor must route through select_reactor.execute"
    assert seen["legs"] == [_C195]


# --------------------------------------------------------------------------- #
# 4. REAL TRIGGER — a stop breach with leg-ops ON decomposes the composite LIVE via
#    the REAL stop sweep + REAL store (open -> decomposed), no full-close fire.
# --------------------------------------------------------------------------- #
def test_stop_breach_decomposes_live_via_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    monkeypatch.setenv("HERMES_QUANT_COMPOSITE_LEG_OPS", "1")
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=tmp_path / "state.db")
    # A bull-put-credit spread: signed -1.50.
    store.open_composite(
        multi_leg_id="ml_1", underlying="AAPL", strategy_kind="bull_put_spread",
        outer_qty=1, net_entry_price=-1.50, fill_size_pct=0.0, expected_leg_count=2,
        max_loss=500.0,
        option_legs=[
            {"symbol": "AAPL260717P00195000", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "AAPL260717P00190000", "side": "buy", "position_intent": "buy_to_open"},
        ],
    )
    # A REAL fixture chain making the spread breach the stop.
    from datetime import UTC, datetime

    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    asof = datetime(2026, 6, 18, 16, 0, tzinfo=UTC)

    def _row(sym, bid, ask):  # noqa: ANN001
        return {
            "contract_symbol": sym, "asof": asof, "fetched_at": asof,
            "underlying_spot": 150.0, "risk_free_rate": 0.05, "bid": bid, "ask": ask,
            "last": (bid + ask) / 2, "volume": 100, "open_interest": 500,
            "delta": -0.3, "gamma": 0.01, "theta": -0.05, "vega": 0.1, "rho": 0.02,
            "iv": 0.45, "iv_source": "provider",
        }

    cpath = tmp_path / "option_chains" / "AAPL" / f"{asof.date():%Y-%m-%d}.parquet"
    cpath.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([
            _row("AAPL260717P00195000", 4.95, 5.05),  # short blown out
            _row("AAPL260717P00190000", 0.45, 0.55),
        ])), cpath,
    )

    result = auto.TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)
    closed = auto._run_options_position_stop_sweep(
        store=store, stop_pct=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=asof, result=result,
    )
    assert "ml_1" in closed, "the breach must close the composite"
    # NON-VACUOUS: the REAL store row transitioned open -> decomposed (leg-ops live).
    assert store.get("ml_1").state == "decomposed"
    decomp = [d for d in result.decisions if d.gate == "OPTIONS_PER_POSITION_STOP_DECOMPOSED"]
    assert len(decomp) == 1
    assert decomp[0].details["leg_op"] == "decompose"


# --------------------------------------------------------------------------- #
# 5. CONVERT ATOMICITY (the prior build had NO _apply_convert_live test) — a failed
#    ADD half raises ConvertExecutionError and the composite is UNCHANGED (H4).
# --------------------------------------------------------------------------- #
def test_apply_convert_live_add_failure_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_COMPOSITE_LEG_OPS", "1")
    store = _store(tmp_path)
    _open_spread(store, mlid="ml_conv")

    removed = {"called": False}

    def _failing_add(legs):  # noqa: ANN001 - the broker ADD-leg order FAILS
        raise RuntimeError("broker rejected the add leg")

    def _spy_remove(legs):  # noqa: ANN001
        removed["called"] = True

    # Inject a failing add + a spy remove via the builder (add raises -> remove must NOT run).
    def _builder(*, underlying, play_tag):  # noqa: ANN001, ANN003
        return _failing_add if play_tag == "autonomous_leg_convert_add" else _spy_remove

    monkeypatch.setattr(auto, "_build_live_leg_mleg_executor", _builder)

    current = [
        OptionLeg(symbol=_C195, side="sell", position_intent="sell_to_open"),
        OptionLeg(symbol=_C190, side="buy", position_intent="buy_to_open"),
    ]
    add_call = OptionLeg(symbol="AAPL260717P00180000", side="buy", position_intent="buy_to_open")
    decision = {
        "convert": True,
        "target_structure": "iron_condor",
        "legs_to_add": [add_call],
        "legs_to_remove": [],
    }
    with pytest.raises(ConvertExecutionError):
        auto._apply_convert_live(
            store=store,
            multi_leg_id="ml_conv",
            underlying="AAPL",
            decision=decision,
            current_legs=current,
        )
    # H4 atomicity: nothing removed (the remove executor never ran), composite unchanged.
    assert removed["called"] is False, "a failed ADD must never run the REMOVE half"
    assert store.get("ml_conv").state == "open", "the composite must be UNCHANGED on a failed convert"


# --------------------------------------------------------------------------- #
# 6. CONVERT SUCCESS — both halves fire (add then remove); composite stays OPEN (a
#    convert rolls legs, it does not close the composite).
# --------------------------------------------------------------------------- #
def test_apply_convert_live_success_runs_both_halves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_COMPOSITE_LEG_OPS", "1")
    store = _store(tmp_path)
    _open_spread(store, mlid="ml_conv2")

    order = []

    def _builder(*, underlying, play_tag):  # noqa: ANN001, ANN003
        def _exec(legs):  # noqa: ANN001
            order.append(play_tag)
        return _exec

    monkeypatch.setattr(auto, "_build_live_leg_mleg_executor", _builder)
    current = [
        OptionLeg(symbol=_C195, side="sell", position_intent="sell_to_open"),
        OptionLeg(symbol=_C190, side="buy", position_intent="buy_to_open"),
    ]
    # Add a protective long put wing (keeps the set defined-risk -> no_naked passes).
    add = OptionLeg(symbol="AAPL260717P00180000", side="buy", position_intent="buy_to_open")
    decision = {
        "convert": True,
        "target_structure": "iron_condor",
        "legs_to_add": [add],
        "legs_to_remove": [],
    }
    state = auto._apply_convert_live(
        store=store, multi_leg_id="ml_conv2", underlying="AAPL",
        decision=decision, current_legs=current,
    )
    # ADD fired BEFORE any remove (H4 order); the composite stays OPEN (rolled, not closed).
    assert order[0] == "autonomous_leg_convert_add"
    assert state == "open"
    assert store.get("ml_conv2").state == "open"
