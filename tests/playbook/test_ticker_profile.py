"""Unit tests for hermes_quant.playbook.ticker_profile (W1).

The unified TickerProfile fitness model — ONE profile-fit fitness distilled
from profiles.py, separating "does this ticker fit WHAT WE TRADE" (profile-fit,
load-bearing tradeability) from "is this the right STRATEGY for it"
(strategy-specific, which belongs to the decision layer, not the watchlist).

These tests do NOT touch the network. They exercise the pure scoring grammar
(reused verbatim from scorers.py) over hand-crafted snapshots.

POSTURE under test:
  * profile-fit rails only — strategy-specific fields (days_since_earnings,
    debt_to_equity, regime_gates, rsi_14, …) must NOT affect fit.
  * silence-by-default — missing market_cap / spread_pct (None) must NOT reject
    (soft rules), and missing-data eviction inputs must NOT fire.
  * fail-closed traps — penny / illiquid / non-equity / vol-runaway evict.
  * grammar reuse — _eval_rule / _eval_eviction / _score_against verbatim from
    scorers.py, NOT a re-implemented rule engine.
  * byte-identical thresholds — the distilled rails reuse the LEAST-restrictive
    eviction floors from profiles.py verbatim (the strategy-agnostic floor).
"""

from __future__ import annotations

import dataclasses

from hermes_quant.playbook.profiles import PROFILES, PlayProfile
from hermes_quant.playbook.scorers import (
    NON_EQUITY_QUOTE_TYPES,
    _eval_eviction,
    _eval_rule,
    _score_against,
)
from hermes_quant.playbook.ticker_profile import (
    TICKER_PROFILE,
    TickerFitness,
    TickerProfile,
    score_ticker_profile,
)

# --------------------------------------------------------------------------- #
# Hand-crafted snapshots
# --------------------------------------------------------------------------- #


def _good_snapshot() -> dict:
    """A snapshot that clears every profile-fit rail (liquid mid-cap equity)."""
    return {
        "symbol": "GOODFIT",
        "asof": "2026-06-17T00:00:00+00:00",
        "quote_type": "EQUITY",
        "last_close": 50.0,                # in [5, 500]
        "market_cap_usd": 5e9,             # >= 5e8
        "avg_dollar_volume_30d": 2e7,      # >= 2e6
        "realized_vol_30d": 0.35,          # in [0.05, 1.50]; not runaway
        "spread_pct": 0.004,               # <= 0.01
        "tradable": True,
    }


# --------------------------------------------------------------------------- #
# Dataclass shape
# --------------------------------------------------------------------------- #


def test_ticker_profile_dataclass_has_unified_shape() -> None:
    fields = {f.name for f in dataclasses.fields(TickerProfile)}
    expected = {
        "symbol",
        "asof",
        "asset_class",
        "options_eligible",
        "shortable",
        "last_close",
        "avg_dollar_volume_30d",
        "market_cap_usd",
        "realized_vol_30d",
        "spread_pct",
        "quote_type",
        "tradable",
        "horizon_set",
    }
    assert fields == expected


def test_ticker_profile_is_a_single_playprofile_shaped_object() -> None:
    # ONE profile, reusing the EXISTING PlayProfile grammar (no new rule engine).
    assert isinstance(TICKER_PROFILE, PlayProfile)


def test_ticker_fitness_mirrors_playfitness_fields() -> None:
    fit = score_ticker_profile(_good_snapshot())
    assert isinstance(fit, TickerFitness)
    # Mirrors PlayFitness: symbol / fit_score / pass_hard / eligible / failed_rules.
    for attr in ("symbol", "fit_score", "pass_hard", "eligible", "failed_rules"):
        assert hasattr(fit, attr), attr


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_good_snapshot_is_eligible() -> None:
    fit = score_ticker_profile(_good_snapshot())
    assert fit.symbol == "GOODFIT"
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert fit.fit_score >= 0.65
    assert fit.failed_rules == []


def test_fit_score_in_unit_interval() -> None:
    fit = score_ticker_profile(_good_snapshot())
    assert 0.0 <= fit.fit_score <= 1.0


# --------------------------------------------------------------------------- #
# Profile-fit HARD rails (the genuine tradeability floor)
# --------------------------------------------------------------------------- #


