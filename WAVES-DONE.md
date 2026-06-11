# WAVES-DONE — autonomous position-management (Wave 3.5 + 4a + 5)

Branch: `feat/autonomous-position-management` · worktree `/tmp/wt-quant-automanage`
Contract: `/tmp/quant-design/REVISED-DESIGN.md`. PAPER-MODE, fail-closed, default-OFF.

This session: fixed the 4 Codex pre-merge findings (Wave 3.5), wired the exit
pass into an importable package seam (Wave 4a), and landed the three Wave-5 P1
fixes. Commits are SMALL (one per wave/fix-group) and NOT pushed. No rebase. The
running gateway/daemon was not touched. The DEPLOYED `~/.hermes/scripts/*` files
were NOT edited — required backports are spelled out at the bottom.

---

## Commits (this session, newest last)

| SHA | Wave | Summary |
|-----|------|---------|
| `dbe4da4` | 3.5 | fix(exits): close cumulative held + fill_price basis + state.db reconcile |
| `63ffdc8` | 4a | feat(autonomous): run_autonomous_cycle seam (exits-then-entries) |
| `7352c46` | 5 | feat(autonomous): mode_override + horizon-to-aggregate + disabled buckets |

Prior (already in the worktree before this session): `345d037`, `85531b3`,
`8ecefdc` (Waves 1–3).

---

## Wave 3.5 — the 4 Codex findings (`dbe4da4`)

### FIX-A [P1] — close the CUMULATIVE held, not the latest target snapshot
**File:** `hermes_quant/exits.py` (`_PosMeta.cumulative_fill`, `_recover_position_meta`, the per-symbol loop).
**Bug:** `reconstruct_portfolio_state` is latest-target-supersedes — every
`PaperReactor` add stamps `target = fill`, so two `+0.1` adds read `held = 0.1`.
But the settlement FIFO matcher (`settlement_loop.join_exit_fills`) nets the
cumulative signed `fill_size_pct` (= `+0.2`). Offsetting `-held = -0.1` marked
`target = 0` (the reader sees flat) yet left a `+0.1` ghost lot in the settlement
view → wrong realized P&L + a lot invisible to future reconstruct-based exit passes.
**Fix:** `_PosMeta` now carries `cumulative_fill` = sum of signed `fill_size_pct`
across the symbol's paper fills; the close offsets THAT, and `qty_sign`/`pnl`
follow the true cumulative direction.
**Proven by** (`tests/test_exits.py`):
- `test_exit_closes_cumulative_not_latest_target` — 2×`+0.1` adds then a stop appends `fill_size_pct=-0.20` (not `-0.10`) + `target=0.0`.
- `test_exit_nets_settlement_flat` — after the close, `join_exit_fills` leaves NO residual open lot for the symbol.

### FIX-B [P2] — evaluate thresholds against fill_price, not decision_price
**File:** `hermes_quant/exits.py` (`_recover_position_meta` entry-basis recovery).
**Bug:** with the v0.2 slippage model, `fill_price` is the actual entry settlement
realizes against; `decision_price` is the pre-slippage quote. Using `decision_price`
as the stop/clamp basis made the exit's `exit_pnl_pct` disagree with settlement.
**Fix:** the entry basis now recovers the positive `fill_price` first, falling back
to `decision_price` only for older records that lack a `fill_price`.
**Proven by** (`tests/test_exits.py`):
- `test_stop_evaluated_against_fill_price_not_decision_price` — entry `fill_price=120` (decision `100`), mark `95`: fires against fill_price (`-20.8%`), would NOT fire against decision_price (`-5%`).
- `test_threshold_falls_back_to_decision_price_when_no_fill_price` — legacy record with `fill_price=null` still evaluates.

