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
- v0.1.1 BMA only updates per-analyst Beta posteriors.

Cross-correlation stacking (B50, flag-gated, DEFAULT-OFF):
- The Beta-binomial posterior treats every analyst as an INDEPENDENT source of
  evidence. When two analysts are perfectly correlated (they're right and wrong
  on the same episodes — e.g. two TA analysts reading the same momentum signal),
  the vote share double-counts them as if they were two independent confirmations.
  This inflates the aggregate confidence relative to the true information content.
- When the flag HERMES_QUANT_STACKING=1 is set (read at call time), the
  aggregator down-weights correlated analysts by an effective-sample-size
  redundancy factor derived from their pairwise correctness-correlation history
  (accumulated in update()). Two perfectly-correlated analysts then contribute
  strictly LESS than two independent ones.
- This is purely a FUSION change upstream of the deterministic risk gate and the
  sizing ladder: it only reshapes how peer views combine into the aggregate
  confidence. It never touches the gate, the sizing ladder, or require_ensemble.
- DEFAULT-OFF: when the flag is unset/!="1", the path is byte-identical to the
  pre-B50 BMA. The correctness history is still accumulated (purely additive,
  cheap) so toggling the flag on does not require a warm-up replay, but it is
  NEVER read while the flag is off.
"""

from __future__ import annotations

import logging
import os
import pickle
from collections import deque
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

# ---------------------------------------------------------------------------
# Cross-correlation stacking (B50) — flag-gated, DEFAULT-OFF
# ---------------------------------------------------------------------------

# Rolling window (number of co-observed episodes) over which pairwise
# correctness-correlation is measured. Bounded so the discount tracks the
# CURRENT correlation structure rather than ancient history, and so memory is
# O(n_analysts × window) regardless of runtime.
STACKING_CORR_WINDOW = 200

# Minimum number of CO-OBSERVED episodes (both analysts settled in the same
# episode) required before a pairwise correlation is trusted. Below this the
# pair is treated as independent (discount factor 1.0) — fail-open to the
# conservative no-discount default rather than down-weighting on a noisy
# 2-sample correlation.
STACKING_CORR_MIN_PAIRS = 10

# c96e: recency-decay sample ring. Bounded per-analyst log of
# (observable_asof_iso, correct) used by the recency-weighted refit when
# HERMES_QUANT_L2_POSTERIOR_DECAY=1. Bounded so memory is O(n_analysts × window)
# and so ancient samples (whose decay weight is ~0 anyway) are dropped.
DECAY_SAMPLE_WINDOW = 500

# Default recency half-life (days) for the decayed refit: a settled sample
# observable one half-life before the decision contributes half the weight of a
# brand-new one. Chosen to track skill over a quarter-ish horizon without
# discarding the recent past.
DEFAULT_DECAY_HALF_LIFE_DAYS = 30.0

# Approximate horizon → timedelta mapping, used ONLY to stamp the *observable*
# time of a settled sample (decision asof + horizon) so the recency refit's
# no-lookahead filter is honest. Calendar months/quarters use fixed day counts;
# exact calendar arithmetic is unnecessary for a recency weight.
_HORIZON_TO_TIMEDELTA: dict[str, pd.Timedelta] = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(weeks=1),
    "1M": pd.Timedelta(days=30),
    "1Q": pd.Timedelta(days=90),
}


def _horizon_delta(horizon: str) -> pd.Timedelta:
    """Map a horizon label to the delay until its outcome is observable.

    Unknown horizons fall back to 1 day — a positive delay, so an unrecognized
    horizon can never make a sample observable *before* its decision (which
    would be a lookahead).
    """
    return _HORIZON_TO_TIMEDELTA.get(horizon, pd.Timedelta(days=1))


def _decay_enabled() -> bool:
    """True iff HERMES_QUANT_L2_POSTERIOR_DECAY=1 (read at CALL TIME).

    c96e recency refit (DEFAULT-OFF, separate from persistence). When unset, the
    per-analyst weight is the plain Beta posterior accuracy (byte-identical to
    pre-c96e). When set, the weight is a recency-weighted, asof-honest refit over
    the per-analyst sample ring: older samples decay, and samples whose outcome
    was not yet observable at the decision asof are excluded.
    """
    return os.environ.get("HERMES_QUANT_L2_POSTERIOR_DECAY", "0") == "1"


def _per_analyst_calib_enabled() -> bool:
    """True iff HERMES_QUANT_L2_PER_ANALYST_CALIB=1 (read at CALL TIME).

    f254 (DEFAULT-OFF). When unset, each view's confidence flows through the
    single global aggregator calibrator exactly as before (byte-identical). When
    set, each view's confidence_raw is recalibrated through a calibrator KEYED BY
    the analyst's own learned Beta posterior (so a skilled analyst's confidence
    is not dragged toward the population average by the merged global fit), and
    the global calibrator is bypassed for that view's contribution. An analyst
    with no track record maps to the neutral prior mean — a safe, non-zero
    fallback.
    """
    return os.environ.get("HERMES_QUANT_L2_PER_ANALYST_CALIB", "0") == "1"


def _lesson_haircut_enabled() -> bool:
    """True iff HERMES_QUANT_L2_LESSON_HAIRCUT=1 (read at CALL TIME).

    57f6 (DEFAULT-OFF). When unset (or no provider is injected), the aggregate
    confidence is untouched — byte-identical to today, and the lesson provider is
    never even consulted. When set AND a provider is injected, a recent
    same-symbol same-direction LOSS lesson applies a bounded, asof-honest
    confidence haircut on the DEFAULT (non-LLM) decision path. This closes the
    reflection->decision loop deterministically.
    """
    return os.environ.get("HERMES_QUANT_L2_LESSON_HAIRCUT", "0") == "1"


# 57f6 haircut tuning. Each distinct matching loss shaves LESSON_HAIRCUT_PER
# off confidence multiplicatively; the compounded cut is clamped so confidence
# can never fall below LESSON_HAIRCUT_FLOOR of its pre-haircut value. The loop
# may DAMPEN conviction on a symbol/direction that recently lost, never silence
# it — silence remains the deterministic gate's job, not the aggregator's.
LESSON_HAIRCUT_PER = 0.15
LESSON_HAIRCUT_FLOOR = 0.5


def _stacking_enabled() -> bool:
    """True iff HERMES_QUANT_STACKING=1 (read at CALL TIME, not import time).

    DEFAULT-OFF: any value other than the exact string "1" leaves the
    aggregator byte-identical to the pre-B50 BMA path.
    """
    return os.environ.get("HERMES_QUANT_STACKING", "0") == "1"


def _posterior_persist_enabled() -> bool:
    """True iff HERMES_QUANT_L2_POSTERIOR_PERSIST=1 (read at CALL TIME).

    c96e (DEFAULT-OFF): when unset/!="1", the aggregator neither loads persisted
    per-analyst Beta posteriors at construction nor saves them in update(), so
    the path is byte-identical to the pre-c96e BMA. When set, learned per-analyst
    skill survives across recommend() lifecycles and process restarts instead of
    resetting to the prior on every fresh BMAAggregator().
    """
    return os.environ.get("HERMES_QUANT_L2_POSTERIOR_PERSIST", "0") == "1"


@dataclass
class _AnalystStats:
    """Per-analyst Beta-binomial posterior.

    ``history`` is a bounded ring of recent ``(episode_idx, correct)`` tuples
    (B50). The episode index lets the correlation-discount path align two
    analysts on CO-OBSERVED episodes only (analysts that settled at the same
    timestamp), so a pair seen on disjoint episodes is never spuriously
    correlated. It is ALWAYS accumulated (cheap, additive) but is ONLY consulted
    when HERMES_QUANT_STACKING=1, so the OFF path is unaffected by its presence.
    """

    name: str
    alpha: float
    beta: float
    n_observations: int = 0
    history: deque[tuple[int, bool]] = field(
        default_factory=lambda: deque(maxlen=STACKING_CORR_WINDOW)
    )
    # c96e: observability timestamp of the most recent settled sample folded
    # into this posterior. Stamped in update() from the episode asof. Persisted
    # alongside (alpha, beta) so a recency-decay refit can reason about
    # staleness without replaying the full sample history. None until the first
    # settlement.
    last_observable_asof: pd.Timestamp | None = None
    # c96e: bounded per-analyst sample ring of (observable_asof, correct) for the
    # recency-weighted refit. ALWAYS accumulated (cheap, additive) but consulted
    # ONLY when HERMES_QUANT_L2_POSTERIOR_DECAY=1, so the OFF path is unaffected
    # by its presence — exactly the B50 ``history`` discipline.
    decay_samples: deque[tuple[pd.Timestamp, bool]] = field(
        default_factory=lambda: deque(maxlen=DECAY_SAMPLE_WINDOW)
    )

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
        posterior_store_path: Path | None = None,
        posterior_recipe_key: str | None = None,
        loss_lesson_provider=None,
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

        # c96e: durable per-analyst Beta posteriors (DEFAULT-OFF). When the
        # HERMES_QUANT_L2_POSTERIOR_PERSIST flag is set, load any persisted
        # posteriors so learned skill survives across recommend() lifecycles
        # instead of resetting to the prior on every fresh aggregator. When the
        # flag is off, this is a no-op and self._stats stays empty (the
        # pre-c96e behavior). A missing/corrupt artifact degrades to cold-start.
        self.posterior_store_path = posterior_store_path
        self.posterior_recipe_key = posterior_recipe_key
        if _posterior_persist_enabled():
            self._load_persisted_posteriors()

        # 57f6: optional loss-lesson provider (DEFAULT None). When None, the
        # lesson-haircut path is a pure no-op regardless of the flag. When set
        # AND HERMES_QUANT_L2_LESSON_HAIRCUT=1, recent same-symbol same-direction
        # losses apply a bounded, asof-honest confidence haircut. Decoupled by
        # design: BMA only calls provider.recent_loss_lessons(ticker, asof); the
        # reflections/decisions JSONL join lives in the provider, not here.
        self.loss_lesson_provider = loss_lesson_provider

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

        # B50 cross-correlation stacking: monotone episode counter used to
        # align per-analyst correctness histories on co-observed episodes.
        # Incremented once per update() call (one EpisodeOutcome == one episode).
        self._episode_idx = 0

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

    def _load_persisted_posteriors(self) -> None:
        """c96e: hydrate self._stats from the persisted snapshot (fail-safe).

        Restores the Beta posterior fields (alpha, beta, n_observations,
        last_observable_asof) AND the c96e ``decay_samples`` recency ring — the
        latter is required so a reloaded aggregator with the decay flag on
        reproduces the same weight instead of refitting from an empty ring and
        collapsing a skilled analyst to the prior mean (see fix in
        _save_persisted_posteriors / commit history). The B50 co-observation
        ``history`` ring is the ONE thing deliberately NOT persisted — it is a
        bounded recent-correlation buffer consulted only when
        HERMES_QUANT_STACKING=1 and rebuilds itself from subsequent settlements.
        A missing/corrupt artifact yields no stats (cold-start), never an
        exception.
        """
        from hermes_quant.learning.posterior_store import load_posteriors

        try:
            persisted = load_posteriors(
                path=self.posterior_store_path,
                recipe_key=self.posterior_recipe_key,
            )
        except Exception as exc:  # noqa: BLE001 — bad cache must not crash init
            logger.warning("BMA: posterior load failed (%s); cold-start", exc)
            return
        for name, p in persisted.items():
            stats = _AnalystStats(
                name=name,
                alpha=p.alpha,
                beta=p.beta,
                n_observations=p.n_observations,
                last_observable_asof=p.last_observable_asof,
            )
            # Restore the recency-refit ring so a reloaded aggregator with the
            # decay flag on reproduces the same weight instead of collapsing to
            # the prior mean. The deque re-imposes its maxlen bound on load.
            stats.decay_samples.extend(p.decay_samples)
            self._stats[name] = stats

    def _save_persisted_posteriors(self, asof: pd.Timestamp) -> None:
        """c96e: atomically persist the current posteriors (fail-safe).

        Called from update() only when the persist flag is on. A write failure
        is logged but never propagates — a bad disk must not abort settlement.
        """
        from hermes_quant.learning.posterior_store import (
            PersistedPosterior,
            save_posteriors,
        )

        snapshot = {
            name: PersistedPosterior(
                alpha=s.alpha,
                beta=s.beta,
                n_observations=s.n_observations,
                last_observable_asof=s.last_observable_asof,
                decay_samples=tuple(s.decay_samples),
            )
            for name, s in self._stats.items()
        }
        try:
            save_posteriors(
                snapshot,
                path=self.posterior_store_path,
                asof=asof,
                recipe_key=self.posterior_recipe_key,
            )
        except Exception as exc:  # noqa: BLE001 — persistence must not abort settlement
            logger.warning("BMA: posterior save failed (%s); continuing", exc)

    def _weight_for(
        self, analyst_name: str, decision_asof: pd.Timestamp | None = None
    ) -> float:
        """Posterior accuracy if calibrated, else uniform proxy.

        c96e recency refit (DEFAULT-OFF): when HERMES_QUANT_L2_POSTERIOR_DECAY=1
        and a ``decision_asof`` is available, the posterior is recomputed from
        the per-analyst sample ring with (a) a HARD no-lookahead filter — only
        samples whose outcome was observable strictly before ``decision_asof``
        count — and (b) an exponential recency decay so stale skill fades toward
        the prior. When the flag is off the weight is the plain posterior
        accuracy, byte-identical to the pre-c96e path.
        """
        stats = self._get_or_create_stats(analyst_name)
        if _decay_enabled() and decision_asof is not None:
            return self._decayed_weight(stats, decision_asof)
        if stats.n_observations < self.n_min_observations:
            # Uniform proxy: use 0.5 (no information)
            return 0.5
        return stats.posterior_accuracy

    def _decayed_weight(
        self, stats: _AnalystStats, decision_asof: pd.Timestamp
    ) -> float:
        """Recency-weighted, asof-honest posterior accuracy from the sample ring.

        Delegates to the standalone, unit-tested refit so the no-lookahead and
        decay semantics live in exactly one place. The refit reduces to the
        prior mean when no admissible samples exist (cold-start safe).
        """
        from hermes_quant.learning.posterior_refit import SkillSample, refit_beta

        samples = [
            SkillSample(observable_asof=ts, correct=correct)
            for ts, correct in stats.decay_samples
        ]
        alpha, beta = refit_beta(
            samples=samples,
            decision_asof=decision_asof,
            prior_alpha=self.prior_alpha,
            prior_beta=self.prior_beta,
            half_life_days=DEFAULT_DECAY_HALF_LIFE_DAYS,
        )
        return alpha / (alpha + beta)

    def _per_analyst_calibrated_confidence(
        self, analyst_name: str, confidence_raw: float, decision_asof: pd.Timestamp | None
    ) -> float:
        """f254: calibrate a raw confidence through the analyst's OWN posterior.

        Uses the analyst's learned Beta(alpha, beta) directional-accuracy
        posterior as the shrinkage prior (the ADR-0009 cold-start formula keyed
        per analyst). When the recency-decay flag is also on and a decision asof
        is available, the (alpha, beta) used are the asof-honest, recency-refit
        posterior rather than the lifetime one — so calibration tracks current
        skill. Cold-start safe: an unseen analyst maps to the neutral prior mean.
        """
        from hermes_quant.learning.per_analyst_calibration import beta_shrinkage_calibrate
        from hermes_quant.learning.posterior_refit import SkillSample, refit_beta

        stats = self._get_or_create_stats(analyst_name)
        if _decay_enabled() and decision_asof is not None:
            samples = [
                SkillSample(observable_asof=ts, correct=correct)
                for ts, correct in stats.decay_samples
            ]
            alpha, beta = refit_beta(
                samples=samples,
                decision_asof=decision_asof,
                prior_alpha=self.prior_alpha,
                prior_beta=self.prior_beta,
                half_life_days=DEFAULT_DECAY_HALF_LIFE_DAYS,
            )
        else:
            alpha, beta = stats.alpha, stats.beta
        return beta_shrinkage_calibrate(confidence_raw, alpha=alpha, beta=beta)

    def _lesson_haircut(
        self,
        confidence: float,
        ticker: str,
        direction: int,
        decision_asof: pd.Timestamp,
    ) -> tuple[float, bool, int]:
        """57f6: apply a bounded, asof-honest loss-lesson haircut (fail-safe).

        Returns ``(confidence, applied, n_lessons)``. A no-op (applied=False)
        when the flag is off, no provider is injected, the provider raises, or no
        lesson matches the (ticker, direction, asof). A provider error is
        swallowed and logged — a bad reflections store must never break the
        decision path (silence-by-default).
        """
        provider = self.loss_lesson_provider
        if provider is None or not _lesson_haircut_enabled():
            return confidence, False, 0

        from hermes_quant.learning.lesson_haircut import apply_lesson_haircut

        try:
            lessons = provider.recent_loss_lessons(ticker, decision_asof)
        except Exception as exc:  # noqa: BLE001 — bad lesson store must not break decisions
            logger.warning("BMA: loss_lesson_provider raised (%s); skipping haircut", exc)
            return confidence, False, 0

        haircut = apply_lesson_haircut(
            confidence=confidence,
            ticker=ticker,
            direction=direction,
            decision_asof=decision_asof,
            lessons=lessons or [],
            per_lesson_haircut=LESSON_HAIRCUT_PER,
            floor_fraction=LESSON_HAIRCUT_FLOOR,
        )
        applied = haircut < confidence
        # Count only the matching lessons that actually drove the haircut so the
        # audit field is honest (re-uses the same asof-honest matcher semantics).
        n_lessons = self._count_matching_lessons(
            lessons or [], ticker, direction, decision_asof
        )
        return haircut, applied, n_lessons

    @staticmethod
    def _count_matching_lessons(lessons, ticker, direction, decision_asof) -> int:
        """Distinct same-ticker/-direction lessons observable before the decision.

        Mirrors apply_lesson_haircut's matcher EXACTLY (same NaT/>= semantics) so
        the audit count can never disagree with whether a haircut was applied.
        """
        decision = pd.Timestamp(decision_asof)
        if pd.isna(decision):
            return 0
        if decision.tz is None:
            decision = decision.tz_localize("UTC")
        ticker_u = ticker.upper()
        seen: set[str] = set()
        for lesson in lessons:
            if lesson.lesson_id in seen:
                continue
            if lesson.ticker.upper() != ticker_u or lesson.direction != direction:
                continue
            tau = pd.Timestamp(lesson.tau_observable)
            if pd.isna(tau):
                continue
            if tau.tz is None:
                tau = tau.tz_localize("UTC")
            if tau >= decision:
                continue
            seen.add(lesson.lesson_id)
        return len(seen)

    def _horizon_weight(self, horizon: str) -> float:
        """Per-ADR-0036 horizon weight multiplier.

        Unknown horizons default to 1.0 (no suppression) so adding a new
        horizon to AnalystView.horizon doesn't silently downweight views.
        """
        return float(self.horizon_weights.get(horizon, 1.0))

    @staticmethod
    def _pairwise_correctness_corr(
        hist_a: list[tuple[int, bool]],
        hist_b: list[tuple[int, bool]],
    ) -> float | None:
        """Pearson correlation of two analysts' CO-OBSERVED correctness series.

        Aligns on shared episode indices only. Returns None when there are
        fewer than ``STACKING_CORR_MIN_PAIRS`` co-observations, or when either
        analyst's correctness is constant over the shared window (zero
        variance → correlation undefined; treat as "no evidence of
        redundancy"). The returned value is clamped to [-1, 1] for numerical
        safety.
        """
        map_a = dict(hist_a)
        map_b = dict(hist_b)
        shared = map_a.keys() & map_b.keys()
        if len(shared) < STACKING_CORR_MIN_PAIRS:
            return None
        xs = np.fromiter((1.0 if map_a[i] else 0.0 for i in shared), dtype=float)
        ys = np.fromiter((1.0 if map_b[i] else 0.0 for i in shared), dtype=float)
        # Constant series → undefined correlation → no redundancy evidence.
        if xs.std() < 1e-12 or ys.std() < 1e-12:
            return None
        rho = float(np.corrcoef(xs, ys)[0, 1])
        if not np.isfinite(rho):
            return None
        return float(np.clip(rho, -1.0, 1.0))

    def _redundancy_factors(self, analyst_names: list[str]) -> dict[str, float]:
        """Per-analyst redundancy discount in (0, 1] from correctness-correlation.

        B50 cross-correlation stacking. Correlated analysts are not independent
        evidence: if analyst i is right/wrong on the SAME episodes as its peers,
        it adds little information beyond them. We discount each analyst by

            f_i = 1 / (1 + Σ_{j != i} max(0, ρ_ij))

        where ρ_ij is the pairwise correctness correlation (None / negative
        treated as 0 — anti-correlated or independent analysts are genuinely
        additional evidence and are NOT discounted). Properties:

          * Two PERFECTLY-correlated analysts (ρ=1): each f = 1/2, so their
            COMBINED contribution is 1.0 — exactly one independent analyst.
          * Two INDEPENDENT analysts (ρ=0 or insufficient data): each f = 1,
            combined contribution 2.0 — strictly MORE than the correlated pair.

        This is the effective-sample-size correction that prevents redundant
        peers from double-counting in the fused confidence. Caller multiplies
        these factors into per-analyst weights upstream of the gate.
        """
        factors: dict[str, float] = {name: 1.0 for name in analyst_names}
        if len(analyst_names) < 2:
            return factors
        hists = {
            name: list(self._get_or_create_stats(name).history) for name in analyst_names
        }
        for name in analyst_names:
            redundancy = 0.0
            for other in analyst_names:
                if other == name:
                    continue
                rho = self._pairwise_correctness_corr(hists[name], hists[other])
                if rho is not None and rho > 0.0:
                    redundancy += rho
            factors[name] = 1.0 / (1.0 + redundancy)
        return factors

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
                    base_weights_map = {
                        v.analyst: self._weight_for(v.analyst, context.asof) for v in views
                    }
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

        # B50 cross-correlation stacking (DEFAULT-OFF, flag read at CALL TIME).
        # When the flag is unset this stays None and the weights loop below is
        # byte-identical to the pre-B50 BMA. When set, correlated analysts are
        # discounted by an effective-sample-size redundancy factor so two
        # perfectly-correlated peers contribute strictly less than two
        # independent ones. This reshapes ONLY the fused confidence upstream of
        # the deterministic gate — it never touches the gate or sizing ladder.
        redundancy_factors: dict[str, float] | None = None
        stacking_used = False
        if _stacking_enabled():
            redundancy_factors = self._redundancy_factors(
                [v.analyst for v in views]
            )
            # Record whether the discount actually bit (any factor < 1) so the
            # OFF/uninformative case is auditable as a no-op in metadata.
            stacking_used = any(f < 1.0 - 1e-12 for f in redundancy_factors.values())

        # f254 (DEFAULT-OFF): per-analyst calibration map. When enabled, each
        # view's confidence_raw is recalibrated through the analyst's OWN learned
        # Beta posterior (keyed by name) so a skilled analyst is not dragged
        # toward the population average by the merged global calibrator fit. The
        # map is the calibrated confidence to USE in the vote in place of
        # v.confidence; when the flag is off it stays None and the vote uses
        # v.confidence exactly as before (byte-identical).
        per_analyst_cal: dict[str, float] | None = None
        if _per_analyst_calib_enabled():
            per_analyst_cal = {
                v.analyst: self._per_analyst_calibrated_confidence(
                    v.analyst, v.confidence_raw, context.asof
                )
                for v in views
            }

        def _vote_confidence(v: AnalystView) -> float:
            if per_analyst_cal is not None:
                return per_analyst_cal[v.analyst]
            return v.confidence

        weights = []
        signed_terms = []  # direction × magnitude × weight × confidence
        signed_dir_terms = []  # direction × weight × confidence (for direction)
        for v in views:
            # Base weight: regime-adjusted if available, else posterior/uniform.
            if _regime_adjusted and v.analyst in _regime_adjusted:
                base_w = _regime_adjusted[v.analyst]
            else:
                base_w = self._weight_for(v.analyst, context.asof)
            # ADR-0036: cross-horizon BMA weighting —
            #   effective weight = base_weight(analyst) × horizon_weight(horizon)
            #   The view's own confidence is multiplied in by signed_dir_terms
            #   below (so the full ADR formula is base × horizon × confidence).
            h_w = self._horizon_weight(v.horizon)
            w = base_w * h_w
            # B50: apply the correlation redundancy discount when stacking is on.
            if redundancy_factors is not None:
                w *= redundancy_factors.get(v.analyst, 1.0)
            weights.append(w)
            signed_dir_terms.append(v.direction * w * _vote_confidence(v))

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
            # f254: for a lone voice, the honest confidence IS the per-analyst
            # calibrated value (already shrunk by that analyst's track record);
            # otherwise fall back to the analyst's own raw confidence as before.
            if per_analyst_cal is not None:
                confidence_raw = float(np.clip(per_analyst_cal[sole_v.analyst], 0.0, 1.0))
            else:
                confidence_raw = float(np.clip(sole_v.confidence_raw, 0.0, 1.0))
        elif non_flat and all(v.direction == composite_direction for v in non_flat):
            # Multi-contributor unanimous: vote_share + agreement bonus.
            if per_analyst_cal is not None:
                # f254: in per-analyst-calibrated mode, vote_share measures
                # agreement, not probability. Aggregate the calibrated
                # contributor probabilities, then apply the same agreement
                # bonus to that probability estimate.
                calibrated_vote = sum(_vote_confidence(v) * w for v, w in contributing) / total_w
                confidence_raw = float(np.clip(calibrated_vote + self.agreement_bonus, 0.0, 1.0))
            else:
                confidence_raw = float(np.clip(vote_share + self.agreement_bonus, 0.0, 1.0))
        else:
            # Multi-contributor with dissent: vote_share only.
            confidence_raw = vote_share

        # Calibrate. CalibratorNotReady fallback uses the same Beta(2,5)
        # prior as ColdStartCalibrator (ADR-0009 §P0-2 amendment 2026-05-26):
        # see hermes_quant/calibrators.py and
        # docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md.
        #
        # f254: when per-analyst calibration is on, confidence_raw is already a
        # calibrated quantity (each contribution was shrunk by its analyst's own
        # posterior). Re-running the merged global calibrator would double-shrink
        # and re-introduce the population-average drag we are trying to remove,
        # so the global calibrate step is BYPASSED in that mode.
        if per_analyst_cal is not None:
            confidence = float(np.clip(confidence_raw, 0.0, 1.0))
        else:
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
                    per_horizon_dir.get(v.horizon, 0.0)
                    + v.direction * w * _vote_confidence(v)
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

        # 57f6: lesson-driven confidence haircut (DEFAULT-OFF). Applied LAST, as
        # the final word on confidence: a recent same-symbol same-direction loss
        # dampens conviction on the deterministic path. asof-honest and bounded;
        # a no-op (and byte-identical metadata) when the flag is off, no provider
        # is injected, or no lesson matches.
        lesson_haircut_applied = False
        lesson_haircut_n = 0
        if self.loss_lesson_provider is not None and _lesson_haircut_enabled():
            confidence, lesson_haircut_applied, lesson_haircut_n = self._lesson_haircut(
                confidence, context.asset, composite_direction, context.asof
            )

        metadata = {
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
        }
        # B50 stacking audit fields — injected ONLY when the flag is on, so the
        # OFF-path metadata dict is byte-identical to the pre-B50 BMA. The
        # discounted weights are already reflected in metadata["weights"].
        if redundancy_factors is not None:
            metadata["stacking_redundancy_factors"] = redundancy_factors
            metadata["stacking_used"] = stacking_used

        # f254 audit field — injected ONLY when per-analyst calibration is on, so
        # the OFF-path metadata dict is byte-identical. Records the per-analyst
        # calibrated confidence that fed the vote (vs the merged global path).
        if per_analyst_cal is not None:
            metadata["per_analyst_calibrated_confidence"] = per_analyst_cal

        # 57f6 audit fields — injected ONLY when the haircut path ran (flag on +
        # provider present), so the OFF-path metadata dict is byte-identical.
        if self.loss_lesson_provider is not None and _lesson_haircut_enabled():
            metadata["lesson_haircut_applied"] = lesson_haircut_applied
            metadata["lesson_haircut_n_lessons"] = lesson_haircut_n

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
            metadata=metadata,
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

        Beta update: per-analyst directional accuracy (unchanged).

        B50 cross-correlation stacking: in ADDITION to the Beta update, each
        analyst's per-episode correctness is appended to a bounded ring keyed
        by a shared episode index. This history feeds the correlation-discount
        path in aggregate() when HERMES_QUANT_STACKING=1. The accumulation is
        purely additive — it mutates no Beta posterior and changes no return
        value, so update() is byte-identical to pre-B50 with the flag off. We
        accumulate UNCONDITIONALLY (not flag-gated) so flipping the flag on does
        not require a settlement replay to warm up the correlation window.
        """
        episode_idx = self._episode_idx
        self._episode_idx += 1
        decision_asof = pd.Timestamp(outcome.asof)
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
            # B50: append (episode_idx, correct) for co-observation alignment.
            stats.history.append((episode_idx, bool(correct)))
            # c96e: stamp WHEN this outcome became observable = decision asof +
            # the view's horizon. NOT the decision asof itself — a 1w view
            # decided at D is only knowable at D+1w, and the recency refit's
            # no-lookahead filter relies on this being honest. ALWAYS recorded
            # (additive); consulted only when the decay flag is on.
            #
            # Source-side no-lookahead defense: if the decision asof is NaT (a
            # malformed settlement record), the observability stamp would be NaT
            # too, which the downstream guards must treat as "unknown" anyway.
            # Skip recording the poisoned sample entirely rather than relying
            # solely on the read-side guards — the alpha/beta credit above still
            # applies (it is asof-independent), only the decay ring is spared.
            if not pd.isna(decision_asof):
                observable_asof = decision_asof + _horizon_delta(view.horizon)
                stats.decay_samples.append((observable_asof, bool(correct)))
                stats.last_observable_asof = observable_asof

        # c96e: persist the evolved posteriors (DEFAULT-OFF). Stamp the snapshot
        # with the settlement asof. Save is fail-safe (logged, never raises).
        if _posterior_persist_enabled():
            self._save_persisted_posteriors(decision_asof)

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
