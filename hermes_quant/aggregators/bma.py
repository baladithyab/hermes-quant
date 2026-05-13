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
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AggregatedSignal,
    Aggregator,
    AnalystView,
    CalibratorNotReady,
    Direction,
    EpisodeOutcome,
    MarketContext,
)

logger = logging.getLogger(__name__)


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
    ):
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self.n_min_observations = n_min_observations
        self.agreement_bonus = agreement_bonus

        self._stats: dict[str, _AnalystStats] = {}
        self.calibrator = ColdStartCalibrator()

        self._n_aggregated = 0
        self._last_aggregated_at: pd.Timestamp | None = None

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
        """
        if not views:
            return self._flat_signal(context)

        weights = []
        signed_terms = []  # direction × magnitude × weight × confidence
        signed_dir_terms = []  # direction × weight × confidence (for direction)
        for v in views:
            w = self._weight_for(v.analyst)
            weights.append(w)
            signed_dir_terms.append(v.direction * w * v.confidence)

        weighted_dir_sum = sum(signed_dir_terms)
        if abs(weighted_dir_sum) < 1e-9:
            # Net flat — silence
            return self._flat_signal(context)

        composite_direction: Direction = 1 if weighted_dir_sum > 0 else -1

        # Magnitude: weighted mean of magnitudes from contributing-direction views
        contributing = [
            (v, w) for v, w in zip(views, weights, strict=False)
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

        # Agreement bonus: if all (non-flat) views agree on direction, bump
        non_flat = [v for v in views if v.direction != 0]
        if non_flat and all(v.direction == composite_direction for v in non_flat):
            confidence_raw = float(np.clip(vote_share + self.agreement_bonus, 0.0, 1.0))
        else:
            confidence_raw = vote_share

        # Calibrate
        try:
            confidence = self.calibrator.calibrate(confidence_raw)
        except CalibratorNotReady:
            confidence = max(0.0, confidence_raw - 0.20)

        # Horizon: use the modal horizon among contributing views; default to first
        horizons = [v.horizon for v, _ in contributing]
        horizon = max(set(horizons), key=horizons.count) if horizons else views[0].horizon

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
            },
        )
        self._n_aggregated += 1
        self._last_aggregated_at = context.asof
        return signal

    def _flat_signal(self, context: MarketContext) -> AggregatedSignal:
        """Construct a flat AggregatedSignal (silence)."""
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
            components=(),
            aggregator=self.name,
            metadata={"reason": "flat_or_no_views"},
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
                self._last_aggregated_at.isoformat()
                if self._last_aggregated_at
                else None
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
