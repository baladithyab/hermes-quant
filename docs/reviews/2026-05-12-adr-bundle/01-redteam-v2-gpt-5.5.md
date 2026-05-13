[P0] ADR-0009 Confidence calibration / Kelly edge semantics
  Issue: The amendment calibrates `confidence` as probability of directional correctness, but the risk gate uses `edge = magnitude * confidence`. A 50% calibrated probability still produces positive edge and positive Kelly size.
  Failure mode: A no-skill signal with `magnitude=1%`, `confidence=0.50` is treated as `0.5%` expected edge, can pass cost gates, and can size positions. This reintroduces the “confidence used as probability/edge” bug in a subtler form.
  Proposed fix: Define the tradable expected edge explicitly. If `confidence = P(direction correct)`, use `edge = abs(magnitude) * max(0, 2 * confidence - 1)` for cost/Kelly, with sign from `direction`; or redefine `magnitude` as already-unconditional expected signed return and stop multiplying by confidence. Add tests that `p=0.5` yields zero tradeable edge.

[P0] ADR-0009 Heartbeat / dead-man-switch bootstrap hole
  Issue: `_check_dead_man_switch()` returns forever when `_last_heartbeat is None`.
  Failure mode: If freqtrade starts/restarts while the daemon is already dead, or if the bus path is wrong, existing open trades are not flattened and new entries may proceed from stale signals until other guards happen to stop them.
  Proposed fix: Require a fresh heartbeat before any entry. If no heartbeat is observed within a bootstrap grace window and there are open trades/orders, cancel/flatten and enter safe-stop. Use wall-clock time for live mode; disable or simulate heartbeats explicitly in backtests.

[P0] ADR-0009 Emergency-stop is not durable halt
  Issue: `hermes quant emergency-stop` writes an `emergency_stop` signal and cancels via broker API, but does not create durable halt-state in the `halts` table / `halt_state.json`.
  Failure mode: After cancellation, the next daemon tick or consumer restart can resume entries because no durable halt blocks trading.
  Proposed fix: Emergency-stop must atomically insert a durable all-scope halt, update `halt_state.json`, bump halt epoch, then cancel/flatten broker-side. Resume only via explicit `hermes quant resume`.

[P0] ADR-0009 JSONL atomic-write protocol
  Issue: The amendment claims POSIX `PIPE_BUF` atomicity for regular files. `PIPE_BUF` applies to pipes/FIFOs, not regular files. Also there are now multiple writers to `signals.jsonl` (`daemon`, `emergency-stop`) and likely to `executions.jsonl`, but only `signals` has a partial protocol.
  Failure mode: Concurrent appends can interleave or be partially observed, producing corrupt records, missed halts, missed fills, or consumer parse-error safe-stops.
  Proposed fix: Use an interprocess lock (`flock`) around every bus append, or funnel all writes through a single daemon-owned append service. Apply the same framing/locking/fsync protocol to `executions.jsonl`, `signals.jsonl`, and heartbeat/emergency records. Do not cite `PIPE_BUF` for regular files.

[P1] ADR-0009 Daemon singleton lock implementation
  Issue: `DaemonLock.acquire()` opens the lock file with `O_TRUNC` before acquiring the lock, erasing the live holder’s PID. It also references `self.account_id` without setting it.
  Failure mode: `quant_doctor` and startup diagnostics cannot reliably report the owning PID; the stale-lock branch can throw confusing/unhandled errors instead of a clean “already running”.
  Proposed fix: Store `self.account_id`; open without truncation first, acquire lock, then truncate/write PID only after lock acquisition. For held locks, read PID from a separate non-truncated metadata file or seek/read before any truncate.

[P1] ADR-0009 Halt table nullable primary key
  Issue: `halts.asset` is nullable but included in a composite primary key. SQLite permits surprising duplicate semantics with `NULL` in composite keys.
  Failure mode: Multiple “same scope” halts with `asset NULL` can coexist ambiguously; consumer halt mirrors may miss or duplicate active halts.
  Proposed fix: Make scope columns `NOT NULL` and use `'*'` for wildcard asset/class/account, or use `WITHOUT ROWID` plus explicit uniqueness constraints that normalize null scope values.

[P1] ADR-0002 / ADR-0009 AnalystView versioning break
  Issue: `confidence_raw` is added as a required field with no default, violating ADR-0002’s add-only-with-default versioning rule.
  Failure mode: Existing/third-party analysts compiled against v1 fail constructing `AnalystView`.
  Proposed fix: Add `confidence_raw: float | None = None` initially, or provide a compatibility constructor that sets `confidence_raw = confidence` with a deprecation warning.

[P1] ADR-0003 / ADR-0009 Aggregator update contract mismatch
  Issue: ADR-0003’s `Aggregator.update(outcomes: list[RealizedOutcome])` remains, while ADR-0009 says aggregators consume `EpisodeOutcome`.
  Failure mode: Implementers cannot know which signature is authoritative; stacking/RL training code will not type-check or interoperate.
  Proposed fix: Supersede the protocol explicitly: `def update(self, episodes: list[EpisodeOutcome]) -> None`, or split `update_realized()` vs `update_episode()`.

