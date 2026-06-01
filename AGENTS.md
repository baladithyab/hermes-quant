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

**82 ADRs** in [docs/adr/](docs/adr/) — the full index is [docs/adr/README.md](docs/adr/README.md).
Read the index before touching code; it is the authoritative decision record and has grown
far beyond the foundational eight. The load-bearing foundations to start with:

- ADR-0001: sidecar architecture (daemon ↔ Hermes plugin)
- ADR-0002: analyst protocol (`MarketContext`, `AnalystView`, `Analyst` Protocol)
- ADR-0003: aggregators (BMA + stacking; RL slot for v0.2)
- ADR-0004: risk gate (deterministic, silence-by-default, ¼-Kelly) — **immutable, final authority**
- ADR-0005: data layer (yfinance / ccxt / alpaca with provider chains)
- ADR-0007: plugin shape (tools = read-only views, CLI = control)
- ADR-0008: freqtrade integration via signal bus (JSONL contract)
- ADR-0015: HITL (propose → human-approve → gate → react)
- ADR-0079: PDR unified architecture (Perception → Decision → Reaction)
- ADR-0080: self-evolution framework (advisory-plane; W-flags; human ships every change)
- ADR-0082/0083/0084: strategy-openness, horizon-neutral foundations, scheduled-event calendar

For the rest (governance plane 0031, options 0027/0028/0029, admissibility 0077, etc.) consult
the index — do NOT assume the eight foundations are the whole picture.

**49 research notes** in [docs/research/](docs/research/) ground the ADR decisions.

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
│   ├── adr/                      # 82 ADRs (see adr/README.md index)
│   └── research/                 # 49 research lenses
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

## Anti-patterns from reference projects we explicitly reject (with citations)

We've studied TauricResearch/TradingAgents, HKUDS/AI-Trader, HKUDS/Vibe-Trading, yolojewjitsu/moon-dev-ai-agents, and the FutureSim paper (arXiv 2605.15188). Some of those projects ship code we should NOT copy. Concrete file:line citations so anyone reading those repos for inspiration sees exactly which patterns we considered and rejected. Full synthesis at `docs/architecture/2026-05-24-reference-project-synthesis.md`; per-project deep-dives under `docs/research/reference-projects/`.

| # | Source | Pattern (rejected) | Why we reject it |
|---|---|---|---|
| 1 | moon-dev `risk_agent.py:319` | `self.override_active = "OVERRIDE" in response_text.upper()` lets an LLM substring-match disable the daily loss limit for 15 minutes | Triple-stacked failure: overridable risk gate + LLM as override authority + string-grep control flow. We require ADR-0004 deterministic risk gate + non-overridable kill-switch. |
| 2 | moon-dev `trading_agent.py:253` | `n.ai_entry(token, amount)` fires the moment the LLM's allocation JSON parses | LLM-output → money-moves with no HITL, no signed approval. We require ADR-0015 propose-decide-react with explicit human confirmation for every order. |
| 3 | moon-dev `trading_agent.py:123-124` | `lines = response.split('\n'); action = lines[0].strip()` makes the first line of free-text output the action verb | Free-text → structured command channel. `int(''.join(filter(str.isdigit, line)))` for confidence is even worse. We require Pydantic structured output with retry-then-silence-by-default fallback. |
| 4 | TradingAgents `agents/trader/trader.py` | `TraderProposal.position_sizing: Optional[str]` — free-text prose sizing rubber-stamped by Portfolio Manager | Free-text sizing escapes the discrete action space. We require explicit `Decimal` sizing within the {0, ±0.05, ±0.10, ±0.15, ±0.20} ladder. |
| 5 | TradingAgents `agents/trader/trader.py` | `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` literal string downstream code grep-matches | String-grep contract on stochastic generator output. We use Pydantic `bind_structured` with retry. |
| 6 | AI-Trader `services.py::_update_position_from_signal` | 1:1 blind copy-trading: subscribe-once, all leader trades cascade into follower's account | Removes the "explicit confirmation per order" guarantee. We require per-order HITL even within a methodology subscription. |
| 7 | AI-Trader bare-string token auth | One token grants both signal-read AND order-execute scopes | No capability separation. We require read-only tools (plugin surface) vs CLI-only execution surface (ADR-0007). |
| 8 | moon-dev (no `as_of` discipline) | Agents pull live data with no backtest-time clamp; `rbi_agent` family backtests on a single hardcoded CSV with no train/validation split | Look-ahead by default. We require ADR-0019 evaluation discipline + `tests/test_no_lookahead.py` CI gate. The FutureSim chronological-replay invariant lands in ADR-0033 evidence store. |
| 9 | moon-dev (no audit trail) | Recommendations live in in-memory DataFrames reset per cycle | No replayability. We require ADR-0001 sidecar reproducibility + the upcoming ADR-0033 evidence store with `evidence_ids` linkage. |

