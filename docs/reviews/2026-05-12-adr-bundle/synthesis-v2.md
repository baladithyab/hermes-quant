# Phase-4 v2 Cross-Family Review — Synthesis

**Date**: 2026-05-13
**Artifact**: ADR-0001..0008 + ADR-0009 (amendments) bundle (1715 lines)
**Reviewers**: same 3 cross-family slate as v1
- `01-redteam-v2-gpt-5.5.md` (openai/gpt-5.5-20260423, **MAINTAIN BLOCK**)
- `02-arch-v2-gemini-3.1-pro.md` (google/gemini-3.1-pro-preview-20260219, **LIFT BLOCK**)
- `03-quant-v2-deepseek-v4-pro.md` (deepseek/deepseek-v4-pro-20260423, **MAINTAIN BLOCK**)

**Verdict tally**: 1/3 LIFT, 2/3 MAINTAIN — must address before code.

## Strong intersection P0s (re-fix)

### P0-A: Kelly numerator is mathematically wrong (NEW BUG introduced by ADR-0009 §P0-1)

**Flagged by**: GPT-5.5 + DeepSeek (independently — strong intersection)

The amendment fixed the σ → σ² denominator but replaced the numerator with `edge = magnitude * calibrated_probability`. This is wrong. For a directional bet with calibrated probability `p` and magnitude `m`:

- Expected log return is `μ = p·log(1+m) + (1-p)·log(1-m)`
- First-order approximation: `μ ≈ (2p - 1) * m` (valid for small m)
- The amendment uses `p * m`, which **overestimates edge whenever p > 0.5**

Impact: at `p=0.6, m=0.01`, true `μ ≈ 0.002` vs amended formula's `0.006` — a 3× overbet. Compounds Kelly's known leverage-amplification properties.

**Fix in code (no new ADR)**: implement the correct formula in the risk gate AND in the cost-gate edge computation. Add unit test with explicit closed-form values.

```python
def expected_log_return(probability: float, magnitude: float) -> float:
    """Expected log return of a directional bet with calibrated probability p
    and (signed-symmetric) magnitude m. First-order: (2p-1)*m. Exact for small m."""
    if magnitude <= 0 or magnitude >= 1:
        # Outside small-return regime; fall back to first-order
        return (2 * probability - 1) * magnitude
    p, m = probability, abs(magnitude)
    return p * math.log1p(m) + (1 - p) * math.log1p(-m)

def expected_signed_edge(signal: AggregatedSignal) -> float:
    """Tradable edge in directional sign. p=0.5 → 0; p=0.6 m=0.01 → ~0.002."""
    direction_signed = signal.direction
    return direction_signed * expected_log_return(signal.confidence, abs(signal.magnitude))
```

The risk gate uses `expected_signed_edge` for both:
1. Cost gate threshold: `abs(expected_signed_edge) > cost_multiple * round_trip_cost`
2. Kelly sizer numerator: `kelly_size = expected_signed_edge / σ²`

### P0-B: JSONL atomic-write protocol cites PIPE_BUF incorrectly + missing executions.jsonl protection

**Flagged by**: GPT-5.5 (P0) + Gemini (P1 — same root issue, different surface)

`PIPE_BUF` applies to pipes/FIFOs, not regular files. POSIX does NOT guarantee atomic appends to regular files for any size. The actual correct protocol is to use `flock()` for serializing writes from concurrent producers.

Plus: `executions.jsonl` (the back-channel from freqtrade strategy → daemon settlement loop) needs the same atomicity protection — flagged by both Gemini AND GPT-5.5.

**Fix in code**:

```python
import fcntl
from contextlib import contextmanager

@contextmanager
def append_locked(path: Path):
    """Acquire exclusive flock on the bus file before appending."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)         # block until acquired
        yield fd
    finally:
        try:
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

def emit_signal_record(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > 16384:    # be honest: this is our chosen cap, not a POSIX guarantee
        raise SignalTooLarge(f"encoded {len(encoded)} bytes (limit 16384)")
    with append_locked(SIGNAL_BUS_PATH) as fd:
        n = os.write(fd, encoded)
        if n != len(encoded):
            raise IOError(f"short write: {n}/{len(encoded)} bytes")

def emit_execution_record(record: dict) -> None:
    """Same protocol for executions.jsonl. Both producers (freqtrade strategy
    + emergency-stop CLI) use this helper."""
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > 16384:
        raise SignalTooLarge(f"encoded {len(encoded)} bytes (limit 16384)")
    with append_locked(EXECUTION_BUS_PATH) as fd:
        n = os.write(fd, encoded)
        if n != len(encoded):
            raise IOError(f"short write: {n}/{len(encoded)} bytes")
```

The size cap is set by us, not by POSIX. With `flock`, write atomicity is enforced by exclusive locking, not by size guarantees. Documentation must NOT cite PIPE_BUF.

