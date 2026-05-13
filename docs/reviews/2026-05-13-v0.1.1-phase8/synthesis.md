# Phase 8 cross-family review — hermes-quant v0.1.1

**Date**: 2026-05-13
**Artifact**: cumulative diff `5679e6e..81dc7ba` (14 commits, 8786 LOC)
**Bundle**: `/tmp/hq-phase8/bundle.md` (336 KB)
**Reviewers**: 3 cross-family via OpenRouter curl-bypass dispatcher
- `claude` → `anthropic/claude-4.7-opus-20260416` (BYOK, $0.00, 92s)
- `gemini` → `google/gemini-3.1-pro-preview-20260219` ($0.42, 238s, finish=length)
- `deepseek` → `deepseek/deepseek-v4-pro-20260423` ($0.29, 179s)
- **Total cost: $0.71 / wall-clock: 238s for slowest**
**Aggregator**: ARIA orchestrator (in-context aggregation per parallel-critique skill §6 Option B)

## Verdict tally

- 1× MERGE_WITH_FOLLOWUPS (claude)
- 2× BLOCK (gemini, deepseek)

The block votes are honest disagreements, not noise. Both flag the **same
chain of bugs** in the settlement/calibration pathway from independent
angles. Cross-family routing verified via `model_advertised` field
(curl-bypass works as designed; `delegate_task` route-fidelity bug avoided).

Note on `your_model` self-tag: claude wrote `claude-sonnet-4-5`, gemini wrote
`o1-2024-12-17`, deepseek wrote `claude-3.5-sonnet`. Per parallel-critique
Hard Rule #9, `your_model` is unreliable; `model_advertised` from the OR
top-level is the truth. All three reviewers were genuinely different model
families.

## Strong intersection (P0 must-fix)

### P0-A: Settlement/calibration loop poisoned by zero realized return

**Flagged by**: claude (P0), gemini (P0), deepseek (P0/P2) — **3-of-3 strong intersection**

**Root cause chain** (the most important Phase-8 finding):

1. `quant_consumer_strategy.py::order_filled` hardcodes
   `decision_price = float(rate) if not signal else float(rate)` — both
   branches return `rate` (the fill price). The ternary is degenerate.
2. The execution record is emitted with `decision_price == fill_price`.
3. `settlement_loop.construct_realized_outcomes` computes
   `realized_return = (fill_price - decision_price) / decision_price`
   for buys. **This is the slippage formula, not the directional return.**
4. With `decision_price == fill_price` from step 2,
   `realized_return == 0.0` for every fill.
5. `direction_correct = (sig_direction > 0 and realized_return > 0) or
   (sig_direction < 0 and realized_return < 0)` is **False for every long
   AND every short**, regardless of whether the trade was actually correct.
6. `BMAAggregator.update` increments only the `beta` (incorrect) counters.
   Per-analyst posterior accuracy drifts toward 0.
7. The cold-start calibrator never accumulates real correctness signal.
8. `RollingSlippageEstimator` (when wired up in v0.1.2) sees zero adverse,
   underestimates costs, the cost gate becomes too permissive.

**Severity**: this would silently corrupt every trained component the moment
the daemon starts running. The system would APPEAR functional (signals are
emitted, fills happen) while permanently destroying its own learning signal.
This is the canonical money-software defect: passes tests, breaks production.

**Mitigations needed (all three):**

- **P0-A.1** (`tick_loop.py::_build_signal_record`): persist `decision_price`
  on the bus signal record — currently absent. Use `last_close` from the
  `MarketContext` at signal emit time. Also persist `confidence_raw` for
  downstream calibrator training (already on AnalystView; needs to land on
  the record).
- **P0-A.2** (`quant_consumer_strategy.py::order_filled`): read
  `decision_price` from the cached signal record (`self._signal_cache[pair]
  ['decision_price']`), not from `rate`. Pass it through into the execution
  record.
