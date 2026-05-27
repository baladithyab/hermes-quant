# ADR-0047: Regime-Aware BMA Weights (Wave 7)

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** Hermes-Quant Subagent (Wave 7)  
**Reference:** Mantshimuli & Mwamba, "Hidden Markov Bayesian Model Averaging for Financial Returns", Springer 2026

---

## Context

BMA aggregation (ADR-0003, ADR-0036) treats analyst weights as stationary: each analyst's
weight is derived solely from its Beta-binomial posterior accuracy.  In practice, analyst
value-add is regime-conditional — sentiment signals are informative in trending bull markets
but noisy in bear reversals; classical TA is most actionable during volatile breakout/breakdown
episodes.  Mantshimuli & Mwamba (2026) formalise this as a Hidden Markov BMA (HM-BMA) where
analyst priors are conditioned on the latent regime state.

Wave 7 ships the v0.1 rule-based approximation as a production-safe foundation.  The
Mantshimuli HMM (v0.2) requires a calibrated transition-matrix fit which is deferred until
enough live data exists (~6 months of daily decisions, ≥250 episodes per regime state).

---

## Decision

### 1. Deterministic rule-based classifier (v0.1)

A `RegimeDetector` classifies each market context into one of four states using three
observable inputs from `StateVariables`:

| Condition | Regime |
|---|---|
| `realized_vol_percentile > 0.7` | `VOLATILE` |
| `trend_strength <= -0.5` AND `realized_vol_percentile <= 0.7` | `BEAR` |
| `trend_strength >= +0.5` AND `realized_vol_percentile <= 0.6` | `BULL` |
| None of the above | `UNKNOWN` |

**Priority**: VOLATILE > BEAR > BULL > UNKNOWN.  Volatility dominates trend.

**Rationale for thresholds**: 70th-percentile realized-vol is the conventional boundary
between "normal" and "stress" regimes in the empirical market microstructure literature
(e.g., Ang & Timmermann 2012).  The ±0.5 trend-strength z-score is a ½-sigma threshold
— modest enough to fire on genuine trends while ignoring noise.

### 2. State variables

`compute_state_variables(bars)` computes:
- `realized_vol_60d`: annualized `stdev(log_returns) × sqrt(252)` over trailing 60 days.
- `realized_vol_percentile`: empirical CDF of current vol vs the trailing 252-day window.
- `yield_curve_slope`: 10y minus 2y from `~/.hermes/quant/cache/yield-curve-cache.json` (optional).
- `trend_strength`: `(close − 50d_MA) / 50d_stdev` (optional; None when `n_bars < 50`).

Yield-curve slope is **optional** — when the cache is absent, `yield_curve_slope=None` and
`metadata['yield_curve_unavailable'] = True`.  The detector handles `None` gracefully
(slope does not participate in the v0.1 rules; reserved for v0.2 HMM features).

### 3. Per-regime weight multipliers

`apply_regime_weights(base_weights, regime)` multiplies each analyst's base weight by the
regime row from `DEFAULT_REGIME_WEIGHTS`:

| Analyst | BULL | BEAR | VOLATILE | UNKNOWN |
|---|---|---|---|---|
| semantic | 1.0 | 1.0 | 0.7 | 1.0 |
| sentiment | 1.2 | 0.6 | 0.4 | 1.0 |
| classical_ta | 0.9 | 1.3 | 1.5 | 1.0 |
| fundamentals | 1.1 | 1.1 | 0.8 | 1.0 |
| kronos | 1.0 | 1.0 | 1.2 | 1.0 |

These priors are taken from Mantshimuli & Mwamba (2026), Table 3, §5.4 "Regime-conditional
priors", with minor adjustment for the hermes-quant analyst taxonomy.

### 4. Weight-multiplier invariant

> **Regime multipliers NEVER change the sign of or zero out a weight.**

Multipliers are strictly positive floats.  The floor is `1e-6` (enforced in
`apply_regime_weights`).  This preserves the identity and directional contribution of every
analyst signal — regime awareness is modulation, not exclusion.  Exclusion is the
responsibility of the IC dedup gate (Wave 6b / ADR implied by Wave 6), not the regime module.

### 5. Integration with the IC dedup gate

Regime adjustment applies **AFTER** IC dedup and **BEFORE** the vote-share calculation:

```
views
  → abstain filter (ADR-0018 §D4)
  → IC dedup gate (Wave 6b)     ← excludes near-duplicate analysts
  → regime weight adjustment    ← modulates surviving analyst weights
  → BMA vote-share aggregation
```

This ordering guarantees:
- The same analysts always survive both the IC and regime passes.
- Regime multipliers apply to the post-dedup weight, not the raw prior.
- `ic_dedup_excluded_analysts` in metadata reflects IC decisions independently.

### 6. Audit log fields

`AggregatedSignal.metadata` gains two keys when `regime_detector` is set:
- `"regime_state"`: string value of `RegimeState` (e.g. `"bear"`), or `None` if detector not set.
- `"regime_weight_multipliers"`: dict of `{analyst: multiplier_float}`, or `None` if not set.

Both fields are `None` (not absent) when `regime_detector=None`, preserving the audit-log
schema for consumers that read these keys.

### 7. Default OFF — bit-identical baseline

`BMAAggregator(regime_detector=None)` (the default) is **bit-identical** to the pre-Wave-7
aggregator.  The regime block is entirely skipped; no imports are loaded at import time
(lazy import pattern mirrors the IC dedup gate).

### 8. HMM v0.2 deferred plan

`RegimeDetector` accepts an optional `hmm_classifier: Callable[[StateVariables], RegimeState]`
parameter.  When provided it overrides the rule-based result.  The HMM will be wired here in
v0.2 without a constructor signature change or any change to the BMA integration point.

Trigger criteria for HMM fit:
- ≥ 250 classified episodes per non-UNKNOWN regime state in the decision log.
- Walk-forward out-of-sample IC of HMM regime labels vs realised direction accuracy ≥ 0.55.

---

## Consequences

**Positive:**
- Analyst signal quality improves in trending and volatile environments without changing the
  analyst code or the risk gate.
- Fully auditable: regime state and weight multipliers are written to every signal's metadata.
- Zero cost when unused (`regime_detector=None`).

**Negative / Risks:**
- The v0.1 rule thresholds are heuristic.  They may misclassify short-duration regime shifts
  (e.g. gap-down on news that reverts within 5 days).
- Yield-curve slope is currently optional, which means equity/macro regime signals are
  partially blind when the cache is not populated.
- Regime classification is point-in-time; no smoothing/hysteresis applied in v0.1.  Rapid
  BULL→BEAR→BULL oscillation on noise is possible.  Hysteresis is planned for v0.2.

**Mitigations:**
- `UNKNOWN` is the safe fallback (all multipliers = 1.0 = no change from baseline).
- Exceptions in regime classification are caught and logged at WARNING; the aggregator
  silently falls back to the baseline (no adjustment, no crash).
- All regime tests assert UNKNOWN gives bit-identical output.

---

## Alternatives Considered

1. **Directly wiring the Mantshimuli HMM**: Deferred.  Requires a transition-matrix fit on
   ≥1,000 historical regime observations.  Deploying an unfitted HMM produces worse results
   than the rule-based baseline.
2. **Using only vol percentile (no trend)**:  Simpler but cannot distinguish BULL from BEAR
   in low-vol environments.  Both trend + vol are needed for the three-way classification.
3. **Separate regime-aware aggregator class**: Rejected.  The optional-hook pattern (same
   class, `regime_detector=None` default) is consistent with the IC dedup pattern and avoids
   a proliferation of aggregator subclasses.
