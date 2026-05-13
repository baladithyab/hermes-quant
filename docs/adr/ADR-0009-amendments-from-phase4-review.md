# ADR-0009: Phase-4 cross-family review amendments to ADR-0001..0008

**Status**: proposed
**Date**: 2026-05-12
**Supersedes**: portions of ADR-0001 through ADR-0008 (specifically called out per finding)

## Provenance

This ADR consolidates 16 P0/P1 fixes from the Phase-4 cross-family review (`docs/reviews/2026-05-12-adr-bundle/synthesis.md`). The pattern follows the parallel-critique skill's "critique-driven revisions" rule: explicit table mapping each change to the source finding so any future reader can audit which review shaped which decision.

When this ADR is accepted (post Phase-4 v2 review pass), the amendments below are binding; the original ADR text remains as historical record but the amendments take precedence.

## Critique-driven revisions table

| Finding | Source | Action | Affects ADR |
|---|---|---|---|
| P0-1 Kelly formula off by factor of σ | DeepSeek + GPT-5.5 | Fix formula to `μ/σ²`, add unit test | ADR-0004 |
| P0-2 Confidence uncalibrated but used as probability | DeepSeek + GPT-5.5 + Gemini | Add isotonic calibrator + cold-start shrinkage | ADR-0002, ADR-0003 |
| P0-3 Split-brain portfolio state | Gemini + GPT-5.5 | Bidirectional bus: `executions.jsonl` back-channel + broker reconciliation | ADR-0001, ADR-0008 |
| P0-4 Halt semantics contradictory | GPT-5.5 | Halt is durable state, NEVER cleared by trading signals | ADR-0004, ADR-0008 |
| P0-5 Risk-gate ordering bug | GPT-5.5 | Circuit breakers FIRST, before signal-flatness check | ADR-0004 |
| P0-6 No daemon singleton enforcement | GPT-5.5 | POSIX advisory lock + PID liveness check | ADR-0001 |
| P0-7 No global kill-switch / dead-man-switch | GPT-5.5 | Heartbeat signal + consumer-side bounded action on staleness | ADR-0008 |
| P0-8 JSONL bus has no atomic write protocol | GPT-5.5 | Single-writer with `O_APPEND`, ≤4096 byte lines, fsync | ADR-0008 |
| P1-9 Cross-asset contagion in circuit breakers | Gemini | Partition Portfolio + RiskConfig per (account, asset_class) | ADR-0004 |
| P1-10 Stacking data starvation | Gemini | Add `EpisodeOutcome` (cross-sectional) alongside `RealizedOutcome` | ADR-0003 |
| P1-11 CLI surface inconsistent across ADRs | GPT-5.5 | Canonical CLI surface defined here; all ADRs cite this | ADR-0007 (+all) |
| P1-12 Slippage defaults too optimistic | DeepSeek | Crypto 12 bps, equities 5 bps, illiquid 25 bps | ADR-0004 |
| P1-13 Walk-forward CV embargo unspecified | DeepSeek | `embargo = max(forecast_horizon, 2 * timeframe)` | ADR-0006 |
| P1-14 Backtest data mismatch (yfinance vs ccxt) | DeepSeek + Gemini | Backtest mode forces single data source via parquet handoff | ADR-0008 |
| P1-15 Sharpe targets ignore funding/borrow | DeepSeek | All Sharpe net of funding + borrow + commission + slippage | ADR-0006 |
| P2-16 BMA→RL graduation Catch-22 | Gemini | Drop "BMA Sharpe ≥ 0.5" criterion; keep DSR significance | ADR-0006 |

## P0-1: Kelly formula correction (ADR-0004)

The original ADR-0004 specified:
```python
kelly_size = (signal.magnitude * signal.confidence) / max(market.volatility, 1e-4)
```

This is **wrong**. The continuous-time Kelly formula for log returns assumes `f* = μ/σ²`, where `μ` is expected log return and `σ²` is variance per period. The original formula has σ in the denominator, off by a factor of σ.

**Replacement (ADR-0004 §Decision Rule 6 supersede)**:

