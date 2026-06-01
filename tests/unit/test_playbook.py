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
        "quote_type": "EQUITY",
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
# B14(c): wheel composite eviction divergence
# --------------------------------------------------------------------------- #
#
# The wheel profile's eviction_rules are the UNION of covered_call's and csp's
# eviction rules, prefixed cc_/csp_ (see profiles.py). The SAME logical key
# (e.g. market_cap_too_small) exists in BOTH legs with DIFFERENT thresholds:
#   * cc_market_cap_too_small  : market_cap_usd < 1.5e9   (stricter)
#   * csp_market_cap_too_small : market_cap_usd < 5e8     (looser)
# Because eviction fires if ANY rule is True, the wheel must evict at the
# STRICTER (CC) threshold — there is a band [5e8, 1.5e9) where CC would evict
# but CSP would not, and the wheel must still evict there. These tests pin that
# "more-restrictive-leg-wins" behavior so a future refactor (e.g. collapsing
# the cc_/csp_ prefixes into one key and silently dropping the stricter
# threshold) cannot relax the wheel's eviction floor unnoticed.


def test_wheel_eviction_uses_stricter_cc_market_cap_threshold() -> None:
    """A market cap in [5e8, 1.5e9): CSP's own eviction does NOT fire, but
    CC's does — the wheel must still be evicted (stricter leg wins)."""
    s = _good_covered_call_snapshot()
    s["market_cap_usd"] = 1e9  # below cc 1.5e9, above csp 5e8

    fit_cc = score_covered_call(s)
    fit_csp = score_csp(s)
    fit_wheel = score_wheel(s)

    # CC's eviction fires at 1e9 < 1.5e9.
    assert any(r == "evict:market_cap_too_small" for r in fit_cc.failed_rules)
    # CSP's *eviction* does NOT fire at 1e9 (its floor is 5e8) — confirm the
    # divergence band actually exists for this snapshot.
    assert not any(r == "evict:market_cap_too_small" for r in fit_csp.failed_rules)

    # The wheel's merged eviction carries the prefixed cc_ rule, so it fires;
    # and because CC is ineligible the AND-of-legs also forces wheel ineligible.
    assert fit_wheel.eligible is False
    assert any("evict:cc_market_cap_too_small" == r for r in fit_wheel.failed_rules)


def test_wheel_eviction_fires_when_only_csp_leg_evicts() -> None:
    """Symmetric case: a field that only CSP's eviction catches must still
    evict the wheel via the csp_-prefixed merged rule."""
    s = _good_covered_call_snapshot()
    # adv between csp/cc evict floors is identical (both 2e6); instead use a
    # market cap that trips CSP eviction (<5e8) — which also trips CC's, so to
    # isolate the csp_ rule we assert the csp_-prefixed name is present.
    s["market_cap_usd"] = 4e8  # below BOTH evict floors

    fit_wheel = score_wheel(s)
    assert fit_wheel.eligible is False
    # Both prefixed rules should be recorded (union semantics), proving the
    # merged profile retained BOTH legs' eviction rules rather than collapsing.
    assert any(r == "evict:cc_market_cap_too_small" for r in fit_wheel.failed_rules)
    assert any(r == "evict:csp_market_cap_too_small" for r in fit_wheel.failed_rules)


def test_wheel_merged_eviction_rule_keys_are_union_of_both_legs() -> None:
    """Pin the prefixed-union composition: every CC eviction key appears as
    cc_<key> and every CSP eviction key as csp_<key> in the wheel profile.
    A naive dict.update() that dropped the prefix would collapse same-named
    keys and silently lose the stricter threshold."""
    cc_rules = PROFILES["covered_call"].eviction_rules
    csp_rules = PROFILES["csp"].eviction_rules
    wheel_rules = PROFILES["wheel"].eviction_rules

    for key, rule in cc_rules.items():
        assert wheel_rules.get(f"cc_{key}") == rule, f"missing/changed cc_{key}"
    for key, rule in csp_rules.items():
        assert wheel_rules.get(f"csp_{key}") == rule, f"missing/changed csp_{key}"

    # No collapse: the count is exactly the sum of both legs' rule counts.
    assert len(wheel_rules) == len(cc_rules) + len(csp_rules)


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


# --------------------------------------------------------------------------- #
# Equity-only gate: ETFs / funds / indices must be rejected by every play
# Regression test for the May-26 noisy-yfinance bug where ETFs ran the full
# scoring pipeline because no profile required `quote_type == "EQUITY"`.
# --------------------------------------------------------------------------- #


def _etf_snapshot(quote_type: str = "ETF") -> dict:
    """Snapshot that would otherwise pass every play's hard rules but is
    flagged as a non-equity instrument via quoteType."""
    s = _good_covered_call_snapshot()
    s["symbol"] = f"GOOD{quote_type}"
    s["quote_type"] = quote_type
    return s


