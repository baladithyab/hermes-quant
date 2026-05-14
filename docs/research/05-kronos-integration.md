# Kronos integration research (for hermes-quant v0.3 KronosAnalyst)

> Source: `shiyu-coder/Kronos` (AAAI 2026), via DeepWiki + arXiv `2508.02739`.
> All upstream paths in this doc are relative to that repo's root.
> Status: research lens — implementation lives in `hermes_quant/analysts/kronos.py`.

## TL;DR (3 bullets)

- **Kronos is a *point* forecaster, not a distributional one.** `KronosPredictor.predict()`
  returns a single OHLCV DataFrame; `sample_count>1` paths are **averaged inside
  `auto_regressive_inference`** (`model/kronos.py`, see the `np.mean(preds, axis=1)` at
  the tail). To get a directional view + uncertainty we must **subclass and override
  `auto_regressive_inference` to expose the raw `[batch, sample_count, pred_len, F]`
  cube** before the mean. This is P0.
- **API is minimal and well-behaved.** `Kronos.from_pretrained("NeoQuasar/Kronos-base")`
  + `KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")` + a
  `KronosPredictor(model, tokenizer, max_context=512, device=...)` wrapper. Tokenization,
  z-score normalization, padding/truncation are all internal. Deps: `torch`,
  `numpy`, `pandas`, `huggingface_hub`, `tqdm` — **no `transformers`, no `einops`**.
- **MIT license, weights on HF Hub, ~102M params for base.** Commercial use OK.
  Latency on CPU is the binding constraint: with `pred_len=12, sample_count=30,
  max_context=512` expect **~3–10 s/call CPU, ~150–400 ms GPU**. At 15-min cadence
  per asset this is fine for a handful of symbols; for >20 we either downshift to
  `Kronos-small` (24.7M) or push to a worker process.

---

## API surface (load + infer + output shape)

Upstream entry points (all imports are from the in-repo `model/` package, not `transformers`):

```python
# upstream: model/kronos.py + model/__init__.py
from model import Kronos, KronosTokenizer, KronosPredictor

tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model     = Kronos.from_pretrained("NeoQuasar/Kronos-base")
predictor = KronosPredictor(model, tokenizer, max_context=512, device="cpu")  # auto: cuda > mps > cpu

pred_df = predictor.predict(
    df=hist[["open","high","low","close","volume","amount"]],   # required cols
    x_timestamp=hist["timestamp"],                              # pd.Series, len = lookback
    y_timestamp=future_ts,                                      # pd.Series, len = pred_len
    pred_len=12,                                                # steps ahead
    T=1.0, top_k=0, top_p=0.9, sample_count=30, verbose=False,
)
# pred_df: pd.DataFrame indexed by y_timestamp, cols = open/high/low/close/volume/amount
```

Key facts (cite `model/kronos.py` unless noted):

- `from_pretrained` is provided via `huggingface_hub.PyTorchModelHubMixin` — it's a
  vanilla HF Hub download; **no `transformers` runtime dep**.
- `KronosPredictor.predict()` does z-score normalize (per-input-window mean/std,
  clip ±5), packs OHLCV + temporal features (minute, hour, weekday, day, month from
  `calc_time_stamps()`), runs `auto_regressive_inference` under sliding window
  truncation to `max_context`, then denormalizes.
- `volume` and `amount` columns are optional; missing ones are zero-filled. We have
  `volume` from all providers; `amount` (quote-currency) only from yfinance/ccxt
  when `quote_volume` is available.
- **Returned DataFrame is the per-step mean across `sample_count` paths.** No std,
  no quantiles. This is the integration's central friction.

## Dependencies + install footprint

From import inspection (DeepWiki could not show the full `requirements.txt`, but
the import surface is small):

| dep             | required? | notes                                                                |
|-----------------|-----------|----------------------------------------------------------------------|
| `torch`         | yes       | ≥ 2.0; mps/cuda auto-detected. CPU works.                            |
| `numpy`         | yes       | any modern                                                           |
| `pandas`        | yes       | any modern; uses `pd.Series`, `pd.DataFrame`                         |
| `huggingface_hub` | yes     | for `PyTorchModelHubMixin.from_pretrained` + weight download         |
| `tqdm`          | yes       | progress bar in autoregressive loop                                  |
| `transformers`  | **no**    | NOT imported anywhere in `model/`                                    |
| `einops`        | **no**    | NOT imported in upstream snippets                                    |

