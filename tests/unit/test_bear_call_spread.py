"""Unit tests for bcs1 — Bear Call Spread producer (ADR-0098 Step 3).

The bear call spread is the call-side mirror of the bull put spread (ADR-0098 Step 2).
Deterministic, offline, no network, no LLM.

Coverage contract:
  BCS-1  Within-budget BCS builds 2 legs: short lower-strike call + long
         higher-strike call; max_loss is finite; gate ADMITS it.
  BCS-2  Over-cap BCS (max_loss > max_position_pct * nav) is REJECTED
         (fail-closed).
  BCS-3  Non-finite long-leg mid (NaN) is rejected pre-gate (fail-closed).
  BCS-4  structure_select returns 'bear_call_spread' for BEARISH +
         defined_risk_credit + MID/HIGH IV ONLY when
         HERMES_QUANT_VERTICAL_SPREADS=1.
  BCS-5  structure_select returns None (abstain) for the same inputs when
         HERMES_QUANT_VERTICAL_SPREADS is absent (flag-OFF; byte-identical to
         today). This is the RED-proof for the flag-gate.
  BCS-6  RED-proof for the producer: a lone short call (no long protection) is
         NAKED and REJECTED — proving the long leg is load-bearing.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionChain,
    OptionGreeksSnapshot,
    OptionLeg,
    OptionSnapshot,
)
from hermes_quant.options.recipes import (
    MultiLegBuildResult,
    RecipeBuildError,
    _finite_mid,
    _pick_long_call,
    _snapshot_to_long_leg,
    _snapshot_to_short_leg,
    build_bear_call_spread_proposal,
)
from hermes_quant.options.structure_select import (
    VERTICAL_SPREADS_FLAG,
    Direction,
    IVRegime,
    select_structure,
    vertical_spreads_enabled,
)
from hermes_quant.risk.options_gate import (
    OptionsRiskConfig,
    StructureBucket,
)

# ---------------------------------------------------------------------------
# Shared fixtures: a minimal two-strike OCC-21 option chain for NVDA
# Expiry: 2026-07-17 (dte ~30 from 2026-06-17 decision). OCC symbols:
#   short:  NVDA260717C00160000  (call, K=160, |delta|~0.25 -> short leg)
#   long:   NVDA260717C00170000  (call, K=170, |delta|~0.15 -> long protection)
# ---------------------------------------------------------------------------

_ASOF = datetime(2026, 6, 17, 15, 0, 0, tzinfo=UTC)
_NAV = 1_000_000.0
_OBP = 500_000.0          # options buying power (ample for 1-lot BCS)
_SPOT = 150.0
_CFG = OptionsRiskConfig()

_SHORT_SYM = "NVDA260717C00160000"
_LONG_SYM  = "NVDA260717C00170000"
_SHORT_STRIKE = 160.0
_LONG_STRIKE  = 170.0
_WIDTH = _LONG_STRIKE - _SHORT_STRIKE  # 10.0
_SHORT_MID = 2.50   # short call premium (collect)
_LONG_MID  = 1.00   # long call premium (pay)
_NET_CREDIT = _SHORT_MID - _LONG_MID  # 1.50 / share
_MAX_LOSS_PER_CONTRACT = (_WIDTH - _NET_CREDIT) * 100  # (10-1.5)*100 = 850.0


def _make_snap(symbol: str, *, mid_bid_ask: float, delta: float) -> OptionSnapshot:
    """Minimal OptionSnapshot with a known mid and delta.

    Uses small gamma/vega values so the greek caps are never the binding constraint.
    The BCS max_loss = (width-credit)*100 is the binding constraint we want to test.
    """
    half = mid_bid_ask / 2
    return OptionSnapshot(
        symbol=symbol,
        asof=_ASOF,
        fetched_at=_ASOF,
        bid=mid_bid_ask - half * 0.05,
        ask=mid_bid_ask + half * 0.05,
        last=mid_bid_ask,
        volume=500,
        open_interest=1000,
        greeks=OptionGreeksSnapshot(
            delta=delta,
            gamma=0.0001,   # tiny gamma -> gamma_dollar << cap for any sizing
            theta=-0.005,   # theta-paying (long call positive theta from short)
            vega=0.001,     # tiny vega -> vega cap not binding
            rho=0.001,
        ),
        underlying_spot=_SPOT,
        risk_free_rate=0.05,
    )


def _make_chain(
    short_mid: float = _SHORT_MID,
    long_mid: float = _LONG_MID,
    short_delta: float = 0.25,
    long_delta: float = 0.15,
) -> OptionChain:
    """Two-snapshot chain (short + long call) for NVDA."""
    return OptionChain(
        underlying="NVDA",
        asof=_ASOF,
        underlying_spot=_SPOT,
        risk_free_rate=0.05,
        snapshots=(
            _make_snap(_SHORT_SYM, mid_bid_ask=short_mid, delta=short_delta),
            _make_snap(_LONG_SYM,  mid_bid_ask=long_mid,  delta=long_delta),
        ),
    )


# ---------------------------------------------------------------------------
# BCS-1: within-budget build — 2 legs, finite max_loss, gate ADMITS
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gate_enabled(monkeypatch):
    """Most tests need the options gate enabled."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


