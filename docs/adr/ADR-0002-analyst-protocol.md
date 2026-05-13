# ADR-0002: Analyst protocol contract

**Status**: proposed
**Date**: 2026-05-12

## Context

The analyst pool is the part of hermes-quant the user said they want to "keep adding to." The protocol that governs how analysts emit views must be:

- **Heterogeneous**: a Kronos forecaster, a classical TA scanner, an order-book microstructure analyzer, and an LLM-based news analyzer all need to fit the same shape so the aggregator can combine them.
- **Versioned**: future analysts will need richer outputs (regime classification, multi-horizon forecasts, options Greeks). The protocol must evolve without breaking existing analysts.
- **Calibrated**: an analyst's confidence score must mean something — when it says 0.8, that should track an ~80% directional hit rate.
- **Lazy**: heavy analysts (Kronos) load weights on demand; never at plugin import.
- **Sync, with thread-pool dispatch**: tick frequencies are 1m+, internal LLM calls can use sync `delegate_task`, the aggregator runs analysts in `concurrent.futures.ThreadPoolExecutor`. Async migration deferred to v0.2 if sub-minute ticks become a thing.

## Decision

The protocol consists of two frozen dataclasses (`MarketContext`, `AnalystView`) and one runtime-checkable Protocol (`Analyst`).

```python
from typing import Protocol, Literal, runtime_checkable
from dataclasses import dataclass
import pandas as pd

@dataclass(frozen=True)
class MarketContext:
    asset: str                       # "BTC/USDT", "AAPL", etc.
    timeframe: str                   # "1m", "5m", "15m", "1h", "4h", "1d"
    bars: pd.DataFrame               # OHLCV with 'timestamp' column, last row = current
    last_close: float
    last_volume: float
    asof: pd.Timestamp               # decision timestamp (UTC)
    extras: dict                     # provider-specific extras (orderbook, news headlines)

@dataclass(frozen=True)
class AnalystView:
    analyst: str                     # name of the emitting analyst
    direction: Literal[-1, 0, +1]    # -1 short, 0 flat, +1 long
    magnitude: float                 # expected return as fraction (e.g. 0.012 = 1.2%)
    confidence: float                # in [0, 1]
    horizon: str                     # "5m" / "1h" / "1d" — over what window the view holds
    rationale: str | None = None     # optional human-readable explanation
    metadata: dict | None = None     # provider-specific extras (CIs, sub-scores, ...)

@runtime_checkable
class Analyst(Protocol):
    name: str
    timeframes: list[str]            # which timeframes this analyst supports
    asset_classes: list[str]         # "crypto", "equity", "option", "fx"
    enabled: bool                    # config-controlled

    def analyze(self, ctx: MarketContext) -> AnalystView | None: ...
    def health(self) -> dict: ...    # surfaces in `quant_doctor`
```

`MarketContext.bars` is a pandas DataFrame indexed by row (NOT timestamp index — keeping the timestamp as a column avoids subtle indexing bugs across analysts that index into `.iloc[-1]`). The columns are at minimum `[timestamp, open, high, low, close, volume]`; some data sources also provide `amount` (Kronos uses it).

`MarketContext.extras` is a string-keyed dict for provider-specific extras. v0.1 ships analysts that consume:
- `orderbook` (top-N bids + asks; populated by ccxt for crypto when available)
- `news` (list of recent headlines + LLM-extracted sentiment; populated by the news provider)
- `regime` (HMM regime label; populated by the regime classifier if enabled)

`AnalystView.confidence` MUST be calibrated. An analyst that emits 0.8 confidence is asserting ~80% directional accuracy. The aggregator will eventually compute calibration error per analyst; an analyst whose ECE > 0.15 will be auto-down-weighted (per the aggregator policy in ADR-0003).

`AnalystView | None` return: an analyst MAY return None when it has no view (insufficient context, asset class out of scope, etc.). The aggregator drops None views before combining.

### Versioning rule