def test_hard_rules_are_exactly_the_profile_fit_set() -> None:
    # market_cap_usd is deliberately NOT a hard rail — it is yfinance-only
    # enrichment absent from the universe artifact, so a hard floor would reject
    # every symbol on the standalone --no-fetch path. It lives in eviction_rules
    # (present-rejects / absent-abstains) instead. See market_cap_too_small below.
    assert set(TICKER_PROFILE.hard_rules.keys()) == {
        "quote_type",
        "avg_dollar_volume_30d",
        "last_close",
    }


def test_hard_rule_thresholds_are_the_least_restrictive_floors() -> None:
    # Strategy-agnostic floors: the LOOSEST eviction-level rails, NOT the
    # per-play hard floors (which are strategy-tightened).
    hr = TICKER_PROFILE.hard_rules
    assert hr["quote_type"] == ("eq", "EQUITY")
    assert hr["avg_dollar_volume_30d"] == ("ge", 2e6)   # eviction ADV floor, not 5e6/1e7
    assert hr["last_close"] == ("between", 5.0, 500.0)  # shared 5 floor / universe 500 ceil


def test_rejects_non_equity_quote_type() -> None:
    s = _good_snapshot()
    s["quote_type"] = "ETF"
    fit = score_ticker_profile(s)
    assert fit.pass_hard is False
    assert fit.eligible is False


def test_rejects_thin_adv_below_floor() -> None:
    s = _good_snapshot()
    s["avg_dollar_volume_30d"] = 1e6  # below the 2e6 floor
    fit = score_ticker_profile(s)
    assert fit.pass_hard is False
    assert fit.eligible is False


def test_rejects_small_cap_below_floor() -> None:
    # A PRESENT sub-5e8 micro-cap is trapped by the market_cap_too_small eviction
    # (not a hard rail). Eviction sets eligible False; pass_hard is unaffected.
    s = _good_snapshot()
    s["market_cap_usd"] = 1e8  # below the 5e8 eviction floor
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("market_cap_too_small" in r for r in fit.failed_rules)


def test_per_play_tightened_mcap_floor_does_not_apply() -> None:
    # A 6e8 mid-cap is BELOW leaps' 1e10 hard floor and csp's 1e9 hard floor
    # (strategy-tightened) but ABOVE the strategy-agnostic 5e8 profile-fit floor.
    # The profile-fit scanner must NOT pre-deny it — that's the decision layer's job.
    # 6e8 > 5e8 so the market_cap_too_small eviction does NOT fire.
    s = _good_snapshot()
    s["market_cap_usd"] = 6e8
    fit = score_ticker_profile(s)
    assert fit.pass_hard is True
    assert fit.eligible is True


def test_per_play_tightened_price_floor_does_not_apply() -> None:
    # last_close = 7.0 is below leaps' 20 and the per-play 10 floors, but above
    # the shared profile-fit floor of 5.0. Must NOT be pre-denied.
    s = _good_snapshot()
    s["last_close"] = 7.0
    fit = score_ticker_profile(s)
    assert fit.pass_hard is True
    assert fit.eligible is True


# --------------------------------------------------------------------------- #
# Profile-fit SOFT rails (scored, never reject)
# --------------------------------------------------------------------------- #


def test_soft_rules_are_exactly_spread_and_vol() -> None:
    assert set(TICKER_PROFILE.soft_rules.keys()) == {"spread_pct", "realized_vol_30d"}
    assert TICKER_PROFILE.soft_rules["spread_pct"] == ("le", 0.01)
    assert TICKER_PROFILE.soft_rules["realized_vol_30d"] == ("between", 0.05, 1.5)


def test_missing_market_cap_abstains_does_not_reject() -> None:
    # market_cap is an EVICTION (market_cap_too_small / lt_field), NOT a hard rail:
    # an ABSENT cap abstains (lt_field is None-safe) so a liquid, in-band, tradable
    # EQUITY on the standalone --no-fetch path (which has no market_cap) is STILL
    # eligible. Money safety is preserved downstream: the decision layer hard-gates
    # market_cap with full enriched data. RED-proof for the W3 zero-fill integration.
    s = _good_snapshot()
    s["market_cap_usd"] = None
    fit = score_ticker_profile(s)
    assert fit.pass_hard is True
    assert fit.eligible is True
    assert not any("market_cap" in r for r in fit.failed_rules)