def test_bcs1_within_budget_two_legs_admitted(monkeypatch) -> None:
    """BCS-1: a within-budget bear-call-spread builds 2 legs and is ADMITTED."""
    chain = _make_chain()
    result = build_bear_call_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=_NAV,
        options_buying_power=_OBP,
        cfg=_CFG,
    )
    assert result.admitted is True, f"expected admitted, got reason={result.reason!r}"
    assert result.proposal is not None

    p = result.proposal
    assert p.strategy_kind == "bear_call_spread"

    # Exactly 2 option legs, no stock leg.
    assert len(p.option_legs) == 2
    assert p.stock_leg is None

    # Leg identity: short lower-strike call first, long higher-strike call second.
    short_leg, long_leg = p.option_legs
    assert short_leg.side == "sell"
    assert short_leg.position_intent == "sell_to_open"
    assert short_leg.symbol == _SHORT_SYM

    assert long_leg.side == "buy"
    assert long_leg.position_intent == "buy_to_open"
    assert long_leg.symbol == _LONG_SYM

    # Both calls.
    assert short_leg.right == "C"
    assert long_leg.right == "C"

    # max_loss is finite and positive.
    assert p.max_loss is not None
    assert isinstance(p.max_loss, Decimal)
    max_loss_float = float(p.max_loss)
    assert math.isfinite(max_loss_float)
    assert max_loss_float > 0

    # The gate computes max_loss at structural_contracts=1 (the single short leg).
    # _MAX_LOSS_PER_CONTRACT = (width - net_credit) * 100 for 1 contract.
    assert max_loss_float == pytest.approx(_MAX_LOSS_PER_CONTRACT, rel=1e-3)

    # Bucket is DEFINED_RISK.
    assert result.bucket == StructureBucket.DEFINED_RISK

    # risk_gate_pass is True.
    assert p.risk_gate_pass is True


def test_bcs1_net_debit_credit_is_negative_credit_received() -> None:
    """BCS-1 supplement: net_debit_credit is negative (credit received)."""
    chain = _make_chain()
    result = build_bear_call_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=_NAV,
        options_buying_power=_OBP,
        cfg=_CFG,
    )
    assert result.admitted is True
    # net_debit_credit < 0 means credit received (same sign convention as CC/CSP/BPS).
    assert result.proposal is not None
    assert float(result.proposal.net_debit_credit) < 0


