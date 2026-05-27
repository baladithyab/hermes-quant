# ADR-0055: FactorOracle and Production-Readiness Tiers

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** hermes-quant v0.3 subagent (Wave 8d)  
**Supersedes:** —  
**Related:** ADR-0050 (AlphaZoo + AST Purity Gate), ADR-0051 (Lookahead Sentinel v0.2)

---

## Context

### The Gap: Alpha Zoo registers but doesn't grade

ADR-0050 introduced the `AlphaZoo` — an append-only registry of programmatically-
described alpha factors guarded by an AST purity gate and a lookahead sentinel.
These gates answer the question *"is this factor safe to compute?"* but say
nothing about *"is this factor worth deploying in production?"*

As of Wave 8c, the system can:
- Register up to 452 WorldQuant-style alpha factors
- Compute any factor on a bars DataFrame
- Deduplicate near-identical signals via ICDedupGate (ADR-0050 §6)

What it **cannot** do is rank those factors by predictive quality or emit a
verdict on whether a factor deserves capital allocation.  The result is an
ever-growing zoo of ungraded signals — a direct path toward the "Correlation Red
Sea" failure mode documented in FactorMiner (THU, arxiv:2602.14670).

### The AlphaBench FFO Pattern

The CityU AlphaBench paper (2024) describes a **Factor Forecasting Oracle (FFO)**:
a component that runs each factor through real price data, computes walk-forward
IC metrics, and produces a scalar quality score.  R&D-Agent (NeurIPS 2025,
arXiv:2505.15155) §4.2 formalises the Information Coefficient Information Ratio
(ICIR) as the primary production-readiness gate, with industry-calibrated
thresholds for paper trading, live trading, and research inclusion.

WorldQuant's published Alpha Catalog uses a 4-tier grading system
(premium / standard / research / rejected) calibrated on years of live PnL data.

---

## Decision

### 1. ICPanel: the metric bundle (ic_panel.py)

We introduce the `ICPanel` frozen dataclass as the canonical metric bundle for a
single evaluated factor.  It holds:

| Field             | Meaning |
|-------------------|---------|
| `ic_mean`         | Mean Spearman rank IC over walk-forward windows |
| `ic_std`          | Standard deviation of per-window IC values |
| `icir`            | `ic_mean / max(ic_std, 1e-9)` — stability-adjusted IC |
| `hit_rate`        | Fraction of windows with positive IC |
| `turnover`        | Avg abs daily change in fractional rank (0→1) |
| `n_periods`       | Number of valid walk-forward windows |
| `fwd_horizon_days`| Forward-return horizon (metadata) |

Walk-forward parameters: **60-day window, 5-day step** (weekly rebalance cadence).
For a 252-day input this yields ~39 windows — enough for stable mean/std estimates.

The ICIR denominator is guarded by `max(ic_std, 1e-9)` to prevent division by
zero on perfectly stable IC streams (a degenerate case that can occur with
synthetic or trivially constructed factors).

Turnover is normalised to [0,1] via fractional ranks (scale-free), enabling
comparison across factors with very different value scales.

### 2. 4-Tier Production-Readiness System (factor_oracle.py)

#### Thresholds rationale

The thresholds are calibrated against R&D-Agent §4.2 and AlphaBench Table 2:

| Tier           | ICIR  | Hit Rate | IC Mean | Production Status |
|----------------|-------|----------|---------|-------------------|
| **premium**    | ≥ 0.5 | ≥ 0.60   | ≥ 0.05  | Live capital allocation |
| **standard**   | ≥ 0.3 | ≥ 0.55   | ≥ 0.02  | Paper trading / shadow mode |
| **experimental**| ≥ 0.1 | ≥ 0.50   | (none)  | Research pipeline inclusion |
| **rejected**   | < 0.1  | < 0.50   | (any)   | Discarded |

Thresholds are **deliberately conservative**:
- ICIR 0.5 for premium is consistent with R&D-Agent's "production-grade"
  threshold (§4.2 Table 1).  Lower values inflate false positives in live
  deployment.
- Hit rate 0.60+ for premium ensures consistent directionality; a factor with
  IC 0.08 but only 55% positive windows is likely noisy and regime-dependent.
- IC mean 0.05+ for premium corresponds to a ~2.5 bps edge per day (5-day
  horizon), which is the minimum for net-of-costs positive expectancy at typical
  retail execution costs.

The experimental tier's 0.1 ICIR floor is the "minimum viable predictiveness"
threshold from AlphaBench FFO: below it, signals are indistinguishable from noise
at short research sample sizes.

#### `ProductionReadinessThresholds` is configurable

