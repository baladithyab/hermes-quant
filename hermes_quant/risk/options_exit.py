"""hermes_quant.risk.options_exit — options composite-exit decision core (ADR-0099 Part A).

This is the OPTIONS analogue of ``per_position_stop.evaluate_stop``: given a composite
options position's fresh market state, decide whether to close the position, and on which
rule. PURE (no I/O, no clock, no env). The tick's caller refreshes the inputs each tick from
LIVE leg prices before calling evaluate_options_exit — this core takes FRESH inputs, never a
stale snapshot.

Five rules, evaluated in priority order (the 2x-loss cap fires BEFORE the TP check, as
the "hard rule"):

  Rule 1  — 50%-of-credit TP:  close when net MtM gain >= 0.50 * initial_credit.
              A NaN/non-finite pnl or credit -> HOLD (silence-by-default).

  Rule 2  — 2x-credit HARD loss cap (fail-CLOSED):  close when net loss >= 2.0 * initial_credit.
              This fires BEFORE the TP check (hard rule, prevents structural max-loss wings
              from clearing). A NaN/non-finite pnl -> CLOSE (fail-CLOSED, not HOLD, because
              a non-computable loss on an options position is the SAFE exit direction — we
              cannot determine whether the position is safe).
              Note: for the loss cap, a NaN credit means we cannot compute 2*credit; we
              fail-CLOSED (close) to be safe.

  Rule 3  — 21-DTE time close: force-close when DTE <= 21 AND the position was entered
              at 40-45 DTE (i.e. the DTE range guard is already baked into the caller's
              composite state). Also tightens the short-delta breach threshold from 0.40 ->
              0.30 inside 21 DTE.

  Rule 4  — Delta-breach: close when any short-leg |delta| exceeds the threshold.
              Threshold: 0.40 outside 21 DTE, 0.30 inside 21 DTE (Rule 3 tightening).
              A NaN delta on a short leg -> CLOSE (fail-CLOSED: a non-finite delta cannot
              pass the guard, and a non-finite short delta is a structural risk signal).

  Rule 5  — Extrinsic-value floor: close a short leg whose extrinsic <= $0.10 OR
              <= 5% of original credit (assignment prevention). This fires when any short
              leg meets the condition. A NaN extrinsic -> CLOSE for that leg (fail-CLOSED:
              a non-computable extrinsic on a short leg is an assignment-risk signal).

POSTURE: DEFAULT-OFF (pure module; live wiring is a later main-thread step). All thresholds
are EVAL-GATE-PENDING constants (ADR-0099 open_calibrations). Finite-guard every numeric
input — NaN/inf defeats every <= gate (the ar08 family). For the loss-cap and delta-breach,
NaN -> fail-CLOSED (close). For the TP, NaN -> HOLD (silence-by-default: a NaN gain must
not trigger a premature profit take).

The inputs ``net_pnl`` and ``initial_credit`` are SIGNED P&L dollars on the NET composite:
  * ``net_pnl > 0``  => the structure is profitable (a credit spread received premium and
    has appreciated in value, i.e. the net position mark is now less than the credit received)
  * ``net_pnl < 0``  => the structure is losing (the net mark has increased beyond the
    initial credit received)
  * ``initial_credit > 0`` => the structure opened for a credit (the common case for
    CC/CSP/credit-spread; a debit structure would have initial_credit <= 0, in which case
    the TP and loss-cap rules are skipped as they apply to CREDIT structures only — see rule
    implementation notes below).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# EVAL-GATE-PENDING threshold constants (ADR-0099 open_calibrations).
# All threshold values here are conservative starting points pending the
# options evidence window (aegis-agoptev1). Do NOT change these constants
# without an eval gate showing the new value improves outcomes.
# ---------------------------------------------------------------------------

# Rule 1: take-profit at 50% of initial credit received.
TP_CREDIT_FRACTION = 0.50
"""Close the composite when net MtM gain >= 50% of initial credit. EVAL-GATE-PENDING."""

# Rule 2: hard loss cap at 2x initial credit.
LOSS_CAP_CREDIT_MULTIPLE = 2.0
"""Close the composite when net loss >= 2x initial credit. Fail-CLOSED hard rule. EVAL-GATE-PENDING."""

# Rule 3: time-based close at 21 DTE.
TIME_CLOSE_DTE = 21
"""Force-close at <= 21 DTE for 40-45 DTE entries. EVAL-GATE-PENDING."""

# Rule 4: delta-breach thresholds.
DELTA_BREACH_OUTSIDE_21DTE = 0.40
"""Short-leg |delta| breach threshold outside 21 DTE. EVAL-GATE-PENDING."""

DELTA_BREACH_INSIDE_21DTE = 0.30
"""Short-leg |delta| breach threshold inside 21 DTE (tighter). EVAL-GATE-PENDING."""

# Rule 5: extrinsic-value floor.
EXTRINSIC_FLOOR_DOLLARS = 0.10
"""Close a short leg with extrinsic <= $0.10 (assignment prevention). EVAL-GATE-PENDING."""

EXTRINSIC_FLOOR_PCT_OF_CREDIT = 0.05
"""Close a short leg with extrinsic <= 5% of original credit. EVAL-GATE-PENDING."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