def test_bcs1_long_strike_is_higher_than_short_strike() -> None:
    """BCS-1 supplement: the long protection leg has a HIGHER strike than the short leg."""
    from hermes_quant.options.recipes import parse_strike

    chain = _make_chain()
    result = build_bear_call_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=_NAV,
        options_buying_power=_OBP,
        cfg=_CFG,
    )
    assert result.admitted is True
    assert result.proposal is not None
    short_leg, long_leg = result.proposal.option_legs
    short_k = float(parse_strike(short_leg.symbol))
    long_k = float(parse_strike(long_leg.symbol))
    assert long_k > short_k, (
        f"bear_call_spread: long strike ({long_k}) must be ABOVE short strike ({short_k})"
    )


# ---------------------------------------------------------------------------
# BCS-2: over-cap — gate REJECTS (fail-closed)
# ---------------------------------------------------------------------------


def test_bcs2_over_cap_max_loss_rejected() -> None:
    """BCS-2: when max_loss > max_position_pct * nav, gate rejects (fail-closed)."""
    # max_position_pct=0.10; tiny_nav=1k -> cap=100.
    # Our spread max_loss = 850/contract; 850 > 100 -> REJECT.
    tiny_nav = 1000.0
    chain = _make_chain()
    result = build_bear_call_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=tiny_nav,
        options_buying_power=500.0,
        cfg=_CFG,
    )
    assert result.admitted is False
    assert result.reason in ("max_loss_exceeds_position_cap", "min_trade_size"), (
        f"expected rejection reason, got {result.reason!r}"
    )


def test_bcs2_gate_reject_is_fail_closed_no_proposal() -> None:
    """BCS-2 supplement: a rejected build returns proposal=None (never a passing proposal)."""
    chain = _make_chain()
    result = build_bear_call_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=100.0,   # tiny nav -> gate rejects
        options_buying_power=50.0,
        cfg=_CFG,
    )
    assert result.admitted is False
    assert result.proposal is None  # no passing proposal on reject


# ---------------------------------------------------------------------------
# BCS-3: non-finite long-leg mid (NaN) is rejected pre-gate (fail-closed)
# ---------------------------------------------------------------------------


def _nan_mid_long_snap() -> OptionSnapshot:
    """A long-call snapshot whose MID is a float('nan').

    This exercises the same wave2-review fail-open path as BPS-3b: a NaN float
    passes BOTH `mid is None` (False) AND `mid <= 0` (NaN<=0 is False), so
    WITHOUT the math.isfinite guard it enters the candidate list. The guard in
    _eligible_snapshots filters it out, leaving no eligible long-call strike.
    """
    nan = float("nan")
    return OptionSnapshot(
        symbol=_LONG_SYM, asof=_ASOF, fetched_at=_ASOF,
        bid=nan, ask=nan, last=None,
        volume=0, open_interest=0,
        greeks=OptionGreeksSnapshot(delta=0.15, gamma=0.01, theta=-0.04, vega=0.06, rho=0.005),
        underlying_spot=_SPOT, risk_free_rate=0.05,
    )


def test_bcs3_nan_float_long_mid_is_filtered_not_treated_as_free() -> None:
    """BCS-3: a NaN-FLOAT long-leg mid must be FILTERED by _eligible_snapshots,
    NOT selected and coerced to 0.0. With the only long candidate carrying a NaN
    mid, the spread has no eligible protective leg and the producer must REJECT
    (RecipeBuildError) — it must NEVER build a spread whose long leg is treated
    as free protection (understates max_loss -> over-sizes).
    """
    import math as _math

    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=0.25)
    long_nan = _nan_mid_long_snap()
    # Precondition: the long snap's mid really IS a NaN float (not None).
    assert long_nan.mid is not None and _math.isnan(long_nan.mid)

    chain = OptionChain(
        underlying="NVDA", asof=_ASOF, underlying_spot=_SPOT, risk_free_rate=0.05,
        snapshots=(short_snap, long_nan),
    )
    # The NaN long is filtered -> no eligible long-call strike -> REJECT (fail-closed).
    with pytest.raises(RecipeBuildError, match="no eligible long-call strike"):
        build_bear_call_spread_proposal(
            symbol="NVDA", asof=_ASOF, chain=chain,
            nav=_NAV, options_buying_power=_OBP, cfg=_CFG,
        )


