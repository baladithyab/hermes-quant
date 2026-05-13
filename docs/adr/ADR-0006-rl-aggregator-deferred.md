# ADR-0006: RL aggregator deferred to v0.2 with concrete success criterion

**Status**: proposed
**Date**: 2026-05-12

## Context

The user's original ask included "let our algorithms auto-evolve through RL." This ADR records why that's deferred and what the v0.2 graduation criteria are.

Per `docs/research/01-rl-for-trading.md` §1, the realistic out-of-sample improvement of an RL aggregator over a Bayesian baseline is 0.1-0.3 Sharpe points after rigorous purged walk-forward — modest. The ceiling is real but requires:

1. ≥30 days of paper-trade data to build a training corpus
2. Walk-forward purged cross-validation discipline (López de Prado)
3. Explicit DeFlated Sharpe Ratio test for statistical significance
4. Anti-look-ahead-bias verification (shuffle-timestamp test)
5. Anti-leverage-hacking action space (already enforced by ADR-0004's discrete steps)
6. Compute budget: 500-2000 GPU-hours for hyperparameter sweep + walk-forward retraining

None of these prerequisites exist on day zero. Shipping an RL aggregator in v0.1 means shipping an untested RL aggregator on a ~30-bar paper-trade dataset, which is exactly the failure mode `01-rl-for-trading.md` §2 warns against (overfitting backtest noise; performance collapses on regime change).

## Decision

v0.1 ships TWO classical aggregators (Bayesian, Stacking — per ADR-0003) and reserves the RL aggregator slot via the `Aggregator` Protocol. The RL aggregator is implemented in v0.2 once the v0.2 graduation criteria are met.

### v0.2 RL aggregator design (architectural sketch, not implementation)

```python
# hermes_quant/aggregators/rl_aggregator.py (v0.2)
class RLAggregator(Aggregator):
    name = "rl"

    def __init__(self, model_path: Path, action_space: ActionSpace, config: RLConfig):
        self._policy = self._load_policy(model_path)
        self._action_space = action_space
        # action space MATCHES ADR-0004's discrete steps: {-0.20, -0.15, ..., 0, ..., 0.20}

    def aggregate(self, views: list[AnalystView]) -> AggregatedSignal:
        feat = _features_from_views(views)
        action_logits = self._policy(feat)
        # The RL output is a position size; the gate still rules on whether to emit
        action_idx = np.argmax(action_logits)
        target_size = self._action_space.idx_to_size(action_idx)
        return _signal_from_action(target_size, views)

    def update(self, outcomes: list[RealizedOutcome]) -> None:
        # offline buffer; periodic retraining handled by the trainer (separate process)
        self._buffer.extend(outcomes)
```

Library: **stable-baselines3 PPO** (per `01-rl-for-trading.md` §4 starter recommendation). Policy is a small MLP (2 layers, 64 units). Discrete action head matches ADR-0004's step grid.

Reward shape:
```
r_t = log(V_{t+1}/V_t) - 0.001 * |action_t - action_{t-1}|
```
Clipped to [-1, +1]. Discount γ=0.99 for 1-min, 0.999 for 1-hour.

Training infrastructure: separate process (`hermes-quant-trainer`) running on the eidolon cluster (Yggdrasil Muninn 2x3050, or Huginn 4xV100 in shared mode for hyperparameter sweep). Reads from `~/.hermes/quant/realized_outcomes` SQLite, writes checkpoints to `~/.hermes/quant/models/rl-<asset>-<timeframe>/`.

### v0.2 graduation criteria (must hit all six to ship the RL aggregator)

1. **≥90 days of v0.1 paper-trade telemetry** with at least one stable analyst pool
2. **Bayesian baseline produces an interpretable Sharpe** ≥ 0.5 net of costs on the paper-trade horizon (otherwise we're tuning RL on top of a broken baseline)
3. **Walk-forward purged cross-validation** is implemented and tested (`hermes_quant.evaluation.cv` module)
4. **DeFlated Sharpe Ratio test** is implemented (`hermes_quant.evaluation.dsr` module)
5. **Shuffle-timestamp test** is implemented as a CI gate that runs against any new aggregator (`tests/test_no_lookahead.py`)
6. **The RL aggregator's DSR p-value < 0.05 vs the Bayesian baseline** on ≥12 walk-forward folds, and matches BMA's max-drawdown to within 25%

Without all six, the RL aggregator stays in `experiments/` and is not exposed to user-facing config. We do NOT ship "experimental RL" in production paths — the failure modes in `01-rl-for-trading.md` §2 are too catastrophic.

### What v0.1 DOES land for the v0.2 path

- The `Aggregator` Protocol with `update(outcomes)` already in place.
- The settlement loop already feeds realized outcomes back to aggregators and analysts.
- The ADR-0004 risk gate already enforces the discrete action space the RL aggregator will produce.
- A `hermes_quant.evaluation` module with stubs for walk-forward CV, DSR, and shuffle-timestamp tests. v0.1 implements walk-forward CV (used for stacking weights) and shuffle-timestamp (used as a CI gate on shipped aggregators); DSR is v0.2.

This means v0.2 is wiring + training, not refactoring. The hard interface decisions are already locked.

## Consequences

### Positive

- v0.1 ships fast and trustworthy. No hand-wavy RL claims.
- The graduation criteria are objective and falsifiable. No fudging "well, it kinda outperformed."
- The interface (`Aggregator` Protocol) is locked in v0.1, so v0.2 has no migration tax.
- Walk-forward CV and shuffle-timestamp tests land in v0.1 — these benefit BMA and stacking too, not just the future RL aggregator.

### Negative

- Users who came for "RL auto-evolution" must wait. Mitigated by the ROADMAP being explicit about the timeline and criteria.
- The graduation criteria are stringent. It's possible the Bayesian baseline never reaches Sharpe 0.5, in which case the RL aggregator is never useful (because there's no edge to amplify). That's a feature, not a bug.
- 90 days of paper-trade is a long time. Mitigated by the user being able to run the v0.1 daemon continuously starting day one.

## Implementation notes

- v0.1 ships `hermes_quant.evaluation.cv.PurgedWalkForward` — used by the stacking aggregator's rolling refit, will be reused by RL training.
- v0.1 ships `hermes_quant.evaluation.lookahead.shuffle_timestamps_test()` — runs in CI against every aggregator and analyst, fails the build if a shuffled-timestamp run produces > chance accuracy.
- v0.2 adds `hermes_quant.evaluation.dsr.deflated_sharpe_ratio()` and the trainer process.
- The trainer process runs ON THE CLUSTER (per the `eidolon-cluster-ops` skill), not on the daemon's host. Checkpoint sync via `~/.hermes/quant/models/` (rsync from cluster to daemon-host before promoting).

## References

- `docs/research/01-rl-for-trading.md` §1, §2, §4, §5 — full landscape and starter recipe
- ADR-0003 — aggregator interface (the slot RL fills)
- ADR-0004 — risk gate (the discrete action space RL emits into)
- López de Prado 2018, "Advances in Financial Machine Learning" — purged walk-forward
- Bailey & López de Prado 2014 — DeFlated Sharpe Ratio test
- AI4Finance-Foundation/FinRL — reference implementation patterns