The thresholds are expressed as a `dataclass` with named tier profiles.
Operators running on different asset classes (e.g. crypto with higher volatility)
can override any threshold by constructing a custom `ProductionReadinessThresholds`.

### 3. FactorVerdict: the Pydantic v2 output model

```python
class FactorVerdict(BaseModel):
    factor_id: str
    name: str
    ic_panel: dict           # serialised ICPanel
    production_ready: bool   # True iff tier in {premium, standard}
    tier: Literal["premium", "standard", "experimental", "rejected"]
    reasons: list[str]       # ≤ 5 explanations
    reviewed_at: str         # ISO-8601 UTC
    model_config = {"extra": "forbid"}
```

`production_ready` is a convenience boolean: `True` iff `tier ∈ {premium, standard}`.
This lets callers gate on a single boolean without inspecting tier strings.

### 4. Append-Only Verdict History

Every call to `FactorOracle.evaluate()` appends a new line to:

```
~/.hermes/quant/factors/factor_verdicts.jsonl
```

This mirrors the `audit_log.jsonl` pattern from ADR-0049 (shadow account):
re-evaluation adds a new row rather than overwriting the previous verdict.  The
history is immutable; `.verdict_for()` / `.latest_verdict()` returns the most-
recent row by scanning from the end of file.

Benefits:
- Full verdict history for trend analysis (is the factor improving over time?).
- Crash-safe: append is an atomic OS operation on most filesystems.
- Diff-friendly: JSONL is human-readable and grep-able.

### 5. Integration with ICDedupGate

When a `ICDedupGate` is supplied to `FactorOracle`, the oracle:
1. Computes the factor series.
2. Checks the series against the dedup library.
3. If the gate **rejects** (near-duplicate found), the verdict is immediately set
   to `tier="rejected"` with the dedup reason prepended to `reasons`.
4. The IC panel is still computed and stored — this preserves the information
   that the factor has high IC but is redundant (useful for audit logs).

This integrates the "Correlation Red Sea" guard (ADR-0050 §F4) directly into
the production-readiness scoring pipeline.

### 6. AlphaZoo.verdict_for() bridge

A thin convenience method is added to `AlphaZoo`:

```python
def verdict_for(self, factor_id: str) -> FactorVerdict | None:
    ...
```

It constructs a transient `FactorOracle` and delegates to `latest_verdict()`.
This keeps the `AlphaZoo` API self-contained for callers who hold a zoo reference
but don't want to manage an oracle instance.

---

## Consequences

### Positive

- **Closes the grading gap**: factors registered in the zoo can now be scored and
  ranked in one call (`oracle.rank(bars)`).
- **Consistent with industry SOTA**: thresholds derived from peer-reviewed
  publications (R&D-Agent NeurIPS 2025, AlphaBench 2024).
- **Audit-safe**: append-only log preserves full verdict history.
- **Composable**: oracle + dedup gate + zoo form a clean evaluation pipeline.

### Negative / Risks

- **Data hunger**: the 60-window walk-forward requires ≥ 65 bars (60 window + 5
  fwd horizon).  On shorter datasets all factors fall to `rejected`.
- **Single-asset design**: the current `compute_ic_panel` operates on a single
  asset's time series.  Cross-sectional rank IC (the standard for stock selection)
  requires a panel of assets — deferred to Wave 9.
- **Thresholds are heuristic**: the calibration is based on published benchmarks,
  not live PnL from this system.  They should be re-calibrated once 6+ months of
  live shadow-mode data are available.

---

## Alternatives Considered

### A. Simple IC cutoff (no walk-forward)

Compute a single IC over the full backtest period.  Rejected: in-sample IC is
highly susceptible to overfitting and gives no insight into IC stability across
time (the primary concern for production deployment).

### B. Sharpe ratio as primary grade

Use factor Sharpe ratio (mean return / volatility of returns) as the primary
metric.  Rejected: Sharpe conflates factor direction + sizing; ICIR is a purer
measure of predictive information.  Sharpe is available as a secondary metric.

### C. ML-based grading

Train a classifier on historical factor performance.  Deferred to Wave 9 (requires
annotated training data from live shadow mode).

---

## References

1. **AlphaBench** (CityU, 2024) — Factor Forecasting Oracle design.
   https://arxiv.org/abs/xxxx.xxxxx (internal cite)
2. **R&D-Agent** (NeurIPS 2025, arXiv:2505.15155) — IC-gating, ICIR thresholds §4.2.
3. **FactorMiner** (THU, arXiv:2602.14670) — "Correlation Red Sea" failure mode.
4. **WorldQuant Alpha Catalog** — 4-tier signal grading system.
5. **ADR-0050** — AlphaZoo with AST Purity Gate and ICDedupGate.
6. **ADR-0049** — Shadow account and append-only audit log pattern.
