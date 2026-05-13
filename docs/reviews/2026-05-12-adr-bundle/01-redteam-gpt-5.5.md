[P0] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: JSONL is canonical bus but no atomic append/locking protocol is specified.  
  Failure mode: Daemon dies mid-write or two writers append concurrently; freqtrade tails/reads a partial or interleaved line, silently skips it on `JSONDecodeError`, missing flatten/halt or entry signals.  
  Proposed fix: Single-writer lockfile + PID ownership, append via `os.write()` with `O_APPEND` and one-line payload under POSIX atomic size assumptions, `fsync` on halt/flatten signals, and consumer must ignore only trailing incomplete line while alerting on repeated malformed records.

[P0] ADR-0001 Sidecar architecture — daemon decoupled from gateway  
  Issue: No daemon singleton enforcement despite systemd/tmux/launchd plus manual starts. `pkill -f` is not a safety mechanism.  
  Failure mode: Two daemons write competing signals/state for same account; freqtrade receives alternating target positions and churns/overtrades.  
  Proposed fix: Mandatory profile/account-scoped advisory lock on startup; daemon refuses to run if lock held by live PID; include daemon UUID in every signal and doctor alarms on multiple writers.

[P0] ADR-0004 Risk gate — deterministic rules, silence-by-default  
  Issue: Circuit breaker checks occur after “flat/zero-confidence” early return.  
  Failure mode: During drawdown, if aggregator emits flat/zero confidence, gate returns `None` instead of flatten+halt; existing losing positions remain open.  
  Proposed fix: Move drawdown/daily-loss/halt checks before signal flatness checks; circuit breakers must always emit flatten/halt independent of current signal.

[P0] ADR-0004 Risk gate — deterministic rules, silence-by-default  
  Issue: Halt flag is written to `state.json` with no atomicity, broker-side synchronization, or in-flight order handling.  
  Failure mode: Daemon sets halt while freqtrade has open orders; orders fill after flatten signal or halt file corrupts mid-write after crash; system believes halted while exposure remains.  
  Proposed fix: Store halt state in SQLite with transactional writes plus durable audit event; consumer must cancel open orders, force-exit positions, verify position=0 from broker, and keep halt latched until explicit resume.

[P0] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: Freqtrade strategy polls entire `signals.jsonl` on every candle and sizes via `wallet_balance * target_position_pct`, not target portfolio exposure/delta.  
  Failure mode: Large bus causes missed candle deadlines; repeated identical entry signals can create duplicate entries/DCA behavior or incorrect stake relative to existing exposure.  
  Proposed fix: Maintain indexed offset/cache or SQLite consumer cursor; enforce idempotency by signal id; translate `target_position_pct` into desired exposure minus current exposure using freqtrade position APIs.

[P0] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: Halt semantics are under-specified and contradictory: ADR says halt persists until cleared, ADR-0008 says “next non-halt signal lifts the halt.”  
  Failure mode: After drawdown halt, next normal daemon signal causes consumer to resume trading while daemon/plugin still thinks halt persists.  
  Proposed fix: Make halt a separate account/asset state with monotonic epoch; only explicit `hermes quant resume` emits a signed/typed resume event. Non-halt trading signals must never clear halt.

[P0] ADR-0003 Aggregator design — Bayesian baseline + logistic stacking, RL deferred  
  Issue: `bma_aggregate()` is not Bayesian model averaging; it is confidence-weighted voting with ad-hoc accuracy weights.  
  Failure mode: Users trust “Bayesian” risk claims; weights are not posterior probabilities, do not model likelihood/calibration, and can overweight lucky/noisy analysts, producing false confidence.  
  Proposed fix: Rename to `weighted_vote` or implement real BMA: calibrated predictive distributions per analyst, log-score likelihood updates, posterior model probabilities with priors and decay.

