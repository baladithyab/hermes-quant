# ADR-0020: Backtest harness — `hermes_quant.backtest`

**Status**: Proposed
**Date**: 2026-05-13 (post v0.3.1)
**Target**: v0.3.2
**Cross-cuts**: ADR-0014 (advisor), ADR-0017 (ccxt), ADR-0019 (evaluation/cv + dsr), ADR-0010 (settlement journal), AGENTS.md "Reproducibility"
**Charter**: §"What I'd build first" — *"Paper-trade for 4-8 weeks logging every decision + every analyst's contribution. Then introduce the RL aggregator and see if it beats the Bayesian baseline out-of-sample."*

---

## Context

The charter requires paper-trade-then-measure BEFORE any RL aggregator
work. v0.3.1 has the analyst pool + aggregator + risk gate + autonomous
mode + journal — every piece needed to actually paper-trade. But the
charter's *empirical gate* is "does the three-analyst committee beat
buy-and-hold risk-adjusted?", and waiting 4-8 weeks of wall-clock for
the answer is impractical. **Historical replay collapses 4-8 weeks of
live paper into seconds-to-minutes.**

A backtest is, per AGENTS.md: *"run the daemon against historical bars,
capture the signal log, replay through freqtrade's backtester."* The
v0.3.2 backtest harness implements this without freqtrade as a hard
dependency: it replays bars through the advisor's same code path
(`recommend()` is already deterministic given seed + as_of), accumulates
paper executions, and computes Sharpe + Deflated Sharpe + max drawdown
+ buy-and-hold baseline.

## Decision

### D1: Scope — REPLAY, not simulation

The backtest is a **replay** of the production advisor pipeline against
historical OHLCV. It uses:

- Real production code paths (`advisor.recommend`, `BMAAggregator`,
  `DefaultRiskGate`, `PaperReactor`)
- Real production analysts (ClassicalTA + MicrostructureLite + Kronos)
- A historical OHLCV DataFrame as the only input

It does NOT simulate:
- Slippage curves (uses the configured `slippage_estimate` from
  `MarketState`, same as live; if backtest Sharpe is 1.5 with 5bps
  slippage, real Sharpe with 12bps slippage will be lower)