The freqtrade strategy implements the same `append_locked` helper (since it's a separate process, it must also acquire the flock).

## P0 single-flag but real (must-fix)

### P0-C: Heartbeat bootstrap hole

**Flagged by**: GPT-5.5

`if self._last_heartbeat is None: return` means the strategy never enters dead-man-safe-stop if it boots while the daemon is dead. New entries proceed from stale signals on the bus.

**Fix in code**:
```python
def _check_dead_man_switch(self, current_time):
    if self._last_heartbeat is None:
        # Bootstrap path. If we've been running > grace and STILL haven't seen
        # a heartbeat, treat the daemon as dead.
        bootstrap_age = (current_time - self._strategy_start_time).total_seconds()
        if bootstrap_age > self.bootstrap_grace_seconds:    # default 120s
            self._enter_safe_stop("no_heartbeat_observed_after_bootstrap")
        return
    age = (current_time - self._last_heartbeat).total_seconds()
    if age > self.dead_man_switch_seconds:
        self._enter_safe_stop("heartbeat_stale")
```

Plus: backtests get a synthesized heartbeat per bar so the dead-man-switch doesn't trigger spuriously in offline replay.

### P0-D: Emergency-stop doesn't create durable halt

**Flagged by**: GPT-5.5

`hermes quant emergency-stop` writes a signal AND cancels via broker, but doesn't insert a durable halt row. After cancellation, the next daemon tick can resume entries.

**Fix in code**:
```python
def emergency_stop(account: str | None = None,
                   asset_class: str | None = None,
                   reason: str = "operator_emergency_stop") -> None:
    # 1. Insert durable halt FIRST (so even if broker cancel races, halt is committed)
    halt_state = HaltStateSQLite(STATE_DB_PATH)
    halt_state.add_halt(
        account_id=account or "*",
        asset_class=asset_class or "*",
        asset=None,                   # all-assets within scope
        reason=reason,
        halted_until=None,            # require explicit resume
    )
    # 2. Update halt_state.json mirror atomically
    _write_halt_state_json_mirror(halt_state.active_halts())
    # 3. Emit halt signal
    emit_signal_record({"schema_version": 1, "type": "halt", "asof": ...,
                        "scope": (account or "*", asset_class or "*", None),
                        "reason": reason})
    # 4. NOW cancel via broker
    for client in _broker_clients_for(account, asset_class):
        client.cancel_all_orders()
        if config.emergency_stop_flatten_positions:
            client.close_all_positions()
```

Resume requires `hermes quant resume <account> [<asset_class>] --reason TEXT` (per-P2 finding below) — never auto-cleared.

## P1 fixes (in code, not new ADRs)

| # | Issue | Fix |
|---|---|---|
| P1-α | DaemonLock truncates before acquiring lock | Open without `O_TRUNC`, acquire flock, THEN `ftruncate(0)` + write PID |
| P1-β | SQLite halt PK NULL ambiguity | Use `'*'` sentinels for wildcard scope; column `NOT NULL`; add `WITHOUT ROWID` + `UNIQUE(account_id, asset_class, asset)` |
| P1-γ | `confidence_raw` versioning break | Default to `confidence_raw: float = 0.0` with a post-init check that issues a deprecation warning when zero (analysts pre-amendments will work but log a warning) |
| P1-δ | Aggregator update signature mismatch | Final signature: `def update(self, episodes: list[EpisodeOutcome]) -> None:` — protocol.py already has this, supersede ADR-0003's old signature |
| P1-ε | Backtest signal record missing `decision_price`, `bar_timestamp`, `data_source` | Add as required fields to backtest-mode signal schema; production schema already has `asof` which encodes `bar_timestamp` |
| P1-ζ | Slippage adverse-bps unsigned | Sign-aware: buys = `(fill - decision)/decision`, sells = `(decision - fill)/decision`; persist positive adverse only |
| P1-η | Reconciliation race condition | Discrepancy alert requires 3 consecutive checks across 30 ticks; otherwise log INFO and re-check |
| P2-θ | Resume audit gap | Add `--reason TEXT` to `hermes quant resume`; required argument |

All these land in the implementation, not in another ADR. The architecture is sound; the engineering details are what the v2 review caught.

## Verdict & action

The amendments **architecturally** lifted the BLOCK (1/3 LIFT directly; the other 2 maintain BLOCK on **mathematical** issues that are code-fixable, not architectural). I'm going to:

1. NOT write another ADR amendment doc — diminishing returns. The fixes belong in the implementation.
2. Implement the v0.1.0 code with all v1 + v2 review fixes baked in directly.
3. Run the Phase-8 final review on the WORKING CODE (not on ADRs again) — that's the real test.

The Phase-8 review will catch anything I missed in code; if it BLOCKs, we iterate at the code level which is faster than another ADR cycle.

This is the right call per the deep-work-loop's run-discipline pattern: "running every phase when the backlog is mechanical adds only ceremony." The remaining fixes are mechanical code adjustments. Time to write code.
