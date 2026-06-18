"""Unit tests for aegis-ic1 — Iron Condor producer (ADR-0098 Step 5).

An iron condor = a SHORT bull-put spread (sell ~0.30-delta OTM put + buy a
further-OTM lower-strike protective put, BELOW spot) + a SHORT bear-call spread
(sell ~0.30-delta OTM call + buy a further-OTM higher-strike protective call,
ABOVE spot) — all four legs on the SAME underlying and SAME expiry. NEUTRAL +
DEFINED_RISK_CREDIT, for HIGH-IV regimes. Deterministic, offline, no network, no LLM.

Coverage contract:
  IC-1  HAPPY PATH: a balanced chain -> a 4-leg iron condor proposal with the
        correct legs (2 puts below spot, 2 calls above), StructureBucket
        defined-risk, FINITE bounded max_loss = max(widths)*100 - net_credit,
        strategy_kind='iron_condor'. RED-prove the max_loss formula.
  IC-2  DEFINED-RISK / NO-NAKED: removing a long wing -> REJECT (no mint), NOT a
        naked short. RED-prove.
  IC-3  FINITE-GUARD: a NaN mid on one wing -> abstain (no proposal), max_loss
        never fabricated. RED-prove.
  IC-4  BREAKEVEN-TOO-TIGHT REJECT: net_credit too small vs width -> gate rejects.
  IC-5  DEFAULT-OFF byte-identical: HERMES_QUANT_IRON_CONDOR unset -> structure_select
        has no iron_condor row -> select_structure_for_plan never returns 'iron_condor'.
        RED-prove the table has no iron_condor result when the flag is off.
  IC-6  NEUTRAL+HIGH-IV gating: structure_select returns 'iron_condor' ONLY for
        NEUTRAL direction + HIGH-IV regime (flag on), None otherwise.
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
    RecipeBuildError,
    build_iron_condor_proposal,
    parse_strike,
)
from hermes_quant.options.structure_select import (
    IRON_CONDOR_FLAG,
    Direction,
    IVRegime,
    iron_condor_enabled,
    select_structure,
)
from hermes_quant.risk.options_gate import (
    OptionsRiskConfig,
    StructureBucket,
)

# ---------------------------------------------------------------------------
# Shared fixtures: a balanced four-strike OCC-21 option chain for NVDA.
# Spot 150. Expiry 2026-07-17 (dte ~30 from 2026-06-17 decision). OCC symbols:
#   short put:  NVDA260717P00140000 (K=140, |delta|~0.30 -> short put)
#   long  put:  NVDA260717P00130000 (K=130, |delta|~0.15 -> long put wing)
#   short call: NVDA260717C00160000 (K=160, |delta|~0.30 -> short call)
#   long  call: NVDA260717C00170000 (K=170, |delta|~0.15 -> long call wing)
# put_width = 140-130 = 10 ; call_width = 170-160 = 10 ; max_width = 10.
# put_credit = 2.50-1.00 = 1.50 ; call_credit = 2.50-1.00 = 1.50 ; total = 3.00.
# max_loss = (max_width - total_credit) * 100 = (10 - 3.00) * 100 = 700.0.
# ---------------------------------------------------------------------------

_ASOF = datetime(2026, 6, 17, 15, 0, 0, tzinfo=UTC)
_NAV = 1_000_000.0
_OBP = 500_000.0
_SPOT = 150.0
_CFG = OptionsRiskConfig()

_SHORT_PUT_SYM = "NVDA260717P00140000"
_LONG_PUT_SYM = "NVDA260717P00130000"
_SHORT_CALL_SYM = "NVDA260717C00160000"
_LONG_CALL_SYM = "NVDA260717C00170000"

_SHORT_PUT_STRIKE = 140.0
_LONG_PUT_STRIKE = 130.0
_SHORT_CALL_STRIKE = 160.0
_LONG_CALL_STRIKE = 170.0

_PUT_WIDTH = _SHORT_PUT_STRIKE - _LONG_PUT_STRIKE   # 10.0
_CALL_WIDTH = _LONG_CALL_STRIKE - _SHORT_CALL_STRIKE  # 10.0
_MAX_WIDTH = max(_PUT_WIDTH, _CALL_WIDTH)            # 10.0

_SHORT_MID = 2.50  # short leg premium (collect), both sides
_LONG_MID = 1.00   # long wing premium (pay), both sides
_TOTAL_NET_CREDIT = (_SHORT_MID - _LONG_MID) * 2     # 3.00 / share
_MAX_LOSS_PER_CONTRACT = (_MAX_WIDTH - _TOTAL_NET_CREDIT) * 100  # (10-3)*100 = 700.0


def _make_snap(
    symbol: str, *, mid_bid_ask: float | None, delta: float, nan_mid: bool = False
) -> OptionSnapshot:
    """Minimal OptionSnapshot with a known mid and delta.

    Tiny gamma/vega so greek caps never bind — the condor max_loss is the
    constraint under test. ``nan_mid`` forces a NaN-float mid (IC-3)."""
    if nan_mid:
        bid = ask = float("nan")
    elif mid_bid_ask is None:
        bid = ask = None
    else:
        half = mid_bid_ask / 2
        bid = mid_bid_ask - half * 0.05
        ask = mid_bid_ask + half * 0.05
    return OptionSnapshot(
        symbol=symbol,
        asof=_ASOF,
        fetched_at=_ASOF,
        bid=bid,
        ask=ask,
        last=mid_bid_ask,
        volume=500,
        open_interest=1000,
        greeks=OptionGreeksSnapshot(
            delta=delta,
            gamma=0.0001,
            theta=-0.005,
            vega=0.001,
            rho=0.001,
        ),
        underlying_spot=_SPOT,
        risk_free_rate=0.05,
    )


def _make_chain(
    *,
    short_put_mid: float = _SHORT_MID,
    long_put_mid: float | None = _LONG_MID,
    short_call_mid: float = _SHORT_MID,
    long_call_mid: float | None = _LONG_MID,
    long_call_nan: bool = False,
    drop_long_call: bool = False,
) -> OptionChain:
    """Balanced four-snapshot iron-condor chain for NVDA.

    Puts carry NEGATIVE deltas (a real put delta is < 0); the gate's short-delta
    cap uses the magnitude. ``drop_long_call`` / ``long_call_nan`` exercise the
    no-naked and finite-guard rejects.
    """
    snaps = [
        _make_snap(_SHORT_PUT_SYM, mid_bid_ask=short_put_mid, delta=-0.30),
        _make_snap(_LONG_PUT_SYM, mid_bid_ask=long_put_mid, delta=-0.15),
        _make_snap(_SHORT_CALL_SYM, mid_bid_ask=short_call_mid, delta=0.30),
    ]
    if not drop_long_call:
        snaps.append(
            _make_snap(
                _LONG_CALL_SYM,
                mid_bid_ask=long_call_mid,
                delta=0.15,
                nan_mid=long_call_nan,
            )
        )
    return OptionChain(
        underlying="NVDA",
        asof=_ASOF,
        underlying_spot=_SPOT,
        risk_free_rate=0.05,
        snapshots=tuple(snaps),
    )


@pytest.fixture(autouse=True)
def _gate_enabled(monkeypatch):
    """Most producer tests need the universal options gate enabled."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


