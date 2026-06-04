# ADR-0036: Multi-Timeframe Analyst Fan-Out

**Status:** Accepted (2026-05-26), implemented
**Date:** 2026-05-26
**Wave:** Multi-timeframe analysis (interday/interweek/intermonth/interquarter)
**Related:** ADR-0002 (Analyst Protocol), ADR-0003 (Aggregator), ADR-0009 (P0-2 calibrator), ADR-0018 (Kronos), ADR-0023 (Deliberative Committee), ADR-0035 (Cadence)
**Cost:** ~2× analyst latency per horizon enabled (mitigated by Kronos GPU batching)

---

## Context

The 5-play playbook from the methodology docs operates on **multiple holding
periods simultaneously**:

| Play | Decision horizon | Outcome horizon |
|---|---|---|
| swing | 1d signal | 30-90 days hold |
| covered_call | 1d signal | 21-36 days hold |
| csp | 1d signal | 21-45 days hold |
| wheel | 1d signal | multi-cycle, weeks/months |
| leaps | 1w signal | 12-18 month hold |

The current `advisor.recommend(symbol, timeframe="1d")` API runs analysts on a
**single horizon** — typically 1d. This means:

1. A LEAPS thesis driven by 1d momentum is structurally myopic — you want
   weekly/monthly trend confirmation before opening a 12-month position.
2. A swing entry confirmed by both daily and weekly direction has higher Sharpe
   than a daily-only entry (textbook MTF confirmation; see Bulkowski 2005).
3. Quarterly review (ADR-0035 §"Quarterly review") needs analyst signals on the
   1Q horizon to flag rebalance candidates — currently it only inspects
   portfolio metrics, not forward-looking analyst views.

The TradingAgents reference project (R1 in `docs/research/reference-projects/2026-05-24-r1-tradingagents.md`)
runs analysts sequentially on a single time bucket, and AI-Trader (R2) uses
single-horizon signals. Neither model multi-timeframe explicitly. This is one
of the few places we can **improve on** the references rather than just port
patterns.

The existing `AnalystView` schema already carries `horizon: str` (per ADR-0002).
The Kronos GPU batched inference (commit `cab7541`) makes running analysts
across N horizons computationally cheap when N ≤ 4.

## Decision

Adopt a **parallel multi-timeframe analyst fan-out** at the `advisor.recommend`
boundary. Each analyst is invoked once per enabled horizon; the resulting
horizon-tagged `AnalystView` objects feed the BMA aggregator and the
deliberative committee unchanged.

### Default horizon set

Five horizons, each tagged with the matching playbook play:

| Horizon | OHLCV bar | Lookback bars | Primary play(s) | Default state |
|---|---|---|---|---|
| `1d` | daily | 252 (1y) | swing, covered_call, csp, wheel | enabled |
| `1w` | weekly (5d resampled) | 156 (3y) | swing, leaps | enabled |
| `1M` | monthly (21d resampled) | 60 (5y) | leaps | opt-in |
| `1Q` | quarterly (63d resampled) | 40 (10y) | quarterly-rebalance | opt-in |

The `1d` horizon is mandatory and matches existing behavior. `1w` becomes the
default-second horizon because the cost is low (Kronos GPU-batched) and it
materially improves swing/LEAPS confirmation. `1M` and `1Q` are opt-in via
config because:

- `1M`/`1Q` lookbacks need 5+ years of history — many small/mid-caps don't have
  that depth, so silence-by-default is correct
- Quarterly resampling produces only ~40 bars from a 10-year history; some
  analysts (Kronos, classical-TA) need ≥60 bars for stable behavior
- Operational cost (yfinance round-trips, BMA matrix size) doubles for each
  enabled horizon

### Fan-out mechanics

The new `advisor.recommend_multi_horizon(symbol, *, horizons, ...)` signature:

```python
def recommend_multi_horizon(
    symbol: str,
    *,
    horizons: Iterable[str] = ("1d", "1w"),
    asset_class: str = "equity",
    as_of: datetime | None = None,
) -> list[AnalystView]:
    """Fan out per-analyst across N horizons. Returns the union of views."""
    views: list[AnalystView] = []
    for h in horizons:
        ctx = build_market_context(symbol, timeframe=h, as_of=as_of)
        for analyst in registered_analysts(asset_class):
            view = analyst.analyze(ctx)
            if view is not None:
                # The view's existing horizon field is set by analyzed ctx.
                views.append(view)
    return views
```

The **horizon string is the only new degree of freedom**. Every existing
analyst already accepts a `MarketContext` with a `timeframe` attribute (per
ADR-0002), so this is a fan-out wrapper, not a new analyst contract.

### Cross-horizon BMA aggregation

The BMA aggregator's existing logic weights views by `analyst × horizon`. The
amendment to ADR-0003 §"Aggregator" makes this explicit:

```
weight_i = base_weight(analyst_i) × horizon_weight(horizon_i) × confidence_i
```

with `horizon_weight` defaulting to:

| Horizon | Default weight | Rationale |
|---|---|---|
| 1d | 1.00 | reference baseline |
| 1w | 1.20 | trend confirmation reduces noise; weekly bars survive whipsaws |
| 1M | 0.80 | useful for thesis confirmation but signal lags reality |
| 1Q | 0.60 | low-frequency: useful for rebalance flagging, weak for entry |

Weights are user-tunable per recipe (PDR runtime per ADR-0021). The
calibrator (ADR-0009 P0-2) eventually subsumes these as it learns
horizon-conditioned hit rates from realized fills.