- **P0-A.3** (`settlement_loop.py::construct_*`): the formula
  `(fill - decision)/decision` is **slippage**, not return. The "did the
  signal direction predict the realized horizon return" calculation is a
  DIFFERENT computation that needs the bar-close at `signal.asof + horizon`.
  For v0.1.1 we approximate by using the next-fill bar (the close-out
  execution) but this requires position tracking. **The honest v0.1.1
  resolution is to mark the calibrator update path as
  `confidence_correctness_pending=True` until v0.1.2 ships proper
  exit-fill joining.** Don't ship a calibrator with a broken signal.
- **P0-A.4** (`settlement_loop.py::direction_correct`): deepseek caught
  that for shorts, `realized_return = (decision - fill)/decision` is
  positive when the short PROFITS (price drops). The current
  `direction_correct` formula then evaluates `(sig_direction < 0 and
  realized_return < 0) → False` for a profitable short. **The sign
  convention is wrong.** Either flip the sell-side formula or make
  `direction_correct` always check `realized_return > 0` (since
  `realized_return` is already side-aligned by construction).

### P0-B: Risk gate cost-gate doesn't enforce sign-alignment with signal direction

**Flagged by**: deepseek (P0), claude (P2 — same root, downgraded)

`risk/gate.py::DefaultRiskGate.gate` Rule 5 silences when `abs(edge) <
threshold`. **A negatively-edged signal in a positive-direction request
(e.g., `signal.direction=+1` but the calibrated probability is so low that
`expected_signed_edge` is negative) passes the gate** when `|edge| >
threshold`. Then Rule 6's `quarter_kelly_size(edge=edge, direction=+1)`
multiplies the negative edge through, producing a NEGATIVE target_size.
Result: the gate emits an action OPPOSITE to the requested direction.

This is a real failure mode. Calibration shrinkage of 0.20 (cold-start)
means a raw confidence of 0.55 emits `confidence = 0.35`, which gives
`expected_signed_edge < 0` for a `direction=+1` signal. The gate would
then short on a long signal.

**Fix** (in `risk/gate.py::DefaultRiskGate.gate` between Rules 5 and 6):

```python
# Edge-sign alignment guard (synthesis-2026-05-13 P0-B)
if edge * signal.direction <= 0:
    self._n_silenced_cost_gate += 1
    return None
```

Or equivalently, redefine the cost gate as `if edge * signal.direction <
threshold` to combine threshold + sign in one check.

### P0-C: Tick loop emits halt-signals to bus but never installs durable halt

**Flagged by**: gemini (P0). Not flagged by claude/deepseek directly — but
their `confirmed_correct` claims about §P0-D ordering are NARROWER than
gemini's read.

`tick_loop.py::run_one_tick` calls `risk_gate.gate(...)` which can return an
`Action(halt=True, halt_scope=..., halt_until=...)` when drawdown or
daily-loss circuit breakers trip. **The tick loop emits the signal record
but never calls `halt_state.add_halt(...)`.** Result: the durable halt is
not committed to SQLite. On the next tick, the same drawdown reading would
re-fire the action; on daemon restart, the halt history is lost; other
assets in the same `(account_id, asset_class)` partition wouldn't be halted.

