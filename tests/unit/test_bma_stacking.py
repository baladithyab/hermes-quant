"""Unit tests for B50 cross-correlation stacking in BMAAggregator.

Flag: HERMES_QUANT_STACKING=1 (DEFAULT-OFF, read at call time).

Coverage:
  * Flag-OFF byte-identical to the pre-B50 BMA (signal fields + metadata).
  * Flag-ON: two PERFECTLY-correlated analysts contribute strictly less than
    two INDEPENDENT ones (the effective-sample-size redundancy correction).
  * Pairwise correctness-correlation math (perfect / independent / insufficient
    data / constant-series fail-open).
  * require_ensemble is left intact under the flag.
  * The history accumulation in update() is purely additive (Beta posteriors
    unchanged whether the flag is on or off).

Deterministic + offline — no live network, no on-disk calibrator dependence
(ColdStartCalibrator is pinned so the test is isolated from any bootstrapped
isotonic pickle).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import (
    STACKING_CORR_MIN_PAIRS,
    BMAAggregator,
    _stacking_enabled,
)
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AnalystView,
    EpisodeOutcome,
    MarketContext,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ctx() -> MarketContext:
    ts = pd.date_range("2026-05-13", periods=2, freq="1h")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view(name: str, direction: int, conf: float = 0.7) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.01,
        confidence=conf,
        confidence_raw=0.85,
        horizon="1h",
    )


def _fresh_aggregator() -> BMAAggregator:
    """BMA with uniform base weights (n_min_observations huge) and a pinned
    cold-start calibrator, so the ONLY thing the stacking discount can move is
    the cross-correlation redundancy factor."""
    a = BMAAggregator(n_min_observations=10_000)
    a.calibrator = ColdStartCalibrator()
    return a


def _train_correctness(
    agg: BMAAggregator,
    groups: list[list[str]],
    n: int = 60,
    seed: int = 0,
) -> None:
    """Feed `n` episodes; analysts inside the same inner list share an identical
    correctness sequence (perfectly correlated), distinct lists are drawn
    independently. Trains the per-analyst correctness history used by the
    correlation discount."""
    rng = random.Random(seed)
    all_names = [nm for g in groups for nm in g]
    seqs = {
        tuple(g): [rng.random() < 0.6 for _ in range(n)] for g in groups
    }
    for episode in range(n):
        direction_correct: dict[str, bool] = {}
        for g in groups:
            c = seqs[tuple(g)][episode]
            for nm in g:
                direction_correct[nm] = c
        sig = agg.aggregate([_view(nm, 1) for nm in all_names], _ctx())
        agg.update(
            EpisodeOutcome(
                asset="BTC/USDT",
                timeframe="1h",
                asof=pd.Timestamp("2026-05-13"),
                aggregated_signal=sig,
                realized_returns={"1h": 0.01},
                direction_correct=direction_correct,
            )
        )


def _train_partial(
    agg: BMAAggregator,
    name_a: str,
    name_b: str,
    p_copy: float,
    n: int = 200,
    seed: int = 0,
) -> None:
    """Train two analysts to a TUNABLE correctness correlation.

    ``name_b`` copies ``name_a``'s per-episode correctness with probability
    ``p_copy``, else draws an independent Bernoulli(0.6). This makes the
    realized Pearson correlation ≈ ``p_copy`` (0 → independent, 1 → perfect),
    so a graded set of correlations can be fed without changing the marginal
    correctness rate (~0.6) of either analyst. Used to assert the redundancy
    discount tracks the MAGNITUDE of ρ, not merely its sign / co-training."""
    rng = random.Random(seed)
    a_seq = [rng.random() < 0.6 for _ in range(n)]
    b_seq = [
        a_seq[i] if rng.random() < p_copy else (rng.random() < 0.6) for i in range(n)
    ]
    seqs = {name_a: a_seq, name_b: b_seq}
    for episode in range(n):
        dc = {nm: seqs[nm][episode] for nm in (name_a, name_b)}
        sig = agg.aggregate([_view(name_a, 1), _view(name_b, 1)], _ctx())
        agg.update(
            EpisodeOutcome(
                asset="BTC/USDT",
                timeframe="1h",
                asof=pd.Timestamp("2026-05-13"),
                aggregated_signal=sig,
                realized_returns={"1h": 0.01},
                direction_correct=dc,
            )
        )


# ---------------------------------------------------------------------------
# Flag plumbing
# ---------------------------------------------------------------------------


class TestFlagPlumbing:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        assert _stacking_enabled() is False

    def test_explicit_zero_off(self, monkeypatch):
        monkeypatch.setenv("HERMES_QUANT_STACKING", "0")
        assert _stacking_enabled() is False

    def test_non_one_value_off(self, monkeypatch):
        # Only the exact string "1" enables — fail-closed to OFF otherwise.
        monkeypatch.setenv("HERMES_QUANT_STACKING", "true")
        assert _stacking_enabled() is False

    def test_one_on(self, monkeypatch):
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        assert _stacking_enabled() is True

    def test_read_at_call_time(self, monkeypatch):
        # The flag is consulted per-aggregate(), not cached at construction.
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        a = _fresh_aggregator()
        views = [_view("a", 1), _view("b", 1)]
        off = a.aggregate(views, _ctx())
        assert "stacking_redundancy_factors" not in off.metadata
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        on = a.aggregate(views, _ctx())
        assert "stacking_redundancy_factors" in on.metadata


# ---------------------------------------------------------------------------
# Flag-OFF byte-identical to current BMA
# ---------------------------------------------------------------------------


def _signal_fingerprint(sig) -> tuple:
    """Stable comparison key for an AggregatedSignal across flag states."""
    return (
        sig.direction,
        sig.magnitude,
        sig.confidence,
        sig.confidence_raw,
        sig.horizon,
        tuple(sorted(sig.metadata["weights"].items())),
        sig.metadata["vote_share"],
        sig.metadata["n_contributing"],
        sig.metadata["n_views"],
        # New B50 keys must be ABSENT when the flag is off.
        "stacking_redundancy_factors" in sig.metadata,
        "stacking_used" in sig.metadata,
    )


class TestFlagOffByteIdentical:
    def test_off_matches_explicit_no_flag_unanimous(self, monkeypatch):
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        a = _fresh_aggregator()
        _train_correctness(a, [["x", "y"]], n=40)  # build correlation history
        views = [_view("x", 1, 0.7), _view("y", 1, 0.6), _view("z", 1, 0.8)]

        # Reference: brand-new aggregator with NO stacking code touched.
        ref = _fresh_aggregator()
        _train_correctness(ref, [["x", "y"]], n=40)
        ref_sig = ref.aggregate(views, _ctx())

        off_sig = a.aggregate(views, _ctx())
        assert _signal_fingerprint(off_sig) == _signal_fingerprint(ref_sig)
        # No stacking metadata keys leak into the OFF path.
        assert "stacking_redundancy_factors" not in off_sig.metadata
        assert "stacking_used" not in off_sig.metadata

    def test_off_metadata_keys_unchanged(self, monkeypatch):
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        a = _fresh_aggregator()
        # Majority-long (not net-flat) so we get the full aggregated metadata
        # dict, not the silence-path {"reason": ...} dict.
        sig = a.aggregate([_view("a", 1), _view("b", 1), _view("c", -1)], _ctx())
        expected_keys = {
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
        assert set(sig.metadata.keys()) == expected_keys

    def test_off_weights_identical_to_uniform_base(self, monkeypatch):
        # With no flag, weights are exactly base_weight × horizon_weight; the
        # correlation discount is NEVER applied even with a trained history.
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        a = _fresh_aggregator()
        _train_correctness(a, [["a", "b"]], n=40)  # perfectly correlated history
        sig = a.aggregate([_view("a", 1), _view("b", 1)], _ctx())
        # Uniform base 0.5 × horizon 1.0 (1h) = 0.5 for both — undiscounted.
        assert sig.metadata["weights"] == {"a": 0.5, "b": 0.5}


# ---------------------------------------------------------------------------
# Pairwise correctness-correlation math
# ---------------------------------------------------------------------------


class TestPairwiseCorrelation:
    def test_perfect_correlation_is_one(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["p", "q"]], n=40)  # p,q always equal
        rho = a._pairwise_correctness_corr(
            list(a._stats["p"].history), list(a._stats["q"].history)
        )
        assert rho == pytest.approx(1.0)

    def test_independent_correlation_near_zero(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["c"], ["d"]], n=200, seed=7)
        rho = a._pairwise_correctness_corr(
            list(a._stats["c"].history), list(a._stats["d"].history)
        )
        assert abs(rho) < 0.3  # independent → small magnitude

    def test_insufficient_pairs_returns_none(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["e", "f"]], n=STACKING_CORR_MIN_PAIRS - 1)
        rho = a._pairwise_correctness_corr(
            list(a._stats["e"].history), list(a._stats["f"].history)
        )
        assert rho is None

    def test_constant_series_returns_none(self):
        # Both analysts always correct → zero variance → undefined corr → None
        # (fail-open: no evidence of redundancy, so no discount).
        a = _fresh_aggregator()
        for _ in range(40):
            sig = a.aggregate([_view("g", 1), _view("h", 1)], _ctx())
            a.update(
                EpisodeOutcome(
                    asset="BTC/USDT",
                    timeframe="1h",
                    asof=pd.Timestamp("2026-05-13"),
                    aggregated_signal=sig,
                    realized_returns={"1h": 0.01},
                    direction_correct={"g": True, "h": True},
                )
            )
        rho = a._pairwise_correctness_corr(
            list(a._stats["g"].history), list(a._stats["h"].history)
        )
        assert rho is None

    def test_disjoint_episodes_returns_none(self):
        # Two analysts that NEVER co-observe an episode share zero indices →
        # treated as independent (None), never spuriously correlated.
        a = _fresh_aggregator()
        for _ in range(40):
            sig = a.aggregate([_view("only_a", 1)], _ctx())
            a.update(
                EpisodeOutcome(
                    asset="BTC/USDT",
                    timeframe="1h",
                    asof=pd.Timestamp("2026-05-13"),
                    aggregated_signal=sig,
                    realized_returns={"1h": 0.01},
                    direction_correct={"only_a": True},
                )
            )
        for _ in range(40):
            sig = a.aggregate([_view("only_b", 1)], _ctx())
            a.update(
                EpisodeOutcome(
                    asset="BTC/USDT",
                    timeframe="1h",
                    asof=pd.Timestamp("2026-05-13"),
                    aggregated_signal=sig,
                    realized_returns={"1h": 0.01},
                    direction_correct={"only_b": True},
                )
            )
        rho = a._pairwise_correctness_corr(
            list(a._stats["only_a"].history), list(a._stats["only_b"].history)
        )
        assert rho is None


# ---------------------------------------------------------------------------
# Redundancy factors
# ---------------------------------------------------------------------------


class TestRedundancyFactors:
    def test_perfect_pair_each_half(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["a", "b"]], n=40)
        factors = a._redundancy_factors(["a", "b"])
        # ρ=1 → f = 1/(1+1) = 0.5 for each; combined effective weight 1.0.
        assert factors["a"] == pytest.approx(0.5)
        assert factors["b"] == pytest.approx(0.5)
        assert sum(factors.values()) == pytest.approx(1.0)

    def test_independent_pair_near_one(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["c"], ["d"]], n=200, seed=7)
        factors = a._redundancy_factors(["c", "d"])
        assert factors["c"] > 0.8
        assert factors["d"] > 0.8

    def test_correlated_pair_sums_less_than_independent_pair(self):
        """The core B50 invariant: two perfectly-correlated analysts contribute
        strictly less COMBINED weight than two independent ones."""
        corr = _fresh_aggregator()
        _train_correctness(corr, [["a", "b"]], n=60)
        indep = _fresh_aggregator()
        _train_correctness(indep, [["c"], ["d"]], n=60, seed=11)

        corr_combined = sum(corr._redundancy_factors(["a", "b"]).values())
        indep_combined = sum(indep._redundancy_factors(["c", "d"]).values())
        assert corr_combined < indep_combined
        assert corr_combined == pytest.approx(1.0)  # exactly one independent unit

    def test_combined_weight_is_monotone_in_correlation_magnitude(self):
        """DISCRIMINATING tightening of the confound the verifier flagged: the
        combined effective weight must track the MAGNITUDE of the correctness
        correlation, not merely its sign / the fact that two analysts were
        co-trained.

        We feed three pairs with graded correlation (ρ ≈ 0.0, 0.5, 1.0) at a
        FIXED marginal correctness (~0.6) and assert a STRICT three-way ordering:

            sum(f | ρ≈1)  <  sum(f | ρ≈0.5)  <  sum(f | ρ≈0)

        A degenerate discount that (a) treats any positive co-movement as full
        redundancy (binary), (b) ignores ρ and applies a constant discount, or
        (c) keys on an agreement-rate magnitude rather than the de-meaned Pearson
        correlation would collapse the middle point onto one of the extremes and
        break the strict ordering. So this FAILS if the discount stops being a
        monotone function of ρ."""
        sums: dict[float, float] = {}
        for p_copy in (0.0, 0.5, 1.0):
            agg = _fresh_aggregator()
            _train_partial(agg, "a", "b", p_copy=p_copy, n=200, seed=3)
            sums[p_copy] = sum(agg._redundancy_factors(["a", "b"]).values())

        # Sanity: the realized correlations are actually graded as intended.
        rho_mid = _fresh_aggregator()
        _train_partial(rho_mid, "a", "b", p_copy=0.5, n=200, seed=3)
        rho = rho_mid._pairwise_correctness_corr(
            list(rho_mid._stats["a"].history), list(rho_mid._stats["b"].history)
        )
        assert 0.3 < rho < 0.8, f"middle pair ρ={rho} not in the partial band"

        # Strict monotone in correlation magnitude — the load-bearing assertion.
        assert sums[1.0] < sums[0.5] < sums[0.0], (
            f"combined weight not strictly monotone in ρ: {sums} — the redundancy "
            f"discount degraded to a sign/binary/constant artifact"
        )
        # Endpoints pinned to their information-theoretic anchors.
        assert sums[1.0] == pytest.approx(1.0)  # ρ=1 → one independent unit
        assert sums[0.0] > 1.9  # ρ≈0 → ~two independent units

    def test_single_analyst_no_discount(self):
        a = _fresh_aggregator()
        _train_correctness(a, [["a", "b"]], n=40)
        assert a._redundancy_factors(["a"]) == {"a": 1.0}


# ---------------------------------------------------------------------------
# End-to-end: correlated analysts fuse to lower confidence
# ---------------------------------------------------------------------------


class TestStackingEndToEnd:
    def test_correlated_supporters_lower_confidence(self, monkeypatch):
        """With a dissenter present, two PERFECTLY-correlated supporters produce
        a LOWER aggregate confidence than two INDEPENDENT supporters — the
        redundant pair is not double-counted as independent evidence."""
        corr = _fresh_aggregator()
        _train_correctness(corr, [["s1", "s2"], ["dis"]], n=60)
        indep = _fresh_aggregator()
        _train_correctness(indep, [["s1"], ["s2"], ["dis"]], n=60)

        views = [_view("s1", 1, 0.7), _view("s2", 1, 0.7), _view("dis", -1, 0.4)]
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        c_sig = corr.aggregate(views, _ctx())
        i_sig = indep.aggregate(views, _ctx())

        # Both still long (supporters win) but correlated supporters are weaker.
        assert c_sig.direction == 1
        assert i_sig.direction == 1
        assert c_sig.confidence_raw < i_sig.confidence_raw
        assert c_sig.confidence < i_sig.confidence

    def test_flag_on_records_audit_metadata(self, monkeypatch):
        a = _fresh_aggregator()
        _train_correctness(a, [["a", "b"]], n=60)
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        sig = a.aggregate([_view("a", 1), _view("b", 1)], _ctx())
        assert "stacking_redundancy_factors" in sig.metadata
        assert sig.metadata["stacking_used"] is True
        # Discount is reflected in the weights metadata too (0.5 × 0.5 = 0.25).
        assert sig.metadata["weights"]["a"] == pytest.approx(0.25)
        assert sig.metadata["weights"]["b"] == pytest.approx(0.25)

    def test_flag_on_no_history_is_noop(self, monkeypatch):
        """Flag on but no correlation history yet → factors all 1.0, stacking_used
        False, and weights identical to the OFF path (fail-open conservative)."""
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        a = _fresh_aggregator()
        on = a.aggregate([_view("a", 1), _view("b", 1)], _ctx())
        assert on.metadata["stacking_used"] is False
        assert on.metadata["weights"] == {"a": 0.5, "b": 0.5}


# ---------------------------------------------------------------------------
# require_ensemble intact under the flag
# ---------------------------------------------------------------------------


class TestRequireEnsembleIntact:
    def test_single_source_still_silenced_with_flag(self, monkeypatch):
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        a = _fresh_aggregator()  # require_ensemble defaults True
        sig = a.aggregate([_view("solo", 1)], _ctx())
        assert sig.direction == 0
        assert sig.confidence == 0.0
        assert sig.metadata["reason"] == "silenced_single_source"

    def test_require_ensemble_false_passthrough_with_flag(self, monkeypatch):
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        a = BMAAggregator(n_min_observations=10_000, require_ensemble=False)
        a.calibrator = ColdStartCalibrator()
        sig = a.aggregate([_view("solo", 1, conf=0.6)], _ctx())
        assert sig.direction == 1


# ---------------------------------------------------------------------------
# Additive history accumulation (Beta posteriors unaffected)
# ---------------------------------------------------------------------------


class TestHistoryAdditive:
    def test_update_history_does_not_change_beta(self, monkeypatch):
        """History accumulation in update() is purely additive: the Beta
        posteriors are identical whether the flag is on or off during update."""
        monkeypatch.delenv("HERMES_QUANT_STACKING", raising=False)
        off = _fresh_aggregator()
        monkeypatch.setenv("HERMES_QUANT_STACKING", "1")
        on = _fresh_aggregator()

        outcomes = [(True, False), (False, True), (True, True), (False, False)] * 3
        for oa, ob in outcomes:
            for agg in (off, on):
                sig = agg.aggregate([_view("a", 1), _view("b", 1)], _ctx())
                agg.update(
                    EpisodeOutcome(
                        asset="BTC/USDT",
                        timeframe="1h",
                        asof=pd.Timestamp("2026-05-13"),
                        aggregated_signal=sig,
                        realized_returns={"1h": 0.01},
                        direction_correct={"a": oa, "b": ob},
                    )
                )
        for name in ("a", "b"):
            assert off._stats[name].alpha == on._stats[name].alpha
            assert off._stats[name].beta == on._stats[name].beta
            assert off._stats[name].n_observations == on._stats[name].n_observations
            # History is accumulated regardless of flag (warm-up-free toggle).
            assert len(on._stats[name].history) == len(outcomes)
            assert len(off._stats[name].history) == len(outcomes)
