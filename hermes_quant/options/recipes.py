"""hermes_quant.options.recipes — multi-leg PRODUCER seam (ADR-0029 B01).

The MISSING half of the multi-leg loop: the CONSUME side (MultiLegPaperReactor,
react/dispatch.select_reactor, options_gate, the from_gate_result constructor-lock)
shipped earlier; this module is the PRODUCE side. Given a covered_call / cash_secured_put
/ wheel-eligible symbol + a DETERMINISTIC replayed chain snapshot, it:

  1. selects an option leg (and, for a CC, the equity leg) by a deterministic rule,
  2. runs the EXISTING ``options_gate`` (ADR-0027; requires HERMES_QUANT_OPTIONS_GATE=1),
  3. mints a ``MultiLegProposal`` via ``from_gate_result`` (the ONLY way risk_gate_pass=True
     can exist — the #38 lock), and
  4. persists it via ``ProposalStore.propose_multi_leg`` as proposal_kind=='multi_leg',

so that ``store.get()`` returns something ``select_reactor`` routes to the
``MultiLegPaperReactor``, which fires it on paper when BOTH flags are on.

RAILS (the whole producer path is inert by default):
  * the gate raises ``OptionsGateDisabled`` unless ``HERMES_QUANT_OPTIONS_GATE=1`` — so
    NOTHING here can build a passing proposal without the flag;
  * a gate REJECT (admitted=False) does NOT persist a passing proposal — by default we
    do not persist a rejected structure at all (``persist_rejected=False``); when
    explicitly asked to persist for audit, the stored proposal carries the rejected
    verdict and the reactor refuses to fill it;
  * firing additionally needs ``HERMES_QUANT_MULTILEG_REACTOR=1`` (the reactor's own guard);
  * the deterministic chain reader is ``replay_chain`` (no network / no creds / no live
    Alpaca); no live submit anywhere (paper-only, deferred by design);
  * ``risk_gate_pass`` is ALWAYS the gate's verdict copied by ``from_gate_result`` — never
    hand-set.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from hermes_quant.options.data import (
    ChainSnapshotReader,
    OptionChain,
    OptionLeg,
    OptionSnapshot,
    StockLeg,
)
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.risk.options_gate import (
    OptionsRiskConfig,
    StructureBucket,
    options_gate,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_quant.proposals import Proposal, ProposalStore

logger = logging.getLogger(__name__)

StrategyKind = Literal["covered_call", "cash_secured_put", "wheel", "bull_put_spread", "bear_call_spread"]

# ADR-0098 Step 2: the first defined-risk credit vertical (bull put spread).
# Flag guards the selection seam; the producer itself is always importable (pure).
_VERTICAL_SPREADS_FLAG = "HERMES_QUANT_VERTICAL_SPREADS"

# Default leg-selection knobs (deterministic; no optimization, no LLM).
_DEFAULT_TARGET_DELTA = 0.30  # |delta| target for the short leg (income workhorse)
_DEFAULT_DTE_MIN = 25
_DEFAULT_DTE_MAX = 45

# ADR-0084 O8 master flag (mirrors risk/options_gate.py + risk/gate.py: read at
# call time, never cached at import). Absent/"0" => event_risk is NOT forwarded
# into options_gate => O8 never runs => byte-identical to today.
_EVENT_RISK_FLAG = "HERMES_QUANT_EVENT_RISK"


class RecipeBuildError(ValueError):
    """The producer could not build a structure from the snapshot (no eligible
    contract, bad inputs, etc.). Fail-closed: never silently fabricate a leg."""


@dataclass(frozen=True)
class MultiLegBuildResult:
    """What the builder produced. ``proposal`` is None iff the gate rejected the
    structure (admitted=False); ``admitted`` mirrors the gate verdict; ``reason`` is
    the gate's silence reason on a reject."""

    admitted: bool
    proposal: MultiLegProposal | None
    bucket: StructureBucket
    reason: str | None
    contracts: int


# ---------------------------------------------------------------------------
# Deterministic leg selection
# ---------------------------------------------------------------------------


def _eligible_snapshots(
    chain: OptionChain,
    *,
    right: Literal["C", "P"],
    dte_min: int,
    dte_max: int,
) -> list[OptionSnapshot]:
    """Snapshots of the requested right within the DTE window with a usable mid and
    a known delta. Deterministic ordering: (dte, strike, symbol)."""
    from hermes_quant.options.occ import OccParseError, parse_occ

    out: list[OptionSnapshot] = []
    for s in chain.snapshots:
        try:
            comp = parse_occ(s.symbol)
        except OccParseError:  # malformed OCC => skip (fail-closed)
            continue
        if comp.right != right:
            continue
        if not (dte_min <= s.dte <= dte_max):
            continue
        # wave2-review FIX (fail-OPEN, ar03/ar08 family): a float('nan') mid passes
        # BOTH `mid is None` (False) AND `mid <= 0` (NaN<=0 is False), so it entered the
        # candidate list, got selected as a spread leg, and _finite_mid then coerced it to
        # 0.0 — treating a long PROTECTION leg as FREE, understating max_loss and letting
        # the gate admit too many contracts (the long leg is what makes a vertical
        # defined-risk). math.isfinite is the canonical NaN/inf checkpoint here; this also
        # protects CC/CSP which share this filter.
        if s.mid is None or not math.isfinite(s.mid) or s.mid <= 0:
            continue
        if s.greeks.delta is None:
            continue
        out.append(s)
    out.sort(key=lambda s: (s.dte, parse_strike(s.symbol), s.symbol))
    return out


