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


def test_nan_spot_silences_not_fail_open(monkeypatch) -> None:
    # cr02: NaN spot must FAIL CLOSED (silence), not silently pass the gamma /
    # net-delta caps. `NaN > threshold` is always False, so without an isfinite
    # guard a NaN spot slips past every spot-scaled cap (the canonical
    # NaN-fail-open class the equity gate already guards at gate.py:536).
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30,
            spot=float("nan"),
        ),
    )
    assert res.admitted is False
    assert res.reason == "nonfinite_market_input"


def test_nan_nav_silences_not_fail_open(monkeypatch) -> None:
    # cr02: NaN nav must FAIL CLOSED — every cap is `... > cfg.x * nav`, so a
    # NaN nav makes the RHS NaN and the comparison always False (fail-open).
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30,
            nav=float("nan"),
        ),
    )
    assert res.admitted is False
    assert res.reason == "nonfinite_market_input"


def test_nan_total_bpr_silences_not_fail_open(monkeypatch) -> None:
    # cs06 (cr02 follow-up): the cr02 guard only covered spot/nav. A NaN
    # total_bpr reaches O6 (`if total_bpr + bpr > cfg.bpr_buffer_pct_nav * nav`),
    # where `NaN + bpr` is NaN and `NaN > x` is always False -> the BPR-buffer
    # check FAILS OPEN and ADMITS an under-validated structure. This is the same
    # NaN-fail-open class cr02 fixed for spot/nav; total_bpr must FAIL CLOSED too.
    # A CSP that is otherwise admissible (mirrors test_csp_admitted_*) isolates
    # total_bpr as the only non-finite input.
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=20_000.0,
            premium_received=250.0,
            total_bpr=float("nan"),
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.reason == "nonfinite_market_input"


def test_nan_options_buying_power_silences_not_fail_open(monkeypatch) -> None:
    # cs06: a NaN options_buying_power flows into the CSP cash-collateral gate
    # (`options_buying_power >= required`) and the at-size re-check; an
    # unvalidated non-finite collateral input must FAIL CLOSED at entry rather
    # than drive the collateral comparisons through NaN.
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=float("nan"),
            premium_received=250.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.reason == "nonfinite_market_input"


def test_nan_premium_received_silences_not_fail_open(monkeypatch) -> None:
    # cs06: recipes.py:225 builds premium_received as `float(short.mid or 0.0)
    # * 100` — `or 0.0` does NOT catch a NaN mid (NaN is truthy), so a NaN
    # premium_received reaches the gate and flows into the CSP BPR math
    # (`strike*100*c - premium_received`), poisoning the O6 buffer comparison.
    # A non-finite premium must FAIL CLOSED at entry.
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=20_000.0,
            premium_received=float("nan"),
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.reason == "nonfinite_market_input"


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
    """composite_intent='wheel': held_shares=300 (3 lots) but the NAV target is
    the binding ceiling. nav 1M, basis 100 -> collateral 10_000, target
    1M*0.25*0.10 = 25_000 -> floor(25000/10000) = 2. The wheel CAP takes
    min(by_shares=3, by_nav=2) = 2: the held-shares count can only SUBTRACT
    against the NAV target, never widen it (the pre-fix bug returned 3)."""
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
    assert res.contracts == 2  # min(by_shares=3, by_nav=2); never the buggy 3


