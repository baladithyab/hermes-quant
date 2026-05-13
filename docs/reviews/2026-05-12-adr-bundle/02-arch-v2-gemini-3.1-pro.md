Here is the ARCHITECTURE-AND-FORWARD-COMPAT review of the v2 bundle.

### New Issues Spotted in Amendments

[P1] ADR-0009 (P0-3) Split-brain portfolio state
  **Issue:** Race condition in periodic broker reconciliation causing spurious halts.
  **Why it matters:** The daemon queries the broker every N ticks to reconcile against its internal `Portfolio` (which is updated by tailing `executions.jsonl`). Because freqtrade writes to `executions.jsonl` asynchronously, there is a guaranteed race condition during active trading: an order fills on the broker, the daemon queries the broker *before* freqtrade flushes the execution log, the daemon sees a >0.5% discrepancy, and it triggers a critical alert/auto-halt.
  **Proposed fix:** Add a reconciliation grace period. Only trigger a discrepancy alert/halt if the mismatch persists across multiple consecutive reconciliation checks (e.g., 3 checks over 30 ticks), ensuring in-flight executions have time to settle on the bus.

[P1] ADR-0009 (P0-8) JSONL bus has no atomic write protocol
  **Issue:** Atomic write guarantees are only mandated for `signals.jsonl`, but omitted for the new `executions.jsonl` back-channel.
  **Why it matters:** Freqtrade writing to `executions.jsonl` using standard Python `open().write()` without `O_APPEND` + `fsync` + size-limit guarantees will inevitably result in partial line writes. When the daemon's settlement loop tails this file, it will encounter `json.JSONDecodeError` and potentially crash the settlement thread.
  **Proposed fix:** Explicitly mandate the same POSIX atomic-append protocol (≤4096 bytes, `O_APPEND`, `fsync`) for the freqtrade strategy's writes to `executions.jsonl`, and ensure the daemon's tailing logic includes the same `_read_signals_safe` partial-line buffering that freqtrade uses.

---

### Per-v1-Finding Verdicts

v1 [P0-1] Kelly formula off by factor of σ
  Status: ADDRESSED
  Notes: Formula corrected to `μ/σ²` and dimensional test fences added.

v1 [P0-2] Confidence uncalibrated but used as probability
  Status: ADDRESSED
  Notes: Isotonic calibrator and cold-start shrinkage elegantly solve the probability mismatch.

v1 [P0-3] Split-brain portfolio state
  Status: PARTIALLY-ADDRESSED
  Notes: `executions.jsonl` back-channel is the correct architectural fix, but the reconciliation loop introduces a race condition (see new issue above).

v1 [P0-4] Halt semantics contradictory
  Status: ADDRESSED
  Notes: Moving halts to durable SQLite state with a `halt_state.json` mirror cleanly separates control-plane state from the signal stream.

v1 [P0-5] Risk-gate ordering bug
  Status: ADDRESSED
  Notes: Circuit breakers correctly moved to the top of the evaluation chain.

v1 [P0-6] No daemon singleton enforcement
  Status: ADDRESSED
  Notes: POSIX advisory lock with PID liveness check is standard and robust.

v1 [P0-7] No global kill-switch / dead-man-switch
  Status: ADDRESSED
  Notes: Heartbeat + strategy-side staleness check + emergency-stop CLI covers all failure modes.

v1 [P0-8] JSONL bus has no atomic write protocol
  Status: PARTIALLY-ADDRESSED
  Notes: Solved perfectly for `signals.jsonl`, but the fix needs to be mirrored for `executions.jsonl` (see new issue above).

v1 [P1-9] Cross-asset contagion in circuit breakers
  Status: ADDRESSED
  Notes: Partitioning by `(account_id, asset_class)` prevents crypto volatility from halting equity trading.

v1 [P1-10] Stacking data starvation
  Status: ADDRESSED
  Notes: `EpisodeOutcome` provides the necessary cross-sectional joint state for the aggregator.

v1 [P1-11] CLI surface inconsistent across ADRs
  Status: ADDRESSED
  Notes: Canonical CLI list with CI test fence ensures documentation matches implementation.

v1 [P1-12] Slippage defaults too optimistic
  Status: ADDRESSED
  Notes: Revised bps defaults and 30-day adaptive rolling window are highly realistic.

v1 [P1-13] Walk-forward CV embargo unspecified
  Status: ADDRESSED
  Notes: `embargo = max(forecast_horizon, 2 * timeframe)` correctly prevents label overlap leakage.

v1 [P1-14] Backtest data mismatch (yfinance vs ccxt)
  Status: ADDRESSED
  Notes: Parquet handoff guarantees the daemon and freqtrade backtester see the exact same OHLCV bars.

v1 [P1-15] Sharpe targets ignore funding/borrow
  Status: ADDRESSED
  Notes: Net-of-cost Sharpe calculation now includes all real-world friction.

v1 [P2-16] BMA→RL graduation Catch-22
  Status: ADDRESSED
  Notes: Replacing the BMA Sharpe requirement with Diebold-Mariano and Benjamini-Hochberg FDR is statistically rigorous and removes the blocker.

---

**LIFT BLOCK**
The core architectural flaws have been comprehensively resolved; the remaining IPC edge cases in the new back-channel are easily patched without structural redesign.
