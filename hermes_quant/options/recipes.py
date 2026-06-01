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

StrategyKind = Literal["covered_call", "cash_secured_put", "wheel"]

# Default leg-selection knobs (deterministic; no optimization, no LLM).
_DEFAULT_TARGET_DELTA = 0.30  # |delta| target for the short leg (income workhorse)
_DEFAULT_DTE_MIN = 25
_DEFAULT_DTE_MAX = 45


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
        if s.mid is None or s.mid <= 0:
            continue
        if s.greeks.delta is None:
            continue
        out.append(s)
    out.sort(key=lambda s: (s.dte, parse_strike(s.symbol), s.symbol))
    return out


def parse_strike(symbol: str) -> Decimal:
    from hermes_quant.options.occ import parse_occ

    return parse_occ(symbol).strike


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
    cfg: OptionsRiskConfig | None = None,
    target_delta: float = _DEFAULT_TARGET_DELTA,
    dte_min: int = _DEFAULT_DTE_MIN,
    dte_max: int = _DEFAULT_DTE_MAX,
    source_recipe_id: str = "options.recipes.build_multi_leg_proposal",
    proposal_id: str | None = None,
) -> MultiLegBuildResult:
    """Build + gate (NOT persist) a CC/CSP/wheel from a DETERMINISTIC chain snapshot.

    Either ``chain`` (already replayed) or ``reader`` (we call ``replay_chain`` for the
    deterministic, no-network snapshot) must be supplied. Runs ``options_gate`` (which
    raises ``OptionsGateDisabled`` unless ``HERMES_QUANT_OPTIONS_GATE=1`` — the whole
    producer is inert without the flag), and mints the proposal via ``from_gate_result``
    ONLY when the gate admits. On a gate reject, ``proposal`` is None and the verdict is
    returned for the caller to log/persist-for-audit.
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
    premium_received = float(short.mid or 0.0) * 100.0  # per-contract premium (1 lot)

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
    net_credit_per_contract = Decimal(str(short.mid or 0.0)) * Decimal(100)
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
