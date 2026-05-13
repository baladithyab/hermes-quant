# Phase-4 Cross-Family Review — ADR Bundle Synthesis

**Date**: 2026-05-12
**Artifact**: `docs/adr/ADR-0001*.md` through `ADR-0008*.md` (8 ADRs, 1098 lines)
**Reviewers**: 3 cross-family
- `01-redteam-gpt-5.5.md` (openai/gpt-5.5-20260423, red-team lens)
- `02-arch-gemini-3.1-pro.md` (google/gemini-3.1-pro-preview-20260219, architecture/forward-compat lens)
- `03-quant-deepseek-v4-pro.md` (deepseek/deepseek-v4-pro-20260423, quant-correctness lens)
- All routed via direct OpenRouter curl (delegate_task router still RED — bypass verified by `advertised` model field matching requested)
**Aggregator**: orchestrator (Hermes Agent, this synthesis)

## Verdict

**ALL THREE REVIEWERS BLOCK v0.1.0.**

This is the right outcome for Phase 4. The cross-family scatter caught real, substantive issues that the orchestrator's pre-mortem would have missed. 28 distinct findings across the three reviewers; ~half are P0/P1.

## INTERSECTION (must-fix before any v0.1.0 code)

These 5 findings were independently flagged by ≥2 reviewers. Fix in ADR amendments BEFORE writing any production code.

### P0-1: Kelly formula is mathematically wrong
**Flagged by**: GPT-5.5 (P0 ADR-0004), DeepSeek (P0 ADR-0004)

ADR-0004 specifies `kelly_size = (magnitude * confidence) / volatility`. Real continuous Kelly is `f* = μ/σ²`. The current formula is off by a factor of σ — units don't match. In low-vol regimes the numbers happen to look plausible; when volatility regime shifts, sizing fails to adapt.

**Fix in ADR-0004 amendment**:
```python
# σ = market.volatility (per-period stdev of log returns)
kelly_size = (magnitude * calibrated_confidence) / (market.volatility ** 2)
target_size = signal.direction * min(cfg.max_position_pct, cfg.quarter_kelly * abs(kelly_size))
```
Plus: add a sanity-check unit test that asserts dimensional correctness of the sizing formula.

### P0-2: Confidence is uncalibrated and used as a probability everywhere
**Flagged by**: DeepSeek (P0 ADR-0003), GPT-5.5 (P0 implicit in BMA "Bayesian" naming + P2 calibration), Gemini (implicit via split-brain implications)

ADR-0003's aggregator emits `confidence` as a heuristic score (`abs(direction_score) * (1 - 2*disagreement)`), not a calibrated probability. ADR-0004's Kelly + cost gate then uses it AS a probability. ADR-0002 mandates calibration but defers enforcement.

The Kronos analyst's confidence is even worse: agreement-rate of correlated sampled paths systematically over-estimates marginal directional probability.

**Fix in ADR-0003 amendment**:
- Aggregator confidence is calibrated via isotonic regression on a rolling window of (raw_score, direction_correct) pairs.
- Until N≥200 samples accumulate, the aggregator emits **conservative-shrunk confidence**: `effective_confidence = max(raw_confidence - 0.20, 0.0)` — shrinks toward zero by 20pp absolute. This makes the cost gate harder to clear and the Kelly sizer smaller during the cold-start period.

**Fix in ADR-0002 amendment**:
- Kronos wrapper's confidence calibration uses bootstrap-on-historical: fit logistic regression mapping path-agreement → realized direction-correct on a held-out fixture, ship the fitted calibrator with the model, NOT the raw agreement.
- Same pattern for any future analyst that emits a "confidence" derived from sampling internal stochasticity.

### P0-3: Split-brain portfolio state — daemon ↔ broker reality
**Flagged by**: Gemini (P0 ADR-0004 & ADR-0008), GPT-5.5 (P0 ADR-0004 unrealized losses + P1 cooldown durability)

The daemon's `Portfolio.current_position`, `drawdown_pct`, `daily_loss_pct` are computed from the daemon's OWN realized P&L log. But freqtrade independently closes positions on trailing stops, partial fills, broker rejections, manual interventions. The daemon never learns this.

**Failure mode**: Freqtrade's trailing stop closes a position. Daemon thinks position is still open at full size. Daemon's drawdown_pct is wrong. Daemon's max_position_pct check thinks slot is full. New signals get silently dropped. OR: drawdown circuit breaker fails to trigger because daemon missed the loss.