This **directly violates synthesis-v2 §P0-D ordering** ("durable halt FIRST,
then any other action"). Claude's `confirmed_correct` claim that "halt-first
ordering is correct in DefaultRiskGate.gate" is right at the gate level
(Rule 0 silences halted scopes) but **wrong at the tick-loop level** for
the OPPOSITE direction (gate emits halt action → tick loop should install
durable halt). The bug is NEW work the gate originates, not pre-existing
state.

**Fix** (in `tick_loop.py::run_one_tick` after `action = risk_gate.gate(...)`,
before `emit_signal_record`):

```python
if action.halt and action.halt_scope is not None:
    try:
        scope_account, scope_class, scope_asset = action.halt_scope
        # add_halt translates '*' wildcards via _normalize_scope; pass
        # them through verbatim
        halt_state.add_halt(
            account_id=scope_account if scope_account != "*" else None,
            asset_class=scope_class if scope_class != "*" else None,
            asset=scope_asset,
            reason=action.reason,
            halted_until=action.halt_until,
        )
    except ValueError:
        # Active halt already exists — fine, the gate's halt action is
        # idempotent in this case (signal still emitted for downstream).
        pass
```

## P1 (must triage; ship most as v0.1.1, file rest as v0.1.2)

### P1-α: portfolio_loader position-flip + partial-close logic is mathematically wrong

**Flagged by**: deepseek (P0 — both branches), claude (P1 — combined finding)

`portfolio_loader.py::reconstruct_portfolio` has two distinct bugs:

1. **Partial-close zeroing**: the `elif (old_qty != 0 and (signed_qty *
   old_qty < 0)) or new_qty == 0:` branch triggers on partial reductions
   (e.g., sell 0.5 BTC of a 1 BTC long). The condition `signed_qty *
   old_qty < 0` is true for ANY opposite-direction fill, regardless of
   magnitude. The branch then sets `positions_qty[asset] = 0.0`, wiping
   the remaining 0.5 BTC. Equity and drawdown calculations corrupt.

2. **Realized PnL sign on direction flip**: the first `if old_qty * new_qty
   < 0` branch computes `realized = (fill - avg_old) * closed_qty * (-1 if
   side == 'sell' else 1)`. For a long-to-short flip (sell), the multiplier
   is `-1`, giving `(avg_old - fill) * closed_qty` — the WRONG sign. A
   profitable long close-out registers as a loss.

**Fix**: rewrite with explicit cases:
- (a) same direction → average cost basis in
- (b) opposite direction, smaller |qty| → partial close at avg_old, retain remaining
- (c) opposite direction, equal |qty| → full close
- (d) opposite direction, larger |qty| → full close + reopen at fill (direction flip)

Add 8+ unit tests covering all four cases × both directions.

This is **P0 in spirit** but ships v0.1.2 because (a) v0.1.1 doesn't yet
wire portfolio reconstruction into the live tick loop (the
`_make_portfolio_factory` in tests returns empty portfolios; `main.py`'s
`portfolio_for` is wired but no fills happen until freqtrade is running),
and (b) the rewrite is non-trivial. **For v0.1.1 we mark
`reconstruct_portfolio` with a docstring warning + raise
`NotImplementedError` on direction-flip cases until v0.1.2.**

### P1-β: HeartbeatChecker uses wall-clock; vulnerable to NTP backstep / VM resume

**Flagged by**: claude (P1)

If system clock jumps backward (NTP correction, VM resume, container
migration), `now < last_heartbeat` produces negative ages. The checker's
`age = (now - last_heartbeat).total_seconds()` becomes negative, the
`age > dead_man_switch_seconds` check is False, and the daemon is reported
alive forever even when dead.

**Fix**: track elapsed time using `time.monotonic()`, keep wall-clock only
for log/audit `asof` fields.

This lands as v0.1.2. v0.1.1 is acceptable since the failure mode requires
adversarial / unusual clock conditions, but document in AGENTS.md.

### P1-γ: Strategy-side append_locked drops oversized records silently

**Flagged by**: claude (P1)

`quant_consumer_strategy.py::_emit_execution` checks `len(encoded) >
RECORD_BYTE_CAP` and **logs a warning + returns**, dropping the record. The
daemon-side `emit_execution_record` raises `SignalTooLarge`. Inconsistent
contract.

**Fix (v0.1.1)**: at minimum, log at ERROR level (not WARNING) and surface
the dropped record's signal_id in the log. The strategy can't easily raise
into freqtrade without crashing the strategy thread.

### P1-δ: `_next_session_open` returns next-UTC-day for tz='America/New_York'

**Flagged by**: claude (P1)

For an equity daily-loss circuit breaker triggered at 14:00 ET,
`halt_until = next-UTC-day 00:00 = 19:00 ET same day`, BEFORE next session
open at 09:30 ET next trading day. `auto_clear_expired` would lift the halt
during after-hours; pre-market access could re-trip.

**Fix (v0.1.1)**: for non-UTC tz, return `now + 24h` rather than
normalize-to-midnight, so it's at least bounded by "one full session"
regardless of when triggered. v0.1.2 will use `trading_calendars` for
proper session boundaries.

### P1-ε: JSON mirror staleness window between SQLite commit and atomic rename

**Flagged by**: claude (P1)

`HaltStateSQLite._write_mirror` runs AFTER the SQLite commit. If the
process crashes between commit and rename, the mirror is stale (missing
new halt). Freqtrade reads the mirror as fast-path.

**Fix (v0.1.2)**: have the strategy fall back to SQLite read if mirror's
mtime is older than the SQLite db's mtime. Or: emit a heartbeat-with-halts
record on the bus so the strategy doesn't depend on the mirror exclusively.

## P2 / questions / non-blockers (file as v0.1.2 backlog or document)

- (claude) Action.signal_id is unused — bus-record-mediated correlation works,
  but the field is dead. Either remove or thread through tick_loop.
- (claude) BMAAggregator posterior weights drift unboundedly with no decay —
  5-year-old correct call has same weight as yesterday's. Add half-life decay
  in v0.1.2 or v0.2.
- (claude) BMA `_get_or_create_stats` dict has no eviction; long-running
  daemon with experimental analysts could grow this dict.
- (claude) Settlement loop's `last_settled_record_count` resets on daemon
  restart — could double-update Beta posteriors on restart-mid-stream. Needs
  exec_id-based dedup.
- (claude) yfinance tz handling on international tickers not exercised in tests.
- (claude) macOS BSD flock vs Linux POSIX flock: not tested on macOS.
- (claude) `iter_jsonl_follow` doesn't detect file rotation — document that
  user's logrotate must not touch `~/.hermes/quant/*.jsonl`.
- (deepseek) `quarter_kelly_size` docstring claims "sign matches direction ×
  sign(edge)" but implementation uses `edge` directly; docstring is
  misleading.

## Confirmed correct (cited so future reviewers don't re-flag)

All three reviewers independently confirmed:

1. ✅ **Kelly numerator** — `expected_signed_edge` uses exact log form
   `p*log(1+m) + (1-p)*log(1-m)` with first-order fallback, NOT the buggy
   `p·m`. Single source of truth from `risk/kelly.py`; both cost gate and
   Kelly sizer call it. Test at p=0.6, m=0.01 documents the 3× overbet the
   buggy formula would produce.

2. ✅ **Synthesis-v2 §P0-D ordering in cmd_emergency_stop** — durable SQLite
   halt FIRST, then bus signal emit, then broker-cancel print. Verified by
   `test_emergency_stop_creates_durable_halt_first` which asserts textual
   ordering of stdout markers.

3. ✅ **Risk gate Rule 0 halt check** — first conditional, BEFORE drawdown/
   daily-loss. Halted scope returns None (silence), does NOT emit a flatten
   action. Tested by `test_halt_takes_priority_over_drawdown`.

4. ✅ **JSONL flock atomicity** — `signal_bus.append_locked` uses
   `fcntl.flock(LOCK_EX)`, fsyncs before unlock. Strategy has verbatim copy.
   Multi-process tests in `test_signal_bus.py` and `test_v0_1_1_e2e.py`
   spawn 4 concurrent writers and verify exact line counts (no corruption).

5. ✅ **Heartbeat bootstrap-grace** — `HeartbeatChecker.check` distinguishes
   bootstrap-active no-heartbeat (alive) from bootstrap-expired no-heartbeat
   (dead). Tested explicitly by `test_after_bootstrap_grace_no_heartbeat_dead`.

6. ✅ **Halt registry SQLite schema** — `'*'` sentinels NOT NULL, WITHOUT
   ROWID, PK includes halt_epoch. Verified by introspection tests querying
   PRAGMA table_info and sqlite_master.

7. ✅ **Side-aware slippage** — `compute_adverse_bps_signed` uses
   `(fill-decision)/decision` for buys and `(decision-fill)/decision` for
   sells; only positive (adverse) values persist into estimator. Symmetry
   test confirms.

8. ✅ **DaemonLock open-flock-truncate ordering** — opens with
   `O_RDWR|O_CREAT` (no O_TRUNC), then flock LOCK_EX|LOCK_NB, then
   ftruncate(0) and write PID. Synthesis-v2 §P1-α correct.

9. ✅ **Tools.py is read-only** — no mutating tool calls; trading happens
   through CLI only.

10. ✅ **Cross-process state via SQLite WAL** — CLI halt/resume/emergency-stop
    create their own HaltStateSQLite instance pointing to the same SQLite
    file. WAL is shared; daemon reads what CLI wrote on next is_halted
    query. No in-memory caching across the boundary.

## Disagreements (none)

All three reviewers converged on the same chain of bugs in the
settlement/calibration pathway. No reviewer-vs-reviewer disagreement
needing human resolution.

## Hallucinations / verified-false (per Hard Rule #14)

None observed. Every P0 finding cited code that I verified by re-reading
the source. The reviewers grounded their findings in concrete file:line
evidence with quoted code, which made false-positive triage cheap.

## Decision matrix — what lands in v0.1.1 vs v0.1.2

| Finding | Severity | Fix complexity | Ship in |
|---------|----------|----------------|---------|
| P0-A.1: persist `decision_price` on bus record | P0 | trivial | **v0.1.1** |
| P0-A.2: strategy reads `decision_price` from cache | P0 | trivial | **v0.1.1** |
| P0-A.3: settlement-loop calibration update gated until v0.1.2 | P0 | doc + early-return | **v0.1.1** |
| P0-A.4: `direction_correct` sign convention | P0 | one-line fix + test | **v0.1.1** |
| P0-B: risk gate edge-sign alignment guard | P0 | one-line fix + test | **v0.1.1** |
| P0-C: tick loop installs durable halt on Action(halt=True) | P0 | small + integration test | **v0.1.1** |
| P1-α: portfolio_loader rewrite | P0-spirit | non-trivial; tests | **v0.1.2** (gate v0.1.1 with NotImplementedError on flip) |
| P1-β: monotonic clock for heartbeat | P1 | mechanical | **v0.1.2** |
| P1-γ: strategy oversized-exec ERROR not WARN | P1 | trivial | **v0.1.1** |
| P1-δ: `_next_session_open` non-UTC tz fix | P1 | trivial | **v0.1.1** |
| P1-ε: mirror staleness fallback | P1 | small | **v0.1.2** |
| All P2 / questions / non-blockers | various | various | **v0.1.2 backlog** |

## Implementation plan for the v0.1.1 Phase-9 fixes

7 commits, in order of dependency:

1. `fix(daemon): persist decision_price + confidence_raw on bus record` — P0-A.1
2. `fix(freqtrade): strategy reads decision_price from signal cache` — P0-A.2
3. `fix(daemon): direction_correct sign convention for sells` — P0-A.4
4. `fix(daemon): gate calibrator updates until v0.1.2 (decision_price wired but exit-bar unknown)` — P0-A.3
5. `fix(risk): edge-sign alignment guard in cost gate` — P0-B
6. `fix(daemon): install durable halt when gate emits Action(halt=True)` — P0-C
7. `fix(daemon): NotImplementedError gate on portfolio_loader direction flips + minor P1 cleanups (P1-γ, P1-δ)`

Each commit cites the synthesis section. Then re-run full test suite; commit
the final state as ready-to-tag.

## Cost / time accounting

- 3 reviewers × 1 dispatch each via curl-bypass: $0.71 / 238s wall-clock
- Aggregator (in-context): ~5 min orchestrator time, no extra spend
- Verification + write-up: ~30 min orchestrator time
- 7 fix commits projected: ~2 hours implementation + tests
- v0.1.1 → v0.1.1 with Phase-8 fixes baked in: ~3 hours total

Compare to "ship v0.1.1 broken, discover the calibration loop is dead in
production": the bug class P0-A is silent (the system runs, just learns
nothing). Detection latency in production = days to weeks, depending on
how often the operator inspects per-analyst posterior weights. Phase-8
caught it in 4 minutes wall-clock. Pre-launch cross-family review on
money-software is non-negotiable.

## Provenance

- Bundle: `/tmp/hq-phase8/bundle.md`
- Reviewer raw outputs: `/tmp/hq-phase8/{claude,gemini,deepseek}.json`
- Reviewer extracted JSON: `/tmp/hq-phase8/{claude,gemini,deepseek}.extracted.txt`
- Dispatcher: `/tmp/hq-phase8/dispatch.sh` (customized from
  `~/.hermes/skills/autonomous-ai-agents/parallel-critique/scripts/phase8_review_dispatch.sh`)
- This synthesis: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md`