def test_missing_spread_does_not_reject() -> None:
    # spread_pct is SOFT — missing (None) must NOT reject (abstain on no-data).
    s = _good_snapshot()
    s["spread_pct"] = None
    fit = score_ticker_profile(s)
    assert fit.pass_hard is True
    assert fit.eligible is True


def test_missing_realized_vol_does_not_reject() -> None:
    # realized_vol_30d soft-rail abstains on None (vol-runaway eviction also
    # does not fire on missing data). Eligibility holds on the hard rails alone.
    s = _good_snapshot()
    s["realized_vol_30d"] = None
    fit = score_ticker_profile(s)
    assert fit.pass_hard is True
    assert fit.eligible is True


def test_missing_both_soft_inputs_still_eligible() -> None:
    s = _good_snapshot()
    s["spread_pct"] = None
    s["realized_vol_30d"] = None
    fit = score_ticker_profile(s)
    assert fit.eligible is True


# --------------------------------------------------------------------------- #
# Eviction = composite halt/penny/illiquid/non-equity/vol-runaway trap
# --------------------------------------------------------------------------- #


def test_eviction_rule_keys_are_the_composite_trap_set() -> None:
    assert set(TICKER_PROFILE.eviction_rules.keys()) == {
        "non_equity",
        "price_too_low",
        "adv_too_thin",
        "vol_runaway",
        "not_tradable",
        "market_cap_too_small",
    }


def test_eviction_tuples_are_byte_identical_to_profiles_py() -> None:
    # The distilled traps reuse the SAME rule tuples as profiles.py verbatim,
    # so the scanner's traps can never drift from the per-play evictions.
    ev = TICKER_PROFILE.eviction_rules
    assert ev["non_equity"] == PROFILES["covered_call"].eviction_rules["non_equity"]
    assert ev["price_too_low"] == PROFILES["covered_call"].eviction_rules["price_too_low"]
    assert ev["adv_too_thin"] == PROFILES["covered_call"].eviction_rules["adv_too_thin"]
    assert ev["vol_runaway"] == PROFILES["swing"].eviction_rules["vol_runaway"]
    # market_cap_too_small reuses csp's loosest eviction floor (5e8) verbatim.
    assert ev["market_cap_too_small"] == PROFILES["csp"].eviction_rules["market_cap_too_small"]


def test_penny_stock_trap_evicts() -> None:
    s = _good_snapshot()
    s["last_close"] = 3.0  # below 5.0 -> price_too_low eviction
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("price_too_low" in r for r in fit.failed_rules)


def test_illiquid_trap_evicts() -> None:
    s = _good_snapshot()
    s["avg_dollar_volume_30d"] = 1e6  # below 2e6 -> adv_too_thin eviction
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("adv_too_thin" in r for r in fit.failed_rules)


def test_non_equity_trap_evicts() -> None:
    s = _good_snapshot()
    s["quote_type"] = "CRYPTOCURRENCY"
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("non_equity" in r for r in fit.failed_rules)


def test_vol_runaway_trap_evicts() -> None:
    s = _good_snapshot()
    s["realized_vol_30d"] = 2.5  # > 2.0 -> vol_runaway eviction (halt/blowup trap)
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("vol_runaway" in r for r in fit.failed_rules)


def test_not_tradable_flag_evicts_fail_closed() -> None:
    s = _good_snapshot()
    s["tradable"] = False
    fit = score_ticker_profile(s)
    assert fit.eligible is False
    assert any("not_tradable" in r for r in fit.failed_rules)


def test_missing_tradable_does_not_evict() -> None:
    # Missing-data eviction inputs do NOT fire (silence-by-default). The ne_field
    # eviction op abstains on None — so a snapshot with no tradable flag is not
    # evicted on that basis (it can still fail/pass on the genuine rails).
    s = _good_snapshot()
    s.pop("tradable", None)
    fit = score_ticker_profile(s)
    assert not any("not_tradable" in r for r in fit.failed_rules)
    assert fit.eligible is True


