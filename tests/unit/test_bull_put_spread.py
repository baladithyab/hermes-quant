"""Unit tests for aegis-bps1 — Bull Put Spread producer (ADR-0098 Step 2).

Deterministic, offline, no network, no LLM.

Coverage contract:
  BPS-1  Within-budget BPS builds 2 legs: short higher-strike put + long
         lower-strike put; max_loss is finite; gate ADMITS it.
  BPS-2  Over-cap BPS (max_loss > max_position_pct * nav) is REJECTED
         (fail-closed).
  BPS-3  Non-finite long-leg mid (NaN) is rejected pre-gate (fail-closed).
  BPS-4  structure_select returns 'bull_put_spread' for BULLISH +
         defined_risk_credit + MID/HIGH IV ONLY when
         HERMES_QUANT_VERTICAL_SPREADS=1.
  BPS-5  structure_select returns None (abstain) for the same inputs when
         HERMES_QUANT_VERTICAL_SPREADS is absent (flag-OFF; byte-identical to
         today). This is the RED-proof for the flag-gate.
  BPS-6  RED-proof for the producer: reverting the long-leg addition makes the
         gate classify the structure as NAKED (short put with no cover/wider-long),
         which is a gate REJECT — proving the test was testing a real defect.
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
    _pick_long_put,
    _snapshot_to_long_leg,
    _snapshot_to_short_leg,
    build_bull_put_spread_proposal,
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
#   short:  NVDA260717P00140000  (put, K=140, |delta|~0.25 -> short leg)
#   long:   NVDA260717P00130000  (put, K=130, |delta|~0.15 -> long protection)
# ---------------------------------------------------------------------------

_ASOF = datetime(2026, 6, 17, 15, 0, 0, tzinfo=UTC)
_NAV = 1_000_000.0
_OBP = 500_000.0          # options buying power (ample for 1-lot BPS)
_SPOT = 150.0
_CFG = OptionsRiskConfig()

_SHORT_SYM = "NVDA260717P00140000"
_LONG_SYM  = "NVDA260717P00130000"
_SHORT_STRIKE = 140.0
_LONG_STRIKE  = 130.0
_WIDTH = _SHORT_STRIKE - _LONG_STRIKE  # 10.0
_SHORT_MID = 2.50   # short put premium (collect)
_LONG_MID  = 1.00   # long put premium (pay)
_NET_CREDIT = _SHORT_MID - _LONG_MID  # 1.50 / share
_MAX_LOSS_PER_CONTRACT = (_WIDTH - _NET_CREDIT) * 100  # (10-1.5)*100 = 850.0


def _make_snap(symbol: str, *, mid_bid_ask: float, delta: float) -> OptionSnapshot:
    """Minimal OptionSnapshot with a known mid and delta.

    Uses small gamma/vega values so the greek caps are never the binding constraint
    even at multi-contract admitted sizes.  The BPS max_loss = (width-credit)*100 is
    the binding constraint we want to test.
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
            theta=0.005,    # theta-collecting (short put positive theta from short)
            vega=0.001,     # tiny vega -> vega cap not binding
            rho=-0.001,
        ),
        underlying_spot=_SPOT,
        risk_free_rate=0.05,
    )


