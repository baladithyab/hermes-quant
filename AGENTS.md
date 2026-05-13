# hermes-quant — AGENTS development guide

This document is for ARIA / Hermes Agent / Codex / Claude Code / any other
coding agent working on this repository.

## Project posture

hermes-quant is **money-software**. It runs a daemon that ultimately decides
where to put real capital. Defects don't just print bad output — they
subtract from the user's bank account. Every code change should be reviewed
through that lens.

Three discipline principles, in priority order:

1. **Silence by default.** When uncertain, the system holds cash. Tests must
   verify the silence path more than the action path. Aggregator on
   disagreement → flat. Risk gate on uncertainty → no action. Daemon on
   data quality issue → skip tick + log warning.

2. **Hard rules over learned policy.** The risk gate enforces deterministic
   limits the aggregator (RL or otherwise) cannot circumvent. Position
   sizing is discrete (0, ±0.05, ±0.10, ±0.15, ±0.20 of NAV). Drawdown
   circuit breakers are non-negotiable. Cost thresholds are non-negotiable.

3. **Reproducibility.** Every signal is replayable from disk. Backtest = run
   the daemon against historical bars, capture the signal log, replay
   through freqtrade's backtester. No "the daemon was running and we got
   lucky" telemetry.

## Architecture map

8 ADRs in [docs/adr/](docs/adr/). Read them before touching code:

- ADR-0001: sidecar architecture (daemon ↔ Hermes plugin)
- ADR-0002: analyst protocol (`MarketContext`, `AnalystView`, `Analyst` Protocol)
- ADR-0003: aggregators (BMA + stacking; RL slot for v0.2)
- ADR-0004: risk gate (deterministic, silence-by-default, ¼-Kelly)
- ADR-0005: data layer (yfinance / ccxt / alpaca with provider chains)
- ADR-0006: RL deferred to v0.2 with concrete graduation criteria
- ADR-0007: plugin shape (tools = read-only views, CLI = control)
- ADR-0008: freqtrade integration via signal bus (JSONL contract)

3 research notes in [docs/research/](docs/research/). The decisions in the
ADRs are grounded in these.

## Repo layout

```
hermes-quant/
├── plugin.yaml                   # Hermes plugin manifest
├── pyproject.toml                # Python deps + entry points
├── README.md
├── LICENSE                       # Apache-2.0
├── AGENTS.md                     # this file
├── CHANGELOG.md
├── docs/
│   ├── adr/                      # 8 ADRs
│   └── research/                 # 3 research lenses
├── hermes_quant/
│   ├── __init__.py               # register(ctx) — Hermes plugin entry point
│   ├── protocol.py               # MarketContext, AnalystView, Analyst Protocol
│   ├── analysts/                 # one analyst per file
│   │   ├── classical_ta.py
│   │   ├── microstructure.py
│   │   └── kronos.py             # both KronosAnalyst and KairosAnalyst
│   ├── aggregators/
│   │   ├── bma.py
│   │   └── stacking.py
│   ├── risk/
│   │   └── gate.py
│   ├── data/
│   │   ├── base.py               # DataProvider Protocol
│   │   ├── yfinance_provider.py
│   │   ├── ccxt_provider.py
│   │   └── alpaca_provider.py
│   ├── daemon/
│   │   ├── main.py               # hermes-quant-daemon entry point
│   │   ├── tick_loop.py
│   │   └── settlement_loop.py
│   ├── consumers/
│   │   └── freqtrade/
│   │       ├── quant_consumer_strategy.py    # drop into freqtrade user_data/strategies/
│   │       └── freqtrade_config.example.json
│   ├── evaluation/
│   │   ├── cv.py                 # PurgedWalkForward
│   │   ├── lookahead.py          # shuffle_timestamps_test
│   │   └── dsr.py                # DeFlated Sharpe (v0.2 placeholder)
│   ├── cli/
│   │   ├── __init__.py           # setup_argparse, dispatch
│   │   ├── setup.py              # hermes quant setup
│   │   ├── lifecycle.py          # start, stop, restart
│   │   ├── status.py             # status, signals, doctor
│   │   └── backtest.py
│   ├── tools/
│   │   └── tools.py              # quant_status, quant_show_signals, ...
│   ├── training/                 # v0.2 RL trainer (stub for now)
│   │   └── main.py
│   └── skills/
│       └── hermes-quant/
│           └── SKILL.md
└── tests/
    ├── fixtures/bars/            # parquet fixture data
    ├── unit/
    ├── integration/
    └── conftest.py
```

## Development workflow

### Setup

```bash
cd /mnt/e/CS/github/hermes-quant
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e '.[all,dev]'
```

### Run tests

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q -n auto    # parallel
```

### Lint + type check

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m ruff check hermes_quant/ tests/
~/.hermes/hermes-agent/venv/bin/python3 -m ruff format hermes_quant/ tests/
~/.hermes/hermes-agent/venv/bin/python3 -m mypy hermes_quant/
```

### Smoke test the plugin

```bash
# Verify register() runs without error
~/.hermes/hermes-agent/venv/bin/python3 -c "
import hermes_quant
class MockCtx:
    def register_tool(self, **kw): print('tool', kw['name'])
    def register_command(self, name, **kw): print('cmd', name)
    def register_cli_command(self, name, **kw): print('cli', name)
    def register_hook(self, name, cb): print('hook', name)
    def register_skill(self, name, path): print('skill', name)
    runner = None
hermes_quant.register(MockCtx())
"
```

## Critical conventions

### Money never goes through tools

Plugin tools (`quant_status`, `quant_show_signals`, etc.) MUST be read-only.
The agent's LLM is in a chat session; if a tool could place a real-money
trade, an accidental "yeah do that" in chat could move thousands of dollars.

