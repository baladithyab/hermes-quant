"""hermes_quant.admissibility.order_state — admissibility -> sizing bridge (ADR-0077).

Converts a discrete NAV target into a whole-share short count, and applies a ShortabilityVerdict
to a proposed target. This module can ONLY shrink a target (REJECT -> 0.0, flatten -> 0.0). It can
NEVER increase one (the ADR-0004 authority boundary, enforced by a property test).

NOTE: this is NOT the ADR-0078 OrderState/OrderEvent machine (that lives at
hermes_quant/react/order_state.py and is out of scope for Wave B). This file holds only the
admissibility -> sizing bridge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .oracle import AdmissibilityState, ShortabilityVerdict


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


def side_of(target_pct: float) -> Side:
    return Side.SHORT if target_pct < 0 else Side.LONG


def target_pct_to_shares(target_pct: float, nav: float, price: float) -> int:
    """Convert a signed NAV fraction to a SIGNED whole-share count.

    Shorts (target_pct < 0) floor toward zero in magnitude so a fractional short can never be
    emitted (live HTTP 422). Longs may be fractional elsewhere, but this helper returns whole
    shares for both so the admissibility path is uniform. price/nav must be > 0 (else 0 shares).

    FAIL-CLOSED on non-finite inputs: a NaN/inf target_pct, nav, or price would make
    `math.floor((abs(target_pct) * nav) / price)` raise (OverflowError for inf,
    ValueError for NaN), which — reached from the autonomous tick via
    `gate_order.admit_or_reject` — would abort the whole tick mid-watchlist instead of
    silencing this one entry. Returning 0 shares makes the oracle REJECT (the contract:
    admitted=False -> SILENCE_ADMISSIBILITY), never an assumed-safe fill.
    """
    if (
        not (math.isfinite(target_pct) and math.isfinite(nav) and math.isfinite(price))
        or price <= 0
        or nav <= 0
    ):
        return 0
    raw = (abs(target_pct) * nav) / price
    shares = math.floor(raw)  # floor magnitude -> never over-shorts, never fractional
    return -shares if target_pct < 0 else shares


@dataclass(frozen=True)
class AdmissibilityAdjustment:
    """Result of applying a verdict to a proposed target. `adjusted_target_pct` is ALWAYS
    such that abs(adjusted) <= abs(original) — the authority boundary."""

    original_target_pct: float
    adjusted_target_pct: float
    verdict: ShortabilityVerdict
    flattened_existing_short: bool = False


def apply_verdict_to_target(
    target_pct: float, verdict: ShortabilityVerdict, *, existing_position_qty: float = 0.0
) -> AdmissibilityAdjustment:
    """REJECT-only / flatten-only adjuster.

    - verdict ACCEPTED          -> adjusted = target_pct (unchanged)
    - verdict REJECTED / PARTIAL on an OPENING short -> adjusted = 0.0 (no order)
    - inadmissible HELD short (existing_position_qty < 0 and verdict not ACCEPTED)
                                -> adjusted = 0.0 (flatten), flattened_existing_short=True
    INVARIANT (asserted): abs(adjusted_target_pct) <= abs(target_pct). Never amplifies.
    """
    if verdict.state is AdmissibilityState.ACCEPTED:
        adjusted = target_pct
        flattened = False
    else:
        # REJECTED or PARTIAL: no opening short; and flatten any inadmissible held short.
        adjusted = 0.0
        flattened = existing_position_qty < 0

    # The authority boundary, defended in code (the property test re-asserts it).
    if abs(adjusted) > abs(target_pct):
        raise AssertionError(f"admissibility amplified a target: |{adjusted}| > |{target_pct}|")
    return AdmissibilityAdjustment(
        original_target_pct=target_pct,
        adjusted_target_pct=adjusted,
        verdict=verdict,
        flattened_existing_short=flattened,
    )