### FIX-C [P2] — reconcile state.db after a non-dry exit
**File:** `hermes_quant/exits.py` (post-exit-loop reconcile block).
**Bug:** the direct append bypasses `PaperReactor.execute()` (which rebuilds
state.db), so after a stop closed a symbol the bus was flat but state.db / cash /
NAV still showed the stale open → kill-switch, status, sizing, admissibility ran
on stale state.
**Fix:** after a real exit, rebuild state.db via
`PortfolioState(state_db_path=quant_home/"state.db").reconstruct_from(bus)` — the
SAME call `ops/scripts/quant-flatten-paper-default.py` uses (@149–154). Guarded to
real exits that closed something; a reconcile failure is logged + alerted (added to
`ExitResult.alerts`), never crashes the exit — the bus is the source of truth.
**Proven by** (`tests/test_exits.py`):
- `test_non_dry_exit_reconciles_state_db` — after a non-dry exit, state.db no longer shows the closed symbol open.
- `test_dry_run_does_not_reconcile_state_db` — dry-run leaves state.db untouched.
- `test_state_db_reconcile_failure_does_not_crash_exit` — a raising `reconstruct_from` is non-fatal; the exit still stands.

**Test-helper change:** `_append_open` gained optional `fill_price` /
`fill_size_pct` / `proposal_id` kwargs (defaulting to the prior values), so the new
tests can model slippage and multi-add fills. All 25 pre-existing exits tests stay
byte-identical. **32 passed.**

---

## Wave 4a — run_autonomous_cycle seam (`63ffdc8`)

**File:** `hermes_quant/autonomous.py` — new `CycleResult` dataclass +
`run_autonomous_cycle(*, dry_run, symbols, advisor_recommend, marks_provider,
clock_provider, quant_home, mode_override)`.

Codex P1#1: the `manage_open_positions()` exit pass had **no production caller**.
`run_autonomous_cycle` is the ONE importable seam the cron + the
`quant_autonomous_tick` tool will call:

1. **Exits FIRST**, gated on the master flag `quant.autonomous.manage_positions`
   (default off → byte-identical no-op → the cycle is a plain `tick()`). The exit
   pass NEVER reads the kill-switch, so a tripped switch (which trips exactly when
   the book is bleeding) does NOT freeze losers open.
2. **Entries SECOND** with `exited_symbols` = the just-flattened symbols, so the
   entry loop skips them (no same-tick re-open, Q5) and frees their slots. `tick()`
   early-returns on a tripped switch → ENTRIES halt while the exits above still fired.

`dry_run` threads to both passes. `quant_home` defaults at CALL TIME to the module
`QUANT_HOME` (not def-time bound) so test isolation's monkeypatch holds.

**Proven by** (`tests/integration/test_autonomous_e2e.py`):
- `test_cycle_runs_exits_before_entries_and_blocks_reopen`
- `test_cycle_exits_run_under_tripped_kill_switch_entries_halt` — the load-bearing asymmetry
- `test_cycle_manage_positions_off_skips_exit_pass` — flag-off → bus untouched, entries unaffected
- `test_cycle_dry_run_honored_end_to_end`

**32 passed** (test_autonomous_e2e at this commit; 35 after Wave 5 adds 3 more).

**NOT done (deferred — operator review gate):** Wave 4b — brief-feeds-tick, the
`precomputed_advisor_results` tick() param, and the `quant-daily-interim.py`
rewire. The brief's `quant_approve` path is UNTOUCHED in this lane.

---

## Wave 5 — P1 fixes (`7352c46`)

### 5a — explicit `mode_override`, replacing the `_read_pdr_mode` monkeypatch
**Files:** `hermes_quant/autonomous.py` (tick + run_autonomous_cycle gained
`mode_override: str | None = None`), `ops/scripts/quant-autonomous-tick.py` (repo
copy: monkeypatch removed, passes `mode_override="autonomous"`).
The deployed cron monkey-patched `auto._read_pdr_mode = lambda: "autonomous"` at
**process scope** — leaking a forced mode to every other importer in the process
and breaking silently on a rename. `tick(mode_override="autonomous")` is scoped +
typed; `mode_override=None` (default) reads config → byte-identical.
**Proven by** (`tests/integration/test_autonomous_e2e.py`):
- `test_mode_override_runs_pipeline_without_config_mode`
- `test_mode_override_none_reads_config_byte_identical`
- `test_cycle_threads_mode_override`