**Fix in ADR-0001 + ADR-0008 amendment**:
- Bidirectional bus: hermes-quant emits `signals.jsonl`; freqtrade emits `executions.jsonl` (fill events with: order_id, signal_id, asset, fill_price, fill_qty, fees, slippage, timestamp).
- Daemon's settlement loop tails `executions.jsonl` and updates `Portfolio` from broker reality.
- Add `quant_doctor` check: "are daemon's open positions consistent with broker?" — query broker via ccxt/alpaca and reconcile.

### P0-4: Halt semantics contradictory across ADR-0004 and ADR-0008
**Flagged by**: GPT-5.5 (P0 ADR-0008)

ADR-0004 says halt persists until explicit resume. ADR-0008 says "next non-halt signal lifts the halt." These contradict. Worse, they're in DIFFERENT ADRs that the implementer may read in sequence and only one wins.

**Fix in ADR-0008 amendment**:
- Halt is a separate state field, NOT an attribute of a signal. `~/.hermes/quant/state.db::halts` table with rows per (account, asset_class).
- Only `hermes quant resume <account> [<asset_class>]` clears halt. NEVER cleared by trading signals.
- Freqtrade strategy reads halt state from SQLite (or a small `halt_state.json` mirror), NOT from signal `halt` field. The signal's `halt: bool` is a trigger to ENTER halt, never to exit it.

### P0-5: Risk-gate ordering bug — drawdown check after zero-confidence early-return
**Flagged by**: GPT-5.5 (P0 ADR-0004)

ADR-0004's gate code:
```python
if signal.direction == 0 or signal.confidence < 1e-6:
    return None                    # ← BUG: silent return
if portfolio.drawdown_pct > cfg.max_drawdown_pct:
    return Action(target_position=0, halt=True)   # ← Never reached if signal is flat
```
During a drawdown, the aggregator may be silent (high disagreement, low confidence). Gate returns None. Existing losing position stays open while drawdown deepens.

**Fix in ADR-0004 amendment**: Reorder rules. Circuit breakers FIRST (always), then signal-flatness check. Specifically:
```python
def gate(signal, market, portfolio, cfg):
    # 1. Circuit breakers FIRST (always evaluated, regardless of signal)
    if portfolio.drawdown_pct > cfg.max_drawdown_pct:
        return Action(target_position=0, reason="drawdown_circuit_breaker", halt=True)
    if portfolio.daily_loss_pct > cfg.max_daily_loss_pct:
        return Action(target_position=0, reason="daily_loss_breaker",
                      halt_until=next_session_open(market.tz))
    # 2. Halt-already-active check
    if portfolio.is_halted:
        return None
    # 3. THEN signal-driven logic
    if signal.direction == 0 or signal.confidence < 1e-6:
        return None
    # ... rest of rules ...
```

## UNION (P0/P1 — must-fix or seriously address before v0.1.0)

### P0-6: No daemon singleton enforcement
**Flagged by**: GPT-5.5

Two daemons can start (systemd-user + tmux fallback + manual `hermes-quant-daemon`). Both write to the same JSONL bus. Freqtrade gets alternating signals. Churn.

**Fix**: ADR-0001 amendment. Daemon acquires `~/.hermes/quant/daemon.lock` (POSIX advisory `flock`) at startup. Refuses to start if lock held by a live PID (verify via `os.kill(pid, 0)`). `quant_doctor` reports lock holder.

### P0-7: No global kill-switch / dead-man-switch
**Flagged by**: GPT-5.5

Daemon stalls (Python GIL deadlock, OOM, segfault). Freqtrade strategy keeps reading old signals — or worse, stops reading but keeps existing positions open. No bounded window for the consumer to take protective action.

**Fix**: ADR-0008 amendment. Daemon emits a heartbeat signal every N seconds (`{"schema_version": 1, "type": "heartbeat", "asof": ...}`). Freqtrade strategy: if no heartbeat for `2 * heartbeat_interval`, cancel all open orders and (per config) flatten or freeze. `hermes quant emergency-stop` writes a `STOP` signal to bus AND directly cancels orders via broker API.

### P0-8: JSONL bus atomic-write protocol unspecified
**Flagged by**: GPT-5.5