```python
# market.volatility = per-period stdev of log returns (not variance)
# signal.calibrated_probability ∈ [0,1] = directional confidence (calibrated, see P0-2)
edge = signal.magnitude * signal.calibrated_probability
kelly_size = edge / max(market.volatility ** 2, 1e-8)
target_size = signal.direction * min(cfg.max_position_pct,
                                      cfg.quarter_kelly * abs(kelly_size))
target_size = round_to_step(target_size, cfg.action_step)
target_size = clip(target_size, -cfg.max_position_pct, +cfg.max_position_pct)
```

**Test fence (mandatory, blocks merge)**:

```python
def test_kelly_dimensional_correctness():
    """Kelly should be dimensionless. Doubling the unit of vol should leave size unchanged."""
    market_unit = MarketState(volatility=0.02, ...)         # 2% per-period stdev
    market_doubled = MarketState(volatility=0.04, ...)       # 4% per-period stdev
    signal = AggregatedSignal(magnitude=0.01, calibrated_probability=0.6, ...)

    size_unit = compute_kelly_size(signal, market_unit, cfg)
    size_doubled = compute_kelly_size(signal, market_doubled, cfg)

    # If σ doubles, σ² quadruples, kelly_size should quarter
    assert abs(size_doubled / size_unit - 0.25) < 0.01

def test_kelly_with_known_values():
    """Closed-form: μ=0.005, p=1.0, σ=0.02 → f* = 0.005 / (0.02)² = 12.5
       Capped by max_position_pct (0.20) and quarter_kelly (0.25)
       Target ≈ 0.20 (saturated to cap)."""
    signal = AggregatedSignal(magnitude=0.005, calibrated_probability=1.0, direction=1, ...)
    market = MarketState(volatility=0.02, ...)
    cfg = RiskConfig(max_position_pct=0.20, quarter_kelly=0.25, action_step=0.05, ...)
    size = compute_kelly_size(signal, market, cfg)
    assert size == 0.20      # saturated
```

## P0-2: Confidence calibration (ADR-0002, ADR-0003)

### Summary of fix

`AnalystView.confidence` and `AggregatedSignal.confidence` MUST be calibrated probabilities. Until ≥200 samples accumulate per analyst, the signal-side confidence is **shrunk** toward zero by a hard 0.20 absolute (so a 0.7 raw becomes 0.5 effective). This makes the cost gate harder to clear and Kelly sizing smaller during the cold-start period.

### Concrete contract change (ADR-0002 §AnalystView supersede)

```python
@dataclass(frozen=True)
class AnalystView:
    analyst: str
    direction: Literal[-1, 0, +1]
    magnitude: float                   # expected return as fraction
    confidence: float                  # in [0, 1] — TARGET: calibrated probability of directional correctness
    confidence_raw: float              # raw, uncalibrated score (for debugging + calibrator training)
    horizon: str
    rationale: str | None = None
    metadata: Mapping[str, Any] | None = None
```

The contract distinguishes raw vs calibrated. Analysts that don't have a fitted calibrator emit `confidence = max(0.0, confidence_raw - COLD_START_SHRINK)` with `COLD_START_SHRINK = 0.20`.

### Per-analyst calibrator (ADR-0002 §Analyst Protocol amendment)

```python
@runtime_checkable
class CalibratableAnalyst(Analyst, Protocol):
    """Analyst whose confidence_raw can be calibrated against realized outcomes."""
    def fit_calibrator(self, history: list[RealizedOutcome]) -> None: ...
    def calibrator_status(self) -> dict:
        """Returns {n_samples, ece, calibrated, last_fit_at}"""
```

Default implementation (in `hermes_quant.calibration.IsotonicCalibrator`):

