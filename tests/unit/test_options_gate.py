"""Unit tests for hermes_quant.risk.options_gate (Wave B2).

Deterministic, no network, no LLM. Per plan §2.3 + ADR-0027 D2/D3/D4/D6/D7.

The disabled-path (silence rail) is the most-tested case.
"""

from __future__ import annotations

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.risk.options_gate import (
    OptionsGateDisabled,
    OptionsGateResult,
    OptionsRiskConfig,
    StructureBucket,
    options_gate,
)

CFG = OptionsRiskConfig()


@pytest.fixture(autouse=True)
def _gate_enabled(monkeypatch):
    """Most tests run with the gate enabled; the disabled-path test overrides."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


def _short_call(strike_sym: str, *, delta: float, theta: float = 0.05) -> OptionLeg:
    return OptionLeg(
        symbol=strike_sym,
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=0.05, rho=0.01
        ),
    )


def _short_put(strike_sym: str, *, delta: float, theta: float = 0.05) -> OptionLeg:
    return OptionLeg(
        symbol=strike_sym,
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=0.05, rho=-0.01
        ),
    )


def _long_call(strike_sym: str, *, delta: float, theta: float = -0.05) -> OptionLeg:
    return OptionLeg(
        symbol=strike_sym,
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=0.05, rho=0.01
        ),
    )


def _base_kwargs(**over):
    kw = dict(
        strategy_kind="covered_call",
        underlying="NVDA",
        spot=150.0,
        nav=1_000_000.0,
        held_shares=0,
        options_buying_power=500_000.0,
        premium_received=250.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=CFG,
    )
    kw.update(over)
    return kw


# ---------------------------------------------------------------------------
# Disabled-path (the most-tested per the silence rail)
# ---------------------------------------------------------------------------


def test_gate_disabled_raises_without_flag(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_GATE", raising=False)
    with pytest.raises(OptionsGateDisabled):
        options_gate(
            [_short_call("NVDA260612C00160000", delta=0.25)],
            **_base_kwargs(strike=160.0),
        )


def test_gate_disabled_flag_zero(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "0")
    with pytest.raises(OptionsGateDisabled):
        options_gate(
            [_short_call("NVDA260612C00160000", delta=0.25)],
            **_base_kwargs(strike=160.0),
        )


# ---------------------------------------------------------------------------
# Three-bucket classifier (the load-bearing fix)
# ---------------------------------------------------------------------------


def test_covered_call_admitted_when_shares_cover() -> None:
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30),
    )
    assert res.admitted is True
    assert res.bucket == StructureBucket.COVERED_CALL


def test_covered_call_naked_when_shares_insufficient() -> None:
    res = options_gate(
        [_short_call("NVDA260612C00160000", delta=0.25)],
        **_base_kwargs(held_shares=50, strike=160.0, min_dte=30),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED
    assert "covering shares" in (res.reason or "") or "naked_short_call" in (res.reason or "")


def test_csp_admitted_when_buying_power_sufficient() -> None:
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=20_000.0,
            premium_received=250.0,
            min_dte=30,
        ),
    )
    assert res.admitted is True
    assert res.bucket == StructureBucket.CASH_SECURED_PUT


def test_csp_naked_when_buying_power_insufficient() -> None:
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=100.0,  # << strike*100 - premium
            premium_received=250.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


def test_debit_vertical_defined_risk_admitted() -> None:
    legs = [
        _long_call("NVDA260612C00140000", delta=0.30),
        _short_call("NVDA260612C00150000", delta=0.18),
    ]
    res = options_gate(
        legs,
        **_base_kwargs(
            strategy_kind="vertical_spread",
            strike=150.0,
            width=10.0,
            net_debit=2.0,
            premium_paid=2.0 * 100,
            min_dte=30,
        ),
    )
    assert res.admitted is True
    assert res.bucket == StructureBucket.DEFINED_RISK
    assert res.max_loss == pytest.approx(200.0)  # net_debit*100*1


def test_lone_short_call_no_cover_is_naked() -> None:
    res = options_gate(
        [_short_call("NVDA260612C00160000", delta=0.25)],
        **_base_kwargs(held_shares=0, strike=160.0, min_dte=30),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


# ---------------------------------------------------------------------------
# Caps (each independently silences)
# ---------------------------------------------------------------------------


def test_net_delta_cap_silences() -> None:
    # Existing portfolio net delta huge -> adding any covered call breaches cap.
    big = NetGreeks(delta=5_000.0)  # 5000 * 150 spot = 750k > 0.50 * 1M
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30,
            portfolio_net_greeks=big,
        ),
    )
    assert res.admitted is False
    assert res.reason == "net_delta_cap"


def test_gamma_cap_silences() -> None:
    big = NetGreeks(gamma=10.0)  # 10 * 150^2 = 225k > 0.05 * 1M = 50k
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30,
            portfolio_net_greeks=big,
        ),
    )
    assert res.admitted is False
    assert res.reason == "portfolio_gamma_cap"


def test_vega_cap_silences() -> None:
    big = NetGreeks(vega=2_000.0)  # 2000 > 0.10 * 1M / 100 = 1000
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30,
            portfolio_net_greeks=big,
        ),
    )
    assert res.admitted is False
    assert res.reason == "portfolio_vega_cap"


def test_theta_collecting_cc_passes_o4() -> None:
    """A theta-collecting covered call (net +theta) must NOT silence on O4."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25, theta=0.05),
        ],
        **_base_kwargs(held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30),
    )
    assert res.admitted is True


