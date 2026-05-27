# ADR-0045: Walk-Forward Backtester with Explicit Cost Model

**Status:** Accepted  
**Date:** 2026-05-27  
**Wave:** 6a  
**Supersedes:** none  
**Related:** ADR-0020 (replay backtest), ADR-0019 (purged walk-forward splits), ADR-0043 (risk committee), ADR-0044 (trader stage)

---

## Context

The 2026 SOTA scan (arxiv:2605.19337, Xia et al., Shenzhen University, May 2026) audited 77
empirical LLM-trading studies published 2022–2026 and found:

> "Only 2/19 empirically-primary studies report time-consistent train/test splits; only 1/19
> includes a transaction-cost model; 15/19 are rated R0 (irreproducible)."

The paper identifies two structural failure modes:

- **F1 — Contaminated backtesting (lookahead leakage):** Strategy code reads future data
  (tomorrow's close, future news) to make today's decisions. The result inflates performance
  metrics to meaninglessness. Only 2/19 papers use valid splits.

- **F2 — Oracle Fallacy in memory:** Memory/reflection stores use information available only
  after the decision window to feed back into the strategy, creating a hidden lookahead.

Neither the original `backtest/replay.py` (ADR-0020) nor any prior wave addressed F1 with
an engine-level guard, and none addressed F2 in the context of multi-step walk-forward.

Additionally, the existing replay backtest has no explicit transaction-cost model. Every fill
was computed at decision price, meaning simulated returns are always optimistic vs. real
execution. The STOCKBENCH ICLR 2026 benchmark (openreview.net/forum?id=XUBKgiO29d) further
confirms that "most frontier LLMs fail to beat a passive buy-and-hold baseline" — making
a correct BuyAndHold benchmark essential for charter gating.

**Minimum R1 reproducibility bar** (arxiv:2605.19337 §4): published split protocol. This
ADR satisfies R1 by documenting the exact split contract, the engine's lookahead guard, and
the cost model coefficients.

---

## Decision

Ship `hermes_quant/backtest/` Wave 6a additions:

1. **`cost_model.py` — CostModel** with Almgren et al. sqrt-impact model.
2. **`engine.py` — WalkForwardEngine** with hard no-lookahead contract.
3. **`stub_llm.py` — StubLLMCommittee** for dry-run mode (no API calls).
4. **`strategy.py` — Strategy protocol + HermesQuantStrategy + BuyAndHoldStrategy**.
5. **`tests/backtest/`** — ≥25 tests including the canonical F1 leakage guard regression.

---

## Cost Model (cost_model.py)

### Formula

One-way cost (in basis points):

```
cost_bps = max(half_spread_bps + coeff × sqrt(participation_pct), slippage_floor_bps)
```

Round-trip:

```
round_trip_bps = 2 × cost_bps(participation_pct)
```

Fill price (adverse execution):

```
fill_price = decision_price × (1 + side × cost_bps / 10_000)
           + side × commission_per_share
```

where `side = +1` for BUY, `side = −1` for SELL.

### Parameter rationale

| Parameter | Default | Justification |
|---|---|---|
| `half_spread_bps` | 5.0 | Conservative estimate for liquid large-cap US equity (Hasbrouck 2009 TAQ study; confirmed by Mai0313/TradingAgents CostTracker defaults) |
| `market_impact_coeff` | 0.10 | Almgren, Thum, Hauptmann & Li (2005) empirical calibration for S&P500 stocks. At 10% ADV participation → ≈3.2 bps impact |
| `commission_per_share` | 0.0 | Commission-free era (Robinhood, IBKR Lite). Set to 0.005 for IBKR Pro simulation |
| `slippage_floor_bps` | 1.0 | Prevents unrealistically zero cost on micro orders |

### Named profiles

| Profile | `half_spread_bps` | `market_impact_coeff` | `commission_per_share` | Use case |
|---|---|---|---|---|
| `LIQUID_EQUITY` | 5.0 | 0.10 | 0.0 | S&P500 members, normal hours |
| `MIDCAP_EQUITY` | 10.0 | 0.18 | 0.0 | Russell 1000 ex-S&P500 |
| `ILLIQUID` | 15.0 | 0.30 | 0.005 | Russell 2000, OTC. ~3× liquid coefficients per Keim & Madhavan (1997) |

The sqrt-impact model is empirically supported by Almgren et al. (2005) as the best single-
parameter model of price impact for US equities. The sub-linear shape (doubling participation
< doubles impact) is consistent with observed market microstructure.

---

## No-Lookahead Engine Contract (engine.py)

**Walk-forward contract: any read of holdout-window data inside strategy.decide() is a
leakage bug. The engine asserts no holdout-window dates are passed into strategy callbacks.**

This quote appears verbatim in `WalkForwardEngine.run()` docstring so it is impossible to
miss during code review.

### Enforcement mechanism

For every step `asof` in the holdout window:

1. `_LookaheadGuardedFrame.build(ohlcv, asof, lookback_days)` returns a **filtered copy**
   of the OHLCV DataFrame containing only rows where `index ≤ asof`. No future rows exist
   in the object passed to `strategy.decide()`.

2. The engine **asserts** `lookback_data.index.max() ≤ asof` before calling
   `strategy.decide()`. If this assertion fails (engine-side bug), `LookaheadViolation` is
   raised rather than silently leaking.

3. Any strategy attempt to `.loc[future_date]` on `lookback_data` receives a `KeyError`
   from pandas (the date is absent from the index). This is tested in
   `TestLookaheadGuard::test_lookback_iloc_plus_one_is_keyerror`.

### F1 Regression test

`tests/backtest/test_walk_forward.py::TestLookaheadGuard` contains the **canonical F1
leakage guard regression test**:

```python
def test_lookback_data_has_no_future_dates(self, ohlcv_90, walk_forward_config_90):
    """LEAKAGE GUARD: lookback_data passed to strategy must never contain dates > asof."""
    cheater = _LookaheadCheatStrategy()  # scans lookback_data for future dates
    engine = WalkForwardEngine(walk_forward_config_90)
    engine.run(cheater, ["SPY"], ohlcv_90)
    assert cheater.future_dates_seen == []
```

This test MUST stay green. A failure means the engine has a leakage bug (F1 regression).

---

## StubLLM Dry-Run Pattern (stub_llm.py)

**Design reference:** Mai0313/TradingAgents `--dry-run StubChatModel` pattern
(github.com/Mai0313/TradingAgents, `src/tradingagents/backtest.py`).

`StubLLMCommittee` is a **deterministic** drop-in for all LLM committee calls:

- `research_plan(direction, confidence, symbol)` → deterministic ResearchPlan-shaped dict.
- `__call__(system_prompt, user_prompt)` → deterministic neutral risk critique string.

**Determinism contract:** Identical inputs → identical outputs, always. No random state,
no time-based output, no side effects. This is tested by
`TestStubDeterminism::test_deterministic_100_calls`.

**Mapping:** `direction > 0 → "Buy"`, `direction == 0 → "Hold"`, `direction < 0 → "Sell"`.

When `dry_run_llm=True` (the default):
- `HermesQuantStrategy` uses `StubLLMCommittee.research_plan()` for the advisor signal.
- `RiskCommittee` receives `stub` as its `llm_caller` (reserved v0.2 interface).
- **Zero API calls are made.** The full advisor → trader → risk_committee pipeline runs
  end-to-end on synthetic OHLCV without any external dependencies.

---

## Strategy Protocol and BuyAndHold Baseline (strategy.py)

### Strategy protocol

```python
class Strategy(Protocol):
    def decide(self, asof: pd.Timestamp, lookback_data: pd.DataFrame) -> list[Decision]: ...
```

All strategies implement this single method. The engine calls `decide()` at each step.

### HermesQuantStrategy

Wires existing components (TraderNode v0.1 + RiskCommittee v0.1) behind the Strategy
protocol. Uses a 20-bar momentum signal as the "advisor" stand-in, producing a direction
signal that drives `StubLLMCommittee.research_plan()`.

**Why compose, not reimplement:** TraderNode and RiskCommittee are fully tested Wave 2/3
components. HermesQuantStrategy is only the wiring + the `decide()` shim. Any improvements
to those components automatically flow through the backtester.

### BuyAndHoldStrategy — the baseline

The BuyAndHold strategy allocates equal weight to every symbol on the first call and holds
forever. It is the **required baseline benchmark**: a strategy that cannot beat BuyAndHold
net of costs over the holdout window fails the charter gate:

> *"if your three-analyst committee on BTC can't beat buy-and-hold risk-adjusted, more
> analysts won't fix it."* — ADR-0020 charter

`alpha_vs_benchmark = total_return − benchmark_return` (computed in `WalkForwardResult`).
Positive alpha is necessary (but not sufficient) for a strategy to proceed to paper trading.

---

## WalkForwardResult Metrics

| Metric | Definition |
|---|---|
| `total_return` | `(final_nav − initial_nav) / initial_nav` |
| `sharpe` | Annualised Sharpe (rf=0): `mean(daily_ret) / std(daily_ret) × sqrt(252)` |
| `sortino` | Annualised Sortino: `mean(daily_ret) / std(negative_daily_rets) × sqrt(252)` |
| `max_drawdown` | Maximum peak-to-trough drawdown (always ≤ 0) |
| `win_rate` | Fraction of completed round-trips with positive gross PnL |
| `n_trades` | Count of fills executed |
| `gross_pnl` | PnL before transaction costs |
| `cost_pnl` | Transaction cost drag (always ≤ 0) |
| `benchmark_return` | Equal-weight BuyAndHold return over the same holdout window |
| `alpha_vs_benchmark` | `total_return − benchmark_return` |
| `decisions_journal` | List of fill records for post-hoc analysis |

---

## Consequences

**Positive:**
- hermes-quant now hits R1 reproducibility (published split protocol, no lookahead).
- Every fill has an explicit cost attached; simulated returns are no longer optimistic.
- `--dry-run` mode enables CI-fast backtesting of the full pipeline with zero API calls.
- The F1 leakage guard is a hard runtime error, not a silent performance inflation.
- `BuyAndHoldStrategy` provides the charter's required benchmark for alpha measurement.

**Negative / trade-offs:**
- HermesQuantStrategy uses a simplified momentum signal, not the full multi-analyst advisor.
  A full integration requires either (a) mocking the data-provider layer or (b) pre-fetching
  OHLCV into the engine — deferred to Wave 6b.
- The sqrt-impact model has a single coefficient per profile. A per-symbol calibration
  (e.g. from ADV data) would be more accurate but requires external data — deferred.
- F2 (Oracle Fallacy in memory) is partially mitigated by the lookback filter but not
  fully addressed if `DecisionLog` is read inside `strategy.decide()`. A future wave should
  audit all memory reads in HermesQuantStrategy against the asof date.

---

## References

- Xia et al. (2026). "Agentic Trading: When LLM Agents Meet Financial Markets."
  arXiv:2605.19337. → SOTA methods audit, F1/F2 failure modes, R0–R3 reproducibility scale.
- Almgren, Thum, Hauptmann & Li (2005). "Direct Estimation of Equity Market Impact."
  *Gestion de Fortunes*. → sqrt-impact model calibration for US equities.
- Hasbrouck (2009). "Trading Costs and Returns for U.S. Equities." *Journal of Finance*.
  → Half-spread calibration for large-cap equities.
- Keim & Madhavan (1997). "Transaction Costs and Investment Style." *Journal of Financial
  Economics* 46(3). → Small-cap / illiquid transaction cost multiples.
- Mai0313/TradingAgents (2026). github.com/Mai0313/TradingAgents.
  → `--dry-run StubChatModel` + `CostTracker` reference implementation.
- STOCKBENCH ICLR 2026. openreview.net/forum?id=XUBKgiO29d.
  → Contamination-free benchmark; confirms LLMs rarely beat BuyAndHold.