def _make_chain(
    short_mid: float = _SHORT_MID,
    long_mid: float = _LONG_MID,
    short_delta: float = -0.25,
    long_delta: float = -0.15,
) -> OptionChain:
    """Two-snapshot chain (short + long put) for NVDA."""
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
# BPS-1: within-budget build — 2 legs, finite max_loss, gate ADMITS
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gate_enabled(monkeypatch):
    """Most tests need the options gate enabled."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_GATE", "1")


def test_bps1_within_budget_two_legs_admitted(monkeypatch) -> None:
    """BPS-1: a within-budget bull-put-spread builds 2 legs and is ADMITTED."""
    chain = _make_chain()
    result = build_bull_put_spread_proposal(
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
    assert p.strategy_kind == "bull_put_spread"

    # Exactly 2 option legs, no stock leg.
    assert len(p.option_legs) == 2
    assert p.stock_leg is None

    # Leg identity: short higher-strike put first, long lower-strike put second.
    short_leg, long_leg = p.option_legs
    assert short_leg.side == "sell"
    assert short_leg.position_intent == "sell_to_open"
    assert short_leg.symbol == _SHORT_SYM

    assert long_leg.side == "buy"
    assert long_leg.position_intent == "buy_to_open"
    assert long_leg.symbol == _LONG_SYM

    # Both puts.
    assert short_leg.right == "P"
    assert long_leg.right == "P"

    # max_loss is finite and positive.
    assert p.max_loss is not None
    assert isinstance(p.max_loss, Decimal)
    max_loss_float = float(p.max_loss)
    assert math.isfinite(max_loss_float)
    assert max_loss_float > 0

    # The gate computes max_loss at structural_contracts=1 (the single short leg).
    # _MAX_LOSS_PER_CONTRACT = (width - net_credit) * 100 for 1 contract.
    # The proposal carries this 1-contract max_loss; the total position max_loss
    # is max_loss * outer_qty (contracts).  We verify the per-contract figure.
    assert max_loss_float == pytest.approx(_MAX_LOSS_PER_CONTRACT, rel=1e-3)

    # Bucket is DEFINED_RISK.
    assert result.bucket == StructureBucket.DEFINED_RISK

    # risk_gate_pass is True.
    assert p.risk_gate_pass is True


def test_bps1_net_debit_credit_is_negative_credit_received() -> None:
    """BPS-1 supplement: net_debit_credit is negative (credit received)."""
    chain = _make_chain()
    result = build_bull_put_spread_proposal(
        symbol="NVDA",
        asof=_ASOF,
        chain=chain,
        nav=_NAV,
        options_buying_power=_OBP,
        cfg=_CFG,
    )
    assert result.admitted is True
    # net_debit_credit < 0 means credit received (same sign convention as CC/CSP).
    assert result.proposal is not None
    assert float(result.proposal.net_debit_credit) < 0


# ---------------------------------------------------------------------------
# BPS-2: over-cap — gate REJECTS (fail-closed)
# ---------------------------------------------------------------------------


def test_bps2_over_cap_max_loss_rejected() -> None:
    """BPS-2: when max_loss > max_position_pct * nav, gate rejects (fail-closed)."""
    # Drive max_loss to exceed the cap.
    # max_position_pct=0.10; nav=1M -> cap=100k.
    # To exceed it: width=200, credit=0.01 -> max_loss ~(200-0.01)*100=19999/contract.
    # With a wide spread (K_short=400, K_long=200), max_loss >> 10k.
    # Actually, the gate admits based on nav * max_position_pct. Let's use a tiny nav.
    tiny_nav = 1000.0  # $1k nav; max_position_pct=10% -> cap=$100
    # Our spread max_loss = 850/contract; 850 > 100 -> REJECT.
    chain = _make_chain()
    result = build_bull_put_spread_proposal(
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


def test_bps2_gate_reject_is_fail_closed_no_proposal() -> None:
    """BPS-2 supplement: a rejected build returns proposal=None (never a passing proposal)."""
    chain = _make_chain()
    result = build_bull_put_spread_proposal(
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
# BPS-3: non-finite long-leg mid (NaN) is rejected pre-gate (fail-closed)
# ---------------------------------------------------------------------------


def test_bps3a_finite_mid_helper_coerces_nonfinite() -> None:
    """BPS-3a (unit): _finite_mid coerces non-finite/None to 0.0, passes a normal value."""
    assert _finite_mid(float("nan")) == 0.0
    assert _finite_mid(float("inf")) == 0.0
    assert _finite_mid(-float("inf")) == 0.0
    assert _finite_mid(None) == 0.0
    assert _finite_mid(1.50) == pytest.approx(1.50)


def _nan_mid_long_snap() -> OptionSnapshot:
    """A long-put snapshot whose MID is a float('nan') (bid=NaN -> mid=NaN), NOT None.

    This is the wave2-review fail-open path: a NaN float passes BOTH `mid is None`
    (False) and `mid <= 0` (NaN<=0 is False), so WITHOUT the math.isfinite guard it
    enters the candidate list, is selected as the long leg, and _finite_mid coerces its
    mid to 0.0 — treating the protective leg as FREE and understating max_loss.
    """
    nan = float("nan")
    return OptionSnapshot(
        symbol=_LONG_SYM, asof=_ASOF, fetched_at=_ASOF,
        bid=nan, ask=nan, last=None,  # bid=NaN -> mid=(NaN+NaN)/2 = NaN (a float, not None)
        volume=0, open_interest=0,
        greeks=OptionGreeksSnapshot(delta=-0.15, gamma=0.01, theta=0.04, vega=0.06, rho=-0.005),
        underlying_spot=_SPOT, risk_free_rate=0.05,
    )


def test_bps3b_nan_float_long_mid_is_filtered_not_treated_as_free() -> None:
    """BPS-3b (the REAL fail-open, wave2-review): a NaN-FLOAT long-leg mid must be
    FILTERED by _eligible_snapshots, NOT selected and coerced to 0.0. With the only
    long candidate carrying a NaN mid, the spread has no eligible protective leg and the
    producer must REJECT (RecipeBuildError) — it must NEVER build a spread whose long
    leg was treated as free protection (which understates max_loss -> over-sizes).

    RED before the fix: math.isfinite was absent from _eligible_snapshots, the NaN snap
    passed the filter, was selected, _finite_mid coerced its mid to 0.0, and the producer
    ADMITTED a spread with understated max_loss instead of rejecting.
    """
    import math as _math

    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=-0.25)
    long_nan = _nan_mid_long_snap()
    # Precondition: the long snap's mid really IS a NaN float (not None).
    assert long_nan.mid is not None and _math.isnan(long_nan.mid)

    chain = OptionChain(
        underlying="NVDA", asof=_ASOF, underlying_spot=_SPOT, risk_free_rate=0.05,
        snapshots=(short_snap, long_nan),
    )
    # The NaN long is filtered -> no eligible long-put strike -> REJECT (fail-closed).
    with pytest.raises(RecipeBuildError, match="no eligible long-put strike"):
        build_bull_put_spread_proposal(
            symbol="NVDA", asof=_ASOF, chain=chain,
            nav=_NAV, options_buying_power=_OBP, cfg=_CFG,
        )


# ---------------------------------------------------------------------------
# BPS-4 / BPS-5: structure_select flag gate
# ---------------------------------------------------------------------------


def test_bps4_select_returns_bull_put_spread_when_flag_on(monkeypatch) -> None:
    """BPS-4: select_structure returns 'bull_put_spread' for BULLISH +
    defined_risk_credit + MID/HIGH IV when HERMES_QUANT_VERTICAL_SPREADS=1."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")

    for regime in (IVRegime.MID, IVRegime.HIGH):
        result = select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result == "bull_put_spread", (
            f"expected 'bull_put_spread' for regime={regime}, got {result!r}"
        )