[P0] ADR-0004 Risk gate — deterministic rules, silence-by-default  
  Issue: “Kelly” formula is mathematically wrong: `magnitude * confidence / volatility` is not Kelly and has unit mismatch. Kelly for returns depends on expected excess return over variance or win probability/payoff.  
  Failure mode: Low volatility assets get oversized positions from tiny unproven edges; high-vol assets get undersized; risk cap masks but still causes systematic mis-sizing and false safety.  
  Proposed fix: Rename to volatility-scaled sizing or implement fractional Kelly as `f*=mu/sigma^2` with robust covariance/edge estimates, confidence shrinkage, hard minimum sample requirements, and live-vs-paper validation.

[P0] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Fallback provider chains can silently mix delayed yfinance data with live ccxt/alpaca data.  
  Failure mode: Primary rate-limits during live trading; daemon falls back to 15-minute delayed/semantically different data and emits stale signals as if live.  
  Proposed fix: Mark every bar with provider, delay, and freshness; forbid delayed fallbacks in live mode unless explicitly enabled; gate refuses trading on stale/mixed-provider data.

[P0] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Data validation drops zero-volume bars universally.  
  Failure mode: Valid crypto/equity intervals with zero volume or exchange outages are removed, collapsing time alignment and creating look-ahead/incorrect returns over horizons.  
  Proposed fix: Preserve regular bar grid; flag halted/missing/zero-volume bars instead of dropping; analysts/gate decide whether to skip.

[P0] ADR-0003 Aggregator design — Bayesian baseline + logistic stacking, RL deferred  
  Issue: Settlement and training labels are not tied to actual executed positions/fills. Realized outcomes use market returns over horizon, not realized P&L after slippage/fees/partial fills.  
  Failure mode: Aggregator learns signals that look directionally correct but lose money after execution friction; live performance diverges from telemetry.  
  Proposed fix: Persist fill-linked outcomes: decision price, intended target, executed size, fees, slippage, realized net P&L, missed fills; train/weight on net executable returns.

[P0] ADR-0001 Sidecar architecture — daemon decoupled from gateway  
  Issue: No real-money global kill switch/dead-man-switch across daemon and freqtrade.  
  Failure mode: Daemon stalls while freqtrade keeps stale positions/orders; plugin is read-only and cannot stop execution; losses continue unattended.  
  Proposed fix: Consumer-side dead-man switch: if no fresh heartbeat/signal within N intervals, cancel orders and flatten or stop entries per config. Add `hermes quant emergency-stop` CLI that affects both daemon and consumer.

[P1] ADR-0001 Sidecar architecture — daemon decoupled from gateway  
  Issue: Tick/state/log paths are inconsistently profile-aware; signals and DB paths are global while logs mention profiles.  
  Failure mode: Multiple Hermes profiles or accounts share `~/.hermes/quant/signals.jsonl`; paper/live or exchange accounts contaminate signals.  
  Proposed fix: Make all state paths profile/account scoped by default: `~/.hermes/profiles/<profile>/quant/<account>/...`; include account id in signal schema.

[P1] ADR-0007 Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop  
  Issue: CLI commands are inconsistent across ADRs. ADR-0001 lists `{setup,start,stop,restart,backtest,signals,doctor}`; ADR-0004 requires `resume`; ADR-0008 requires `freqtrade-setup` and `freqtrade-backtest`; ADR-0007 omits `resume`, `freqtrade-*` from tree.  
  Failure mode: Docs/tests/users call commands that do not exist; halt cannot be cleared or freqtrade cannot be wired.  
  Proposed fix: Define one authoritative CLI spec and generate docs/tests from it; include `resume`, `freqtrade-setup`, `freqtrade-backtest`, and explicit aliases/deprecations.

[P1] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: Stale-signal threshold formula is wrong: `max(timeframe_seconds * 2, 600)` gives 2 hours for 1h candles, while text says “at most 2 bars or 10 minutes, whichever larger.”  
  Failure mode: Freqtrade can execute a 119-minute-old 1h signal after regime change.  
  Proposed fix: Use per-timeframe `max_signal_age=min(timeframe_seconds/2, configured_cap)` or explicit defaults; document and test.

