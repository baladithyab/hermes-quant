"""hermes_quant.admissibility.gate_order — the SINGLE reusable pre-trade gate seam (ADR-0077 / ADR-0079).

ADR-0079 names admissibility a **REACTION-layer fidelity gate** that runs as a hard PRECONDITION.
Today it is wired only at the autonomous-tick decision seam (`autonomous.py`). The daily-interim
brief, the HITL `quant_approve` path, and `PaperReactor.execute` are NOT admissibility-aware, so
with `HERMES_QUANT_ADMISSIBILITY=1` an inadmissible short can still execute on paper.

This module factors the autonomous seam's logic into ONE pure function — `admit_or_reject` — so
the brief, the HITL path, and the reactor can all call the SAME deterministic, fail-closed,
REJECT-only seam instead of each re-implementing the oracle + share-conversion + verdict dance.

Rails (identical to ADR-0077):
  * REJECT-only / flatten-only. It can ONLY shrink a target (REJECT -> 0.0). It NEVER amplifies,
    widens, forces, or flips a side. `abs(adjusted) <= abs(target_pct)` always
    (asserted in `apply_verdict_to_target`).
  * FAIL-CLOSED. A missing / None / unresolvable trade-affecting input (no oracle decision, no NAV,
    no price, fractional share, unknown shortability) => REJECT, never assume-safe.
  * Default-OFF. `select_oracle()` returns the `NullShortabilityOracle` (everything ACCEPTED,
    bit-for-bit today) unless `HERMES_QUANT_ADMISSIBILITY=1`. Callers MUST themselves stay behind
    the flag for a true no-op; this function honors the flag THROUGH `select_oracle()`, so calling
    it with the flag OFF yields ACCEPTED for every input.
  * Pure. No I/O, no env writes, no execution-bus writes. NAV / price / asof are passed in by the
    caller (the reactor, brief, and autonomous seam each already resolve these the same way).

Longs / buys (target_pct >= 0) are never constrained by the short-admissibility predicate — the
oracle short-circuits them to ACCEPTED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .oracle import (
    AdmissibilityContext,
    AdmissibilityState,
    ShortabilityVerdict,
    select_oracle,
)
from .order_state import (
    Side,
    apply_verdict_to_target,
    side_of,
    target_pct_to_shares,
)


@dataclass(frozen=True)
class AdmissibilityVerdict:
    """The unified result every caller (reactor / brief / HITL / autonomous) reads.

    `admitted` is the single boolean the caller branches on: True => proceed with
    `adjusted_target_pct` (== `target_pct` for an admissible order); False => do NOT
    execute (a REJECT-only no-fill). `reason` / `state` / `qty_shares` carry the
    audit detail (the same fields the autonomous seam already records).
    """

    admitted: bool
    original_target_pct: float
    adjusted_target_pct: float
    side: Side
    qty_shares: int
    state: AdmissibilityState
    reason: str | None
    annual_cbr: float

    @property
    def rejected(self) -> bool:
        return not self.admitted


def admit_or_reject(
    symbol: str,
    side: str,
    target_pct: float,
    nav: float | None,
    price: float | None,
    asof: datetime,
    *,
    existing_position_qty: float = 0.0,
    account_equity: float | None = None,
    available_bp: float | None = None,
) -> AdmissibilityVerdict:
    """The ONE pre-trade admissibility seam — pure, fail-closed, REJECT-only.

    Wraps `select_oracle()` (flag-honoring factory) + `target_pct_to_shares()`
    (NAV-fraction -> whole-share UNIT BRIDGE) + `oracle.verdict()` +
    `apply_verdict_to_target()` (REJECT/flatten-only adjuster). Reuses the existing
    ADR-0077 primitives verbatim — no logic is duplicated.

    Args:
        symbol: the ticker (e.g. "GME").
        side: "short"/"sell_short"/"ss" => constrained; anything else => LONG/BUY,
            always ACCEPTED. We derive the side from BOTH the explicit `side` arg AND
            the sign of `target_pct`; a SHORT requires either to indicate short so a
            caller can't accidentally route a negative target through the long path.
        target_pct: signed NAV fraction (e.g. -0.20 = 20% NAV short).
        nav: account NAV (USD) used ONLY for the NAV-fraction -> whole-share UNIT
            BRIDGE. None / non-positive => fail-closed (0 shares -> REJECT).
        price: decision/quote price the order would fill at. None / non-positive =>
            fail-closed (0 shares + no quote -> REJECT).
        asof: decision time (UTC) — passed through to the oracle for snapshot honesty.
        existing_position_qty: signed held qty; a held inadmissible short flattens to 0.
        account_equity: account equity (USD) for the live oracle's hard checks (the
            < $2,000 short-capability floor, step 5). None => the live oracle
            fails-closed (MISSING_ACCOUNT_CONTEXT) — never an assumed pass. For the
            paper account this IS the NAV (`equity_total`); pass `account_equity=nav`.
        available_bp: account buying power (USD) for the live oracle's Reg-T / Alpaca
            BP hard check (step 8b). None => the live oracle fails-closed
            (MISSING_ACCOUNT_CONTEXT). A true value needs a live broker account fetch
            (the materialized paper state tracks equity_total, not buying power);
            BOTH the autonomous seam and the PaperReactor seam now resolve it from the
            SAME fail-closed ``live_buying_power()`` oracle helper (Workstream C seam
            parity), so identical inputs produce identical verdicts at both seams. When
            that fetch fails / creds are missing / BP is non-positive the helper returns
            None and the short fails-closed — never a fabricated sufficiency.

    Returns:
        AdmissibilityVerdict. `admitted=True` with `adjusted_target_pct == target_pct`
        on ACCEPTED; `admitted=False` with `adjusted_target_pct == 0.0` on REJECT/PARTIAL.
    """
    # Derive the effective side. A short is indicated by EITHER an explicit short `side`
    # token OR a negative target — fail-closed toward "treat as short / constrain" so a
    # negative target can never slip through the unconstrained long path.
    is_short = side_of(target_pct) is Side.SHORT or str(side).strip().lower() in {
        "short",
        "sell_short",
        "ss",
    }
    effective_side = "short" if is_short else "long"

    # UNIT BRIDGE (ADR-0077): the oracle's whole-share check expects a SHARE count, not a
    # NAV fraction. Convert with the SAME price the reactor fills at. Fail-closed: missing /
    # non-positive NAV or price -> 0 shares, which the live oracle REJECTs (never a faked qty).
    if nav is not None and nav > 0 and price is not None and price > 0:
        signed_shares = target_pct_to_shares(target_pct, nav, price)
    else:
        signed_shares = 0
    qty_shares = abs(signed_shares)

    oracle = select_oracle()
    # ctx carries what IS available at this seam: the decision price as the quote, plus
    # whatever account context the caller could resolve. `account_equity` (= the paper
    # NAV, `equity_total`) is plumbed so an ETB whole-share short can clear the
    # equity floor (step 5) instead of fail-closing on MISSING_ACCOUNT_CONTEXT.
    # `available_bp` (Workstream C) is now resolved by BOTH the autonomous and the
    # PaperReactor callers from the SAME fail-closed `live_buying_power()` oracle helper
    # and passed in here, so the BP hard check (step 8b) produces identical verdicts at
    # both seams. When that fetch is unavailable the caller passes None and the live
    # oracle fails-closed on BP — never an assume-safe pass.
    ctx = AdmissibilityContext(
        current_ask=price if (price is not None and price > 0) else None,
        account_equity=account_equity,
        available_bp=available_bp,
    )
    verdict: ShortabilityVerdict = oracle.verdict(
        symbol, effective_side, qty_shares, asof, ctx
    )
    adj = apply_verdict_to_target(
        target_pct, verdict, existing_position_qty=existing_position_qty
    )
    # `admitted` keys off the verdict STATE, not the adjusted magnitude: only ACCEPTED proceeds.
    # PARTIAL (e.g. SSR marketable short) and REJECTED both yield a no-fill — the reactor is a
    # paper actuator with no deferral primitive, so anything short of ACCEPTED is treated as a
    # REJECT (fail-closed; can only shrink, never widen).
    admitted = verdict.state is AdmissibilityState.ACCEPTED
    return AdmissibilityVerdict(
        admitted=admitted,
        original_target_pct=target_pct,
        adjusted_target_pct=adj.adjusted_target_pct,
        side=side_of(target_pct),
        qty_shares=qty_shares,
        state=verdict.state,
        reason=verdict.reason,
        annual_cbr=verdict.annual_cbr,
    )
