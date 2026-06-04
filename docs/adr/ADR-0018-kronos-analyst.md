# ADR-0018: KronosAnalyst — third voice, not the oracle

**Status**: Accepted (2026-05-13), implemented
**Date**: 2026-05-13
**Target**: v0.3.0
**Cross-cuts**: ADR-0002 (Analyst Protocol), ADR-0003 (BMA aggregator), ADR-0012 (LLMAnalyst protocol — separate, deferred), founding charter §"Kronos is one analyst, not the whole system"
**Research**: [`docs/research/05-kronos-integration.md`](../research/05-kronos-integration.md)

---

## Context

The founding charter — quoting directly:

> *"**Key insight for your framework:** Kronos is *one analyst, not the whole system*. It produces a probabilistic OHLCV forecast. Your ensemble should treat it as a single feature-generator alongside others, not as the oracle."*

> *"AAAI 2026 acceptance ≠ alpha. Kronos is a good foundation model. It is NOT a profitable trading system."*

The MVP recipe explicitly lists Kronos as the third voice in the BMA committee (alongside ClassicalTA and MicrostructureLite). v0.2.0 ships TA + MicrostructureLite. v0.3 adds Kronos.

Research findings ([05-kronos-integration.md](../research/05-kronos-integration.md)) sharpened the brief:

1. **Kronos is a POINT forecaster, not distributional** despite the marketing. `KronosPredictor.predict()` averages all `sample_count` paths internally via `np.mean(preds, axis=1)`. To get distributional output we'd subclass and expose the pre-mean cube.
2. **Foundation-model overconfidence is the dominant risk class.** Kairos (the academic derivative) found: A-shares daily IC was *negative*, crypto-1m-h30 IC was +0.076. Same model, opposite alpha. Kronos confidence is uncalibrated to your asset class.
3. **MIT license.** Commercial use OK. Weights on HF Hub at `NeoQuasar/Kronos-{mini,small,base,large}`. We ship `base` (102M) as default; `large` (499M) is closed-source — unavailable.
4. **Latency**: ~3-10s CPU, 150-400ms GPU per call at `ctx=512, pred_len=12`. Our 15-min cadence has 900s budget per tick × 5-symbol watchlist = 180s/symbol → CPU is fine. GPU is nice-to-have.
5. **Dependencies**: `torch`, `numpy`, `pandas`, `huggingface_hub`, `tqdm`. **NO** `transformers`, **NO** `einops`. Smaller install footprint than expected.

## Decision

### D1: KronosAnalyst is one voice, weighted by BMA like every other analyst

The aggregator (BMA) does not know Kronos exists. It sees an `AnalystView` with `confidence`, `direction`, `magnitude`, `horizon` — same Protocol as ClassicalTA and MicrostructureLite. If Kronos's calibrator (per ADR-0010) shows it underperforms in a given regime, BMA downweights it.

This codifies the charter's *"one analyst, not the oracle"* principle in the type system: there's no special case anywhere for Kronos.

### D2: Distributional output via subclass

We do NOT use `KronosPredictor.predict()`'s averaged output. Instead:

```python
class _DistributionalKronosPredictor(KronosPredictor):
    """Exposes pre-mean sample paths for ensemble-disagreement confidence."""
    def predict_distributional(self, *args, **kwargs):
        # Mirror KronosPredictor.predict's pre-processing + tokenization
        # but skip the np.mean(preds, axis=1) collapse
        ...
        return preds_cube   # shape: [batch, sample_count, pred_len, n_features]
```

The `S=30` sample-path output gives us a proxy for the model's epistemic uncertainty. **Path-agreement confidence** = fraction of paths whose pred_len-step return has the same sign as the ensemble median. This is calibrated to predictive disagreement, not to nominal raw-token probability.

### D3: Confidence clipping (foundation-model-overconfidence guard)

```python
RAW_CONFIDENCE_CLIP = (0.30, 0.85)
```

Even when path-agreement is 30/30 unanimous, we clip nominal confidence at 0.85. Even when paths split 16/14, we floor at 0.30. The BMA aggregator's calibrator can shrink further; the clip is the absolute floor/ceiling.

This is the direct mitigation for the Kairos failure mode: A-shares Kronos confidence was high but realized accuracy was negative. Without clipping, BMA's calibrator takes ~200 fills to learn that lesson; with clipping, the worst-case overconfidence is bounded immediately.

### D4: Lazy-load + offline-fallback

Kronos weights are downloaded from HuggingFace at first `analyze()` call, NOT at register time. Two reasons:
- `register(ctx)` runs at gateway startup; a slow HF download blocks the entire Hermes gateway
- Operators on offline boxes can install hermes-quant + use ClassicalTA + MicrostructureLite without ever hitting HF