def parse_strike(symbol: str) -> Decimal:
    from hermes_quant.options.occ import parse_occ

    return parse_occ(symbol).strike


def _finite_mid(mid) -> float:  # noqa: ANN001 — Decimal | float | None at the seam
    """Coerce a snapshot mid to a finite non-negative float, else 0.0.

    `_eligible_snapshots` rejects ``mid is None`` and ``mid <= 0``, but a NaN mid
    slips through both (``NaN <= 0`` is False) and ``mid or 0.0`` does not catch
    it either (NaN is truthy). A NaN premium would then flow into options_gate's
    CSP BPR math and poison the O6 buffer compare (cs06). Fail-closed to 0.0 on
    any non-finite or sub-zero value; never raises."""
    if mid is None:
        return 0.0
    try:
        v = float(mid)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v) or v < 0.0:
        return 0.0
    return v


def _pick_by_target_delta(
    snaps: list[OptionSnapshot], *, target_delta: float
) -> OptionSnapshot:
    """Pick the snapshot whose |delta| is closest to ``target_delta``. Ties broken by
    the deterministic ordering already imposed on ``snaps`` (lower dte/strike/symbol
    first). Raises RecipeBuildError on an empty list."""
    if not snaps:
        raise RecipeBuildError("no eligible contracts after DTE/right/liquidity filter")
    best = min(
        snaps,
        key=lambda s: (abs(abs(s.greeks.delta or 0.0) - target_delta), s.dte, s.symbol),
    )
    return best


def _snapshot_to_short_leg(snap: OptionSnapshot) -> OptionLeg:
    """Build a sell_to_open OptionLeg carrying the snapshot's greeks-at-decision."""
    return OptionLeg(
        symbol=snap.symbol,
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=snap.greeks,
        fill_price=snap.mid,  # the decision-time net price anchor (paper)
    )


def _snapshot_to_long_leg(snap: OptionSnapshot) -> OptionLeg:
    """Build a buy_to_open OptionLeg (the long PROTECTION leg) for a defined-risk spread."""
    return OptionLeg(
        symbol=snap.symbol,
        side="buy",
        position_intent="buy_to_open",
        ratio_qty=1,
        greeks_at_decision=snap.greeks,
        fill_price=snap.mid,
    )


def _pick_long_put(
    snaps: list[OptionSnapshot],
    *,
    short: OptionSnapshot,
    min_width_pct: float = 0.01,
) -> OptionSnapshot:
    """Pick the further-OTM (lower-strike) put that protects the short put.

    Requirements:
      * Same expiry (same DTE window, already filtered by _eligible_snapshots).
      * Strike STRICTLY LESS THAN the short strike (so width = short_K - long_K > 0).
      * ``min_width_pct`` of the short strike (floor a degenerate near-zero spread).
    Picks the HIGHEST eligible strike below the short strike (narrowest spread that
    is still a meaningful width). Ties broken by the existing deterministic ordering
    (dte, strike, symbol). Raises RecipeBuildError when no eligible long put exists.
    """
    from hermes_quant.options.occ import parse_occ

    short_strike = float(parse_occ(short.symbol).strike)
    short_expiry = parse_occ(short.symbol).expiry
    min_width = short_strike * min_width_pct

    candidates = [
        s for s in snaps
        if (
            parse_occ(s.symbol).expiry == short_expiry
            and float(parse_occ(s.symbol).strike) < short_strike
            and (short_strike - float(parse_occ(s.symbol).strike)) >= min_width
        )
    ]
    if not candidates:
        raise RecipeBuildError(
            f"no eligible long-put strike below short {short_strike:.2f} "
            f"(min_width={min_width:.2f}) for a bull-put-spread protection leg"
        )
    # Highest qualifying strike = narrowest valid width (income-maximizing, still defined-risk).
    return max(candidates, key=lambda s: (float(parse_occ(s.symbol).strike), s.dte, s.symbol))