[P1] ADR-0008 / ADR-0009 Backtest consistency field missing
  Issue: The CI gate requires every signal’s `decision_price` to match the bar close, but the signal schema in ADR-0008/0009 does not add `decision_price`.
  Failure mode: The proposed consistency test cannot be implemented against the declared schema; backtest data mismatch can persist silently.
  Proposed fix: Add required `decision_price`, `bar_timestamp`, and `data_source` fields to backtest signal records, or change the test to derive price from a declared persisted tick table.

[P1] ADR-0004 / ADR-0009 Slippage adaptation sign bug
  Issue: Rolling slippage is defined as `(fill_price - decision_price) / decision_price` without side/adverse normalization.
  Failure mode: Sell-side adverse slippage appears negative and can reduce the 90th percentile estimate, understating costs.
  Proposed fix: Store adverse bps by side: buys `(fill - decision)/decision`, sells `(decision - fill)/decision`; use positive adverse slippage distribution.

[P2] ADR-0009 CLI resume audit gap
  Issue: Halt semantics say resume requires confirmation prompt plus reason text, but canonical CLI has no `--reason` / `--note`.
  Failure mode: Resumes are unauditable or implementation-specific.
  Proposed fix: Add `hermes quant resume ... --reason TEXT` or document interactive required reason capture.

v1 P0-1: Kelly formula off by factor of σ
  Status: PARTIALLY-ADDRESSED
  Notes: σ² correction is present, but tradable edge still incorrectly uses `p` instead of excess probability over chance.

v1 P0-2: Confidence uncalibrated but used as probability
  Status: PARTIALLY-ADDRESSED
  Notes: Isotonic calibration is added, but confidence semantics still create positive edge at 50%, and `confidence_raw` breaks compatibility.

v1 P0-3: Split-brain portfolio state
  Status: PARTIALLY-ADDRESSED
  Notes: Execution back-channel and reconciliation are good directionally, but bus atomicity and broker/consumer fill authority remain underspecified.

v1 P0-4: Halt semantics contradictory
  Status: PARTIALLY-ADDRESSED
  Notes: Durable halt model is better, but emergency-stop does not create durable halt and SQLite null scope is unsafe.

v1 P0-5: Risk-gate ordering bug
  Status: ADDRESSED
  Notes: Circuit breakers now precede flat-signal early return.

v1 P0-6: No daemon singleton enforcement
  Status: PARTIALLY-ADDRESSED
  Notes: Locking is added, but the implementation truncates before lock acquisition and has a missing field bug.

v1 P0-7: No global kill-switch / dead-man-switch
  Status: PARTIALLY-ADDRESSED
  Notes: Heartbeats and emergency-stop exist, but no-heartbeat bootstrap and non-durable emergency halt are P0 holes.

v1 P0-8: JSONL bus has no atomic write protocol
  Status: NEW-BUG-INTRODUCED
  Notes: The proposed fix relies on an invalid `PIPE_BUF` guarantee for regular files and ignores multi-writer cases.

v1 P1-9: Cross-asset contagion in circuit breakers
  Status: ADDRESSED
  Notes: Portfolio/risk partitions by `(account_id, asset_class)` address the main contagion issue.

v1 P1-10: Stacking data starvation
  Status: PARTIALLY-ADDRESSED
  Notes: `EpisodeOutcome` is added, but the aggregator protocol signature was not updated.

v1 P1-11: CLI surface inconsistent across ADRs
  Status: ADDRESSED
  Notes: ADR-0009 gives a canonical CLI list, with only minor audit-text gaps.

v1 P1-12: Slippage defaults too optimistic
  Status: PARTIALLY-ADDRESSED
  Notes: Defaults are improved, but adaptive slippage must normalize adverse cost by trade side.

v1 P1-13: Walk-forward CV embargo unspecified
  Status: ADDRESSED
  Notes: Explicit embargo rule is now specified.

v1 P1-14: Backtest data mismatch
  Status: PARTIALLY-ADDRESSED
  Notes: Single parquet handoff is specified, but required `decision_price` schema is missing.

v1 P1-15: Sharpe targets ignore funding/borrow
  Status: ADDRESSED
  Notes: Net-of-cost Sharpe now includes funding, borrow, commission, spread, and slippage.

v1 P2-16: BMA→RL graduation Catch-22
  Status: ADDRESSED
  Notes: The BMA Sharpe prerequisite is removed and replaced with paired comparison / FDR criteria.

**MAINTAIN BLOCK** — ADR-0009 fixes many structural issues, but reintroduces P0 risk through incorrect probability-to-edge math, unsafe bus atomicity assumptions, and incomplete kill-switch durability.
