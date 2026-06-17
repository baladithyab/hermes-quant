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

import logging
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from hermes_quant.options.data import (
    GreekComputationError,
    NetGreeks,
    OptionLeg,
    StockLeg,
    aggregate_net_greeks,
)
from hermes_quant.options.occ import OccParseError

logger = logging.getLogger(__name__)


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
    # ADR-0084 O8 (earnings-proximity / IV-crush guard). ADDITIVE, default-OFF
    # at the call seam (event_risk=None => no check). Only the net-theta-paying /
    # net-vega-long side (long premium) is ever flagged; premium sellers HARVEST
    # the crush and are exempt.
    earnings_proximity_dte: int = 5
    """Days BEFORE a scheduled earnings date within which opening a long-premium
    structure is rejected (IV-crush risk). Default 5 matches the existing
    covered_call days_since_earnings>=5 convention (ADR-0084 §Consequences)."""


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

    # A short option is DEFINED_RISK only if a long leg actually COVERS it:
    # same underlying, same (covering) right, and expiry >= the short's expiry.
    # A same-right long with expiry no earlier than the short caps the otherwise
    # unbounded tail (the long offsets 1:1 past both strikes); the caller's
    # width/credit math validates the max-loss MAGNITUDE within the envelope.
    # Anything that does not cover EVERY short (e.g. a naked short call paired
    # with an unrelated long put) is NAKED — never silently DEFINED_RISK.
    if longs and all(_is_covered_short(short, longs) for short in shorts):
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
        # ADR-0027 D4: cash-secured requires the FULL assignment cash reserved
        # (strike*100*contracts). The premium received does NOT reduce the
        # collateral requirement — on assignment the operator must buy the
        # shares outright at the strike. Netting the premium would admit an
        # under-collateralized (effectively naked) short put.
        required = strike * 100 * contracts
        if options_buying_power >= required:
            return StructureBucket.CASH_SECURED_PUT
        return StructureBucket.NAKED

    # Mixed lone shorts (e.g. short call + short put, no cover) => naked strangle.
    return StructureBucket.NAKED


def _is_covered_short(short: OptionLeg, longs: Sequence[OptionLeg]) -> bool:
    """True iff some long leg actually covers `short` (caps its unbounded tail).

    Covering requires: same underlying, same right (a long call caps a short
    call's upside; a long put caps a short put's downside), and the long's
    expiry >= the short's expiry (an earlier-expiring long leaves the short
    naked after the long expires). The loss-MAGNITUDE (strike width / credit) is
    validated separately by the max-loss caller; here we only decide naked vs
    defined-risk, so a same-right same-underlying long-dated long is sufficient
    to cap the tail. Fail-closed: any unparseable OCC field => not covered.
    """
    try:
        s_under = short.underlying
        s_right = short.right
        s_expiry = short.expiry
    except OccParseError:
        return False
    for lng in longs:
        try:
            if (
                lng.underlying == s_under
                and lng.right == s_right
                and lng.expiry >= s_expiry
            ):
                return True
        except OccParseError:
            continue
    return False


def _shares_needed(contracts: int) -> int:
    return 100 * contracts