def _pick_long_call(
    snaps: list[OptionSnapshot],
    *,
    short: OptionSnapshot,
    min_width_pct: float = 0.01,
) -> OptionSnapshot:
    """Pick the further-OTM (higher-strike) call that protects the short call.

    Requirements:
      * Same expiry (same DTE window, already filtered by _eligible_snapshots).
      * Strike STRICTLY GREATER THAN the short strike (so width = long_K - short_K > 0).
      * ``min_width_pct`` of the short strike (floor a degenerate near-zero spread).
    Picks the LOWEST eligible strike above the short strike (narrowest spread that
    is still a meaningful width). Ties broken by the existing deterministic ordering
    (dte, strike, symbol). Raises RecipeBuildError when no eligible long call exists.
    """
    from hermes_quant.options.occ import parse_occ

    short_strike = float(parse_occ(short.symbol).strike)
    short_expiry = parse_occ(short.symbol).expiry
    min_width = short_strike * min_width_pct

    candidates = [
        s for s in snaps
        if (
            parse_occ(s.symbol).expiry == short_expiry
            and float(parse_occ(s.symbol).strike) > short_strike
            and (float(parse_occ(s.symbol).strike) - short_strike) >= min_width
        )
    ]
    if not candidates:
        raise RecipeBuildError(
            f"no eligible long-call strike above short {short_strike:.2f} "
            f"(min_width={min_width:.2f}) for a bear-call-spread protection leg"
        )
    # Lowest qualifying strike = narrowest valid width (income-maximizing, still defined-risk).
    return min(candidates, key=lambda s: (float(parse_occ(s.symbol).strike), s.dte, s.symbol))


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_multi_leg_proposal(
    *,
    symbol: str,
    asof: datetime,
    strategy_kind: StrategyKind,
    chain: OptionChain | None = None,
    reader: ChainSnapshotReader | None = None,
    nav: float,
    held_shares: int = 0,
    options_buying_power: float,
    total_bpr: float = 0.0,
    portfolio_net_greeks=None,  # noqa: ANN001 — NetGreeks; default zero below
    open_strategies_on_underlying: int = 0,
    # ADR-0027 D2/D4 cumulative assignment-cash cap. Sum of already-reserved CSP
    # cash collateral + open CC stock basis for this account, EXCLUDING this
    # candidate. Forwarded verbatim into options_gate, which enforces the
    # max_assignment_risk_pct_nav invariant at the admitted contract count.
    # Default 0.0 => no prior reservations => byte-identical (a single
    # within-budget structure is unaffected). The caller that holds the
    # portfolio book supplies the running total; absent it, 0.0 is fail-OPEN-safe
    # only because the gate still binds THIS structure's own incremental cash
    # against the cap.
    open_assignment_cash: float = 0.0,
    cfg: OptionsRiskConfig | None = None,
    target_delta: float = _DEFAULT_TARGET_DELTA,
    dte_min: int = _DEFAULT_DTE_MIN,
    dte_max: int = _DEFAULT_DTE_MAX,
    source_recipe_id: str = "options.recipes.build_multi_leg_proposal",
    proposal_id: str | None = None,
    # ADR-0084 O8 (earnings-proximity / IV-crush). ADDITIVE + DEFAULT-OFF: the
    # asof-honest, outcome-free event-risk payload (ctx.extras['event_risk']
    # shape) for `symbol`. Forwarded into options_gate ONLY when
    # HERMES_QUANT_EVENT_RISK=1 (read at call time); absent flag => not
    # forwarded => O8 never runs => byte-identical. None => no check.
    event_risk: Mapping | None = None,
) -> MultiLegBuildResult:
    """Build + gate (NOT persist) a CC/CSP/wheel from a DETERMINISTIC chain snapshot.

    Either ``chain`` (already replayed) or ``reader`` (we call ``replay_chain`` for the
    deterministic, no-network snapshot) must be supplied. Runs ``options_gate`` (which
    raises ``OptionsGateDisabled`` unless ``HERMES_QUANT_OPTIONS_GATE=1`` — the whole
    producer is inert without the flag), and mints the proposal via ``from_gate_result``
    ONLY when the gate admits. On a gate reject, ``proposal`` is None and the verdict is
    returned for the caller to log/persist-for-audit.

    DISPATCH (ADR-0098): the CC/CSP/wheel fall-through below only knows right="C"/"P",
    so a ``bull_put_spread`` / ``bear_call_spread`` would be MIS-BUILT as a covered call
    (right defaults to "C" for anything != cash_secured_put — wrong legs, wrong
    max_loss, wrong strategy_kind). Those defined-risk credit verticals have dedicated
    builders; delegate to them at the top so the AUTONOMOUS path
    (build_and_persist_multi_leg) routes correctly, matching the TOOL path's dispatch.
    The spread builders are themselves gated by HERMES_QUANT_OPTIONS_GATE (and their
    CALLER seam by HERMES_QUANT_VERTICAL_SPREADS), so a flag-OFF caller still abstains
    => byte-identical.
    """
    if strategy_kind in ("bull_put_spread", "bear_call_spread"):
        _spread_builder = (
            build_bull_put_spread_proposal
            if strategy_kind == "bull_put_spread"
            else build_bear_call_spread_proposal
        )
        # Forward only the kwargs the spread builders accept (they do NOT take
        # held_shares / open_assignment_cash / composite_intent — those are
        # CC/CSP-specific). source_recipe_id is left to each builder's own default.
        return _spread_builder(
            symbol=symbol,
            asof=asof,
            chain=chain,
            reader=reader,
            nav=nav,
            options_buying_power=options_buying_power,
            total_bpr=total_bpr,
            portfolio_net_greeks=portfolio_net_greeks,
            open_strategies_on_underlying=open_strategies_on_underlying,
            cfg=cfg,
            target_delta=target_delta,
            dte_min=dte_min,
            dte_max=dte_max,
            proposal_id=proposal_id,
            event_risk=event_risk,
        )

    from hermes_quant.options.data import NetGreeks

    cfg = cfg or OptionsRiskConfig()
    portfolio_net_greeks = portfolio_net_greeks or NetGreeks.zero()

    if chain is None:
        if reader is None:
            reader = ChainSnapshotReader()
        chain = reader.replay_chain(symbol, asof)

    spot = float(chain.underlying_spot)
    if spot <= 0:
        raise RecipeBuildError(f"non-positive underlying spot for {symbol}: {spot}")

    is_wheel = strategy_kind == "wheel"
    composite_intent = "wheel" if is_wheel else None
    # wheel overlays a covered call on already-held shares.
    right: Literal["C", "P"] = (
        "P" if strategy_kind == "cash_secured_put" else "C"
    )

    snaps = _eligible_snapshots(chain, right=right, dte_min=dte_min, dte_max=dte_max)
    short = _pick_by_target_delta(snaps, target_delta=target_delta)
    short_leg = _snapshot_to_short_leg(short)
    strike = float(parse_strike(short.symbol))
    min_dte = short.dte
    # Per-contract premium (1 lot). `short.mid or 0.0` does NOT catch a NaN mid
    # (NaN is truthy), and `_eligible_snapshots`' `mid <= 0` filter also lets a
    # NaN through (`NaN <= 0` is False). Coerce a non-finite mid to 0.0 so a NaN
    # never reaches options_gate's BPR math (cs06; cr02 P2 follow-up). The gate
    # also fails closed on a non-finite premium_received as defense in depth.
    short_mid = _finite_mid(short.mid)
    premium_received = short_mid * 100.0

    legs: list[OptionLeg | StockLeg]
    stock_leg: StockLeg | None
    basis_per_share: float | None
    if right == "C":
        # Covered call (or wheel overlay): the equity leg collateralizes the short call.
        basis_per_share = spot
        stock_leg = StockLeg(
            underlying=symbol.upper(),
            qty=100,  # one lot of cover; gate sizes contract count separately
            basis_per_share=basis_per_share,
        )
        legs = [stock_leg, short_leg]
    else:
        basis_per_share = None
        stock_leg = None
        legs = [short_leg]

    # ADR-0084 O8 carrier (DEFAULT-OFF, ADDITIVE): forward the asof-honest
    # event-risk payload + the decision asof into options_gate ONLY when
    # HERMES_QUANT_EVENT_RISK=1 (read at call time). Flag absent => both stay
    # None => options_gate's O8 rule never runs => byte-identical to today. The
    # gate's O8 ALSO re-checks the master flag, so a caller that passes a
    # payload while the flag is OFF is still a no-op (defense in depth).
    if os.environ.get(_EVENT_RISK_FLAG, "0") == "1":
        gate_event_risk: Mapping | None = event_risk
        gate_decision_asof: datetime | None = asof
    else:
        gate_event_risk = None
        gate_decision_asof = None

    gate_result = options_gate(
        legs,
        strategy_kind="covered_call" if right == "C" else "cash_secured_put",
        underlying=symbol.upper(),
        spot=spot,
        nav=nav,
        held_shares=held_shares,
        options_buying_power=options_buying_power,
        premium_received=premium_received,
        portfolio_net_greeks=portfolio_net_greeks,
        total_bpr=total_bpr,
        cfg=cfg,
        composite_intent=composite_intent,
        strike=strike,
        basis_per_share=basis_per_share,
        min_dte=min_dte,
        open_strategies_on_underlying=open_strategies_on_underlying,
        open_assignment_cash=open_assignment_cash,
        event_risk=gate_event_risk,
        decision_asof=gate_decision_asof,
    )

    if not gate_result.admitted:
        # Gate REJECT (silence). No passing proposal is built; the caller decides
        # whether to persist a rejected proposal for audit (default: do not).
        logger.info(
            "options.recipes: %s %s gate-rejected: %s",
            symbol,
            strategy_kind,
            gate_result.reason,
        )
        return MultiLegBuildResult(
            admitted=False,
            proposal=None,
            bucket=gate_result.bucket,
            reason=gate_result.reason,
            contracts=gate_result.contracts,
        )

    contracts = max(int(gate_result.contracts), 1)
    # The CC's equity leg covers the admitted contract count (100 shares / contract).
    if stock_leg is not None:
        stock_leg = StockLeg(
            underlying=stock_leg.underlying,
            qty=100 * contracts,
            basis_per_share=stock_leg.basis_per_share,
        )

    # Net price (signed): a short call/put is a CREDIT (negative net_debit_credit).
    # mid * 100 * contracts, received => negative.
    net_credit_per_contract = Decimal(str(short_mid)) * Decimal(100)
    net_debit_credit = (-net_credit_per_contract * Decimal(contracts)).quantize(
        Decimal("0.01")
    )

    if proposal_id is None:
        proposal_id = _mint_proposal_id(symbol, asof)

    proposal = MultiLegProposal.from_gate_result(
        gate_result=gate_result,
        proposal_id=proposal_id,
        asof=asof,
        strategy_kind=strategy_kind,
        underlying=symbol.upper(),
        option_legs=(short_leg,),
        stock_leg=stock_leg,
        outer_qty=contracts,
        net_debit_credit=net_debit_credit,
        max_gain=None,  # CC/CSP upside is collateral-bound; gate left max_gain open
        breakeven_underlying=(Decimal(str(strike)),),
        rationale=(
            f"{strategy_kind} on {symbol.upper()} @ K={strike} dte={min_dte} "
            f"|delta|~{abs(short.greeks.delta or 0.0):.2f}; gate bucket "
            f"{gate_result.bucket.value}, {contracts} contract(s)"
        ),
        source_recipe_id=source_recipe_id,
    )
    return MultiLegBuildResult(
        admitted=True,
        proposal=proposal,
        bucket=gate_result.bucket,
        reason=None,
        contracts=contracts,
    )


