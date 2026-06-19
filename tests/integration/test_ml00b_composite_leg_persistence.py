"""ml00b: persist composite rows + option_legs at options origination.

This is the prerequisite that UNBLOCKS aegis-agmon1 + aegis-agmon2. The iter-3
review proved those sweeps were STRUCTURALLY DEAD because (1) CompositePlayRow had
no option_legs field and (2) open_composite() was never called on the live
origination path — so store.list_open() never returned a composite WITH legs to
mark + sign.

Pins
----
1. UNBLOCK PROOF: a successful options fire through _originate_mleg_proposal
   persists a composite_plays row whose store.list_open() carries the option_legs
   (OCC symbol + side + position_intent) — the EXACT shape agmon1/agmon2 read.
   RED-prove: before the wire, list_open() is empty after a fire.
2. DEFAULT-OFF byte-identical: with HERMES_QUANT_AUTONOMOUS_OPTIONS unset, the
   helper is never entered and NO composite row is written.
3. BEST-EFFORT: a store-write failure does NOT abort the already-confirmed fire
   (the fire is real; the lifecycle row is observability).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.options.data import OptionLeg
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.state.composite_plays import CompositePlaysStore

# Two real OptionLegs of a call vertical (the leg data agmon1/agmon2 mark + sign).
_LEGS = (
    OptionLeg(
        symbol="AAPL260116C00190000",
        side="sell",
        position_intent="sell_to_open",
    ),
    OptionLeg(
        symbol="AAPL260116C00200000",
        side="buy",
        position_intent="buy_to_open",
    ),
)


def _fake_mleg() -> SimpleNamespace:
    """A stand-in carrier exposing the attributes the wire reads off a real
    MultiLegProposal (option_legs / underlying / strategy_kind / outer_qty /
    net_debit_credit / max_loss / proposal_id)."""
    return SimpleNamespace(
        proposal_id="prop_ml00b_001",
        underlying="AAPL",
        strategy_kind="vertical_spread",
        option_legs=_LEGS,
        outer_qty=1,
        net_debit_credit=Decimal("1.50"),
        max_loss=Decimal("50.0"),
    )


def _fill_record():
    """A real ExecutionRecord that the shared accounting tail treats as a fill."""
    return ExecutionRecord(
        proposal_id="prop_ml00b_001",
        signal_id=None,
        asset="AAPL",
        asset_class="multi_leg",
        timeframe="1d",
        asof_decision="2026-06-18T00:00:00Z",
        asof_execution="2026-06-18T00:00:00Z",
        target_position_pct=0.0,
        decision_price=1.50,
        fill_price=1.50,
        fill_size_pct=0.0,  # options size by contracts, not NAV fraction
        reactor_name="multileg-paper",
        human_in_the_loop=False,
        reactor_metadata={"parent_status": "filled", "outer_qty": 1},
    )


def _wire_origination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub the producer + reactor so _originate_mleg_proposal reaches a CONFIRMED
    fire, and point QUANT_HOME at a tmp dir so the composite store writes there."""
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)

    mleg = _fake_mleg()
    persisted = SimpleNamespace(proposal_id="prop_ml00b_001", multi_leg_proposal=mleg)

    # structure_select returns a producible kind (not None -> no abstain).
    monkeypatch.setattr(
        "hermes_quant.options.structure_select.select_structure_for_plan",
        lambda plan, **kw: "vertical_spread",
    )
    # the producer admits + persists.
    monkeypatch.setattr(
        "hermes_quant.options.recipes.build_and_persist_multi_leg",
        lambda **kw: (SimpleNamespace(reason=None), persisted),
    )
    # get_default_store is called for the proposals store (irrelevant here).
    monkeypatch.setattr("hermes_quant.proposals.get_default_store", lambda: object())

    # the reactor fills.
    reactor = SimpleNamespace(
        name="multileg-paper",
        execute=lambda mleg, **kw: _fill_record(),
    )
    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda prop: reactor)
    # the shared journal write is a no-op for this test.
    monkeypatch.setattr(
        "hermes_quant.journal.writer.append_human_override",
        lambda prop, **kw: None,
    )