Live trading goes through the CLI ONLY, with explicit confirmation prompts.

### Action space is discrete

Position sizes are `{0, ±0.05, ±0.10, ±0.15, ±0.20}` of NAV by default.
Continuous action spaces invite the RL aggregator to reward-hack via
max-leverage. Don't widen this without a corresponding ADR amendment.

### All times are UTC end-to-end

`MarketContext.asof`, signal `asof`, tick db `ts` columns — all UTC.
Localization is display-only.

### Bar data validation at the boundary

Data providers MUST validate before returning. Drop NaN OHLC rows. Drop
zero-volume rows (halted tickers). Dedupe on timestamp. Sort. If
< 2 valid bars remain, raise `DataQualityError` (don't return empty).

### No look-ahead bias

The CI gate `tests/test_no_lookahead.py` runs `shuffle_timestamps_test()`
against every shipped analyst and aggregator. Any analyst that performs
better than chance on shuffled timestamps has look-ahead bias and the
build fails.

### Analyst confidence MUST be calibrated

When an analyst emits `confidence=0.8`, that should track ~80% directional
accuracy over a recent window. Per ADR-0002 / ADR-0003. Calibration drift
is auto-detected and surfaced in `quant_doctor`.

### Plugin authoring constraints (from references/plugin-authoring.md)

- `register(ctx)` is called ONCE at gateway startup. No daemon spawning here.
- `optional_env`, NEVER `requires_env` (the latter blocks install).
- Tool handlers return JSON-serializable dicts/strings only.
- CLI subcommand `--profile` collides with global; we use `--use-profile`.
- Discord slash command install via `pre_gateway_dispatch` hook.
- No editing of Hermes core SQLite tables.

### Cross-process state

The daemon writes to `~/.hermes/quant/`:
- `signals.jsonl` (append-only signal bus)
- `ticks.db` (SQLite WAL — tick metadata, analyst views, realized outcomes)
- `state.json` (small atomic state file — halt flags, cooldown timers)

Plugin tools READ from these. Freqtrade strategy READS from `signals.jsonl`.

When you write to `state.json`, use atomic-rename pattern:
```python
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(state))
tmp.replace(path)   # atomic on POSIX
```

When you append to `signals.jsonl`, flush after each line:
```python
with open(path, "a", buffering=1) as f:   # line-buffered
    f.write(json.dumps(record) + "\n")
    f.flush()
    os.fsync(f.fileno())   # for the paranoid; required for crash-safety
```

## Testing discipline

### Unit tests must be deterministic

No live API calls, no network, no time-of-day-sensitive behavior. Use
`pd.Timestamp` directly with explicit values. Use the parquet fixtures
in `tests/fixtures/bars/` for any test that needs market data.

### Integration tests gate live providers

Tests that do hit live providers (alpaca paper, yfinance) live in
`tests/integration/` and are skipped by default. Run with
`pytest tests/integration/ --run-integration`.

### Fixture data

`tests/fixtures/bars/` contains:
- `BTC-USDT-1h-2024-01-01-2024-12-31.parquet` (8,760 bars)
- `AAPL-1d-2020-01-01-2026-01-01.parquet` (~1,500 bars)
- `SPY-1h-2024-01-01-2024-12-31.parquet` (~1,750 bars)

When adding tests that need market data, USE these fixtures. Don't hit
live providers from CI.

### Property-based tests for the gate

The risk gate has many branches. Use `hypothesis` for property tests
that verify invariants:
- Never returns position size > `max_position_pct`
- Never returns action when drawdown exceeds breaker
- Always returns silence when expected_edge < cost threshold

## Common debugging flows

### Daemon won't start

```bash
hermes quant doctor                          # surface obvious issues
systemctl --user status hermes-quant         # systemd state
journalctl --user -u hermes-quant -n 100     # last 100 log lines
```

### Signals aren't reaching freqtrade

```bash
tail -f ~/.hermes/quant/signals.jsonl        # is daemon emitting?
ls -la ~/.hermes/quant/                      # bus exists?
hermes quant status                          # daemon reports last signal time
# Then check freqtrade's strategy log for parse errors
```

### Analyst's confidence looks wrong

```bash
hermes quant show-views --asset BTC/USDT --analyst kronos-small --tail 50
```

### Calibration drift

```bash
hermes quant doctor --calibration            # full ECE table
```

## Commit conventions

```
type(scope): subject

Body if needed.
```

Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `perf`.

Scopes: `daemon`, `analysts`, `aggregators`, `risk`, `data`, `cli`, `tools`,
`consumers`, `evaluation`, `plugin`, `adr`, `research`.

Example: `feat(analysts): add MicrostructureLite with order-book imbalance`.

## Things to NEVER do

1. **Place a real-money trade from a tool handler.** Live trading is CLI-only.
2. **Modify Hermes core SQLite tables.** Add your own; key on Hermes IDs without FK constraints.
3. **Hot-reload plugin code mid-session.** Restart the gateway.
4. **Hardcode credentials.** Always read from env or `~/.hermes/.env`.
5. **`pkill -f 'hermes_cli.main gateway'` from inside the gateway** (self-kill).
6. **Widen the discrete action space without an ADR amendment.**
7. **Bypass the risk gate.** Even for testing — use `RiskConfig` overrides.
8. **Train an RL aggregator on < 90 days of data.** ADR-0006 graduation criteria are the gate.
9. **Skip the shuffle-timestamp test on a new analyst.** That CI gate exists for a reason.
10. **Bundle freqtrade into hermes-quant.** Sidecar architecture preserves the GPL boundary.