- Order book dynamics (we're a slow-cadence chat-mode trader, not HFT)
- Funding/borrow drift (assumed zero for v0.3.2; v0.4 will model)
- Tax (out of scope for trading-system R&D; freqtrade reports model this)

The point of D1 is fidelity to the production code path. Charter
*"Reproducibility"* invariant is honored: every backtest decision is
identical to what the live advisor would have produced at that bar.

### D2: Iteration loop

```python
def replay(
    bars: pd.DataFrame,
    *,
    symbol: str,
    asset_class: str,
    timeframe: str,
    initial_equity: float = 10_000.0,
    warmup_bars: int = 60,
    advisor_recommend=None,   # inject for testing
) -> BacktestResult:
    """Walk bars[warmup_bars:] forward bar-by-bar.

    For each bar at index i:
      1. as_of = bars.iloc[i]['timestamp']
      2. result = advisor.recommend(symbol, as_of=as_of, provider=ReplayProvider(bars))
      3. action = result['risk_gate'] -> Action
      4. apply Action to PaperPortfolio (mark-to-market via bar close)
      5. record per-bar equity, position, P&L

    Return: BacktestResult(equity_curve, executions, sharpe, dsr, max_dd, ...)
    """
```

`ReplayProvider` is a thin adapter that wraps the input bars and honors
the `as_of` filter (already a Protocol contract per ADR-0017 §D3). The
same lookahead-safety invariant applies: at bar `i`, only bars closing
at or before `bars.iloc[i]['timestamp']` are visible.

### D3: PaperPortfolio — minimal mark-to-market accounting

```python
@dataclass
class PaperPortfolio:
    cash: float
    position_qty: float           # signed; positive = long, negative = short
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def apply(self, action: Action, bar_close: float, *,
              commission: float = 0.001,
              slippage: float = 0.0005) -> None:
        """Move the portfolio toward action.target_position_pct of NAV."""
        ...

    def equity(self, mark_price: float) -> float:
        return self.cash + self.position_qty * mark_price - self.fees_paid

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position_qty == 0:
            return 0.0
        return self.position_qty * (mark_price - self.avg_entry_price)
```

This is deliberately simpler than `portfolio_loader` (production lot
matching). Backtest = single book, single symbol, no withdrawals,
no funding. v0.4 will unify with `portfolio_loader` once the production
calibrator-from-fills path needs both.

### D4: BacktestResult schema

```python
@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    timeframe: str
    asset_class: str
    n_bars: int
    n_decisions: int            # bars where advisor returned a non-flat signal
    n_fires: int                # bars where action.target_position_pct != 0

    initial_equity: float
    final_equity: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    deflated_sharpe: float       # via evaluation.dsr
    max_drawdown_pct: float
    n_trades: int

    # Buy-and-hold baseline
    buy_hold_total_return_pct: float
    buy_hold_sharpe: float
    excess_return_vs_buy_hold_pct: float

    # Per-bar series (for plotting / further analysis)
    equity_curve: pd.Series      # indexed by timestamp
    positions: pd.Series         # signed qty over time
    decisions: list[dict]        # per-bar advisor result snapshots

    # Reproducibility
    run_at: pd.Timestamp
    config_hash: str             # sha256 of advisor + analyst configs

    def to_markdown_report(self) -> str:
        """Operator-readable summary."""
        ...
```

The `excess_return_vs_buy_hold_pct` is THE charter-gating number:
*"if your three-analyst committee on BTC can't beat buy-and-hold
risk-adjusted, more analysts won't fix it."* If excess < 0 over a
sufficient backtest window, the answer to "should we work on RL
aggregator now?" is "no, fix the analysts/aggregator first."

### D5: CLI surface

```bash
# Run a backtest on local CSV/parquet
hermes quant backtest --symbol BTC/USDT --asset-class crypto \
  --timeframe 1h \
  --bars-file ~/.hermes/quant/cache/btc-usdt-2024.parquet \
  --output-dir ~/.hermes/quant/backtests/

# Or fetch via configured provider (yfinance for equities, ccxt for crypto)
hermes quant backtest --symbol AAPL --asset-class equity \
  --timeframe 1d --start 2024-01-01 --end 2024-12-31 \
  --output-dir ~/.hermes/quant/backtests/

# Output: a directory containing
#   result.json (machine-readable BacktestResult)
#   report.md (human-readable summary with Sharpe + DSR + buy-and-hold)
#   equity_curve.csv (per-bar equity)
#   decisions.jsonl (per-bar advisor result)
```

### D6: Buy-and-hold baseline computed inside `replay()`

Charter clause: *"if your three-analyst committee on BTC can't beat
buy-and-hold risk-adjusted on paper, more analysts won't fix it."*

The backtest computes buy-and-hold as a PAIRED comparison: we hold the
asset from `bars[warmup_bars]` to `bars[-1]` with full initial_equity,
record per-bar equity (= initial_equity * close[i] / close[warmup_bars]),
compute its Sharpe over the same period.

`BacktestResult.excess_return_vs_buy_hold_pct` = strategy - buy_hold.
This is the charter-gating headline metric.

### D7: Deflated Sharpe Ratio (already shipped in v0.3)

`evaluation.dsr.deflated_sharpe(observed_sharpe, n_trials=1, ...)` is
called against the strategy's daily returns. With `n_trials=1` (single
strategy, no search), DSR collapses to the Probabilistic Sharpe Ratio.
This hedges the headline Sharpe against the false-discovery probability
the charter explicitly warns about (*"survivorship bias in backtests is
brutal"*).

When v0.4 introduces RL aggregator hyperparameter search, `n_trials`
becomes the search budget, and DSR auto-tightens.

### D8: Replayability via deterministic seed

Every backtest run captures `config_hash` (sha256 of all analyst configs
+ aggregator config + risk gate config + KronosConfig.deterministic_seed).
Two runs with the same `(bars_file, config_hash)` MUST produce
byte-identical `result.json` (charter "Reproducibility" invariant).

This is testable: a CI gate runs the backtest twice on a fixture and
asserts byte-identical output.

### D9: Failure modes

| Failure | Behavior |
|---|---|
| Insufficient bars (< warmup_bars + 10) | Raise `ValueError` immediately |
| Advisor errors mid-backtest (e.g., transient inference NaN) | Log + treat as flat decision; continue |
| All analysts abstain for the whole run | Result has `n_decisions=0`; Sharpe undefined → reported as `nan` |
| Bars contain duplicates | Pre-deduped by `validate_bars` already; if input bypasses, raise |
| `as_of` timezone drift | Replay uses bar timestamps directly; tz preserved end-to-end |

### D10: Cross-references

- ADR-0014 — advisor pipeline (the code under test)
- ADR-0017 — ccxt provider (for fetching backtest data when not from file)
- ADR-0019 — `evaluation.dsr` consumed for DSR; `evaluation.cv` consumed by v0.4 RL training (NOT v0.3.2 backtest)
- ADR-0010 — settlement journal: backtest does NOT write to the production journal (separate path: `~/.hermes/quant/backtests/<run-id>/`)
- AGENTS.md "Reproducibility" — every signal replayable from disk

## Consequences

### Positive
- The charter's empirical gate becomes computable. Operators run
  `hermes quant backtest --symbol BTC/USDT ...` and get a buy-and-hold-
  excess number in minutes.
- Reuses production code paths exactly — backtest fidelity is high
- DSR hedge against backtest-overfitting baked in
- Reproducible by `config_hash`

### Negative
- D1's "no slippage simulation" means backtest Sharpe overstates real
  Sharpe by ~5-10bps per trade in liquid markets, more in illiquid.
  Documented; v0.4 may add a per-bar slippage model.
- Backtest doesn't model funding/borrow drift, which becomes a real
  cost on perp futures + multi-day shorts
- Single-symbol backtests miss correlation effects that multi-asset
  RL will need; v0.4+ multi-symbol backtest is its own ADR
- Walk-forward CV (already in `evaluation/cv.py`) is NOT integrated yet;
  v0.3.2 backtest is single-window. v0.4 RL training will compose
  `cv.PurgedWalkForward.split() -> backtest.replay()` per fold.

## Provenance
- Charter §"What I'd build first" + §"What works"
- AGENTS.md "Reproducibility"
- ADR-0019 evaluation scaffolding (DSR consumer)
