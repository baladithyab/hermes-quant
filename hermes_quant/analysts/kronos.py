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

GPU migration (2026-05-26):
- Single-call batched `predict_distributional` (sample_count=30 in one
  forward, ~30× faster than the 30-iter loop on any device).
- Multi-symbol `analyze_batch` for universe scans (~300× wall-clock vs
  per-symbol CPU loop on RTX 5090). See `kronos-gpu-migration-plan.md`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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

    device: str = "auto"  # auto | cpu | cuda | cuda:0 | mps
    """Inference device. CPU latency is 3-10s/call at base; GPU is 150-400ms.
    `auto` picks cuda:0 → mps → cpu in that order (see `_resolve_device`)."""

    dtype: str = "auto"  # auto | fp32 | fp16 | bf16
    """Inference precision. `auto` picks bf16 on CUDA Blackwell+ (sign-of-
    return lives in the exponent, so bf16 is materially safer than fp16 for
    this workload), fp32 elsewhere. fp32 is the audit/replay default."""

    max_context: int = 512
    """Maximum input bar count. Kronos pads/truncates internally."""

    pred_len: int = 12
    """Forecast horizon in bars. For 1h data → 12 hours ahead."""

    sample_count: int = 30
    """Number of stochastic forecast paths to draw. Higher = more reliable
    path-agreement confidence; cost is linear in sample_count. 30 is the
    Kronos paper's default and gives ~3% MC noise on the agreement metric."""

    batch_size: int = 32
    """Symbols per GPU batch in `analyze_batch`. With sample_count=30 each
    chunk goes to ~960 along the autoregressive batch dim — well within
    RTX 5090 32 GB envelope. Drop to 8-16 on smaller cards."""

    weights_dir: str | None = None
    """Local directory containing `Kronos-{model}/` and `Kronos-Tokenizer-
    {model}/` mirrors (e.g. ~/.models-kronos). When set, `from_pretrained`
    reads those local paths instead of HF Hub. Defaults to None (HF cache
    in $HOME is durable across /tmp wipes anyway)."""

    compile: bool = False
    """torch.compile the model. Per migration plan §7.1 this is HIGH risk
    on Blackwell + autoregressive control flow + rolling buffers; ship off
    by default and only flip on after a dedicated benchmarking session."""

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

    max_magnitude_per_horizon: dict[str, float] = field(
        default_factory=lambda: {"1d": 0.10, "5d": 0.20, "20d": 0.40}
    )
    """Per-horizon hard cap on |magnitude| (signed return). Foundation-model
    overconfidence guard for volatile names — Kronos can occasionally emit
    physically implausible magnitudes (e.g. AMD daily forecast 36.6%, vs.
    realized mega-cap equity 1d std ~2-3%). The cap is applied AFTER the
    distributional median is taken; direction and confidence_raw (path
    agreement) are preserved. Defaults are loose enough to admit real
    macro-shock days but reject foundation-model hallucinations:
      - 1d: 10%   (>3σ for liquid mega-caps)
      - 5d: 20%   (~weekly tail)
      - 20d: 40%  (~monthly tail)
    Lookup is by `horizon_label`; missing keys disable clipping for that
    horizon. Codex MED 2026-05-26."""

    deterministic_seed: int | None = 42
    """RNG seed for path sampling. The charter requires every signal
    to be replayable from disk (AGENTS.md "Reproducibility"). Without
    seeding, Kronos's stochastic sampling makes signals non-replayable.
    Set to None for production runs where stochastic exploration is
    desired and replayability is sacrificed; otherwise leave at 42 (or
    set per-tick to the bar timestamp for deterministic-yet-evolving
    behavior)."""