### 5b — align trader holding horizon to the aggregate's detection horizon
**File:** `hermes_quant/agents/trader.py` (`_aggregate_horizon_to_days` +
`_build` precedence).
`TraderNode._build` derived `time_horizon_days` purely from the rating ladder, so a
5m-detected signal got a 30-day "Buy" hold (the horizon-contract mismatch). It now
prefers the aggregate's `AggregatedSignal.horizon` (carried through `advisor_signal`)
mapped to a representative day-count, falling back to the rating ladder then
`horizon_emphasis`. **Smaller than adding `signal_horizon`/`holding_horizon` schema
fields** — one file, no `ResearchPlan` change. Byte-identical when the signal carries
no horizon (every legacy call shape — `_signal_with_price` omits it).
**Proven by** (`tests/agents/test_trader.py`, class `TestTraderNodeHorizonAlignment`):
- `test_horizon_from_aggregate_takes_precedence[5m/1h/1d/1w/1M/1Q]`
- `test_no_aggregate_horizon_falls_back_to_rating_ladder` (byte-identical)
- `test_unrecognized_aggregate_horizon_falls_back`

### 5c — label disabled watchlist buckets "disabled", not active=0
**Files:** `hermes_quant/playbook/watchlist_evolution.py` (`_bucket_status` +
`status` field on each `per_play` summary entry, in both the empty-universe
early-return and the main builder), `scripts/quant-watchlist-evolve.py` (renders
the label).
A play with zero active rows now reads `status="disabled"` instead of the ambiguous
`active=0` — a different operational signal from a play with 3 active names.
Additive; existing `n_active` assertions unchanged.
**Proven by** (`tests/unit/test_watchlist_evolution.py`):
- `test_bucket_with_no_active_rows_is_labeled_disabled`
- `test_bucket_with_active_rows_is_labeled_active`
- `test_empty_universe_buckets_labeled_disabled`

---

## Verification (named files only — NOT the full suite)

Run with the repo venv, PYTHONPATH = worktree root:

```
PYTHONPATH=/tmp/wt-quant-automanage ~/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_exits.py \
  tests/integration/test_autonomous_e2e.py \
  tests/test_react_fill_size_invariant.py \
  tests/ops/test_quant_daily_interim_cap_safety.py \
  tests/state/test_portfolio_state.py \
  tests/agents/test_trader.py \
  tests/unit/test_watchlist_evolution.py
```

| File | Result |
|------|--------|
| tests/test_exits.py | 32 passed |
| tests/integration/test_autonomous_e2e.py | 35 passed |
| tests/test_react_fill_size_invariant.py | 13 passed |
| tests/ops/test_quant_daily_interim_cap_safety.py | 3 passed |
| tests/state/test_portfolio_state.py | 52 passed |
| tests/agents/test_trader.py | 91 passed |
| tests/unit/test_watchlist_evolution.py | 19 passed |
| **Total** | **245 passed, 0 failed** |

**Flag-OFF byte-identical invariant** asserted and holding:
`test_flag_off_is_byte_identical_noop` (exits master flag), the
`test_cycle_manage_positions_off_*` cycle no-op, `mode_override=None`, and the
no-aggregate-horizon trader fallback are each byte-for-byte unchanged vs. pre-change.
The re-homed fail-closed cap-safety fence (`tests/ops/...`) is intact.

---

## REQUIRED ORCHESTRATOR BACKPORTS to `~/.hermes/scripts/` (NOT done here)

These deployed scripts live OUTSIDE the worktree and were intentionally left
untouched. The orchestrator must backport the following AFTER this branch merges.

### 1. `~/.hermes/scripts/quant-autonomous-tick.py` — Wave 5a (drop the monkeypatch)

