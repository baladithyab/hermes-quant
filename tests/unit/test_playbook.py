"""Unit tests for hermes_quant.playbook (profile + scorer mechanics).

These tests do NOT touch the network. compute_play_snapshot is exercised in
the live smoke run shipped alongside the package, not here.
"""

from __future__ import annotations

import pytest

from hermes_quant.playbook import (
    PROFILES,
    PlayFitness,
    PlayProfile,
    score_all,
    score_covered_call,
    score_csp,
    score_leaps,
    score_swing,
    score_wheel,
)

# --------------------------------------------------------------------------- #
# Hand-crafted "good" snapshots (one per play)
# --------------------------------------------------------------------------- #


def _good_covered_call_snapshot() -> dict:
    return {
        "symbol": "GOODCC",
        "last_close": 50.0,
        "market_cap_usd": 5e9,           # mid-cap
        "avg_dollar_volume_30d": 2e7,    # liquid
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


def _good_csp_snapshot() -> dict:
    s = _good_covered_call_snapshot()
    s["symbol"] = "GOODCSP"
    s["market_cap_usd"] = 5e10  # large, fine
    s["debt_to_equity"] = 0.8
    return s


def _good_leaps_snapshot() -> dict:
    return {
        "symbol": "GOODLEAP",
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
        "last_close": 80.0,
        "market_cap_usd": 5e10,
        "avg_dollar_volume_30d": 3e7,
        "realized_vol_30d": 0.60,           # in [0.30, 1.50]
        "rsi_14": 25.0,                      # extreme — passes soft "or" rule
        "atr_14": 2.5,
        "atr_pct_of_spot": 0.031,            # > 0.02 soft
        "distance_from_52w_high_pct": -0.05,
        "five_d_return_pct": -0.08,          # in window, nonzero
        "dividend_yield": 0.0,
        "debt_to_equity": 0.5,
        "beta": 1.4,
        "free_cash_flow_yield": 0.02,
        "return_on_equity": 0.10,
        "gross_margin": 0.40,
        "revenue_growth_yoy": 0.05,
        "days_since_earnings": 30,
    }


# --------------------------------------------------------------------------- #
# Profile sanity
# --------------------------------------------------------------------------- #


def test_profiles_dict_has_five_plays() -> None:
    assert set(PROFILES.keys()) == {"covered_call", "csp", "wheel", "leaps", "swing"}
    for p in PROFILES.values():
        assert isinstance(p, PlayProfile)


def test_profile_is_frozen() -> None:
    p = PROFILES["covered_call"]
    # frozen dataclass raises FrozenInstanceError (subclass of AttributeError)
    with pytest.raises(AttributeError):
        p.name = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Each play accepts its hand-crafted "good" snapshot
# --------------------------------------------------------------------------- #


def test_covered_call_accepts_good_snapshot() -> None:
    fit = score_covered_call(_good_covered_call_snapshot())
    assert isinstance(fit, PlayFitness)
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert fit.score >= 0.65
    assert fit.failed_rules == []


def test_csp_accepts_good_snapshot() -> None:
    fit = score_csp(_good_csp_snapshot())
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert fit.score >= 0.65


def test_leaps_accepts_good_snapshot() -> None:
    fit = score_leaps(_good_leaps_snapshot())
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert fit.score >= 0.65


def test_swing_accepts_good_snapshot() -> None:
    fit = score_swing(_good_swing_snapshot())
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert fit.score >= 0.65


# --------------------------------------------------------------------------- #
# Each play rejects a snapshot that violates a hard rule
# --------------------------------------------------------------------------- #


def test_covered_call_rejects_too_small_mcap() -> None:
    s = _good_covered_call_snapshot()
    s["market_cap_usd"] = 1e9  # below 2e9 hard rule (and triggers eviction <1.5e9)
    fit = score_covered_call(s)
    assert fit.pass_hard is False
    assert fit.eligible is False
    assert any("market_cap_usd" in r for r in fit.failed_rules)


def test_csp_rejects_high_debt() -> None:
    s = _good_csp_snapshot()
    s["debt_to_equity"] = 2.5
    fit = score_csp(s)
    assert fit.pass_hard is False
    assert fit.eligible is False


def test_leaps_rejects_too_small_mcap() -> None:
    s = _good_leaps_snapshot()
    s["market_cap_usd"] = 5e9  # below 1e10 hard
    fit = score_leaps(s)
    assert fit.pass_hard is False
    assert fit.eligible is False


def test_swing_rejects_low_volatility() -> None:
    s = _good_swing_snapshot()
    s["realized_vol_30d"] = 0.10  # below 0.30 hard
    fit = score_swing(s)
    assert fit.pass_hard is False
    assert fit.eligible is False


# --------------------------------------------------------------------------- #
# Wheel = covered_call AND csp
# --------------------------------------------------------------------------- #


def test_wheel_eligible_only_when_both_legs_eligible() -> None:
    s = _good_covered_call_snapshot()
    # Both legs should pass for this snapshot.
    fit_cc = score_covered_call(s)
    fit_csp = score_csp(s)
    fit_wheel = score_wheel(s)
    assert fit_cc.eligible is True
    assert fit_csp.eligible is True
    assert fit_wheel.eligible is True


def test_wheel_rejects_when_csp_fails() -> None:
    s = _good_covered_call_snapshot()
    s["debt_to_equity"] = 3.0  # blows up CSP hard rule
    fit_wheel = score_wheel(s)
    fit_csp = score_csp(s)
    assert fit_csp.eligible is False
    assert fit_wheel.eligible is False


def test_wheel_rejects_when_cc_fails() -> None:
    s = _good_covered_call_snapshot()
    s["market_cap_usd"] = 2e11  # blows up CC hard rule (and CC eviction)
    fit_wheel = score_wheel(s)
    assert fit_wheel.eligible is False


# --------------------------------------------------------------------------- #
# Eviction logic
# --------------------------------------------------------------------------- #


def test_covered_call_eviction_fires_on_microcap() -> None:
    s = _good_covered_call_snapshot()
    s["market_cap_usd"] = 1e9  # under 1.5e9
    fit = score_covered_call(s)
    assert any(r.startswith("evict:") for r in fit.failed_rules)
    assert fit.eligible is False


def test_csp_eviction_fires_on_thin_adv() -> None:
    s = _good_csp_snapshot()
    s["avg_dollar_volume_30d"] = 1e6  # under 2e6
    fit = score_csp(s)
    assert any(r.startswith("evict:") for r in fit.failed_rules)
    assert fit.eligible is False


def test_swing_eviction_fires_on_runaway_vol() -> None:
    s = _good_swing_snapshot()
    s["realized_vol_30d"] = 2.5  # over 2.0 eviction
    fit = score_swing(s)
    assert any(r.startswith("evict:") for r in fit.failed_rules)
    assert fit.eligible is False


# --------------------------------------------------------------------------- #
# None-handling: silence-by-default
# --------------------------------------------------------------------------- #


def test_none_in_hard_rule_input_fails_hard() -> None:
    s = _good_covered_call_snapshot()
    s["market_cap_usd"] = None
    fit = score_covered_call(s)
    assert fit.pass_hard is False
    assert fit.eligible is False
    assert any("hard:market_cap_usd=None" in r for r in fit.failed_rules)


def test_none_in_soft_rule_input_does_not_crash_or_reject_outright() -> None:
    s = _good_covered_call_snapshot()
    s["rsi_14"] = None
    s["realized_vol_30d"] = None
    s["distance_from_52w_high_pct"] = None
    fit = score_covered_call(s)
    # Hard rules still pass (none of them require these)
    assert fit.pass_hard is True
    # All soft missing → pass_soft=False but no crash
    assert fit.pass_soft is False
    # Eligibility requires score >= 0.65; soft contributes 0 ⇒ score = 0.6
    assert fit.eligible is False


def test_completely_empty_snapshot_does_not_crash() -> None:
    fit = score_covered_call({"symbol": "EMPTY"})
    assert fit.pass_hard is False
    assert fit.eligible is False
    # all hard rules fail with None
    assert len(fit.failed_rules) >= len(PROFILES["covered_call"].hard_rules)


def test_score_all_returns_one_per_play() -> None:
    out = score_all(_good_covered_call_snapshot())
    assert set(out.keys()) == {"covered_call", "csp", "wheel", "leaps", "swing"}
    for v in out.values():
        assert isinstance(v, PlayFitness)
        assert 0.0 <= v.score <= 1.0


# --------------------------------------------------------------------------- #
# Score formula sanity (0.6 hard / 0.4 soft)
# --------------------------------------------------------------------------- #


def test_score_formula_all_hard_no_soft() -> None:
    s = _good_covered_call_snapshot()
    # Wreck every soft rule
    s["realized_vol_30d"] = 5.0  # outside [0.20, 0.60]
    s["rsi_14"] = 5.0
    s["distance_from_52w_high_pct"] = -0.50
    fit = score_covered_call(s)
    assert fit.pass_hard is True
    # score should be 0.6 * 1.0 + 0.4 * 0.0 = 0.6 → not eligible (<0.65)
    assert fit.score == pytest.approx(0.6, abs=1e-6)
    assert fit.eligible is False