```python
class IsotonicCalibrator:
    """Maps confidence_raw -> calibrated probability via isotonic regression."""
    MIN_SAMPLES = 200       # below this, fall back to cold-start shrinkage
    REFIT_EVERY = 50        # refit after every 50 new samples

    def fit(self, raw_scores: np.ndarray, direction_correct: np.ndarray) -> None:
        if len(raw_scores) < self.MIN_SAMPLES:
            self._calibrator = None
            return
        from sklearn.isotonic import IsotonicRegression
        self._calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
        self._calibrator.fit(raw_scores, direction_correct.astype(float))
        self._n_fit_samples = len(raw_scores)

    def calibrate(self, raw_score: float) -> float:
        if self._calibrator is None:
            return max(0.0, raw_score - 0.20)   # cold-start shrinkage
        return float(self._calibrator.predict([raw_score])[0])
```

### Kronos-specific calibration (ADR-0002 §Kronos wrapper supersede)

The Kronos wrapper's `confidence_raw` is the path-agreement rate. Per DeepSeek's catch (and verified in `docs/research/01-rl-for-trading.md` and `03-plugin-architecture.md`), this systematically over-estimates marginal directional probability because sampled paths are correlated.

**Bootstrap calibration (mandatory before shipping the analyst)**: ship a fitted `IsotonicCalibrator` trained on the public Kronos demo data + a 1-year held-out OHLCV slice (BTC/USDT 1h), so the analyst arrives pre-calibrated rather than starting from naive shrinkage. The fitted calibrator is shipped as `hermes_quant/analysts/kronos_calibrator-v1.pkl` (committed to repo, < 100 KB).

### Aggregator confidence (ADR-0003 §BMA + §Stacking supersede)

The aggregator's emitted `AggregatedSignal.confidence` is calibrated via a separate isotonic calibrator at the aggregator level (different from per-analyst — the aggregator's score is its own quantity). Same `IsotonicCalibrator` class, fitted on the rolling 30-day window of (raw_aggregator_score, realized_direction) pairs.

Until aggregator calibrator has ≥200 samples, **emitted confidence = max(0.0, raw_score - 0.20)**.

## P0-3: Bidirectional bus — split-brain fix (ADR-0001, ADR-0008)

### Add executions.jsonl back-channel (new section in ADR-0008)

The freqtrade strategy emits a parallel JSONL stream `~/.hermes/quant/executions.jsonl` whenever an order fills, partials, or is cancelled:

```json
{
  "schema_version": 1,
  "id": "exec-20260513T010515Z-001",
  "asof": "2026-05-13T01:05:15Z",
  "signal_id": "sig-20260513T010500Z-BTC-USDT-1h-0001",
  "asset": "BTC/USDT",
  "exchange": "binance",
  "event_type": "fill",
  "side": "buy",
  "fill_qty": 0.00234,
  "fill_price": 73420.50,
  "fee_quote": 0.012,
  "slippage_bps": 4.2,
  "current_position_qty": 0.00234,
  "current_position_value_quote": 171.80,
  "account_balance_quote": 9852.34,
  "trailing_stop_active": true
}
```

Other `event_type` values: `partial_fill`, `cancel`, `reject`, `trailing_stop_triggered`, `forced_exit`, `heartbeat`.

### Daemon settlement loop reads executions.jsonl (new in ADR-0001)

The daemon's settlement loop tails `executions.jsonl` and updates `Portfolio` from broker reality:

```python
class Portfolio:
    """REAL portfolio state, sourced from executions.jsonl + periodic broker reconciliation."""
    def __init__(self, account_id: str, asset_class: str): ...
    def apply_execution(self, exec_event: dict) -> None: ...
    def reconcile_with_broker(self, broker_state: dict) -> list[Discrepancy]: ...
    @property
    def current_position_pct(self) -> float: ...     # MTM, includes unrealized
    @property
    def drawdown_pct(self) -> float: ...             # MTM peak vs current equity
    @property
    def daily_loss_pct(self) -> float: ...           # MTM
```

Plus a periodic broker reconciliation: every N ticks (default 10), the daemon directly queries the broker (alpaca-py / ccxt) for actual position + balance, compares to its in-memory `Portfolio`, surfaces discrepancies in `quant_doctor`, and treats discrepancies > 0.5% as critical alerts (writes WARNING to log, may trigger auto-halt per config).

