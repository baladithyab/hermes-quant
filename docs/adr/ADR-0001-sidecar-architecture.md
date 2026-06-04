# ADR-0001: Sidecar architecture — daemon decoupled from gateway

**Status**: Accepted (2026-05-12), implemented
**Date**: 2026-05-12
**Deciders**: Codeseys, ARIA (Hermes Agent)
**Supersedes**: —

## Context

hermes-quant is distributed as a Hermes Agent plugin. The plugin's job is to compute trading signals from a multi-analyst ensemble (Kronos foundation model, classical TA, microstructure, news-LLM) and emit them for execution.

Three hosting candidates were evaluated (see `docs/research/03-plugin-architecture.md`):

1. **`cronjob` tool per tick** — wrong shape; cron is for agent tasks, not signal computation. Cold-start tax + LLM call dominate the tick budget even when no thinking is required.
2. **Asyncio task on the gateway loop** — heavy ML compute (Kronos forward pass) starves other adapters. Gateway-only liveness; CLI-mode users get nothing.
3. **External daemon process managed by systemd-user (Linux/WSL) or launchd (macOS)** — full decoupling. The gateway is for chat; the daemon is for signal computation.

A fourth dimension: research showed (Gemini 3.1 Pro Preview, `02-framework-integration.md`) that LLM-based analysts have inference times in seconds, which **breaks the synchronous backtester loop** of every framework we evaluated (freqtrade vectorized, Backtrader event-driven, VectorBT Numba-compiled). The sidecar pattern bypasses this entirely: hermes-quant emits signals to a bus; freqtrade (or NautilusTrader, v0.2) reads the bus and executes. Backtest replays historical bars through hermes-quant, captures the signal log, and replays the log through freqtrade's backtester.

## Decision

hermes-quant ships as **two cooperating processes**:

1. **The daemon** (`hermes-quant-daemon`, console_scripts entry point) is the long-running tick engine. It owns:
   - Data ingestion (yfinance, alpaca-py, ccxt)
   - Analyst pool execution
   - Aggregator (Bayesian baseline; RL aggregator slot for v0.2)
   - Risk gate
   - Signal emission to `~/.hermes/quant/signals.jsonl`
   - Tick metadata persistence to `~/.hermes/quant/ticks.db` (SQLite WAL)
   - Structured logging to `~/.hermes/logs/quant-daemon.log`

2. **The Hermes plugin** (`hermes-quant`) is a thin layer over the daemon's persisted state. It owns:
   - Plugin tools (`quant_status`, `quant_show_signals`, `quant_show_views`, `quant_doctor`) — **read-only views** into daemon state
   - CLI subcommand tree (`hermes quant {setup, start, stop, restart, backtest, signals, doctor}`) — control plane
   - Slash command `/quant` (Discord + Telegram + CLI)
   - Skill registration so the agent learns hermes-quant idioms

The daemon is started via `hermes quant start` which writes a systemd-user unit (`~/.config/systemd/user/hermes-quant.service`) and runs `systemctl --user enable --now`. Stop is the inverse. The unit has `Restart=on-failure` and `RestartSec=30s`. On macOS the install path generates a launchd plist; on Windows/WSL without systemd-user, the fallback is a tmux-detached session managed via `hermes quant {start,stop}`.

## Consequences

### Positive

- **Async-friendly**: agents and Kronos forwards can take seconds without blocking the gateway or breaking any backtester.
- **Survives gateway/CLI restarts**: the daemon is its own systemd unit. `hermes gateway restart` has zero effect on the daemon.
- **Clean backtest path**: hermes-quant against historical bars produces a signal log. Replay the log through freqtrade or NautilusTrader. We get production-grade backtest fidelity without forcing async agents into a sync loop.
- **Resource isolation**: heavy ML inference doesn't starve Discord/Telegram message handling.
- **Pluggable execution**: today freqtrade reads the bus; tomorrow NautilusTrader reads the same bus. Same signal contract, different consumer.

### Negative

- **Two processes to manage** (daemon + freqtrade). Mitigated by `hermes quant doctor` showing both processes' health in one place.
- **systemd-user dependency on Linux/WSL** for the canonical path. Users on bare WSL without `systemd=true` in `/etc/wsl.conf` need the tmux fallback.
- **Tick log file growth**: ~50-100 MB/month at 1-min × 5 assets. Mitigated by retention policy + monthly vacuum + weekly backup.
- **Cross-process coordination cost**: signal latency = daemon tick + bus write + consumer poll. For 5-min and longer ticks (the v0.1 default) this is a non-issue; for sub-minute ticks (deferred to v0.2) it forces a switch from JSONL polling to a Redis or zmq bus.

### Neutral / accepted

- The plugin's `register(ctx)` is small — registers tools, CLI, slash command, skill. No daemon spawning at register time. Users opt in via `hermes quant start`.
- Signal bus is JSONL + SQLite. Simple, debuggable, no extra services.

## Implementation notes

- Daemon process tag = `hermes-quant-daemon` (matches the systemd unit name) so `pkill -f hermes-quant-daemon` is safe.
- Plugin tools NEVER spawn the daemon as a side effect; explicit `hermes quant start` only.
- `quant_doctor` reports: daemon process health, last tick timestamp, signal bus growth rate, MLflow URI (if set), Kronos availability, broker connectivity.
- Logs in `~/.hermes/logs/` are profile-aware — they go to `~/.hermes/profiles/<name>/logs/` when a profile is active.

## References

- `docs/research/03-plugin-architecture.md` §1 — host-loop pattern analysis
- `docs/research/02-framework-integration.md` §2 — async LLM agent latency vs sync backtester loops
- hermes-s2s `_internal/discord_bridge.py` — reference for adapter monkeypatch + deferred slash install