def _run_fire() -> auto.TickResult:
    result = auto.TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)
    out = auto._originate_mleg_proposal(
        symbol="AAPL",
        asof=datetime.now(UTC),
        advisor_result={"aggregated_signal": {"direction": 1, "magnitude": 0.05}},
        nav=100000.0,
        options_buying_power=50000.0,
        iv_rank=60.0,
        structure_intent="premium_capture",
        result=result,
    )
    assert out, "the stubbed chain must reach a confirmed fire (returns an execution_id)"
    return result


# --------------------------------------------------------------------------- #
# 1. UNBLOCK PROOF — list_open() returns a row WITH legs after a fire.
# --------------------------------------------------------------------------- #
def test_origination_persists_composite_with_legs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", "1")
    _wire_origination(monkeypatch, tmp_path)

    result = _run_fire()
    assert result.fires == 1

    # The agmon1/agmon2 read path: list_open() on the SAME state.db must now carry
    # a composite WITH its option_legs (OCC symbol + side).
    store = CompositePlaysStore(db_path=tmp_path / "state.db")
    open_rows = store.list_open()
    assert len(open_rows) == 1, "the confirmed fire must persist exactly one open composite"
    row = open_rows[0]
    assert row.multi_leg_id == "prop_ml00b_001"
    assert row.underlying == "AAPL"
    assert row.strategy_kind == "vertical_spread"
    # The leg data agmon1/agmon2 mark + sign:
    assert len(row.option_legs) == 2
    assert [leg["symbol"] for leg in row.option_legs] == [
        "AAPL260116C00190000",
        "AAPL260116C00200000",
    ]
    assert [leg["side"] for leg in row.option_legs] == ["sell", "buy"]
    assert [leg["position_intent"] for leg in row.option_legs] == [
        "sell_to_open",
        "buy_to_open",
    ]


# --------------------------------------------------------------------------- #
# 2. DEFAULT-OFF byte-identical — flag unset => helper unreached => NO row.
# --------------------------------------------------------------------------- #
def test_default_off_writes_no_composite_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", raising=False)
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)

    result = auto.TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)
    out = auto._originate_mleg_proposal(
        symbol="AAPL",
        asof=datetime.now(UTC),
        advisor_result={"aggregated_signal": {"direction": 1, "magnitude": 0.05}},
        nav=100000.0,
        options_buying_power=50000.0,
        iv_rank=60.0,
        structure_intent="premium_capture",
        result=result,
    )
    assert out is None, "flag-OFF must abstain (byte-identical to today)"
    assert result.fires == 0
    # No state.db should have been written by the composite store at all.
    db_path = tmp_path / "state.db"
    if db_path.exists():
        store = CompositePlaysStore(db_path=db_path)
        assert store.list_open() == [], "flag-OFF must persist NO composite row"


# --------------------------------------------------------------------------- #
# 3. BEST-EFFORT — a store-write failure does NOT abort the confirmed fire.
# --------------------------------------------------------------------------- #
def test_store_failure_does_not_abort_the_fire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_QUANT_AUTONOMOUS_OPTIONS", "1")
    _wire_origination(monkeypatch, tmp_path)

    # Force the composite store to blow up at open_composite.
    def _boom(*a, **k):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(CompositePlaysStore, "open_composite", _boom)

    result = _run_fire()
    # The fire is still real + counted even though the lifecycle row failed.
    assert result.fires == 1


# --------------------------------------------------------------------------- #
# 4. The persist helper is fail-CLOSED on the ROW (no multi_leg_id => no row),
#    fail-OPEN on the fire (returns without raising).
# --------------------------------------------------------------------------- #
def test_persist_helper_skips_when_no_multi_leg_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    mleg = SimpleNamespace(
        proposal_id="",
        underlying="AAPL",
        strategy_kind="vertical_spread",
        option_legs=_LEGS,
        outer_qty=1,
        net_debit_credit=Decimal("1.50"),
        max_loss=None,
    )
    persisted = SimpleNamespace(proposal_id="")
    # No raise (best-effort), and no row written (no id).
    auto._persist_composite_play(mleg, persisted=persisted, execution_id="")
    db_path = tmp_path / "state.db"
    if db_path.exists():
        store = CompositePlaysStore(db_path=db_path)
        assert store.list_open() == []
