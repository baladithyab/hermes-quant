"""Unit tests for hermes_quant.options.multileg.MultiLegProposal (ADR-0029 D5).

Deterministic, no network. Verifies the structural consume-never-bypass guarantee:
from_gate_result copies the gate verdict verbatim, money fields are Decimal, and
is_mleg / all_symbols behave per the order-shape facts.
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
from hermes_quant.options.occ import parse_occ
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=-0.02, vega=0.10, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


def _call(strike="C00160000") -> OptionLeg:
    return OptionLeg(
        symbol=f"NVDA260626{strike}",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(),
    )


def _gate_result(*, admitted=True, bucket=StructureBucket.COVERED_CALL) -> OptionsGateResult:
    return OptionsGateResult(
        admitted=admitted,
        bucket=bucket,
        reason=None if admitted else "naked_short_call",
        net_greeks=NetGreeks(delta=75.0, gamma=-1.0, theta=3.0, vega=-10.0),
        bpr_estimate=1234.5,
        max_loss=None,
        contracts=1,
        warnings=("soft_warn",),
    )


def _build(gate_result, **overrides) -> MultiLegProposal:
    kw = dict(
        gate_result=gate_result,
        proposal_id="prop_20260530T180000_NVDA_abc123",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(_call(),),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="test",
        source_recipe_id="recipe_cc",
    )
    kw.update(overrides)
    return MultiLegProposal.from_gate_result(**kw)


def test_from_gate_result_copies_verdict_verbatim() -> None:
    gr = _gate_result(admitted=True)
    p = _build(gr)
    assert p.risk_gate_pass is gr.admitted
    assert p.risk_gate_bucket == gr.bucket.value
    assert p.risk_gate_reason == gr.reason
    assert p.net_greeks == gr.net_greeks
    assert p.bpr_estimate == Decimal(str(gr.bpr_estimate))
    assert p.max_loss is None  # gr.max_loss is None for CC
    assert p.risk_gate_warnings == gr.warnings


def test_from_gate_result_rejected_carries_false() -> None:
    gr = _gate_result(admitted=False)
    p = _build(gr)
    assert p.risk_gate_pass is False
    assert p.risk_gate_reason == "naked_short_call"


def test_is_mleg_true_for_two_plus_option_legs() -> None:
    gr = _gate_result()
    long_leaps = OptionLeg(
        symbol="NVDA271217C00120000",
        side="buy",
        position_intent="buy_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=0.8),
    )
    p = _build(
        gr,
        strategy_kind="pmcc",
        option_legs=(long_leaps, _call()),
        stock_leg=None,
    )
    assert p.is_mleg is True


def test_is_mleg_false_for_single_option_leg() -> None:
    p = _build(_gate_result())
    assert p.is_mleg is False  # CC: single option leg


def test_all_symbols_roundtrip_through_parse_occ() -> None:
    p = _build(_gate_result())
    for sym in p.all_symbols:
        comp = parse_occ(sym)
        assert comp.underlying == "NVDA"


def test_money_fields_must_be_decimal() -> None:
    gr = _gate_result()
    with pytest.raises(TypeError):
        _build(gr, net_debit_credit=-4.50)  # float rejected


def test_max_loss_decimal_when_gate_provides_it() -> None:
    gr = OptionsGateResult(
        admitted=True,
        bucket=StructureBucket.DEFINED_RISK,
        reason=None,
        net_greeks=NetGreeks(),
        bpr_estimate=500.0,
        max_loss=300.0,
        contracts=1,
    )
    p = _build(gr, strategy_kind="vertical_spread")
    assert isinstance(p.max_loss, Decimal)
    assert p.max_loss == Decimal("300.0")


# --------------------------------------------------------------------------- #
# RR1 (review): the #38 constructor-lock is regression-blind. The mutation proof
# was that neutralizing BOTH __post_init__ checks left every multileg test green.
# These tests exercise the lock through the real seam so a regression FAILS:
#   * a passing verdict (risk_gate_pass=True) is UNREPRESENTABLE by direct cls(...)
#     — it raises ValueError unless minted inside from_gate_result;
#   * a non-bool risk_gate_pass raises TypeError (a truthy 1/"yes" must not slip the
#     `is True` lock);
#   * the ContextVar mint token does NOT leak past one from_gate_result() call;
#   * risk_gate_pass=False builds freely (rejected proposals are persisted for audit).
# --------------------------------------------------------------------------- #


def _direct_kwargs(**overrides) -> dict:
    """All required fields for a DIRECT MultiLegProposal(...) construction (bypassing
    the from_gate_result mint seam). Defaults to a money-valid, rejected proposal."""
    kw = dict(
        proposal_id="prop_20260530T180000_NVDA_abc123",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(_call(),),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        net_greeks=NetGreeks(delta=75.0, gamma=-1.0, theta=3.0, vega=-10.0),
        bpr_estimate=Decimal("1234.5"),
        max_loss=None,
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="test",
        source_recipe_id="recipe_cc",
        risk_gate_pass=False,
        risk_gate_bucket="covered_call",
        risk_gate_reason="naked_short_call",
        risk_gate_warnings=(),
    )
    kw.update(overrides)
    return kw


def test_direct_construction_with_pass_true_raises_valueerror() -> None:
    """(a) A passing gate verdict cannot be hand-built: direct cls(risk_gate_pass=True)
    with no mint token raises ValueError. This is the heart of the consume-never-bypass
    lock — neutralizing it must FAIL this test."""
    with pytest.raises(ValueError, match="from_gate_result"):
        MultiLegProposal(**_direct_kwargs(risk_gate_pass=True, risk_gate_reason=None))


def test_non_bool_pass_raises_typeerror() -> None:
    """(b) A truthy non-bool (1) must NOT slip the `is True` lock: __post_init__ rejects
    any non-bool risk_gate_pass with TypeError so the type invariant holds end-to-end."""
    with pytest.raises(TypeError, match="risk_gate_pass must be bool"):
        MultiLegProposal(**_direct_kwargs(risk_gate_pass=1, risk_gate_reason=None))


def test_mint_token_does_not_leak_after_from_gate_result() -> None:
    """(c) The ContextVar mint token is reset immediately after the cls(...) inside
    from_gate_result, so a later direct cls(risk_gate_pass=True) on the SAME thread still
    raises. Proves the mint authorization cannot leak to an unrelated construction."""
    minted = _build(_gate_result(admitted=True))  # legal: minted through the seam
    assert minted.risk_gate_pass is True
    with pytest.raises(ValueError, match="from_gate_result"):
        MultiLegProposal(**_direct_kwargs(risk_gate_pass=True, risk_gate_reason=None))


def test_direct_construction_with_pass_false_succeeds() -> None:
    """(d) risk_gate_pass=False builds freely by direct construction — rejected proposals
    are persisted for replay/audit; the reactor refuses to FILL them at runtime, but the
    lock is only on the True verdict, never on the False one."""
    p = MultiLegProposal(**_direct_kwargs(risk_gate_pass=False))
    assert p.risk_gate_pass is False
    assert p.risk_gate_reason == "naked_short_call"