Install footprint estimate: torch + numpy + pandas + hf_hub ≈ 1.5 GB on CPU build,
plus ~410 MB for `Kronos-base` weights + ~50 MB for the tokenizer. We make this
optional via `pip install hermes-quant[kronos]` extra in `pyproject.toml`.

## Distributional output → directional view conversion

Upstream gives us a mean OHLCV path. We need `(direction ∈ {-1,0,+1}, magnitude,
confidence_raw)` per ADR-0002.

**The Kairos approach (from charter notes):** ran on crypto-1m with `pred_len=30`,
took `sign(pred_close[-1] / last_close - 1)` as direction, magnitude as the abs
return. **Found IC alpha only on crypto-1m-h30; A-shares daily was negative IC.**
This is consistent with foundation-model overconfidence on slow regimes — the
12B-bar pretraining corpus is dominated by minute-bar liquid markets. Their
mistake: **no calibration shrinkage, no horizon-aware temperature, used only
the mean path so confidence was a flat 1.0.**

**Our approach (P0, supersedes Kairos):**

1. Subclass `KronosPredictor` → `_DistributionalKronosPredictor`. Override
   `auto_regressive_inference` to return the pre-mean cube
   `Z ∈ ℝ^{sample_count × pred_len × 6}` instead of the mean.
2. From Z, take `pred_close = Z[:, -1, 3]` (close col index 3 in OHLCV-V-A order
   — verify against `model/kronos.py` constant; flag in tests).
3. Compute path returns `r_i = pred_close_i / last_close - 1`, `i ∈ [1, sample_count]`.
4. **Direction** = `sign(median(r_i))`, but only if `|median| > σ_intrabar / 4`
   (else flat — silence-by-default).
5. **Magnitude** = `clip(|median(r_i)|, 0.0005, 0.05)`.
6. **Raw confidence** = fraction of paths agreeing with median direction
   (range 0.5 → 1.0; renormalize to [0, 1] as `2 * frac - 1`).
7. Pass `confidence_raw` through `ColdStartCalibrator` (`max(0, raw - 0.20)`)
   until N≥200 fitted samples per `(asset_class, timeframe, horizon)` bucket.

This gives us a real distributional confidence (path agreement) instead of
Kairos's de-facto constant `confidence=1.0`.

## Inference cost + latency budget for our 15-min cadence

No upstream benchmarks in the repo (DeepWiki confirms). Reference points from
the paper §4 + transformer scaling laws:

| config                                          | CPU (8c)    | GPU (T4/L4)  |
|-------------------------------------------------|-------------|--------------|
| Kronos-small (24.7M), ctx=512, pred=12, S=30    | 1.0–2.5 s   | 80–150 ms    |
| Kronos-base  (102M),  ctx=512, pred=12, S=30    | 3–10 s      | 150–400 ms   |
| Kronos-base  (102M),  ctx=512, pred=120, S=30   | 30–90 s     | 1.5–4 s      |

Latency scales ~linearly in `pred_len * sample_count` (each is one forward pass
through the AR head per token; `sample_count` is parallelized inside the batch
dim — see `x.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(...)` in
`auto_regressive_inference`).

**Budget at our 15-min cadence with ~5 assets:**

- CPU box, Kronos-small, pred=8 (2 hours fwd on 15-min), S=20 → ~10 s/tick total
  for 5 assets → fits comfortably in the 15-min window.
- CPU box, Kronos-base, pred=8, S=20 → ~30 s/tick for 5 assets → also fits but
  no headroom; **default = small, opt-in to base via config.**
- For >10 assets we run the predictor in a side process and feed `AnalystView`s
  via a queue (deferred to v0.3.1).

## Lazy-load + offline-fallback design

Money-software discipline: **silent fallback on weight-load failure, never crash.**

```python
class KronosAnalyst:
    def __init__(self, model_id="NeoQuasar/Kronos-small", ...):
        self._predictor = None        # lazy
        self._load_failed = False     # cached failure flag
        self._load_error: str | None = None

    def _ensure_loaded(self) -> bool:
        if self._predictor is not None:
            return True
        if self._load_failed:
            return False
        try:
            from model import Kronos, KronosTokenizer, KronosPredictor   # lazy import
            tok = KronosTokenizer.from_pretrained(self.tokenizer_id, local_files_only=self.offline)
            mdl = Kronos.from_pretrained(self.model_id, local_files_only=self.offline)
            self._predictor = KronosPredictor(mdl, tok, max_context=512, device=self.device)
            return True
        except Exception as e:
            self._load_failed = True
            self._load_error = repr(e)
            logger.warning("kronos load failed (will silence): %s", e)
            return False
```

