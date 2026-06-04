# ADR-0019: `evaluation/` module promotion (CV + lookahead + DSR)

**Status**: Accepted (2026-05-13), implemented
**Date**: 2026-05-13
**Target**: v0.3.0
**Cross-cuts**: ADR-0006 (RL deferred + lookahead invariant), AGENTS.md "No look-ahead bias", founding charter §"What works — walk-forward training with embargo"

---

## Context

`AGENTS.md` documents `hermes_quant/evaluation/` as part of the target tree:
```
hermes_quant/
├── evaluation/
│   ├── cv.py                 # PurgedWalkForward
│   ├── lookahead.py          # shuffle_timestamps_test
│   └── dsr.py                # Deflated Sharpe (v0.2 placeholder)
```

It hasn't been built yet. v0.1.2 shipped `tests/test_no_lookahead.py` (5 tests, CI release-blocker per ADR-0006) — those tests use an inline `_polluted_but_sliced_pattern` helper. v0.3 promotes that pattern into the canonical `evaluation/` module so:

1. Future analysts can use `evaluation.lookahead.shuffle_timestamps_test()` without copy-pasting test scaffolding
2. The walk-forward CV scaffolding lands BEFORE the v0.4 RL aggregator needs it
3. Deflated Sharpe Ratio (DSR) ships as a placeholder so paper-book Sharpe (V03-5 P&L attribution) is hedged against multiple-comparisons bias

This is scaffolding work — none of these modules ship behavioral changes for the v0.3 paper-book MVP, but they're the contract surfaces v0.4 RL training needs to plug into.

## Decision

### D1: Three modules, one package

```
hermes_quant/evaluation/
├── __init__.py    # public API re-exports
├── cv.py          # PurgedWalkForward (López de Prado)
├── lookahead.py   # shuffle_timestamps_test (CI gate)
└── dsr.py         # DeflatedSharpe (Bailey & López de Prado 2014 placeholder)
```

### D2: `cv.PurgedWalkForward` — train/val/test windows with embargo

Charter §"What works": *"Train aggregator on `[t-N, t-K]`, validate on `[t-K, t-K/2]`, test on `[t-K/2, t]`. Slide forward."*

Plus embargo: a buffer between train and val that drops the M most recent train samples to prevent leakage when features have lag (per López de Prado, "Advances in Financial Machine Learning"). For v0.3 we ship the API; v0.4 RL training uses it.

```python
@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    val_start: pd.Timestamp
    val_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

class PurgedWalkForward:
    def __init__(self, *, n_splits: int = 5, embargo_pct: float = 0.01,
                 train_pct: float = 0.6, val_pct: float = 0.2):
        ...
    def split(self, df: pd.DataFrame) -> Iterator[WalkForwardSplit]:
        ...
```

v0.3 ships the class with full splits + an `embargo_pct` parameter that drops the trailing fraction of each train window. v0.3 has 1 unit test + 1 lookahead test that fails if val_start < train_end + embargo.

### D3: `lookahead.shuffle_timestamps_test` — promote from inline test

The existing inline pattern in `tests/test_no_lookahead.py` becomes `evaluation.lookahead.shuffle_timestamps_test(analyst_or_aggregator, bars, *, n_shuffles=10, alpha=0.05)`. Returns:

```python
@dataclass(frozen=True)
class LookaheadTestResult:
    p_value: float                     # null: shuffled performance == real
    real_score: float                  # baseline Sharpe / IC / hit rate
    shuffled_scores: list[float]       # distribution under H0
    passed: bool                       # p_value > alpha (no leakage detected)
```

CI gate at `tests/test_no_lookahead.py` becomes:
```python
def test_classical_ta_no_lookahead():
    result = shuffle_timestamps_test(ClassicalTAAnalyst(), bars=fixture_bars())
    assert result.passed, f"lookahead detected: p={result.p_value}"
```

This is the API the charter describes; tests don't change, just the import.

### D4: `dsr.DeflatedSharpe` — placeholder for v0.4

Bailey & López de Prado's Deflated Sharpe Ratio adjusts an observed Sharpe for the multiple-comparisons + non-normality bias inherent to backtest-mined strategies. v0.3 ships the formula stub; v0.4 RL training wires it.

```python
def deflated_sharpe(
    observed_sharpe: float,
    n_trials: int,
    *,
    n_observations: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """
    Bailey & López de Prado 2014. Returns the probability that the observed
    Sharpe is not a false discovery, given:
      n_trials: how many strategies were searched
      n_observations: window length
      skew, kurtosis: of the return distribution

    For v0.3 paper-book Sharpe reporting, n_trials=1 (we're not searching),
    so DSR ≈ probabilistic-Sharpe-ratio-of-observed-Sharpe.
    """
    ...
```

The function works for `n_trials=1` (single-strategy paper book — what v0.3 actually does). Multi-strategy multiple-comparisons usage is a v0.4 hook.

### D5: `__init__.py` re-exports

```python
from .cv import PurgedWalkForward, WalkForwardSplit
from .lookahead import LookaheadTestResult, shuffle_timestamps_test
from .dsr import deflated_sharpe

__all__ = [
    "PurgedWalkForward", "WalkForwardSplit",
    "LookaheadTestResult", "shuffle_timestamps_test",
    "deflated_sharpe",
]
```

## Consequences

### Positive
- The lookahead CI gate becomes a one-line import for new analysts
- v0.4 RL aggregator can land cleanly with `from hermes_quant.evaluation import PurgedWalkForward, deflated_sharpe`
- Charter clause "Walk-forward + embargo + log-return-after-costs reward + multi-regime" gets its scaffolding
- DSR placeholder hedges paper-book Sharpe reporting against the false-discovery problem

### Negative
- v0.3 ships these as scaffolding; the practical use is v0.4. Risk: the API drifts before it has a real consumer. Mitigated by writing the API to match López de Prado's canonical formulations exactly — the consumer (RL training) will conform to the standard, not to a custom shape
- DSR with `n_trials=1` is informationally weak (it just becomes the probabilistic Sharpe ratio); honest documentation says so

## Cross-references
- ADR-0006 §"lookahead invariant" — formalized via `shuffle_timestamps_test`
- AGENTS.md §"No look-ahead bias" — implementation lives here now
- Charter §"What works" — walk-forward + embargo
- López de Prado (2018) "Advances in Financial Machine Learning" — canonical reference for PurgedWalkForward
- Bailey & López de Prado (2014) "The Deflated Sharpe Ratio" — canonical reference for `deflated_sharpe`

## Provenance
- Audit `docs/audits/2026-05-13-charter-vs-shipped-v020.md` §"V03-3"
- AGENTS.md target tree (existed since project bootstrap)