# ---------------------------------------------------------------------------
# BCS-4 / BCS-5: structure_select flag gate
# ---------------------------------------------------------------------------


def test_bcs4_select_returns_bear_call_spread_when_flag_on(monkeypatch) -> None:
    """BCS-4: select_structure returns 'bear_call_spread' for BEARISH +
    defined_risk_credit + MID/HIGH IV when HERMES_QUANT_VERTICAL_SPREADS=1."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")

    for regime in (IVRegime.MID, IVRegime.HIGH):
        result = select_structure(
            direction=Direction.BEARISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result == "bear_call_spread", (
            f"expected 'bear_call_spread' for regime={regime}, got {result!r}"
        )


def test_bcs5_select_abstains_when_flag_off(monkeypatch) -> None:
    """BCS-5 (flag-OFF byte-identical test): select_structure returns None for
    BEARISH defined_risk_credit when HERMES_QUANT_VERTICAL_SPREADS is absent/off.
    This is the flag-OFF proof — proves the flag gate is real."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)
    assert vertical_spreads_enabled() is False

    for regime in (IVRegime.MID, IVRegime.HIGH, IVRegime.LOW):
        result = select_structure(
            direction=Direction.BEARISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result is None, (
            f"flag OFF: expected None for BEARISH defined_risk_credit+{regime}, got {result!r}"
        )


def test_bcs5_flag_off_premium_capture_unaffected(monkeypatch) -> None:
    """BCS-5 supplement: the VERTICAL_SPREADS_FLAG does NOT affect PREMIUM_CAPTURE rows.
    CC/CSP/wheel remain selectable regardless of the flag."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)

    # PREMIUM_CAPTURE + BEARISH + HIGH -> covered_call (unaffected).
    assert (
        select_structure(
            direction=Direction.BEARISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.HIGH,
        )
        == "covered_call"
    )

    # PREMIUM_CAPTURE + BULLISH + MID -> cash_secured_put (unaffected).
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.MID,
        )
        == "cash_secured_put"
    )


def test_bcs5_low_iv_defined_risk_credit_abstains_even_when_flag_on(monkeypatch) -> None:
    """BCS-5 supplement: LOW IV + BEARISH defined_risk_credit abstains even when
    the flag is ON. The table has no LOW-IV rows for defined_risk_credit."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")

    result = select_structure(
        direction=Direction.BEARISH,
        structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
        iv_regime=IVRegime.LOW,
    )
    assert result is None, f"expected None for LOW IV, got {result!r}"


