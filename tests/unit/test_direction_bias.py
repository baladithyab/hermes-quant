"""Unit tests for the direction-vs-play-bias compatibility predicate (B04 / A5).

Background: the autonomous tick used to fire the advisor's direction signal
through whichever play the symbol was ELIGIBLE for, with NO direction-vs-bias
check. Live evidence: AXP fired SHORT (target_position_pct=-0.2) via the 'csp'
play — but CSP (cash-secured put) is a BULLISH-bias structure. A SHORT signal
must NEVER route through a bullish-bias play.

These tests pin the pure predicate that closes that gap.
"""

from __future__ import annotations

import pytest

from hermes_quant.playbook import (
    PROFILES,
    bias_allows_direction,
    compatible_plays,
    direction_play_compatible,
    play_bias,
)

# --------------------------------------------------------------------------- #
# Profile bias wiring
# --------------------------------------------------------------------------- #


def test_every_profile_has_a_known_bias() -> None:
    """Each shipped play must declare a recognized bias (no silent defaults
    that could let a SHORT slip through an unlabeled play)."""
    for name, profile in PROFILES.items():
        assert profile.bias in {"bullish", "bearish", "agnostic"}, (
            f"play {name!r} has unrecognized bias {profile.bias!r}"
        )


def test_option_plays_are_bullish_and_swing_is_agnostic() -> None:
    """covered_call / csp / wheel / leaps are bullish-bias structures; swing
    is direction-agnostic."""
    assert PROFILES["covered_call"].bias == "bullish"
    assert PROFILES["csp"].bias == "bullish"
    assert PROFILES["wheel"].bias == "bullish"
    assert PROFILES["leaps"].bias == "bullish"
    assert PROFILES["swing"].bias == "agnostic"


def test_play_bias_unknown_play_returns_none() -> None:
    assert play_bias("not_a_real_play") is None


# --------------------------------------------------------------------------- #
# bias_allows_direction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("bias", "direction", "expected"),
    [
        # LONG (direction > 0): bullish or agnostic OK
        ("bullish", 1, True),
        ("agnostic", 1, True),
        ("bearish", 1, False),
        # SHORT (direction < 0): bearish or agnostic OK
        ("bearish", -1, True),
        ("agnostic", -1, True),
        ("bullish", -1, False),
        # direction == 0: never routes (no trade)
        ("agnostic", 0, False),
        ("bullish", 0, False),
        # unknown bias: silence-by-default → never routes
        (None, 1, False),
        (None, -1, False),
        ("weird", 1, False),
        ("weird", -1, False),
    ],
)
def test_bias_allows_direction(bias, direction, expected) -> None:
    assert bias_allows_direction(bias, direction) is expected


# --------------------------------------------------------------------------- #
# direction_play_compatible — the four required scenarios
# --------------------------------------------------------------------------- #


def test_short_through_csp_is_incompatible() -> None:
    """SHORT-through-CSP → must NOT be allowed (the AXP defect)."""
    assert direction_play_compatible(-1, ["csp"]) is False


def test_short_through_swing_is_allowed() -> None:
    """SHORT-through-swing → allowed (swing is direction-agnostic)."""
    assert direction_play_compatible(-1, ["swing"]) is True


def test_long_through_csp_is_allowed() -> None:
    """LONG-through-CSP → allowed (csp is bullish-bias)."""
    assert direction_play_compatible(1, ["csp"]) is True


def test_unknown_play_never_fires() -> None:
    """Unknown play → incompatible for both directions (silence-by-default)."""
    assert direction_play_compatible(1, ["not_a_real_play"]) is False
    assert direction_play_compatible(-1, ["not_a_real_play"]) is False


# --------------------------------------------------------------------------- #
# direction_play_compatible — edge cases
# --------------------------------------------------------------------------- #


def test_short_routes_through_any_agnostic_among_eligible_plays() -> None:
    """A symbol eligible for both csp (bullish) and swing (agnostic) CAN take a
    SHORT — because at least one eligible play (swing) admits it."""
    assert direction_play_compatible(-1, ["csp", "swing"]) is True


def test_long_through_all_bullish_plays_is_allowed() -> None:
    assert direction_play_compatible(1, ["covered_call", "csp", "leaps"]) is True


def test_long_routed_through_only_bullish_when_no_agnostic() -> None:
    """A symbol eligible only for bullish plays still takes a LONG fine."""
    assert direction_play_compatible(1, ["covered_call", "wheel"]) is True


def test_short_through_only_bullish_plays_is_incompatible() -> None:
    assert direction_play_compatible(-1, ["covered_call", "csp", "wheel", "leaps"]) is False


def test_empty_play_list_never_fires() -> None:
    assert direction_play_compatible(1, []) is False
    assert direction_play_compatible(-1, []) is False


def test_zero_direction_never_fires() -> None:
    """direction == 0 is no-trade; nothing to route regardless of plays."""
    assert direction_play_compatible(0, ["swing"]) is False
    assert direction_play_compatible(0, ["csp"]) is False


def test_compatible_plays_filters_to_admitting_subset() -> None:
    assert compatible_plays(-1, ["csp", "swing", "leaps"]) == ["swing"]
    assert compatible_plays(1, ["csp", "swing", "leaps"]) == ["csp", "swing", "leaps"]
    # unknown play names are dropped, not raised on
    assert compatible_plays(1, ["csp", "bogus"]) == ["csp"]
