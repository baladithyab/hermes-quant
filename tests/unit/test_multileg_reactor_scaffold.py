"""Unit tests for hermes_quant.react.multileg — flag-gate + dispatch surface.

Deterministic, no network. The Wave-B2 scaffold tests (flag-gate, no-write-while-
disabled, Protocol conformance, name) are KEPT verbatim — the default-OFF property is
the load-bearing rail. The B01 go-live wave fleshed out the body, so:
  * test_not_in_react_all INVERTS (the reactor is now exported so dispatch can import
    it from the package — a deliberate, documented change).
  * the "raises NotImplementedError when enabled" test is replaced by the gate-final
    and deterministic-fill tests (the body now exists, behind the flag).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.react import __all__ as react_all
from hermes_quant.react.base import Reactor
from hermes_quant.react.multileg import (
    GateRejectedProposal,
    MultiLegPaperReactor,
    MultiLegReactorDisabled,
)
from hermes_quant.react.paper import FillSizeInvariantError
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket


def _cc_proposal(*, risk_gate_pass: bool = True) -> MultiLegProposal:
    """A minimal covered-call proposal (NVDA +100 + 1 short call)."""
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=OptionGreeksSnapshot(
            delta=0.25, gamma=0.01, theta=-0.03, vega=0.10, rho=0.01, iv=0.4
        ),
        fill_price=4.50,
    )
    common = dict(
        proposal_id="prop_20260530T180000_NVDA_abc123",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="test cc",
        source_recipe_id="recipe_cc",
    )
    # A passing verdict is unrepresentable by direct construction (ADR-0029/#38),
    # so mint it through the blessed seam; a rejected (False) proposal builds
    # freely and is the explicit non-admitted shape.
    gate = OptionsGateResult(
        admitted=risk_gate_pass,
        bucket=StructureBucket.COVERED_CALL if risk_gate_pass else StructureBucket.NAKED,
        reason=None if risk_gate_pass else "naked_short_call",
        net_greeks=NetGreeks(delta=75.0, gamma=-1.0, theta=3.0, vega=-10.0),
        bpr_estimate=0.0,
        max_loss=None,
        contracts=1 if risk_gate_pass else 0,
        warnings=(),
    )
    return MultiLegProposal.from_gate_result(gate_result=gate, **common)


# --------------------------------------------------------------------------- #
# Default-OFF rail (KEPT verbatim from the scaffold)
# --------------------------------------------------------------------------- #
def test_execute_raises_disabled_when_flag_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(_cc_proposal(), fill_size_pct=0.05)
    # Nothing written: the bus must not even be created while disabled.
    assert not bus.exists()


def test_execute_writes_nothing_to_existing_bus_when_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    bus = tmp_path / "executions.jsonl"
    bus.write_text("")  # pre-existing empty bus
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(_cc_proposal(), fill_size_pct=0.05)
    assert bus.read_text() == ""  # unchanged


# --------------------------------------------------------------------------- #
# Gate-is-final-authority (flag ON, gate-rejected proposal => zero writes)
# --------------------------------------------------------------------------- #
def test_gate_rejected_proposal_raises_before_any_write(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(GateRejectedProposal):
        reactor.execute(_cc_proposal(risk_gate_pass=False), fill_size_pct=0.05)
    # The gate check is BEFORE any bus mkdir/touch — nothing written.
    assert not bus.exists()


@pytest.mark.parametrize("fill_size_pct", [float("nan"), 2.0])
def test_fill_size_invariant_raises_before_multileg_write(
    monkeypatch, tmp_path, fill_size_pct: float
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    with pytest.raises(FillSizeInvariantError):
        reactor.execute(_cc_proposal(), fill_size_pct=fill_size_pct)

    assert not bus.exists()


def test_enabled_fills_via_deterministic_model(monkeypatch, tmp_path) -> None:
    """Flag ON + risk_gate_pass=True => fills via the deterministic (no-creds) model,
    returns a parent ExecutionRecord, writes parent + N children."""
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    bus = tmp_path / "executions.jsonl"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(
        "hermes_quant.state.portfolio_state._singleton",
        None,
        raising=False,
    )
    from hermes_quant.state.portfolio_state import PortfolioState

    ps = PortfolioState(state_db_path=state_db)
    monkeypatch.setattr(
        "hermes_quant.react.multileg.get_portfolio_state",
        lambda *a, **k: ps,
        raising=False,
    )
    # The reactor imports get_portfolio_state lazily inside _reconcile_state; patch the
    # source so the tmp state.db is used.
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)

    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc_proposal(), fill_size_pct=0.05)
    assert parent.asset_class == "multi_leg"
    assert (parent.reactor_metadata or {})["multi_leg_id"] == _cc_proposal().proposal_id
    lines = [ln for ln in bus.read_text().splitlines() if ln.strip()]
    # parent + 2 children (1 option + 1 equity)
    assert len(lines) == 3


# --------------------------------------------------------------------------- #
# Protocol conformance + identity (KEPT verbatim)
# --------------------------------------------------------------------------- #
def test_protocol_conformance() -> None:
    assert isinstance(MultiLegPaperReactor(), Reactor) is True


def test_name_and_credentials() -> None:
    reactor = MultiLegPaperReactor()
    assert reactor.name == "multileg-paper"
    assert reactor.requires_credentials is False


def test_now_in_react_all() -> None:
    """B01 go-live: the reactor is NOW exported so dispatch can import it (the
    Wave-B2 'not in __all__' guard deliberately inverts this wave)."""
    assert "MultiLegPaperReactor" in react_all
