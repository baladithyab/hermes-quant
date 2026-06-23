"""Codex-critique spine fixes in hermes_quant/autonomous.py (cx0 / cx1 / cx3-legless).

cx0 [HIGH] evidence-gate inversion: arming HERMES_QUANT_AUTONOMOUS_OPTIONS=1 WITHOUT
  HERMES_QUANT_OPTIONS_EVIDENCE_GATE=1 must NOT bypass the GATE-2 clean-window unlock.
  The evidence gate is MANDATORY whenever autonomous options are armed; the only escape
  is the NEW default-OFF HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE (documented dangerous).

cx1 [P1] leg-op order hardcoded outer_qty/contracts=1 + non-unique proposal_id: a
  composite with outer_qty>1 must carry its REAL outer_qty into the leg-op MLEG order,
  and two same-underlying composites must mint DISTINCT proposal_ids (keyed on the
  composite's multi_leg_id) so the reactor idempotency does not collide.

cx3-legless [P2] _persist_composite_play must SKIP (never write) a legless composite
  row — an empty option_legs => no durable [] row (it would re-create the agmon1
  dead-path: a [] row skipped forever every sweep).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.options.data import OptionLeg
from hermes_quant.state.composite_plays import CompositePlaysStore

_C190 = "AAPL260717C00190000"
_C195 = "AAPL260717C00195000"


# =========================================================================== #
# cx0 — evidence-gate is MANDATORY when autonomous options are armed.
# =========================================================================== #
def test_cx0_evidence_gate_locked_when_armed_without_gate_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUTONOMOUS_OPTIONS=1 + no unlock marker + EVIDENCE_GATE unset => LOCKED.

    Pre-fix this returned True (the gate only ran when EVIDENCE_GATE=1), letting
    options originate with zero GATE-2 enforcement. Post-fix the gate ALWAYS runs.
    """
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)  # no unlock marker on this home
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", "1")
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_EVIDENCE_GATE", raising=False)
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # read_options_unlocked() home

    assert auto._options_evidence_gate_ok() is False, (
        "armed options + no unlock marker => the evidence gate must LOCK origination"
    )


def test_cx0_evidence_gate_unlocked_with_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A persisted gate2_cleared=True unlock marker => the gate passes (True)."""
    import json

    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker = tmp_path / "quant" / "options_unlock.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"gate2_cleared": True}), encoding="utf-8")

    assert auto._options_evidence_gate_ok() is True


def test_cx0_emergency_override_bypasses_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The NEW default-OFF HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE=1 is the ONLY escape."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)  # no marker
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE", "1")

    assert auto._options_evidence_gate_ok() is True, (
        "the explicit emergency override (default-OFF) bypasses the gate"
    )


def test_cx0_read_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read error from read_options_unlocked => LOCKED (fail-CLOSED)."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_EVIDENCE_OVERRIDE", raising=False)

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated unlock read failure")

    monkeypatch.setattr("hermes_quant.eval.clean_window.read_options_unlocked", _boom)
    assert auto._options_evidence_gate_ok() is False


# =========================================================================== #
# cx1 — leg-op order carries the REAL outer_qty + a UNIQUE proposal_id.
# =========================================================================== #
def test_cx1_leg_op_carries_real_outer_qty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composite with outer_qty=3 => the leg-op MLEG order carries outer_qty=3.

    Pre-fix the executor hardcoded outer_qty=1, so a 3-wide composite submitted ONE
    spread then marked the WHOLE composite decomposed -> 2 residual contracts unmanaged.
    """
    seen = {"order": None}

    class _Reactor:
        name = "multileg-paper"

        def execute(self, order, **kw):  # noqa: ANN001, ANN003
            seen["order"] = order
            return SimpleNamespace(
                reactor_metadata={"parent_status": "filled"},
                fill_size_pct=0.0,
                proposal_id=order.proposal_id,
                asset_class="multi_leg",
            )

    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda p: _Reactor())

    executor = auto._build_live_leg_mleg_executor(
        underlying="AAPL", play_tag="autonomous_leg_decompose", outer_qty=3,
        multi_leg_id="ml_big",
    )
    executor([OptionLeg(symbol=_C195, side="buy", position_intent="buy_to_close")])

    assert seen["order"] is not None
    assert seen["order"].outer_qty == 3, "the leg-op order must carry the composite's REAL outer_qty"