# ---------------------------------------------------------------------------
# IC-1: HAPPY PATH — 4 legs, finite bounded max_loss, defined-risk bucket
# ---------------------------------------------------------------------------


def test_ic1_happy_path_four_legs_defined_risk() -> None:
    """IC-1: a balanced chain builds a 4-leg iron condor with the correct legs."""
    chain = _make_chain()
    result = build_iron_condor_proposal(
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
    assert p.strategy_kind == "iron_condor"

    # Exactly 4 option legs, no stock leg.
    assert len(p.option_legs) == 4
    assert p.stock_leg is None

    short_put, long_put, short_call, long_call = p.option_legs

    # Put side BELOW spot: short put higher strike, long put lower strike.
    assert short_put.right == "P" and short_put.side == "sell"
    assert short_put.position_intent == "sell_to_open"
    assert long_put.right == "P" and long_put.side == "buy"
    assert long_put.position_intent == "buy_to_open"
    assert float(parse_strike(short_put.symbol)) == pytest.approx(_SHORT_PUT_STRIKE)
    assert float(parse_strike(long_put.symbol)) == pytest.approx(_LONG_PUT_STRIKE)
    assert float(parse_strike(long_put.symbol)) < float(parse_strike(short_put.symbol))
    assert float(parse_strike(short_put.symbol)) < _SPOT  # both puts below spot

    # Call side ABOVE spot: short call lower strike, long call higher strike.
    assert short_call.right == "C" and short_call.side == "sell"
    assert short_call.position_intent == "sell_to_open"
    assert long_call.right == "C" and long_call.side == "buy"
    assert long_call.position_intent == "buy_to_open"
    assert float(parse_strike(short_call.symbol)) == pytest.approx(_SHORT_CALL_STRIKE)
    assert float(parse_strike(long_call.symbol)) == pytest.approx(_LONG_CALL_STRIKE)
    assert float(parse_strike(long_call.symbol)) > float(parse_strike(short_call.symbol))
    assert float(parse_strike(short_call.symbol)) > _SPOT  # both calls above spot

    # Bucket is DEFINED_RISK.
    assert result.bucket == StructureBucket.DEFINED_RISK

    # max_loss is FINITE, positive, and equals max(widths)*100 - net_credit (700).
    assert p.max_loss is not None
    assert isinstance(p.max_loss, Decimal)
    max_loss_float = float(p.max_loss)
    assert math.isfinite(max_loss_float)
    assert max_loss_float > 0
    assert max_loss_float == pytest.approx(_MAX_LOSS_PER_CONTRACT, rel=1e-6), (
        f"iron condor max_loss must be max(widths)*100 - net_credit "
        f"= {_MAX_LOSS_PER_CONTRACT}, got {max_loss_float}"
    )

    # risk_gate_pass mirrors the gate verdict.
    assert p.risk_gate_pass is True


def test_ic1_max_loss_is_not_sum_of_widths_nor_no_credit() -> None:
    """IC-1 RED-proof: the max_loss formula is load-bearing. A wrong formula
    (sum-of-widths, or width*100 with no credit) diverges from the correct value.

    correct        = max(10,10)*100 - 300 = 700
    sum-of-widths  = (10+10)*100 - 300 = 1700  (WRONG: both wings can't both lose)
    no-credit      = 10*100            = 1000  (WRONG: credit reduces the loss)
    """
    chain = _make_chain()
    result = build_iron_condor_proposal(
        symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
        options_buying_power=_OBP, cfg=_CFG,
    )
    assert result.proposal is not None
    ml = float(result.proposal.max_loss)
    sum_of_widths = (_PUT_WIDTH + _CALL_WIDTH) * 100 - _TOTAL_NET_CREDIT * 100  # 1700
    no_credit = _MAX_WIDTH * 100  # 1000
    assert ml == pytest.approx(700.0, rel=1e-6)
    assert ml != pytest.approx(sum_of_widths, rel=1e-6)
    assert ml != pytest.approx(no_credit, rel=1e-6)


def test_ic1_net_debit_credit_is_negative_credit_received() -> None:
    """IC-1 supplement: net_debit_credit is negative (TOTAL credit received)."""
    chain = _make_chain()
    result = build_iron_condor_proposal(
        symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
        options_buying_power=_OBP, cfg=_CFG,
    )
    assert result.proposal is not None
    # Per-contract credit = total_net_credit * 100 = 300; stored negative.
    ndc = float(result.proposal.net_debit_credit)
    assert ndc < 0
    contracts = result.contracts
    assert ndc == pytest.approx(-_TOTAL_NET_CREDIT * 100 * contracts, rel=1e-6)


def test_ic1_two_breakevens_bracket_the_short_strikes() -> None:
    """IC-1 supplement: the condor carries TWO breakevens that bracket the profit
    zone (short put - credit, short call + credit)."""
    chain = _make_chain()
    result = build_iron_condor_proposal(
        symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
        options_buying_power=_OBP, cfg=_CFG,
    )
    assert result.proposal is not None
    bes = result.proposal.breakeven_underlying
    assert len(bes) == 2
    lower, upper = float(bes[0]), float(bes[1])
    assert lower == pytest.approx(_SHORT_PUT_STRIKE - _TOTAL_NET_CREDIT, rel=1e-6)
    assert upper == pytest.approx(_SHORT_CALL_STRIKE + _TOTAL_NET_CREDIT, rel=1e-6)
    assert lower < upper


# ---------------------------------------------------------------------------
# IC-2: DEFINED-RISK / NO-NAKED — removing a long wing -> REJECT (no mint)
# ---------------------------------------------------------------------------


def test_ic2_missing_long_call_wing_rejects_not_naked() -> None:
    """IC-2 (RED-proof): dropping the long call wing leaves the short call NAKED;
    the producer must REJECT (RecipeBuildError) and NEVER mint a naked-short side.

    With only the long PUT wing present, the call side has no protection. The
    producer's _pick_long_call finds no eligible higher-strike call and raises —
    the structure is never built. This proves the long call wing is load-bearing:
    without it, a side would be a naked short call (undefined risk).
    """
    chain = _make_chain(drop_long_call=True)
    with pytest.raises(RecipeBuildError, match="no eligible long-call strike"):
        build_iron_condor_proposal(
            symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
            options_buying_power=_OBP, cfg=_CFG,
        )


def test_ic2_gate_classifies_three_legs_missing_wing_as_naked() -> None:
    """IC-2 supplement (gate-level RED-proof): a 3-leg structure (short put + long
    put + short call, NO long call) is classified NAKED by the gate — the short
    call has neither covering shares NOR a covering long call. This proves the
    bucket is genuinely naked when a wing is missing (not silently defined-risk).
    """
    from hermes_quant.risk.options_gate import options_gate

    g_short = OptionGreeksSnapshot(delta=-0.30, gamma=0.0001, theta=-0.005, vega=0.001, rho=0.001)
    g_long = OptionGreeksSnapshot(delta=-0.15, gamma=0.0001, theta=-0.004, vega=0.0008, rho=0.0005)
    g_short_c = OptionGreeksSnapshot(delta=0.30, gamma=0.0001, theta=-0.005, vega=0.001, rho=0.001)

    short_put = OptionLeg(symbol=_SHORT_PUT_SYM, side="sell", position_intent="sell_to_open", greeks_at_decision=g_short)
    long_put = OptionLeg(symbol=_LONG_PUT_SYM, side="buy", position_intent="buy_to_open", greeks_at_decision=g_long)
    short_call = OptionLeg(symbol=_SHORT_CALL_SYM, side="sell", position_intent="sell_to_open", greeks_at_decision=g_short_c)

    # 3 legs: the short call has no long-call cover -> NAKED (one short uncovered).
    res = options_gate(
        [short_put, long_put, short_call],
        strategy_kind="iron_condor",
        underlying="NVDA",
        spot=_SPOT,
        nav=_NAV,
        held_shares=0,
        options_buying_power=_OBP,
        premium_received=300.0,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=_SHORT_PUT_STRIKE,
        width=_MAX_WIDTH,
        net_credit=_TOTAL_NET_CREDIT,
        min_dte=30,
    )
    assert res.admitted is False
    assert res.bucket == StructureBucket.NAKED


# ---------------------------------------------------------------------------
# IC-3: FINITE-GUARD — a NaN mid on one wing -> abstain (no proposal)
# ---------------------------------------------------------------------------


def test_ic3_nan_long_call_mid_is_filtered_not_treated_as_free() -> None:
    """IC-3 (RED-proof): a NaN-FLOAT mid on the long call wing must be FILTERED by
    _eligible_snapshots (math.isfinite), NOT selected and coerced to 0.0 free
    protection. With the only long-call candidate carrying a NaN mid, the condor
    has no eligible call protection and the producer must REJECT — it must NEVER
    fabricate a max_loss from a free wing (which understates risk -> over-sizes).
    """
    chain = _make_chain(long_call_nan=True)
    # Precondition: the long call snap's mid really is a NaN float (not None).
    long_call_snap = next(s for s in chain.snapshots if s.symbol == _LONG_CALL_SYM)
    assert long_call_snap.mid is not None and math.isnan(long_call_snap.mid)

    with pytest.raises(RecipeBuildError, match="no eligible long-call strike"):
        build_iron_condor_proposal(
            symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
            options_buying_power=_OBP, cfg=_CFG,
        )


def test_ic3_no_proposal_and_no_fabricated_max_loss_on_nan() -> None:
    """IC-3 supplement: on the NaN-wing path nothing is minted — there is no
    proposal object carrying a fabricated max_loss. (The reject is by raise; we
    assert the raise leaves no MultiLegBuildResult with a proposal.)"""
    chain = _make_chain(long_call_nan=True)
    with pytest.raises(RecipeBuildError):
        build_iron_condor_proposal(
            symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
            options_buying_power=_OBP, cfg=_CFG,
        )


# ---------------------------------------------------------------------------
# IC-4: BREAKEVEN-TOO-TIGHT REJECT — tiny net_credit vs width -> gate rejects
# ---------------------------------------------------------------------------


def test_ic4_thin_credit_wide_max_loss_rejected_by_gate() -> None:
    """IC-4: a thin net credit against a wide max_loss is rejected by the gate.

    With a tiny credit (0.05/leg net) the max_loss ~= max_width*100 = ~990, which
    against a tiny nav exceeds the position cap (max_position_pct=0.10; nav=1000
    -> cap=100). The gate rejects (max_loss_exceeds_position_cap or min_trade_size)
    -> no passing proposal. This mirrors the verticals' own gate-driven reject.
    """
    tiny_nav = 1000.0
    # short 0.55, long 0.50 -> 0.05 net credit/leg -> 0.10 total -> max_loss ~= 990.
    chain = _make_chain(
        short_put_mid=0.55, long_put_mid=0.50,
        short_call_mid=0.55, long_call_mid=0.50,
    )
    result = build_iron_condor_proposal(
        symbol="NVDA", asof=_ASOF, chain=chain, nav=tiny_nav,
        options_buying_power=500.0, cfg=_CFG,
    )
    assert result.admitted is False
    assert result.proposal is None  # fail-closed: no passing proposal on reject
    assert result.reason in ("max_loss_exceeds_position_cap", "min_trade_size"), (
        f"expected a gate rejection reason, got {result.reason!r}"
    )


def test_ic4_nonpositive_credit_rejected_pre_gate() -> None:
    """IC-4 supplement: a NON-POSITIVE total net credit (paying more for the wings
    than collected on the shorts) is rejected BEFORE the gate (fail-closed) — a
    condor that pays net debit is not a credit structure and has no valid
    max_loss = (width - credit) interpretation.
    """
    # Wings cost MORE than the shorts collect -> negative net credit.
    chain = _make_chain(
        short_put_mid=1.00, long_put_mid=2.00,
        short_call_mid=1.00, long_call_mid=2.00,
    )
    result = build_iron_condor_proposal(
        symbol="NVDA", asof=_ASOF, chain=chain, nav=_NAV,
        options_buying_power=_OBP, cfg=_CFG,
    )
    assert result.admitted is False
    assert result.proposal is None
    assert result.reason == "iron_condor_credit_or_max_loss_not_finite_or_nonpositive"


# ---------------------------------------------------------------------------
# IC-5: DEFAULT-OFF byte-identical — flag unset -> no iron_condor row
# ---------------------------------------------------------------------------


def test_ic5_select_abstains_when_iron_condor_flag_off(monkeypatch) -> None:
    """IC-5 (RED-proof for the flag gate): with HERMES_QUANT_IRON_CONDOR absent,
    select_structure NEVER returns 'iron_condor' for NEUTRAL defined_risk_credit
    at ANY regime. This is the byte-identical / default-OFF proof."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.delenv(IRON_CONDOR_FLAG, raising=False)
    assert iron_condor_enabled() is False

    for regime in (IVRegime.LOW, IVRegime.MID, IVRegime.HIGH):
        result = select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result is None, (
            f"flag OFF: expected None for NEUTRAL defined_risk_credit+{regime}, "
            f"got {result!r}"
        )


def test_ic5_select_for_plan_never_returns_iron_condor_when_flag_off(monkeypatch) -> None:
    """IC-5 supplement: the consumer wrapper select_structure_for_plan never
    returns 'iron_condor' when the iron-condor flag is OFF, even with the
    structure-select seam enabled and a NEUTRAL HIGH-IV plan."""
    from hermes_quant.agents.research_debate.schemas import (
        PortfolioRating,
        StructureIntent,
    )
    from hermes_quant.options.structure_select import select_structure_for_plan

    monkeypatch.setenv("HERMES_QUANT_STRUCTURE_SELECT", "1")
    monkeypatch.delenv(IRON_CONDOR_FLAG, raising=False)

    class _Plan:
        recommendation = PortfolioRating.HOLD  # -> NEUTRAL
        structure_intent = StructureIntent.DEFINED_RISK_CREDIT

    result = select_structure_for_plan(_Plan(), iv_regime=IVRegime.HIGH)
    assert result != "iron_condor"
    assert result is None  # neutral defined-risk-credit abstains with flag OFF


def test_ic5_iron_condor_flag_does_not_leak_to_verticals(monkeypatch) -> None:
    """IC-5 supplement: enabling the IRON_CONDOR flag alone does NOT enable the
    single-side verticals (independent flags), and enabling VERTICAL_SPREADS alone
    does NOT enable the iron condor."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent
    from hermes_quant.options.structure_select import VERTICAL_SPREADS_FLAG

    # IRON_CONDOR on, VERTICAL_SPREADS off: condor selectable, verticals NOT.
    monkeypatch.setenv(IRON_CONDOR_FLAG, "1")
    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)
    assert (
        select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=IVRegime.HIGH,
        )
        == "iron_condor"
    )
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=IVRegime.HIGH,
        )
        is None  # vertical flag OFF -> abstain
    )

    # VERTICAL_SPREADS on, IRON_CONDOR off: verticals selectable, condor NOT.
    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")
    monkeypatch.delenv(IRON_CONDOR_FLAG, raising=False)
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=IVRegime.HIGH,
        )
        == "bull_put_spread"
    )
    assert (
        select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=IVRegime.HIGH,
        )
        is None  # iron-condor flag OFF -> abstain
    )