def test_gamma_cap_rechecked_at_admitted_size_not_one_lot() -> None:
    """REGRESSION (Codex #11 / Facet-4 BLOCKING): the greek caps must be evaluated
    against the ADMITTED contract count, not the 1-lot structural footprint.

    Setup: a covered call that sizes to contracts=2 (structural_contracts=1).
    Portfolio gamma is preloaded just under the cap so the PRE-sizing check (1 lot)
    passes, but the admitted 2-lot footprint breaches it. Pre-fix, the gate
    ADMITTED this cap-breaching order (it only ever checked 1 lot). Post-fix, the
    at-size re-check silences it.

      cap            = 0.05 * 1M = 50,000 dollar-gamma
      dollar-gamma   = gamma_units * spot^2 = gamma_units * 22_500
      per-lot net gamma = -1.0 (0.01 * 100 contract-multiplier, SHORT sign=-1)
      admitted contracts = 2 ; cap = 0.05 * 1M = 50,000 ; dollar = |net.gamma|*22_500
      preload gamma=-0.5 -> 1-lot: |−0.5−1.0|*22500 = 33,750 < 50,000  (pre-check PASSES)
                         -> 2-lot: |−0.5−2.0|*22500 = 56,250 > 50,000  (at-size REJECTS)
    (Short options are short gamma, so more lots push |net gamma| further from zero
    when the book is already net-short gamma — the realistic cap-breach direction.)
    """
    preload = NetGreeks(gamma=-0.5)
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=1000, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=1_000_000.0, held_shares=1000, strike=160.0, basis_per_share=100.0,
            min_dte=30, premium_received=250.0, portfolio_net_greeks=preload,
        ),
    )
    # The order sizes to 2 lots, whose true gamma footprint breaches the cap.
    assert res.admitted is False, "cap-breaching 2-lot order must be REJECTED, not admitted"
    assert res.reason == "portfolio_gamma_cap_at_size"


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


# ---------------------------------------------------------------------------
# Pre-go-live hardening (Codex Facets 1+2+4 convergent correctness bugs)
# ---------------------------------------------------------------------------


def _long_put(strike_sym: str, *, delta: float, theta: float = -0.05) -> OptionLeg:
    return OptionLeg(
        symbol=strike_sym,
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=0.01, theta=theta, vega=0.05, rho=-0.01
        ),
    )


def test_naked_short_call_with_unrelated_long_put_is_naked() -> None:
    """Bug 1: a short call paired with an UNRELATED long put (wrong right) must
    NOT be classified DEFINED_RISK. The long put does not cap the short call's
    unbounded upside tail -> the short is naked -> reject. (Pre-fix: any long
    leg present made the structure DEFINED_RISK, bypassing the no-naked check.)"""
    res = options_gate(
        [
            _short_call("NVDA260612C00160000", delta=0.25),
            _long_put("NVDA260612P00120000", delta=-0.20),
        ],
        **_base_kwargs(held_shares=0, strike=160.0, min_dte=30),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


def test_short_call_with_earlier_expiry_long_call_is_naked() -> None:
    """Bug 1 (expiry guard): a long call that expires BEFORE the short leaves the
    short naked after the long expires -> not covering -> reject."""
    res = options_gate(
        [
            _short_call("NVDA260612C00160000", delta=0.25),  # expires 2026-06-12
            _long_call("NVDA260605C00170000", delta=0.20),  # expires 2026-06-05 (earlier)
        ],
        **_base_kwargs(held_shares=0, strike=160.0, min_dte=30),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


def test_min_dte_none_rejects_fail_closed() -> None:
    """Bug 2: an unknown DTE (min_dte=None) on a new entry MUST reject, not skip
    the pin-risk + min-DTE envelope (pre-fix the whole block was nested under
    `if min_dte is not None` -> fail-OPEN). This proves fail-closed silence."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=1000, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=1_000_000.0, held_shares=1000, strike=160.0, basis_per_share=100.0,
            min_dte=None,
        ),
    )
    assert res.admitted is False
    assert res.reason == "dte_unknown_for_new_entry"
    assert res.contracts == 0


def test_csp_under_collateralized_rejected_full_assignment_cash() -> None:
    """Bug 3: cash-secured put requires the FULL assignment cash
    (strike*100*contracts) reserved; the premium does NOT reduce it. BP that
    covers strike*100 - premium but NOT strike*100 must be rejected as naked."""
    # strike 140 -> full required = 14_000; premium-netted (buggy) = 13_750.
    res = options_gate(
        [_short_put("NVDA260612P00140000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            strike=140.0,
            options_buying_power=13_800.0,  # >= 13_750 (buggy) but < 14_000 (full)
            premium_received=250.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


def test_wheel_capped_by_nav_target_not_held_shares() -> None:
    """Bug 4: wheel CC overlay is capped by the NAV sizing target, never just
    held_shares//100. held_shares=1000 (10 lots) but NAV target only allows 2."""
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=1000, basis_per_share=100.0),
            _short_call("NVDA260612C00160000", delta=0.25),
        ],
        **_base_kwargs(
            nav=1_000_000.0, held_shares=1000, strike=160.0, basis_per_share=100.0,
            min_dte=30, composite_intent="wheel", open_strategies_on_underlying=1,
        ),
    )
    # by_shares = 10; by_nav = floor(1M*0.25*0.10 / 10_000) = 2 -> min = 2.
    assert res.admitted is True
    assert res.contracts == 2
    assert res.contracts < 1000 // 100  # the NAV cap strictly subtracted


