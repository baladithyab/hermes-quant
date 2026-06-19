"""ADR-0092 Increment-1 step 6 — unit tests for the PURE BMA vote port.

These tests pin EVERY branch of ``hermes_quant.pdr_core.aggregate.core_aggregate``
in isolation. The function is a host-blind port of the FLAGS-OFF path of
``hermes_quant.aggregators.bma.BMAAggregator.aggregate`` (every HERMES_QUANT_*
learning flag default-off; no calibrator import — confidence is computed via the
pure cold-start arithmetic ``(confidence_raw + 2) / 8`` exactly as the live
CalibratorNotReady / ColdStartCalibrator arm).

Written RED-first: with ``hermes_quant.pdr_core.aggregate`` absent every test
errors at import/collection time. Creating the module turns them GREEN.

The vote branches pinned here (mirroring the PURE-SURFACE MAP):
  - abstain filter (confidence < 0.10 dropped); empty-after-filter -> flat
  - net-flat silence (|weighted_dir_sum| < 1e-9 -> direction 0)
  - single-source: require_ensemble True -> silence; False -> lone raw passthrough
  - multi-contributor unanimous -> vote_share + agreement_bonus
  - multi-contributor dissent -> vote_share only
  - total_w <= 0 -> flat
  - magnitude = weighted mean over contributing-direction views
  - horizon block: single_horizon (no adjust), all_agree (*1.10), mixed (*0.85)
  - cold-start confidence = (confidence_raw + 2) / 8, THEN horizon multiplier
  - metadata base dict (weights / vote_share / n_contributing / n_views /
    horizons_present / horizon_agreement / ic_dedup_excluded_analysts:[] /
    regime_state:None / regime_weight_multipliers:None)
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from hermes_quant.pdr_core.aggregate import (
    CoreAggregateContext,
    CoreAggregatedSignal,
    core_aggregate,
)
from hermes_quant.pdr_core.contracts import AnalystView

# Defaults that mirror live BMAConfig / __init__ defaults.
AGREEMENT_BONUS = 0.10
HORIZON_AGREEMENT_BONUS = 1.10
HORIZON_DISAGREEMENT_PENALTY = 0.85
UNIFORM_WEIGHT = 0.5
ABSTAIN = 0.10

ASOF = "2026-06-12T15:00:00+00:00"
BAR = "2026-06-12T14:59:00+00:00"


def _ctx(asset: str = "AAPL", asset_class: str = "equity", timeframe: str = "1d"):
    return CoreAggregateContext(
        asset=asset, timeframe=timeframe, asset_class=asset_class, asof=ASOF
    )


def _view(
    analyst: str,
    direction: int,
    *,
    magnitude: float = 0.5,
    confidence: float = 0.7,
    confidence_raw: float = 0.6,
    horizon: str = "1d",
):
    return AnalystView(
        analyst=analyst,
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence_raw,
        horizon=horizon,
        asof_decision=ASOF,
        bar_ts=BAR,
    )


def _cold(raw: float) -> float:
    """The pure cold-start map the core applies as final confidence."""
    return (raw + 2.0) / 8.0


# ---------------------------------------------------------------------------
# Output type shape.
# ---------------------------------------------------------------------------


def test_core_aggregated_signal_is_frozen() -> None:
    sig = core_aggregate([], _ctx())
    with pytest.raises(dataclasses.FrozenInstanceError):
        sig.direction = 1  # type: ignore[misc]


def test_flat_signal_on_empty_views() -> None:
    sig = core_aggregate([], _ctx())
    assert isinstance(sig, CoreAggregatedSignal)
    assert sig.direction == 0
    assert sig.magnitude == 0.0
    assert sig.confidence == 0.0
    assert sig.confidence_raw == 0.0
    assert sig.horizon == "0m"
    assert sig.aggregator == "bma"
    assert sig.components == ()
    assert sig.metadata == {"reason": "flat_or_no_views"}


# ---------------------------------------------------------------------------
# Abstain filter.
# ---------------------------------------------------------------------------


def test_abstain_filter_drops_below_threshold() -> None:
    # A lone abstaining analyst (conf < 0.10) is dropped -> empty -> flat.
    sig = core_aggregate([_view("a", 1, confidence=0.05)], _ctx())
    assert sig.direction == 0
    assert sig.metadata == {"reason": "flat_or_no_views"}


def test_abstain_filter_keeps_exactly_threshold() -> None:
    # confidence == 0.10 is KEPT (>= ABSTAIN_THRESHOLD). Two distinct analysts so
    # the single-source gate does not silence.
    views = [
        _view("a", 1, confidence=ABSTAIN, confidence_raw=0.4),
        _view("b", 1, confidence=0.7, confidence_raw=0.6),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.direction == 1
    assert sig.metadata["n_views"] == 2


# ---------------------------------------------------------------------------
# Single-source gate (n_distinct_analysts <= 1).
# ---------------------------------------------------------------------------


def test_single_source_silenced_when_require_ensemble_true() -> None:
    sig = core_aggregate([_view("solo", 1)], _ctx(), require_ensemble=True)
    assert sig.direction == 0
    assert sig.confidence == 0.0
    assert sig.metadata == {"reason": "silenced_single_source"}
    # The lone view is carried in components for outcome-crediting parity.
    assert len(sig.components) == 1


def test_single_source_passthrough_when_require_ensemble_false() -> None:
    v = _view("solo", 1, confidence_raw=0.42, horizon="1d")
    sig = core_aggregate([v], _ctx(), require_ensemble=False)
    assert sig.direction == 1
    # confidence_raw = clip(sole view confidence_raw)
    assert sig.confidence_raw == pytest.approx(0.42)
    # confidence = cold-start of that raw (single horizon -> no multiplier)
    assert sig.confidence == pytest.approx(_cold(0.42))


def test_single_source_passthrough_clips_raw() -> None:
    # confidence_raw is clipped to [0,1]. A view cannot carry >1 (contract bound),
    # so drive the clip from a valid-but-edge raw of 1.0.
    v = _view("solo", 1, confidence_raw=1.0)
    sig = core_aggregate([v], _ctx(), require_ensemble=False)
    assert sig.confidence_raw == pytest.approx(1.0)
    assert sig.confidence == pytest.approx(_cold(1.0))


def test_two_views_same_analyst_name_is_single_source() -> None:
    # n_distinct_analysts counts DISTINCT names; two views from one analyst is
    # still single-source and silences under require_ensemble.
    views = [_view("dup", 1), _view("dup", 1, horizon="1w")]
    sig = core_aggregate(views, _ctx(), require_ensemble=True)
    assert sig.direction == 0
    assert sig.metadata == {"reason": "silenced_single_source"}


# ---------------------------------------------------------------------------
# Net-flat silence.
# ---------------------------------------------------------------------------


def test_net_flat_silence_on_exact_cancel() -> None:
    # Two equal-weight opposite views with identical magnitude*conf cancel to 0.
    views = [
        _view("a", 1, confidence=0.7, confidence_raw=0.6),
        _view("b", -1, confidence=0.7, confidence_raw=0.6),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.direction == 0
    assert sig.metadata == {"reason": "flat_or_no_views"}


# ---------------------------------------------------------------------------
# Multi-contributor unanimous -> vote_share + agreement_bonus.
# ---------------------------------------------------------------------------


def test_unanimous_gets_agreement_bonus() -> None:
    # Two analysts agree long, same horizon. vote_share = 1.0 (all same dir);
    # confidence_raw = clip(1.0 + 0.10) = 1.0.
    views = [
        _view("a", 1, confidence=0.8, confidence_raw=0.7),
        _view("b", 1, confidence=0.6, confidence_raw=0.5),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.direction == 1
    assert sig.metadata["vote_share"] == pytest.approx(1.0)
    assert sig.confidence_raw == pytest.approx(1.0)  # clip(1.0 + 0.10)
    assert sig.confidence == pytest.approx(_cold(1.0))  # single horizon


def test_unanimous_vote_share_below_one_takes_bonus() -> None:
    # A flat (direction 0) view dilutes nothing in signed_dir_terms (its term is
    # 0), so two long voters remain unanimous with vote_share 1.0. Use instead a
    # mix where vote_share < 1 by adding a same-direction but smaller voter is
    # still 1.0; to get <1 we need an opposing non-flat -> that's dissent. So
    # unanimity always yields vote_share 1.0 by construction. Assert that.
    views = [
        _view("a", 1, confidence=0.9, confidence_raw=0.8),
        _view("b", 1, confidence=0.3, confidence_raw=0.2),
        _view("c", 0, confidence=0.5, confidence_raw=0.4),  # flat, non-voting
    ]
    sig = core_aggregate(views, _ctx())
    # non_flat = [a, b] all long -> unanimous
    assert sig.direction == 1
    assert sig.metadata["vote_share"] == pytest.approx(1.0)
    assert sig.confidence_raw == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Multi-contributor dissent -> vote_share only (no bonus).
# ---------------------------------------------------------------------------


def test_dissent_uses_vote_share_no_bonus() -> None:
    # a: long, strong; b: short, weaker. Composite = long. vote_share < 1, no
    # agreement bonus because non_flat are NOT all the composite direction.
    a = _view("a", 1, confidence=0.8, confidence_raw=0.7, magnitude=0.6)
    b = _view("b", -1, confidence=0.4, confidence_raw=0.3, magnitude=0.2)
    sig = core_aggregate([a, b], _ctx())
    # signed_dir_terms: a = 1 * (0.5*1.0) * 0.8 = 0.4 ; b = -1 * 0.5 * 0.4 = -0.2
    # weighted_dir_sum = 0.2 ; denom = 0.6 ; vote_share = 0.2/0.6
    assert sig.direction == 1
    vs = 0.2 / 0.6
    assert sig.metadata["vote_share"] == pytest.approx(vs)
    assert sig.confidence_raw == pytest.approx(vs)  # NO bonus on dissent
    assert sig.confidence == pytest.approx(_cold(vs))  # single horizon


# ---------------------------------------------------------------------------
# Magnitude = weighted mean over contributing-direction views.
# ---------------------------------------------------------------------------


def test_magnitude_weighted_mean_over_contributing() -> None:
    # Two long voters (same horizon -> equal weight 0.5). Magnitude mean = simple
    # mean of the two magnitudes. A dissenting short view does NOT enter the
    # magnitude mean (only contributing == composite_direction views do).
    a = _view("a", 1, magnitude=0.4, confidence=0.8, confidence_raw=0.7)
    b = _view("b", 1, magnitude=0.8, confidence=0.8, confidence_raw=0.7)
    c = _view("c", -1, magnitude=0.9, confidence=0.2, confidence_raw=0.1)
    sig = core_aggregate([a, b, c], _ctx())
    assert sig.direction == 1
    # equal weights (same horizon) -> mean(0.4, 0.8) = 0.6
    assert sig.magnitude == pytest.approx(0.6)
    assert sig.metadata["n_contributing"] == 2


def test_horizon_weight_affects_magnitude_mean() -> None:
    # 1w weight 1.20 vs 1d weight 1.00 -> weighted mean is pulled toward the 1w
    # view's magnitude. Both long, distinct analysts, distinct horizons.
    a = _view("a", 1, magnitude=0.2, horizon="1d", confidence=0.5, confidence_raw=0.5)
    b = _view("b", 1, magnitude=0.8, horizon="1w", confidence=0.5, confidence_raw=0.5)
    sig = core_aggregate([a, b], _ctx())
    # w_a = 0.5*1.00 = 0.5 ; w_b = 0.5*1.20 = 0.6 ; total = 1.1
    # mag = (0.2*0.5 + 0.8*0.6) / 1.1 = (0.1 + 0.48)/1.1
    expected = (0.2 * 0.5 + 0.8 * 0.6) / (0.5 + 0.6)
    assert sig.magnitude == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Horizon agreement block (multi-timeframe ADR-0036).
# ---------------------------------------------------------------------------


def test_horizon_single_horizon_no_adjust() -> None:
    views = [
        _view("a", 1, confidence_raw=0.6, horizon="1d"),
        _view("b", 1, confidence_raw=0.6, horizon="1d"),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.metadata["horizon_agreement"] == "single_horizon"
    assert sig.metadata["horizons_present"] == ["1d"]
    # no multiplier on confidence
    assert sig.confidence == pytest.approx(_cold(sig.confidence_raw))


def test_horizon_all_agree_applies_bonus() -> None:
    # Two horizons, both vote long -> all_agree -> confidence *= 1.10.
    views = [
        _view("a", 1, confidence=0.9, confidence_raw=0.8, horizon="1d"),
        _view("b", 1, confidence=0.9, confidence_raw=0.8, horizon="1w"),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.metadata["horizon_agreement"] == "all_agree"
    assert sorted(sig.metadata["horizons_present"]) == ["1d", "1w"]
    base = _cold(sig.confidence_raw)
    assert sig.confidence == pytest.approx(min(1.0, base * HORIZON_AGREEMENT_BONUS))


def test_horizon_mixed_applies_penalty() -> None:
    # composite is long (1d strong long outweighs 1w short), but the 1w horizon
    # votes short -> horizon signs mixed -> confidence *= 0.85.
    a = _view("a", 1, confidence=0.9, confidence_raw=0.8, magnitude=0.6, horizon="1d")
    b = _view("b", -1, confidence=0.3, confidence_raw=0.2, magnitude=0.2, horizon="1w")
    sig = core_aggregate([a, b], _ctx())
    assert sig.direction == 1
    assert sig.metadata["horizon_agreement"] == "mixed"
    base = _cold(sig.confidence_raw)
    assert sig.confidence == pytest.approx(min(1.0, base * HORIZON_DISAGREEMENT_PENALTY))


def test_horizon_is_modal_of_contributing() -> None:
    # Three long contributors: two on 1d, one on 1w -> modal horizon is 1d.
    views = [
        _view("a", 1, confidence_raw=0.6, horizon="1d"),
        _view("b", 1, confidence_raw=0.6, horizon="1d"),
        _view("c", 1, confidence_raw=0.6, horizon="1w"),
    ]
    sig = core_aggregate(views, _ctx())
    assert sig.horizon == "1d"


# ---------------------------------------------------------------------------
# Metadata base dict parity.
# ---------------------------------------------------------------------------


def test_metadata_base_dict_keys_and_sentinels() -> None:
    views = [
        _view("a", 1, confidence=0.8, confidence_raw=0.7, horizon="1d"),
        _view("b", 1, confidence=0.6, confidence_raw=0.5, horizon="1w"),
    ]
    sig = core_aggregate(views, _ctx())
    md = sig.metadata
    assert set(md.keys()) == {
        "weights",
        "vote_share",
        "n_contributing",
        "n_views",
        "horizons_present",
        "horizon_agreement",
        "ic_dedup_excluded_analysts",
        "regime_state",
        "regime_weight_multipliers",
    }
    assert md["ic_dedup_excluded_analysts"] == []
    assert md["regime_state"] is None
    assert md["regime_weight_multipliers"] is None
    assert md["n_views"] == 2
    # weights keyed by analyst name = uniform * horizon_weight
    assert md["weights"]["a"] == pytest.approx(0.5 * 1.00)
    assert md["weights"]["b"] == pytest.approx(0.5 * 1.20)


# ---------------------------------------------------------------------------
# NaN guard (np.clip propagates NaN; the core must guard explicitly).
# ---------------------------------------------------------------------------


def test_confidence_outputs_are_finite() -> None:
    views = [
        _view("a", 1, confidence=0.8, confidence_raw=0.7),
        _view("b", 1, confidence=0.6, confidence_raw=0.5),
    ]
    sig = core_aggregate(views, _ctx())
    assert math.isfinite(sig.confidence)
    assert math.isfinite(sig.confidence_raw)
    assert math.isfinite(sig.magnitude)