`analyze(ctx)` first calls `self._ensure_loaded()`; on `False` it returns `None`
(silence) and `health()` exposes `{"loaded": False, "load_error": ...}` for
`quant_doctor`. **No crash, no retry-loop, just silence.** First successful
load caches; first failure caches too — operator restarts daemon to retry
(or `hermes quant kronos warmup` CLI in v0.3.1).

`local_files_only=True` (config flag `kronos.offline=true`) is the offline-box
mode — uses the HF cache pre-warmed during `hermes quant setup`.

## Confidence-calibration concerns

Kronos is pretrained autoregressively on 12B bars with no calibration loss.
**Foundation-model overconfidence is the headline risk.** Three concrete
mitigations:

1. **Path-agreement confidence (above)** instead of model-internal logits
   — softmax temperature is unreliable across regimes.
2. **Magnitude clipping `[0.0005, 0.05]`.** The model can hallucinate
   double-digit moves on noisy crypto bars; clip protects the sizer.
3. **Per-(asset_class, timeframe, horizon) IsotonicCalibrator** trained from
   N≥200 RealizedOutcomes. Cold-start = `max(0, raw - 0.20)` for the first
   200 ticks per bucket. Same machinery as classical_ta + microstructure.
4. **Horizon-temperature gating.** Set `T=1.0` for ≤30-step horizons, `T=0.7`
   for longer (sharper sampling damps drift). This is empirical from Kairos's
   crypto-1m-h30 result.
5. **Negative-IC detector.** If the calibrator's rolling 30-day IC against
   realized returns goes negative for a bucket, **downweight that bucket to
   confidence ≤ 0.2 in the BMA** — don't silently let a flipped signal hurt us.

## Adapter shape for `Analyst` Protocol (concrete code sketch)

```python
# hermes_quant/analysts/kronos.py
from __future__ import annotations
import logging
from typing import Any
import numpy as np
import pandas as pd
from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AnalystView, Direction, MarketContext, RealizedOutcome,
)

logger = logging.getLogger(__name__)

class KronosAnalyst:
    name = "kronos"
    timeframes = ["5m", "15m", "1h", "4h", "1d"]
    asset_classes = ["crypto", "equity", "etf"]
    enabled = True

    def __init__(self, *, model_id: str = "NeoQuasar/Kronos-small",
                 tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base",
                 max_context: int = 512, lookback: int = 256,
                 pred_len: int = 8, sample_count: int = 20,
                 horizon: str = "1h", device: str = "cpu",
                 offline: bool = False, T: float = 1.0):
        self.model_id, self.tokenizer_id = model_id, tokenizer_id
        self.max_context, self.lookback = max_context, lookback
        self.pred_len, self.sample_count = pred_len, sample_count
        self.horizon, self.device, self.offline, self.T = horizon, device, offline, T
        self._predictor = None
        self._load_failed = False
        self._load_error: str | None = None
        self._n_views = 0
        self._calibrator = ColdStartCalibrator()

    def _ensure_loaded(self) -> bool:  # see "Lazy-load" section above
        ...

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        if not self._ensure_loaded():
            return None
        bars = ctx.bars.tail(self.lookback)
        if len(bars) < 64:                # arbitrary min; fewer = silence
            return None
        try:
            paths = self._predict_paths(bars, ctx.asof)   # (S, pred_len, 6)
            last_close = float(bars["close"].iloc[-1])
            term_close = paths[:, -1, 3]                  # close index = 3
            r = term_close / last_close - 1.0
            med = float(np.median(r))
            agree = float(np.mean(np.sign(r) == np.sign(med)))
            raw_conf = float(np.clip(2 * agree - 1, 0.0, 1.0))
            mag = float(np.clip(abs(med), 0.0005, 0.05))
            if mag < 0.0005 or raw_conf < 0.1:
                return None                               # silence
            direction: Direction = 1 if med > 0 else -1
            calibrated = self._calibrator.calibrate(raw_conf)
            self._n_views += 1
            return AnalystView(
                analyst=self.name, direction=direction, magnitude=mag,
                confidence=calibrated, confidence_raw=raw_conf,
                horizon=self.horizon,
                rationale=f"kronos S={self.sample_count} agree={agree:.2f} med_r={med:.4f}",
                metadata={"path_std": float(r.std()), "n_paths": int(paths.shape[0])},
            )
        except Exception as e:                            # noqa: BLE001
            logger.exception("kronos analyze failed: %s", e)
            return None

    def update(self, outcome: RealizedOutcome) -> None:
        self._calibrator.fit([outcome.view.confidence_raw], [outcome.direction_correct])

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name, "loaded": self._predictor is not None,
            "load_error": self._load_error, "n_views_emitted": self._n_views,
            "calibrator_status": self._calibrator.status(),
        }
```

