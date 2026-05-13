[P0] ADR-0004 & ADR-0008: Split-Brain Portfolio State
  Issue: ADR-0008 delegates order management (including trailing stops and partial fills) to Freqtrade. However, ADR-0004 states the daemon's risk gate calculates `Portfolio.current_position` and `drawdown_pct` from its own "realized P&L log" based on signal horizons. 
  Why it matters: If Freqtrade closes a position early due to a trailing stop, the daemon has no idea. The daemon's `Portfolio` state will immediately diverge from reality. The risk gate will think it is at `max_position_pct` and silently drop new signals, or fail to trigger the `max_drawdown_pct` circuit breaker because it missed the stop-loss execution. This is a fatal split-brain architecture for a live trading system.
  Proposed fix: The daemon must not guess portfolio state. Either Freqtrade must write back an `executions.jsonl` that the daemon tails to update its `Portfolio` state, or the daemon must query the broker directly (via `ccxt`/`alpaca`) at the start of every tick to construct the true `Portfolio` object before passing it to the risk gate.

[P1] ADR-0002 & ADR-0003: RL / Stacking Aggregator Data Starvation
  Issue: ADR-0003 states the aggregator learns via `Aggregator.update(outcomes: list[RealizedOutcome])`. ADR-0002 defines `RealizedOutcome` as containing only a *single* `AnalystView`. 
  Why it matters: To train an RL policy (or even the Logistic Stacking model), the aggregator needs the *cross-sectional* state at time T (e.g., "What did all 5 analysts say at 10:00 AM?"). If the aggregator only receives a flat list of individual, isolated analyst outcomes, it cannot reconstruct the joint distribution or learn correlations (e.g., "when Analyst A and B disagree, default to flat"). 
  Proposed fix: Redefine the settlement loop to emit a `TickOutcome` or `EpisodeExperience` that contains the full `AggregatedSignal` (which already holds the `components` tuple of all concurrent views) alongside the realized return for that tick.

[P1] ADR-0004 & ADR-0005: Cross-Asset Contagion in Circuit Breakers
  Issue: ADR-0005 introduces multi-asset support (Crypto via Binance, Equities via Alpaca). ADR-0004 defines a single `Portfolio` object with global `drawdown_pct` and `daily_loss_pct` circuit breakers.
  Why it matters: If a user runs both crypto and equities, a 15% weekend flash crash in crypto will trigger the `max_drawdown_pct` circuit breaker, halting the Alpaca equities strategy on Monday morning. Risk limits must respect account and asset-class isolation.
  Proposed fix: Partition `Portfolio` state and RiskConfig by `account_id` or `asset_class`. The risk gate signature should accept a partitioned portfolio, and halts should be scoped to the affected partition.

[P2] ADR-0004: Options-Incompatible Risk Math
  Issue: The risk gate hardcodes position sizing using a modified Kelly formula: `kelly_size = (magnitude * confidence) / volatility`. 
  Why it matters: This formula assumes linear, delta-1 instruments (spot crypto/equities). When v0.2 introduces options, applying this formula using implied volatility and asymmetric return profiles will result in mathematically invalid, catastrophic position sizing. It locks in a spot-only assumption at the lowest level of the risk gate.
  Proposed fix: Abstract the sizing math into an `InstrumentSizer` protocol, or explicitly add a guard clause: `if market.asset_class not in ["crypto", "equity"]: return None` until options-specific Greeks-based sizing is implemented.

[P2] ADR-0006: The RL Graduation Catch-22
  Issue: Graduation criterion #2 requires the Bayesian baseline to produce a "Sharpe ≥ 0.5" before the RL aggregator is allowed to ship. 
  Why it matters: The primary reason to use RL or Stacking over BMA is to capture *non-linear* interactions between analysts (e.g., Analyst A is only right when the Microstructure analyst is quiet). If the ensemble relies on non-linear logic, BMA will fail (Sharpe < 0.5), which permanently blocks the RL aggregator from ever being trained or shipped.
  Proposed fix: Remove Criterion #2. The graduation requirement should solely be Criterion #6 (RL significantly outperforms BMA out-of-sample via Deflated Sharpe Ratio), regardless of BMA's absolute performance.

[P2] ADR-0005 & ADR-0008: Delayed Data vs Live Execution Mismatch
  Issue: ADR-0005 allows `yfinance` (15-min delayed) as a fallback data provider. ADR-0008 uses Freqtrade connected to live CCXT for execution.
  Why it matters: If the daemon generates a signal based on 15-min delayed yfinance data, it will stamp the signal with a delayed `asof` timestamp. When Freqtrade receives this signal, its `process_only_new_candles` logic and live-orderbook pricing will either reject the signal as stale or execute it at a completely different live price, destroying the strategy's expected edge.
  Proposed fix: The daemon must enforce that if an external live executor is configured, delayed data providers (`yfinance` intraday) are strictly disabled. Add a startup validation check in `hermes quant start`.

BLOCK (Split-brain portfolio state between daemon and Freqtrade guarantees catastrophic risk-gate failures in v0.1).
