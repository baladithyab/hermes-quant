# ADR-0003: Aggregator design — Bayesian baseline + logistic stacking, RL deferred

**Status**: Accepted (2026-05-12), implemented
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

---

## Amendment 2026-05-13: Calibration-quality lifecycle (Phase-8 P0-A.3 + v0.1.2 transition)

### Context

Phase-8 cross-family review (synthesis 2026-05-13 §P0-A) caught a soundness defect in the v0.1.0 settlement path: the per-fill formula `(fill_price - decision_price) / decision_price` was being stored on `RealizedOutcome.realized_return` and fed into `analyst.update()` / `aggregator.update()`. That value is **per-fill slippage**, not the directional return over `signal.horizon`. Three reviewers independently flagged that running the daemon as-shipped would zero out `direction_correct` for nearly every fill (slippage is dominated by microstructure noise, not the multi-bar drift the signal is forecasting), corrupting every Beta posterior in the analyst stack and the BMA weights in the aggregator. The honest fix is to refuse posterior updates until we have the entry+exit pair joined.

This amendment pins the two-step lifecycle: (1) what shipped in v0.1.1 to gate the bad path off, (2) what lands in v0.1.2 to turn the gate green.

### What changed in v0.1.1 (shipped)

The `realized_return` field on `RealizedOutcome` and `EpisodeOutcome.realized_returns[horizon]` is now **polymorphic**. It carries either per-fill slippage *or* true horizon return; consumers MUST gate on a quality tag before treating the value as directional.

The tag lives on `view.metadata['_calibration_quality']` (per-analyst outcomes) and on `AggregatedSignal.metadata['_calibration_quality']` (episode outcomes). Two values are defined in `hermes_quant/daemon/settlement_loop.py:76-77` as the source of truth:

```python
CALIBRATION_QUALITY_SLIPPAGE_ONLY = "slippage_only"     # v0.1.1: single-fill slippage
CALIBRATION_QUALITY_HORIZON_RETURN = "horizon_return"   # v0.1.2+: true horizon return
```

`construct_realized_outcomes` (`settlement_loop.py:111-194`) and `construct_episode_outcomes` (`settlement_loop.py:197-288`) tag every outcome they emit as `slippage_only`. `dispatch_settlement` (`settlement_loop.py:291+`) reads the tag and SKIPS `analyst.update()` and `aggregator.update()` when it equals `slippage_only`, incrementing `stats["n_skipped_slippage_only"]`. The `RollingSlippageEstimator` is unaffected — it explicitly wants per-fill adverse-bps, so it consumes the same records by a different code path.

Net effect: v0.1.1 settles fills, logs them, refines the slippage estimator, but does NOT mutate Beta posteriors or BMA weights. Calibration state is frozen at prior until v0.1.2.

### What changes in v0.1.2 (planned)

Entry+exit fill joining lands in the settlement loop, and tags flip from `slippage_only` to `horizon_return`. The transition is:

1. **Entry markers** — `tick_loop` persists an `entry_record` on the signal bus (`entry_signal_id`, `decision_price`, `asof`, `direction`) whenever an `Action` with non-zero `target_position_pct` is emitted. Per ADR-0011 the marker chains via `exec_id`.
2. **Join** — `settlement_loop` joins ENTRY exec records (side aligns with `signal.direction`) with EXIT exec records (opposite side closing the position) through the `portfolio_loader` exec_id chain. This piggybacks on the new portfolio loader's case (c) full-close handling (ADR-0011).
3. **Compute** — `horizon_return = (exit_close_price − decision_price) / decision_price * sign(direction)`. Time elapsed `exit.asof − entry.asof` MUST be `>= signal.horizon`; positions closed early do not count as horizon outcomes.
4. **Tag** — the resulting `RealizedOutcome` and its `AggregatedSignal` carry `_calibration_quality = "horizon_return"`. `dispatch_settlement` then dispatches to `analyst.update()` + `aggregator.update()` normally; BMA posteriors begin evolving.
5. **Alpha** — `alpha_return = horizon_return − benchmark_return_over_same_window` (BTC for crypto, SPY for equities; per TradingAgents-comparison round-2 pattern #9a). Stored alongside `horizon_return` on the outcome; the calibrator uses `alpha_return` for direction-correct, not raw return.
6. **Drop slippage tag where possible** — `construct_realized_outcomes` removes the `slippage_only` tag for any case where the exit-fill join succeeded. **Orphan fills** (entry with no matching exit yet, or exit that cannot be matched to an entry) STILL get `slippage_only` and STILL get skipped by `dispatch_settlement`. Orphans are not a bug — they reflect open positions and partial-fill races and are expected.

**Partial-exit attribution.** When an exit closes only part of a position, attribution uses **average-cost basis**, matching ADR-0011 and freqtrade's default. FIFO lot accounting is deferred to v0.2+ and explicitly out of scope for this amendment; revisit when tax-lot reporting becomes a deliverable.

### Schema diff

```
RealizedOutcome:
  realized_return: float
-   # v0.1.0: assumed to be horizon return; was actually per-fill slippage
+   # v0.1.1+: POLYMORPHIC — slippage OR horizon return
+   # gate on view.metadata['_calibration_quality'] before use
  view: AnalystView
+   metadata['_calibration_quality']: 'slippage_only' | 'horizon_return'

EpisodeOutcome.aggregated_signal: AggregatedSignal
+   metadata['_calibration_quality']: 'slippage_only' | 'horizon_return'

# v0.1.2 only:
RealizedOutcome:
+   alpha_return: float | None        # horizon_return − benchmark, see step 5
+   benchmark_symbol: str | None      # 'BTC-USD' | 'SPY' | None for slippage_only
```

The field name `realized_return` is preserved for wire compatibility with v0.1.0 signal logs; renaming was considered and rejected (breaks replay). The polymorphism is loadbearing.

### Test fence

- `test_no_slippage_only_outcomes_reach_analyst_update` — assertion (CI-blocking): `analyst.update` is never called with `view.metadata['_calibration_quality'] == 'slippage_only'`. Symmetric assertion for `aggregator.update`. Already in v0.1.1.
- `test_horizon_return_outcomes_dispatch` — positive-path coverage that `horizon_return`-tagged outcomes DO reach `update()`. Already in v0.1.1 (synthetic outcomes).
- `test_v0_1_2_entry_exit_join_produces_horizon_return` — NEW in v0.1.2: feed paired entry+exit exec records, assert the produced outcome is tagged `horizon_return` with the correct sign and magnitude.
- `test_orphan_exit_falls_back_to_slippage_only` — NEW in v0.1.2 (defensive): exit with no joinable entry → outcome stays `slippage_only` and stays skipped.

### Cross-cuts

- **ADR-0009 §P1-10 (EpisodeOutcome)**: the schema is now polymorphic via `aggregated_signal.metadata['_calibration_quality']`. Cross-link this amendment from §P1-10.
- **ADR-0011 (portfolio reconstruction)**: the v0.1.2 exit-fill join is not a parallel implementation — it consumes case (c) full-close and case (b) partial-close from the portfolio loader. Average-cost basis is shared between this ADR and ADR-0011; do not diverge.
- **ADR-0010 (settlement journal)**: Phase B journal entries resolve with `horizon_return` once v0.1.2 lands. Until then, Phase B records the `slippage_only` tag and stays in lockstep with the calibrator (no posterior update, no journal "settled" transition for calibration purposes).
- **ADR-0006 (RL deferred)**: graduation criteria require `horizon_return`-quality outcomes. v0.1.1 `slippage_only` data does NOT count toward the ≥12 walk-forward folds or the DSR p<0.05 gate. The graduation clock effectively starts on the v0.1.2 release.