ExitRule = Literal[
    "tp_50pct_credit",
    "loss_cap_2x",
    "time_close_21dte",
    "delta_breach",
    "extrinsic_floor",
    "hold",
]


@dataclass(frozen=True)
class ShortLegState:
    """The per-short-leg state the caller supplies each tick.

    All values from LIVE prices refreshed by the caller — never a stale snapshot
    (ADR-0099 H5: greeks refreshed from live leg prices).

    ``delta``: the option's per-unit delta (positive for calls, negative for puts
      as returned by the BSM/provider).  The caller passes the RAW greeks value
      and this core takes |delta|.
    ``extrinsic_value``: current extrinsic (time) value in dollars per share
      (i.e. mid - max(mid - (spot - strike), 0) for a call, or the provider
      value; must be >= 0 for a valid short leg).  None means unknown -> fail-CLOSED.
    """

    delta: float | None  # raw per-unit delta; None -> fail-CLOSED on delta-breach check
    extrinsic_value: float | None  # USD per share; None -> fail-CLOSED on extrinsic check


@dataclass(frozen=True)
class OptionsExitDecision:
    """The composite-level options exit verdict for ONE composite position.

    ``should_close`` is True when any exit rule fires.
    ``reason`` is the human-readable exit reason string.
    ``which_rule`` identifies the exit rule (or "hold" if no rule fires).
    ``firing_leg_idx`` is the index (0-based) of the short leg that triggered Rule 4/5,
      or None for Rule 1/2/3 (composite-level rules).
    """

    should_close: bool
    reason: str
    which_rule: ExitRule
    firing_leg_idx: int | None = field(default=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_finite(x: float | None) -> bool:
    """True iff x is a float and math.isfinite(x)."""
    return x is not None and isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _loss_amount(net_pnl: float) -> float:
    """Return the loss as a POSITIVE dollar amount (0 when net_pnl >= 0)."""
    return max(-net_pnl, 0.0)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def evaluate_options_exit(
    *,
    net_pnl: float,
    initial_credit: float,
    dte: int | float,
    short_legs: list[ShortLegState],
    # Override thresholds (caller can pass explicit values; None = use module defaults).
    tp_credit_fraction: float = TP_CREDIT_FRACTION,
    loss_cap_multiple: float = LOSS_CAP_CREDIT_MULTIPLE,
    time_close_dte: int = TIME_CLOSE_DTE,
    delta_breach_outside: float = DELTA_BREACH_OUTSIDE_21DTE,
    delta_breach_inside: float = DELTA_BREACH_INSIDE_21DTE,
    extrinsic_floor_dollars: float = EXTRINSIC_FLOOR_DOLLARS,
    extrinsic_floor_pct: float = EXTRINSIC_FLOOR_PCT_OF_CREDIT,
) -> OptionsExitDecision:
    """Decide whether to close a composite options position.

    Evaluates five rules in priority order (Rule 2 hard loss cap BEFORE Rule 1 TP).
    Returns the FIRST firing rule. Returns ``which_rule="hold"`` when none fire.

    CALLER CONTRACT: supply FRESH inputs refreshed from live leg prices each tick.
    This core never reads market data — it is purely a decision function over
    the caller-supplied state.

    Args:
        net_pnl: NET P&L on the composite in dollars. Positive = profitable
            (e.g. a credit structure whose net mark is now < initial credit).
            Negative = loss (net mark has grown beyond initial credit).
        initial_credit: net credit received when the composite was opened, in
            dollars. Must be > 0 for Rule 1 (TP) and Rule 2 (loss cap) to apply.
            For debit structures (initial_credit <= 0) these two rules skip
            (they are credit-structure rules). Finite-guard: NaN/non-finite credit
            with net_pnl < 0 -> fail-CLOSED on Rule 2.
        dte: days-to-expiry of the shortest-dated leg (or the composite's DTE).
            Integer preferred; float accepted. NaN/non-finite -> Rule 3 fires
            (fail-CLOSED: unknown DTE on a credit structure is a time-risk signal).
        short_legs: list of ShortLegState, one per short option leg in the
            composite (e.g. [short_put] for a CSP, [short_call, short_put] for
            an iron condor). May be empty if the structure has no short legs
            (Rules 4 and 5 are then skipped — a pure long structure has no
            assignment or delta-breach risk from short legs). Each leg's
            delta and extrinsic_value are FRESH market values.
        tp_credit_fraction: TP fraction of initial credit (default 0.50).
        loss_cap_multiple: loss cap multiple of initial credit (default 2.0).
        time_close_dte: DTE threshold for time-based close (default 21).
        delta_breach_outside: short |delta| cap outside time_close_dte (default 0.40).
        delta_breach_inside: short |delta| cap inside time_close_dte (default 0.30).
        extrinsic_floor_dollars: extrinsic value floor in dollars (default $0.10).
        extrinsic_floor_pct: extrinsic floor as % of initial credit (default 5%).

    Returns:
        OptionsExitDecision with should_close, reason, which_rule, firing_leg_idx.
    """

    # Finite-guard override thresholds (ar08 family: a non-finite/<=0 threshold
    # must NOT silently disable the rail — fall back to the safe module default).
    if not _is_finite(tp_credit_fraction) or tp_credit_fraction <= 0.0:
        tp_credit_fraction = TP_CREDIT_FRACTION
    if not _is_finite(loss_cap_multiple) or loss_cap_multiple <= 0.0:
        loss_cap_multiple = LOSS_CAP_CREDIT_MULTIPLE
    if not isinstance(time_close_dte, int) or time_close_dte < 0:
        # wave3-review FIX: a non-int OR NEGATIVE time_close_dte -> the module default.
        # The prior code did `int(time_close_dte) if _is_finite(...)` — but a negative int
        # IS finite, so int(-1)=-1 was kept, DISABLING Rule 3 (the time-close rail) instead
        # of restoring the 21-DTE default (a negative override silently disarmed a safety
        # rule — fail-OPEN). Always fall back to TIME_CLOSE_DTE on an invalid value.
        time_close_dte = TIME_CLOSE_DTE
    if not _is_finite(delta_breach_outside) or delta_breach_outside <= 0.0:
        delta_breach_outside = DELTA_BREACH_OUTSIDE_21DTE
    if not _is_finite(delta_breach_inside) or delta_breach_inside <= 0.0:
        delta_breach_inside = DELTA_BREACH_INSIDE_21DTE
    if not _is_finite(extrinsic_floor_dollars) or extrinsic_floor_dollars < 0.0:
        extrinsic_floor_dollars = EXTRINSIC_FLOOR_DOLLARS
    if not _is_finite(extrinsic_floor_pct) or extrinsic_floor_pct < 0.0:
        extrinsic_floor_pct = EXTRINSIC_FLOOR_PCT_OF_CREDIT

    # Determine whether we are inside the 21-DTE window.
    # NaN/non-finite DTE -> fail-CLOSED (Rule 3): we cannot determine how much time
    # remains; for a credit structure that is a time-risk signal.
    dte_finite = _is_finite(dte) if isinstance(dte, float) else (isinstance(dte, int) and not math.isnan(dte) if hasattr(math, 'isnan') else True)
    # Robust DTE finiteness check:
    try:
        dte_val = float(dte)
        dte_ok = math.isfinite(dte_val)
    except (TypeError, ValueError):
        dte_ok = False
        dte_val = float("nan")

    inside_21dte = dte_ok and dte_val <= time_close_dte

    # Active delta-breach threshold (tighter inside 21 DTE).
    active_delta_thr = delta_breach_inside if inside_21dte else delta_breach_outside

    # ---- Rule 2 (HARD loss cap, evaluated FIRST, fail-CLOSED on NaN). ----
    # Fire BEFORE Rule 1 TP: the 2x-loss-cap is the hard rule and fires even
    # if the credit is non-finite (we fail-CLOSED in that case too).
    if not _is_finite(net_pnl):
        # A non-finite P&L on a credit structure is a structural risk signal.
        # Fail-CLOSED: close to prevent runaway loss exposure.
        return OptionsExitDecision(
            should_close=True,
            reason="net_pnl non-finite -> fail-CLOSED (Rule 2)",
            which_rule="loss_cap_2x",
        )
    if not _is_finite(initial_credit) or initial_credit <= 0.0:
        # Credit is non-finite or zero/negative (debit structure or bad input).
        # Rule 2 only applies to credit structures (initial_credit > 0).
        # For debit structures, skip Rule 2.
        # But for non-finite credit with a LOSS, we fail-CLOSED.
        if not _is_finite(initial_credit) and net_pnl < 0.0:
            return OptionsExitDecision(
                should_close=True,
                reason="initial_credit non-finite with net_pnl loss -> fail-CLOSED (Rule 2)",
                which_rule="loss_cap_2x",
            )
        # Debit structure (initial_credit <= 0) or zero-credit: skip Rules 1+2.
        pass
    else:
        # initial_credit is finite and > 0: apply the loss cap.
        loss = _loss_amount(net_pnl)
        cap = loss_cap_multiple * initial_credit
        if loss >= cap:
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"net loss ${loss:.2f} >= {loss_cap_multiple:.1f}x credit "
                    f"${initial_credit:.2f} = ${cap:.2f} (hard Rule 2)"
                ),
                which_rule="loss_cap_2x",
            )

    # ---- Rule 1 (50%-of-credit TP). ----
    # NaN net_pnl was caught above (Rule 2). Here net_pnl is finite.
    # Only applies to credit structures (initial_credit finite and > 0).
    if _is_finite(initial_credit) and initial_credit > 0.0:
        tp_threshold = tp_credit_fraction * initial_credit
        if net_pnl >= tp_threshold:
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"net gain ${net_pnl:.2f} >= {tp_credit_fraction*100:.0f}% of "
                    f"initial credit ${initial_credit:.2f} = ${tp_threshold:.2f} (Rule 1 TP)"
                ),
                which_rule="tp_50pct_credit",
            )

    # wave3-review FIX (rule ordering — make the inside-21DTE tighter delta reachable):
    # The DTE-non-finite fail-CLOSED stays HERE (a credit structure with unknown time
    # remaining is closed on time risk regardless of delta). But the 21-DTE CALENDAR close
    # is moved to AFTER Rule 4/5 — because a delta breach (assignment risk) or an extrinsic
    # collapse is MORE urgent than the calendar, and a leg breaching INSIDE the 21-DTE
    # window must be evaluated against the TIGHTER delta_breach_inside threshold rather than
    # pre-empted by the time close (which made DELTA_BREACH_INSIDE_21DTE dead code). The
    # inside-window state is already computed (inside_21dte) and used by active_delta_thr.
    if not dte_ok:
        # DTE is non-finite/unknown -> fail-CLOSED (time risk for credit structures).
        return OptionsExitDecision(
            should_close=True,
            reason="DTE non-finite/unknown -> fail-CLOSED on time risk (Rule 3)",
            which_rule="time_close_21dte",
        )

    # ---- Rule 4 (delta-breach). Checked BEFORE the calendar time close so an
    # inside-21DTE leg fires on the tighter delta_breach_inside threshold. ----
    for idx, leg in enumerate(short_legs):
        delta = leg.delta
        if delta is None or not _is_finite(delta):
            # Non-finite delta on a short leg -> fail-CLOSED.
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"short leg[{idx}] delta is non-finite/None -> "
                    f"fail-CLOSED on delta risk (Rule 4)"
                ),
                which_rule="delta_breach",
                firing_leg_idx=idx,
            )
        if abs(delta) > active_delta_thr:
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"short leg[{idx}] |delta| {abs(delta):.4f} > "
                    f"threshold {active_delta_thr:.2f} "
                    f"({'inside' if inside_21dte else 'outside'} {time_close_dte} DTE, Rule 4)"
                ),
                which_rule="delta_breach",
                firing_leg_idx=idx,
            )

    # ---- Rule 5 (extrinsic-value floor, assignment prevention). ----
    # Compute the extrinsic floor dollar amount: max($0.10, 5% of initial_credit).
    # If initial_credit is non-finite or <= 0, use only the dollar floor.
    if _is_finite(initial_credit) and initial_credit > 0.0:
        pct_floor = extrinsic_floor_pct * initial_credit
        active_extrinsic_floor = max(extrinsic_floor_dollars, pct_floor)
    else:
        active_extrinsic_floor = extrinsic_floor_dollars

    for idx, leg in enumerate(short_legs):
        ext = leg.extrinsic_value
        if ext is None or not _is_finite(ext):
            # Non-finite extrinsic on a short leg -> fail-CLOSED (assignment risk).
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"short leg[{idx}] extrinsic_value is non-finite/None -> "
                    f"fail-CLOSED on assignment risk (Rule 5)"
                ),
                which_rule="extrinsic_floor",
                firing_leg_idx=idx,
            )
        if ext <= active_extrinsic_floor:
            return OptionsExitDecision(
                should_close=True,
                reason=(
                    f"short leg[{idx}] extrinsic ${ext:.4f} <= "
                    f"floor ${active_extrinsic_floor:.4f} "
                    f"(${extrinsic_floor_dollars:.2f} or "
                    f"{extrinsic_floor_pct*100:.0f}% of credit ${initial_credit if _is_finite(initial_credit) else 'N/A'}, "
                    f"Rule 5 assignment prevention)"
                ),
                which_rule="extrinsic_floor",
                firing_leg_idx=idx,
            )

    # ---- Rule 3 (21-DTE CALENDAR time close) — evaluated LAST among the close rules
    # (wave3-review reorder): the calendar close is the DEFAULT action at the DTE
    # threshold, applied only after the more-urgent delta/extrinsic assignment-risk rules
    # have NOT fired. This lets an inside-21DTE leg be judged on the tighter delta
    # (delta_breach_inside) first; if no leg breached, the calendar still force-closes the
    # structure at <= time_close_dte. ----
    if inside_21dte:
        return OptionsExitDecision(
            should_close=True,
            reason=f"DTE {dte_val:.0f} <= {time_close_dte} (Rule 3 calendar time close)",
            which_rule="time_close_21dte",
        )

    # ---- No rule fired -> HOLD. ----
    return OptionsExitDecision(
        should_close=False,
        reason="no exit rule fired -> HOLD",
        which_rule="hold",
    )