```python
def analyze(self, ctx: MarketContext) -> AnalystView:
    if self._predictor is None:
        try:
            self._predictor = self._load_kronos()
        except (ImportError, OSError, RuntimeError) as exc:
            logger.warning("KronosAnalyst: weight load failed: %s; abstaining", exc)
            return AnalystView(
                analyst=self.name,
                direction=0, magnitude=0.0,
                confidence=0.0,    # zero confidence = silence_gate filters out
                horizon="1h",
                metadata={"abstain_reason": str(exc)},
            )
    ...
```

The zero-confidence abstain plays cleanly with the silence-bias gate (ADR-0016 §D2 dim 1: `min_confidence=0.65` ensures abstaining KronosAnalyst doesn't accidentally veto via `min_analysts_emitted` since we still count as emitted-but-zero-confidence).

**Wait — that's a footgun.** The silence-bias gate's `min_analysts_emitted=2` counts views by `len(analyst_views)`, not by confidence. An abstaining Kronos still bumps the count to 3-of-3. Mitigation: BMA aggregator's `aggregate()` filters out views with `confidence < 0.10` BEFORE constructing the aggregated signal — abstainers are pruned upstream of the silence-bias gate. (Amends ADR-0003 §"abstain handling" — already there in stub form, formalize.)

### D5: Distributional → directional adapter

```python
def _direction_from_paths(paths: np.ndarray, pred_len: int = 12) -> tuple[int, float, float]:
    """paths: [sample_count, pred_len, n_features]. Returns (direction, magnitude, conf)."""
    last_close = ctx.bars["close"].iloc[-1]
    end_close_paths = paths[:, -1, 0]    # close column of last predicted bar
    pct_returns = (end_close_paths - last_close) / last_close

    median_return = np.median(pct_returns)
    sign_agreement = np.mean(np.sign(pct_returns) == np.sign(median_return))

    direction = 1 if median_return > 0 else (-1 if median_return < 0 else 0)
    magnitude = abs(median_return)
    confidence = float(np.clip(sign_agreement, *RAW_CONFIDENCE_CLIP))
    return direction, magnitude, confidence
```

Magnitude is the median % return; confidence is the path agreement on sign; direction is the sign of the median.

### D6: Default model = `base` (102M)

Operators who want bigger choose `large` (499M, closed-source — won't load) or `small` (24.7M). Default `base`.

```yaml
# ~/.hermes/config.yaml
quant:
  analysts:
    kronos:
      model: base                              # base | small | mini
      device: cpu                              # cpu | cuda | mps
      max_context: 512
      pred_len: 12
      sample_count: 30
      raw_confidence_clip: [0.30, 0.85]
```

### D7: Optional-extras install

```
[project.optional-dependencies]
kronos = ["torch>=2.0", "huggingface_hub>=0.20"]
```

Install: `pip install 'hermes-quant[kronos]'`. Lazy-loaded, so missing extras don't break import — just abstain-at-runtime per D4.

### D8: Kronos NEVER trains, only infers

Even when v0.4+ adds RL aggregator training, the analysts (Kronos especially) STAY FROZEN. Charter §"What works": *"PPO or recurrent SAC for the aggregator, with the analyst pool frozen most of the time. Re-train analysts on a slow cycle (monthly/quarterly) on actual realized data."*

Kronos retraining is months-quarters work, requiring its own dataset+compute. v0.3 ships frozen-only.

## Consequences

### Positive
- Charter's MVP recipe completes (3-of-3 analysts shipped)
- Foundation-model overconfidence bounded at the leaf via D3 clipping
- Kairos failure mode (A-shares neg-IC) is structurally prevented; BMA calibrator learns the regime-specific weight
- Lazy-load means installs without HF connectivity still work
- One-line config to swap model size

### Negative
- Adds torch dependency (~500MB) for operators who opt in
- 3-10s CPU latency per call eats into the 15-min cadence budget on big watchlists; mitigated by per-symbol parallel fetching in autonomous.tick (deferred to v0.4 if it bites)
- Distributional via subclass is brittle — Kronos upstream changes their predict() pre-processing and our subclass breaks. Mitigated by pinning `kronos==<sha>` in the optional-extra and shipping a compat shim
- v0.3 has no Kronos calibrator yet — the calibrator landing in V03-5 will start from no priors and shrink confidence aggressively for first ~200 fills (cold-start). This is correct behavior, not a bug; documented in v0.3 release notes

## Cross-references
- ADR-0002 §"Analyst Protocol" — the contract Kronos honors
- ADR-0003 §"abstain handling" — formalized to filter `confidence < 0.10` in `aggregate()`
- ADR-0010 §"calibrator-from-fills" — Kronos calibrator becomes a per-analyst BMA weight
- ADR-0012 §"LLMAnalyst" — separate; Kronos is foundation-model OHLCV, not chat
- Charter §"Layer 1: Analyst Pool" + §"Things I want to flag — AAAI 2026 acceptance ≠ alpha"

## Provenance
- Charter §"What Kronos actually is (so we don't over-index on it)" — verbatim
- User directive 2026-05-13: "proceed with the rest of the work and use the deep work loop"
- Research: docs/research/05-kronos-integration.md (288 lines, deepwiki-grounded)