### Multi-timeframe agreement bonus

A view-aggregation post-step rewards horizon consensus:

```
if all horizons agree on direction:
    final_confidence = mean(per_horizon_confidence) × 1.10  # +10% boost
elif any horizon disagrees:
    final_confidence = mean(per_horizon_confidence) × 0.85  # -15% penalty
```

This is a `final_confidence` adjustment only; it does not modify per-view
confidence (which remains the calibrator's training data). Capped at 1.0.

### Silence-by-default invariants preserved

- If any horizon's analyst returns `None` (insufficient bars / no signal /
  abstain), the view is skipped, not penalized. Multi-timeframe presence is
  best-effort.
- If only `1d` survives and `1w` was requested, the system runs the 1d-only
  pipeline and tags the aggregated signal with `horizons_present: ["1d"]` for
  audit.
- If zero horizons produce views, the aggregator emits silence per the
  existing protocol.

### Daily-cadence implications

The daily playbook tick (ADR-0035) currently hard-codes `timeframe="1d"`. It
becomes:

```python
horizons = os.environ.get("HERMES_QUANT_HORIZONS", "1d,1w").split(",")
views = recommend_multi_horizon(symbol, horizons=horizons, ...)
```

Default-on for `1d,1w`, opt-out via `HERMES_QUANT_HORIZONS=1d`.

The weekly rebalance and quarterly review crons (ADR-0035) get the same
horizon set, scaled appropriately:

- Weekly rebalance: default `1d,1w,1M`
- Quarterly review: default `1w,1M,1Q`

## Consequences

### Positive

- **Sharpe improvement** from MTF confirmation is well-documented in the
  literature; the +10%/-15% confidence adjustment encodes it with conservative
  defaults.
- **Cost is bounded** by Kronos GPU batching — ~120 s for a 500-symbol scan
  doubles to ~240 s when `1d,1w` is enabled, still inside the pre-open window.
- **No analyst code changes required** — every existing analyst already
  consumes `MarketContext.timeframe`. This is a wrapper-level decision.
- **The horizon field on `AnalystView` is finally load-bearing** — it was
  reserved in ADR-0002 but never differentiated from the recipe-default.
- **Improves quarterly review meaningfully** — instead of just flagging
  exposure based on portfolio metrics, the quarterly cron can now ask "what
  do my analysts think *forward* on the 1Q horizon?" and gate rebalances on
  forward-looking analyst signals.

### Negative

- **2× analyst latency per added horizon** (or N× for N horizons). For the
  default `1d,1w` set, this is ~240s per universe scan post-Kronos-GPU. Still
  comfortable inside pre-open window; gets tighter if `1M`/`1Q` are added.
- **yfinance rate-limiting risk** — each horizon is a separate `tk.history`
  call. Mitigation: cache the 5y-history once per symbol per day, resample
  in-memory for shorter horizons. The cache key is
  `(symbol, asof_date, longest_horizon_lookback)`.
- **BMA matrix size grows** — the aggregator must handle K analysts × H
  horizons views. For K=5 analysts × H=4 horizons = 20 input views per symbol
  vs the current 5. Memory is trivial; the calibrator's epistemic state grows.
  Mitigation: per-(analyst,horizon) calibrators, not a single fused calibrator.
- **`1M` and `1Q` resampling has implementation pitfalls** — bar-aggregation
  must respect calendar quarters / months (not just N consecutive days), or
  the lookback semantics drift. Mitigation: use pandas resample with
  `BQE-DEC` (business quarter end) and `BME` (business month end) frequencies.

### Neutral

- The horizon-weight defaults are starting points. Production tuning will
  adjust them per asset class (crypto's 1d weights might be lower than equity's
  because crypto is 24/7 and intraday noise bleeds into daily bars more).

## Out of scope

- **Sub-daily horizons** (`1h`, `4h`, `15m`). Per ADR-0035, the system has no
  intraday strategies; sub-daily horizons would be unused decoration.
- **Tick-level horizons.** Same.
- **Cross-asset horizon coupling** (e.g. "TLT 1Q signal informs SPY 1d
  weight"). Future work, post-Wave-G.

## Implementation notes

- `recommend_multi_horizon` lives next to `advisor.recommend` in
  `hermes_quant/advisor.py`. The single-horizon `recommend` becomes a thin
  shim over `recommend_multi_horizon([timeframe])` after the fan-out lands,
  preserving backward compatibility.
- The horizon-cache (`hermes_quant/data/horizon_cache.py`) memoizes the
  longest-history fetch per `(symbol, as_of_date)` and returns resampled
  views for shorter horizons. Cleared on calendar day boundary.
- Tests:
  - `tests/unit/test_multi_horizon_advisor.py` — fan-out correctness
  - `tests/unit/test_horizon_resample.py` — calendar-aware resampling
  - `tests/integration/test_multi_horizon_smoke.py` — live yfinance smoke,
    skipped if `HERMES_QUANT_INTEGRATION=1` not set
- Migration: existing callers passing `timeframe="1d"` continue to work
  unchanged (single-horizon path). Opt-in via `HERMES_QUANT_HORIZONS` env var
  or per-recipe override in the PDR runtime.

## Decision summary

We commit to **parallel multi-timeframe analyst fan-out** with `1d,1w` as the
default-on set, `1M,1Q` as opt-in, cross-horizon BMA weighting with
conservative defaults, and a +10%/-15% multi-timeframe-agreement adjustment.
Existing analyst contracts are preserved; this is a wrapper-level extension.