def _scale_cover_to_lots(
    legs: Sequence[OptionLeg | StockLeg], *, lots: int
) -> list[OptionLeg | StockLeg]:
    """Rebuild every ``StockLeg`` cover to the ``lots``-lot footprint (100*lots shares)
    so a net-greeks aggregation evaluates the structure the order actually establishes.

    ``aggregate_net_greeks`` scales OPTION legs by ``order_qty`` (units = sign *
    ratio_qty * order_qty * 100) but treats ``StockLeg.qty`` as an ALREADY-scaled
    absolute share count (NOT scaled by order_qty — data.py). The covered-call recipe
    builds a ONE-lot cover (``StockLeg(qty=100)``) and hands the gate that 1-lot stock.
    Aggregating a 1-lot cover against an N-lot option footprint understates the
    directional exposure (monotonically in N) and can FLIP the net-delta sign for a
    ratio structure, ADMITTING an over-the-cap covered call (fail-OPEN). We rebuild the
    cover to ``100 * lots`` (the same share count ``_shares_needed`` requires of the
    classifier) at every gate aggregation so the net-delta / gamma / vega caps and the
    reported ``net_greeks`` evaluate the true established position. Scaling here (the
    gate's known-1-lot recipe seam) rather than inside ``aggregate_net_greeks`` avoids
    double-scaling a caller that already passes a pre-scaled cover.
    """
    cover_shares = 100 * max(int(lots), 1)
    return [
        replace(leg, qty=cover_shares) if isinstance(leg, StockLeg) else leg
        for leg in legs
    ]


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
            # Overlay on already-held shares. The held-shares lot count is one
            # binding constraint, but it can NEVER be the only one: the gate
            # never sizes up, so the NAV sizing target (target_nav over the
            # per-contract collateral basis = basis_per_share*100) is an
            # additional ceiling. We take the MIN of the two so the wheel can
            # only subtract relative to the NAV target, never widen exposure
            # beyond max_position_pct / action_step (ADR-0027 D3).
            by_shares = held_shares // 100
            if not collateral_per_contract or collateral_per_contract <= 0:
                # No collateral basis to bound the NAV target -> fail-closed to
                # the share cap alone would WIDEN; size 0 instead (silence).
                return 0
            by_nav = math.floor(target / collateral_per_contract)
            return min(by_shares, by_nav)
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


def _event_risk_enabled() -> bool:
    """True iff HERMES_QUANT_EVENT_RISK=1 (ADR-0084 O8 master flag). Read at
    call time (os.environ.get; never cached at import)."""
    return os.environ.get("HERMES_QUANT_EVENT_RISK", "0") == "1"