This addresses both Gemini's split-brain finding and GPT-5.5's "drawdown_pct ignores unrealized losses" — the new Portfolio is mark-to-market by construction.

## P0-4: Halt is durable state, NEVER cleared by signals (ADR-0004, ADR-0008)

### Halt state model (ADR-0008 supersede)

Halts are stored in a SQLite table, not in signal records:

```sql
CREATE TABLE halts (
    account_id      TEXT NOT NULL,
    asset_class     TEXT NOT NULL,        -- '*' for all-asset halt
    asset           TEXT,                  -- NULL for all-asset halt within class
    reason          TEXT NOT NULL,
    halted_at       TEXT NOT NULL,         -- ISO 8601 UTC
    halted_until    TEXT,                  -- ISO 8601 UTC; NULL = until explicit resume
    halt_epoch      INTEGER NOT NULL,      -- monotonic per (account, asset_class, asset)
    cleared_at      TEXT,
    cleared_by      TEXT,                  -- 'cli:hermes_quant_resume' | 'auto:halted_until_expired'
    PRIMARY KEY (account_id, asset_class, asset, halt_epoch)
);
```

Signals carry `halt: bool` only as a TRIGGER to ENTER halt. They never trigger exit from halt. Exit from halt requires:

- `hermes quant resume <account> [<asset_class>] [<asset>]` (CLI, requires confirmation prompt + reason text)
- `halted_until` timestamp passing (auto-clear, only for daily-loss breakers — not for drawdown breakers, which require explicit resume)

### Freqtrade strategy reads halt-state mirror (ADR-0008 supersede)

The daemon writes a small `halt_state.json` mirror at `~/.hermes/quant/halt_state.json` (atomic write — write-temp-then-rename) that the strategy reads on every tick. Format:

```json
{
  "schema_version": 1,
  "asof": "2026-05-13T01:00:00Z",
  "halts": [
    {"account": "alpaca-paper", "asset_class": "equity", "asset": null, "reason": "drawdown_circuit_breaker", "epoch": 7}
  ]
}
```

Strategy logic on halt: cancel all open orders for the (account, asset_class, asset) tuple, force-exit all open positions, log warning, refuse new entries until next tick that observes halts cleared.

## P0-5: Risk-gate rule ordering (ADR-0004 §Decision supersede)

Replace ADR-0004's gate function:

```python
def gate(signal: AggregatedSignal, market: MarketState, portfolio: Portfolio,
         halt_state: HaltState, cfg: RiskConfig) -> Optional[Action]:
    # Rule 0: account/asset-class halt active → silent
    if halt_state.is_halted(portfolio.account_id, portfolio.asset_class, signal.asset):
        return None

    # Rule 1: drawdown circuit breaker (FIRST — independent of signal)
    if portfolio.drawdown_pct > cfg.max_drawdown_pct:
        return Action(target_position=0,
                      reason="drawdown_circuit_breaker", halt=True,
                      halt_scope=(portfolio.account_id, portfolio.asset_class, None))

    # Rule 2: daily-loss circuit breaker (FIRST — independent of signal)
    if portfolio.daily_loss_pct > cfg.max_daily_loss_pct:
        return Action(target_position=0,
                      reason="daily_loss_circuit_breaker",
                      halt=True,
                      halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                      halt_until=next_session_open(market.tz))

    # Rule 3: cooldown after recent loss (durable, read from SQLite)
    cooldown_until = portfolio.cooldown_until(signal.asset)
    if cooldown_until is not None and pd.Timestamp.utcnow() < cooldown_until:
        return None

    # Rule 4: signal-flat / zero-confidence (NOW it's safe to early-return)
    if signal.direction == 0 or signal.confidence < 1e-6:
        return None

    # Rule 5: transaction-cost-aware threshold
    expected_edge = abs(signal.magnitude) * signal.confidence
    transaction_cost = market.commission + market.spread + market.slippage_estimate + market.funding_cost
    if expected_edge < cfg.cost_multiple * transaction_cost:
        return None

    # Rule 6: Kelly-fractional sizing (P0-1 fix applied)
    kelly_size = (signal.magnitude * signal.confidence) / max(market.volatility ** 2, 1e-8)
    target_size = signal.direction * min(cfg.max_position_pct,
                                          cfg.quarter_kelly * abs(kelly_size))
    target_size = round_to_step(target_size, cfg.action_step)
    target_size = clip(target_size, -cfg.max_position_pct, +cfg.max_position_pct)

    # Rule 7: minimum trade-size guard (no churn)
    delta = target_size - portfolio.current_position_pct
    if abs(delta) < cfg.min_trade_size:
        return None

    return Action(target_position=target_size,
                  reason=f"signal_dir={signal.direction}_conf={signal.confidence:.3f}",
                  signal_id=signal.id)
```