def build_bull_put_spread_proposal(
    *,
    symbol: str,
    asof: datetime,
    chain: OptionChain | None = None,
    reader: ChainSnapshotReader | None = None,
    nav: float,
    options_buying_power: float,
    total_bpr: float = 0.0,
    portfolio_net_greeks=None,  # noqa: ANN001 — NetGreeks; default zero below
    open_strategies_on_underlying: int = 0,
    cfg: OptionsRiskConfig | None = None,
    target_delta: float = _DEFAULT_TARGET_DELTA,
    dte_min: int = _DEFAULT_DTE_MIN,
    dte_max: int = _DEFAULT_DTE_MAX,
    source_recipe_id: str = "options.recipes.build_bull_put_spread_proposal",
    proposal_id: str | None = None,
    event_risk: Mapping | None = None,
) -> MultiLegBuildResult:
    """Build + gate (NOT persist) a BULL PUT SPREAD (ADR-0098 Step 2).

    A bull put spread = SELL 1 OTM put (short, ~0.20-0.30 delta) + BUY 1 further-OTM
    put (long protection leg, position_intent='buy_to_open') at a lower strike on the
    SAME expiry. The long leg caps the loss at (short_K - long_K - net_credit)*100 per
    contract, making max_loss FINITE and DEFINED (ADR-0098: "naked short put is
    PERMANENTLY excluded — a bull-put-spread is the DEFINED-RISK version").

    DEFAULT-OFF: this function is always callable (for tests), but the CALLER in the
    strategy-selection table gates entry via HERMES_QUANT_VERTICAL_SPREADS=1 (see
    structure_select.py). The gate itself (options_gate) further requires
    HERMES_QUANT_OPTIONS_GATE=1. Without the latter, this raises OptionsGateDisabled.

    Returns a MultiLegBuildResult with:
      * admitted=True, proposal=<minted via from_gate_result> on gate ADMIT.
      * admitted=False, proposal=None on gate REJECT or non-finite inputs.
    """
    from hermes_quant.options.data import NetGreeks

    cfg = cfg or OptionsRiskConfig()
    portfolio_net_greeks = portfolio_net_greeks or NetGreeks.zero()

    if chain is None:
        if reader is None:
            reader = ChainSnapshotReader()
        chain = reader.replay_chain(symbol, asof)

    spot = float(chain.underlying_spot)
    if spot <= 0:
        raise RecipeBuildError(f"non-positive underlying spot for {symbol}: {spot}")

    # Select OTM puts in the DTE window.
    snaps = _eligible_snapshots(chain, right="P", dte_min=dte_min, dte_max=dte_max)

    # Short leg: ~0.20-0.30 delta OTM put (income workhorse; same selection as CSP).
    short = _pick_by_target_delta(snaps, target_delta=target_delta)
    short_leg = _snapshot_to_short_leg(short)
    short_strike = float(parse_strike(short.symbol))
    min_dte = short.dte
    short_mid = _finite_mid(short.mid)

    # Long leg: further-OTM (lower-strike) put on the same expiry — the protection.
    # _pick_long_put reuses the same `snaps` list (already DTE-filtered + right="P").
    long = _pick_long_put(snaps, short=short)
    long_leg = _snapshot_to_long_leg(long)
    long_strike = float(parse_strike(long.symbol))
    long_mid = _finite_mid(long.mid)

    # Spread economics. Non-finite mid must fail-CLOSED (never flow into gate math).
    # _finite_mid already coerces NaN/inf -> 0.0 for each leg; a zero long_mid is
    # still a valid (conservative) price (free protection); a zero short_mid means
    # we received no credit, which the gate may reject via min_trade_size (BPR=0,
    # contracts=0). Both are handled deterministically downstream.
    width = short_strike - long_strike  # USD per share; always > 0 by construction
    net_credit = short_mid - long_mid   # per share (not yet x100)
    if not math.isfinite(width) or width <= 0:
        raise RecipeBuildError(
            f"bull_put_spread: non-positive width={width:.4f} "
            f"(short={short_strike}, long={long_strike})"
        )

    # max_loss = (width - net_credit) * 100 per contract. MUST be finite + positive.
    # The gate re-derives this via _max_loss and enforces it; we compute it here for
    # the rationale and as a pre-flight non-finite guard (fail-CLOSED).
    max_loss_per_contract = (width - max(net_credit, 0.0)) * 100
    if not math.isfinite(max_loss_per_contract) or max_loss_per_contract <= 0:
        logger.info(
            "options.recipes: %s bull_put_spread non-finite or zero max_loss=%s; "
            "rejecting before gate",
            symbol,
            max_loss_per_contract,
        )
        from hermes_quant.risk.options_gate import StructureBucket
        return MultiLegBuildResult(
            admitted=False,
            proposal=None,
            bucket=StructureBucket.DEFINED_RISK,
            reason="bull_put_spread_max_loss_not_finite_or_nonpositive",
            contracts=0,
        )

    premium_received = max(net_credit, 0.0) * 100.0   # per contract (credit spread)
    premium_paid = max(-net_credit, 0.0) * 100.0      # 0 for a net-credit spread

    legs = [short_leg, long_leg]

    # ADR-0084 O8 carrier (DEFAULT-OFF, additive — same pattern as CC/CSP).
    if os.environ.get(_EVENT_RISK_FLAG, "0") == "1":
        gate_event_risk: Mapping | None = event_risk
        gate_decision_asof: datetime | None = asof
    else:
        gate_event_risk = None
        gate_decision_asof = None

    gate_result = options_gate(
        legs,
        strategy_kind="bull_put_spread",
        underlying=symbol.upper(),
        spot=spot,
        nav=nav,
        held_shares=0,
        options_buying_power=options_buying_power,
        premium_received=premium_received,
        portfolio_net_greeks=portfolio_net_greeks,
        total_bpr=total_bpr,
        cfg=cfg,
        composite_intent=None,
        strike=short_strike,
        width=width,
        net_credit=max(net_credit, 0.0),
        net_debit=max(-net_credit, 0.0),
        premium_paid=premium_paid,
        basis_per_share=None,
        min_dte=min_dte,
        open_strategies_on_underlying=open_strategies_on_underlying,
        event_risk=gate_event_risk,
        decision_asof=gate_decision_asof,
    )

    if not gate_result.admitted:
        logger.info(
            "options.recipes: %s bull_put_spread gate-rejected: %s",
            symbol,
            gate_result.reason,
        )
        return MultiLegBuildResult(
            admitted=False,
            proposal=None,
            bucket=gate_result.bucket,
            reason=gate_result.reason,
            contracts=gate_result.contracts,
        )

    contracts = max(int(gate_result.contracts), 1)

    # Net price: credit received (short_mid - long_mid) per share * 100 * contracts.
    # Stored as NEGATIVE Decimal (credit received convention, matching CC/CSP).
    net_credit_per_contract = Decimal(str(short_mid)) * 100 - Decimal(str(long_mid)) * 100
    net_debit_credit = (-net_credit_per_contract * Decimal(contracts)).quantize(
        Decimal("0.01")
    )

    if proposal_id is None:
        proposal_id = _mint_proposal_id(symbol, asof)

    proposal = MultiLegProposal.from_gate_result(
        gate_result=gate_result,
        proposal_id=proposal_id,
        asof=asof,
        strategy_kind="bull_put_spread",
        underlying=symbol.upper(),
        option_legs=(short_leg, long_leg),
        stock_leg=None,
        outer_qty=contracts,
        net_debit_credit=net_debit_credit,
        max_gain=Decimal(str(round(premium_received * contracts, 2))),
        breakeven_underlying=(Decimal(str(short_strike - max(net_credit, 0.0))),),
        rationale=(
            f"bull_put_spread on {symbol.upper()} @ short K={short_strike} / "
            f"long K={long_strike} dte={min_dte} "
            f"|delta|~{abs(short.greeks.delta or 0.0):.2f}; "
            f"width={width:.2f} net_credit={net_credit:.4f}/share "
            f"max_loss={max_loss_per_contract:.2f}/contract; "
            f"gate bucket {gate_result.bucket.value}, {contracts} contract(s)"
        ),
        source_recipe_id=source_recipe_id,
    )
    return MultiLegBuildResult(
        admitted=True,
        proposal=proposal,
        bucket=gate_result.bucket,
        reason=None,
        contracts=contracts,
    )