# --------------------------------------------------------------------------- #
# Strategy-specific rules must NOT affect fit (the core separation)
# --------------------------------------------------------------------------- #


def test_strategy_specific_fields_do_not_affect_fit() -> None:
    # days_since_earnings (CC timing), debt_to_equity (csp/leaps credit),
    # dividend_yield/beta (csp soft), rsi_14/atr/five_d (swing momentum),
    # revenue_growth/roe/gross_margin (leaps quality), distance_from_52w_high —
    # ALL belong to the decision layer, NOT the watchlist. Perturbing them to
    # hostile values must leave fit_score / eligibility UNCHANGED.
    base = _good_snapshot()
    base_fit = score_ticker_profile(base)

    hostile = dict(base)
    hostile.update(
        {
            "days_since_earnings": 0,       # CC would reject (ge 5)
            "debt_to_equity": 9.9,          # csp/leaps would reject (lt 2.0 / 1.5)
            "dividend_yield": 0.0,          # csp soft miss
            "beta": 4.0,                    # csp soft miss
            "rsi_14": 50.0,                 # swing soft miss (not extreme)
            "atr_pct_of_spot": 0.0,         # swing soft miss
            "five_d_return_pct": 0.0,       # swing soft miss (zero)
            "revenue_growth_yoy": -0.5,     # leaps soft miss
            "return_on_equity": -0.1,       # leaps soft miss
            "gross_margin": 0.05,           # leaps soft miss
            "distance_from_52w_high_pct": -0.9,
        }
    )
    hostile_fit = score_ticker_profile(hostile)

    assert hostile_fit.fit_score == base_fit.fit_score
    assert hostile_fit.eligible is True
    assert hostile_fit.pass_hard is True


def test_regime_gate_does_not_pre_deny_ticker() -> None:
    # The watchlist must NOT pre-deny a ticker on regime (ADR-0004: the decision
    # gate + structure_select own regime-vs-direction). TICKER_PROFILE carries no
    # regime_gates, and a BEAR-regime snapshot is still eligible at the watchlist.
    assert TICKER_PROFILE.regime_gates == {}
    s = _good_snapshot()
    s["regime"] = "bear"  # would DENY in any per-play profile
    fit = score_ticker_profile(s)
    assert fit.eligible is True


def test_profile_carries_no_strategy_specific_rule_fields() -> None:
    # No strategy-specific field name may appear in ANY rule bucket.
    strategy_specific = {
        "days_since_earnings",
        "debt_to_equity",
        "dividend_yield",
        "free_cash_flow_yield",
        "beta",
        "revenue_growth_yoy",
        "return_on_equity",
        "gross_margin",
        "rsi_14",
        "atr_14",
        "atr_pct_of_spot",
        "five_d_return_pct",
        "distance_from_52w_high_pct",
    }
    used = (
        set(TICKER_PROFILE.hard_rules)
        | set(TICKER_PROFILE.soft_rules)
        | set(TICKER_PROFILE.eviction_rules)
    )
    assert used.isdisjoint(strategy_specific)


# --------------------------------------------------------------------------- #
# Grammar reuse — score_ticker_profile must delegate to the scorers.py engine
# --------------------------------------------------------------------------- #


def test_fit_score_matches_score_against_engine() -> None:
    # score_ticker_profile must REUSE _score_against verbatim, not re-implement
    # the 0.6*hard + 0.4*soft formula. The fit_score must equal the engine's
    # score for the SAME profile + snapshot.
    s = _good_snapshot()
    fit = score_ticker_profile(s)
    engine = _score_against(TICKER_PROFILE, s)
    assert fit.fit_score == engine.score
    assert fit.pass_hard == engine.pass_hard
    assert fit.eligible == engine.eligible


def test_eviction_eval_uses_shared_grammar() -> None:
    # The composite trap uses _eval_eviction's ne_field / lt_field / gt_field ops.
    s = _good_snapshot()
    s["last_close"] = 2.0
    assert _eval_eviction(s, TICKER_PROFILE.eviction_rules["price_too_low"]) is True
    s2 = _good_snapshot()
    assert _eval_eviction(s2, TICKER_PROFILE.eviction_rules["price_too_low"]) is False