Note: `transaction_cost` no longer multiplies spread by 0.5. The original formula (`commission + 0.5 * spread + slippage`) was per-leg; round-trip is `commission + spread + slippage`. The current `expected_edge < cost_multiple * (commission + spread + slippage + funding_cost)` formulation compares per-trade edge against round-trip costs with the cost_multiple safety factor on top. (Closes a subtle bug GPT-5.5 raised in passing.)

## P0-6: Daemon singleton enforcement (ADR-0001 supersede)

```python
import fcntl, os
from pathlib import Path

class DaemonLock:
    """POSIX advisory lock with PID liveness check.

    Acquired at daemon startup. Released on graceful shutdown. On startup,
    if the lock is held by a non-existent PID (stale lock from crash),
    we forcibly take it. If held by a live PID, we refuse to start.
    """
    def __init__(self, account_id: str):
        d = Path.home() / ".hermes/quant"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"daemon-{account_id}.lock"
        self._fd = None

    def acquire(self) -> None:
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another daemon holds it. Read its PID, check if it's alive.
            with open(self.path) as f:
                content = f.read().strip()
            try:
                pid = int(content.split()[0])
                os.kill(pid, 0)         # raises if dead
                raise DaemonAlreadyRunning(f"Daemon for account {self.account_id} "
                                            f"already running (PID {pid})")
            except (ValueError, ProcessLookupError):
                # Stale lock — force it
                os.close(self._fd)
                self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(self._fd, f"{os.getpid()} {datetime.utcnow().isoformat()}Z\n".encode())
        os.fsync(self._fd)
```

`hermes-quant-daemon` calls `DaemonLock(account_id).acquire()` as the first thing after argv parsing. `quant_doctor` reports the lock holder and its uptime.

## P0-7: Heartbeat + dead-man-switch (ADR-0008 supersede)

### Heartbeat from daemon

The daemon emits a heartbeat record to `signals.jsonl` every `heartbeat_interval_seconds` (default 60), regardless of whether any analyst produced a view:

```json
{"schema_version": 1, "type": "heartbeat", "asof": "2026-05-13T01:00:30Z",
 "daemon_pid": 4321, "uptime_seconds": 14400}
```

### Strategy enforces staleness window

The freqtrade strategy maintains a `last_heartbeat_ts`. On each tick:

```python
def _check_dead_man_switch(self, current_time):
    """Cancel all orders + flatten positions if heartbeat is stale."""
    if self._last_heartbeat is None:
        return  # bootstrap state — wait for first heartbeat
    age = (current_time - self._last_heartbeat).total_seconds()
    if age > self.dead_man_switch_seconds:        # default 180s = 3x heartbeat interval
        logger.error(f"hermes-quant heartbeat stale ({age:.0f}s) — "
                     f"cancelling all orders and entering safe-stop mode")
        # Cancel all open orders
        for trade in Trade.get_open_trades():
            self.dp.send_msg(f"DEAD_MAN_SWITCH: closing {trade.pair}")
            self._force_exit(trade)
        # Refuse new entries until heartbeat resumes
        self._safe_stop_until = current_time + timedelta(minutes=15)
```

### emergency-stop CLI

```
hermes quant emergency-stop [--account ACCOUNT] [--asset-class CLASS]
```