Fields are **added only** to `MarketContext` and `AnalystView`. Never renamed, never removed, before a major version bump. New fields have defaults. The aggregator ignores fields it doesn't know. This is the same evolution pattern the hermes-s2s plugin uses for `ConnectOptions` (see hermes-s2s ADR-0014).

### Stateful but pure-modulo-randomness

Analysts MAY have internal state (model weights, rolling caches, calibration histories). The contract: same `MarketContext` produces same `AnalystView` modulo sampling randomness (Kronos's `sample_count` introduces variance). The aggregator is robust to noise.

Stateful learning is encapsulated **inside** the analyst via an optional `update(realized: RealizedOutcome)` method called by the daemon's settlement loop. Analysts that don't implement `update` are static (TA rules, etc.).

```python
@dataclass(frozen=True)
class RealizedOutcome:
    view: AnalystView                # the view we emitted
    asof_view: pd.Timestamp          # when it was emitted
    asof_settlement: pd.Timestamp    # when the horizon expired
    realized_return: float           # actual return over the horizon
    direction_correct: bool

class StatefulAnalyst(Analyst, Protocol):
    def update(self, outcome: RealizedOutcome) -> None: ...
```

### Discovery

Analysts are registered via Python entry points in `[project.entry-points."hermes_quant.analysts"]`. The daemon enumerates and instantiates entries on startup, gated by config (each analyst can be `enabled: true|false` in `~/.hermes/config.yaml::quant.analysts.<name>`).

```toml
[project.entry-points."hermes_quant.analysts"]
classical_ta = "hermes_quant.analysts.classical_ta:ClassicalTAAnalyst"
microstructure_lite = "hermes_quant.analysts.microstructure:MicrostructureLite"
kronos_small = "hermes_quant.analysts.kronos:KronosAnalyst"
kairos_btc = "hermes_quant.analysts.kronos:KairosAnalyst"
```

This mirrors the freqtrade strategy discovery pattern but uses standard setuptools entry points instead of strategy directories. Plugin authors can publish their own analyst-only packages and they auto-register.

## Consequences

### Positive

- Heterogeneous analysts share one contract; aggregator code is simple.
- Calibration is a first-class concern — confidence is meaningful, not decorative.
- Versioning rule (add-only) is the same one Hermes core uses; users will recognize it.
- Entry-point-based discovery means third-party analyst packages plug in without forking the repo.
- Pure-modulo-randomness contract makes testing tractable; replay tests just compare on a tolerance.

### Negative

- Heterogeneity loses information. An analyst that has a full forecast distribution must compress it to (direction, magnitude, confidence). v0.2 may add a richer `metadata` channel the aggregator can opt into.
- Calibration enforcement is slow to ramp up — needs ≥30 days of realized outcomes to be reliable. v0.1 ships uncalibrated and warns; v0.2 adds isotonic calibration.
- Sync API limits concurrency. A 5-analyst pool with one slow analyst (Kronos CPU = ~3-5s) bottlenecks the tick. ThreadPoolExecutor mitigates I/O-bound; CPU-bound stays serial.
- Dataclass evolution requires discipline. CI test pins the field set; adding a field requires a version bump and a CHANGELOG entry.

## Implementation notes

- All three protocol classes live in `hermes_quant/protocol.py` — single source of truth, no circular imports.
- Analyst implementations import from `hermes_quant.protocol` only; never from each other.
- The daemon runs analysts via `concurrent.futures.ThreadPoolExecutor(max_workers=N)` where N = `len(enabled_analysts)`. Per-analyst timeout = 30s for 5m+ timeframes, 5s for 1m. Timeout → drop the view + log warning + report in `quant_doctor`.
- Calibration tracking: per-analyst rolling 30-day window of (confidence, direction_correct) pairs in `analyst_calibration` SQLite table. ECE computed in `quant_doctor`.

## References

- `docs/research/03-plugin-architecture.md` §3 — protocol design exploration
- `docs/research/01-rl-for-trading.md` §3 — analyst contract requirements (calibration, time horizon)
- hermes-s2s `voice/connect_options.py` — versioning rule reference
