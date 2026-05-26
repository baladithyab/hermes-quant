"""hermes_quant.analysts.kronos — Kronos foundation-model analyst.

Per ADR-0018 + research/05-kronos-integration.md:
- Kronos is ONE voice in the BMA committee, not the oracle.
- Lazy-loaded (avoid blocking gateway startup).
- Distributional output via subclass that exposes pre-mean sample paths.
- Path-agreement confidence with [0.30, 0.85] hard clip (foundation-model
  overconfidence guard; mitigates the Kairos A-shares neg-IC failure mode).
- Zero-confidence abstain on weight-load failure (BMA's `aggregate()` filters
  views with confidence < 0.10, so abstainers don't pollute downstream).
- MIT license; weights on HF Hub at NeoQuasar/Kronos-{mini,small,base}.

Foundation model is Kronos (Shi et al., AAAI 2026 — arxiv 2508.02739).

Charter clauses honored:
- "Kronos is one analyst, not the whole system" (ADR-0018 §D1)
- "AAAI 2026 acceptance ≠ alpha" (ADR-0018 §D3 confidence clipping)
- "PPO or recurrent SAC for the aggregator, with the analyst pool frozen"
  → KronosAnalyst NEVER trains, only infers (ADR-0018 §D8)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AnalystView,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (per ADR-0018 §D6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KronosConfig:
    model: str = "base"  # base | small | mini
    """Kronos model variant. `base` = 102M params, recommended default.
    `small` = 24.7M (lighter), `mini` = 4.1M (CPU-comfortable). `large`
    is closed-source per upstream and won't load."""

    device: str = "cpu"  # cpu | cuda | mps
    """Inference device. CPU latency is 3-10s/call at base; GPU is 150-400ms."""

    max_context: int = 512
    """Maximum input bar count. Kronos pads/truncates internally."""

    pred_len: int = 12
    """Forecast horizon in bars. For 1h data → 12 hours ahead."""

    sample_count: int = 30
    """Number of stochastic forecast paths to draw. Higher = more reliable
    path-agreement confidence; cost is linear in sample_count. 30 is the
    Kronos paper's default and gives ~3% MC noise on the agreement metric."""

    raw_confidence_clip_low: float = 0.30
    """Floor on raw confidence (path-agreement fraction). Prevents
    high-disagreement paths from pushing confidence to ~0 — even maximum
    disagreement under sampling noise rarely justifies < 0.30."""

    raw_confidence_clip_high: float = 0.85
    """Ceiling on raw confidence. Foundation models are systematically
    overconfident on tokenized OHLCV (per the Kairos A-shares failure
    mode in the charter §"Kronos lessons"). Clipping at 0.85 ensures
    even unanimous path agreement leaves room for BMA calibrator
    shrinkage to do meaningful work."""

    horizon_label: str = "1d"
    """The horizon string written to AnalystView (consumed by aggregator)."""

    deterministic_seed: int | None = 42
    """RNG seed for path sampling. The charter requires every signal
    to be replayable from disk (AGENTS.md "Reproducibility"). Without
    seeding, Kronos's stochastic sampling makes signals non-replayable.
    Set to None for production runs where stochastic exploration is
    desired and replayability is sacrificed; otherwise leave at 42 (or
    set per-tick to the bar timestamp for deterministic-yet-evolving
    behavior)."""


# ---------------------------------------------------------------------------
# KronosAnalyst — main class
# ---------------------------------------------------------------------------