Writes `{"type": "emergency_stop", ...}` to `signals.jsonl` AND directly cancels all open orders via broker API (alpaca-py / ccxt). The broker-side cancellation is critical — relying on the strategy to read the bus loses time.

## P0-8: JSONL atomic-write protocol (ADR-0008 supersede)

```python
def emit_signal(record: dict) -> None:
    """Atomic-line append to signals.jsonl.

    POSIX guarantees write atomicity for writes ≤ PIPE_BUF (4096 bytes) on
    regular files when O_APPEND is set. We enforce the size cap and use
    a single os.write call.
    """
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > 4096:
        # Hard fail rather than risk a partial write
        raise SignalTooLarge(f"signal {record.get('id')} encodes to {len(encoded)} bytes "
                             f"(limit 4096); split components")
    fd = os.open(SIGNAL_BUS_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        n = os.write(fd, encoded)
        if n != len(encoded):
            raise IOError(f"short write: {n}/{len(encoded)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)
```

Schema enforcement keeps signals under 4 KB:
- `components` is capped at 16 entries; if more analysts vote, drop the lowest-confidence ones (recorded in metadata)
- `rationale` strings are truncated to 256 chars
- `metadata` is JSON-encoded and capped at 1024 chars after encode

The freqtrade strategy:
```python
def _read_signals_safe(self):
    with open(self.SIGNAL_BUS_PATH, "rb") as f:
        f.seek(self._last_offset)
        chunk = f.read()
        # Find the last complete line
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return []           # no complete line yet
        complete = chunk[:last_newline + 1]
        self._last_offset += last_newline + 1
        signals = []
        for line in complete.split(b"\n"):
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except json.JSONDecodeError as e:
                self._parse_error_count += 1
                if self._parse_error_count >= 5:
                    logger.error(f"5 consecutive bus parse errors; bus may be corrupt: {e}")
        return signals
```

## P1-9: Asset-class isolation (ADR-0004 supersede)

`Portfolio` and `RiskConfig` are partitioned by `(account_id, asset_class)`. The daemon maintains:

```python
@dataclass
class PortfolioPartition:
    account_id: str       # e.g., "alpaca-paper", "binance-spot"
    asset_class: str      # "crypto" | "equity" | "option" (v0.2)
    positions: dict[str, Position]    # asset -> Position
    realized_pnl: float
    peak_equity: float
    daily_open_equity: float

def get_portfolio(account_id: str, asset_class: str) -> PortfolioPartition: ...
```

Risk config also per partition:
```yaml
quant:
  risk:
    profile: conservative
    partitions:
      "alpaca-paper:equity":
        profile: conservative
      "binance-spot:crypto":
        profile: conservative
        # overrides:
        # max_position_pct: 0.15
```

A drawdown halt scopes to `(account_id, asset_class)` — never bleeds across.

## P1-10: EpisodeOutcome for stacking/RL (ADR-0003 supersede)

Add to ADR-0002 protocol:

```python
@dataclass(frozen=True)
class EpisodeOutcome:
    """Cross-sectional snapshot at decision time + realized outcomes per horizon.

    Required for stacking/RL — the aggregator must see what ALL analysts said
    at time T to learn correlations.
    """
    asof: pd.Timestamp
    asset: str
    timeframe: str
    aggregated_signal: AggregatedSignal       # contains components: tuple[AnalystView, ...]
    realized_returns: dict[str, float]         # horizon -> realized return: {"5m": 0.003, "1h": 0.012}
    direction_correct: dict[str, bool]
    realized_net_pnl: float | None = None      # P0-3 fix: actual after-fee P&L from executions
```

The settlement loop emits `EpisodeOutcome`s in addition to per-analyst `RealizedOutcome`s:
- Aggregators consume `EpisodeOutcome` for `update()` — they need the joint state
- Analysts consume `RealizedOutcome` for `update()` — per-analyst slice
- Risk-gate calibrator consumes `EpisodeOutcome` mapped to `(aggregated_signal.confidence, direction_correct["primary_horizon"])`