def test_theta_burning_entry_silences() -> None:
    """A theta-burning long-debit entry beyond budget silences on O4."""
    # net theta from a single long call with large negative theta.
    leg = OptionLeg(
        symbol="NVDA260612C00150000",
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=0.30, gamma=0.01, theta=-250.0, vega=0.05, rho=0.01
        ),
    )
    res = options_gate(
        [leg],
        **_base_kwargs(
            strategy_kind="swing_directional", strike=150.0, premium_paid=200.0,
            net_debit=2.0, width=0.0, min_dte=30,
        ),
    )
    # theta_burn = 250 * 100 = 25000 > 0.02 * 1M = 20000 -> silence
    assert res.admitted is False
    assert res.reason == "theta_budget"


def test_bpr_buffer_silences() -> None:
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=1_000_000.0,
            premium_received=250.0,
            total_bpr=799_000.0,  # + ~13750 new BPR > 0.80 * 1M
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.reason == "bpr_buffer"


def test_pin_risk_silences() -> None:
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00150000", delta=0.25),
        ],
        # spot 150, strike 150 -> moneyness 0; dte 2 <= 3 -> pin risk
        **_base_kwargs(
            held_shares=100, strike=150.0, basis_per_share=100.0, min_dte=2,
        ),
    )
    assert res.admitted is False
    assert res.reason == "pin_risk"


def test_short_call_delta_cap_silences() -> None:
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00155000", delta=0.45),  # > 0.30 cap
        ],
        **_base_kwargs(held_shares=100, strike=155.0, basis_per_share=100.0, min_dte=30),
    )
    assert res.admitted is False
    assert res.reason == "short_call_delta_exceeds_cap"


# ---------------------------------------------------------------------------
# Sizing (ADR-0027 D3 + amendment)
# ---------------------------------------------------------------------------


def test_cc_initiation_sizing_includes_x100() -> None:
    """basis 100, mid 2.50, nav 100k, kelly 0.25, max_pos 0.10
    -> collateral_per_contract 10_000 -> contracts 0; the buggy 25 is NOT produced."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=100_000.0, held_shares=100, strike=160.0, basis_per_share=100.0,
            min_dte=30, premium_received=250.0,
        ),
    )
    # collateral_per_contract = 100 * 100 = 10_000; target = 100k*0.25*0.10 = 2500
    # floor(2500/10000) = 0 -> min-contract guard silences.
    assert res.contracts == 0
    assert res.admitted is False
    assert res.contracts != 25  # the buggy per-share-basis result


def test_cc_initiation_sizing_correct_when_capital_allows() -> None:
    """nav 1M, same values -> contracts 2."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=1000, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=1_000_000.0, held_shares=1000, strike=160.0, basis_per_share=100.0,
            min_dte=30, premium_received=250.0,
        ),
    )
    # target = 1M*0.25*0.10 = 25_000; floor(25000/10000) = 2
    assert res.admitted is True
    assert res.contracts == 2


def test_cc_overlay_sizes_against_held_shares() -> None:
    """composite_intent='wheel', held_shares=300 -> max_contracts_by_held_shares=3."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=300, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=300, strike=160.0, basis_per_share=100.0, min_dte=30,
            composite_intent="wheel", open_strategies_on_underlying=1,
        ),
    )
    assert res.admitted is True
    assert res.contracts == 3


# ---------------------------------------------------------------------------
# REJECT-only invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nav", [50_000.0, 100_000.0, 250_000.0, 1_000_000.0, 5_000_000.0])
def test_reject_only_never_sizes_beyond_floor(nav) -> None:
    """The gate never returns contracts > floor(target_nav/collateral) and never
    admits a defined-risk structure whose max_loss/nav exceeds max_position_pct."""
    basis = 100.0
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100_000, basis_per_share=basis),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=nav, held_shares=100_000, strike=160.0, basis_per_share=basis,
            min_dte=30,
        ),
    )
    collateral = basis * 100
    target = nav * CFG.quarter_kelly * CFG.max_position_pct
    import math

    max_allowed = math.floor(target / collateral)
    assert res.contracts <= max_allowed


def test_reject_only_defined_risk_within_position_cap() -> None:
    """A defined-risk structure with max_loss > max_position_pct*nav is rejected
    (the gate cannot admit beyond the envelope)."""
    legs = [
        _long_call("NVDA260612C00140000", delta=0.30),
        _short_call("NVDA260612C00150000", delta=0.18),
    ]
    res = options_gate(
        legs,
        **_base_kwargs(
            nav=1_000.0,  # 0.10 * 1000 = 100 max position
            strategy_kind="vertical_spread",
            strike=150.0,
            width=10.0,
            net_debit=2.0,  # max_loss = 200 > 100
            premium_paid=200.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.reason == "max_loss_exceeds_position_cap"


def test_result_is_options_gate_result_type() -> None:
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=1000, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=1_000_000.0, held_shares=1000, strike=160.0, basis_per_share=100.0,
            min_dte=30,
        ),
    )
    assert isinstance(res, OptionsGateResult)


# ---------------------------------------------------------------------------
# Wheel composite (ADR-0027 D7)
# ---------------------------------------------------------------------------


def test_wheel_third_leg_silenced() -> None:
    """A third leg (CC + CSP already open) -> silence with wheel_double_up_blocked."""
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=1_000_000.0,
            premium_received=250.0,
            min_dte=30,
            composite_intent="wheel",
            open_strategies_on_underlying=2,  # CC + CSP already open
        ),
    )
    assert res.admitted is False
    assert res.reason == "wheel_double_up_blocked"


def test_max_strategies_per_underlying_silenced() -> None:
    """A second non-wheel strategy on the same underlying is blocked."""
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=1_000_000.0,
            premium_received=250.0,
            min_dte=30,
            open_strategies_on_underlying=1,
        ),
    )
    assert res.admitted is False
    assert res.reason == "max_strategies_per_underlying"
