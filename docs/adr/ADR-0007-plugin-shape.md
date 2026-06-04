# ADR-0007: Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop

**Status**: Accepted (2026-05-12), implemented
**Date**: 2026-05-12

## Context

ADR-0001 established that hermes-quant is two cooperating processes: a daemon and a Hermes plugin. This ADR specifies the plugin's surface area precisely.

Per `references/plugin-authoring.md`, a Hermes plugin can:
- Register tools (`ctx.register_tool`) — the agent's LLM can call them
- Register slash commands (`ctx.register_command`) — `/quant ...` in CLI + gateway
- Register CLI subcommands (`ctx.register_cli_command`) — `hermes quant ...`
- Register hooks (`ctx.register_hook`) — lifecycle callbacks
- Register skills (`ctx.register_skill`) — surface a SKILL.md to the agent

`register(ctx)` runs ONCE at gateway startup; not hot-reloaded; bot adapters not yet logged in.

## Decision

The plugin's contract:

### Tools (read-only views)

```python
ctx.register_tool(name="quant_status", toolset="quant", schema=..., handler=...)
ctx.register_tool(name="quant_show_signals", toolset="quant", schema=..., handler=...)
ctx.register_tool(name="quant_show_views", toolset="quant", schema=..., handler=...)
ctx.register_tool(name="quant_doctor", toolset="quant", schema=..., handler=...)
```

| Tool | Returns |
|---|---|
| `quant_status` | daemon process status, last tick, asset count, signal rate, current positions |
| `quant_show_signals` | last N signals from `signals.jsonl` (filterable by asset, timeframe, direction) |
| `quant_show_views` | last N analyst views from `ticks.db` (per-analyst breakdown for a given timestamp) |
| `quant_doctor` | full health: data providers, analysts, aggregator, broker connectivity, MLflow, Kronos availability |

**No tool spawns the daemon.** No tool starts a backtest. No tool changes config. All those are CLI-only operations. The agent gets observation, not control.

Rationale: the agent is in a chat session. If the agent could `quant_run_backtest` mid-conversation, a 30-second backtest would block the entire conversation and burn LLM tokens on idle waits. CLI control plane keeps long operations out of the conversation loop.

### Slash commands

```python
ctx.register_command(
    "quant",
    handler=...,
    description="hermes-quant: /quant status | /quant signals [N] | /quant doctor",
)
```

`/quant` is a multiplexer:
- `/quant status` — calls the same backend as `quant_status` tool
- `/quant signals [N]` — show last N signals
- `/quant doctor` — health check

Discord slash command registration uses the deferred-install pattern from hermes-s2s (per `references/plugin-authoring.md` "Discord slash-command fingerprint-skip"). At `register()` time the bot isn't logged in, so we register a `pre_gateway_dispatch` hook that installs `/quant` on first inbound message and calls `await tree.sync()` afterward to bypass the fingerprint-skip.

### CLI subcommands

```python
ctx.register_cli_command(
    name="quant",
    help="hermes-quant: trading daemon control plane",
    setup_fn=cli.setup_argparse,
    handler_fn=cli.dispatch,
)
```

Subcommand tree:
```
hermes quant setup [--profile <conservative|moderate|aggressive>]
hermes quant start              # writes systemd unit + enables + starts
hermes quant stop               # stops daemon, leaves unit installed
hermes quant restart
hermes quant uninstall          # removes systemd unit
hermes quant status             # equivalent to quant_status tool
hermes quant signals [-n 50]
hermes quant backtest <asset> --from <date> --to <date> [--analyst-set <name>]
hermes quant doctor [--fix]
hermes quant config edit
hermes quant logs [--follow] [-n 200]
```

`--profile` is the risk-config profile from ADR-0004. Per `references/plugin-authoring.md` "GOTCHA: --profile collides", we expose this as `--use-profile` (with positional `<profile>` as alternative) — NEVER `--profile`, which collides with hermes' top-level flag.

