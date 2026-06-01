# hermes-quant

> ARIA-powered multi-analyst algorithmic trading framework.
> Distributed as a [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin.
> **v0.6.4 — alpha. Paper/backtest/HITL scaffolding only. Do not run live with real money.**

```
                    ┌─────────────────────────┐
                    │   hermes-quant DAEMON    │
                    │   (systemd / launchd)    │
                    │                          │
   ┌────────────┐   │  ┌────────────────────┐  │   ┌──────────────────┐
   │ data       │──▶│  │ analyst pool       │  │   │ signal bus       │
   │ providers  │   │  │  - kronos / kairos │  │   │  signals.jsonl   │
   │ yfinance   │   │  │  - classical TA    │──┼──▶│  ticks.db        │──▶ freqtrade
   │ ccxt       │   │  │  - microstructure  │  │   │                  │    (or your
   │ alpaca     │   │  └─────────┬──────────┘  │   │                  │     consumer)
   └────────────┘   │            ▼             │   │                  │
                    │  ┌────────────────────┐  │   │                  │
                    │  │ aggregator         │  │   │                  │
                    │  │  bma / stacking    │  │   │                  │
                    │  │  (RL slot, v0.2)   │  │   │                  │
                    │  └─────────┬──────────┘  │   │                  │
                    │            ▼             │   │                  │
                    │  ┌────────────────────┐  │   │                  │
                    │  │ risk gate          │  │   │                  │
                    │  │  ¼-Kelly + circuit │  │   │                  │
                    │  │  breakers          │  │   │                  │
                    │  └────────────────────┘  │   └──────────────────┘
                    └──────────┬───────────────┘
                               │
                               ▼
                    ┌─────────────────────────┐
                    │  Hermes plugin tools     │ ◀── chat with ARIA via
                    │   quant_status           │     CLI / Discord / Telegram
                    │   quant_show_signals     │     (read-only views)
                    │   quant_show_views       │
                    │   quant_doctor           │
                    └─────────────────────────┘
```

## What this is

A trading framework where multiple independent analysts emit views, an aggregator combines them, and a risk gate decides whether to act. Built around three principles:

1. **Multi-analyst by design.** Pluggable analyst modules that emit a uniform `AnalystView` (direction, magnitude, confidence, horizon). v0.1.0 ships classical TA, microstructure-lite, [Kronos foundation model](https://github.com/shiyu-coder/Kronos), and the Kairos BTC fine-tune. Add your own.
2. **Sidecar architecture.** A long-running daemon emits signals on a JSONL bus. [freqtrade](https://www.freqtrade.io/) (v0.1) or NautilusTrader (v0.2) reads the bus and executes. We don't reinvent order management; we focus on signal intelligence.
3. **Silence by default.** Aggregator emits zero on disagreement; risk gate enforces hard rules the aggregator can't bypass; circuit breakers flatten on drawdown. Designed to lose money slowly, not catastrophically.

## What this is NOT (yet)

- **Not a strategy.** v0.1.0 ships the framework + 3 baseline analysts. You bring the alpha — or wait for the analyst marketplace.
- **Not RL-driven.** The RL aggregator is reserved for v0.2 and gated on objective graduation criteria (DSR p<0.05, ≥12 walk-forward folds, shuffle-timestamp test passing). See [ADR-0006](docs/adr/ADR-0006-rl-aggregator-deferred.md).
- **Not live-trade-ready.** v0.1.0 is paper-trade only by default. Live trading requires explicit CLI invocation and 30+ days of paper-trade track record.
- **Not for funded accounts.** Until v0.2 lands proper position-sizing math review and audit logging, treat this as a learning project.

## Status

- ✅ Architecture (8 ADRs, [docs/adr/](docs/adr/))
- ✅ Research (3 lenses, [docs/research/](docs/research/))
- 🚧 v0.1.0 implementation (in progress as of 2026-05-12)
- 📋 v0.2 — Alpaca equities via NautilusTrader, RL aggregator (graduation-gated)
- 📋 v0.3 — Options, news-LLM analyst via OpenRouter scatter

## Install

```bash
# 1. Install the plugin via Hermes
hermes plugins install baladithyab/hermes-quant

# 2. Install Python deps INTO THE HERMES VENV (NOT your shell pip)
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-quant'[all]'

# 3. Enable — add the plugin to the config.yaml allow-list (NOT `hermes plugins enable`)
#    hermes-quant is an entry-point ("standalone") plugin: it loads only if its name
#    is in ~/.hermes/config.yaml under `plugins.enabled`. `hermes plugins enable` manages
#    only bundled/git-installed plugins and will print "not installed or bundled" here.
#    Add it to the list:
#        plugins:
#          enabled:
#            - hermes-quant
#    (edit ~/.hermes/config.yaml by hand, or however you manage that file).

# 4. Restart gateway so it picks up the newly-enabled plugin
hermes gateway restart

# 5. Setup
hermes quant setup
```

> ⚠️  Use the explicit hermes venv pip path above. A bare `pip install -e .` will install into your conda/system Python where Hermes can't see it. See [Hermes plugin authoring gotchas](https://hermes-agent.nousresearch.com/) for context.
> ⚠️  Step 3 is the `config.yaml plugins.enabled` allow-list, **not** `hermes plugins enable`
> (which only handles bundled/git plugins). See `docs/operations/HERMES-INTEGRATION.md` §1.3.

### CUDA torch (optional)

Kronos can run on CPU (acceptable latency for 5m+ ticks) or GPU. For CUDA 12.1:

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e ~/.hermes/plugins/hermes-quant'[all]' \
    --extra-index-url https://download.pytorch.org/whl/cu121
```

`hermes quant doctor` reports the resolved torch + CUDA availability.

## Quickstart — install, inspect, and run safely

```bash
# List named PDR recipes
hermes quant recommend BTC/USDT --asset-class crypto --recipe-id btc-usdt-mvp --json

# Write a replayable Hermes semantic packet
hermes quant semantic-packet write \
  --asset BTC/USDT --horizon 1h --stance neutral \
  --confidence 0.35 --magnitude 0.0 \
  --summary 'Mixed regime; prefer low conviction until quantitative agreement improves.' \
  --source 'note:operator-thesis|manual thesis' \
  --model hermes:manual

# Create a deterministic committee-turn artifact from packets
hermes quant committee run --asset BTC/USDT --semantic-packet-file <packet.json>

# Optional: set up autonomous semantic perception via Hermes cron
hermes quant perception start --asset BTC/USDT --horizon 1h --cadence 1h --dry-run

# Customize a PDR recipe without editing Python
hermes quant recipes example --output ~/.hermes/quant/recipes/my-btc.yaml
hermes quant recipes validate ~/.hermes/quant/recipes/my-btc.yaml
hermes quant recipes list

# Check whether semantic perception is fresh enough for a recipe
hermes quant perception status --recipe-id btc-usdt-deliberative
```

## Quickstart — paper trade BTC on yfinance in 5 minutes

```bash
# Setup with conservative risk profile
hermes quant setup conservative

# Verify
hermes quant doctor

# Start the daemon (creates systemd-user unit on Linux/WSL)
hermes quant start

# Watch signals as they're emitted
hermes quant signals -n 20 --follow

# After ~24 hours of ticks, check status
hermes quant status
```

In a chat session with ARIA:

```
You: how is the trading daemon doing?
ARIA: [calls quant_status]
       Daemon: running (uptime 23h 14m)
       Last tick: BTC/USDT 1h @ 2026-05-13T22:00:00Z
       Recent signals: 4 (3 long, 1 flat)
       Open positions: 1 long BTC at 7% NAV
       Today's P&L: +$12.34 (paper)
```

## Architecture

See `docs/adr/`:

| ADR | Topic |
|---|---|
| [0001](docs/adr/ADR-0001-sidecar-architecture.md) | Sidecar daemon decoupled from gateway |
| [0002](docs/adr/ADR-0002-analyst-protocol.md) | Analyst protocol contract |
| [0003](docs/adr/ADR-0003-aggregator.md) | Bayesian + stacking aggregators |
| [0004](docs/adr/ADR-0004-risk-gate.md) | Deterministic risk gate, silence-by-default |
| [0005](docs/adr/ADR-0005-data-layer.md) | Data layer with provider chains |
| [0006](docs/adr/ADR-0006-rl-aggregator-deferred.md) | RL aggregator deferred to v0.2 |
| [0007](docs/adr/ADR-0007-plugin-shape.md) | Plugin shape — read-only tools, CLI control |
| [0008](docs/adr/ADR-0008-freqtrade-integration.md) | Freqtrade integration via signal bus |

## Research

See `docs/research/`:

- [01-rl-for-trading.md](docs/research/01-rl-for-trading.md) — RL SOTA + failure modes (DeepSeek V4 Pro)
- [02-framework-integration.md](docs/research/02-framework-integration.md) — Framework comparison + Alpaca/yfinance specifics (Gemini 3.1 Pro Preview)
- [03-plugin-architecture.md](docs/research/03-plugin-architecture.md) — Hermes plugin patterns + Kronos wrapping (orchestrator-authored)

## License

Apache-2.0. See [LICENSE](LICENSE).

The freqtrade consumer strategy at `hermes_quant/consumers/freqtrade/` is shipped as a separate file you drop into freqtrade's own `user_data/`. Hermes-quant itself does NOT bundle freqtrade — install it separately if you want to use it as the execution engine. Freqtrade is GPL-3.0; the sidecar architecture means the two run in separate processes with the JSONL bus between them, which keeps the licensing clean.

## Disclaimer

This software is provided "as is" without warranty. **Algorithmic trading carries substantial risk of loss.** Past performance does not predict future results. Backtest results, especially on short windows, are systematically optimistic. The authors and contributors are not registered investment advisers and provide no investment advice. Use at your own risk; never trade with money you can't afford to lose; always paper-trade for extended periods before going live.