`_predict_paths` is the only Kronos-touching method; it owns the
`auto_regressive_inference` override that returns the pre-mean cube. Keeping it
in one private method means a future weight-API change is a one-method patch.

## Risks + mitigations

| risk                                         | likelihood | impact | mitigation                                                                  |
|----------------------------------------------|------------|--------|-----------------------------------------------------------------------------|
| HF Hub download fails at startup             | medium     | low    | Lazy-load + silence fallback; pre-warm cache in `hermes quant setup`        |
| Weights revision drift (NeoQuasar repush)    | low        | medium | Pin a `revision=` kwarg in `from_pretrained`; recorded in `quant_doctor`    |
| Upstream API change in `model/` package      | low        | high   | Vendor a snapshot under `hermes_quant/_vendor/kronos/` (gated by config)    |
| Foundation-model overconfidence              | high       | high   | Path-agreement conf + magnitude clip + isotonic calib + neg-IC detector     |
| CPU latency overruns 15-min budget           | medium     | medium | Default `Kronos-small`, downshift `pred_len`+`sample_count`, side-process   |
| Kronos-large not open-source                 | n/a        | low    | We don't use it; base/small only                                            |
| Look-ahead via `y_timestamp` future leak     | low        | high   | y_timestamp is generated forward from `ctx.asof` only; CI shuffle test      |
| MIT license on weights changes               | very low   | low    | Pin to current revision; vendor weights to internal mirror for prod         |

## P0/P1/P2 implementation order for v0.3

**P0 (must ship in v0.3):**

1. `hermes_quant/analysts/kronos.py` with `KronosAnalyst` per the sketch above.
2. `_DistributionalKronosPredictor` subclass in same file — overrides
   `auto_regressive_inference` to return the `(S, pred_len, F)` cube.
3. `pyproject.toml` extras: `[kronos] = ["torch>=2.0", "huggingface_hub>=0.23",
   "kronos @ git+https://github.com/shiyu-coder/Kronos@<pinned-sha>"]`.
   We pin a SHA; we do not track main.
4. Lazy-load + silence-on-failure path; `quant_doctor` surfaces `loaded` + `load_error`.
5. Unit test `tests/unit/analysts/test_kronos.py` with a fake-predictor that
   returns a known cube; verify direction/magnitude/confidence math.
6. Lookahead CI test passes (analyze on shuffled timestamps yields no edge).
7. BMA registration: `KronosAnalyst` joins ClassicalTA + MicrostructureLite as
   the third voice. No bypass — risk gate still owns the silence path.

**P1 (v0.3.1):**

1. `hermes quant kronos warmup` CLI to pre-download weights into HF cache.
2. Side-process predictor for >10 asset universes (queue-fed).
3. Per-bucket IsotonicCalibrator promotion at N=200; rolling neg-IC detector.
4. Pin a vendored snapshot of `model/` under `hermes_quant/_vendor/kronos/`
   so we're insulated from upstream churn.
5. Horizon-aware temperature schedule (`T=1.0` for ≤30 steps, `T=0.7` longer).

**P2 (v0.4+):**

1. Optional fine-tuning loop on user data (`finetune/` upstream uses Qlib;
   we'd wire to our own bars).
2. ONNX export for sub-100 ms CPU inference.
3. Multi-horizon ensemble: emit multiple `AnalystView`s per tick at different
   `pred_len`s, let the BMA aggregate across horizons.
4. Replace point-mean with a learned distributional head (Kronos + diffusion
   tail) — research-grade, not v0.3 critical.

---

### Upstream file map (cite-points used in this doc)

- `model/kronos.py` — `Kronos`, `KronosPredictor`, `auto_regressive_inference`,
  `generate`, normalization, `np.mean(preds, axis=1)` averaging site.
- `model/module.py` — neural net building blocks; not directly touched.
- `model/__init__.py` — `from model import Kronos, KronosTokenizer, KronosPredictor`.
- `examples/prediction_example.py` — canonical usage example (referenced in README).
- `tests/test_kronos_regression.py` — pinned revisions for `Kronos-small` and
  `Kronos-Tokenizer-base`; useful for our own pin choice.
- `LICENSE` — MIT.
- HF Hub: `NeoQuasar/Kronos-{mini,small,base}` + `NeoQuasar/Kronos-Tokenizer-{2k,base}`.