The deployed copy still has, around line 262:

```python
    auto._read_pdr_mode = lambda: "autonomous"  # type: ignore[attr-defined]
```

**Edit (a):** DELETE that line.

**Edit (b):** in the `auto.tick(...)` call (deployed ~line 382), add the
`mode_override` kwarg:

```python
        result = auto.tick(
            dry_run=not armed,
            symbols=entries,
            advisor_recommend=_direction_screened_recommend,
            mode_override="autonomous",          # <-- ADD
        )
```

The repo copy `ops/scripts/quant-autonomous-tick.py` already has exactly this
shape — diff it against the deployed copy and apply. (Requires the merged
`hermes_quant.autonomous` carrying the new `mode_override` param — i.e. backport
AFTER merge, not before.)

### 2. `~/.hermes/scripts/quant-autonomous-tick.py` — Wave 4a (call the seam)

To route the deployed cron through the new exits-then-entries seam, replace the
`auto.tick(...)` call above with `auto.run_autonomous_cycle(...)`:

```python
        cycle = auto.run_autonomous_cycle(
            dry_run=not armed,
            symbols=entries,
            advisor_recommend=_direction_screened_recommend,
            mode_override="autonomous",
        )
        result = cycle.tick_result          # downstream audit loop is unchanged
        exit_result = cycle.exit_result      # surface exits in the summary/audit
```

NOTE: this only fires exits when `quant.autonomous.manage_positions: true` is set
in config.yaml (default off = byte-identical to today: the cycle is a plain tick).
This is a firing-path change with exit side-effects under the flag — apply it as a
deliberate operator step (review a `--dry-run` cycle log first), NOT silently.
The repo copy keeps `auto.tick(...)` for now so this lane does not change the
deployed firing path; flip to `run_autonomous_cycle` in the same operator-reviewed
step that enables `manage_positions`.

### 3. `~/.hermes/scripts/quant-watchlist-evolve.py` — Wave 5c (render the label)

Backport the print-loop change from `scripts/quant-watchlist-evolve.py`: read
`stats.get("status", ...)` and print `disabled` instead of `active=0` for buckets
with no active rows. Cosmetic / operator-readability only; safe to apply anytime
after the `hermes_quant.playbook.watchlist_evolution` change (the new `status`
field) merges.

---

## Round-2 Codex fixes (newest, NOT pushed)

A Codex re-review of the Wave 3.5/4a/5 commits found 2 issues. Both fixed here,
TDD (failing test first), one small commit each, no push, no rebase. The running
gateway/daemon and the deployed `~/.hermes/scripts/*` were NOT touched.

| SHA | Pri | Summary |
|-----|-----|---------|
| `672437c` | P1 | fix(exits): blended size-weighted cost basis for cumulative exits |
| `d9bb0a2` | P2 | fix(autonomous): dry-run cycle suppresses would_exit entries |