```python
p_setup = sub.add_parser("setup")
p_setup.add_argument("profile_pos", nargs="?",
                     choices=["conservative", "moderate", "aggressive"],
                     default=None, metavar="PROFILE",
                     help="Risk profile to apply")
p_setup.add_argument("--use-profile", dest="profile", default=None,
                     choices=["conservative", "moderate", "aggressive"],
                     help="Risk profile to apply (alias for positional)")
```

### Hooks

```python
ctx.register_hook("pre_gateway_dispatch", _install_discord_slash_command)
ctx.register_hook("on_session_start", _quiet_telemetry_for_quant_session)
```

`pre_gateway_dispatch`: deferred Discord `/quant` install (per the gotcha above).

`on_session_start`: when the agent's session starts and `quant` toolset is enabled, log a one-line "hermes-quant tools available; daemon is {running|stopped}" so the user knows. Read-only; doesn't affect the session.

### Skill registration

```python
skill_md = Path(__file__).parent / "skills" / "hermes-quant" / "SKILL.md"
if skill_md.exists():
    ctx.register_skill("hermes-quant", skill_md)
```

The shipped skill describes:
- When to use the tools (status checks, signal review)
- When to recommend CLI commands (start/stop/backtest, never via tools)
- Common debugging flows (daemon not running → run `hermes quant doctor`)
- The graduation path from paper to live (must hit ADR-0006's criteria)

### What the plugin does NOT do

- It does NOT spawn the daemon at register time. Daemon is opt-in via `hermes quant start`.
- It does NOT touch config at register time. Setup is interactive via `hermes quant setup`.
- It does NOT register any tool that places a real-money trade. Only paper-trade goes through the agent surface; live-money trades require explicit CLI invocation with confirmation prompt.
- It does NOT install `requires_env` keys in `plugin.yaml` (per `references/plugin-authoring.md` — `requires_env` blocks install for users without ALL keys, even with `required: false`). All credentials are `optional_env` and surfaced at config-time via `hermes quant doctor`.

## Consequences

### Positive

- Clean agent UX: tools are observations, CLI is control. Long operations stay out of the conversation.
- No live-trade footgun via the agent surface — accidental "place a $10K market order" is structurally impossible from a tool call.
- Plugin install never blocks on credentials. Users can install + paper-trade with zero API keys via yfinance.
- Discord `/quant` lands cleanly via the deferred-install pattern, no fingerprint-skip surprises.

### Negative

- Splitting tools (read-only) and CLI (control) means users must learn two surfaces. Mitigated by `quant_status` tool surfacing the available CLI commands when daemon isn't running.
- Live trade requires CLI invocation, which feels heavyweight. This is intentional — friction here saves a real-money mistake.
- The slash command multiplexer (`/quant <subcommand>`) is harder to autocomplete than separate `/quant_status`, `/quant_signals` etc. We pick the multiplexer for namespace cleanliness.

## Implementation notes

- `plugin.yaml` declares `optional_env: [ALPACA_API_KEY, ALPACA_API_SECRET, BINANCE_API_KEY, BINANCE_API_SECRET, OPENROUTER_API_KEY, MLFLOW_TRACKING_URI, HF_TOKEN]`. None are `required`.
- `__init__.py::register()` runs in <50ms — no model loading, no network calls. Heavy imports are lazy.
- `cli.py::dispatch()` handlers are in `hermes_quant.cli.*` modules; each subcommand has its own file for testability.
- Tool handlers return JSON-serializable dicts/strings only. They wrap daemon state reads in `try/except DataProviderError` so a bad daemon doesn't crash the agent.

## References

- `references/plugin-authoring.md` — full plugin authoring reference
- ADR-0001 — sidecar architecture (the daemon side this ADR mirrors)
- hermes-s2s `__init__.py::register` — reference implementation of all the patterns above