def test_bcs4_bull_put_spread_unchanged_when_flag_on(monkeypatch) -> None:
    """BCS-4 supplement: BULLISH + defined_risk_credit + MID/HIGH still returns
    'bull_put_spread' (bear_call_spread does not displace it)."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")

    for regime in (IVRegime.MID, IVRegime.HIGH):
        result = select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result == "bull_put_spread", (
            f"expected 'bull_put_spread' for BULLISH + regime={regime}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# BCS-6: RED-proof — without the long call the gate classifies as NAKED (reject)
# ---------------------------------------------------------------------------


def test_bcs6_red_proof_single_short_call_is_naked_no_long_leg(monkeypatch) -> None:
    """BCS-6 (RED-proof): a lone short call (no long protection) is NAKED and REJECTED.

    This proves that the long leg is load-bearing: removing it from the legs list
    causes _classify_structure to return NAKED (the short call has neither covering
    shares NOR a long call cap), and the gate rejects it as naked.
    """
    from hermes_quant.risk.options_gate import options_gate

    _small_greeks_short = OptionGreeksSnapshot(
        delta=0.25, gamma=0.0001, theta=-0.005, vega=0.001, rho=0.001,
    )
    _small_greeks_long = OptionGreeksSnapshot(
        delta=0.15, gamma=0.0001, theta=-0.004, vega=0.0008, rho=0.0005,
    )

    short_leg = OptionLeg(
        symbol=_SHORT_SYM,
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=_small_greeks_short,
    )
    # ONE-LEG: just the short call, no long protection. This is a naked short call.
    result_naked = options_gate(
        [short_leg],
        strategy_kind="covered_call",   # even as CC framing
        underlying="NVDA",
        spot=_SPOT,
        nav=_NAV,
        held_shares=0,                  # zero shares -> NOT covered -> NAKED
        options_buying_power=_OBP,
        premium_received=_SHORT_MID * 100,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=_SHORT_STRIKE,
        min_dte=30,
    )
    assert result_naked.admitted is False
    assert result_naked.bucket == StructureBucket.NAKED

    # TWO-LEG (adding the long call): now DEFINED_RISK and can be admitted.
    long_leg = OptionLeg(
        symbol=_LONG_SYM,
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=_small_greeks_long,
    )
    result_defined = options_gate(
        [short_leg, long_leg],
        strategy_kind="bear_call_spread",
        underlying="NVDA",
        spot=_SPOT,
        nav=_NAV,
        held_shares=0,
        options_buying_power=_OBP,
        premium_received=(_SHORT_MID - _LONG_MID) * 100,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=_SHORT_STRIKE,
        width=_WIDTH,
        net_credit=_NET_CREDIT,
        min_dte=30,
    )
    assert result_defined.admitted is True
    assert result_defined.bucket == StructureBucket.DEFINED_RISK

    # The max_loss for the defined-risk version must be finite.
    assert result_defined.max_loss is not None
    assert math.isfinite(result_defined.max_loss)


# ---------------------------------------------------------------------------
# _pick_long_call unit tests
# ---------------------------------------------------------------------------


def test_pick_long_call_selects_lowest_qualifying_strike() -> None:
    """_pick_long_call returns the lowest-strike call above the short strike
    (narrowest valid width, income-maximizing)."""
    from hermes_quant.options.occ import parse_occ

    # Three candidate higher-strike calls on the same expiry.
    cands = [
        _make_snap("NVDA260717C00170000", mid_bid_ask=1.00, delta=0.15),  # K=170
        _make_snap("NVDA260717C00175000", mid_bid_ask=0.70, delta=0.12),  # K=175
        _make_snap("NVDA260717C00180000", mid_bid_ask=0.50, delta=0.10),  # K=180
    ]
    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=0.25)
    all_snaps = cands + [short_snap]

    chosen = _pick_long_call(all_snaps, short=short_snap)
    # Lowest qualifying K above 160 = 170.
    assert float(parse_occ(chosen.symbol).strike) == pytest.approx(170.0)


def test_pick_long_call_raises_when_no_eligible_strike() -> None:
    """_pick_long_call raises RecipeBuildError when no higher-strike exists."""
    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=0.25)
    # Only the short snap, no higher-strike candidates.
    with pytest.raises(RecipeBuildError, match="no eligible long-call strike"):
        _pick_long_call([short_snap], short=short_snap)


# ---------------------------------------------------------------------------
# vertical_spreads_enabled helper (mirrors BPS tests)
# ---------------------------------------------------------------------------


def test_vertical_spreads_enabled_default_off(monkeypatch) -> None:
    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)
    assert vertical_spreads_enabled() is False


@pytest.mark.parametrize("val", ["0", "", "true", "yes", "2", "on"])
def test_vertical_spreads_enabled_only_literal_1(monkeypatch, val) -> None:
    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, val)
    assert vertical_spreads_enabled() is False


def test_vertical_spreads_enabled_literal_1(monkeypatch) -> None:
    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")
    assert vertical_spreads_enabled() is True
