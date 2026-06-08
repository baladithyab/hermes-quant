"""Unit tests for hermes_quant.react.dispatch.select_reactor (ADR-0029 §2.5).

Deterministic, no network. Verifies equity Proposal -> PaperReactor, MultiLegProposal
-> MultiLegPaperReactor, and that approving a multi-leg proposal with the flag unset
surfaces MultiLegReactorDisabled (NOT a silent equity fill).
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
from hermes_quant.react.dispatch import is_multi_leg_proposal, select_reactor
from hermes_quant.react.multileg import MultiLegPaperReactor, MultiLegReactorDisabled
from hermes_quant.react.paper import PaperReactor
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket


class _EquityProposal:
    """Stand-in for an equity hermes_quant.proposals.Proposal (no option_legs)."""

    proposal_id = "prop_eq"
    symbol = "AAPL"
    asset_class = "equity"
    timeframe = "1d"
    advisor_result: dict = {}


def _ml() -> MultiLegProposal:
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=OptionGreeksSnapshot(
            delta=0.25, gamma=0.01, theta=-0.02, vega=0.1, rho=0.01, iv=0.4
        ),
        fill_price=4.5,
    )
    # risk_gate_pass=True is unrepresentable by direct construction (ADR-0029/#38);
    # mint through the blessed seam with a minimal admitted gate result.
    return MultiLegProposal.from_gate_result(
        gate_result=OptionsGateResult(
            admitted=True,
            bucket=StructureBucket.COVERED_CALL,
            reason=None,
            net_greeks=NetGreeks(),
            bpr_estimate=0.0,
            max_loss=None,
            contracts=1,
            warnings=(),
        ),
        proposal_id="prop_ml",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.5"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.5"),),
        rationale="cc",
        source_recipe_id="r",
    )


def test_equity_proposal_routes_to_paper(monkeypatch) -> None:
    # Clear BOTH equity-routing flags so this asserts the documented legacy default
    # (PaperReactor) hermetically — the operator shell / daemon may export
    # HERMES_QUANT_DETERMINISTIC_EQUITY=1, which correctly routes to the deterministic
    # reactor and would otherwise fail this default-path assertion.
    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)
    monkeypatch.delenv("HERMES_QUANT_DETERMINISTIC_EQUITY", raising=False)
    assert isinstance(select_reactor(_EquityProposal()), PaperReactor)
    assert is_multi_leg_proposal(_EquityProposal()) is False


def test_multileg_proposal_routes_to_multileg() -> None:
    assert isinstance(select_reactor(_ml()), MultiLegPaperReactor)
    assert is_multi_leg_proposal(_ml()) is True


def test_multileg_disabled_surfaces_not_silent_equity(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    reactor = select_reactor(_ml())
    assert isinstance(reactor, MultiLegPaperReactor)
    # Flag unset => MultiLegReactorDisabled, NOT a silent equity fill.
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(_ml(), fill_size_pct=0.05)