def test_bps5_select_abstains_when_flag_off(monkeypatch) -> None:
    """BPS-5 (flag-OFF byte-identical test): select_structure returns None for
    defined_risk_credit when HERMES_QUANT_VERTICAL_SPREADS is absent/off.
    This is the flag-OFF proof — proves the flag gate is real."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)
    assert vertical_spreads_enabled() is False

    for regime in (IVRegime.MID, IVRegime.HIGH, IVRegime.LOW):
        result = select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
            iv_regime=regime,
        )
        assert result is None, (
            f"flag OFF: expected None for defined_risk_credit+{regime}, got {result!r}"
        )


def test_bps5_flag_off_premium_capture_unaffected(monkeypatch) -> None:
    """BPS-5 supplement: the VERTICAL_SPREADS_FLAG does NOT affect PREMIUM_CAPTURE rows.
    CC/CSP/wheel remain selectable regardless of the flag."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.delenv(VERTICAL_SPREADS_FLAG, raising=False)

    # PREMIUM_CAPTURE + BULLISH + HIGH -> cash_secured_put (unaffected).
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.HIGH,
        )
        == "cash_secured_put"
    )

    # PREMIUM_CAPTURE + NEUTRAL + MID -> wheel (unaffected).
    assert (
        select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.MID,
        )
        == "wheel"
    )


