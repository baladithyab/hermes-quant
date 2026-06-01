"""hermes_quant.options.multileg — MultiLegProposal carrier (ADR-0029 D5).

The reactor's INPUT. Produced upstream (recipe -> options_gate -> HITL approve) and
consumed by MultiLegPaperReactor. Immutable: a revision is a NEW proposal_id
(ADR-0029 D5). Carries the ALREADY-PASSED gate result and the ALREADY-APPROVED
net price so the reactor's job is to FILL, never to re-decide.

The structural consume-never-bypass guarantee lives in ``from_gate_result``: it
copies ``admitted``/``bucket``/``reason``/``net_greeks``/``bpr_estimate``/
``max_loss`` straight off the ``OptionsGateResult``, so the proposal CANNOT carry a
gate verdict that disagrees with the gate (ADR-0079 / plan §8). A
``risk_gate_pass=False`` proposal is persisted for replay/audit but the reactor
refuses to fill it (raises ``GateRejectedProposal``).

A ``risk_gate_pass=True`` proposal is, in addition, UNREPRESENTABLE by direct
construction: ``__post_init__`` raises unless the construction came through
``from_gate_result`` (the mint seam). This is defense-in-depth on top of the
reactor's runtime ``risk_gate_pass is not True`` refusal (#23.3/#38): a passing
gate verdict can only originate from the gate, never from a hand-built proposal.

Money on the proposal is ``Decimal`` (never float) — ADR-0029 D1 strike-rounding
rationale extends to the net price (``net_debit_credit``).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from hermes_quant.options.data import NetGreeks, OptionLeg, StockLeg
from hermes_quant.options.occ import parse_occ

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_quant.risk.options_gate import OptionsGateResult


# Module-private mint token. A ``risk_gate_pass=True`` proposal is ONLY legal when
# it is constructed inside ``from_gate_result`` (the blessed seam that copies the
# gate verdict verbatim). We carry the "this construction is gate-minted" signal in
# a ContextVar — NOT in a dataclass field — so it:
#   * never leaks into repr / asdict / to_dict / JSON (it is not a field at all),
#   * does not change ``__dataclass_fields__`` (callers that re-spread a proposal
#     via ``**{k: getattr(p, k) for k in p.__dataclass_fields__}`` are unaffected),
#   * preserves the frozen dataclass contract (no extra slot, no equality change).
# The ContextVar is set only for the duration of the ``cls(...)`` call inside
# ``from_gate_result`` and reset immediately, so the mint authorization cannot leak
# to any unrelated construction (and is correct under threads/async: each context
# carries its own value; the token object itself is process-private and unexported).
_GATE_MINTED = object()
_minting: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "_multileg_gate_minting", default=None
)


@dataclass(frozen=True)
class MultiLegProposal:
    """Frozen carrier the multi-leg reactor consumes (ADR-0029 D5).

    Reconciles the ADR-0029 D5 body with both 2026-05-24 amendments and the
    existing ``options/data.py`` ``OptionLeg`` (reused, NOT a new leg type).
    """

    proposal_id: str  # prop_<ISO>_<underlying>_<rand6> (proposals.py shape)
    asof: datetime  # UTC decision/publication time (fidelity anchor)
    strategy_kind: str  # 'covered_call'|'cash_secured_put'|'vertical_spread'|
    #                     'iron_condor'|'calendar_spread'|'butterfly'|'pmcc'|'roll'
    underlying: str
    option_legs: tuple[OptionLeg, ...]  # the OPTION legs (OCC-21 + intent + ratio_qty)
    stock_leg: StockLeg | None  # the EQUITY leg of a covered call (None otherwise)
    outer_qty: int  # spread count (mleg OUTER qty); single-leg uses contracts
    net_debit_credit: Decimal  # signed: +debit paid / -credit received (the HITL price)
    net_greeks: NetGreeks  # gate-aggregated at admitted size
    bpr_estimate: Decimal  # buying-power reduction, USD (from OptionsGateResult)
    max_loss: Decimal | None  # USD; None for share/cash-collateralized (CC/CSP)
    max_gain: Decimal | None  # None = unbounded -> must have been blocked by 0027
    breakeven_underlying: tuple[Decimal, ...]
    rationale: str
    source_recipe_id: str
    # Gate PRECONDITION (set by options_gate; the reactor re-asserts, never recomputes):
    risk_gate_pass: bool  # MUST be True for the reactor to fill
    risk_gate_bucket: str  # OptionsGateResult.bucket.value
    risk_gate_reason: str | None  # None when admitted
    risk_gate_warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_mleg(self) -> bool:
        """True iff this routes as a single mleg order (>=2 option legs, no stock
        leg in legs[]). CC/CSP open as a single-leg L1 + (CC) a separate equity
        order, NOT as an mleg order (research §1.3)."""
        return len(self.option_legs) >= 2

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return tuple(leg.symbol for leg in self.option_legs)

    @classmethod
    def from_gate_result(
        cls,
        *,
        gate_result: OptionsGateResult,
        proposal_id: str,
        asof: datetime,
        strategy_kind: str,
        underlying: str,
        option_legs: tuple[OptionLeg, ...],
        stock_leg: StockLeg | None,
        outer_qty: int,
        net_debit_credit: Decimal,
        max_gain: Decimal | None,
        breakeven_underlying: tuple[Decimal, ...],
        rationale: str,
        source_recipe_id: str,
    ) -> MultiLegProposal:
        """The ONLY constructor the producer should use.

        Copies ``admitted``/``bucket``/``reason``/``net_greeks``/``bpr_estimate``/
        ``max_loss`` straight off the ``OptionsGateResult`` so the proposal CANNOT
        carry a gate verdict that disagrees with the gate. This is the structural
        "the reactor consumes the gate, never bypasses it" guarantee (plan §8).

        It is also the ONLY seam authorized to mint a ``risk_gate_pass=True``
        proposal: it sets the module-private mint token for the duration of the
        ``cls(...)`` call so ``__post_init__`` accepts a passing verdict. Any other
        construction with ``risk_gate_pass=True`` raises (defense-in-depth, #38).

        Money fields off the gate (``bpr_estimate``, ``max_loss``) are coerced to
        ``Decimal`` (the gate stores them as float; the proposal is the money seam).
        """
        bpr = Decimal(str(gate_result.bpr_estimate))
        max_loss = (
            None if gate_result.max_loss is None else Decimal(str(gate_result.max_loss))
        )
        token = _minting.set(_GATE_MINTED)
        try:
            return cls(
                proposal_id=proposal_id,
                asof=asof,
                strategy_kind=strategy_kind,
                underlying=underlying,
                option_legs=tuple(option_legs),
                stock_leg=stock_leg,
                outer_qty=outer_qty,
                net_debit_credit=net_debit_credit,
                net_greeks=gate_result.net_greeks,
                bpr_estimate=bpr,
                max_loss=max_loss,
                max_gain=max_gain,
                breakeven_underlying=tuple(breakeven_underlying),
                rationale=rationale,
                source_recipe_id=source_recipe_id,
                risk_gate_pass=gate_result.admitted,
                risk_gate_bucket=gate_result.bucket.value,
                risk_gate_reason=gate_result.reason,
                risk_gate_warnings=tuple(gate_result.warnings),
            )
        finally:
            # Reset immediately so the mint authorization NEVER leaks to a later,
            # unrelated construction on this same context/thread.
            _minting.reset(token)

    def __post_init__(self) -> None:
        # Gate-pass is UNREPRESENTABLE except via from_gate_result (the mint seam).
        # A passing verdict can ONLY originate from the gate; a hand-built proposal
        # must never assert one. ``risk_gate_pass=False`` / rejected proposals build
        # freely (they are persisted for replay/audit; the reactor refuses to fill
        # them). This is belt-and-suspenders on top of the reactor's runtime
        # ``risk_gate_pass is not True`` refusal (ADR-0029 reactor-consumes-gate).
        # risk_gate_pass is typed bool; reject any non-bool (a truthy 1/"yes" must not
        # slip the lock below via `is True`). The reactor's runtime guard is also
        # `is not True`, so a non-bool would be refused at fill regardless — but we
        # forbid it at construction so the type invariant holds end-to-end.
        if not isinstance(self.risk_gate_pass, bool):
            raise TypeError(
                f"risk_gate_pass must be bool, got {type(self.risk_gate_pass).__name__}"
            )
        if self.risk_gate_pass is True and _minting.get() is not _GATE_MINTED:
            raise ValueError(
                "risk_gate_pass=True may only be set via "
                "MultiLegProposal.from_gate_result(); direct construction with a "
                "passing gate verdict is forbidden (ADR-0029 reactor-consumes-gate "
                "invariant)"
            )
        # Money MUST be Decimal (never float) — ADR-0029 D1 rationale.
        if not isinstance(self.net_debit_credit, Decimal):
            raise TypeError(
                f"net_debit_credit must be Decimal, got {type(self.net_debit_credit).__name__}"
            )
        if not isinstance(self.bpr_estimate, Decimal):
            raise TypeError(
                f"bpr_estimate must be Decimal, got {type(self.bpr_estimate).__name__}"
            )
        if self.max_loss is not None and not isinstance(self.max_loss, Decimal):
            raise TypeError(
                f"max_loss must be Decimal or None, got {type(self.max_loss).__name__}"
            )
        # Validate OCC parse on every option leg at construction (fail-closed):
        # a malformed OCC symbol must never reach the reactor's fill path.
        for leg in self.option_legs:
            parse_occ(leg.symbol)
