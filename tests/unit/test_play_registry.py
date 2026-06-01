"""Registry-derivation tests for the play scorers (ADR-0082 Part A).

These tests pin the *anti-drift* refactor: ``score_all()``, ``PLAY_NAMES`` and
the per-play wrapper dispatch are all DERIVED from ``profiles.PROFILES`` (the
single source of truth) instead of hand-maintained parallel lists.

The contract under test:

1. ``score_all`` over PROFILES is byte-identical to the OLD literal 5-key dict
   (the same 5 plays, computed by the same per-play functions, same order).
2. ``PLAY_NAMES`` equals the OLD hardcoded tuple, in the OLD order, and is the
   same object/value everywhere it's re-exported.
3. Adding a profile to PROFILES flows through ``score_all`` / ``PLAY_NAMES`` /
   ``score_play`` with NO edit to scorers.py — proving the drift footgun is gone.

Offline / deterministic: no network. We reuse the hand-crafted snapshots and
compute everything in-process.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from hermes_quant.playbook import PROFILES, PlayProfile
from hermes_quant.playbook import scorers as scorers_mod
from hermes_quant.playbook import watchlist_evolution as we_mod
from hermes_quant.playbook.scorers import (
    PLAY_NAMES,
    PlayFitness,
    score_all,
    score_covered_call,
    score_csp,
    score_leaps,
    score_play,
    score_swing,
    score_wheel,
)

# The exact tuple that used to be hardcoded in watchlist_evolution.py:55 and
# implied by scorers.score_all()'s literal 5-key dict. This is the frozen
# golden value the refactor must reproduce.
_OLD_PLAY_NAMES: tuple[str, ...] = (
    "covered_call",
    "csp",
    "wheel",
    "leaps",
    "swing",
)


def _old_literal_score_all(snapshot: dict) -> dict[str, PlayFitness]:
    """The PRE-refactor body of score_all() — the literal 5-key dict.

    Kept here verbatim as the golden reference. The new registry-derived
    score_all() must produce a byte-identical result for every input.
    """
    return {
        "covered_call": score_covered_call(snapshot),
        "csp": score_csp(snapshot),
        "wheel": score_wheel(snapshot),
        "leaps": score_leaps(snapshot),
        "swing": score_swing(snapshot),
    }


# --------------------------------------------------------------------------- #
# Diverse offline snapshots (cover eligible, ineligible, evicted, empty, None) #
# --------------------------------------------------------------------------- #


def _good_covered_call_snapshot() -> dict:
    return {
        "symbol": "GOODCC",
        "quote_type": "EQUITY",
        "last_close": 50.0,
        "market_cap_usd": 5e9,
        "avg_dollar_volume_30d": 2e7,
        "realized_vol_30d": 0.35,
        "rsi_14": 55.0,
        "atr_14": 1.0,
        "atr_pct_of_spot": 0.02,
        "distance_from_52w_high_pct": -0.05,
        "five_d_return_pct": 0.02,
        "dividend_yield": 0.012,
        "debt_to_equity": 0.5,
        "beta": 1.0,
        "free_cash_flow_yield": 0.04,
        "return_on_equity": 0.18,
        "gross_margin": 0.45,
        "revenue_growth_yoy": 0.12,
        "days_since_earnings": 20,
    }


def _good_leaps_snapshot() -> dict:
    return {
        "symbol": "GOODLEAP",
        "quote_type": "EQUITY",
        "last_close": 100.0,
        "market_cap_usd": 5e10,
        "avg_dollar_volume_30d": 5e7,
        "realized_vol_30d": 0.30,
        "rsi_14": 55.0,
        "atr_14": 2.0,
        "atr_pct_of_spot": 0.02,
        "distance_from_52w_high_pct": -0.10,
        "five_d_return_pct": 0.01,
        "dividend_yield": 0.0,
        "debt_to_equity": 0.4,
        "beta": 1.1,
        "free_cash_flow_yield": 0.05,
        "return_on_equity": 0.25,
        "gross_margin": 0.55,
        "revenue_growth_yoy": 0.20,
        "days_since_earnings": 30,
    }


def _good_swing_snapshot() -> dict:
    return {
        "symbol": "GOODSWG",
        "quote_type": "EQUITY",
        "last_close": 80.0,
        "market_cap_usd": 5e10,
        "avg_dollar_volume_30d": 3e7,
        "realized_vol_30d": 0.60,
        "rsi_14": 25.0,
        "atr_14": 2.5,
        "atr_pct_of_spot": 0.031,
        "distance_from_52w_high_pct": -0.05,
        "five_d_return_pct": -0.08,
        "dividend_yield": 0.0,
        "debt_to_equity": 0.5,
        "beta": 1.4,
        "free_cash_flow_yield": 0.02,
        "return_on_equity": 0.10,
        "gross_margin": 0.40,
        "revenue_growth_yoy": 0.05,
        "days_since_earnings": 30,
    }


def _etf_snapshot() -> dict:
    s = _good_covered_call_snapshot()
    s["symbol"] = "ETFX"
    s["quote_type"] = "ETF"
    return s


def _microcap_snapshot() -> dict:
    s = _good_covered_call_snapshot()
    s["symbol"] = "TINY"
    s["market_cap_usd"] = 5e8  # fires multiple eviction rules
    return s


def _regime_bear_snapshot() -> dict:
    s = _good_covered_call_snapshot()
    s["symbol"] = "BEARISH"
    s["regime"] = "bear"  # deny for cc/csp/wheel/leaps, allow for swing
    return s


def _regime_volatile_snapshot() -> dict:
    s = _good_covered_call_snapshot()
    s["symbol"] = "VOLATILE"
    s["regime"] = "volatile"  # warn (30% penalty) for cc/csp/wheel/leaps
    return s


_SNAPSHOTS = [
    _good_covered_call_snapshot(),
    _good_leaps_snapshot(),
    _good_swing_snapshot(),
    _etf_snapshot(),
    _microcap_snapshot(),
    _regime_bear_snapshot(),
    _regime_volatile_snapshot(),
    {"symbol": "EMPTY"},          # completely empty
    {},                            # no symbol at all
    {"symbol": "NONES", "quote_type": None, "market_cap_usd": None},
]


def _fits_equal(a: PlayFitness, b: PlayFitness) -> bool:
    """Strict field-by-field equality of two PlayFitness results."""
    return asdict(a) == asdict(b)


# --------------------------------------------------------------------------- #
# 1. score_all == old literal output, for ALL 5 plays, across all snapshots
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
def test_score_all_byte_identical_to_old_literal(snapshot: dict) -> None:
    got = score_all(snapshot)
    want = _old_literal_score_all(snapshot)

    # Same keys, same order (dict insertion order is part of the contract).
    assert list(got.keys()) == list(want.keys()) == list(_OLD_PLAY_NAMES)

    # Each play's PlayFitness is byte-identical, field for field.
    for play in _OLD_PLAY_NAMES:
        assert _fits_equal(got[play], want[play]), (
            f"play {play!r} drifted for snapshot {snapshot.get('symbol')!r}: "
            f"{asdict(got[play])} != {asdict(want[play])}"
        )


@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
def test_score_play_matches_named_wrappers(snapshot: dict) -> None:
    # score_play(name, ...) must equal the corresponding named wrapper exactly.
    assert _fits_equal(score_play("covered_call", snapshot), score_covered_call(snapshot))
    assert _fits_equal(score_play("csp", snapshot), score_csp(snapshot))
    assert _fits_equal(score_play("wheel", snapshot), score_wheel(snapshot))
    assert _fits_equal(score_play("leaps", snapshot), score_leaps(snapshot))
    assert _fits_equal(score_play("swing", snapshot), score_swing(snapshot))


def test_score_all_keys_derived_from_profiles() -> None:
    # The dict keys ARE the PROFILES keys, in order — not a literal.
    assert list(score_all(_good_covered_call_snapshot()).keys()) == list(PROFILES.keys())


# --------------------------------------------------------------------------- #
# 2. PLAY_NAMES == old tuple, derived from PROFILES, consistent everywhere
# --------------------------------------------------------------------------- #


def test_play_names_equals_old_tuple() -> None:
    assert PLAY_NAMES == _OLD_PLAY_NAMES


def test_play_names_derived_from_profiles_order() -> None:
    assert PLAY_NAMES == tuple(PROFILES.keys())


def test_play_names_consistent_across_modules() -> None:
    # scorers, watchlist_evolution, and the package facade must all agree.
    import hermes_quant.playbook as pb

    assert we_mod.PLAY_NAMES == PLAY_NAMES
    assert pb.PLAY_NAMES == PLAY_NAMES
    assert we_mod.PLAY_NAMES is scorers_mod.PLAY_NAMES  # re-export, same object


# --------------------------------------------------------------------------- #
# 3. Adding a profile flows through with NO edit to scorers.py
# --------------------------------------------------------------------------- #


def test_added_profile_flows_through_registry() -> None:
    """A new profile injected into PROFILES is picked up by the registry-
    derived API (score_all / PLAY_NAMES-equivalent / score_play) without any
    edit to scorers.py. We mutate PROFILES in a try/finally to avoid leaking.
    """
    test_play = PlayProfile(
        name="test_momentum",
        bias="agnostic",
        hard_rules={"quote_type": ("eq", "EQUITY")},
        soft_rules={"rsi_14": ("gt", 50.0)},
        eviction_rules={},
        regime_gates={},
    )
    assert "test_momentum" not in PROFILES
    PROFILES["test_momentum"] = test_play
    try:
        snap = _good_covered_call_snapshot()

        # score_all now includes the new play, in insertion order (last).
        out = score_all(snap)
        assert "test_momentum" in out
        assert list(out.keys())[-1] == "test_momentum"
        assert isinstance(out["test_momentum"], PlayFitness)

        # PLAY_NAMES recomputed from PROFILES picks it up too. (PLAY_NAMES is a
        # module-level tuple captured at import; the *derivation* is what we
        # assert, matching how a play loader would rebuild it.)
        assert tuple(PROFILES.keys())[-1] == "test_momentum"

        # score_play dispatches it through the generic scorer (no override).
        fit = score_play("test_momentum", snap)
        assert fit.play == "test_momentum"
        # Generic scorer ran: EQUITY hard rule passes, rsi soft rule passes.
        assert fit.pass_hard is True
        assert fit.eligible is True

        # The 5 original plays are UNAFFECTED by the addition (still byte-id).
        for play in _OLD_PLAY_NAMES:
            assert _fits_equal(out[play], _old_literal_score_all(snap)[play])
    finally:
        del PROFILES["test_momentum"]
    assert "test_momentum" not in PROFILES


def test_unknown_play_raises_keyerror() -> None:
    # score_play must fail loud on an unknown play (not silently return a stub).
    with pytest.raises(KeyError):
        score_play("does_not_exist", _good_covered_call_snapshot())