**Convergent failure across all four reference projects:** every one of them lets the LLM be the final execution authority somewhere. TradingAgents at the trader role; AI-Trader at the copy-trading cascade; moon-dev at the override boundary. Vibe-Trading is the lone exception — it explicitly draws the boundary at "no live execution" via absence of broker SDKs in the tool registry. **Our deterministic-risk-gate + HITL pattern is the inverse of the reference-project failure mode.** Don't regress.


<!-- seeds:start -->
## Issue Tracking (Seeds)
<!-- seeds-onboard:v0.4.5 -->
<!-- seeds-onboard-schema:4 -->

This project uses [Seeds](https://github.com/jayminwest/seeds) v0.4.5 for git-native issue tracking.

**At the start of every session**, run:
```
sd prime
```

This injects session context: rules, command reference, and workflows. Pass `--format json|compact|markdown|plain|ids` on any command for agent-friendly output.

**Quick reference:**
- `sd ready` — Find unblocked work
- `sd search <query>` — Full-text search across titles + descriptions
- `sd create --title "..." --type task --priority 2` — Create issue
- `sd update <id> --status in_progress` — Claim work
- `sd close <id>` — Complete work
- `sd dep add <id> <depends-on>` — Add dependency between issues
- `sd sync` — Sync with git (run before pushing)

### Planning
Use `sd plan` when work is large or ambiguous enough that an LLM benefits from structured decomposition. Submit spawns one child seed per step; `step.blocks` uses forward semantics (step i with `blocks: [j]` means step i blocks step j, and step j gets step i's id in its `blockedBy`).

- `sd plan templates` — List built-ins (`feature`, `bug`, `refactor`) plus custom templates
- `sd plan prompt <seed-id>` — Emit a structured prompt the LLM fills in
- `sd plan submit <seed-id> --plan <file>` — Validate + spawn child seeds
- `sd plan show <pl-id>` — View sections, children, sub-plans
- `sd plan outcome <pl-id> --result success|partial|failure` — Record outcome (storage-only)
- `sd plan review <pl-id> --by <name>` — Record reviewer (informational)

### Before You Finish
1. Close completed issues: `sd close <id>`
2. File issues for remaining work: `sd create --title "..."`
3. Sync and push: `sd sync && git push`
<!-- seeds:end -->

<!-- hermes-quant-seeds-policy -->
### Seeds policy for hermes-quant (project-specific)

**Seeds is the single source of truth for what we need to do and have done.** Track ALL work here —
pending, in-progress, blocked, and done — including externally-gated items (never silently drop them).

- **On session start:** `sd prime` then `sd ready` (unblocked work) and `sd blocked` (what's waiting + why).
- **Every backlog item, bug, capability gap, review finding, and operator-gated action gets a seed.** The
  consolidated backlog (`docs/research/2026-05-30-backlog-consolidated.md`), the review-team findings
  (`docs/research/2026-05-31-review-team-findings.md`), and the flag-flip/enablement runbooks are the
  *prose* record; **seeds are the live tracker** — mirror them in.
- **Externally-gated items** (operator cron-registration, data-volume accumulation, market events,
  governance) are seeds with a `blocked` status + a label (`gated:operator` / `gated:data` / `gated:market` / `gated:deferred` / `gated:maintenance`) and the precise unblock condition in the description. They are NOT closed until the
  gate clears — surfacing the blocker IS the deliverable.
- **Rails are non-negotiable and override any seed:** a seed may PROPOSE work, but the deterministic risk
  gate (ADR-0004), the discrete sizing ladder {0, ±0.05, ±0.10, ±0.15, ±0.20}, and the kill-switch are
  immutable. Every new capability ships default-OFF, eval-gated, byte-identical when off. No seed
  justifies a degrading flag flip or a fire on a non-event.
- **Before finishing a wave:** `sd close` the done items, `sd create` for newly-discovered work
  (review findings, follow-ups), then `sd sync`. The deep-work-loop (audit→research→architect→execute→
  review→reconcile) reads from and writes back to seeds each iteration.
<!-- /hermes-quant-seeds-policy -->