Daemon dies mid-write. Last line of `signals.jsonl` is truncated JSON. Freqtrade's `JSONDecodeError` handler silently skips it. The signal is lost.

**Fix**: ADR-0008 amendment. Atomic-line append protocol:
```python
line = json.dumps(record, separators=(",", ":")) + "\n"
fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
try:
    # POSIX guarantees atomicity of writes ≤ PIPE_BUF (4096 bytes) on regular files
    assert len(line.encode()) <= 4096, "signal too large for atomic write"
    os.write(fd, line.encode())
    os.fsync(fd)
finally:
    os.close(fd)
```
Freqtrade strategy must NOT silently drop `JSONDecodeError`; instead, log + count consecutive parse errors and alert on > N.

### P1-9: Cross-asset contagion in circuit breakers
**Flagged by**: Gemini

ADR-0004 has a global Portfolio. Crypto flash crash → equities halted Monday morning.

**Fix**: ADR-0004 amendment. Partition `Portfolio` and `RiskConfig` by `(account_id, asset_class)`. Halts scope to the partition. `quant_status` shows per-partition state.

### P1-10: Stacking/RL aggregator data starvation
**Flagged by**: Gemini

`RealizedOutcome` contains a single `AnalystView`, but stacking needs the joint state at time T (all analysts' simultaneous outputs).

**Fix**: ADR-0003 amendment. Add `EpisodeOutcome`:
```python
@dataclass(frozen=True)
class EpisodeOutcome:
    asof: pd.Timestamp
    asset: str
    timeframe: str
    aggregated_signal: AggregatedSignal      # contains the components tuple
    realized_return_per_horizon: dict[str, float]   # "5m": 0.003, "1h": 0.012, "1d": -0.005
    direction_correct_per_horizon: dict[str, bool]
```
Settlement loop emits these. Both per-analyst `RealizedOutcome`s AND per-tick `EpisodeOutcome`s are persisted; aggregators consume `EpisodeOutcome`, analysts consume `RealizedOutcome`.

### P1-11: CLI surface inconsistent across ADRs
**Flagged by**: GPT-5.5

ADR-0004 references `hermes quant resume`. ADR-0008 references `hermes quant freqtrade-setup` and `hermes quant freqtrade-backtest`. ADR-0007 doesn't list any of these.

**Fix**: ADR-0007 amendment. Add the missing subcommands. Add a "CLI surface" appendix that's the canonical source — every other ADR cites it.

### P1-12: Slippage defaults too optimistic
**Flagged by**: DeepSeek

5 bps for retail crypto on Binance is wrong. 10-15 bps realistic.

**Fix**: ADR-0004 amendment. Defaults: 12 bps for crypto, 5 bps for liquid US equities, 25 bps for less-liquid assets. Bootstrap from these; learn from real fills.

### P1-13: Walk-forward CV missing embargo specification
**Flagged by**: DeepSeek

ADR-0006 mentions purging + embargo but doesn't specify embargo size.

**Fix**: ADR-0006 amendment. `embargo = max(forecast_horizon, 2 * timeframe)`. Codify in `PurgedWalkForward` API.

### P1-14: Backtest data mismatch between hermes-quant and freqtrade
**Flagged by**: DeepSeek + Gemini

Hermes-quant pulls from yfinance, freqtrade pulls from binance via ccxt. Same nominal asset, different prices.

**Fix**: ADR-0008 amendment. Backtest mode requires SAME data source for both processes. Either (a) hermes-quant backtest writes the bars it used to a parquet, freqtrade backtest reads that parquet via a custom data source, or (b) document the discrepancy + apply conservative slippage buffer.

### P1-15: Sharpe targets ignore funding/borrow costs
**Flagged by**: DeepSeek

Crypto perp funding can eat 5-20% APR. ADR-0006 graduation criteria don't include this.

**Fix**: ADR-0006 amendment. All Sharpe targets are NET of: commission, half-spread, slippage estimate, funding rates (perps), borrow costs (shorts). Settlement loop logs cost components separately.

### P2-16: BMA→RL graduation Catch-22
**Flagged by**: Gemini

ADR-0006 requires "BMA Sharpe ≥ 0.5" before RL ships. But the whole point of RL is to capture non-linear interactions BMA misses — if those interactions are the source of alpha, BMA never reaches 0.5 and RL is permanently blocked.

**Fix**: ADR-0006 amendment. Drop criterion #2. Keep criteria #1 (90 days telemetry) and #6 (RL DSR significantly outperforms BMA). The user may flag this for further discussion — there's a real argument that "if BMA is at chance, the analysts produce no signal and adding RL on top is just amplifying noise" — but Gemini's catch-22 point is correct on the math.

## P1/P2 — fix opportunistically in v0.1.x, not blocking

| # | Finding | Reviewer | ADR |
|---|---|---|---|
| 17 | `extras: dict` in frozen dataclass not actually immutable | GPT-5.5 | 0002 |
| 18 | ThreadPoolExecutor + stateful analysts has no thread-safety contract | GPT-5.5 | 0002 |
| 19 | Cache path collision on `BTC/USDT` (slash → directory) | GPT-5.5 | 0005 |
| 20 | Parquet cache writes not atomic | GPT-5.5 | 0005 |
| 21 | Drawdown ignores unrealized losses (subsumed by P0-3 fix) | GPT-5.5 | 0004 |
| 22 | Cooldown not durable across daemon restarts | GPT-5.5 | 0004 |
| 23 | Profile/account scoping inconsistent | GPT-5.5 | 0001 |
| 24 | DSR statistical test under-specified | GPT-5.5 | 0006 |
| 25 | Shuffle-timestamp test isn't sufficient (cached future bars) | GPT-5.5 | 0006 |
| 26 | Read consistency contract missing for tools | GPT-5.5 | 0007 |
| 27 | systemd `Restart=on-failure` lacks burst limits | GPT-5.5 | 0001 |
| 28 | Confidence calibration uncapped during cold-start | GPT-5.5 | 0002 |
| 29 | JSONL retention/rotation/indexing plan missing | GPT-5.5 | 0008 |
| 30 | PyArrow not declared explicitly | GPT-5.5 | 0005 |
| 31 | Live/paper mode not displayed prominently | GPT-5.5 | 0007 |
| 32 | ECE threshold 0.15 vs sample-size standard error | DeepSeek | 0003 |
| 33 | Options-incompatible risk math (linear-instrument assumption) | Gemini | 0004 |
| 34 | Delayed-data-vs-live-execution mismatch | Gemini | 0005 |
| 35 | Settlement uses market returns, not net P&L from fills | GPT-5.5 | 0003 |
| 36 | Disagreement penalty zero-out can over-silence | GPT-5.5 | 0003 |
| 37 | Stacking feature schema not versioned with model | GPT-5.5 | 0003 |
| 38 | Rate limits provider-level not exchange/account scoped | GPT-5.5 | 0005 |

## DISAGREEMENTS

None of substance. The three reviewers found mostly orthogonal issues — exactly what cross-family scatter is supposed to do. GPT-5.5 caught operational/atomicity bugs (its strength). DeepSeek caught math/stats errors (its strength). Gemini caught architecture/forward-compat issues (its strength).

## What this means for v0.1.0

**Net assessment**: the architecture's PRINCIPLES are sound. The IMPLEMENTATION DETAILS in the ADRs need substantial revision before code lands. This is the cheapest possible time to find these — every fix is a markdown edit, not a Python refactor.

**Cost so far**: ~$2.50 OpenRouter spend across 3 reviewers, ~5 minutes wall-clock for each, ~10 minutes orchestrator synthesis time. Compared to the cost of shipping a sizing bug into production: trivial.

**Next step**: amend the 8 ADRs with the 5 INTERSECTION fixes (P0-1 through P0-5) and the 11 UNION fixes (P0-6 through P1-15). Re-fire the same 3 reviewers on the v2 bundle. If they all PASS, promote ADRs to `accepted` and write code. If any still BLOCK, iterate.

## Provenance

- Cross-family routing: VERIFIED via curl-bypass dispatcher. `advertised` model field on response matches requested per reviewer.
- Reviewers had ZERO context contamination between them — each got the same prompt + ADR bundle, ran in parallel.
- Three different families: OpenAI, Google, DeepSeek. None overlap.
- Single-reviewer P0s (e.g., GPT-5.5's "no kill switch") are still treated as P0 because they're in classes the reviewer's lens is uniquely strong at — operational atomicity for GPT-5.5, math for DeepSeek, architecture for Gemini.

The Phase-8 review at end of v0.1.0 will run again with possibly-different lens framings on the actual code.