P0-3 also lands here: `realized_net_pnl` comes from the executions.jsonl back-channel, NOT from market-return computation.

## P1-11: Canonical CLI surface

Authoritative list (every other ADR cites THIS):

```
hermes quant setup [PROFILE]                          # interactive; PROFILE in {conservative, moderate, aggressive}
hermes quant setup --use-profile PROFILE              # alias

# Daemon lifecycle
hermes quant start [--account ACCOUNT]
hermes quant stop [--account ACCOUNT]
hermes quant restart [--account ACCOUNT]
hermes quant uninstall [--account ACCOUNT]
hermes quant status [--account ACCOUNT] [--all]

# Halt / resume
hermes quant resume <account> [<asset_class>] [<asset>]   # confirmation prompt
hermes quant halt <account> [<asset_class>] [<asset>] --reason TEXT
hermes quant emergency-stop [--account ACCOUNT]            # immediate cancel + flatten

# Information
hermes quant signals [-n N] [--asset ASSET] [--follow]
hermes quant show-views [-n N] [--asset ASSET] [--analyst NAME]
hermes quant doctor [--fix] [--calibration]
hermes quant logs [--follow] [-n N]

# Backtest
hermes quant backtest <asset> --from DATE --to DATE [--timeframe TF] [--analyst-set NAME]
hermes quant backtest-replay <run_id>             # replay signal log through freqtrade backtester

# Freqtrade integration
hermes quant freqtrade-setup [--freqtrade-dir DIR]
hermes quant freqtrade-backtest <signal_log> [--freqtrade-config PATH]

# Configuration
hermes quant config edit
hermes quant config show
hermes quant config validate
```

CI test fence: `tests/test_cli_surface.py` parses this section with a regex and asserts every listed subcommand has a registered handler.

## P1-12: Slippage default revision (ADR-0004 supersede)

```python
DEFAULT_SLIPPAGE_BPS = {
    "crypto": 12,           # was 5; corrected per DeepSeek
    "equity": 5,            # liquid US equities (SPY, AAPL); was 2
    "equity-illiquid": 25,  # less-liquid (small-cap, low ADV)
    "option": 50,           # placeholder for v0.2
}
```

Plus runtime adaptation: after 30 days of fills, slippage estimate is the rolling 90th-percentile of observed `(fill_price - decision_price) / decision_price` per asset.

## P1-13: Walk-forward CV embargo (ADR-0006 supersede)

```python
class PurgedWalkForward:
    """López de Prado purged walk-forward CV with explicit embargo.

    Per ADR-0009 amendment: embargo = max(forecast_horizon, 2 * timeframe)
    """
    def __init__(self, n_folds: int, train_size: pd.Timedelta,
                 val_size: pd.Timedelta, forecast_horizon: pd.Timedelta,
                 timeframe: pd.Timedelta):
        self.n_folds = n_folds
        self.train_size = train_size
        self.val_size = val_size
        self.embargo = max(forecast_horizon, 2 * timeframe)

    def split(self, data: pd.DataFrame): ...
```

The embargo is BOTH between train and validation folds AND between consecutive validation folds (to avoid label-overlap-induced auto-correlation in test-statistic estimates).

## P1-14: Backtest data unification (ADR-0008 supersede)

Backtest mode forces both processes to read the SAME bars:

1. `hermes quant backtest BTC/USDT --from 2024-01-01 --to 2026-01-01 --timeframe 1h --data-source ccxt:binance` runs hermes-quant against bars fetched from binance via ccxt. Bars are written to `~/.hermes/quant/backtests/<run_id>/bars.parquet` AS WELL AS being used for signal generation.
2. `hermes quant freqtrade-backtest <run_id>` invokes freqtrade's backtester pointed at the same parquet file via a custom `IDataProvider` (we ship `hermes_quant.consumers.freqtrade.parquet_data_provider`).
3. CI gate: `tests/test_backtest_data_consistency.py` — runs hermes-quant backtest, runs freqtrade backtest, asserts every signal's `decision_price` matches the bar's `close` at signal `asof`.