def test_cx1_distinct_proposal_ids_per_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-underlying composites => DISTINCT leg-op proposal_ids.

    Pre-fix both minted f"legop_AAPL_autonomous_leg_decompose" -> reactor idempotency
    returned the prior parent without sending a new order for the second composite.
    """
    ids = []

    class _Reactor:
        name = "multileg-paper"

        def execute(self, order, **kw):  # noqa: ANN001, ANN003
            ids.append(order.proposal_id)
            return SimpleNamespace(
                reactor_metadata={"parent_status": "filled"},
                fill_size_pct=0.0,
                proposal_id=order.proposal_id,
                asset_class="multi_leg",
            )

    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda p: _Reactor())

    leg = [OptionLeg(symbol=_C195, side="buy", position_intent="buy_to_close")]
    auto._build_live_leg_mleg_executor(
        underlying="AAPL", play_tag="autonomous_leg_decompose", multi_leg_id="ml_one",
    )(leg)
    auto._build_live_leg_mleg_executor(
        underlying="AAPL", play_tag="autonomous_leg_decompose", multi_leg_id="ml_two",
    )(leg)

    assert len(ids) == 2
    assert ids[0] != ids[1], "two same-underlying composites must mint DISTINCT proposal_ids"
    assert "ml_one" in ids[0] and "ml_two" in ids[1]


def test_cx1_decompose_live_threads_outer_qty_from_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL trigger: a stop breach on a 3-wide composite decomposes it carrying
    outer_qty=3 into the leg-op order (threaded through _maybe_decompose_on_close)."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    monkeypatch.setenv("HERMES_QUANT_COMPOSITE_LEG_OPS", "1")
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=tmp_path / "state.db")
    store.open_composite(
        multi_leg_id="ml_3wide", underlying="AAPL", strategy_kind="bull_put_spread",
        outer_qty=3, net_entry_price=-1.50, fill_size_pct=0.0, expected_leg_count=2,
        max_loss=500.0,
        option_legs=[
            {"symbol": "AAPL260717P00195000", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "AAPL260717P00190000", "side": "buy", "position_intent": "buy_to_open"},
        ],
    )

    seen = {"orders": []}

    class _Reactor:
        name = "multileg-paper"

        def execute(self, order, **kw):  # noqa: ANN001, ANN003
            seen["orders"].append(order)
            return SimpleNamespace(
                reactor_metadata={"parent_status": "filled"},
                fill_size_pct=0.0,
                proposal_id=order.proposal_id,
                asset_class="multi_leg",
            )

    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda p: _Reactor())

    state = auto._maybe_decompose_on_close(
        store=store, row=store.get("ml_3wide"), reason="test_stop",
    )
    assert state == "decomposed"
    assert seen["orders"], "the live decompose must fire a leg-op order"
    assert all(o.outer_qty == 3 for o in seen["orders"]), (
        "the leg-op order(s) must carry the composite's REAL outer_qty=3"
    )
    assert all("ml_3wide" in o.proposal_id for o in seen["orders"]), (
        "the leg-op proposal_id must be keyed on the composite's multi_leg_id"
    )


# =========================================================================== #
# cx3-legless — _persist_composite_play SKIPS a legless mleg (no [] row).
# =========================================================================== #
def test_cx3_legless_mleg_writes_no_composite_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fired mleg with EMPTY option_legs => no composite row written (was: a [] row)."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    mleg = SimpleNamespace(
        proposal_id="prop_legless_001",
        underlying="AAPL",
        strategy_kind="vertical_spread",
        option_legs=[],  # legless
        outer_qty=1,
        net_debit_credit=Decimal("1.50"),
        max_loss=None,
    )
    persisted = SimpleNamespace(proposal_id="prop_legless_001")

    # Best-effort: never raises, fire stays real.
    auto._persist_composite_play(mleg, persisted=persisted, execution_id="prop_legless_001")

    db_path = tmp_path / "state.db"
    if db_path.exists():
        store = CompositePlaysStore(db_path=db_path)
        assert store.list_open() == [], (
            "a legless mleg must NOT persist a durable [] composite row"
        )


def test_cx3_legged_mleg_still_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION GUARD: a normal legged mleg STILL persists a row (the skip is
    surgical to the legless case only)."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    mleg = SimpleNamespace(
        proposal_id="prop_legged_001",
        underlying="AAPL",
        strategy_kind="vertical_spread",
        option_legs=[
            OptionLeg(symbol=_C195, side="sell", position_intent="sell_to_open"),
            OptionLeg(symbol=_C190, side="buy", position_intent="buy_to_open"),
        ],
        outer_qty=1,
        net_debit_credit=Decimal("1.50"),
        max_loss=None,
    )
    persisted = SimpleNamespace(proposal_id="prop_legged_001")
    auto._persist_composite_play(mleg, persisted=persisted, execution_id="prop_legged_001")

    store = CompositePlaysStore(db_path=tmp_path / "state.db")
    rows = store.list_open()
    assert len(rows) == 1
    assert rows[0].multi_leg_id == "prop_legged_001"
    assert len(rows[0].option_legs) == 2
