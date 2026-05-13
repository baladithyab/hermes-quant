# ADR-0008: Freqtrade integration via signal bus (sidecar consumer)

**Status**: proposed
**Date**: 2026-05-12

## Context

ADR-0001 established sidecar architecture: hermes-quant emits signals to a bus; an external execution engine consumes them. This ADR specifies the v0.1 consumer (freqtrade) and the signal contract that future consumers (NautilusTrader, custom Alpaca executor) will also implement.

Per `docs/research/02-framework-integration.md` §1-2, freqtrade is the obvious v0.1 choice for crypto execution because:

- Mature (5+ years), GPL-3.0
- CCXT-based; supports Binance, Kraken, Coinbase, OKX, etc.
- Strong paper-trade ("dry-run") support
- Battle-tested order management (trailing stops, partial fills, retry on broker errors)
- The user already has it cloned at `/mnt/e/CS/github/freqtrade`

It is NOT the right choice for v0.2 (US equities via Alpaca) — freqtrade is crypto-only. NautilusTrader will be the v0.2 consumer for equities. Same signal bus contract, different consumer.

## Decision

### Signal bus format

`~/.hermes/quant/signals.jsonl` — append-only newline-delimited JSON:

```json
{
  "schema_version": 1,
  "id": "sig-20260512T230545Z-BTC-USDT-1h-0001",
  "asof": "2026-05-12T23:05:45Z",
  "asset": "BTC/USDT",
  "exchange": "binance",
  "timeframe": "1h",
  "direction": 1,
  "magnitude": 0.018,
  "confidence": 0.72,
  "horizon": "4h",
  "target_position_pct": 0.10,
  "reason": "bma_aggregator_dir=1_conf=0.72",
  "halt": false,
  "components": [
    {"analyst": "kronos-small", "direction": 1, "magnitude": 0.022, "confidence": 0.78, "horizon": "1h"},
    {"analyst": "classical-ta", "direction": 1, "magnitude": 0.011, "confidence": 0.65, "horizon": "1d"},
    {"analyst": "microstructure-lite", "direction": 1, "magnitude": 0.014, "confidence": 0.70, "horizon": "1h"}
  ]
}
```

Fields:
- `schema_version`: integer; bumped on breaking changes. Consumers reject signals with unknown major version.
- `id`: globally unique signal id.
- `asof`: UTC timestamp of decision.
- `asset`, `exchange`, `timeframe`: routing.
- `direction`, `magnitude`, `confidence`, `horizon`: from the aggregator (post-gate).
- `target_position_pct`: from the risk gate. THIS is what consumers act on, not direction × magnitude.
- `reason`: human-readable string for log correlation.
- `halt`: if true, consumers MUST flatten this asset and stop accepting signals until cleared.
- `components`: per-analyst contribution (for debugging; consumers ignore).

### SQLite mirror

The same signals are also written to `~/.hermes/quant/state.db::signals` table for queryability. The JSONL is the canonical bus (consumers tail it); SQLite is for `quant_show_signals` and post-hoc analysis.

### Freqtrade consumer (v0.1)

`hermes_quant.consumers.freqtrade` ships:

1. `quant_consumer_strategy.py` — a freqtrade `IStrategy` that reads `signals.jsonl`. Drop into freqtrade's `user_data/strategies/`.

2. A `freqtrade_config.example.json` showing the freqtrade-side config (exchange, fees, timeframe, dry-run mode).

3. `hermes quant freqtrade-setup` — CLI wizard that:
   - Detects local freqtrade install (looks for `freqtrade` on PATH or asks for path)
   - Symlinks the strategy into the user's freqtrade `user_data/strategies/`
   - Generates `user_data/config.json` with hermes-quant-friendly defaults (1h timeframe, dry-run on, signal bus path resolved)
   - Prints next-step commands for the user to start freqtrade

The strategy itself:

```python
# quant_consumer_strategy.py — drops into freqtrade's user_data/strategies/
import json
from pathlib import Path
from freqtrade.strategy import IStrategy
from typing import Optional
import pandas as pd

class HermesQuantConsumer(IStrategy):
    """Reads signals from ~/.hermes/quant/signals.jsonl and executes.

    The strategy itself is intentionally trivial — all intelligence is upstream
    in the hermes-quant daemon. Position sizing, direction, and timing all come
    from the signal. Freqtrade owns: order placement, trailing stops, partial
    fills, exchange retries.
    """
    timeframe = "1h"
    can_short = False  # spot-only by default; flip for margin/futures pairs
    process_only_new_candles = True
    startup_candle_count = 0

    SIGNAL_BUS_PATH = Path.home() / ".hermes/quant/signals.jsonl"

    def populate_indicators(self, df, metadata):
        return df

    def populate_entry_trend(self, df, metadata):
        signals = self._load_signals(metadata["pair"])
        df["enter_long"] = self._mark_signals(df, signals, side="long")
        return df

    def populate_exit_trend(self, df, metadata):
        signals = self._load_signals(metadata["pair"])
        df["exit_long"] = self._mark_signals(df, signals, side="exit")
        return df

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake,
                            min_stake, max_stake, leverage, entry_tag, side, **kwargs):
        # Use signal's target_position_pct to size
        signal = self._latest_signal_for(pair, current_time)
        if signal is None:
            return 0
        wallet_balance = self.wallets.get_total_stake_amount()
        return min(max_stake, wallet_balance * signal["target_position_pct"])

    def _load_signals(self, pair: str) -> list[dict]:
        if not self.SIGNAL_BUS_PATH.exists():
            return []
        out = []
        with open(self.SIGNAL_BUS_PATH) as f:
            for line in f:
                try:
                    sig = json.loads(line)
                    if sig.get("schema_version") != 1:
                        continue
                    if sig["asset"] != pair:
                        continue
                    out.append(sig)
                except json.JSONDecodeError:
                    continue
        return out

    def _mark_signals(self, df, signals, *, side):
        # ... (timestamp matching + halt handling) ...
        pass
```

Real implementation has timestamp-aligned signal matching, halt handling, schema-version checking, and a "stale signal" guard that ignores signals older than `max_signal_age_minutes` (default 30 for 1h timeframe).

### Backtest path

Backtesting in this architecture:
1. `hermes quant backtest BTC/USDT --from 2024-01-01 --to 2026-01-01 --timeframe 1h` runs the daemon against historical bars and **emits a signal log** to `~/.hermes/quant/backtests/<run_id>/signals.jsonl`.
2. `hermes quant freqtrade-backtest --signals ~/.hermes/quant/backtests/<run_id>/signals.jsonl` invokes freqtrade's `backtesting` mode against the signal log.
3. Combined report stitches both: hermes-quant per-tick analyst views + aggregator decisions, and freqtrade's order-fill and PnL accounting.

This is the cleanest separation. The hermes-quant decision process is replayable; the execution is fidelity-tested by freqtrade's mature backtester.

### v0.2: NautilusTrader consumer for equities

The same signal bus contract works. NautilusTrader's `Strategy` class reads the JSONL, executes via Alpaca/IBKR. v0.2 ADR-0014 will detail this. Sidecar makes this drop-in.

### v0.3: Custom Alpaca executor (lightweight)

For users who don't want freqtrade or NautilusTrader weight, a lightweight `hermes_quant.consumers.alpaca_executor` that uses alpaca-py directly. ~200 LOC. Trades reliability for simplicity.

## Consequences

### Positive

- Hermes-quant doesn't reinvent order management. We ship signal generation; freqtrade ships execution. Each is best-in-class at its layer.
- Backtest path is robust: hermes-quant's signal log + freqtrade's backtester = production-grade reproduction.
- Bus is JSONL, not Redis/zmq — debuggable with `tail -f`, `jq`, and `grep`. No infra to manage.
- Same bus contract for v0.2 (NautilusTrader) and v0.3 (Alpaca executor) means consumers are interchangeable.

### Negative