### FIX-1 [P1] — BLENDED cost basis for cumulative exits (`672437c`)
**File:** `hermes_quant/exits.py` (`_PosMeta.blended_entry`, new `_fill_entry_basis`
helper, the `_recover_position_meta` walk, the per-symbol consumer).
**Bug (Codex, runnable repro):** FIX-A made the close QUANTITY cumulative
(`cumulative_fill` = sum of signed `fill_size_pct`), but the entry PRICE basis
still came from only the LATEST non-zero fill. A position added at two prices was
evaluated against the wrong entry. Repro: `+0.10@100` then `+0.10@200`, mark 170.
The true size-weighted blended basis is `(0.10*100 + 0.10*200)/0.20 = 150`, so
mark 170 is **+13% (a profit — must NOT stop out)**. The latest-fill basis (200)
read it as **-15%** and fired a stop, liquidating the whole `+0.20` book. A
false-exit money bug.
**Fix:** recover a SIZE-WEIGHTED blended entry basis over ALL opening legs —
`sum(basis_i * |fill_size_pct_i|) / sum(|fill_size_pct_i|)`. Each leg's basis is
FIX-B's precedence (positive `fill_price` first, `decision_price` fallback).
Closing legs (`target_position_pct == 0`) are excluded (their `fill_price` is the
EXIT mark, which would corrupt the entry basis); the exit pass only does full
closes, so a live position's legs are pure adds. `pnl_pct = qty_sign *
(mark/blended - 1)` with `qty_sign` from the TRUE cumulative direction (short side
correct). Fail-closed: a poisoned leg (no usable price/size), a zero total weight,
or a non-finite/`<=0` blend all yield `blended_entry=None` => the symbol is
skipped exactly like a bad mark — never a fabricated breach.
**Proven by** (`tests/test_exits.py`):
- `test_blended_basis_two_adds_profit_no_stop` — the exact Codex repro: blended
  150, mark 170 => +13% => NO exit.
- `test_blended_basis_two_adds_real_loss_exits` — blended 150, mark 120 => -20%
  => stop fires, closes full `-0.20`, `exit_pnl_pct == -0.20` (not the latest-fill
  `-0.40`).
- `test_blended_basis_short` — a short added at two prices evaluates pnl with the
  correct sign against the blended basis.
- the FIX-A cumulative-qty + settlement-flat tests stay green. **35 passed.**

### FIX-2 [P2] — dry-run cycle suppresses would_exit entries (`d9bb0a2`)
**File:** `hermes_quant/autonomous.py` (`run_autonomous_cycle` suppression set).
**Bug:** in `run_autonomous_cycle(dry_run=True)`, `manage_open_positions` appends
nothing so `exit_result.exited_symbols` is empty. Passing only `exited_symbols`
to `tick()` meant a symbol that WOULD exit AND is on the entry watchlist reported
a FIRE in the dry-run forecast, while the real (non-dry) cycle suppresses that
same-tick re-open. The forecast diverged from live.
**Fix:** `dry_run=True` => suppress `union(exited_symbols, would_exit)` (the set
the live cycle would flatten then suppress); `dry_run=False` => suppress
`exited_symbols` (the actually-flattened set — `would_exit` on the live path is
the cap-selected pre-append set and is NOT what got suppressed). The dry-run
forecast now predicts live behavior.
**Proven by** (`tests/integration/test_autonomous_e2e.py`):
- `test_dry_run_cycle_suppresses_entry_for_would_exit_symbol` — a would-exit
  symbol on the watchlist is `SILENCE_EXITED_THIS_TICK` in a dry-run cycle, not a
  FIRE.
- `test_non_dry_cycle_suppresses_only_actually_exited` — the live-path contrast:
  both paths agree on the suppressed symbol. **37 passed.**

### Round-2 verification (named files only)
```
PYTHONPATH=/tmp/wt-quant-automanage ~/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_exits.py \
  tests/integration/test_autonomous_e2e.py \
  tests/state/test_portfolio_state.py \
  tests/test_react_fill_size_invariant.py
```
| File | Result |
|------|--------|
| tests/test_exits.py | 35 passed |
| tests/integration/test_autonomous_e2e.py | 37 passed |
| tests/state/test_portfolio_state.py | 52 passed |
| tests/test_react_fill_size_invariant.py | 13 passed |
| **Total** | **137 passed, 0 failed** |

**Flag-OFF byte-identical invariant** still holds: `test_flag_off_is_byte_identical_noop`,
`test_cycle_manage_positions_off_skips_exit_pass`, `test_cycle_dry_run_honored_end_to_end`,
and `test_exited_symbols_default_none_is_unchanged` all green (the new suppression
set is only consulted when `manage_positions` is enabled and a symbol breaches;
default-OFF the cycle is still a plain `tick()`).

---

## Still deferred (NOT in this lane — operator review gate)

- Wave 4b: brief-feeds-tick, the `precomputed_advisor_results` tick() param, and
  the `quant-daily-interim.py` rewire. The brief's `quant_approve` path is
  untouched.