[P1] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: Freqtrade is spot-long-only by default (`can_short=False`) but signal contract supports negative target positions.  
  Failure mode: Short signals are ignored or misinterpreted as exits; backtest/live behavior diverges from daemon’s assumed NAV allocation.  
  Proposed fix: Add mode field/capabilities negotiation: spot-long-only maps negative target to zero/exit, margin futures enables shorts; daemon risk gate must know consumer capabilities before emitting.

[P1] ADR-0002 Analyst protocol contract  
  Issue: Frozen dataclass contains mutable `extras: dict`, `metadata: dict`; “pure modulo randomness” cannot be enforced.  
  Failure mode: Analysts mutate shared context/extras across threads, causing nondeterministic signals and corrupt views.  
  Proposed fix: Use immutable mappings/deep copies per analyst; validate no mutation in tests; serialize analyst inputs for replay.

[P1] ADR-0002 Analyst protocol contract  
  Issue: ThreadPoolExecutor with pandas DataFrames and stateful analysts has no thread-safety contract.  
  Failure mode: Rolling caches/calibration histories are updated/read concurrently, yielding inconsistent views or crashes.  
  Proposed fix: One analyst instance per worker or per asset/timeframe; require `analyze()` side-effect-free; protect `update()` with locks and serialize update vs analyze.

[P1] ADR-0003 Aggregator design — Bayesian baseline + logistic stacking, RL deferred  
  Issue: Disagreement penalty ports PDR “silence by default” incorrectly. Variance over `[-1,0,1]` penalizes calibrated contrarian or neutral specialists and can zero confidence arbitrarily.  
  Failure mode: Ensemble goes silent exactly when specialist analysts disagree for valid regime reasons, or confidence stays high when all are confidently wrong together.  
  Proposed fix: Penalize predictive distribution entropy and historical conditional accuracy by regime; distinguish abstention from opposite forecast; calibrate penalty on out-of-sample P&L.

[P1] ADR-0003 Aggregator design — Bayesian baseline + logistic stacking, RL deferred  
  Issue: Stacking feature vector has fixed “per analyst” shape but entry-point analysts are dynamic and may be missing/timeout.  
  Failure mode: Weekly refit/checkpoint feature order differs from live inference; model consumes shifted columns and emits bogus probabilities.  
  Proposed fix: Persist feature schema with model; deterministic analyst ordering; explicit missing-view indicators; reject inference if schema mismatch.

[P1] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Cache file path uses raw asset symbols like `BTC/USDT`; slash creates nested paths and collisions across exchanges/providers.  
  Failure mode: Binance BTC/USDT and Coinbase BTC/USDT caches mix or overwrite; filesystem path bugs on special symbols.  
  Proposed fix: Normalize path components with URL-safe encoding and include exchange/feed/account/provider/schema version.

[P1] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Parquet cache writes have no atomic replace/reader lock.  
  Failure mode: Backtest or daemon reads half-written parquet during refresh and crashes or uses corrupted data.  
  Proposed fix: Write to temp file, fsync, atomic rename; file locks around write; readers retry on transient parquet errors.

[P1] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Rate limits are provider-level, not exchange/account/IP scoped; ccxt limits vary by endpoint and exchange.  
  Failure mode: Multi-asset polling trips bans or HTTP 429; fallback to stale providers emits poor signals.  
  Proposed fix: Exchange-specific adaptive limiter using ccxt metadata/headers; exponential backoff; scheduling budget per asset/timeframe.

[P1] ADR-0004 Risk gate — deterministic rules, silence-by-default  
  Issue: `Portfolio.drawdown_pct` and `daily_loss_pct` from realized P&L ignore unrealized losses.  
  Failure mode: Open position can be down beyond max drawdown but breaker does not trigger until closed/settled.  
  Proposed fix: Compute risk limits from mark-to-market equity including open positions, pending orders, fees, and slippage estimates.