- Two processes to run. Mitigated by `hermes quant doctor` showing both daemon and freqtrade health, and by `hermes quant freqtrade-setup` automating the wiring.
- JSONL polling has latency (typical: 1-2s). Fine for 5m+ timeframes; at 1m, the polling delay eats into the tick budget. Mitigated for v0.2 by adding an inotify-based file watcher.
- Schema versioning has a perpetual coordination cost between daemon and consumers. Schema-v2 means a coordinated release. Mitigated by consumers always checking `schema_version` and falling back gracefully (skip unknown versions, don't crash).
- Freqtrade is GPL-3.0 — using it as a downstream consumer is fine (sidecar architecture, separate process), but distributing a freqtrade-bundled hermes-quant in a non-GPL way is not. We don't bundle; users install freqtrade separately. README clarifies the licensing relationship.

## Implementation notes

- The strategy file is shipped under `hermes_quant/consumers/freqtrade/quant_consumer_strategy.py`.
- `freqtrade-setup` uses the user's freqtrade `user_data` directory; never modifies system files.
- A halt signal flushes all open positions for that asset via `force_exit_signal=True` in the strategy. The next non-halt signal lifts the halt.
- Stale signal threshold = `max(timeframe_seconds * 2, 600)` — at most 2 bars or 10 minutes, whichever is larger.
- The strategy is intentionally tiny (~100 LOC). The intelligence is upstream.

## References

- `docs/research/02-framework-integration.md` §1-2, 5 — framework comparison and recommendation
- ADR-0001 — sidecar architecture (this ADR specifies the consumer side)
- ADR-0005 — data layer (the daemon's input side)
- freqtrade docs: https://www.freqtrade.io/
- freqtrade-strategies repo for reference patterns


---

## Amendment 2026-05-13: Mirror staleness fallback + monotonic-clock heartbeat

**Status**: planned (v0.1.2)
**Date**: 2026-05-13

### Context

Phase-8 review of the v0.1.1 daemon surfaced two narrow but real correctness gaps that touch the consumer-side contract specified by this ADR. Both fixes are PLANNED for v0.1.2. Neither changes the signal-bus wire format or the freqtrade strategy's semantic contract; both harden the cross-process state surface that this ADR depends on.

- **Part A** (P1-ε): the `~/.hermes/quant/halt_state.json` mirror that the strategy reads can lag the SQLite-of-record across a daemon crash, opening a window where the strategy trades through an installed halt.
- **Part B** (P1-β): `HeartbeatChecker` computes elapsed time from wall-clock pandas Timestamps and is fooled by backward clock jumps (NTP correction, VM resume, container migration).

Money-software discipline (per `AGENTS.md`): "plausible" is not "safe". A halt that fails to apply is a missed circuit-breaker, and a heartbeat check that reports `daemon_alive=True` when the daemon is in fact dead is a missed dead-man-switch. Both cost capital.

### Part A — Halt mirror staleness fallback (v0.1.2)

This ADR establishes `~/.hermes/quant/halt_state.json` as the freqtrade strategy's fast-path read for halts, written by the daemon via atomic-rename so the strategy avoids SQLite lock contention. Phase-8 caught a small staleness window in the producer:

`HaltStateSQLite._write_mirror` (`hermes_quant/daemon/halt_state.py`) runs **after** the SQLite commit. If the daemon crashes between the commit and the atomic-rename, SQLite is correct (halt installed) but the mirror is stale (missing the new halt). On daemon restart, the freqtrade strategy reads the stale mirror and trades through the halt until the daemon next writes — which, in the worst case, is the next halt event.

Window size is typically <1ms. But a halt is a circuit-breaker; the cost of missing one is asymmetric, so v0.1.2 closes this consumer-side.

**v0.1.2 fix.** The strategy MUST cross-check `mirror.mtime` against `sqlite.mtime`. If `sqlite.mtime > mirror.mtime + STALENESS_TOLERANCE_S` (default 5s), the strategy falls back to a read-only SQLite open. SQLite WAL mode (already enabled per ADR-0005) allows concurrent readers without contending with the daemon's writer.

```python
STALENESS_TOLERANCE_S = 5.0  # generous; mirror writes are sub-ms but allow clock jitter

def get_halts_for_strategy() -> list[Halt]:
    mirror_path = QUANT_DIR / "halt_state.json"
    db_path = QUANT_DIR / "halts.db"
    try:
        mirror_mtime = mirror_path.stat().st_mtime
        db_mtime = db_path.stat().st_mtime
    except FileNotFoundError:
        return []  # neither exists -> no halts

    if db_mtime > mirror_mtime + STALENESS_TOLERANCE_S:
        # Mirror is stale beyond tolerance - read SQLite directly
        logger.warning("halt mirror stale by %.1fs, falling back to SQLite",
                       db_mtime - mirror_mtime)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return _read_halts_from_sqlite(conn)

    return _read_halts_from_mirror(mirror_path)
```

**Why mtime, not sequence numbers.** Cross-process coordination via shared filesystem mtime is the simplest robust signal available without adding a coordination primitive. Any halt write touches both files; the atomic-rename guarantees ordering on POSIX. Clock jitter on virtualized environments can cause sub-second mtime skew between two `stat()` calls on different inodes — hence the 5s tolerance, which is wide enough to swallow jitter and narrow enough that a real producer crash is detected on the next strategy poll. Sequence numbers would require a third coordination file and a write fence; not worth the complexity for a fallback path.

**Tests.** `tests/integration/test_halt_mirror_staleness.py` simulates the crash window: write SQLite, do **not** update the mirror (or write a stale mirror with backdated mtime), assert the strategy picks up the halt via the SQLite fallback path and that the warning fires.

### Part B — Monotonic-clock heartbeat (v0.1.2)

`HeartbeatChecker.check` (`hermes_quant/daemon/heartbeat.py:208-261`) computes elapsed time as `(now - last_heartbeat).total_seconds()` on wall-clock `pd.Timestamp` values. If the system clock jumps backward (NTP correction, VM resume, container migration), `now < last_heartbeat` → negative ages → `daemon_alive=True` indefinitely, even when the daemon is in fact dead.

**v0.1.2 fix.** Track elapsed time using `time.monotonic()`. Wall-clock `pd.Timestamp` is retained for log/audit `asof` fields (those need real-time stamps for correlation with bar data and signal logs).

**Implementation note.** Mechanical: swap `(now - last).total_seconds()` for `time.monotonic() - last_monotonic` in the elapsed-time math. Keep wall-clock for `asof`. No semantic contract change — bootstrap grace, dead-man-switch threshold, and the backtest synthetic-time path all stay identical.

**Tests.** `tests/unit/test_heartbeat_clock_skew.py` mocks `time.monotonic` to return increasing values while wall-clock walks backward; asserts `daemon_alive=False` once monotonic ages exceed threshold regardless of wall-clock direction.

### Cross-cuts

- **ADR-0001 (sidecar architecture).** Both fixes preserve the daemon ↔ strategy decoupling; no new IPC, no new shared state. Strategy still reads files; daemon still writes files.
- **ADR-0009 §P0-D (durable halt ordering).** Part A is the **consumer-side** enforcement of the producer-side ordering ADR-0009 specifies. The producer writes SQLite-then-mirror; the consumer trusts SQLite when the mirror is stale. The two halves close the loop.
- **Phase-8 P0-C (tick loop installs durable halt on `Action(halt=True)`).** Part A makes the strategy correctly observe those halts even across the producer crash window that P0-C alone does not cover.
- **Signal-bus contract (this ADR §"Signal bus format").** Unchanged. The `halt` flag on signals remains the inline mechanism; the mirror+SQLite path is the out-of-band durable mechanism for halts that outlive a single signal.

### Provenance

- Phase-8 synthesis: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md` §P1-ε (Claude P1) and §P1-β (Claude P1).
- Current implementations referenced: `hermes_quant/daemon/halt_state.py::_write_mirror` (Part A), `hermes_quant/daemon/heartbeat.py:208-261` (Part B).