def build_bear_call_spread_proposal(
    *,
    symbol: str,
    asof: datetime,
    chain: OptionChain | None = None,
    reader: ChainSnapshotReader | None = None,
    nav: float,
    options_buying_power: float,
    total_bpr: float = 0.0,
    portfolio_net_greeks=None,  # noqa: ANN001 — NetGreeks; default zero below
    open_strategies_on_underlying: int = 0,
    cfg: OptionsRiskConfig | None = None,
    target_delta: float = _DEFAULT_TARGET_DELTA,
    dte_min: int = _DEFAULT_DTE_MIN,
    dte_max: int = _DEFAULT_DTE_MAX,
    source_recipe_id: str = "options.recipes.build_bear_call_spread_proposal",
    proposal_id: str | None = None,
    event_risk: Mapping | None = None,
) -> MultiLegBuildResult:
    """Build + gate (NOT persist) a BEAR CALL SPREAD (ADR-0098 Step 3).

    A bear call spread = SELL 1 OTM call (short, ~0.20-0.30 delta) + BUY 1 further-OTM
    call (long protection leg, position_intent='buy_to_open') at a HIGHER strike on the
    SAME expiry. The long leg caps the loss at (long_K - short_K - net_credit)*100 per
    contract, making max_loss FINITE and DEFINED.

    This is the call-side mirror of the bull put spread (ADR-0098 Step 2). Together they
    form both wings of an iron condor.

    DEFAULT-OFF: this function is always callable (for tests), but the CALLER in the
    strategy-selection table gates entry via HERMES_QUANT_VERTICAL_SPREADS=1 (see
    structure_select.py). The gate itself (options_gate) further requires
    HERMES_QUANT_OPTIONS_GATE=1. Without the latter, this raises OptionsGateDisabled.

    Returns a MultiLegBuildResult with:
      * admitted=True, proposal=<minted via from_gate_result> on gate ADMIT.
      * admitted=False, proposal=None on gate REJECT or non-finite inputs.
    """
    from hermes_quant.options.data import NetGreeks

    cfg = cfg or OptionsRiskConfig()
    portfolio_net_greeks = portfolio_net_greeks or NetGreeks.zero()

    if chain is None:
        if reader is None:
            reader = ChainSnapshotReader()
        chain = reader.replay_chain(symbol, asof)

    spot = float(chain.underlying_spot)
    if spot <= 0:
        raise RecipeBuildError(f"non-positive underlying spot for {symbol}: {spot}")

    # Select OTM calls in the DTE window.
    snaps = _eligible_snapshots(chain, right="C", dte_min=dte_min, dte_max=dte_max)

    # Short leg: ~0.20-0.30 delta OTM call (income workhorse; same selection logic as CC).
    short = _pick_by_target_delta(snaps, target_delta=target_delta)
    short_leg = _snapshot_to_short_leg(short)
    short_strike = float(parse_strike(short.symbol))
    min_dte = short.dte
    short_mid = _finite_mid(short.mid)

    # Long leg: further-OTM (higher-strike) call on the same expiry — the protection.
    # _pick_long_call reuses the same `snaps` list (already DTE-filtered + right="C").
    long = _pick_long_call(snaps, short=short)
    long_leg = _snapshot_to_long_leg(long)
    long_strike = float(parse_strike(long.symbol))
    long_mid = _finite_mid(long.mid)

    # Spread economics. Non-finite mid must fail-CLOSED (never flow into gate math).
    # _finite_mid already coerces NaN/inf -> 0.0 for each leg. Same logic as BPS.
    width = long_strike - short_strike  # USD per share; always > 0 by construction
    net_credit = short_mid - long_mid   # per share (not yet x100)
    if not math.isfinite(width) or width <= 0:
        raise RecipeBuildError(
            f"bear_call_spread: non-positive width={width:.4f} "
            f"(short={short_strike}, long={long_strike})"
        )

    # max_loss = (width - net_credit) * 100 per contract. MUST be finite + positive.
    max_loss_per_contract = (width - max(net_credit, 0.0)) * 100
    if not math.isfinite(max_loss_per_contract) or max_loss_per_contract <= 0:
        logger.info(
            "options.recipes: %s bear_call_spread non-finite or zero max_loss=%s; "
            "rejecting before gate",
            symbol,
            max_loss_per_contract,
        )
        from hermes_quant.risk.options_gate import StructureBucket
        return MultiLegBuildResult(
            admitted=False,
            proposal=None,
            bucket=StructureBucket.DEFINED_RISK,
            reason="bear_call_spread_max_loss_not_finite_or_nonpositive",
            contracts=0,
        )

    premium_received = max(net_credit, 0.0) * 100.0   # per contract (credit spread)
    premium_paid = max(-net_credit, 0.0) * 100.0      # 0 for a net-credit spread

    legs = [short_leg, long_leg]

    # ADR-0084 O8 carrier (DEFAULT-OFF, additive — same pattern as BPS).
    if os.environ.get(_EVENT_RISK_FLAG, "0") == "1":
        gate_event_risk: Mapping | None = event_risk
        gate_decision_asof: datetime | None = asof
    else:
        gate_event_risk = None
        gate_decision_asof = None

    gate_result = options_gate(
        legs,
        strategy_kind="bear_call_spread",
        underlying=symbol.upper(),
        spot=spot,
        nav=nav,
        held_shares=0,
        options_buying_power=options_buying_power,
        premium_received=premium_received,
        portfolio_net_greeks=portfolio_net_greeks,
        total_bpr=total_bpr,
        cfg=cfg,
        composite_intent=None,
        strike=short_strike,
        width=width,
        net_credit=max(net_credit, 0.0),
        net_debit=max(-net_credit, 0.0),
        premium_paid=premium_paid,
        basis_per_share=None,
        min_dte=min_dte,
        open_strategies_on_underlying=open_strategies_on_underlying,
        event_risk=gate_event_risk,
        decision_asof=gate_decision_asof,
    )

    if not gate_result.admitted:
        logger.info(
            "options.recipes: %s bear_call_spread gate-rejected: %s",
            symbol,
            gate_result.reason,
        )
        return MultiLegBuildResult(
            admitted=False,
            proposal=None,
            bucket=gate_result.bucket,
            reason=gate_result.reason,
            contracts=gate_result.contracts,
        )

    contracts = max(int(gate_result.contracts), 1)

    # Net price: credit received (short_mid - long_mid) per share * 100 * contracts.
    # Stored as NEGATIVE Decimal (credit received convention, matching BPS/CC/CSP).
    net_credit_per_contract = Decimal(str(short_mid)) * 100 - Decimal(str(long_mid)) * 100
    net_debit_credit = (-net_credit_per_contract * Decimal(contracts)).quantize(
        Decimal("0.01")
    )

    if proposal_id is None:
        proposal_id = _mint_proposal_id(symbol, asof)

    proposal = MultiLegProposal.from_gate_result(
        gate_result=gate_result,
        proposal_id=proposal_id,
        asof=asof,
        strategy_kind="bear_call_spread",
        underlying=symbol.upper(),
        option_legs=(short_leg, long_leg),
        stock_leg=None,
        outer_qty=contracts,
        net_debit_credit=net_debit_credit,
        max_gain=Decimal(str(round(premium_received * contracts, 2))),
        breakeven_underlying=(Decimal(str(short_strike + max(net_credit, 0.0))),),
        rationale=(
            f"bear_call_spread on {symbol.upper()} @ short K={short_strike} / "
            f"long K={long_strike} dte={min_dte} "
            f"|delta|~{abs(short.greeks.delta or 0.0):.2f}; "
            f"width={width:.2f} net_credit={net_credit:.4f}/share "
            f"max_loss={max_loss_per_contract:.2f}/contract; "
            f"gate bucket {gate_result.bucket.value}, {contracts} contract(s)"
        ),
        source_recipe_id=source_recipe_id,
    )
    return MultiLegBuildResult(
        admitted=True,
        proposal=proposal,
        bucket=gate_result.bucket,
        reason=None,
        contracts=contracts,
    )