def test_hard_rule_eval_uses_shared_grammar() -> None:
    assert _eval_rule(2e6, TICKER_PROFILE.hard_rules["avg_dollar_volume_30d"]) is True
    assert _eval_rule(1e6, TICKER_PROFILE.hard_rules["avg_dollar_volume_30d"]) is False
    assert _eval_rule(None, TICKER_PROFILE.hard_rules["avg_dollar_volume_30d"]) is None


def test_non_equity_set_reused_not_reinvented() -> None:
    # The non_equity eviction must reject every member of the shared vocabulary,
    # proving it leans on scorers.NON_EQUITY_QUOTE_TYPES semantics (ne EQUITY).
    for qt in NON_EQUITY_QUOTE_TYPES:
        s = _good_snapshot()
        s["quote_type"] = qt
        fit = score_ticker_profile(s)
        assert fit.eligible is False, qt


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_empty_snapshot_does_not_crash() -> None:
    fit = score_ticker_profile({})
    assert isinstance(fit, TickerFitness)
    assert fit.eligible is False


def test_symbol_threaded_through() -> None:
    fit = score_ticker_profile({"symbol": "ABCD"})
    assert fit.symbol == "ABCD"


# --------------------------------------------------------------------------- #
# wave-review (W1 DEFECT fix): the soft-abstain-vs-genuine-fail discriminator + a
# FRACTIONAL fit_score parity test. The original suite only fed None (abstain) or
# the vol_runaway eviction rail through the soft path, and only checked fit_score
# parity on the all-1.0 snapshot — so the load-bearing "present-but-out-of-band soft
# input REJECTS while a missing soft input ABSTAINS" branch was untested (mutants survived).
# --------------------------------------------------------------------------- #
def test_present_out_of_band_soft_input_rejects_not_abstains() -> None:
    """A PRESENT but failing soft input (spread_pct way over the 0.01 ceiling) must drag
    fit_score below the eligibility floor and REJECT — distinct from a MISSING (None) soft
    input which abstains. RED-proof: forcing soft_genuinely_failed always-False (the mutant)
    wrongly ADMITS this snapshot."""
    s = _good_snapshot()
    s["spread_pct"] = 0.9          # present + far out of band (vs the 0.01 ceiling)
    s["realized_vol_30d"] = 0.01   # present + below the soft vol band too
    fit = score_ticker_profile(s)
    assert fit.eligible is False, (
        "a present-and-failing soft input must REJECT (genuine soft fail), not abstain — "
        f"got eligible={fit.eligible} fit_score={fit.fit_score}"
    )


def test_missing_soft_input_abstains_stays_eligible() -> None:
    """The CONTRAST case proving the discriminator: the SAME snapshot with the soft inputs
    MISSING (None) must ABSTAIN (silence-by-default) and stay eligible — so the reject above
    is genuinely the present-out-of-band path, not just 'any soft variation rejects'."""
    s = _good_snapshot()
    s["spread_pct"] = None
    s["realized_vol_30d"] = None
    fit = score_ticker_profile(s)
    assert fit.eligible is True, (
        "missing (None) soft inputs must ABSTAIN, not reject (silence-by-default) — "
        f"got eligible={fit.eligible} fit_score={fit.fit_score}"
    )


def test_fit_score_matches_engine_on_FRACTIONAL_snapshot() -> None:
    """fit_score must equal the underlying _score_against engine score on a snapshot whose
    score is FRACTIONAL (not 1.0) — so a hardcoded fit_score=1.0 (the surviving mutant)
    is caught. Construct a snapshot that passes hard rails but misses a soft rule -> 0<score<1."""
    from hermes_quant.playbook.scorers import _score_against  # the engine
    from hermes_quant.playbook.ticker_profile import TICKER_PROFILE
    s = _good_snapshot()
    s["spread_pct"] = 0.9  # present soft miss -> fractional score
    fit = score_ticker_profile(s)
    engine = _score_against(TICKER_PROFILE, s)
    assert fit.fit_score == engine.score, (
        f"fit_score ({fit.fit_score}) must equal the engine score ({engine.score}) verbatim"
    )
    assert 0.0 < fit.fit_score < 1.0, f"fixture must be FRACTIONAL to catch a hardcoded 1.0; got {fit.fit_score}"