@pytest.mark.parametrize("play_name", ["covered_call", "csp", "wheel", "leaps", "swing"])
@pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY"])
def test_non_equity_instruments_rejected_by_all_plays(
    play_name: str, quote_type: str
) -> None:
    """Every play in the playbook must reject non-equity instruments outright,
    both via hard-rule (pass_hard=False) AND via eviction (eligible=False with
    'evict:non_equity' in failed_rules). This double-gate prevents ETFs from
    entering the watchlist regardless of evict_floor configuration."""
    snap = _etf_snapshot(quote_type)
    # Override fields so ONLY the quote_type rule blocks it — proves the gate.
    snap["last_close"] = 50.0
    snap["market_cap_usd"] = 5e10
    snap["avg_dollar_volume_30d"] = 5e7
    snap["debt_to_equity"] = 0.4
    snap["realized_vol_30d"] = 0.40

    fit = score_all(snap)[play_name]
    assert fit.eligible is False, (
        f"{play_name} accepted a {quote_type} instrument: {fit}"
    )
    # Wheel prefixes inherited eviction rules with cc_/csp_, so accept any
    # 'evict:*non_equity' rule name. The hard-rule failure is also acceptable.
    has_evict = any("non_equity" in r for r in fit.failed_rules)
    has_hard_fail = any("quote_type" in r for r in fit.failed_rules)
    assert has_evict or has_hard_fail, (
        f"{play_name} failed to reject {quote_type}: {fit.failed_rules}"
    )


def test_missing_quote_type_silenced_by_default() -> None:
    """If quote_type is missing entirely (data outage), the eq hard-rule
    should fail-safe to silence (pass_hard=False) rather than guess."""
    s = _good_covered_call_snapshot()
    s.pop("quote_type", None)
    fit = score_covered_call(s)
    assert fit.pass_hard is False
    assert any("quote_type" in r for r in fit.failed_rules)



# --------------------------------------------------------------------------- #
# Regime gate tests (ADR-0035 amendment 2026-05-28)
# --------------------------------------------------------------------------- #


def test_regime_gate_deny_in_bear_for_covered_call() -> None:
    """A good covered_call snapshot is forced ineligible in BEAR regime."""
    s = _good_covered_call_snapshot()
    s["regime"] = "bear"
    fit = score_covered_call(s)
    assert fit.eligible is False
    assert fit.score == 0.0 or any("regime_gate" in r for r in fit.failed_rules)
    assert any("regime=bear" in n.lower() or "bear" in n.lower() for n in fit.notes)


def test_regime_gate_warn_in_volatile_for_covered_call() -> None:
    """VOLATILE regime applies a 30% score penalty but does not deny."""
    s_neutral = _good_covered_call_snapshot()
    fit_neutral = score_covered_call(s_neutral)

    s_vol = _good_covered_call_snapshot()
    s_vol["regime"] = "volatile"
    fit_vol = score_covered_call(s_vol)

    # Score should be ~70% of the un-warned score.
    assert fit_vol.score < fit_neutral.score
    assert fit_vol.score == pytest.approx(fit_neutral.score * 0.7, rel=0.01)


def test_regime_gate_allow_in_bull_is_no_op() -> None:
    """BULL regime does not change the score vs no-regime baseline."""
    s_neutral = _good_covered_call_snapshot()
    fit_neutral = score_covered_call(s_neutral)

    s_bull = _good_covered_call_snapshot()
    s_bull["regime"] = "bull"
    fit_bull = score_covered_call(s_bull)

    assert fit_bull.score == fit_neutral.score
    assert fit_bull.eligible == fit_neutral.eligible


def test_regime_gate_no_regime_field_is_no_op() -> None:
    """Snapshots without 'regime' field score identically to pre-2026-05-28 behavior."""
    s = _good_covered_call_snapshot()
    assert "regime" not in s  # baseline assumption
    fit = score_covered_call(s)
    assert fit.eligible is True


def test_regime_gate_swing_unaffected_in_all_regimes() -> None:
    """Swing's regime_gates is all-allow; behavior is identical regardless of regime."""
    s_base = _good_swing_snapshot()
    fit_base = score_swing(s_base)
    for label in ("bull", "bear", "volatile", "unknown"):
        s = _good_swing_snapshot()
        s["regime"] = label
        fit = score_swing(s)
        assert fit.score == fit_base.score, f"swing score changed under {label!r}"
        assert fit.eligible == fit_base.eligible


def test_regime_gate_packet_with_label_attr() -> None:
    """Snapshot regime can be a RegimePacket-like object with a .label attribute."""
    class FakeRegimePacket:
        def __init__(self, label: str) -> None:
            self.label = label
    s = _good_covered_call_snapshot()
    s["regime"] = FakeRegimePacket("bear")
    fit = score_covered_call(s)
    assert fit.eligible is False  # bear → deny


def test_regime_gate_dict_with_label_key() -> None:
    """Snapshot regime can be a dict with a 'label' key (e.g. JSON-serialized RegimePacket)."""
    s = _good_covered_call_snapshot()
    s["regime"] = {"label": "bear", "volatility_tier": 0}
    fit = score_covered_call(s)
    assert fit.eligible is False


def test_regime_gate_unknown_label_defaults_to_allow() -> None:
    """A regime label not in profile.regime_gates defaults to allow (forward-compat)."""
    s = _good_covered_call_snapshot()
    s["regime"] = "transitional"  # not in our gate dict
    fit = score_covered_call(s)
    assert fit.eligible is True


def test_regime_gate_csp_deny_in_bear() -> None:
    """CSP in BEAR is a structural deny, parallel to CC."""
    s = _good_csp_snapshot()
    s["regime"] = "bear"
    fit = score_csp(s)
    assert fit.eligible is False


def test_regime_gate_wheel_inherits_deny() -> None:
    """Wheel = CC + CSP; deny in BEAR via inheritance through both legs."""
    s = _good_csp_snapshot()
    s["regime"] = "bear"
    fit = score_wheel(s)
    assert fit.eligible is False


def test_regime_gate_leaps_deny_in_bear() -> None:
    """LEAPS = long-call thesis; deny in BEAR (delta + theta both work against you)."""
    s = _good_leaps_snapshot()
    s["regime"] = "bear"
    fit = score_leaps(s)
    assert fit.eligible is False