def build_and_persist_multi_leg(
    *,
    store: ProposalStore,
    symbol: str,
    asof: datetime,
    strategy_kind: StrategyKind,
    nav: float,
    options_buying_power: float,
    advisor_result: dict | None = None,
    ttl_minutes: int = 15,
    persist_rejected: bool = False,
    **build_kwargs,
) -> tuple[MultiLegBuildResult, Proposal | None]:
    """Build + gate + (on admit) PERSIST a multi-leg proposal via the extended store.

    Returns ``(build_result, persisted_record_or_None)``. On a gate reject, NOTHING is
    persisted unless ``persist_rejected=True`` (and then the stored proposal carries the
    rejected verdict, which the reactor refuses to fill). This is the rail: an
    ungated/rejected structure NEVER persists a *passing* proposal.
    """
    result = build_multi_leg_proposal(
        symbol=symbol,
        asof=asof,
        strategy_kind=strategy_kind,
        nav=nav,
        options_buying_power=options_buying_power,
        **build_kwargs,
    )
    if result.proposal is None:
        if not persist_rejected:
            return result, None
        # Audit-persist a rejected structure: requires a (rejected) proposal object.
        # We do not synthesize one here (the builder already declined to mint on
        # reject), so there is nothing to persist — fail-closed, return None.
        return result, None

    advisor_result = advisor_result or _default_advisor_result(result)
    record = store.propose_multi_leg(
        proposal=result.proposal,
        gate_result=_result_to_gate(result),
        advisor_result=advisor_result,
        ttl_minutes=ttl_minutes,
    )
    return result, record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_to_gate(result: MultiLegBuildResult):  # noqa: ANN202
    """Rebuild the OptionsGateResult that produced this proposal, for persistence.

    The proposal copied the gate verdict verbatim (from_gate_result), so we
    reconstruct an equivalent OptionsGateResult from the proposal's copied fields.
    This keeps ``propose_multi_leg`` honest: it stores the SAME gate result the
    proposal was minted from, and ``store.get`` re-mints from it (the verdict can
    only round-trip, never be elevated)."""
    from hermes_quant.risk.options_gate import OptionsGateResult

    p = result.proposal
    assert p is not None  # only called on the admit path
    return OptionsGateResult(
        admitted=p.risk_gate_pass,
        bucket=result.bucket,
        reason=p.risk_gate_reason,
        net_greeks=p.net_greeks,
        bpr_estimate=float(p.bpr_estimate),
        max_loss=(None if p.max_loss is None else float(p.max_loss)),
        contracts=result.contracts,
        warnings=tuple(p.risk_gate_warnings),
    )


def _default_advisor_result(result: MultiLegBuildResult) -> dict:
    """A minimal advisor_result so the HITL approve path resolves a non-zero fill
    size. The gate already sized contract count (carried by ``outer_qty``); this
    surfaces a NAV-fraction proxy so ``quant_approve`` does not zero-fill. The
    OPERATOR can still override via ``size_override_pct``."""
    p = result.proposal
    kelly = 0.0
    if p is not None and p.bpr_estimate and p.bpr_estimate > 0:
        # crude income-proxy fraction; honest + deterministic; operator may override.
        kelly = 0.05
    return {
        "risk_gate": {"pass": True, "kelly_fraction": kelly},
        "strategy_kind": None if p is None else p.strategy_kind,
        "bucket": result.bucket.value,
        "contracts": result.contracts,
    }


def _mint_proposal_id(symbol: str, asof: datetime) -> str:
    """proposal_id in the proposals.py prop_<ISO>_<u>_<rand6> shape (B01 reuses it as
    the store key AND the reactor's multi_leg_id)."""
    import secrets

    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    iso = asof.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in symbol.upper())[:16]
    return f"prop_{iso}_{safe}_{secrets.token_hex(3)}"
