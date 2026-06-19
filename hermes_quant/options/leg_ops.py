"""hermes_quant.options.leg_ops — deterministic composite LEG-OPERATION rules (aegis-ml01).

The DECISION layer that lets AEGIS either execute / manage a multi-leg composite WHOLE, OR
break it apart and manage each leg — decompose / convert / single-leg risk-adjust — driving
the ``composite_plays`` state machine WITHOUT orphaning (ADR-0098 §H1-H4, operator decision
#5).

Three DECISION functions (deterministic, no LLM, no order placement here — they emit a
decision the React layer would execute):

  * ``decompose_decision``   — WHEN to break a composite into independently-managed legs:
        (a) a single leg breaches its OWN risk (a long wing's stop, a short leg's loss cap
            from ``options_exit``),
        (b) the composite THESIS is invalidated,
        (c) ASSIGNMENT looms on a short leg (DTE / delta / ITM — the ``options_exit`` Rule 4
            delta-breach / Rule 5 extrinsic-floor signals).
  * ``convert_decision``     — roll / leg-into another admissible ``StrategyKind`` (e.g. a
        bull-put-spread -> iron-condor by adding a call wing; roll a tested short out/down).
        ATOMICITY: a convert is either fully completed via one MLEG order, OR guarded so a
        half-applied convert can NOT strand a naked / undefined-risk leg (see ``apply_convert``
        — the add-leg half executes BEFORE any remove half so a failed add leaves the original
        defined-risk legs intact; the no-naked predicate is checked on the TARGET leg-set at
        decision time so a convert that would produce a naked side is rejected up front).
  * ``risk_adjust_decision`` — close / roll / hedge ONE leg without orphaning the rest. If the
        adjust would make a side NAKED (an uncovered short call / short put), it is REJECTED
        (the ``composite_has_naked_side`` no-naked guard).

The state transition through ``CompositePlaysStore`` is the side effect the ``apply_*``
helpers drive — open->partial (some legs still composite) or open->decomposed (all legs now
independent), NEVER an auto-close (the store's H1 guard forbids that).

POSTURE
-------
* DETERMINISTIC: the rules are pure functions of the supplied leg-set + per-leg signals +
  the composite thesis flag. The LLM never decides a leg op.
* DEFAULT-OFF: with ``HERMES_QUANT_COMPOSITE_LEG_OPS`` unset, every decision function returns
  a ``no_action`` no-op and the ``apply_*`` helpers drive NO transition — the composite is
  managed WHOLE (byte-identical to today). The flag is read at CALL time (never cached at
  import), mirroring the rest of the codebase.
* NO ORPHAN (H1, cardinal): a leg op must never leave a composite in 'open' with fewer active
  legs than expected. The ``apply_decompose`` helper transitions the composite through the
  store BEFORE / as part of the same caller step that closes the leg, and a partial decompose
  goes to 'partial' (NEVER auto-'closed').
* NO NAKED: a convert / risk-adjust that would strand a naked / undefined-risk short is
  REJECTED at decision time (``composite_has_naked_side``).
* FINITE-GUARD: the decision functions take structured signals (booleans / indices), not raw
  numerics, so the finite-guard discipline lives in the upstream ``options_exit`` core that
  produces those signals (ar08 family). Where a numeric does flow (none here directly), the
  source core has already guarded it.

References
----------
ADR-0098 Part B §"Decompose / convert / risk-adjust" + §H1-H4 hazards.
ADR-0099 Part A (``risk/options_exit.py``) — the per-leg / per-composite exit signals reused
here (delta-breach / extrinsic-floor / loss-cap) rather than duplicated.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_quant.options.data import OptionLeg, StockLeg
    from hermes_quant.risk.options_exit import OptionsExitDecision
    from hermes_quant.state.composite_plays import CompositePlaysStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flag (read at call time, never cached at import — mirrors options_gate / recipes).
# DEFAULT-OFF: absent => the composite is managed WHOLE => byte-identical.
# ---------------------------------------------------------------------------

COMPOSITE_LEG_OPS_FLAG = "HERMES_QUANT_COMPOSITE_LEG_OPS"


def leg_ops_enabled() -> bool:
    """True iff HERMES_QUANT_COMPOSITE_LEG_OPS == "1" (read at call time)."""
    return os.environ.get(COMPOSITE_LEG_OPS_FLAG, "0") == "1"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConvertExecutionError(RuntimeError):
    """Raised when the ADD-leg half of an atomic convert fails.

    The contract (ADR-0098 H4): the add-leg half executes BEFORE any remove half, so a
    failed add leaves the original defined-risk legs intact (nothing was removed, the
    composite was NOT transitioned). The React-layer caller catches this and leaves the
    composite managed-whole — never proceeding to remove a leg that would strand a naked
    side.
    """


# ---------------------------------------------------------------------------
# Per-leg risk signal (the adapter over options_exit verdicts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegRisk:
    """The per-leg risk signal a decompose decision consumes.

    Deliberately a STRUCTURED signal (booleans + index), not raw numerics: the numeric
    finite-guarding + threshold comparison lives in the ``options_exit`` core that produces
    the verdict (the ar08 family lives there). This keeps leg_ops a pure routing layer over
    those verdicts rather than a second copy of the exit thresholds.

    Attributes
    ----------
    leg_idx:
        0-based index of this leg within the composite's leg list.
    breaches_own_risk:
        True when this single leg breaches its OWN risk envelope — a long wing's stop, a
        short leg's loss cap (``options_exit`` Rule 2 loss_cap on the leg-as-standalone).
    assignment_looms:
        True when assignment looms on this (short) leg — DTE / delta / ITM, i.e. the
        ``options_exit`` Rule 4 delta-breach or Rule 5 extrinsic-floor verdict.
    """

    leg_idx: int
    breaches_own_risk: bool = False
    assignment_looms: bool = False

    @classmethod
    def from_exit_decision(
        cls, *, leg_idx: int, decision: OptionsExitDecision
    ) -> LegRisk:
        """Map an ``OptionsExitDecision`` (the tp3/tp1-2 core) into a leg-op signal.

        Reuses ``options_exit``'s verdicts rather than duplicating exit logic:
          * ``delta_breach`` / ``extrinsic_floor`` -> ``assignment_looms`` (short-leg
            assignment-risk signals).
          * ``loss_cap_2x`` -> ``breaches_own_risk`` (the leg breached its own loss cap).
          * ``time_close_21dte`` -> ``assignment_looms`` (a credit short approaching expiry
            is an assignment-risk / time-risk signal).
          * ``tp_50pct_credit`` / ``hold`` -> no decompose trigger (the composite is being
            taken-profit as a whole, or held — not a single-leg breach).
        """
        rule = decision.which_rule
        return cls(
            leg_idx=leg_idx,
            breaches_own_risk=(rule == "loss_cap_2x"),
            assignment_looms=(
                rule in ("delta_breach", "extrinsic_floor", "time_close_21dte")
            ),
        )


# ---------------------------------------------------------------------------
# No-naked predicate (the cardinal undefined-risk guard)
# ---------------------------------------------------------------------------


def composite_has_naked_side(legs: list[Any]) -> bool:
    """Return True iff the leg-set has a NAKED / undefined-risk short side.

    A short option leg (``side == "sell"``) is NAKED unless it is covered by EITHER:
      * a long option of the SAME right (C/P) — the defined-risk spread / wing pattern
        (e.g. a short put covered by a long put; a short call covered by a long call), OR
      * the underlying stock leg (a short CALL covered by long stock — the covered call /
        collar pattern; a short PUT is NOT covered by stock, only by cash collateral which
        is handled at the gate, so a short put here requires a long put to be defined-risk).

    This is the predicate ``convert_decision`` and ``risk_adjust_decision`` use to REJECT any
    leg op that would strand a naked side (ADR-0098 "no naked / undefined-risk leverage").
    It mirrors the iron-condor builder's no-naked assertion in ``options.recipes`` (exactly
    2 short + 2 long for a condor) generalized to any admissible structure.

    Convention: a short CALL is covered by long stock (qty > 0) OR a long call. A short PUT
    is covered only by a long put (cash-secured collateral is enforced at the gate, not here).
    """
    from hermes_quant.options.data import OptionLeg, StockLeg

    option_legs = [leg for leg in legs if isinstance(leg, OptionLeg)]
    has_long_stock = any(
        isinstance(leg, StockLeg) and leg.qty > 0 for leg in legs
    )

    short_calls = [leg for leg in option_legs if leg.side == "sell" and leg.right == "C"]
    long_calls = [leg for leg in option_legs if leg.side == "buy" and leg.right == "C"]
    short_puts = [leg for leg in option_legs if leg.side == "sell" and leg.right == "P"]
    long_puts = [leg for leg in option_legs if leg.side == "buy" and leg.right == "P"]

    # A short call is covered by a long call OR by long stock (covered call).
    # If there are MORE short calls than (long calls), and no stock cover, a call side is
    # naked (a ratio / uncovered short).
    if short_calls:
        call_cover = len(long_calls) + (1 if has_long_stock else 0)
        if len(short_calls) > call_cover:
            return True

    # A short put is covered ONLY by a long put (long stock does NOT cover a short put).
    if short_puts and len(short_puts) > len(long_puts):
        return True

    return False


# ---------------------------------------------------------------------------
# DECOMPOSE
# ---------------------------------------------------------------------------


def decompose_decision(
    *,
    legs: list[Any],
    leg_signals: list[LegRisk],
    thesis_invalidated: bool,
) -> dict[str, Any]:
    """Decide WHETHER to break a composite into independently-managed legs.

    DETERMINISTIC triggers (ADR-0098):
      (a) a single leg breaches its OWN risk (``LegRisk.breaches_own_risk``),
      (b) the composite THESIS is invalidated (``thesis_invalidated`` -> decompose ALL legs;
          the structure is no longer wanted as a combo),
      (c) ASSIGNMENT looms on a short leg (``LegRisk.assignment_looms``).

    DEFAULT-OFF: with the flag unset returns ``{decompose: False, ..., reason: "no_action"}``
    so the composite is managed WHOLE (byte-identical).

    Returns
    -------
    dict with keys:
      * ``decompose`` (bool): True iff any trigger fired.
      * ``legs_to_independently_manage`` (list[int]): 0-based leg indices to break out. On a
        thesis invalidation this is EVERY leg; otherwise just the legs whose own signal fired.
      * ``reason`` (str): human-readable trigger reason ("no_action" when flag-off).
    """
    if not leg_ops_enabled():
        return {
            "decompose": False,
            "legs_to_independently_manage": [],
            "reason": "no_action",
        }

    # (b) Thesis invalidation: decompose the WHOLE structure (every leg goes independent).
    if thesis_invalidated:
        return {
            "decompose": True,
            "legs_to_independently_manage": list(range(len(legs))),
            "reason": "composite thesis invalidated -> decompose all legs",
        }

    # (a)/(c) Per-leg triggers: break out exactly the legs whose own risk breached or whose
    # assignment looms. Deterministic ascending order.
    breach_idxs = sorted(
        sig.leg_idx for sig in leg_signals if sig.breaches_own_risk
    )
    assign_idxs = sorted(
        sig.leg_idx for sig in leg_signals if sig.assignment_looms
    )
    to_manage = sorted(set(breach_idxs) | set(assign_idxs))

    if not to_manage:
        return {
            "decompose": False,
            "legs_to_independently_manage": [],
            "reason": "no leg breached its own risk; no assignment looms; thesis intact -> HOLD",
        }

    reason_parts: list[str] = []
    if breach_idxs:
        reason_parts.append(f"leg(s) {breach_idxs} breach own risk")
    if assign_idxs:
        reason_parts.append(f"assignment looms on short leg(s) {assign_idxs}")
    return {
        "decompose": True,
        "legs_to_independently_manage": to_manage,
        "reason": "; ".join(reason_parts),
    }


def apply_decompose(
    *,
    store: CompositePlaysStore,
    multi_leg_id: str,
    decision: dict[str, Any],
    legs_remaining_after: int,
) -> str:
    """Drive the composite_plays transition for a decompose decision (the side effect).

    NO-ORPHAN (H1): when the decompose decision is to break out some-but-not-all legs
    (``legs_remaining_after > 0``), the composite transitions open->partial (NEVER
    auto-closed). When ALL legs become independent (``legs_remaining_after == 0``), the
    composite transitions to 'decomposed'. Either way the composite leaves 'open', so a
    subsequent ``detect_orphan`` (which only flags 'open' composites short of legs) returns
    False.

    DEFAULT-OFF: with the flag unset (or ``decision["decompose"] is False``) NO transition is
    driven — the store is untouched and the current state is returned (the composite stays
    managed-whole, byte-identical).

    Parameters
    ----------
    legs_remaining_after:
        Number of legs that REMAIN part of the composite AFTER this decompose step (i.e. not
        yet broken out / closed). 0 => all legs independent => 'decomposed'. >0 => 'partial'.

    Returns
    -------
    str
        The composite's state after the (possible) transition.
    """
    if not leg_ops_enabled() or not decision.get("decompose"):
        row = store.get(multi_leg_id)
        return row.state if row is not None else ""

    if legs_remaining_after <= 0:
        # All legs now independent -> the composite is fully decomposed.
        store.transition_state(multi_leg_id, target_state="decomposed")
    else:
        # Some legs still composite -> H1 partial (NEVER auto-close). record_leg_close with
        # is_decompose=True drives open->partial and keeps a partial composite partial on
        # subsequent calls (the store's _compute_target_state H1 invariant).
        store.record_leg_close(
            multi_leg_id, is_decompose=True, legs_remaining=legs_remaining_after
        )

    row = store.get(multi_leg_id)
    return row.state if row is not None else ""


# ---------------------------------------------------------------------------
# CONVERT
# ---------------------------------------------------------------------------


def convert_decision(
    *,
    current_legs: list[Any],
    target_structure: str,
    legs_to_add: list[Any],
    legs_to_remove: list[str],
    reason: str,
) -> dict[str, Any]:
    """Decide WHETHER to convert (roll / leg-into another structure).

    A convert rolls or legs-into another admissible ``StrategyKind`` (e.g. bull-put-spread ->
    iron-condor by adding a call wing; roll a tested short out/down). The decision computes
    the RESULTING leg-set (current - removed + added) and REJECTS the convert if that target
    would have a naked / undefined-risk side (``composite_has_naked_side``) — a convert must
    never be the path that creates a naked leg.

    DEFAULT-OFF: flag unset -> ``{convert: False, ..., reason: "no_action"}``.

    Returns
    -------
    dict with keys:
      * ``convert`` (bool)
      * ``target_structure`` (str)
      * ``legs_to_add`` (list)  / ``legs_to_remove`` (list[str OCC symbols])
      * ``reason`` (str)
    """
    if not leg_ops_enabled():
        return {
            "convert": False,
            "target_structure": target_structure,
            "legs_to_add": [],
            "legs_to_remove": [],
            "reason": "no_action",
        }

    # Compute the TARGET leg-set: current minus removed (by OCC symbol) plus added.
    remove_set = set(legs_to_remove)
    surviving = [leg for leg in current_legs if _leg_symbol(leg) not in remove_set]
    target_legs = surviving + list(legs_to_add)

    # NO-NAKED: a convert that would strand a naked / undefined-risk side is rejected.
    if composite_has_naked_side(target_legs):
        return {
            "convert": False,
            "target_structure": target_structure,
            "legs_to_add": [],
            "legs_to_remove": [],
            "reason": (
                f"REJECT convert to {target_structure}: target leg-set has a NAKED / "
                f"undefined-risk side (no_naked guard)"
            ),
        }

    return {
        "convert": True,
        "target_structure": target_structure,
        "legs_to_add": list(legs_to_add),
        "legs_to_remove": list(legs_to_remove),
        "reason": reason,
    }


def apply_convert(
    *,
    store: CompositePlaysStore,
    multi_leg_id: str,
    decision: dict[str, Any],
    current_legs: list[Any],
    add_executor: Callable[[list[Any]], None],
    remove_executor: Callable[[list[str]], None] | None = None,
) -> str:
    """Drive an ATOMIC convert (ADR-0098 H4).

    ATOMICITY contract: the ADD-leg half executes FIRST (via ``add_executor``). Only if the
    add SUCCEEDS does the remove half run (via ``remove_executor``). This ordering guarantees
    that a failed add (``add_executor`` raises) leaves the ORIGINAL defined-risk legs intact
    and the composite UNCHANGED — a half-applied convert can NEVER strand a naked /
    undefined-risk leg because the protective add always lands before any protective remove
    is attempted. On an add failure we raise ``ConvertExecutionError`` WITHOUT mutating the
    store; the React-layer caller leaves the composite managed-whole.

    Prefer the broker's single MLEG order class for true atomicity where the broker supports
    it; when it does not, this add-before-remove guard is the fallback (the H1 partial-state
    is the further fallback if the remove half itself fails after a successful add, which the
    caller handles by leaving the composite 'partial').

    DEFAULT-OFF: flag unset (or ``decision["convert"] is False``) -> no execution, no
    transition; returns the current state.

    Returns
    -------
    str
        The composite's state after the convert (unchanged on the flag-off / no-op paths).

    Raises
    ------
    ConvertExecutionError
        If the ADD-leg half fails. The store is NOT mutated (atomic rollback by construction:
        nothing was removed, nothing transitioned).
    """
    if not leg_ops_enabled() or not decision.get("convert"):
        row = store.get(multi_leg_id)
        return row.state if row is not None else ""

    legs_to_add = decision.get("legs_to_add", [])
    legs_to_remove = decision.get("legs_to_remove", [])

    # ADD half FIRST (defined-risk protective legs land before any removal).
    try:
        add_executor(legs_to_add)
    except Exception as exc:  # noqa: BLE001 — re-wrapped as the convert-specific error
        # Atomic by construction: nothing removed, nothing transitioned. Fail-CLOSED.
        logger.warning(
            "leg_ops.apply_convert: ADD half failed for %s -> %s convert; composite "
            "left UNCHANGED (no naked leg stranded): %s",
            multi_leg_id,
            decision.get("target_structure"),
            exc,
        )
        raise ConvertExecutionError(
            f"convert ADD half failed for {multi_leg_id}; composite left unchanged"
        ) from exc

    # REMOVE half only AFTER a successful add.
    if remove_executor is not None and legs_to_remove:
        remove_executor(legs_to_remove)

    # A convert that legs-INTO a new structure keeps the composite OPEN under its new
    # strategy_kind (the legs were rolled, not closed). The store's strategy_kind is a
    # descriptive field, not a transition; we do not force a state move here. The React layer
    # may open a NEW composite row for the converted structure if it tracks it separately.
    row = store.get(multi_leg_id)
    return row.state if row is not None else ""


# ---------------------------------------------------------------------------
# RISK-ADJUST (single-leg, no-naked)
# ---------------------------------------------------------------------------


def risk_adjust_decision(
    *,
    current_legs: list[Any],
    leg_symbol: str,
    action: Literal["close", "roll", "hedge"],
    reason: str,
) -> dict[str, Any]:
    """Decide a SINGLE-LEG risk adjustment (close / roll / hedge) WITHOUT orphaning the rest.

    NO-NAKED guard: simulate the resulting leg-set after the adjust. A ``close`` removes the
    leg; a ``roll`` replaces it with itself (same right/side, different strike/expiry — the
    coverage relationship is preserved, so it never newly-nakeds a side); a ``hedge`` ADDS a
    protective leg (never removes). If the resulting set has a naked / undefined-risk side
    (``composite_has_naked_side``), the adjust is REJECTED (``action == "reject"``).

    DEFAULT-OFF: flag unset -> ``{action: "no_action", ...}``.

    Returns
    -------
    dict with keys:
      * ``action`` (str): the requested action, or "reject" (no-naked) / "no_action"
        (flag-off).
      * ``leg`` (str): the OCC symbol of the leg adjusted.
      * ``reason`` (str)
    """
    if not leg_ops_enabled():
        return {"action": "no_action", "leg": leg_symbol, "reason": "no_action"}

    if action == "close":
        # Simulate removing the leg.
        resulting = [leg for leg in current_legs if _leg_symbol(leg) != leg_symbol]
        if composite_has_naked_side(resulting):
            return {
                "action": "reject",
                "leg": leg_symbol,
                "reason": (
                    f"REJECT close of {leg_symbol}: would leave a NAKED / undefined-risk "
                    f"side (no_naked guard) — closing a protective leg orphans the short it "
                    f"covers"
                ),
            }
    # A 'roll' preserves the same right/side coverage relationship (replace-in-place); a
    # 'hedge' only ADDS protection. Neither can newly-naked a side, so they are allowed
    # provided the CURRENT set is already defined-risk. Defense-in-depth: if the current set
    # is somehow already naked, a hedge is the correct response (allow it); a roll/close on
    # an already-naked set still must not be the thing that creates a naked side — handled by
    # the close-branch simulation above.

    return {"action": action, "leg": leg_symbol, "reason": reason}


def apply_risk_adjust(
    *,
    store: CompositePlaysStore,
    multi_leg_id: str,
    decision: dict[str, Any],
    legs_remaining_after: int,
) -> str:
    """Drive the composite_plays side effect for a risk-adjust decision.

    A single-leg risk adjust is, by design, the operation that keeps the composite VALID and
    managed-whole — so on a ``roll`` / ``hedge`` (which preserve the leg count / coverage)
    NO transition is driven and the composite stays in its current state. On a confirmed
    ``close`` of one leg that leaves OTHER legs still composite, the composite transitions
    open->partial via the store's H1 guard (NEVER auto-closed) so a closed leg never orphans
    the composite (``legs_remaining_after > 0`` => 'partial'; ``== 0`` would mean the whole
    structure was unwound, handled by the store as a direct close).

    DEFAULT-OFF / REJECTED: flag unset, ``action == "no_action"``, or a REJECTED adjust
    (``action == "reject"``, the no-naked guard fired) drives NO transition; returns the
    current state.

    Returns
    -------
    str
        The composite's state after the (possible) transition.
    """
    action = decision.get("action")
    if not leg_ops_enabled() or action in (None, "no_action", "reject"):
        row = store.get(multi_leg_id)
        return row.state if row is not None else ""

    if action == "close" and legs_remaining_after > 0:
        # Closing ONE leg of a still-multi-leg composite -> H1 partial (NEVER auto-close).
        store.record_leg_close(
            multi_leg_id, is_decompose=True, legs_remaining=legs_remaining_after
        )

    # roll / hedge keep the composite intact (managed-whole) -> no transition.
    row = store.get(multi_leg_id)
    return row.state if row is not None else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _leg_symbol(leg: Any) -> str:
    """The identity symbol for a leg (OCC-21 for option legs; underlying for stock legs)."""
    sym = getattr(leg, "symbol", None)
    if sym is not None:
        return sym
    # StockLeg has no `symbol`; use its underlying (stock legs are never removed by OCC).
    return getattr(leg, "underlying", "")