def _parse_event_ts(s) -> datetime | None:
    """Coerce an event timestamp (ISO string or datetime) to tz-aware UTC, or
    None on any failure. Missing/malformed => None => NO blackout (ADR-0084
    Negative: never fabricate an earnings date). Pure; never raises."""
    try:
        if isinstance(s, datetime):
            dt = s
        elif isinstance(s, str):
            v = s.strip()
            if not v:
                return None
            dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
        else:
            return None
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _earnings_proximity_violation(
    event_risk: Mapping | None,
    candidate_net: NetGreeks,
    *,
    underlying: str,
    asof: datetime | None,
    dte_window: int,
) -> str | None:
    """O8: flag a LONG-PREMIUM structure opening into an imminent earnings date.

    The IV-crush trap (research §earnings-IV-crush literature, ADR-0084) is on
    the NET-THETA-PAYING / NET-VEGA-LONG side: a long-premium structure spanning
    earnings pays decay and is long vega, so the post-print IV collapse erodes
    its value. Premium SELLERS (net theta >= 0, collecting decay) HARVEST the
    crush and MUST NOT be blocked — so this predicate is a no-op for them.

    Returns a reject reason string iff ALL hold:
      * the structure is long-premium (net theta < 0 AND net vega > 0), and
      * ``event_risk`` carries a HIGH-impact ``earnings`` event for ``underlying``
        whose ``scheduled_for`` is FORWARD of ``asof`` and within ``dte_window``
        days (imminent print).

    asof-honest: ``event_risk`` was filtered upstream to
    ``announced_at <= decision_asof`` (the earnings date's EXISTENCE was knowable
    at decision time); this only tests the forward ``scheduled_for``. Missing
    data => None => NO reject (never fabricate a blackout).

    CLOCK-FREE (mirrors ``risk.gate.in_event_blackout``): this predicate reads NO
    wall clock. A ``None`` ``asof`` cannot anchor the forward window, so it
    returns None (never fabricate a blackout, never silently substitute
    ``now()`` — a now() fallback in a past-dated replay would measure the forward
    earnings window against the future, systematically MISSING imminent-earnings
    rejects). The gate seam is responsible for FAIL-CLOSED rejecting a supplied
    ``event_risk`` payload that arrives without a ``decision_asof``. Pure; never
    raises.
    """
    if event_risk is None or not isinstance(event_risk, Mapping):
        return None
    if asof is None:
        return None  # no anchor => no forward window; never clock, never fabricate
    # Only long-premium (net theta-paying AND net vega-long) is at IV-crush risk.
    # A theta-collecting OR vega-short structure (CC/CSP/credit spread) is exempt.
    if not (candidate_net.theta < 0 and candidate_net.vega > 0):
        return None
    events = event_risk.get("events")
    if not events:
        return None
    when = asof
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    horizon = when + timedelta(days=dte_window)
    sym_u = (underlying or "").strip().upper()
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        if str(ev.get("kind") or "").strip().lower() != "earnings":
            continue
        if str(ev.get("impact") or "").strip().lower() != "high":
            continue
        # Single-name scope: an earnings event applies only to its own symbol.
        # If the payload carries a symbol, it must match the structure's
        # underlying; a symbol-less earnings row is conservatively in-scope.
        ev_sym = ev.get("symbol")
        if ev_sym is not None and str(ev_sym).strip().upper() != sym_u:
            continue
        scheduled = _parse_event_ts(ev.get("scheduled_for"))
        if scheduled is None or scheduled < when:
            continue  # missing/past => not a forward IV-crush risk
        if scheduled <= horizon:
            return "earnings_proximity_iv_crush"
    return None


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
    # ADR-0027 D2/D4 cumulative assignment-cash cap. The sum of ALREADY-reserved
    # CSP cash collateral (strike*100*contracts) + open CC stock basis for this
    # account, EXCLUDING this candidate. Default 0.0 => no prior reservations =>
    # the only binding term is this structure's own incremental cash, so a single
    # within-budget CSP/CC is byte-identical to today. The cap bounds the
    # all-CSPs-assign tail (ADR-0027 D4 wheel HARD rule: "Sum of CSP cash
    # reservations + CC stock basis <= max_assignment_risk_pct_nav").
    open_assignment_cash: float = 0.0,
    # ADR-0084 O8 (earnings-proximity / IV-crush). ADDITIVE + DEFAULT-OFF at the
    # seam: event_risk=None => O8 never runs => byte-identical to today. The
    # check ALSO requires HERMES_QUANT_EVENT_RISK=1 (master flag), so even a
    # caller that passes a payload is a no-op when the feature is OFF.
    event_risk: Mapping | None = None,
    decision_asof: datetime | None = None,
) -> OptionsGateResult:
    """Run the O1-O8 sequence. Returns silence (admitted=False) on any
    violation. Rules in order: O-classify -> O1 max-loss/margin -> O2 no-naked
    -> O3 gamma -> O4 theta -> O5 vega -> O6 BPR buffer -> O7 pin-risk ->
    O8 earnings-proximity (long-premium IV-crush) -> sizing -> min-contract
    guard -> size-scaling re-checks -> cumulative assignment-cash cap (ADR-0027
    D2/D4: open_assignment_cash + this structure's incremental CSP/CC cash
    <= max_assignment_risk_pct_nav * nav).

    Raises OptionsGateDisabled unless HERMES_QUANT_OPTIONS_GATE=1.
    """
    if os.environ.get("HERMES_QUANT_OPTIONS_GATE", "0") != "1":
        raise OptionsGateDisabled(
            "options gate is default-OFF; set HERMES_QUANT_OPTIONS_GATE=1 to "
            "enable (this wave never sets it live)"
        )

    # ---- Non-finite market-input guard (cr02 fail-closed; cs06 extension). ----
    # Every spot-/nav-scaled cap below is `value > cfg.x * nav` or
    # `abs(... * spot) > ...`. A NaN spot or nav makes the comparison always
    # False (`NaN > x` is False) — the canonical NaN-fail-open class — and a NaN
    # also propagates into _round_to_step() as an unhandled ValueError. The
    # equity gate already fails closed on non-finite inputs (gate.py:536); mirror
    # that here: a NaN/inf spot or nav silences deterministically (reject), never
    # admits and never aborts the tick.
    #
    # cs06 (cr02 P2 follow-up): the same NaN-fail-open class also lives in the
    # BPR/collateral inputs that cr02 left unguarded.
    #   * total_bpr — O6 is `if total_bpr + bpr > cfg.bpr_buffer_pct_nav * nav`.
    #     A NaN total_bpr makes the LHS NaN and `NaN > x` False => the BPR buffer
    #     ADMITS (fail-open) an under-validated structure.
    #   * options_buying_power — the CSP cash-collateral gate is
    #     `options_buying_power >= required`; a NaN drives that comparison False.
    #   * premium_received — feeds the CSP BPR math (`strike*100*c -
    #     premium_received`), so a NaN premium poisons the same O6 buffer compare.
    #     (recipes.py builds this as `float(short.mid or 0.0) * 100`; `or 0.0`
    #     does NOT catch a NaN mid, so a NaN can reach the gate.)
    # All are caller-supplied money-state inputs we cannot validate; a non-finite
    # value silences deterministically (reject), same posture as cr02.
    if (
        not math.isfinite(spot)
        or not math.isfinite(nav)
        or not math.isfinite(total_bpr)
        or not math.isfinite(options_buying_power)
        or not math.isfinite(premium_received)
    ):
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            "nonfinite_market_input",
        )

    # ---- Finite-guard the cumulative-assignment-cash money input (ar88). ----
    # A NaN/inf open_assignment_cash would defeat the `>` cap comparison below
    # (nan > x is always False), silently admitting a structure that breaches the
    # all-CSPs-assign cap — the recurring NaN-defeats-gate fail-open family. Kept
    # as a SEPARATE guard with its own reason (distinct from the market-input
    # guard above) so the diagnostic pinpoints the assignment-cash input. Fail
    # CLOSED (silence), mirroring the math.isfinite guards elsewhere in the gate.
    if not math.isfinite(open_assignment_cash):
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            "open_assignment_cash_not_finite",
        )

    # ---- O-classify (D7 composite-intent feeds the classifier sizing). ----
    # Provisional contract count for the share-coverage classification: use the
    # short-call ratio_qty sum as the structural contract count, before sizing.
    shorts = _short_option_legs(legs)
    structural_contracts = sum(leg.ratio_qty for leg in shorts) or 1

    # Aggregate candidate net greeks first (fail-closed on missing greeks).
    # This MUST come before any per-position check (ADR-0027 D6). Scale by the
    # proposed order quantity (structural_contracts) so a multi-contract order is
    # checked against its REAL greek footprint, not a single-lot footprint — a
    # per-lot aggregate would let a multi-lot order slip past O3/O5/net-delta.
    # A leg missing greeks raises GreekComputationError; we convert that to a
    # deterministic silence (reject) rather than aborting the tick (ADR-0027 D6).
    #
    # Scale the covered-structure StockLeg cover to the structural lot count too: a
    # ratio_qty>1 short call has structural_contracts>1, and aggregating the recipe's
    # 1-lot cover (StockLeg(qty=100)) against the N-lot option footprint understates
    # (and can sign-flip) the net delta — fail-OPEN on the net-delta cap AND a wrong
    # reported net_greeks. _scale_cover_to_lots rebuilds the cover to the
    # 100*structural_contracts shares the classifier already requires (_shares_needed).
    legs_at_structural = _scale_cover_to_lots(legs, lots=structural_contracts)
    try:
        candidate_net = aggregate_net_greeks(
            legs_at_structural, order_qty=structural_contracts
        )
    except GreekComputationError as exc:
        # GENUINELY missing/incomplete greeks => deterministic silence (reject),
        # the intended fail-closed path (ADR-0027 D6).
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            f"greeks_unavailable: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed but DON'T mislabel
        # Any OTHER exception (e.g. an unsupported leg type raising TypeError, or
        # an arithmetic/coding bug) is NOT "missing greeks". Masking it as
        # `greeks_unavailable` would hide a real defect behind the silence rail.
        # Still fail closed (silence, never admit), but LOG it with a distinct
        # reason so the bug surfaces instead of being silently swallowed.
        logger.error(
            "options_gate: unexpected error aggregating candidate greeks "
            "(underlying=%s): %r",
            underlying,
            exc,
        )
        return OptionsGateResult.silence(
            StructureBucket.NAKED,
            f"greeks_aggregation_error: {type(exc).__name__}",
        )
    portfolio_after = portfolio_net_greeks + candidate_net

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

    # ---- O7: pin-risk filter + min-DTE hard envelope. ----
    # FAIL-CLOSED: an unknown DTE on a new entry is a trade-affecting input we
    # cannot verify; we MUST reject rather than skip both checks (the old
    # `if min_dte is not None` nesting silently bypassed pin-risk AND the
    # min_dte_for_new_entry envelope when DTE was unknown — fail-OPEN).
    if min_dte is None:
        return OptionsGateResult.silence(
            bucket, "dte_unknown_for_new_entry", net_greeks=candidate_net,
            bpr_estimate=bpr, max_loss=max_loss,
        )
    # Pin-risk filter (min DTE <= threshold AND |moneyness| <= thr). Checked
    # before the min-DTE envelope to preserve the more-specific pin-risk reason.
    if strike > 0 and spot > 0:
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

    # ---- O8: earnings-proximity / IV-crush guard (ADR-0084, DEFAULT-OFF). ----
    # A LONG-PREMIUM structure (net theta-paying AND net vega-long) opening into
    # an imminent HIGH-impact earnings date for this underlying is rejected — the
    # post-print IV collapse erodes long premium. Premium sellers (theta-
    # collecting / vega-short) HARVEST the crush and are EXEMPT (the predicate is
    # a no-op for them). asof-honest: event_risk was filtered upstream to
    # announced_at<=decision_asof; this only tests the forward scheduled_for.
    # RAILS: this can ONLY reject — it never sizes, never amplifies. Fully gated
    # on HERMES_QUANT_EVENT_RISK=1 AND a non-None event_risk payload; otherwise a
    # no-op (byte-identical).
    if event_risk is not None and _event_risk_enabled():
        # FAIL-CLOSED asof guard: a caller that supplies event_risk MUST also
        # supply decision_asof. Without it the forward earnings window has no
        # anchor; silently defaulting to wall-clock now() (the pre-fix behavior)
        # would, in a past-dated replay/backtest, measure the forward window
        # against the future and systematically MISS imminent-earnings rejects
        # (under-block). The equity path (gate.in_event_blackout) already REQUIRES
        # asof; mirror that here. Reject (silence) rather than measure-against-now.
        if decision_asof is None:
            return OptionsGateResult.silence(
                bucket, "event_risk_requires_decision_asof", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        o8_reason = _earnings_proximity_violation(
            event_risk,
            candidate_net,
            underlying=underlying,
            asof=decision_asof,
            dte_window=cfg.earnings_proximity_dte,
        )
        if o8_reason is not None:
            return OptionsGateResult.silence(
                bucket, o8_reason, net_greeks=candidate_net,
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
        # FULL assignment cash per contract = strike*100. This MUST match the
        # classifier's collateral requirement (_classify_structure admits a CSP
        # only when options_buying_power >= strike*100*contracts, premium NOT
        # netted — on assignment the operator buys the shares outright at the
        # strike). Netting the premium here (the pre-fix `strike*100 -
        # premium/contracts`) would use a SMALLER denominator than the cash the
        # classifier actually requires, sizing UP one extra contract relative to
        # the reserved collateral. A larger (full) denominator can only size the
        # same or fewer contracts — never widen (ADR-0027 D3/D4; the gate never
        # sizes up).
        collateral_per_contract = strike * 100

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

    # ---- O3/O4/O5/net-delta RE-CHECK at the ADMITTED contract count. ----
    # The earlier cap checks ran against `structural_contracts` (the short-leg
    # ratio sum = 1 for every covered_call / cash_secured_put), but `_size_contracts`
    # routinely admits MORE lots (e.g. 2). The greek footprint scales with the
    # admitted size, so a 2-lot CC whose 1-lot gamma is under the cap can breach it
    # at 2 lots. A gate that admits a cap-breaching order violates the rail
    # "deterministic gate is FINAL authority; gates can only REJECT". So we
    # re-aggregate at the admitted `contracts` and re-run every size-scaling cap;
    # any breach silences. The admitted-size aggregate is the authoritative
    # net_greeks reported. (Fail-closed on missing greeks, as above.)
    #
    # ar106: scale the covered-structure StockLeg cover to the ADMITTED lot count.
    # `aggregate_net_greeks(legs, order_qty=contracts)` scales OPTION legs by
    # `contracts` (data.py:295) but treats StockLeg.qty as an ALREADY-scaled absolute
    # share count (data.py:305 — NOT scaled by order_qty). The CC recipe builds a
    # 1-lot cover (StockLeg(qty=100), recipes.py:261) and only rebuilds it to
    # qty=100*contracts AFTER the gate returns. Aggregating the 1-lot stock against
    # the N-lot option would evaluate the net-delta cap against |100 - 30N| instead
    # of the TRUE |100N - 30N| — understating exposure (monotonically in N) and
    # ADMITTING an over-the-cap covered call (fail-OPEN). _scale_cover_to_lots rebuilds
    # every StockLeg to its admitted cover; same fail-open closed at the first-pass
    # aggregation above, here at the admitted size. (Scaling at the gate's known-1-lot
    # recipe seam, not inside aggregate_net_greeks, avoids double-scaling a pre-scaled
    # caller — which the existing tests and the post-gate recipe path both pass.)
    if contracts != structural_contracts:
        legs_at_admitted = _scale_cover_to_lots(legs, lots=contracts)
        try:
            admitted_net = aggregate_net_greeks(legs_at_admitted, order_qty=contracts)
        except GreekComputationError as exc:
            return OptionsGateResult.silence(
                bucket, f"greeks_unavailable: {exc}", bpr_estimate=bpr, max_loss=max_loss,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed but DON'T mislabel
            logger.error(
                "options_gate: unexpected error aggregating admitted-size greeks "
                "(underlying=%s, contracts=%d): %r",
                underlying,
                contracts,
                exc,
            )
            return OptionsGateResult.silence(
                bucket,
                f"greeks_aggregation_error: {type(exc).__name__}",
                bpr_estimate=bpr,
                max_loss=max_loss,
            )
        portfolio_admitted = portfolio_net_greeks + admitted_net
        if abs(portfolio_admitted.gamma * spot * spot) > cfg.gamma_cap_pct_nav * nav:
            return OptionsGateResult.silence(
                bucket, "portfolio_gamma_cap_at_size", net_greeks=admitted_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        if admitted_net.theta < 0 and abs(admitted_net.theta) > cfg.theta_budget_pct_nav_per_day * nav:
            return OptionsGateResult.silence(
                bucket, "theta_budget_at_size", net_greeks=admitted_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        if abs(portfolio_admitted.vega) > cfg.vega_cap_pct_nav * nav / 100.0:
            return OptionsGateResult.silence(
                bucket, "portfolio_vega_cap_at_size", net_greeks=admitted_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        if abs(portfolio_admitted.delta * spot) > cfg.max_net_delta_pct_nav * nav:
            return OptionsGateResult.silence(
                bucket, "net_delta_cap_at_size", net_greeks=admitted_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        candidate_net = admitted_net  # report the true admitted-size footprint

    # ---- O6 BPR + CSP cash-collateral RE-CHECK at the ADMITTED contract count. ----
    # The earlier O6 BPR check (line ~534) and the classifier's CSP cash-collateral
    # gate (_classify_structure, called with structural_contracts) both validated
    # against `structural_contracts` (= 1 for every covered_call / cash_secured_put).
    # But `_size_contracts` routinely admits MORE lots (e.g. 2). Both BPR and the
    # full assignment cash scale LINEARLY with the admitted lot count, so a 2-lot CSP
    # whose 1-lot BPR is under the buffer (and whose 1-lot collateral the operator
    # has) can breach the buffer / be under-collateralized at 2 lots. The greeks were
    # already re-checked at size; BPR and collateral were NOT — leaving the exact
    # naked/over-leveraged admission this gate exists to reject. Mirror the greeks
    # re-check: recompute BPR at the admitted `contracts`, re-run O6, and (for a CSP)
    # re-validate the full strike*100*contracts cash requirement. Any breach silences
    # (fail-closed; the gate can only REJECT, never size up).
    if contracts != structural_contracts:
        bpr = _bpr(
            bucket,
            strike=strike,
            contracts=contracts,
            premium_received=premium_received,
            premium_paid=premium_paid,
            width=width,
            net_credit=net_credit,
        )
        if total_bpr + bpr > cfg.bpr_buffer_pct_nav * nav:
            return OptionsGateResult.silence(
                bucket, "bpr_buffer_at_size", net_greeks=candidate_net,
                bpr_estimate=bpr, max_loss=max_loss,
            )
        if bucket == StructureBucket.CASH_SECURED_PUT:
            # Full assignment cash at the admitted size (premium NOT netted —
            # identical to _classify_structure's requirement, just at `contracts`).
            required = strike * 100 * contracts
            if options_buying_power < required:
                return OptionsGateResult.silence(
                    bucket, "csp_collateral_at_size", net_greeks=candidate_net,
                    bpr_estimate=bpr, max_loss=max_loss,
                )

    # ---- Cumulative assignment-cash cap (ADR-0027 D2/D4 HARD rule). ----
    # The per-call collateral gate (_classify_structure + the at-size re-check
    # above) only verifies THIS structure is individually cash-secured — it does
    # NOT bound the PORTFOLIO-WIDE all-assign tail. ADR-0027 D4 (wheel) makes that
    # explicit: "Sum of CSP cash reservations + CC stock basis <=
    # max_assignment_risk_pct_nav". Without this rule N separately-admitted CSPs
    # (each ~2.5% NAV Kelly-sized) reserve cumulative assignment cash far past the
    # 20%-NAV cap. Compute THIS structure's incremental assignment cash at the
    # ADMITTED contract count and silence if open_assignment_cash + incremental
    # breaches the cap. CSP: strike*100*contracts (full assignment cash, premium
    # NOT netted — matches the collateral gate). CC: basis_per_share*100*contracts
    # (the stock basis backing the cover). Defined-risk spreads carry no
    # assignment-cash obligation (max-loss-bound), so they contribute 0. Reject
    # only — never a size-up; default open_assignment_cash=0.0 keeps a single
    # within-budget structure byte-identical.
    incremental_assignment_cash = 0.0
    if bucket == StructureBucket.CASH_SECURED_PUT:
        incremental_assignment_cash = strike * 100 * contracts
    elif bucket == StructureBucket.COVERED_CALL:
        incremental_assignment_cash = (basis_per_share or 0.0) * 100 * contracts
    if (
        open_assignment_cash + incremental_assignment_cash
        > cfg.max_assignment_risk_pct_nav * nav
    ):
        return OptionsGateResult.silence(
            bucket, "cumulative_assignment_risk_cap", net_greeks=candidate_net,
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