[P1] ADR-0004 Risk gate — deterministic rules, silence-by-default  
  Issue: Rule 4 cooldown runs before cost/size and uses volatile in-memory-ish portfolio state; restart semantics unspecified.  
  Failure mode: Daemon restart clears/incorrectly computes cooldown, allowing immediate revenge trade after loss.  
  Proposed fix: Persist loss/cooldown events transactionally and evaluate from durable fill log.

[P1] ADR-0006 RL aggregator deferred to v0.2 with concrete success criterion  
  Issue: Graduation criteria are statistically flawed/ambiguous: DSR “p-value vs BMA” is not standard as written; ≥12 folds may be highly dependent; paper-trade telemetry is non-stationary and strategy-influenced.  
  Failure mode: RL is promoted on invalid significance, then fails live.  
  Proposed fix: Specify exact hypothesis tests, purging/embargo, multiple-testing correction across hyperparameter sweeps, benchmark paired tests on net P&L, and pre-registered evaluation windows.

[P1] ADR-0006 RL aggregator deferred to v0.2 with concrete success criterion  
  Issue: Shuffle-timestamp test can pass while look-ahead remains via cached future bars, settlement labels, or provider backfill.  
  Failure mode: CI gives false confidence; backtest performance leaks future data.  
  Proposed fix: Add point-in-time data snapshots, feature timestamp audits, horizon embargo tests, and “truncate-at-t” replay tests for every analyst.

[P1] ADR-0007 Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop  
  Issue: Tools read daemon state directly but no read consistency contract is specified.  
  Failure mode: `quant_show_signals` reads JSONL while SQLite mirror has not committed matching action/components; user sees contradictory state and resumes/trades incorrectly.  
  Proposed fix: Prefer SQLite transactional reads for tools; if JSONL is shown, include last durable sequence number and consistency status.

[P2] ADR-0001 Sidecar architecture — daemon decoupled from gateway  
  Issue: `Restart=on-failure` with no start-limit/backoff beyond 30s can thrash APIs after persistent config/provider failure.  
  Failure mode: User hits provider bans and fills logs/disk.  
  Proposed fix: Add systemd `StartLimitBurst`, `StartLimitIntervalSec`, health-based fatal exits, and alerting.

[P2] ADR-0002 Analyst protocol contract  
  Issue: Confidence calibration is mandated but v0.1 ships uncalibrated and only warns after 30 days.  
  Failure mode: Risk gate uses decorative confidence for sizing/cost threshold during the most dangerous initial period.  
  Proposed fix: Apply conservative default confidence caps per analyst until minimum outcome count and ECE threshold are met.

[P2] ADR-0008 Freqtrade integration via signal bus (sidecar consumer)  
  Issue: JSONL bus has no retention/rotation/indexing plan despite append-only consumer.  
  Failure mode: Months of multi-asset 1m signals make strategy startup/polling slow and disk usage unbounded.  
  Proposed fix: Rotate by date/profile, maintain compact “latest signal per pair” state file/table, archive old logs, and have consumer read latest-state not full history.

[P2] ADR-0005 Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities  
  Issue: Claims PyArrow is a transitive dependency via `pandas[parquet]`; this is not guaranteed unless dependency is explicitly declared.  
  Failure mode: Cache/backtest fails at runtime on minimal installs.  
  Proposed fix: Declare `pyarrow` or `fastparquet` explicitly in package extras/base requirements used by quant.

[P2] ADR-0007 Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop  
  Issue: “Only paper-trade goes through agent surface” conflicts with tools being read-only and freqtrade/live execution being outside agent surface.  
  Failure mode: Users misunderstand safety boundary and assume agent cannot influence live system while CLI/config can.  
  Proposed fix: Clarify: no trading actions through tools; live mode requires explicit CLI setup and consumer config; display live/paper mode prominently in every status output.

**BLOCK v0.1.0** — catastrophic bus atomicity, singleton, halt/circuit-breaker, sizing, stale-data, and consumer semantics must be fixed before real or paper money execution.
