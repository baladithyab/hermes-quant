"""hermes_quant.risk.options_gate — options-aware deterministic risk gate (ADR-0027).

EXTENDS ADR-0004's rule sequence with O1-O7. COLLATERAL-SECURED, not
defined-risk-only (research §2 / Gemini catch: a strict max_gain-is-None reject
rejects every CC and CSP — the exact strategies the effort exists to enable).

Three admissible buckets; everything else is rejected-as-naked:
  - covered_call:     admit iff held_shares[underlying] >= 100 * contracts
  - cash_secured_put: admit iff options_buying_power >= strike*100*c - premium
  - defined_risk:     admit iff max_loss finite AND <= caps (vertical/condor/fly)

DEFAULT-OFF behind HERMES_QUANT_OPTIONS_GATE=1. When the flag is absent the
public entrypoint raises OptionsGateDisabled (this wave never sets it live).
The gate can ONLY reject (silence) or pass-through; it never amplifies, never
sizes up, never overrides the equity gate (gate.py stays the equity/crypto
authority).
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from hermes_quant.options.data import (
    NetGreeks,
    OptionLeg,
    StockLeg,
    aggregate_net_greeks,
)


class OptionsGateDisabled(RuntimeError):  # noqa: N818 — plan/ADR-0027-mandated name
    """Raised when options_gate() is called without HERMES_QUANT_OPTIONS_GATE=1."""


class StructureBucket(StrEnum):
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    DEFINED_RISK = "defined_risk"
    NAKED = "naked"  # the ONLY reject-as-naked terminal bucket


@dataclass(frozen=True)
class OptionsRiskConfig:
    """Frozen mirror of the `options_default` profile (ADR-0027 D2).

    Envelope keys (max_position_pct, max_drawdown_pct, max_daily_loss_pct,
    bpr_kill_switch_pct) are NOT overridable below their floor — same posture as
    gate.py. Defaults exactly as ADR-0027 D2 lists them.
    """

    # Inherited from ADR-0004 / ADR-0009 (envelope):
    max_position_pct: float = 0.10
    action_step: float = 0.05
    cost_multiple: float = 2.0
    max_drawdown_pct: float = 0.05
    max_daily_loss_pct: float = 0.005
    quarter_kelly: float = 0.25
    cooldown_after_loss_minutes: int = 60
    # Options-specific (ADR-0027 D2):
    max_short_call_delta_per_position: float = 0.30
    max_short_put_delta_per_position: float = 0.30
    max_net_delta_pct_nav: float = 0.50
    gamma_cap_pct_nav: float = 0.05
    vega_cap_pct_nav: float = 0.10
    theta_budget_pct_nav_per_day: float = 0.02
    bpr_buffer_pct_nav: float = 0.80
    bpr_kill_switch_pct: float = 0.95
    max_assignment_risk_pct_nav: float = 0.20
    min_dte_for_new_entry: int = 7
    pin_risk_dte_threshold: int = 3
    pin_risk_moneyness_threshold: float = 0.02
    max_strategies_per_underlying: int = 1
    max_concurrent_open_positions: int = 8


@dataclass(frozen=True)
class OptionsGateResult:
    """REJECT-only result. `admitted=False` => silence with `reason`.
    `admitted=True` carries the deterministic sizing the caller MAY use
    (contract count), but the gate never *raises* a size — contracts is a
    floor() of a discrete-step NAV target, identical posture to gate.py."""

    admitted: bool
    bucket: StructureBucket
    reason: str | None  # populated iff not admitted
    net_greeks: NetGreeks
    bpr_estimate: float  # buying-power reduction, USD
    max_loss: float | None  # USD; None only for covered (share-collateralized)
    contracts: int  # deterministic floor() sizing; 0 => silence
    warnings: tuple[str, ...] = field(default_factory=tuple)  # soft, non-silencing

    @classmethod
    def silence(
        cls,
        bucket: StructureBucket,
        reason: str,
        *,
        net_greeks: NetGreeks | None = None,
        bpr_estimate: float = 0.0,
        max_loss: float | None = None,
        warnings: tuple[str, ...] = (),
    ) -> OptionsGateResult:
        return cls(
            admitted=False,
            bucket=bucket,
            reason=reason,
            net_greeks=net_greeks or NetGreeks.zero(),
            bpr_estimate=bpr_estimate,
            max_loss=max_loss,
            contracts=0,
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Internal predicates (pure, fixture-tested, no LLM)
# ---------------------------------------------------------------------------


def _short_option_legs(legs: Sequence[OptionLeg | StockLeg]) -> list[OptionLeg]:
    return [
        leg for leg in legs if isinstance(leg, OptionLeg) and leg.side == "sell"
    ]


def _long_option_legs(legs: Sequence[OptionLeg | StockLeg]) -> list[OptionLeg]:
    return [leg for leg in legs if isinstance(leg, OptionLeg) and leg.side == "buy"]


def _classify_structure(
    legs: Sequence[OptionLeg | StockLeg],
    *,
    held_shares: int,
    options_buying_power: float,
    premium_received: float,
    strike: float,
    contracts: int,
) -> StructureBucket:
    """Three-bucket classifier per research §2.1. The ONLY NAKED case is a short
    leg with neither >=100 covering shares/contract, NOR strike*100 cash, NOR a
    wider long leg.
    """
    shorts = _short_option_legs(legs)
    longs = _long_option_legs(legs)

    # No short option leg at all => long-only/debit structure; defined-risk.
    if not shorts:
        return StructureBucket.DEFINED_RISK

    # A short option with a covering wider long leg (vertical / condor / fly) is
    # defined-risk. We treat "any long option leg present alongside the short(s)"
    # as the spread case (the max-loss caller validates the width/credit math).
    if longs:
        return StructureBucket.DEFINED_RISK

    # Lone short leg(s). Distinguish covered call vs cash-secured put vs naked.
    # Covered call: every short is a call AND held_shares covers it.
    all_calls = all(leg.right == "C" for leg in shorts)
    all_puts = all(leg.right == "P" for leg in shorts)

    if all_calls:
        if held_shares >= _shares_needed(contracts):
            return StructureBucket.COVERED_CALL
        return StructureBucket.NAKED

    if all_puts:
        required = strike * 100 * contracts - premium_received
        if options_buying_power >= required:
            return StructureBucket.CASH_SECURED_PUT
        return StructureBucket.NAKED

    # Mixed lone shorts (e.g. short call + short put, no cover) => naked strangle.
    return StructureBucket.NAKED


def _shares_needed(contracts: int) -> int:
    return 100 * contracts


def _max_loss(
    bucket: StructureBucket,
    *,
    width: float,
    net_credit: float,
    net_debit: float,
    contracts: int,
) -> float | None:
    """Closed-form max loss per bucket (ADR-0027 D3 / research §2.1).

    debit vertical: net_debit*100*c ; credit vertical/condor/fly:
    (width - net_credit)*100*c ; covered_call: None (share-collateralized) ;
    cash_secured_put: strike-based, computed by the caller as collateral.
    """
    c = max(contracts, 1)
    if bucket == StructureBucket.COVERED_CALL:
        return None  # share-collateralized; "loss" is stock basis, not options
    if bucket == StructureBucket.DEFINED_RISK:
        if net_debit > 0:
            return net_debit * 100 * c
        return (width - net_credit) * 100 * c
    if bucket == StructureBucket.CASH_SECURED_PUT:
        # Max loss is strike*100 - premium per contract (down to 0 underlying).
        return None  # collateral-bound; the BPR formula is the binding number
    return None


def _bpr(
    bucket: StructureBucket,
    *,
    strike: float,
    contracts: int,
    premium_received: float,
    premium_paid: float,
    width: float,
    net_credit: float,
) -> float:
    """Buying-power reduction per structure (research §2.2).

    CSP: strike*100*c - premium_received ; CC: 0 incremental ;
    credit vertical/condor: (width - net_credit)*100*c ; long/debit: premium_paid.
    """
    c = max(contracts, 1)
    if bucket == StructureBucket.COVERED_CALL:
        return 0.0  # the 100 shares are already the collateral
    if bucket == StructureBucket.CASH_SECURED_PUT:
        return max(strike * 100 * c - premium_received, 0.0)
    if bucket == StructureBucket.DEFINED_RISK:
        if net_credit > 0:
            return max((width - net_credit) * 100 * c, 0.0)
        return premium_paid  # debit structure: max loss == premium paid
    return 0.0


def _size_contracts(
    bucket: StructureBucket,
    *,
    target_nav: float,
    max_loss_per_contract: float | None,
    collateral_per_contract: float | None,
    held_shares: int,
    composite_intent: str | None,
) -> int:
    """Deterministic floor() contract sizing (ADR-0027 D3 + amendment).

    CC initiation uses basis_per_share*100 denominator; CC overlay
    (composite_intent='wheel') sizes against held_shares//100. All integer
    floor(). The gate NEVER sizes up — contracts is a floor of target_nav over
    the per-contract capital-at-risk.
    """
    target = abs(target_nav)

    if bucket == StructureBucket.COVERED_CALL:
        if composite_intent == "wheel":
            # Overlay on already-held shares: no new capital deployed; only the
            # truly-covered constraint binds.
            return held_shares // 100
        # Initiation: collateral_per_contract = basis_per_share * 100.
        if not collateral_per_contract or collateral_per_contract <= 0:
            return 0
        return math.floor(target / collateral_per_contract)

    if bucket == StructureBucket.CASH_SECURED_PUT:
        if not collateral_per_contract or collateral_per_contract <= 0:
            return 0
        return math.floor(target / collateral_per_contract)

    if bucket == StructureBucket.DEFINED_RISK:
        if not max_loss_per_contract or max_loss_per_contract <= 0:
            return 0
        return math.floor(target / max_loss_per_contract)

    return 0


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def options_gate(
    legs: Sequence[OptionLeg | StockLeg],
    *,
    strategy_kind: str,
    underlying: str,
    spot: float,
    nav: float,
    held_shares: int,
    options_buying_power: float,
    premium_received: float,
    portfolio_net_greeks: NetGreeks,
    total_bpr: float,
    cfg: OptionsRiskConfig,
    composite_intent: str | None = None,
    # Auxiliary structure inputs (caller-supplied; defined-risk math).
    strike: float = 0.0,
    width: float = 0.0,
    net_credit: float = 0.0,
    net_debit: float = 0.0,
    premium_paid: float = 0.0,
    basis_per_share: float | None = None,
    min_dte: int | None = None,
    open_strategies_on_underlying: int = 0,
    edge: float = 0.0,
) -> OptionsGateResult:
    """Run the O1-O7 sequence. Returns silence (admitted=False) on any
    violation. Rules in order: O-classify -> O1 max-loss/margin -> O2 no-naked
    -> O3 gamma -> O4 theta -> O5 vega -> O6 BPR buffer -> O7 pin-risk ->
    sizing -> min-contract guard.

    Raises OptionsGateDisabled unless HERMES_QUANT_OPTIONS_GATE=1.
    """
    if os.environ.get("HERMES_QUANT_OPTIONS_GATE", "0") != "1":
        raise OptionsGateDisabled(
            "options gate is default-OFF; set HERMES_QUANT_OPTIONS_GATE=1 to "
            "enable (this wave never sets it live)"
        )

    # Aggregate candidate net greeks first (fail-closed on missing greeks).
    # This MUST come before any per-position check (ADR-0027 D6).
    candidate_net = aggregate_net_greeks(legs)
    portfolio_after = portfolio_net_greeks + candidate_net

    # ---- O-classify (D7 composite-intent feeds the classifier sizing). ----
    # Provisional contract count for the share-coverage classification: use the
    # short-call ratio_qty sum as the structural contract count, before sizing.
    shorts = _short_option_legs(legs)
    structural_contracts = sum(leg.ratio_qty for leg in shorts) or 1

    # D7 wheel: refuse a third leg (CC + CSP already open -> any new leg).
    if composite_intent == "wheel" and open_strategies_on_underlying >= 2:
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            "wheel_double_up_blocked",
            net_greeks=candidate_net,
        )
    # max_strategies_per_underlying envelope (one CC OR one CSP, not both).
    if (
        open_strategies_on_underlying >= cfg.max_strategies_per_underlying
        and composite_intent != "wheel"
    ):
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            "max_strategies_per_underlying",
            net_greeks=candidate_net,
        )

    bucket = _classify_structure(
        legs,
        held_shares=held_shares,
        options_buying_power=options_buying_power,
        premium_received=premium_received,
        strike=strike,
        contracts=structural_contracts,
    )

    # ---- O2: no-naked (v0.5.0). The classifier surfaces NAKED for any short ---
    # leg without cover/cash/wider-long.
    if bucket == StructureBucket.NAKED:
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            _naked_reason(legs, held_shares, structural_contracts),
            net_greeks=candidate_net,
        )

    max_loss = _max_loss(
        bucket,
        width=width,
        net_credit=net_credit,
        net_debit=net_debit,
        contracts=structural_contracts,
    )
    bpr = _bpr(
        bucket,
        strike=strike,
        contracts=structural_contracts,
        premium_received=premium_received,
        premium_paid=premium_paid,
        width=width,
        net_credit=net_credit,
    )

    # ---- O1: max-loss / margin validation. ----
    # Defined-risk structures must have a finite max-loss within the position
    # envelope. Covered/CSP are collateral-secured (max_loss None by design).
    if bucket == StructureBucket.DEFINED_RISK:
        if max_loss is None or not math.isfinite(max_loss):
            return OptionsGateResult.silence(
                bucket, "max_loss_not_finite", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        if max_loss > cfg.max_position_pct * nav:
            return OptionsGateResult.silence(
                bucket, "max_loss_exceeds_position_cap", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )

    # Short-leg delta caps (ADR-0027 D4 per-strategy hard rule).
    delta_reason = _short_delta_violation(legs, cfg)
    if delta_reason is not None:
        return OptionsGateResult.silence(
            bucket, delta_reason, net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    # ---- O3: portfolio gamma cap (portfolio-level). ----
    gamma_dollar = abs(portfolio_after.gamma * spot * spot)
    if gamma_dollar > cfg.gamma_cap_pct_nav * nav:
        return OptionsGateResult.silence(
            bucket, "portfolio_gamma_cap", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    # ---- O4: theta budget. Silence ONLY for a theta-burning entry. ----
    # Convention: net theta stored as aggregated per-day number. A theta-burning
    # entry has net theta < 0 (paying decay); theta-collecting (CC/CSP/credit)
    # passes O4.
    if candidate_net.theta < 0:
        theta_burn = abs(candidate_net.theta)
        if theta_burn > cfg.theta_budget_pct_nav_per_day * nav:
            return OptionsGateResult.silence(
                bucket, "theta_budget", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )

    # ---- O5: vega cap (portfolio-level; |net vega * 1pt| <= cap). ----
    if abs(portfolio_after.vega) > cfg.vega_cap_pct_nav * nav / 100.0:
        return OptionsGateResult.silence(
            bucket, "portfolio_vega_cap", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    # ---- Net-delta cap (|net_delta * spot| <= max_net_delta_pct_nav * NAV). ----
    if abs(portfolio_after.delta * spot) > cfg.max_net_delta_pct_nav * nav:
        return OptionsGateResult.silence(
            bucket, "net_delta_cap", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    # ---- O6: BPR buffer. ----
    if total_bpr + bpr > cfg.bpr_buffer_pct_nav * nav:
        return OptionsGateResult.silence(
            bucket, "bpr_buffer", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    # ---- O7: pin-risk filter (min DTE <= threshold AND |moneyness| <= thr). ----
    if min_dte is not None and strike > 0 and spot > 0:
        moneyness = abs(spot - strike) / spot
        if (
            min_dte <= cfg.pin_risk_dte_threshold
            and moneyness <= cfg.pin_risk_moneyness_threshold
        ):
            return OptionsGateResult.silence(
                bucket, "pin_risk", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        # min_dte_for_new_entry envelope (never open new <7 DTE).
        if min_dte < cfg.min_dte_for_new_entry:
            return OptionsGateResult.silence(
                bucket, "below_min_dte_for_new_entry", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )

    # ---- Sizing (D3 + amendment). Contract count is floor() of a discrete-step ---
    # NAV target. Per ADR-0027 D3 amendment, the income/collateral sizing target
    # is `nav * kelly_fraction * max_position_pct` (the x100 lives in the
    # per-contract denominator, NOT the numerator). `kelly_fraction` defaults to
    # the profile's quarter_kelly unless the edge implies a smaller fraction.
    kelly_fraction = cfg.quarter_kelly
    if edge > 0:
        # An explicit edge can only TIGHTEN the fraction, never widen it (the
        # gate never sizes up): take the smaller of edge and quarter_kelly.
        kelly_fraction = min(cfg.quarter_kelly, abs(edge))
    kelly_target = nav * kelly_fraction * cfg.max_position_pct
    # The discrete action_step applies to the NAV target before contract-count.
    target_nav = _round_to_step(kelly_target, cfg.action_step * nav)
    if target_nav <= 0:
        # action_step flooring drove the target below one step; preserve the
        # un-stepped kelly_target so small accounts still size deterministically
        # (the floor() in _size_contracts is the hard cap, never an amplifier).
        target_nav = kelly_target

    collateral_per_contract: float | None = None
    if bucket == StructureBucket.COVERED_CALL and basis_per_share:
        collateral_per_contract = basis_per_share * 100
    elif bucket == StructureBucket.CASH_SECURED_PUT:
        collateral_per_contract = max(strike * 100 - premium_received / max(
            structural_contracts, 1
        ), 0.0) or strike * 100

    contracts = _size_contracts(
        bucket,
        target_nav=target_nav,
        max_loss_per_contract=(
            None if max_loss is None else max_loss / max(structural_contracts, 1)
        ),
        collateral_per_contract=collateral_per_contract,
        held_shares=held_shares,
        composite_intent=composite_intent,
    )

    # ---- Min-contract guard. ----
    if contracts < 1:
        return OptionsGateResult.silence(
            bucket, "min_trade_size", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )

    return OptionsGateResult(
        admitted=True,
        bucket=bucket,
        reason=None,
        net_greeks=candidate_net,
        bpr_estimate=bpr,
        max_loss=max_loss,
        contracts=contracts,
        warnings=(),
    )


def _naked_reason(
    legs: Sequence[OptionLeg | StockLeg], held_shares: int, contracts: int
) -> str:
    shorts = _short_option_legs(legs)
    if shorts and all(leg.right == "C" for leg in shorts):
        return (
            f"naked_short_call: need >={_shares_needed(contracts)} covering shares, "
            f"have {held_shares}"
        )
    if shorts and all(leg.right == "P" for leg in shorts):
        return "naked_short_put: insufficient cash collateral / options buying power"
    return "naked_uncovered_short"


def _short_delta_violation(
    legs: Sequence[OptionLeg | StockLeg], cfg: OptionsRiskConfig
) -> str | None:
    for leg in _short_option_legs(legs):
        g = leg.greeks_at_decision
        if g is None or g.delta is None:
            continue
        mag = abs(g.delta)
        if leg.right == "C" and mag > cfg.max_short_call_delta_per_position:
            return "short_call_delta_exceeds_cap"
        if leg.right == "P" and mag > cfg.max_short_put_delta_per_position:
            return "short_put_delta_exceeds_cap"
    return None