def test_bps5_low_iv_defined_risk_credit_abstains_even_when_flag_on(monkeypatch) -> None:
    """BPS-5 supplement: LOW IV + defined_risk_credit abstains even when the flag is ON.
    The table has no LOW-IV rows for defined_risk_credit (thin premium -> no spread)."""
    from hermes_quant.agents.research_debate.schemas import StructureIntent

    monkeypatch.setenv(VERTICAL_SPREADS_FLAG, "1")

    result = select_structure(
        direction=Direction.BULLISH,
        structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
        iv_regime=IVRegime.LOW,
    )
    assert result is None, f"expected None for LOW IV, got {result!r}"


# ---------------------------------------------------------------------------
# BPS-6: RED-proof — without the long leg the gate classifies as NAKED (reject)
# ---------------------------------------------------------------------------


def test_bps6_red_proof_single_short_put_is_naked_no_long_leg(monkeypatch) -> None:
    """BPS-6 (RED-proof): a lone short put (no long protection) is NAKED and REJECTED.

    This proves that the long leg is load-bearing: removing it from the legs list
    would cause _classify_structure to return NAKED (the short put has neither
    covering shares NOR a wider long leg), and the gate would reject it as naked.
    Verifies the BPS-1 test was testing a real behavioral difference, not a no-op.
    """
    from hermes_quant.risk.options_gate import options_gate

    # Use small gamma/vega to avoid cap failures at multi-contract sizes.
    _small_greeks_short = OptionGreeksSnapshot(
        delta=-0.25, gamma=0.0001, theta=0.005, vega=0.001, rho=-0.001,
    )
    _small_greeks_long = OptionGreeksSnapshot(
        delta=-0.15, gamma=0.0001, theta=0.004, vega=0.0008, rho=-0.0005,
    )

    short_leg = OptionLeg(
        symbol=_SHORT_SYM,
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=_small_greeks_short,
    )
    # ONE-LEG: just the short put, no long protection. This is a naked short put.
    result_naked = options_gate(
        [short_leg],
        strategy_kind="cash_secured_put",   # even as CSP framing
        underlying="NVDA",
        spot=_SPOT,
        nav=_NAV,
        held_shares=0,
        options_buying_power=0.0,           # zero cash -> NOT cash-secured -> NAKED
        premium_received=_SHORT_MID * 100,
        portfolio_net_greeks=NetGreeks.zero(),
        total_bpr=0.0,
        cfg=_CFG,
        strike=_SHORT_STRIKE,
        min_dte=30,
    )
    assert result_naked.admitted is False
    assert result_naked.bucket == StructureBucket.NAKED

    # TWO-LEG (adding the long put): now DEFINED_RISK and can be admitted.
    long_leg = OptionLeg(
        symbol=_LONG_SYM,
        side="buy",
        position_intent="buy_to_open",
        greeks_at_decision=_small_greeks_long,
    )
    result_defined = options_gate(
        [short_leg, long_leg],
        strategy_kind="bull_put_spread",
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
# _pick_long_put unit tests
# ---------------------------------------------------------------------------


def test_pick_long_put_selects_highest_qualifying_strike() -> None:
    """_pick_long_put returns the highest-strike put below the short strike."""
    from hermes_quant.options.occ import parse_occ

    # Three candidate lower-strike puts on the same expiry.
    cands = [
        _make_snap("NVDA260717P00120000", mid_bid_ask=0.50, delta=-0.10),  # K=120
        _make_snap("NVDA260717P00125000", mid_bid_ask=0.70, delta=-0.12),  # K=125
        _make_snap("NVDA260717P00130000", mid_bid_ask=1.00, delta=-0.15),  # K=130
    ]
    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=-0.25)
    all_snaps = cands + [short_snap]

    chosen = _pick_long_put(all_snaps, short=short_snap)
    # Highest qualifying K below 140 = 130.
    assert float(parse_occ(chosen.symbol).strike) == pytest.approx(130.0)


def test_pick_long_put_raises_when_no_eligible_strike() -> None:
    """_pick_long_put raises RecipeBuildError when no lower-strike exists."""
    short_snap = _make_snap(_SHORT_SYM, mid_bid_ask=_SHORT_MID, delta=-0.25)
    # Only the short snap, no lower-strike candidates.
    with pytest.raises(RecipeBuildError, match="no eligible long-put strike"):
        _pick_long_put([short_snap], short=short_snap)


# ---------------------------------------------------------------------------
# vertical_spreads_enabled helper
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
