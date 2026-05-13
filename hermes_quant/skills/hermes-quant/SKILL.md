# hermes-quant skill

> Multi-analyst algorithmic trading framework, distributed as a Hermes
> Agent plugin. Sidecar daemon emits signals; freqtrade (or NautilusTrader)
> executes. Silence by default.

## When to load this skill

- User asks about hermes-quant — installation, configuration, daemon ops, analyst pool, aggregator, risk gate, freqtrade integration, RL aggregator graduation
- User runs `hermes quant <anything>` and asks a follow-up
- User mentions trading signals, paper-trade, backtest, Kronos, Kairos, Bayesian model averaging, Kelly sizing, walk-forward CV, drawdown circuit breaker
- User asks about ADR-0001..0009 in this repo

## Architecture (one-paragraph orientation)

`hermes-quant` runs as TWO processes:
1. **Daemon** — long-lived (systemd/launchd/tmux), ticks every N minutes, runs analysts, aggregates views, emits signals to a JSONL bus
2. **Hermes plugin tools** (THIS) — read-only views over the daemon's state. The plugin DOES NOT control trading; CLI does that.

The daemon emits to `~/.hermes/quant/signals.jsonl`. Freqtrade (or NautilusTrader v0.2) reads the bus and executes. A back-channel `~/.hermes/quant/executions.jsonl` flows fills back to the daemon for portfolio reconciliation.

Three discipline principles:
1. **Silence by default** — when uncertain, hold cash. Aggregator silence on disagreement; risk gate silence below cost threshold.
2. **Hard rules over learned policy** — risk gate enforces deterministic limits the aggregator (RL or otherwise) can't bypass.
3. **Reproducibility** — every signal replayable from disk. Backtest replays signal logs through freqtrade.

## Tool usage patterns

### "How is the trading daemon doing?"

Call `quant_status`. Response includes daemon running/stopped, last signal, last heartbeat, recent signal count.

If `daemon_running: false`, recommend:
```bash
hermes quant doctor       # see what's wrong
hermes quant start         # if doctor reports clean
```

### "What signals has the daemon emitted recently?"

Call `quant_show_signals(n=20)`. Default filters out heartbeats. Sort is recent-first.

For drilling into one asset: `quant_show_signals(n=20, asset="BTC/USDT")`.

### "Why did the aggregator decide to go long BTC?"

Call `quant_show_views(asset="BTC/USDT", n=10)`. Returns per-analyst contributions to the most recent aggregated signal. Each view shows direction + magnitude + confidence + horizon + rationale.

### "Is everything healthy?"

Call `quant_doctor`. Reports daemon health, data provider connectivity, optional library availability (torch + CUDA, ccxt, alpaca-py, mlflow), config validity. Add `calibration: true` for per-analyst calibration error tables (slower).

## CLI control plane (NEVER do this from a tool)

These are CLI-only by design — never call them via tools:

- `hermes quant setup [PROFILE]` — interactive setup wizard
- `hermes quant start | stop | restart` — daemon lifecycle
- `hermes quant resume <account> [<asset_class>] --reason TEXT` — lift halt
- `hermes quant halt <account> --reason TEXT` — manually halt
- `hermes quant emergency-stop` — cancel all orders + durable halt
- `hermes quant backtest <asset> --from DATE --to DATE` — historical replay
- `hermes quant freqtrade-setup` — wire hermes-quant into local freqtrade

The agent should suggest these commands when the user asks for control-plane operations, but NOT execute them itself.

## Common debugging flows

### Daemon won't start

1. `quant_doctor` first — surfaces obvious issues (missing config, lock contention, dependencies).
2. If lock contention reported: `pkill -f hermes-quant-daemon` then retry.
3. systemd-user logs: user runs `journalctl --user -u hermes-quant -n 100`.

### Signals aren't reaching freqtrade

1. `quant_status` — is the daemon emitting? Check `last_signal` timestamp.
2. User checks freqtrade strategy log for parse errors.
3. User runs `tail -f ~/.hermes/quant/signals.jsonl` to verify writes.

### Aggregator confidence looks wrong / suspicious

1. `quant_doctor --calibration` — show per-analyst Expected Calibration Error.
2. `quant_show_views --asset BTC/USDT` — drill into per-analyst contributions.
3. Per ADR-0009: until ≥200 calibrator samples, confidence is shrunk by 0.20 (cold-start).
   Expect early signals to look conservative. This is intentional.

### Daemon crashes or unexpected halt

1. Check halt state: `quant_status` shows active halts.
2. Halts are durable — must be explicitly cleared via `hermes quant resume` with `--reason`.
3. Drawdown circuit-breaker halts (>15% from peak) require manual review before resume.

## Reading signals

Each signal has these fields (see ADR-0008):
- `direction`: -1 short, 0 flat, +1 long
- `magnitude`: expected return as fraction (0.012 = 1.2%)
- `confidence`: CALIBRATED probability of directional correctness in [0, 1]
- `horizon`: how long the view holds ("5m", "1h", "1d")
- `target_position_pct`: AFTER risk-gate sizing (this is what freqtrade acts on)
- `components`: per-analyst contributions

`target_position_pct` is the canonical action — `direction × magnitude` is just the aggregator's pre-gate view.

## Risk profile guidance

Three profiles ship in v0.1 (per ADR-0004):
- **conservative** — 10% max position, 10% drawdown breaker, 3% daily loss breaker. Default. Use for first 30+ days.
- **moderate** — 20% max, 15% drawdown, 5% daily. Switch only after 30+ days of paper-trade with positive Sharpe.
- **aggressive** — 40% max, 20% drawdown, 10% daily. Track record required. Live only.

Never recommend a profile change without seeing actual paper-trade telemetry.

## v0.1.0 vs v0.1.1+ status

**v0.1.0 (current)** ships:
- Plugin tools + slash command + CLI surface
- Architecture (8 ADRs + amendments)
- Protocol contracts (MarketContext, AnalystView, AggregatedSignal, ...)
- Documentation + LICENSE

**v0.1.0 does NOT ship**:
- Working daemon (scaffold only — `hermes quant start` prints a placeholder)
- Concrete analysts (Kronos, classical-TA, microstructure)
- Concrete aggregator (BMA, stacking)
- Risk gate implementation
- Data providers (yfinance, ccxt, alpaca)
- Freqtrade consumer strategy

When a user asks for any of the above, point them at the GitHub roadmap and recommend they wait for v0.1.1.

## What NOT to do

- Don't suggest live-trading anything in v0.1.0. Even after v0.1.1 ships, recommend ≥30 days paper-trade first.
- Don't recommend the user "just patch the Kelly formula" or change the discrete action steps. ADR-0004 hardness is load-bearing.
- Don't bypass the risk gate. The aggregator (RL or otherwise) is supposed to be unable to.
- Don't offer to spawn the daemon from a tool. Daemon control is CLI-only.
- Don't mix freqtrade and the daemon in the same process. Sidecar architecture is the GPL-license firewall.
- Don't claim Kronos has out-of-sample alpha. Per `docs/research/01-rl-for-trading.md`, only Kairos-base-crypto on BTC/ETH 1-min has documented modest alpha; everything else is unproven.

## References

- README.md — install + quickstart
- AGENTS.md — development guide
- docs/adr/ADR-0001..0009 — architecture
- docs/research/01-rl-for-trading.md — RL SOTA + failure modes
- docs/research/02-framework-integration.md — framework comparison
- docs/research/03-plugin-architecture.md — plugin patterns + Kronos wrapping
- docs/reviews/2026-05-12-adr-bundle/ — Phase-4 cross-family review (full provenance)