def test_csp_sizing_uses_full_strike_collateral_not_premium_netted() -> None:
    """Bug 6 (S3 reconciliation): the CSP sizing denominator MUST be the FULL
    per-contract collateral (strike*100), consistent with the classifier's
    full-assignment-cash requirement (strike*100*contracts, premium NOT netted).

    The pre-fix denominator `strike*100 - premium_received/contracts` was SMALLER
    than the reserved collateral, so it could admit ONE extra contract relative to
    the cash actually set aside — sizing UP past the collateral. The full
    denominator can only size the same or fewer (the gate never sizes up).

    Construction (every value pins the boundary):
      strike=100  -> full collateral_per_contract = 100*100 = 10_000
      premium_received=1000, structural_contracts=1
                  -> buggy denom = 10_000 - 1000/1 = 9_000
      nav=720_000 -> kelly_target = 720_000 * 0.25 * 0.10 = 18_000
                     (action_step flooring -> 0, falls back to the un-stepped target)
      full:  floor(18_000 / 10_000) = 1
      buggy: floor(18_000 /  9_000) = 2   (the rejected over-sizing)
    """
    res = options_gate(
        [_short_put("NVDA260612P00100000", delta=-0.25)],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            nav=720_000.0,
            strike=100.0,
            options_buying_power=1_000_000.0,  # >> full collateral; admits the CSP
            premium_received=1000.0,
            min_dte=30,
        ),
    )
    assert res.admitted is True
    assert res.bucket == StructureBucket.CASH_SECURED_PUT
    assert res.contracts == 1  # full strike*100 denominator
    assert res.contracts != 2  # the premium-netted (smaller-denominator) over-size


