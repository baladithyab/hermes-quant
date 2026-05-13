[P0] ADR-0004 Kelly sizing formula is mathematically incorrect
  Issue: `kelly_size = (magnitude * confidence) / volatility` uses σ instead of σ². The continuous Kelly criterion is f* = μ/σ². The current formula is off by a factor of 1/σ, making position sizing arbitrary and not growth-optimal. Additionally, `confidence` is a heuristic score, not a calibrated probability, so the entire Kelly calculation is unreliable.
  Why it matters: Position sizes will be wrong relative to true risk. In low-volatility regimes, the formula may still produce plausible numbers by accident, but it will fail to adapt correctly when volatility changes, leading to suboptimal capital allocation and potential drawdown amplification.
  Proposed fix: Replace with f* = (magnitude * calibrated_probability) / (volatility²). Calibrate `confidence` to a true directional probability (see next finding) before using it in the Kelly formula. Use quarter-Kelly on the corrected f*.

[P0] ADR-0003 Aggregated confidence is not calibrated
  Issue: The aggregator computes `confidence = abs(direction_score) * max(0, 1 - 2*disagreement)`. This is a heuristic score, not a calibrated probability. The system then treats it as a probability in the Kelly sizing (`magnitude * confidence`) and in the cost gate (`expected_edge = abs(magnitude) * confidence`).
  Why it matters: Using an uncalibrated score as a probability will cause systematic mis-sizing and incorrect trade filtering. Overconfident signals will lead to excessive position sizes; underconfident signals will leave money on the table. This directly subtracts from PnL.
  Proposed fix: Calibrate the aggregated confidence using historical outcomes (e.g., isotonic regression or Platt scaling on a rolling window of aggregated signals vs. realized directions). The output must be a calibrated probability in [0,1].

[P0] ADR-0002 / ADR-0003 Kronos path-based confidence is not calibrated for direction
  Issue: Kronos wrapper sets `confidence = max(P(path > last), P(path < last))` over sampled paths. Sampled-path agreement is not a marginal directional probability—paths are correlated, so the fraction of paths ending above the last price systematically overestimates the true probability of the direction being correct.
  Why it matters: Kronos will emit overconfident signals, inflating its weight in the ensemble and causing the aggregator to over-allocate to Kronos’s views. This leads to larger position sizes on low-quality signals and increased drawdowns.
  Proposed fix: Calibrate Kronos’s raw path-based score using historical directional outcomes (e.g., fit a logistic regression mapping the path-score to actual direction correctness). The output `confidence` must be a calibrated probability.

[P1] ADR-0004 Slippage bootstrap defaults are too optimistic
  Issue: Default slippage estimates are 5 bps for crypto, 2 bps for equities. Realistic retail crypto slippage on Binance for sub-$10K orders is 5–15 bps. Using 5 bps will underestimate transaction costs.
  Why it matters: The cost gate (`expected_edge >= 2 * transaction_cost`) will let through signals that appear profitable but become losing trades after real slippage. This quietly erodes PnL.
  Proposed fix: Increase crypto default to at least 10 bps. Allow user override. Use a conservative bootstrap until 30 days of real fill data exist.

[P1] ADR-0003 ECE down-weight threshold is too tight for the sample size
  Issue: Analysts with ECE > 0.15 are down-weighted. With a 30-day rolling window (~700 hourly samples), the standard error of ECE is ~0.02–0.04. A true ECE of 0.13 will frequently exceed 0.15 and be wrongly penalized.
  Why it matters: Good analysts will be intermittently down-weighted, reducing ensemble performance and causing unnecessary turnover in weights.
  Proposed fix: Use a longer calibration window (e.g., 90 days) or a higher threshold (e.g., 0.20). Alternatively, apply Bayesian shrinkage to ECE estimates.

[P1] ADR-0006 Walk-forward purged CV: embargo size not specified
  Issue: The ADR mentions purging and embargo but does not specify the embargo period. Without an embargo, training data can leak into validation folds, inflating backtest performance.
  Why it matters: Overestimated backtest Sharpe will lead to premature promotion of the RL aggregator and live deployment of an overfit model, causing real-money losses.
  Proposed fix: Define embargo = max(forecast_horizon, 2 * timeframe). Enforce in the `PurgedWalkForward` implementation.

[P1] ADR-0008 Backtest fidelity: potential data mismatch between signal generation and execution simulation
  Issue: Hermes-quant backtest uses its own data providers (e.g., yfinance), while freqtrade backtest uses exchange data (e.g., Binance). If the price series differ, fill prices in the simulation will not match the prices assumed during signal generation.
  Why it matters: The backtest will not faithfully reproduce live trading conditions, leading to an overoptimistic PnL estimate and unexpected slippage in production.
  Proposed fix: Ensure both signal generation and execution backtesting use the same data source (e.g., a unified parquet cache). If not possible, document the discrepancy and apply a conservative slippage buffer.

[P1] ADR-0006 Sharpe targets ignore funding rates / borrow costs
  Issue: The ADR sets Sharpe targets (0.5, 0.8–1.2, 1.5) but does not mention funding rates on perpetuals or borrow costs for shorting. Crypto funding can be a significant drag.
  Why it matters: A strategy that appears to have a Sharpe of 0.5 gross may have a net Sharpe near zero after funding costs, making the RL graduation gate misleading and potentially allowing a losing strategy to be promoted.
  Proposed fix: Include funding rate costs in the net return calculation. Adjust Sharpe targets to be net of all holding costs.

**Verdict: BLOCK** — The Kelly sizing formula is wrong, and the aggregated confidence is uncalibrated. These two P0 issues will cause incorrect position sizing and trade filtering, directly costing money. Fix before shipping v0.1.