# ---------------------------------------------------------------------------
# IC-6: NEUTRAL+HIGH-IV gating — only NEUTRAL + HIGH returns 'iron_condor'
# ---------------------------------------------------------------------------


def test_ic6_select_returns_iron_condor_only_neutral_high(monkeypatch) -> None:
    """IC-6: with the flag ON, select_structure returns 'iron_condor' ONLY for
    NEUTRAL direction + HIGH-IV regime; every other (direction, regime) for
    defined_risk_credit returns either a vertical/None, never 'iron_condor' for a
    non-NEUTRAL stance, and None for NEUTRAL at non-HIGH regimes."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(IRON_CONDOR_FLAG, "1")
    # The verticals' flag is independent and OFF here, so BULLISH/BEARISH
    # defined_risk_credit abstains (None) rather than returning a vertical.
    monkeypatch.delenv("HERMES_QUANT_VERTICAL_SPREADS", raising=False)

    # The ONLY iron_condor cell: NEUTRAL + HIGH.
    assert (
        select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=IVRegime.HIGH,
        )
        == "iron_condor"
    )

    # NEUTRAL at non-HIGH regimes -> None (no MID/LOW condor row).
    for regime in (IVRegime.MID, IVRegime.LOW):
        assert (
            select_structure(
                direction=Direction.NEUTRAL,
                structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
                iv_regime=regime,
            )
            is None
        )

    # Non-NEUTRAL directions never return 'iron_condor'.
    for direction in (Direction.BULLISH, Direction.BEARISH):
        for regime in (IVRegime.LOW, IVRegime.MID, IVRegime.HIGH):
            assert (
                select_structure(
                    direction=direction,
                    structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
                    iv_regime=regime,
                )
                != "iron_condor"
            )


def test_ic6_iron_condor_enabled_only_literal_1(monkeypatch) -> None:
    """IC-6 supplement: iron_condor_enabled is fail-closed — only the literal '1'
    enables it (a typo / partial config never silently enables the seam)."""
    for val in ("0", "", "true", "yes", "2", "on"):
        monkeypatch.setenv(IRON_CONDOR_FLAG, val)
        assert iron_condor_enabled() is False
    monkeypatch.setenv(IRON_CONDOR_FLAG, "1")
    assert iron_condor_enabled() is True