def test_csp_collateral_rechecked_at_admitted_size_not_one_lot() -> None:
    """BLOCKING (H-opt+pil #1): the CSP cash-collateral requirement must be
    re-validated against the ADMITTED contract count, not the 1-lot structural
    footprint. The classifier admits a CSP when options_buying_power covers the
    1-lot full assignment cash (strike*100*structural_contracts, structural=1),
    but `_size_contracts` routinely sizes UP (here to 2 lots). Full assignment
    cash scales linearly, so buying power that covers ONE lot but not TWO would,
    pre-fix, admit an under-collateralized (effectively naked) 2-lot short put —
    the exact admission this gate exists to reject.

    Construction (every value pins the boundary):
      strike=100 -> collateral_per_contract = strike*100 = 10_000
      nav=800_000 -> kelly_target = 800_000 * 0.25 * 0.10 = 20_000
                     (action_step 0.05*nav=40_000 floors to 0 -> falls back to
                     the un-stepped 20_000)
      contracts  = floor(20_000 / 10_000) = 2  (> structural_contracts=1)
      options_buying_power = 15_000:
        1-lot classifier needs strike*100*1 = 10_000 -> 15_000 >= 10_000 PASSES
        2-lot admitted    needs strike*100*2 = 20_000 -> 15_000 <  20_000 REJECTS
    BPR at 2 lots (20_000 - premium) is far below the 0.80*nav=640_000 buffer, so
    the collateral check (not BPR) is the binding reject. The short put carries
    zero gamma/vega so the (earlier) greeks-at-size re-check passes cleanly and
    the collateral re-check is demonstrably what silences.
    """
    flat_put = OptionLeg(
        symbol="NVDA260612P00100000",
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=-0.25, gamma=0.0, theta=0.05, vega=0.0, rho=-0.01
        ),
    )
    res = options_gate(
        [flat_put],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            nav=800_000.0,
            strike=100.0,
            options_buying_power=15_000.0,  # covers 1 lot (10k) but not 2 (20k)
            premium_received=250.0,
            total_bpr=0.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False, "under-collateralized 2-lot CSP must be REJECTED"
    assert res.reason == "csp_collateral_at_size"
    assert res.contracts == 0


def test_csp_bpr_buffer_rechecked_at_admitted_size_not_one_lot() -> None:
    """BLOCKING (H-opt+pil #1): the O6 BPR buffer must be re-validated against the
    ADMITTED contract count, not the 1-lot structural footprint. The pre-sizing O6
    check ran against structural_contracts (=1); BPR scales linearly with lots, so
    a 2-lot order whose 1-lot BPR fits the buffer can breach it at 2 lots. Pre-fix
    the gate admitted the buffer-breaching order (it never re-checked at size).

    Construction:
      strike=100, premium=250 -> BPR/lot = strike*100 - premium = 9_750
      nav=800_000 -> buffer = 0.80 * 800_000 = 640_000 ; contracts = 2 (as above)
      total_bpr = 625_000:
        1-lot: 625_000 + 9_750  = 634_750 <= 640_000  PASSES
        2-lot: 625_000 + 19_750 = 644_750 >  640_000  REJECTS
      options_buying_power = 1_000_000 covers 2-lot collateral (20_000), so BPR
      (not collateral) is the binding reject. Zero gamma/vega so the greeks-at-size
      re-check passes and the O6-at-size BPR check is demonstrably what silences.
    """
    flat_put = OptionLeg(
        symbol="NVDA260612P00100000",
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=OptionGreeksSnapshot(
            delta=-0.25, gamma=0.0, theta=0.05, vega=0.0, rho=-0.01
        ),
    )
    res = options_gate(
        [flat_put],
        **_base_kwargs(
            strategy_kind="cash_secured_put",
            nav=800_000.0,
            strike=100.0,
            options_buying_power=1_000_000.0,  # >> 2-lot collateral; isolates BPR
            premium_received=250.0,
            total_bpr=625_000.0,
            min_dte=30,
        ),
    )
    assert res.admitted is False, "buffer-breaching 2-lot CSP must be REJECTED"
    assert res.reason == "bpr_buffer_at_size"
    assert res.contracts == 0


def test_missing_greeks_returns_silence_not_raise() -> None:
    """Bug 5: a leg missing greeks must yield a deterministic silence (reject),
    never abort the tick by raising out of options_gate()."""
    leg_no_greeks = OptionLeg(
        symbol="NVDA260612C00160000",
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=None,
    )
    res = options_gate(
        [
            StockLeg(underlying="NVDA", qty=100, basis_per_share=100.0),
            leg_no_greeks,
        ],
        **_base_kwargs(held_shares=100, strike=160.0, basis_per_share=100.0, min_dte=30),
    )
    assert isinstance(res, OptionsGateResult)
    assert res.admitted is False
    assert res.contracts == 0
    assert (res.reason or "").startswith("greeks_unavailable")


def test_non_greeks_exception_silences_with_distinct_reason_not_mislabeled(caplog) -> None:
    """Bug 7 (S3): an UNEXPECTED exception during greek aggregation (e.g. an
    unsupported leg type raising TypeError, or an arithmetic/coding bug) must NOT
    be masked as `greeks_unavailable` (the missing-greeks silence path). It must
    still fail closed (silence, never admit), but with a DISTINCT
    `greeks_aggregation_error` reason and a logged error, so a real defect surfaces
    instead of hiding behind the silence rail."""
    import logging

    class _BogusLeg:
        """Not an OptionLeg/StockLeg -> aggregate_net_greeks raises TypeError."""

    with caplog.at_level(logging.ERROR, logger="hermes_quant.risk.options_gate"):
        res = options_gate(
            [_BogusLeg()],  # type: ignore[list-item]
            **_base_kwargs(strike=160.0, min_dte=30),
        )
    assert isinstance(res, OptionsGateResult)
    assert res.admitted is False
    assert res.contracts == 0
    # Distinct reason: NOT mislabeled as missing greeks.
    assert (res.reason or "").startswith("greeks_aggregation_error")
    assert not (res.reason or "").startswith("greeks_unavailable")
    assert "TypeError" in (res.reason or "")
    # The unexpected error was logged (not silently swallowed).
    assert any(
        "unexpected error aggregating" in rec.getMessage() for rec in caplog.records
    )