class KronosAnalyst:
    """Kronos foundation-model OHLCV forecaster as an Analyst.

    See ADR-0018 + module docstring. The class is constructed cheaply
    (no HF download); weights load lazily on first `analyze()` call.
    Failed loads degrade gracefully to zero-confidence abstain.
    """

    name = "kronos"

    def __init__(
        self,
        config: KronosConfig | None = None,
        *,
        calibrator: ColdStartCalibrator | None = None,
        # Test seam: inject a fake distributional predictor
        _predictor_factory=None,
    ):
        self.config = config or KronosConfig()
        self.calibrator = calibrator or ColdStartCalibrator()
        self._predictor = None
        self._abstain_reason: str | None = None
        self._predictor_factory = _predictor_factory

    # ------------------------------------------------------------------
    # Analyst Protocol
    # ------------------------------------------------------------------

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """Per ADR-0002 + ADR-0018 §D4. Returns None per Protocol when
        the analyst genuinely cannot produce a view (extremely insufficient
        data); otherwise returns a view (possibly zero-confidence abstain
        per ADR-0018 §D4 lazy-load failure).
        """
        bars = ctx.bars
        if bars is None or len(bars) < 32:
            # Genuinely insufficient — Protocol-clean None
            return None

        # Lazy-load on first call
        if self._predictor is None and self._abstain_reason is None:
            self._lazy_load()

        if self._abstain_reason is not None:
            return self._abstain(reason=self._abstain_reason)

        # Run distributional inference
        try:
            paths = self._predict_paths(bars)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "kronos: inference failed: %s; abstaining for this call",
                exc,
                exc_info=True,
            )
            return self._abstain(reason=f"inference_error: {exc}")

        direction, magnitude, raw_confidence = self._direction_from_paths(
            paths,
            last_close=float(bars["close"].iloc[-1]),
        )

        # ColdStart shrinkage (-0.20 until calibrator has 200+ samples)
        confidence = self.calibrator.calibrate(raw_confidence)

        return AnalystView(
            analyst=self.name,
            direction=direction,
            magnitude=magnitude,
            confidence=float(confidence),
            confidence_raw=float(raw_confidence),
            horizon=self.config.horizon_label,
            rationale=(
                f"Kronos-{self.config.model}: median return {magnitude:.4f} "
                f"({direction:+d}); {int(raw_confidence * self.config.sample_count)}/"
                f"{self.config.sample_count} paths agreed"
            ),
            metadata={
                "model": self.config.model,
                "device": self.config.device,
                "n_paths": self.config.sample_count,
                "pred_len": self.config.pred_len,
                "raw_confidence_clipped": (
                    raw_confidence == self.config.raw_confidence_clip_high
                    or raw_confidence == self.config.raw_confidence_clip_low
                ),
            },
        )

    def update_calibrator(self, outcome: RealizedOutcome) -> None:
        """Per ADR-0010 calibrator-from-fills hook. Forwarded to the
        calibrator's `fit()` once enough samples accumulate (the BMA
        aggregator manages this — analysts just expose the seam)."""
        # ColdStartCalibrator.fit takes batches; analysts collect outcomes
        # and a wave-D cron job batches them into fit() calls.
        # For now this is a no-op stub honoring the hook contract.
        pass

    def health(self) -> dict:
        return {
            "name": self.name,
            "model": self.config.model,
            "device": self.config.device,
            "loaded": self._predictor is not None,
            "abstain_reason": self._abstain_reason,
            "calibrator": self.calibrator.status(),
        }

    # ------------------------------------------------------------------
    # Lazy load (the failure-handling path is the important one)
    # ------------------------------------------------------------------

    def _lazy_load(self) -> None:
        """Load Kronos weights from HuggingFace on first analyze call.

        Failures (no internet, missing torch, missing kronos package,
        weight-shape mismatch) all funnel to self._abstain_reason; the
        analyst stays "registered" but emits zero-confidence views.
        """
        if self._predictor_factory is not None:
            try:
                self._predictor = self._predictor_factory()
            except Exception as exc:  # noqa: BLE001
                self._abstain_reason = f"factory_failed: {exc}"
                logger.warning("kronos: test factory failed: %s", exc)
            return

        try:
            # The Kronos package; if not installed, fail fast.
            from kronos import (  # type: ignore
                Kronos,
                KronosPredictor,
                KronosTokenizer,
            )
        except ImportError as exc:
            self._abstain_reason = (
                f"kronos package not installed: {exc}. "
                "Install with: pip install 'hermes-quant[kronos]'"
            )
            logger.info(
                "KronosAnalyst: kronos package missing; abstaining at all calls. "
                "Install: pip install 'hermes-quant[kronos]'"
            )
            return

        try:
            model_id = f"NeoQuasar/Kronos-{self.config.model}"
            tokenizer_id = f"NeoQuasar/Kronos-Tokenizer-{self.config.model}"
            tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
            model = Kronos.from_pretrained(model_id)
            base_predictor = KronosPredictor(
                model=model,
                tokenizer=tokenizer,
                device=self.config.device,
                max_context=self.config.max_context,
            )
            self._predictor = _DistributionalKronosPredictor(base_predictor)
            logger.info(
                "kronos: loaded %s on %s; pred_len=%d sample_count=%d",
                model_id,
                self.config.device,
                self.config.pred_len,
                self.config.sample_count,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._abstain_reason = f"weight_load_failed: {exc}"
            logger.warning(
                "kronos: weight load from HF failed: %s; abstaining",
                exc,
            )

    def _abstain(self, *, reason: str) -> AnalystView:
        """Return a zero-confidence abstain view. BMA's aggregate() filters
        views with confidence < 0.10, so this is structurally pruned upstream
        of the silence-bias gate — the analyst stays "registered" without
        polluting downstream decisions."""
        return AnalystView(
            analyst=self.name,
            direction=0,
            magnitude=0.0,
            confidence=0.0,
            confidence_raw=0.0,
            horizon=self.config.horizon_label,
            rationale=f"abstain: {reason}",
            metadata={"abstain": True, "reason": reason},
        )

    # ------------------------------------------------------------------
    # Distributional inference + direction extraction
    # ------------------------------------------------------------------

    def _predict_paths(self, bars: pd.DataFrame) -> np.ndarray:
        """Run Kronos with sample_count paths, return shape
        [sample_count, pred_len, n_features].

        Per ADR-0018 §D2 we use a subclass to expose pre-mean paths.
        Per Phase-7 follow-up: seed torch + numpy with config's
        deterministic_seed so signals are replayable from disk
        (charter "Reproducibility" invariant).
        """
        # Seed for replayability (charter invariant)
        if self.config.deterministic_seed is not None:
            np.random.seed(self.config.deterministic_seed)
            try:
                import torch  # type: ignore

                torch.manual_seed(self.config.deterministic_seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(self.config.deterministic_seed)
            except ImportError:
                pass

        # Truncate to max_context most recent bars
        recent = bars.tail(self.config.max_context).reset_index(drop=True)
        return self._predictor.predict_distributional(
            df=recent,
            pred_len=self.config.pred_len,
            sample_count=self.config.sample_count,
        )

    def _direction_from_paths(
        self,
        paths: np.ndarray,
        *,
        last_close: float,
    ) -> tuple[Direction, float, float]:
        """Convert sample paths to (direction, magnitude, raw_confidence).

        - direction: sign of median pct_return across paths
        - magnitude: |median_pct_return|
        - raw_confidence: path-agreement fraction, clipped to [low, high]

        See ADR-0018 §D5.
        """
        # paths shape: [sample_count, pred_len, n_features]
        # Use close = column 0 (Kronos OHLCV ordering matches ours)
        end_close_paths = paths[:, -1, 0]
        pct_returns = (end_close_paths - last_close) / last_close

        median_return = float(np.median(pct_returns))

        # Direction is sign of median (+1, 0, -1)
        if median_return > 0:
            direction: Direction = 1
        elif median_return < 0:
            direction = -1
        else:
            direction = 0

        magnitude = abs(median_return)

        # Path-agreement = fraction of paths with same sign as median
        # (when median is 0, agreement is undefined; treat as 0.5 floor)
        if direction == 0:
            sign_agreement = 0.5
        else:
            sign_agreement = float(np.mean(np.sign(pct_returns) == np.sign(median_return)))

        # Clip to [low, high] (foundation-model overconfidence guard)
        raw_confidence = float(
            np.clip(
                sign_agreement,
                self.config.raw_confidence_clip_low,
                self.config.raw_confidence_clip_high,
            )
        )

        return direction, magnitude, raw_confidence


# ---------------------------------------------------------------------------
# _DistributionalKronosPredictor — subclass to expose sample paths
# ---------------------------------------------------------------------------


class _DistributionalKronosPredictor:
    """Wrap upstream KronosPredictor to expose pre-mean paths.

    KronosPredictor.predict() averages all sample_count paths internally
    via np.mean(preds, axis=1). This wrapper either:
    1. Intercepts the call to extract paths before averaging (preferred), or
    2. Falls back to running predict() sample_count times to build paths
       (slower but deterministic against upstream API drift).

    For v0.3 we ship the fallback approach (#2): it's slower (sample_count
    extra calls) but immune to upstream Kronos API changes. The optimized
    path is a v0.4 perf improvement.
    """

    def __init__(self, base_predictor):
        self._base = base_predictor

    def predict_distributional(
        self,
        df: pd.DataFrame,
        pred_len: int,
        sample_count: int,
    ) -> np.ndarray:
        """Return [sample_count, pred_len, n_features] array."""
        # Per ADR-0018 §D2 fallback: run predict sample_count times.
        # Each call uses Kronos's internal stochasticity; the average is
        # what predict() returns by default. We collect the per-call output
        # which IS the per-sample mean. To get distinct paths we need to
        # call with sample_count=1 each time so internal mean is identity.
        paths = []
        for _ in range(sample_count):
            out_df = self._base.predict(
                df=df,
                pred_len=pred_len,
                sample_count=1,
            )
            # out_df has columns ['open', 'high', 'low', 'close', 'volume']
            paths.append(out_df[["close", "open", "high", "low", "volume"]].values)
        return np.array(paths)
