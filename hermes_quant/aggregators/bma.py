"""hermes_quant.aggregators.bma — Bayesian Model Averaging across analyst views.

Per ADR-0003: aggregator combines AnalystViews into a single AggregatedSignal.

BMA approach (v0.1.1):
- Each analyst i has a posterior weight w_i derived from its track record
  via Beta-binomial conjugacy:
      Beta(α_i, β_i) prior, updated by RealizedOutcome.direction_correct
      α_i = α_0 + n_correct_i,  β_i = β_0 + n_incorrect_i
      P(correct | analyst_i) ≈ α_i / (α_i + β_i)
- The aggregated direction is the SIGN of:
      Σ_i w_i × view_i.direction × view_i.confidence
  where w_i is the posterior expected accuracy.
- The aggregated magnitude is a weighted mean of contributing magnitudes.
- The aggregated confidence is the weighted vote share (agreement metric):
      |Σ_i w_i × view_i.direction × view_i.confidence| / Σ_i w_i × view_i.confidence
- Pre-calibration: while no analyst has its 200-sample threshold met, use
  uniform weights (w_i = 1/N) — the safe default per ADR-0009 §P1-12.

Post-aggregation calibration:
- The aggregator has its own calibrator (ColdStartCalibrator initially,
  switching to IsotonicCalibrator after 200 outcomes via update()).
- confidence_raw on AggregatedSignal is the pre-calibration vote share.
- confidence is the calibrated probability (cold-start: shrinkage of 0.20).

Per ADR-0009 §P1-10:
- update() takes EpisodeOutcome (cross-sectional snapshot of all analysts at
  a single timestamp) — required for stacking/RL learning correlations.
- v0.1.1 BMA only updates per-analyst Beta posteriors; correlations are
  not yet exploited (deferred to StackingAggregator in v0.1.3).
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator, IsotonicCalibrator
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    CalibratorNotReady,
    Direction,
    EpisodeOutcome,
    MarketContext,
)

# ICDedupGate is an optional dependency — import lazily to avoid circular
# issues and to allow the BMA module to load even if hermes_quant.factors is
# not yet installed (e.g. partial deploys).
try:
    from hermes_quant.factors.ic_dedup import ICDedupGate as _ICDedupGate
except ImportError:  # pragma: no cover
    _ICDedupGate = None  # type: ignore[assignment,misc]

# RegimeDetector is an optional dependency (Wave 7).  When None (default) the
# aggregator behaves identically to pre-Wave-7 code (bit-identical output).
try:
    from hermes_quant.regime.detector import RegimeDetector as _RegimeDetector
    from hermes_quant.regime.per_regime_weights import apply_regime_weights as _apply_regime_weights
    from hermes_quant.regime.state_variables import compute_state_variables as _compute_state_variables
except ImportError:  # pragma: no cover
    _RegimeDetector = None  # type: ignore[assignment,misc]
    _apply_regime_weights = None  # type: ignore[assignment,misc]
    _compute_state_variables = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Multi-timeframe configuration (ADR-0036)
# ---------------------------------------------------------------------------

# Default per-horizon weight multipliers per ADR-0036 §"Cross-horizon BMA
# aggregation" table:
#   1d → 1.00 (reference baseline)
#   1w → 1.20 (trend confirmation reduces noise)
#   1M → 0.80 (useful for thesis confirmation but signal lags reality)
#   1Q → 0.60 (low-frequency: useful for rebalance flagging, weak for entry)
DEFAULT_HORIZON_WEIGHTS: dict[str, float] = {
    "1d": 1.00,
    "1w": 1.20,
    "1M": 0.80,
    "1Q": 0.60,
}

# Multi-timeframe agreement adjustments (ADR-0036 §"Multi-timeframe agreement
# bonus"). Applied AFTER per-view calibration:
#   all-agree across distinct horizons → confidence × 1.10 (capped at 1.0)
#   any disagreement across horizons   → confidence × 0.85
#   single horizon present             → no adjustment (no signal to compare)
HORIZON_AGREEMENT_BONUS = 1.10
HORIZON_DISAGREEMENT_PENALTY = 0.85


@dataclass
class BMAConfig:
    """Tunable configuration for BMAAggregator (ADR-0036 amendment).

    The defaults mirror pre-ADR-0036 behavior except for the new
    horizon_weights field, which is an additive enhancement: views without an
    entry in this dict get weight 1.0 (no suppression), so single-horizon
    callers see no behavioral change.
    """

    horizon_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_HORIZON_WEIGHTS)
    )
    horizon_agreement_bonus: float = HORIZON_AGREEMENT_BONUS
    horizon_disagreement_penalty: float = HORIZON_DISAGREEMENT_PENALTY

# Canonical persistence location for the bootstrapped IsotonicCalibrator.
# Mirrors hermes_quant.training.bootstrap_calibrator.DEFAULT_CALIBRATOR_PATH;
# we don't import that module here to keep the BMA dependency surface clean.
_DEFAULT_CALIBRATOR_PATH = Path.home() / ".hermes" / "quant" / "calibrators" / "isotonic.pkl"

logger = logging.getLogger(__name__)


# Per ADR-0018 §D4: views with confidence below this threshold are
# treated as abstains and dropped from aggregation. KronosAnalyst (and
# any future foundation-model analyst) emits zero-confidence views on
# weight-load failure; the threshold is positive (not zero) to handle
# floating-point noise from calibrators.
ABSTAIN_THRESHOLD = 0.10


@dataclass
class _AnalystStats:
    """Per-analyst Beta-binomial posterior."""

    name: str
    alpha: float
    beta: float
    n_observations: int = 0

    @property
    def posterior_accuracy(self) -> float:
        """Expected directional accuracy under Beta(alpha, beta)."""
        return self.alpha / (self.alpha + self.beta)


class BMAAggregator:
    """Bayesian model averaging aggregator.

    Per ADR-0003 + ADR-0009 §P0-2 + §P1-10.

    Discoverable via [project.entry-points."hermes_quant.aggregators"] = "bma".

    Args:
        prior_alpha: Beta prior alpha (uniform-ish; default 5 = mild prior).
        prior_beta: Beta prior beta (default 5).
        n_min_observations: below this, use uniform weights (avoid noisy posteriors).
        agreement_bonus: in [0, 1]; bumps confidence_raw when all weighted
            voters agree on direction. Default 0.10.
    """

    name = "bma"

    def __init__(
        self,
        *,
        prior_alpha: float = 5.0,
        prior_beta: float = 5.0,
        n_min_observations: int = 30,
        agreement_bonus: float = 0.10,
        require_ensemble: bool = True,
        calibrator_path: Path | None = None,
        config: BMAConfig | None = None,
        ic_dedup_gate=None,
        regime_detector=None,
    ):
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self.n_min_observations = n_min_observations
        self.agreement_bonus = agreement_bonus
        # `require_ensemble`: when True (default, added 2026-05-26), the
        # aggregator silences candidates with n_contributing < 2. BMA's
        # value-add is ensemble disagreement-resolution; with one voice
        # there's no ensemble. The previous behavior produced
        # confidence=1.00 because vote_share collapses to 1.0 for a
        # single contributor (|w*d*c| / |w*d*c| = 1.0). When False, the
        # aggregator falls back to the lone analyst's confidence_raw —
        # honest but still actionable. Set False only in tests or in
        # research configurations where you explicitly want single-
        # source signals to flow through.
        self.require_ensemble = require_ensemble

        # Multi-timeframe config (ADR-0036). Default-on for cross-horizon
        # weighting; the table contains 1d=1.00 so single-horizon callers
        # see no behavior change.
        self.config = config or BMAConfig()
        self.horizon_weights = dict(self.config.horizon_weights)

        self._stats: dict[str, _AnalystStats] = {}

        # Calibrator: prefer a fitted IsotonicCalibrator persisted on disk;
        # fall back to ColdStartCalibrator on missing-file or any unpickle
        # error. The fallback path preserves silence-by-default — a corrupt
        # pickle does NOT crash the aggregator, it just downgrades to
        # cold-start (which is the conservative default).
        self.calibrator_path = (
            Path(calibrator_path) if calibrator_path is not None else _DEFAULT_CALIBRATOR_PATH
        )
        self.calibrator = self._load_calibrator(self.calibrator_path)

        self._n_aggregated = 0
        self._last_aggregated_at: pd.Timestamp | None = None

        # IC dedup gate (Wave 6b).  When None (default), behavior is
        # bit-identical to pre-Wave-6 BMA.  When provided, analyst views whose
        # returns would be rejected by the gate are excluded from aggregation;
        # the exclusion list is recorded in signal metadata under the key
        # ``ic_dedup_excluded_analysts``.
        self.ic_dedup_gate = ic_dedup_gate

        # Regime detector (Wave 7).  When None (default), behavior is
        # bit-identical to pre-Wave-7 BMA.  When provided, the detector
        # classifies the current regime from context.bars and multiplies
        # per-analyst weights by regime-specific multipliers BEFORE the vote-
        # share calculation.  The regime state and multipliers are recorded in
        # AggregatedSignal.metadata under 'regime_state' and
        # 'regime_weight_multipliers'.
        self.regime_detector = regime_detector

    @staticmethod
    def _load_calibrator(path: Path):
        """Load IsotonicCalibrator from `path`, or return ColdStartCalibrator on any error.

        Money-software discipline: a corrupt or schema-shifted pickle must
        NOT propagate. The aggregator falls back to the cold-start calibrator
        (whose Beta(2,5) prior caps confidence at 0.375 — silence-by-default
        for the cost gate). The fallback is logged at WARNING so operators
        can catch a stale/bad calibrator file in journalctl.
        """
        try:
            if not path.exists():
                logger.info(
                    "BMAAggregator: no persisted calibrator at %s; using ColdStartCalibrator",
                    path,
                )
                return ColdStartCalibrator()
            with open(path, "rb") as f:
                obj = pickle.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "BMAAggregator: failed to load calibrator from %s (%s); falling back "
                "to ColdStartCalibrator",
                path,
                exc,
            )
            return ColdStartCalibrator()

        if not isinstance(obj, IsotonicCalibrator):
            logger.warning(
                "BMAAggregator: pickle at %s is %s, not IsotonicCalibrator; "
                "falling back to ColdStartCalibrator",
                path,
                type(obj).__name__,
            )
            return ColdStartCalibrator()
        if not getattr(obj, "is_calibrated", False):
            logger.warning(
                "BMAAggregator: IsotonicCalibrator at %s is not fitted; "
                "falling back to ColdStartCalibrator",
                path,
            )
            return ColdStartCalibrator()

        logger.info(
            "BMAAggregator: loaded fitted IsotonicCalibrator from %s (n_samples=%d)",
            path,
            obj.n_samples,
        )
        return obj

    def _get_or_create_stats(self, analyst_name: str) -> _AnalystStats:
        if analyst_name not in self._stats:
            self._stats[analyst_name] = _AnalystStats(
                name=analyst_name,
                alpha=self.prior_alpha,
                beta=self.prior_beta,
            )
        return self._stats[analyst_name]

    def _weight_for(self, analyst_name: str) -> float:
        """Posterior accuracy if calibrated, else uniform proxy."""
        stats = self._get_or_create_stats(analyst_name)
        if stats.n_observations < self.n_min_observations:
            # Uniform proxy: use 0.5 (no information)
            return 0.5
        return stats.posterior_accuracy

    def _horizon_weight(self, horizon: str) -> float:
        """Per-ADR-0036 horizon weight multiplier.

        Unknown horizons default to 1.0 (no suppression) so adding a new
        horizon to AnalystView.horizon doesn't silently downweight views.
        """
        return float(self.horizon_weights.get(horizon, 1.0))

    def aggregate(
        self,
        views: list[AnalystView],
        context: MarketContext,
    ) -> AggregatedSignal:
        """Combine analyst views into a single signal.

        Per ADR-0003:
        - Empty views → flat signal with confidence=0
        - Each view contributes view.direction × view.confidence × posterior_weight
        - Direction = sign of weighted sum
        - Magnitude = weighted mean of contributing magnitudes
        - confidence_raw = vote share (|net| / |total|), bumped by agreement_bonus
        - confidence = calibrator(confidence_raw)

        Per ADR-0018 §D4 (abstain filter):
        - Views with confidence < ABSTAIN_THRESHOLD are dropped BEFORE
          aggregation. This is the structural pruning that prevents
          KronosAnalyst's zero-confidence abstain (or any analyst's
          'I have no view' signal) from polluting:
            (a) the silence-bias gate's `min_analysts_emitted` count
                (which uses len(analyst_views))
            (b) the BMA vote share itself
          Without this filter, an abstaining analyst still counts as a
          'voice' even though it provided no signal.
        """
        # ADR-0018 §D4 abstain filter
        views = [v for v in (views or []) if v.confidence >= ABSTAIN_THRESHOLD]

        if not views:
            return self._flat_signal(context)

        # Wave 6b: IC dedup gate filter.
        # When ic_dedup_gate is None this block is entirely skipped —
        # bit-identical pre-Wave-6 behavior is preserved.
        ic_dedup_excluded: list[str] = []
        if self.ic_dedup_gate is not None:
            # Track which analysts have been kept so far so we never exclude
            # the LAST representative of a correlated cluster — IC dedup
            # semantics is "keep one, discard near-duplicates", not
            # "discard everything correlated".
            kept_so_far: list[str] = []
            filtered_views = []
            for v in views:
                analyst_name = v.analyst
                if (
                    analyst_name in self.ic_dedup_gate
                    and len(self.ic_dedup_gate.library) > 1
                    and len(kept_so_far) > 0  # only run dedup once we have a representative
                ):
                    # Check the candidate against the analysts ALREADY KEPT
                    # (not the full library minus self) — this is the
                    # cluster-representative semantics that prevents the
                    # all-correlated → all-excluded degenerate case.
                    lib_kept = {
                        k: self.ic_dedup_gate.library[k]
                        for k in kept_so_far
                        if k in self.ic_dedup_gate
                    }
                    if lib_kept:
                        result = self.ic_dedup_gate.check(
                            self.ic_dedup_gate.library[analyst_name],
                            existing_library=lib_kept,
                        )
                        if not result.passes:
                            ic_dedup_excluded.append(analyst_name)
                            logger.debug(
                                "BMA: ic_dedup_gate excluded analyst %r "
                                "(%s)",
                                analyst_name,
                                result.reason,
                            )
                            continue
                kept_so_far.append(analyst_name)
                filtered_views.append(v)
            views = filtered_views

        # Wave 7: regime-aware weight adjustment.
        # When regime_detector is None (default) this block is entirely skipped —
        # bit-identical pre-Wave-7 behavior is preserved.
        # Regime applies AFTER IC dedup (same analysts always survive both
        # filters) and BEFORE the vote-share calculation.
        regime_state_value: str | None = None
        regime_weight_multipliers: dict[str, float] | None = None
        if self.regime_detector is not None and _apply_regime_weights is not None and _compute_state_variables is not None:
            try:
                bars_df = getattr(context, "bars", None)
                if bars_df is not None and len(bars_df) >= 2:
                    state_vars = _compute_state_variables(bars_df)
                    regime_state, _regime_reason = self.regime_detector.classify(state_vars)
                    regime_state_value = str(regime_state.value)
                    # Build base weight map (analyst → base_w) so we can record multipliers
                    base_weights_map = {v.analyst: self._weight_for(v.analyst) for v in views}
                    adjusted_map = _apply_regime_weights(base_weights_map, regime_state)
                    regime_weight_multipliers = {
                        analyst: (
                            adjusted_map[analyst] / base_weights_map[analyst]
                            if base_weights_map[analyst] > 1e-12
                            else 1.0
                        )
                        for analyst in base_weights_map
                    }
                    # Stash for the weights loop below (analyst → adjusted weight)
                    _regime_adjusted: dict[str, float] = adjusted_map
                    logger.debug(
                        "BMA: regime=%s, multipliers=%s",
                        regime_state_value,
                        regime_weight_multipliers,
                    )
                else:
                    _regime_adjusted = {}
                    regime_state_value = "unknown"
                    logger.debug(
                        "BMA: regime_detector set but bars unavailable/empty; "
                        "regime_state=unknown, no weight adjustment"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "BMA: regime_detector raised %s; skipping regime adjustment", exc
                )
                _regime_adjusted = {}
        else:
            _regime_adjusted = {}

        weights = []
        signed_terms = []  # direction × magnitude × weight × confidence
        signed_dir_terms = []  # direction × weight × confidence (for direction)
        for v in views:
            # Base weight: regime-adjusted if available, else posterior/uniform.
            if _regime_adjusted and v.analyst in _regime_adjusted:
                base_w = _regime_adjusted[v.analyst]
            else:
                base_w = self._weight_for(v.analyst)
            # ADR-0036: cross-horizon BMA weighting —
            #   effective weight = base_weight(analyst) × horizon_weight(horizon)
            #   The view's own confidence is multiplied in by signed_dir_terms
            #   below (so the full ADR formula is base × horizon × confidence).
            h_w = self._horizon_weight(v.horizon)
            w = base_w * h_w
            weights.append(w)
            signed_dir_terms.append(v.direction * w * v.confidence)

        weighted_dir_sum = sum(signed_dir_terms)
        if abs(weighted_dir_sum) < 1e-9:
            # Net flat — silence
            return self._flat_signal(context)

        composite_direction: Direction = 1 if weighted_dir_sum > 0 else -1

        # Magnitude: weighted mean of magnitudes from contributing-direction views
        contributing = [
            (v, w)
            for v, w in zip(views, weights, strict=False)
            if v.direction == composite_direction
        ]
        total_w = sum(w for _, w in contributing)
        if total_w <= 0:
            return self._flat_signal(context)
        magnitude = sum(v.magnitude * w for v, w in contributing) / total_w

        # Vote share: |net signed| / Σ |contribution magnitude|
        denom = sum(abs(t) for t in signed_dir_terms)
        if denom <= 0:
            vote_share = 0.0
        else:
            vote_share = abs(weighted_dir_sum) / denom

        # Single-contributor degenerate-confidence guard (added 2026-05-26
        # after MoA committee + production scan surfaced 24 picks at
        # confidence=1.00 driven by lone Kronos votes — silence-by-default
        # in TA/microstructure was working correctly, but BMA's vote_share
        # mathematically collapses to 1.0 when n_contributing == 1 because
        # `|w*d*c| / |w*d*c| = 1.0`, which then takes the agreement_bonus
        # AND looks like full unanimity to downstream gates).
        #
        # Two-tier fix:
        #   1. n_contributing == 1 → use the lone analyst's own
        #      confidence_raw (not vote_share). The "ensemble" is a single
        #      voice; BMA reports honestly that the strength is whatever
        #      that voice claimed.
        #   2. The require_ensemble flag (default True) makes single-
        #      source signals silence at the aggregator. This honors
        #      AGENTS.md silence-by-default — BMA's value-add is ensemble
        #      disagreement-resolution, so with one voice we should
        #      either pass through (require_ensemble=False) or silence
        #      (require_ensemble=True). Default is silence.
        # Single-source guard: count DISTINCT analysts in the input views
        # (not just `contributing` to composite_direction). This is the
        # right anti-degeneracy criterion — a single Kronos vote with all
        # other analysts abstaining IS single-source; two analysts on
        # different horizons disagreeing on direction is NOT (the
        # disagreement IS the ensemble signal). Silencing on the latter
        # would discard genuine multi-source dissent.
        n_distinct_analysts = len({v.analyst for v in views})
        non_flat = [v for v in views if v.direction != 0]
        if n_distinct_analysts <= 1:
            if self.require_ensemble:
                # Silence: log that we had a single-source candidate so
                # operators can debug analyst dropouts. Carry the lone
                # view in components so the calibrator-update loop can
                # still credit per-analyst outcomes.
                lone = views[0] if views else None
                logger.debug(
                    "BMA: silencing %s — n_distinct_analysts=%d (require_ensemble=True). "
                    "Sole analyst was %s with raw_conf=%.3f",
                    context.asset if context else "?",
                    n_distinct_analysts,
                    lone.analyst if lone else "none",
                    lone.confidence_raw if lone else 0.0,
                )
                return self._flat_signal(
                    context,
                    components=tuple(views),
                    reason="silenced_single_source",
                )
            # Pass-through: keep the aggregator alive but report honest
            # confidence. Downstream gates / sizing decide based on the
            # analyst's actual confidence, not synthesized unanimity.
            sole_v, _w = contributing[0]
            confidence_raw = float(np.clip(sole_v.confidence_raw, 0.0, 1.0))
        elif non_flat and all(v.direction == composite_direction for v in non_flat):
            # Multi-contributor unanimous: vote_share + agreement bonus.
            confidence_raw = float(np.clip(vote_share + self.agreement_bonus, 0.0, 1.0))
        else:
            # Multi-contributor with dissent: vote_share only.
            confidence_raw = vote_share

        # Calibrate. CalibratorNotReady fallback uses the same Beta(2,5)
        # prior as ColdStartCalibrator (ADR-0009 §P0-2 amendment 2026-05-26):
        # see hermes_quant/calibrators.py and
        # docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md.
        try:
            confidence = self.calibrator.calibrate(confidence_raw)
        except CalibratorNotReady:
            confidence = (confidence_raw + 2.0) / 8.0

        # Horizon: use the modal horizon among contributing views; default to first
        horizons = [v.horizon for v, _ in contributing]
        horizon = max(set(horizons), key=horizons.count) if horizons else views[0].horizon

        # ADR-0036: multi-timeframe agreement adjustment.
        # `horizons_present` is the set of distinct horizons across ALL views
        # (not just contributing) — a disagreeing horizon, even if it loses
        # the direction vote, still signals divergence.
        horizons_present = sorted({v.horizon for v in views})
        if len(horizons_present) <= 1:
            # Only one horizon survived — no cross-horizon signal to compare.
            horizon_agreement = "single_horizon"
        else:
            # Check whether every distinct horizon, taken collectively, votes
            # in the same direction as the composite. We treat each horizon as
            # the sign of its own weighted-sum so that mixed-direction analysts
            # within a single horizon don't flip the verdict.
            per_horizon_dir: dict[str, float] = {}
            for v, w in zip(views, weights, strict=False):
                per_horizon_dir[v.horizon] = (
                    per_horizon_dir.get(v.horizon, 0.0) + v.direction * w * v.confidence
                )
            horizon_signs = {
                h: (1 if s > 0 else (-1 if s < 0 else 0)) for h, s in per_horizon_dir.items()
            }
            non_zero_signs = [s for s in horizon_signs.values() if s != 0]
            if non_zero_signs and all(s == composite_direction for s in non_zero_signs):
                horizon_agreement = "all_agree"
                confidence = float(
                    np.clip(confidence * self.config.horizon_agreement_bonus, 0.0, 1.0)
                )
            else:
                horizon_agreement = "mixed"
                confidence = float(
                    np.clip(confidence * self.config.horizon_disagreement_penalty, 0.0, 1.0)
                )

        signal = AggregatedSignal(
            asset=context.asset,
            timeframe=context.timeframe,
            asset_class=context.asset_class,
            asof=context.asof,
            direction=composite_direction,
            magnitude=float(magnitude),
            confidence=float(confidence),
            confidence_raw=float(confidence_raw),
            horizon=horizon,
            components=tuple(views),
            aggregator=self.name,
            metadata={
                "weights": {v.analyst: w for v, w in zip(views, weights, strict=False)},
                "vote_share": float(vote_share),
                "n_contributing": len(contributing),
                "n_views": len(views),
                # ADR-0036 audit fields
                "horizons_present": horizons_present,
                "horizon_agreement": horizon_agreement,
                # Wave 6b: IC dedup audit
                "ic_dedup_excluded_analysts": ic_dedup_excluded,
                # Wave 7: regime audit (None when regime_detector is not set)
                "regime_state": regime_state_value,
                "regime_weight_multipliers": regime_weight_multipliers,
            },
        )
        self._n_aggregated += 1
        self._last_aggregated_at = context.asof
        return signal

    def _flat_signal(
        self,
        context: MarketContext,
        components: tuple = (),
        reason: str = "flat_or_no_views",
    ) -> AggregatedSignal:
        """Construct a flat AggregatedSignal (silence).

        ``components`` is the original views list — included so that
        ``update()`` (calibration loop) can still credit per-analyst
        outcomes even when the aggregator silenced. Default empty for
        backward compatibility with the upstream callers that already
        passed no components.
        """
        return AggregatedSignal(
            asset=context.asset,
            timeframe=context.timeframe,
            asset_class=context.asset_class,
            asof=context.asof,
            direction=0,
            magnitude=0.0,
            confidence=0.0,
            confidence_raw=0.0,
            horizon="0m",
            components=components,
            aggregator=self.name,
            metadata={"reason": reason},
        )

    def update(self, outcome: EpisodeOutcome) -> None:
        """Update per-analyst Beta posteriors from a cross-sectional outcome.

        Per ADR-0009 §P1-10: EpisodeOutcome contains AggregatedSignal +
        per-horizon realized returns + per-analyst direction_correct map.

        v0.1.1: per-analyst Beta update only. Cross-correlation not yet
        exploited (StackingAggregator in v0.1.3).
        """
        for view in outcome.aggregated_signal.components:
            stats = self._get_or_create_stats(view.analyst)
            correct = outcome.direction_correct.get(view.analyst)
            if correct is None:
                continue
            stats.n_observations += 1
            if correct:
                stats.alpha += 1.0
            else:
                stats.beta += 1.0

    def status(self) -> dict:
        return {
            "name": self.name,
            "n_aggregated": self._n_aggregated,
            "last_aggregated_at": (
                self._last_aggregated_at.isoformat() if self._last_aggregated_at else None
            ),
            "analyst_stats": {
                name: {
                    "alpha": s.alpha,
                    "beta": s.beta,
                    "n_observations": s.n_observations,
                    "posterior_accuracy": s.posterior_accuracy,
                }
                for name, s in self._stats.items()
            },
            "calibrator_status": self.calibrator.status(),
        }
