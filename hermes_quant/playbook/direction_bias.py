"""Direction-vs-play-bias compatibility predicate (B04 / A5 fix).

The autonomous tick fires the advisor's direction signal through whichever
play the symbol is ELIGIBLE for. But the five plays are not all
direction-agnostic: covered_call / csp / wheel / leaps are BULLISH-bias
structures (you profit when the underlying holds or rises), while swing is
direction-agnostic. A SHORT signal routed through a cash-secured put (csp)
is structurally incoherent — that's the live AXP defect this module closes.

This module is a PURE PREDICATE: no IO, no logging, no global state. The
autonomous tick consumes it to decide whether to propagate a signal or
silence it with gate=DIRECTION_BIAS_MISMATCH.

Routing rule (silence-by-default):
    direction < 0 (SHORT) → may route ONLY through 'agnostic' or 'bearish' plays
    direction > 0 (LONG)  → may route ONLY through 'bullish' or 'agnostic' plays
    direction == 0        → no trade; nothing to route (incompatible by definition)
    unknown play name     → INCOMPATIBLE (don't fire)
    unknown / missing bias → INCOMPATIBLE (don't fire)
"""

from __future__ import annotations

from collections.abc import Iterable

from .profiles import PROFILES

# Allowed play biases per signal direction. Anything not listed here — including
# an unknown bias string — is treated as incompatible (silence-by-default).
_LONG_OK: frozenset[str] = frozenset({"bullish", "agnostic"})
_SHORT_OK: frozenset[str] = frozenset({"bearish", "agnostic"})


def play_bias(play: str) -> str | None:
    """Return the directional bias of a play, or None if the play is unknown.

    A None return means the caller MUST treat the play as incompatible with
    any direction (silence-by-default).
    """
    profile = PROFILES.get(play)
    if profile is None:
        return None
    bias = getattr(profile, "bias", None)
    if not isinstance(bias, str):
        return None
    return bias


def bias_allows_direction(bias: str | None, direction: int) -> bool:
    """True iff a play of the given ``bias`` may carry a signal of ``direction``.

    Silence-by-default: an unknown bias (None or an unrecognized string) and a
    zero/None direction both return False.
    """
    if bias is None:
        return False
    if direction is None:
        return False
    if direction > 0:
        return bias in _LONG_OK
    if direction < 0:
        return bias in _SHORT_OK
    # direction == 0 → no trade; nothing to route.
    return False


def compatible_plays(direction: int, plays: Iterable[str]) -> list[str]:
    """Return the subset of ``plays`` whose bias admits ``direction``.

    Unknown play names and unknown biases are dropped (silence-by-default).
    """
    out: list[str] = []
    for play in plays:
        if bias_allows_direction(play_bias(play), direction):
            out.append(play)
    return out


def direction_play_compatible(direction: int, plays: Iterable[str]) -> bool:
    """True iff AT LEAST ONE eligible play can structurally carry ``direction``.

    This is the gate the autonomous tick checks BEFORE propagating an advisor
    signal. When it returns False the signal must NOT fire; the tick emits an
    audit record with gate=DIRECTION_BIAS_MISMATCH instead of FIRE.

    Silence-by-default: an empty play list, a zero/None direction, unknown play
    names, and unknown biases all yield False.
    """
    return len(compatible_plays(direction, plays)) > 0
