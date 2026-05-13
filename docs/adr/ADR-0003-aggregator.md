# ADR-0003: Aggregator design — Bayesian baseline + logistic stacking, RL deferred

**Status**: proposed
**Date**: 2026-05-12

## Context

The aggregator combines N `AnalystView`s into a single `AggregatedSignal` that the risk gate consumes. Three approaches were evaluated (`docs/research/03-plugin-architecture.md` §4, `01-rl-for-trading.md` §3):

- **Equal-weight ensemble**: trivial but ignores analyst skill.
- **Bayesian model averaging**: weights track recent forecast accuracy; mathematically clean.
- **Logistic stacking**: simplest "real" learning approach; `LogisticRegression` on a rolling window of analyst views as features.
- **RL aggregator**: PPO/SAC on the analyst-view feature vector. Higher ceiling, much higher complexity. Per `01-rl-for-trading.md` §1 & §4, the realistic out-of-sample improvement over a Bayesian baseline is 0.1-0.3 Sharpe points after rigorous purged walk-forward — modest. The RL slot is reserved but not implemented in v0.1.

## Decision

v0.1 ships **two** aggregators side-by-side, user-selectable via config:

### (A) Bayesian model averaging (`bma`, default)

```python
def bma_aggregate(views: list[AnalystView],
                  weights: dict[str, float],          # rolling-accuracy weights
                  recent_calibration: dict[str, float]) -> AggregatedSignal:
    if not views:
        return _flat_signal()
    eff = [(v, weights.get(v.analyst, 1.0) * v.confidence) for v in views]
    total_weight = sum(w for _, w in eff)
    if total_weight < 1e-6:
        return _flat_signal()
    direction_score = sum(v.direction * w for v, w in eff) / total_weight
    magnitude = sum(v.magnitude * w for v, w in eff) / total_weight
    direction = int(np.sign(direction_score)) if abs(direction_score) > 0.1 else 0

    # Disagreement penalty (PDR silence-by-default)
    direction_signs = [v.direction * recent_calibration.get(v.analyst, 1.0) for v in views]
    disagreement = float(np.var(direction_signs))
    confidence = float(abs(direction_score)) * max(0.0, 1.0 - 2.0 * disagreement)

    return AggregatedSignal(
        direction=direction, magnitude=magnitude, confidence=confidence,
        horizon=_dominant_horizon(views), components=tuple(views))
```

Analyst weights `weights` come from a rolling 30-day window of realized accuracy:
`w_a = max(0.1, recent_accuracy_a - 0.5) * 2` — analysts above chance get scaled weight, analysts at-or-below chance get a 0.1 floor (not zero — preserves some signal during regime transitions).

### (B) Logistic stacking (`stacking`)

```python
def stacking_aggregate(views: list[AnalystView],
                       fitted_model: sklearn.linear_model.LogisticRegression,
                       horizon_buckets: list[float]) -> AggregatedSignal:
    if not views or fitted_model is None:
        return _flat_signal()
    feat = _features_from_views(views)   # flat vector of (direction, magnitude, confidence) per analyst
    proba = fitted_model.predict_proba([feat])[0]
    direction_idx = int(np.argmax(proba))
    direction = [-1, 0, +1][direction_idx]
    confidence = float(proba[direction_idx])
    magnitude = _magnitude_from_horizon_buckets(proba, horizon_buckets)
    return AggregatedSignal(direction=direction, magnitude=magnitude,
                            confidence=confidence, horizon=_dominant_horizon(views),
                            components=tuple(views))
```

Stacking is fitted on a rolling window of (analyst views at time t, realized direction at t+horizon). The daemon refits weekly; the fitted model is checkpointed to `~/.hermes/quant/models/stacking-<asset>-<timeframe>.pkl` so daemon restarts don't lose state.

### Disagreement-aware position sizing

Both aggregators emit `confidence` that is multiplicatively reduced by analyst disagreement. This is the "silence by default" prior from Eidolon's PDR architecture — when analysts disagree, the system defaults toward flat. The risk gate (ADR-0004) further uses confidence to size the position (Kelly-fractional).

### RL aggregator slot (deferred to v0.2)

The aggregator interface is:

```python
class Aggregator(Protocol):
    name: str
    def aggregate(self, views: list[AnalystView]) -> AggregatedSignal: ...
    def update(self, outcomes: list[RealizedOutcome]) -> None: ...
```

The v0.2 RL aggregator will implement this same interface, replacing `bma`/`stacking`. v0.1 lays the groundwork (entry-points discovery, settlement loop calling `update`) so adding the RL aggregator is wiring, not a refactor.

### Success criterion (per `01-rl-for-trading.md` §4)

A new aggregator (RL or otherwise) is "better" than the BMA baseline iff:
- DeFlated Sharpe Ratio test (Bailey & López de Prado 2014) p < 0.05 over ≥12 walk-forward folds
- Survives the **shuffle-timestamp test** (random permutation of analyst-view timestamps drops performance to ~chance — confirms no look-ahead leak)
- Matches BMA's max-drawdown to within 25%

Anything else is overfitting.

## Consequences

### Positive

- Two baselines means we can compare them out-of-sample on day 1; v0.1 ships with empirical evidence rather than a single guess.
- Disagreement-aware sizing prevents over-trading on noisy ensembles (the most common failure mode for naive equal-weight stacks).
- RL slot is reserved but the v0.1 surface area is small enough to test thoroughly.

### Negative

- BMA's weight-update path is sensitive to short rolling windows. With 30 days of 1h-tick data on BTC (≈ 720 bars) per analyst, weights are noisy. Mitigated by the 0.1 floor and by averaging weights across asset+timeframe pairs.
- Stacking requires sklearn — pulled in via the `[stacking]` extra. Acceptable; sklearn is small and well-known.
- Both aggregators discard non-numeric metadata. Future analysts that emit regime-class or options-Greek metadata will not influence the aggregated signal until v0.2.

## Implementation notes

- Aggregators live in `hermes_quant/aggregators/`. Module name = entry-point name.
- Per-asset, per-timeframe state. The daemon keeps `dict[(asset, timeframe), Aggregator]`.
- ECE (expected calibration error) is computed in `quant_doctor`. Threshold: warn at ECE > 0.10, error at ECE > 0.15.
- The settlement loop (`docs/research/03-plugin-architecture.md` §1) runs on a separate thread, fetches realized outcomes for views whose horizons have expired, calls `aggregator.update()` and `analyst.update()`, persists to `realized_outcomes` SQLite table.

## References

- `docs/research/01-rl-for-trading.md` §1, §3, §4 — aggregator landscape, ensemble vs end-to-end RL, success criteria
- `docs/research/03-plugin-architecture.md` §4 — aggregator design space
- Bailey & López de Prado 2014, "The Deflated Sharpe Ratio"
