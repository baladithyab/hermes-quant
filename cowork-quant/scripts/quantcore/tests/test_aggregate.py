"""Aggregator (B-05): Beta-binomial weights + margin-gated weighted vote.

Per AGENTS.md, the silence paths get at least as much coverage as the action
paths: zero-weight floors, near-split margin silencing, cold-start fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
UTC = timezone.utc

import pytest

from quantcore.aggregate import aggregate, analyst_weight, shrink_confidence
from quantcore.schemas import AnalystView

ASOF = datetime(2026, 6, 9, 14, 0, tzinfo=UTC)


def _view(analyst, direction=1, confidence=0.6, magnitude=0.02, rationale=""):
    return AnalystView(
        analyst=analyst,
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        horizon="5d",
        asof_decision=ASOF,
        rationale=rationale,
    )


def _cal(n, n_correct, bucket="0.6"):
    return {bucket: {"n": n, "n_correct": n_correct}}


# Two well-calibrated analysts: 8/10 -> Beta(9,3) -> mean 0.75 -> weight 0.5.
STRONG = {"alpha-ta": _cal(10, 8), "beta-fund": _cal(10, 8), "gamma-cat": _cal(10, 8)}


# --- analyst_weight: Beta posterior math ------------------------------------


def test_weight_beta_posterior_7_of_10_uniform_prior():
    # Tallies summed ACROSS buckets: 4/6 + 3/4 = 7/10.
    cal = {"a": {"0.6": {"n": 6, "n_correct": 4}, "0.7": {"n": 4, "n_correct": 3}}}
    # Beta(1+7, 1+3) -> posterior mean 8/12 = 2/3 -> weight 2*(2/3)-1 = 1/3.
    assert analyst_weight(cal, "a") == pytest.approx(1.0 / 3.0)


def test_weight_cold_start_is_prior_mean_zero_weight():
    assert analyst_weight({}, "a") == 0.0  # Beta(1,1) mean 0.5 -> weight 0
    assert analyst_weight({"other": _cal(10, 9)}, "a") == 0.0  # no data for "a"


def test_weight_informative_prior_respected():
    # Beta(3,1) prior, no data -> mean 0.75 -> weight 0.5.
    assert analyst_weight({}, "a", prior_alpha=3.0, prior_beta=1.0) == pytest.approx(0.5)


def test_weight_floor_at_chance_and_below_never_negative():
    assert analyst_weight({"a": _cal(10, 5)}, "a") == 0.0  # mean 6/12 = 0.50 exactly
    assert analyst_weight({"a": _cal(10, 2)}, "a") == 0.0  # mean 0.25 -> floored, NOT -0.5
    assert analyst_weight({"a": _cal(50, 0)}, "a") == 0.0  # pathological: still zero


# --- aggregate: action path ---------------------------------------------------


def test_unanimous_committee_gets_agreement_bonus_and_weighted_means():
    views = [
        _view("alpha-ta", confidence=0.6, magnitude=0.02),
        _view("beta-fund", confidence=0.6, magnitude=0.04),
    ]
    out = aggregate(views, STRONG)
    assert out["direction"] == 1
    # equal weights (0.5 each): mean conf 0.6 + 0.03 bonus
    assert out["confidence"] == pytest.approx(0.63)
    assert out["magnitude"] == pytest.approx(0.03)
    assert out["dissent"] == ""
    assert out["n_distinct_analysts"] == 2
    assert out["weights"] == {"alpha-ta": pytest.approx(0.5), "beta-fund": pytest.approx(0.5)}


def test_confidence_capped_at_max_confidence():
    views = [_view("alpha-ta", confidence=0.74), _view("beta-fund", confidence=0.78)]
    out = aggregate(views, STRONG)
    # mean 0.76 + 0.03 bonus = 0.79 -> capped
    assert out["confidence"] == pytest.approx(0.75)


def test_weights_reported_from_calibration():
    cal = {"a": {"0.6": {"n": 6, "n_correct": 4}, "0.7": {"n": 4, "n_correct": 3}},
           "b": _cal(10, 8)}
    out = aggregate([_view("a"), _view("b")], cal)
    assert out["weights"]["a"] == pytest.approx(1.0 / 3.0)
    assert out["weights"]["b"] == pytest.approx(0.5)


# --- aggregate: margin rule + dissent ----------------------------------------


def test_near_split_margin_silences_direction():
    views = [
        _view("alpha-ta", direction=1, confidence=0.60),
        _view("beta-fund", direction=-1, confidence=0.58, rationale="distribution day"),
    ]
    out = aggregate(views, STRONG)
    # |0.5*0.60 - 0.5*0.58| / (0.5*1.18) = 0.0169... < 0.10 -> silence
    assert out["direction"] == 0
    assert out["confidence"] == 0.0
    assert out["magnitude"] == 0.0
    # the losing (vs raw vote sign) view is STILL recorded
    assert out["dissent"] == "beta-fund: distribution day"


def test_dissent_captures_losing_view_verbatim():
    cal = {"bull-ta": _cal(10, 8), "bear-ta": _cal(10, 6)}  # weights 0.5 vs 1/6
    views = [
        _view("bull-ta", direction=1, confidence=0.70, rationale="trend up, volume confirms"),
        _view("bear-ta", direction=-1, confidence=0.55, rationale="RSI overbought; fade the move"),
    ]
    out = aggregate(views, cal)
    assert out["direction"] == 1
    assert out["dissent"] == "bear-ta: RSI overbought; fade the move"
    # winning-side confidence diluted by the dissenter's weight:
    # 0.5*0.70 / (0.5 + 1/6) = 0.525; no bonus (not unanimous)
    assert out["confidence"] == pytest.approx(0.525)
    assert out["magnitude"] == pytest.approx(0.02)  # winning side only


# --- aggregate: cold-start fallback -------------------------------------------


def test_cold_start_falls_back_to_unweighted_with_null_weights():
    views = [_view("a", confidence=0.6), _view("b", confidence=0.7)]
    out = aggregate(views, {})  # nobody has a track record
    assert out["direction"] == 1  # committee not bricked
    assert out["confidence"] == pytest.approx((0.6 + 0.7) / 2 + 0.03)
    assert out["weights"] == {"a": None, "b": None}  # null signals cold-start


def test_all_at_or_below_chance_also_falls_back():
    cal = {"a": _cal(10, 5), "b": _cal(10, 3)}  # both weight 0
    out = aggregate([_view("a", confidence=0.6), _view("b", confidence=0.7)], cal)
    assert out["direction"] == 1
    assert out["weights"] == {"a": None, "b": None}


def test_cold_start_still_respects_margin_rule():
    views = [
        _view("a", direction=1, confidence=0.60),
        _view("b", direction=-1, confidence=0.58, rationale="no"),
    ]
    out = aggregate(views, {})
    assert out["direction"] == 0 and out["confidence"] == 0.0
    assert out["weights"] == {"a": None, "b": None}


# --- aggregate: flat views + determinism + edges ------------------------------


def test_flat_views_dilute_confidence_but_count_in_total_weight():
    two = [_view("alpha-ta", confidence=0.7), _view("beta-fund", confidence=0.7)]
    with_flat = two + [_view("gamma-cat", direction=0, confidence=0.6)]
    out2 = aggregate(two, STRONG)
    out3 = aggregate(with_flat, STRONG)
    assert out3["direction"] == 1  # margin 1.4/2.0 = 0.7 still clears
    # flat view adds total weight but no winning confidence; bonus also lost
    assert out3["confidence"] == pytest.approx(1.4 * 0.5 / (3 * 0.5))  # 0.4667
    assert out3["confidence"] < out2["confidence"]


def test_deterministic_under_view_reordering():
    views = [
        _view("alpha-ta", direction=1, confidence=0.65, magnitude=0.03, rationale="up"),
        _view("beta-fund", direction=-1, confidence=0.55, magnitude=0.02, rationale="rich"),
        _view("gamma-cat", direction=1, confidence=0.60, magnitude=0.05, rationale="8-K"),
    ]
    out_fwd = aggregate(views, STRONG)
    out_rev = aggregate(list(reversed(views)), STRONG)
    out_rot = aggregate(views[1:] + views[:1], STRONG)
    assert out_fwd == out_rev == out_rot


def test_empty_views_is_silence():
    out = aggregate([], STRONG)
    assert out == {
        "direction": 0,
        "magnitude": 0.0,
        "confidence": 0.0,
        "dissent": "",
        "n_distinct_analysts": 0,
        "weights": {},
    }


def test_output_has_exactly_the_contract_keys():
    out = aggregate([_view("a"), _view("b")], {})
    assert set(out) == {
        "direction", "magnitude", "confidence", "dissent", "n_distinct_analysts", "weights",
    }


# --- shrink_confidence ---------------------------------------------------------


def test_shrink_noop_at_or_below_threshold():
    assert shrink_confidence(0.8, 0.10) == 0.8
    assert shrink_confidence(0.8, 0.0) == 0.8


def test_shrink_pulls_toward_half_above_threshold():
    # factor 1 - 0.2 = 0.8: 0.5 + 0.3*0.8 = 0.74
    assert shrink_confidence(0.8, 0.2) == pytest.approx(0.74)
    # symmetric below 0.5
    assert shrink_confidence(0.2, 0.2) == pytest.approx(0.26)


def test_shrink_factor_floored_at_half():
    # ece 0.9 -> min(ece, 0.5) = 0.5 -> factor 0.5: 0.5 + 0.3*0.5 = 0.65
    assert shrink_confidence(0.8, 0.9) == pytest.approx(0.65)