def _resolve_device(cfg: KronosConfig) -> str:
    """Resolve `device='auto'` to a concrete device string. Idempotent for
    explicit values."""
    if cfg.device != "auto":
        return cfg.device
    try:
        import torch  # type: ignore
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda:0"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_dtype(cfg: KronosConfig, device: str):
    """Resolve `dtype='auto'` to a torch dtype. Returns torch.dtype or None
    (None = leave model at upstream default = fp32)."""
    try:
        import torch  # type: ignore
    except ImportError:
        return None
    name = cfg.dtype
    if name == "auto":
        # bf16 only on CUDA where it's safe (Blackwell, A100, H100). MPS &
        # CPU stay fp32 — bf16 on CPU is supported but slower than fp32.
        if device.startswith("cuda") and torch.cuda.is_bf16_supported():
            return torch.bfloat16
        return torch.float32
    table = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}
    return table.get(name, torch.float32)


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
        # Resolved device for telemetry; populated by _lazy_load.
        self._resolved_device: str | None = None

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

        return self._build_view_from_paths(ctx, paths)

    def analyze_batch(
        self, ctxs: list[MarketContext]
    ) -> list[AnalystView | None]:
        """GPU-batched analysis across N symbols.

        Symbols with insufficient bars (<32) get None at their slot.
        Symbols whose history truncated to a different length than the
        batch's modal length fall back to per-symbol `analyze()` so the
        upstream `predict_batch` uniform-seq-len constraint is honored
        (plan §7.6).

        The aggregator already handles None and abstain views, so
        per-symbol failures inside this method must NOT crash the batch
        — they return None at the offending slot.
        """
        results: list[AnalystView | None] = [None] * len(ctxs)

        if not ctxs:
            return results

        # Lazy-load once for the batch
        if self._predictor is None and self._abstain_reason is None:
            self._lazy_load()
        if self._abstain_reason is not None:
            return [self._abstain(reason=self._abstain_reason) for _ in ctxs]

        # Step 1: filter to symbols with sufficient bars
        eligible: list[int] = []
        for i, ctx in enumerate(ctxs):
            if ctx.bars is not None and len(ctx.bars) >= 32:
                eligible.append(i)
            # else: results[i] stays None (genuinely insufficient — Protocol)

        if not eligible:
            return results

        # Step 2: group eligible symbols by their truncated context length
        # so each batch chunk has uniform seq_len (upstream invariant).
        by_seq_len: dict[int, list[int]] = {}
        for i in eligible:
            seq_len = min(len(ctxs[i].bars), self.config.max_context)
            by_seq_len.setdefault(seq_len, []).append(i)

        # Seed once for the whole batch (charter replayability invariant)
        self._seed()

        # Step 3: process each (seq_len-group × batch_size chunk) on GPU
        for seq_len, idx_list in by_seq_len.items():
            for chunk_start in range(0, len(idx_list), self.config.batch_size):
                chunk = idx_list[chunk_start : chunk_start + self.config.batch_size]
                ctxs_chunk = [ctxs[i] for i in chunk]
                try:
                    paths_per_symbol = self._predict_paths_batch(ctxs_chunk)
                except Exception as exc:  # noqa: BLE001
                    # One-bad-batch fallback: try per-symbol analyze for this
                    # chunk so 31 good symbols don't lose their view because
                    # of one malformed bar.
                    logger.warning(
                        "kronos: batch inference failed (chunk size %d): %s; "
                        "falling back to per-symbol",
                        len(chunk),
                        exc,
                        exc_info=True,
                    )
                    for sym_idx in chunk:
                        try:
                            results[sym_idx] = self.analyze(ctxs[sym_idx])
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning(
                                "kronos: per-symbol fallback also failed for "
                                "ctx %d: %s",
                                sym_idx,
                                exc2,
                            )
                            results[sym_idx] = None
                    continue

                # Map chunk paths back to absolute indices
                for sym_idx, paths in zip(chunk, paths_per_symbol):
                    if paths is None:
                        results[sym_idx] = None
                        continue
                    try:
                        results[sym_idx] = self._build_view_from_paths(
                            ctxs[sym_idx], paths
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "kronos: view-construction failed for ctx %d: %s",
                            sym_idx,
                            exc,
                        )
                        results[sym_idx] = self._abstain(
                            reason=f"view_construction_error: {exc}"
                        )

        return results

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
            "device": self._resolved_device or self.config.device,
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
            device = _resolve_device(self.config)
            self._resolved_device = device
            dtype = _resolve_dtype(self.config, device)

            if self.config.weights_dir:
                model_id = f"{self.config.weights_dir}/Kronos-{self.config.model}"
                tokenizer_id = (
                    f"{self.config.weights_dir}/"
                    f"Kronos-Tokenizer-{self.config.model}"
                )
            else:
                model_id = f"NeoQuasar/Kronos-{self.config.model}"
                tokenizer_id = f"NeoQuasar/Kronos-Tokenizer-{self.config.model}"
            tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
            model = Kronos.from_pretrained(model_id)

            # Cast to dtype before handing to KronosPredictor — predictor's
            # __init__ does the .to(device) for us. Tokenizer stays fp32:
            # its decode path runs `.float()` on bit-mask tensors and feeds
            # them through `post_quant_embed` (an nn.Linear), so casting the
            # tokenizer to bf16 produces a Float×BFloat16 mismatch.
            if dtype is not None and dtype is not _torch_default_dtype():
                model = model.to(dtype=dtype)

            base_predictor = KronosPredictor(
                model=model,
                tokenizer=tokenizer,
                device=device,
                max_context=self.config.max_context,
            )

            if self.config.compile:
                # OPT-IN ONLY (plan §7.1 HIGH risk). Compile the model body;
                # leave tokenizer eager (variable-length decode buffers).
                try:
                    import torch  # type: ignore

                    base_predictor.model = torch.compile(  # type: ignore[assignment]
                        base_predictor.model, mode="reduce-overhead"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "kronos: torch.compile failed (%s); falling back to "
                        "eager (this is the documented safe path)",
                        exc,
                    )

            self._predictor = _DistributionalKronosPredictor(
                base_predictor, dtype=dtype
            )
            logger.info(
                "kronos: loaded %s on %s (dtype=%s); "
                "pred_len=%d sample_count=%d batch_size=%d",
                model_id,
                device,
                dtype,
                self.config.pred_len,
                self.config.sample_count,
                self.config.batch_size,
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

    def _seed(self) -> None:
        """Seed numpy + torch for replayability (charter invariant)."""
        if self.config.deterministic_seed is None:
            return
        np.random.seed(self.config.deterministic_seed)
        try:
            import torch  # type: ignore

            torch.manual_seed(self.config.deterministic_seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(self.config.deterministic_seed)
        except ImportError:
            pass

    def _predict_paths(self, bars: pd.DataFrame) -> np.ndarray:
        """Run Kronos with sample_count paths, return shape
        [sample_count, pred_len, n_features].

        Per ADR-0018 §D2 we use a subclass to expose pre-mean paths.
        Per Phase-7 follow-up: seed torch + numpy with config's
        deterministic_seed so signals are replayable from disk
        (charter "Reproducibility" invariant).
        """
        self._seed()
        # Truncate to max_context most recent bars
        recent = bars.tail(self.config.max_context).reset_index(drop=True)
        return self._predictor.predict_distributional(
            df=recent,
            pred_len=self.config.pred_len,
            sample_count=self.config.sample_count,
        )

    def _predict_paths_batch(
        self, ctxs_chunk: list[MarketContext]
    ) -> list[np.ndarray | None]:
        """Multi-symbol GPU batched forward.

        Returns one [sample_count, pred_len, n_features] array per ctx, or
        None for slots that the underlying batch could not produce (e.g.
        per-symbol prep raised). Order matches `ctxs_chunk`.
        """
        dfs = [
            ctx.bars.tail(self.config.max_context).reset_index(drop=True)
            for ctx in ctxs_chunk
        ]
        return self._predictor.predict_distributional_batch(
            dfs,
            pred_len=self.config.pred_len,
            sample_count=self.config.sample_count,
        )

    def _build_view_from_paths(
        self, ctx: MarketContext, paths: np.ndarray
    ) -> AnalystView:
        """Pure refactor of the view-construction tail of analyze().

        No semantic change — direction extraction → magnitude clip →
        calibrator → AnalystView. Shared by analyze() and analyze_batch().
        """
        bars = ctx.bars
        direction, magnitude, raw_confidence = self._direction_from_paths(
            paths,
            last_close=float(bars["close"].iloc[-1]),
        )

        # Per-horizon magnitude clip (foundation-model hallucination guard;
        # Codex MED 2026-05-26 — AMD 1d forecast hit 36.6%). Applied AFTER
        # direction/confidence are derived so path-agreement signal is
        # preserved; only |magnitude| is bounded.
        magnitude, magnitude_clipped = self._clip_magnitude(
            magnitude, direction=direction
        )

        # ColdStart shrinkage (-0.20 until calibrator has 200+ samples)
        confidence = self.calibrator.calibrate(raw_confidence)

        # ADR-0063: regime-aware confidence multiplier (gated by env flag).
        # Applied AFTER ADR-0018 calibrator (which already enforced [0.30, 0.85]
        # on raw_confidence). Kronos rule: dampen ×0.85 if regime label is UNKNOWN.
        # Clip to [0, 1] to be safe — the multiplier never inflates above 1.0
        # for any of our values but this is defensive.
        try:
            from hermes_quant.regime.regime_aware_confidence import apply_regime_multiplier
            _regime = ctx.extras.get("regime") if hasattr(ctx, "extras") else None
            confidence = apply_regime_multiplier(float(confidence), _regime, "kronos")
            confidence = max(0.0, min(1.0, float(confidence)))
        except Exception:  # noqa: BLE001
            pass

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
                "device": self._resolved_device or self.config.device,
                "n_paths": self.config.sample_count,
                "pred_len": self.config.pred_len,
                "raw_confidence_clipped": (
                    raw_confidence == self.config.raw_confidence_clip_high
                    or raw_confidence == self.config.raw_confidence_clip_low
                ),
                "magnitude_clipped": magnitude_clipped,
            },
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
            sign_agreement = float(
                np.mean(np.sign(pct_returns) == np.sign(median_return))
            )

        # Clip to [low, high] (foundation-model overconfidence guard)
        raw_confidence = float(
            np.clip(
                sign_agreement,
                self.config.raw_confidence_clip_low,
                self.config.raw_confidence_clip_high,
            )
        )

        return direction, magnitude, raw_confidence

    def _clip_magnitude(
        self,
        magnitude: float,
        *,
        direction: Direction,
    ) -> tuple[float, bool]:
        """Apply per-horizon magnitude cap.

        Per Codex MED 2026-05-26: Kronos can emit physically implausible
        magnitudes on volatile names (e.g. AMD 1d=36.6%, vs realized
        mega-cap equity 1d std ~2-3%). The cap from
        ``config.max_magnitude_per_horizon[horizon_label]`` bounds
        ``|magnitude|`` while leaving direction/confidence intact.

        Returns (clipped_magnitude, was_clipped).
        Missing horizon key → no clipping (returns input unchanged).
        """
        cap = self.config.max_magnitude_per_horizon.get(
            self.config.horizon_label
        )
        if cap is None:
            return magnitude, False
        if magnitude <= cap:
            return magnitude, False

        logger.warning(
            "kronos: magnitude %.4f exceeds cap %.4f for horizon=%s "
            "(direction=%+d) — clipping. This indicates foundation-model "
            "overconfidence on a volatile name; review the symbol.",
            magnitude,
            cap,
            self.config.horizon_label,
            direction,
        )
        return float(cap), True


def _torch_default_dtype():
    """Helper to compare against torch.get_default_dtype()."""
    try:
        import torch  # type: ignore

        return torch.get_default_dtype()
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# _DistributionalKronosPredictor — exposes pre-mean paths
# ---------------------------------------------------------------------------


class _DistributionalKronosPredictor:
    """Wrap upstream KronosPredictor to expose pre-mean paths.

    Two interfaces:
      - predict_distributional(df, ...) → [sample_count, pred_len, n_feat]
      - predict_distributional_batch([df, ...], ...) → list of arrays

    Both run a SINGLE batched forward (see GPU migration plan §2 + §4):
    the wrapper inlines the upstream `auto_regressive_inference` body up
    to (but not including) the final `np.mean(preds, axis=1)` call. This
    gives a ~30× speedup on any device vs the previous 30-iteration loop
    AND lets us GPU-batch across symbols.
    """

    # ---- Constructor ----

    def __init__(self, base_predictor, *, dtype=None):
        self._base = base_predictor
        # dtype for input tensor casts; None → match model's current dtype
        self._dtype = dtype

    # ---- Helpers ----

    @staticmethod
    def _x_timestamp_from_df(df: pd.DataFrame) -> pd.Series:
        """Extract a Series of datetimes for the historical bars.

        IMPORTANT: upstream Kronos's `calc_time_stamps` calls `.dt.minute`
        which only exists on pandas Series (not on DatetimeIndex). We
        always pass a Series of datetimes.
        """
        if isinstance(df.index, pd.DatetimeIndex):
            return pd.Series(df.index)
        if "timestamp" in df.columns:
            return pd.Series(pd.to_datetime(df["timestamp"]).values)
        # Codex review HIGH (2026-05-26): silently synthesizing timestamps
        # from utcnow() loses temporal alignment with the bars and
        # produces nonsense Kronos forecasts (foundation-model time
        # embeddings depend on actual hour/weekday/month). Fail closed.
        raise ValueError(
            "KronosAnalyst requires bars with a DatetimeIndex or a "
            "'timestamp' column. Synthesizing timestamps from utcnow() "
            "produces wrong forecasts because Kronos's time embeddings "
            "depend on actual hour/weekday/month."
        )

    @staticmethod
    def _y_timestamp_from_x(x_timestamp: pd.Series, pred_len: int) -> pd.Series:
        """Synthesize pred_len business-day timestamps after the last bar."""
        last = pd.Timestamp(x_timestamp.iloc[-1])
        return pd.Series(
            pd.date_range(
                start=last + pd.Timedelta(days=1), periods=pred_len, freq="B"
            )
        )

    def _prep_one(self, df: pd.DataFrame, pred_len: int):
        """Per-symbol prep shared by single + batched paths.

        Mirrors `KronosPredictor.predict()` lines 519-551 of upstream:
        - validate price columns
        - fill missing volume/amount
        - build x, x_stamp, y_stamp arrays
        - z-normalize x and clip to [-clip, clip]

        Returns (x_norm, x_stamp, y_stamp, x_mean, x_std).
        """
        from model.kronos import calc_time_stamps  # type: ignore

        base = self._base
        if not isinstance(df, pd.DataFrame):
            raise ValueError("Input must be a pandas DataFrame.")
        if not all(col in df.columns for col in base.price_cols):
            raise ValueError(
                f"Price columns {base.price_cols} not found in DataFrame."
            )

        df = df.copy()
        if base.vol_col not in df.columns:
            df[base.vol_col] = 0.0
            df[base.amt_vol] = 0.0
        if base.amt_vol not in df.columns and base.vol_col in df.columns:
            df[base.amt_vol] = (
                df[base.vol_col] * df[base.price_cols].mean(axis=1)
            )

        if df[base.price_cols + [base.vol_col, base.amt_vol]].isnull().values.any():
            raise ValueError(
                "Input DataFrame contains NaN values in price or volume columns."
            )

        x_timestamp = self._x_timestamp_from_df(df)
        y_timestamp = self._y_timestamp_from_x(x_timestamp, pred_len)

        x_time_df = calc_time_stamps(x_timestamp)
        y_time_df = calc_time_stamps(y_timestamp)

        x = df[base.price_cols + [base.vol_col, base.amt_vol]].values.astype(
            np.float32
        )
        x_stamp = x_time_df.values.astype(np.float32)
        y_stamp = y_time_df.values.astype(np.float32)

        x_mean = np.mean(x, axis=0)
        x_std = np.std(x, axis=0)

        x_norm = (x - x_mean) / (x_std + 1e-5)
        x_norm = np.clip(x_norm, -base.clip, base.clip)
        return x_norm, x_stamp, y_stamp, x_mean, x_std

    # ---- Inlined batched autoregressive inference (returns pre-mean paths) ----

    def _ari_distributional(
        self,
        x_batch: np.ndarray,
        x_stamp_batch: np.ndarray,
        y_stamp_batch: np.ndarray,
        pred_len: int,
        sample_count: int,
    ) -> np.ndarray:
        """Inline of upstream `auto_regressive_inference` minus the final
        `np.mean(preds, axis=1)`. Returns pre-mean paths in shape
        [num_series, sample_count, pred_len_total, n_features], where
        `pred_len_total = initial_seq_len + pred_len`. Caller should
        slice to `[..., -pred_len:, :]` and denormalize per series.

        Mirrors model/kronos.py lines 389-469. Stays inside torch.no_grad
        (matches upstream line 390).
        """
        import torch  # type: ignore

        base = self._base
        tokenizer = base.tokenizer
        model = base.model
        max_context = base.max_context
        clip = base.clip
        # Match upstream defaults
        T_temp = 1.0
        top_k = 0
        top_p = 0.99

        device = base.device
        # Determine the model's actual weight dtype. The tokenizer is kept
        # in fp32 (its `indices_to_bits` does `.float()` internally and feeds
        # `post_quant_embed`/`encoder`/`decoder` linears that must match its
        # input dtype). The MAIN model however was cast to `model_dtype` in
        # `_lazy_load`, and its `decode_s1`/`decode_s2` linears want stamps
        # in that dtype. So:
        #   - x      → fp32 (fed into tokenizer.encode)
        #   - stamps → model_dtype (fed into model.decode_s1/s2)
        try:
            model_dtype = next(model.parameters()).dtype
        except StopIteration:
            model_dtype = torch.float32
        x_t = torch.from_numpy(np.array(x_batch).astype(np.float32)).to(
            device=device, dtype=torch.float32
        )
        x_stamp_t = torch.from_numpy(
            np.array(x_stamp_batch).astype(np.float32)
        ).to(device=device, dtype=model_dtype)
        y_stamp_t = torch.from_numpy(
            np.array(y_stamp_batch).astype(np.float32)
        ).to(device=device, dtype=model_dtype)

        with torch.no_grad():
            x = torch.clip(x_t, -clip, clip)
            num_series = x.size(0)

            # Tile each symbol across sample_count rows along dim 0
            x = (
                x.unsqueeze(1)
                .repeat(1, sample_count, 1, 1)
                .reshape(-1, x.size(1), x.size(2))
            )
            x_stamp = (
                x_stamp_t.unsqueeze(1)
                .repeat(1, sample_count, 1, 1)
                .reshape(-1, x_stamp_t.size(1), x_stamp_t.size(2))
            )
            y_stamp = (
                y_stamp_t.unsqueeze(1)
                .repeat(1, sample_count, 1, 1)
                .reshape(-1, y_stamp_t.size(1), y_stamp_t.size(2))
            )

            x_token = tokenizer.encode(x, half=True)
            initial_seq_len = x.size(1)
            batch_size = x_token[0].size(0)
            total_seq_len = initial_seq_len + pred_len
            full_stamp = torch.cat([x_stamp, y_stamp], dim=1)

            generated_pre = x_token[0].new_empty(batch_size, pred_len)
            generated_post = x_token[1].new_empty(batch_size, pred_len)

            pre_buffer = x_token[0].new_zeros(batch_size, max_context)
            post_buffer = x_token[1].new_zeros(batch_size, max_context)
            buffer_len = min(initial_seq_len, max_context)
            if buffer_len > 0:
                start_idx = max(0, initial_seq_len - max_context)
                pre_buffer[:, :buffer_len] = x_token[0][
                    :, start_idx : start_idx + buffer_len
                ]
                post_buffer[:, :buffer_len] = x_token[1][
                    :, start_idx : start_idx + buffer_len
                ]

            from model.kronos import sample_from_logits  # type: ignore

            for i in range(pred_len):
                current_seq_len = initial_seq_len + i
                window_len = min(current_seq_len, max_context)

                if current_seq_len <= max_context:
                    input_tokens = [
                        pre_buffer[:, :window_len],
                        post_buffer[:, :window_len],
                    ]
                else:
                    input_tokens = [pre_buffer, post_buffer]

                context_end = current_seq_len
                context_start = max(0, context_end - max_context)
                current_stamp = full_stamp[
                    :, context_start:context_end, :
                ].contiguous()

                s1_logits, context = model.decode_s1(
                    input_tokens[0], input_tokens[1], current_stamp
                )
                s1_logits = s1_logits[:, -1, :]
                sample_pre = sample_from_logits(
                    s1_logits,
                    temperature=T_temp,
                    top_k=top_k,
                    top_p=top_p,
                    sample_logits=True,
                )

                s2_logits = model.decode_s2(context, sample_pre)
                s2_logits = s2_logits[:, -1, :]
                sample_post = sample_from_logits(
                    s2_logits,
                    temperature=T_temp,
                    top_k=top_k,
                    top_p=top_p,
                    sample_logits=True,
                )

                generated_pre[:, i] = sample_pre.squeeze(-1)
                generated_post[:, i] = sample_post.squeeze(-1)

                if current_seq_len < max_context:
                    pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                    post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
                else:
                    pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                    post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                    pre_buffer[:, -1] = sample_pre.squeeze(-1)
                    post_buffer[:, -1] = sample_post.squeeze(-1)

            full_pre = torch.cat([x_token[0], generated_pre], dim=1)
            full_post = torch.cat([x_token[1], generated_post], dim=1)

            context_start = max(0, total_seq_len - max_context)
            input_tokens = [
                full_pre[:, context_start:total_seq_len].contiguous(),
                full_post[:, context_start:total_seq_len].contiguous(),
            ]
            z = tokenizer.decode(input_tokens, half=True)
            # Reshape to [num_series, sample_count, T, feat]. NB: upstream
            # then averages over the sample_count dim — we DO NOT.
            z = z.reshape(num_series, sample_count, z.size(1), z.size(2))
            # Cast float buffer (model may be bf16/fp16) to fp32 for downstream
            # numpy ops; values are already denoised by the autoregressive
            # head sampling, so no precision loss for direction/magnitude.
            preds = z.float().cpu().numpy()
            return preds  # [num_series, sample_count, total_seq_len, feat]

    # ---- Public batched API ----

    def predict_distributional(
        self,
        df: pd.DataFrame,
        pred_len: int,
        sample_count: int,
    ) -> np.ndarray:
        """Single-symbol path: returns [sample_count, pred_len, n_feat].

        Single batched forward (sample_count along the GPU batch dim).
        Replaces the previous 30-iteration loop (~30× faster on any
        device; multiplicative with GPU vs CPU).
        """
        x_norm, x_stamp, y_stamp, x_mean, x_std = self._prep_one(df, pred_len)

        x_batch = x_norm[np.newaxis, :, :]  # (1, seq_len, feat)
        x_stamp_batch = x_stamp[np.newaxis, :, :]
        y_stamp_batch = y_stamp[np.newaxis, :, :]

        preds = self._ari_distributional(
            x_batch, x_stamp_batch, y_stamp_batch, pred_len, sample_count
        )
        # preds: [1, sample_count, total_seq_len, feat]
        # Slice to pred_len-only window, drop the symbol dim
        preds = preds[0, :, -pred_len:, :]  # [sample_count, pred_len, feat]
        # Denormalize
        preds = preds * (x_std + 1e-5) + x_mean
        # Reorder upstream column order [open, high, low, close, vol, amt] →
        # legacy contract [close, open, high, low, volume]
        # Upstream price_cols = ['open', 'high', 'low', 'close']; idx 3 = close
        return preds[..., [3, 0, 1, 2, 4]]

    def predict_distributional_batch(
        self,
        df_list: list[pd.DataFrame],
        pred_len: int,
        sample_count: int,
    ) -> list[np.ndarray | None]:
        """Multi-symbol path: returns list of [sample_count, pred_len, feat]
        arrays, one per input df. Slots that fail prep return None.

        All dfs MUST share the same historical length (upstream constraint
        — see KronosPredictor.predict_batch line 642). Caller (analyze_batch)
        is responsible for grouping by seq_len.
        """
        if not df_list:
            return []

        # Per-symbol prep
        x_norms = []
        x_stamps = []
        y_stamps = []
        x_means = []
        x_stds = []
        prep_errors: dict[int, str] = {}
        for i, df in enumerate(df_list):
            try:
                x_norm, x_stamp, y_stamp, x_mean, x_std = self._prep_one(
                    df, pred_len
                )
                x_norms.append(x_norm)
                x_stamps.append(x_stamp)
                y_stamps.append(y_stamp)
                x_means.append(x_mean)
                x_stds.append(x_std)
            except Exception as exc:  # noqa: BLE001
                prep_errors[i] = str(exc)
                # Insert placeholder so list indexing stays aligned during
                # batch construction; we'll filter to good slots below.
                x_norms.append(None)
                x_stamps.append(None)
                y_stamps.append(None)
                x_means.append(None)
                x_stds.append(None)

        good_idx = [i for i, x in enumerate(x_norms) if x is not None]
        if not good_idx:
            return [None] * len(df_list)

        # Verify uniform seq_len among good symbols (upstream constraint)
        seq_lens = {x_norms[i].shape[0] for i in good_idx}
        if len(seq_lens) != 1:
            raise ValueError(
                "predict_distributional_batch requires uniform historical "
                f"length across symbols, got: {seq_lens}"
            )

        x_batch = np.stack([x_norms[i] for i in good_idx], axis=0).astype(
            np.float32
        )
        x_stamp_batch = np.stack(
            [x_stamps[i] for i in good_idx], axis=0
        ).astype(np.float32)
        y_stamp_batch = np.stack(
            [y_stamps[i] for i in good_idx], axis=0
        ).astype(np.float32)

        preds = self._ari_distributional(
            x_batch, x_stamp_batch, y_stamp_batch, pred_len, sample_count
        )
        # preds: [num_series, sample_count, total_seq_len, feat]

        results: list[np.ndarray | None] = [None] * len(df_list)
        for slot_idx, abs_idx in enumerate(good_idx):
            p = preds[slot_idx, :, -pred_len:, :]  # [sample_count, pred_len, feat]
            p = p * (x_stds[abs_idx] + 1e-5) + x_means[abs_idx]
            # Reorder to [close, open, high, low, volume]
            results[abs_idx] = p[..., [3, 0, 1, 2, 4]]
        for bad_idx, err in prep_errors.items():
            logger.warning(
                "kronos: predict_distributional_batch prep failed for slot "
                "%d: %s",
                bad_idx,
                err,
            )
        return results