## P1-15: Net-of-cost Sharpe (ADR-0006 supersede)

All Sharpe calculations are NET of:
- Commission (per-fill, from broker)
- Half-spread × 2 (round trip)
- Slippage estimate (per-asset adaptive)
- Funding rate (perp positions; queryable from binance/bybit/etc. via ccxt)
- Borrow rate (short equities; from alpaca for now, polygon-marginable in v0.2)

The settlement loop computes `realized_net_pnl` per `EpisodeOutcome`. All evaluation metrics derive from this.

## P2-16: Drop BMA→RL graduation Catch-22 (ADR-0006 supersede)

ADR-0006's original graduation criteria #2 ("BMA Sharpe ≥ 0.5 net of costs") is REMOVED. The valid argument: if BMA can't extract alpha but the RL aggregator's non-linear interactions can, blocking RL on BMA's performance permanently locks out the case RL exists to handle.

Replacement criteria for v0.2 RL aggregator graduation:

1. **≥90 days of v0.1 paper-trade telemetry** with at least one stable analyst pool (unchanged)
2. ~~BMA Sharpe ≥ 0.5~~ **DROPPED — Catch-22 per Gemini P2 finding**
3. **Walk-forward purged CV** implemented and tested with embargo per P1-13 (unchanged)
4. **Hypothesis test paired-comparison**: Diebold-Mariano test on net P&L, p < 0.05 across all walk-forward folds (replaces underspecified DSR — addresses GPT-5.5 P1-24)
5. **Multiple-testing correction**: Benjamini-Hochberg FDR < 0.05 across the hyperparameter sweep (per GPT-5.5 P1-24)
6. **Shuffle-timestamp test** (with the addendum from P1-25 below) — RL aggregator's performance on shuffled-timestamp inputs must drop to within 1 std-dev of chance accuracy
7. **Truncate-at-t replay test** (per GPT-5.5 P1-25) — for every analyst feeding the RL aggregator, replay the entire backtest with each analyst's input truncated at decision time; performance must NOT collapse if the analyst is not actually using future data
8. **Max-drawdown ratio**: RL's max-drawdown ≤ 1.25 × BMA's max-drawdown on the same walk-forward window (unchanged)

## Promotion plan

Once the same 3 reviewers (or equivalent cross-family slate) PASS this amendments doc + the original 8 ADRs, all 9 ADRs are promoted to `accepted`.

Until then, all 8 originals stay `proposed` and this ADR-0009 stays `proposed`.


---

## Cross-link 2026-05-13: §P0-D ordering extends to tick loop (Phase-8 P0-C)

§P0-D ("durable halt ordering") was originally specified for the
`cmd_emergency_stop` CLI path: when the operator triggers emergency stop,
install the durable SQLite halt FIRST, then emit the bus signal, then
announce broker intent. Phase-8 review (2026-05-13) caught that the ordering
rule must ALSO apply at the tick loop's circuit-breaker emit point: when
the gate's drawdown or daily-loss circuit breaker returns
`Action(halt=True, halt_scope=...)`, `tick_loop.run_one_tick` MUST call
`halt_state.add_halt(...)` BEFORE `emit_signal_record(...)`.

Without this, the halt action is announced on the bus but not committed to
durable storage. On daemon restart the halt history is lost; on the next
tick the same circuit-breaker reading would re-fire; other assets in the
same scope wouldn't observe the halt.

**Implementation**: `hermes_quant/daemon/tick_loop.py:231-252` (shipped in
v0.1.1). Idempotency: existing-active-halt -> swallow `ValueError` (the
gate's halt action is idempotent in that case).

**Tests**: `tests/unit/test_tick_settlement.py::TestRunOneTick::{test_drawdown_circuit_breaker_installs_durable_halt, test_drawdown_halt_install_is_idempotent_across_ticks}`.

**Cross-link forward**: ADR-0008 amendment 2026-05-13 Part A (mirror
staleness fallback) is the consumer-side enforcement of this producer-side
ordering rule.
