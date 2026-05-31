# HERMES-INTEGRATION — how the Hermes Agent installs + runs hermes-quant as a plugin

**Status:** authoritative integration reference
**Date:** 2026-05-30
**Host verified:** `hermes-agent` **v0.15.1** (`~/.hermes/hermes-agent/`)
**Plugin verified:** hermes-quant **v0.4.4** (`__version__` in `hermes_quant/__init__.py`)

> This is the definitive doc on how Hermes discovers, loads, and runs hermes-quant,
> and how our PDR architecture (ADR-0079) fits under Hermes. It is grounded in:
> - `docs/research/2026-05-30-r-hermes-plugin-system.md` (the plugin-system research, confirmed against the installed `~/.hermes/hermes-agent/hermes_cli/plugins.py`)
> - `AGENTS.md` "Plugin authoring constraints"
> - the actual `hermes_quant/__init__.py` `register(ctx)`, `plugin.yaml`, and `docs/adr/ADR-0079-perception-decision-reaction-architecture.md`
> - the live cron registry at `~/.hermes/cron/jobs.json` (16 quant crons) and `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md`
>
> Where a fact is confirmed by a live probe on this machine, the probe is shown.

---

## TL;DR

1. We ship as a **pip entry-point plugin** (`hermes_agent.plugins :: hermes-quant = hermes_quant`), `kind: standalone`. Hermes **discovers** it from `importlib.metadata` entry points — no `~/.hermes/plugins/` directory needed.
2. `register(ctx)` is **shape-correct** for the installed `PluginContext` (all five `ctx.register_*` methods match), runs in <50 ms, spawns no daemon. It wires **16 read-only tools**, a `/quant` slash command, a `hermes quant` CLI control plane, the `pre_gateway_dispatch` hook (Discord slash install), and the bundled skill.
3. **LIVE BLOCKER:** `standalone` entry-point plugins are **opt-in** — they load only if their name is in `~/.hermes/config.yaml` `plugins.enabled`. `hermes-quant` is **NOT** in that list today, so it is discovered but **never loaded**. Fix: `hermes plugins enable hermes-quant` + gateway restart. See §5.
4. **Money is CLI-only.** Hermes does not distinguish "money" from "read-only" tools — *we* enforce it by registering only read/propose/approve-a-record tools (no execution tool exists), and putting `start/stop/backtest` under `register_cli_command`. `quant_approve` fires the **`PaperReactor`** against an already-human-approved proposal record, never a live broker.
5. The PDR pipeline (ADR-0079) runs **under Hermes** not inside `register()`: **DB-backed crons** drive the Perception→Decision→Reaction ticks (the deployed `~/.hermes/scripts/quant-*.py`), tools are read-only views into the state those crons write, and the gateway chat / Discord `/quant` is the advisor + HITL surface.
6. Manifest drift to fix (§6 checklist): `provides_tools` undercounts (`quant_recipes` missing → 15 declared vs 16 registered); `provides_hooks` over-declares `on_session_start` (never wired); `version: "0.4.4"` lags the v0.6.x feature work. All cosmetic — none block loading.

---

## 1. Install / discovery flow

### 1.1 What the manifest is and where it lives

The manifest is **`plugin.yaml`** at the repo root: `/mnt/e/CS/github/hermes-quant/plugin.yaml`. The installed `PluginManifest` dataclass (`hermes_cli/plugins.py`) reads exactly these fields: `name, version, description, author, requires_env, provides_tools, provides_hooks, source, path, kind, key`. Any other key in our YAML (`license, homepage, manifest_version, platforms, optional_env`) is **silently ignored** by the parser — harmless, human-documentation only.

`kind: standalone` is the correct kind for hermes-quant: it adds its own tools/hooks/CLI/skill; it is not a `backend` (memory/storage provider), not `exclusive`/`platform` (gateway adapter), not `model-provider` (LLM provider).

### 1.2 The four discovery sources (later overrides earlier on name collision)

1. Bundled — `<hermes-repo>/plugins/<name>/`
2. User — `~/.hermes/plugins/<name>/`
3. Project — `./.hermes/plugins/<name>/` (gated by `HERMES_ENABLE_PROJECT_PLUGINS`)
4. **Pip — packages exposing the `hermes_agent.plugins` entry-point group** ← **hermes-quant uses this**

Sources 1–3 require a directory with both `plugin.yaml` **and** `__init__.py:register(ctx)`. The entry-point source skips the directory requirement: the manifest is **synthesized from the entry point** (`name=ep.name`, `source="entrypoint"`, `path=ep.value`, `key=ep.name`) and the module is imported via `ep.load()`.

We declare the entry point in `pyproject.toml`:
```toml
[project.entry-points."hermes_agent.plugins"]
hermes-quant = "hermes_quant"
```

**Live probe — Hermes discovers us:**
```
$ ~/.hermes/hermes-agent/venv/bin/python3 -c "import importlib.metadata as m; \
    print([(e.name, e.value) for e in m.entry_points().select(group='hermes_agent.plugins')])"
[('hermes-quant', 'hermes_quant'), ('hermes-huly', ...), ('mission-control-bootstrap', ...), ('hermes-s2s', ...)]
```

### 1.3 Install steps (what an operator runs)

```bash
# 1. Install the package into the Hermes venv (editable for dev, or from a release wheel)
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e '/mnt/e/CS/github/hermes-quant[all]'

# 2. Enable it (opt-in — REQUIRED for standalone entry-point plugins; see §5)
~/.hermes/hermes-agent/venv/bin/hermes plugins enable hermes-quant
#    (equivalently: add `- hermes-quant` under `plugins.enabled` in ~/.hermes/config.yaml)

# 3. Restart the gateway so register(ctx) runs once at startup
#    (do NOT pkill from inside the gateway — self-kill; restart via your service manager)
```

There is **no host-version handshake**: `PluginManifest` has no "compatible host version" field, so nothing version-checks plugin `0.4.4` against host `0.15.1`. The two version numbers are independent.

### 1.4 Discovery → load path for our entry-point plugin

1. `discover_and_load()` scans the four sources; `_scan_entry_points()` finds `hermes-quant = hermes_quant` and synthesizes `PluginManifest(name="hermes-quant", source="entrypoint", path="hermes_quant", key="hermes-quant")`.
2. model-provider plugins are recorded-not-imported; bundled backend/platform auto-load; **everything else (us) is gated on `plugins.enabled`**.
3. If enabled → `_load_entrypoint_module()` → `ep.load()` imports `hermes_quant` → builds `PluginContext(manifest, manager)` → calls `hermes_quant.register(ctx)` **once**.
4. `register_tool` → global `ToolRegistry` (+ tracked in `_plugin_tool_names`); `register_hook` → `_hooks[name]`, fired later via `invoke_hook(name, **kwargs)`.

---

## 2. The `register(ctx)` contract — our actual surface

`register(ctx)` is called **exactly once at gateway startup**. If it raises, only this plugin is disabled and Hermes continues. Per AGENTS.md: **no daemon spawning** in register; heavy imports are lazy; it runs in <50 ms. Our impl complies — it spawns nothing and lazy-imports `tool_schemas`, `tools`, `cli`, `discord_slash`.

The five `PluginContext` methods we use, all confirmed against the installed signatures:

| Hermes method | What we register |
|---|---|
| `register_tool(name, toolset, schema, handler, ...)` | 16 tools, all `toolset="quant"` |
| `register_command(name, handler, description)` | the `/quant` slash (CLI + gateway) |
| `register_cli_command(name, help, setup_fn, handler_fn)` | the `hermes quant` control-plane subcommand tree |
| `register_hook(hook_name, callback)` | `pre_gateway_dispatch` → `install_quant_slash_on_pre_dispatch` |
| `register_skill(name, path)` | `hermes-quant` → `hermes_quant/skills/hermes-quant/SKILL.md` |

### 2.1 The 16 tools (name → kind → what it does → read-only / writes-where)

All 16 are registered in `hermes_quant/__init__.py` under `toolset="quant"`. **None is an execution tool.** "Writes" below means writes to *our own* state under `~/.hermes/quant/` (proposal store / watchlist / paper-reactor journal) — never a live broker, never Hermes core SQLite.

| # | Tool | Kind | What it does | Read-only? |
|---|---|---|---|---|
| 1 | `quant_status` | view | Daemon/state status: last signal time, halts, mode | read-only |
| 2 | `quant_show_signals` | view | Tail the `signals.jsonl` bus | read-only |
| 3 | `quant_show_views` | view | Per-analyst `AnalystView`s for a symbol | read-only |
| 4 | `quant_recommend` | view (compute) | Run analysts→BMA→gate for one symbol; returns the advisor recommendation. Pure compute, **no order** | read-only |
| 5 | `quant_recipes` | view | List available PDR recipes | read-only |
| 6 | `quant_propose` | HITL write | Create a pending **proposal record** for human approval (ADR-0015). Writes proposal store; **no fill** | writes proposal store |
| 7 | `quant_approve` | HITL write | Approve a pending proposal → fires **`PaperReactor`** on the record (paper fill, journaled). Operates on an existing human-surfaced record; **never a live broker order** | writes paper journal |
| 8 | `quant_reject` | HITL write | Reject a pending proposal with a reason | writes proposal store |
| 9 | `quant_pending` | view | List pending proposals | read-only |
| 10 | `quant_proposal` | view | Look up one proposal record | read-only |
| 11 | `quant_autonomous_tick` | view/dry | Run an autonomous PDR tick; **defaults to dry-run** (paper only when armed via CLI/cron env) | read-only by default |
| 12 | `quant_autonomous_status` | view | Mode, watchlist, gate config, kill-switch state | read-only |
| 13 | `quant_watchlist_add` | config write | Add/update a watchlist entry (no money) | writes watchlist |
| 14 | `quant_watchlist_remove` | config write | Remove a watchlist entry | writes watchlist |
| 15 | `quant_watchlist_list` | view | List watchlist entries | read-only |
| 16 | `quant_doctor` | view | Health check: data feeds, calibration drift, halts | read-only |

Plugin tools **bypass the toolset filter** (always visible to the chat LLM once the plugin loads). Tool handlers return a **JSON string** (`json.dumps({...})`) per the Hermes tool convention.

### 2.2 The slash command and CLI

- `register_command("quant", handler=handle_quant_slash, ...)` → `/quant status | recommend <SYM> | propose <SYM> | approve <ID> | reject <ID> <reason> | pending | doctor` in **both** the CLI and gateway (Discord) sessions.
- `register_cli_command("quant", ..., setup_fn=cli.setup_argparse, handler_fn=cli.dispatch)` → the **`hermes quant ...` control plane**, reachable only from a shell, never from the chat LLM. This is where money-moving lifecycle lives: `hermes quant start | stop | restart | backtest | setup`. Per AGENTS.md the CLI uses **`--use-profile`** (not `--profile`, which collides with the global flag).

### 2.3 The two hooks

- **`pre_gateway_dispatch`** (correctly wired): fires once per incoming `MessageEvent` before auth/dispatch, with `event, gateway, session_store` kwargs. Our `install_quant_slash_on_pre_dispatch(**kwargs)` uses it purely as a **first-message latch** to lazily install the Discord `/quant` app-command on the now-live `gateway.adapters["discord"]` and force `tree.sync()` (the adapter's fingerprint-skip would otherwise hide a late-added slash). Idempotent (module-global sentinel), returns `None` (never alters flow), no-ops in CLI mode where `gateway` is absent.
- **`on_session_start`** — **declared in `plugin.yaml` but NOT wired** in `register()`. Harmless (a declared-but-unregistered hook simply never fires), but the manifest is inaccurate. See §6.

### 2.4 The skill

`register_skill("hermes-quant", <pkg>/skills/hermes-quant/SKILL.md)` registers the bundled voice/ops playbook, resolvable as `hermes-quant:hermes-quant`. **Note:** this is distinct from the larger operator skill the host actually leans on at runtime, `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md` (the "approve LSCC / halt SOFI / what's pending" ops skill) — that one lives in the Hermes skills tree, not in the package.

---

## 3. Money-via-CLI-only + read-only-tools enforcement — and how Hermes upholds it

**Hermes does not know "money" from "read-only."** Once the plugin loads, the chat LLM can call any registered tool. So the invariant — *plugin tools are read-only views; live trading is CLI-only with confirmation* (AGENTS.md "Money never goes through tools", ADR-0007) — is enforced **by us, structurally**, not by a Hermes capability check:

1. **No execution tool is ever registered.** The 16-tool surface contains read views, a `recommend` that only computes, watchlist/proposal config writes, and `approve` which fires only the **`PaperReactor`** on an already-human-surfaced proposal record. There is no tool that opens a live broker order. (Confirmed: `quant_approve` imports `hermes_quant.react.PaperReactor` and `hermes_quant.proposals.get_default_store` — paper + proposal-store only.)
2. **The dangerous lifecycle is CLI-only.** `start / stop / restart / backtest` live under `register_cli_command`, reachable only from a shell, never from chat.
3. **Autonomy is off by default in tools.** `quant_autonomous_tick` defaults to dry-run; paper firing requires the armed cron env (`HERMES_QUANT_AUTONOMY=paper`, or the wrapper-set `HERMES_QUANT_AUTONOMOUS=1 + _ARMED=1`), set on the deployed cron, not reachable by an LLM "yeah do that."
4. **The deterministic gate is the final authority** (ADR-0004/ADR-0079 D-1): LLM/committee/semantic/social are evidence that can only *silence*, never authorize. No LLM is on the action path.
5. **No `requires_env`.** `plugin.yaml` uses `optional_env` only, so install is never gated and all 16 tools are always available; yfinance bootstrap needs zero credentials. AGENTS.md: NEVER `requires_env` (it blocks install).

What Hermes *does* uphold: it scopes the CLI surface to the shell (the chat LLM cannot invoke `register_cli_command` handlers), it keeps the `.env` (credentials + `HERMES_QUANT_*` flags) tool-guarded, and it runs `register()` in a try/except so a plugin fault degrades to "plugin disabled" rather than crashing the gateway.

---

## 4. How the PDR pipeline runs UNDER Hermes

ADR-0079 ratifies **Perception → Decision → Reaction** as the organizing architecture. Critically, **the pipeline does not run inside `register()`** (which only wires surfaces and returns). It runs as scheduled work on the Hermes host:

```
            ┌──────────────────────── Hermes gateway (process) ─────────────────────────┐
            │  register(ctx) at startup → 16 read-only tools + /quant slash + hooks +    │
            │                              hermes quant CLI + skill                       │
            │                                                                             │
  operator ─┼─ chat / Discord  ──►  /quant, quant_* tools  ──►  READ ~/.hermes/quant/*    │
            │                              (advisor + HITL surface)                       │
            └──────────────────────────────────────────────────────────────────────────-┘
                                              ▲ reads
                                              │
            ┌──────── Hermes DB-backed cron scheduler (~/.hermes/cron/jobs.json) ────────┐
            │  16 quant crons fire the DEPLOYED ~/.hermes/scripts/quant-*.py             │
            │  → PERCEPTION (scan/fetch/regime/catalyst) → DECISION (BMA→gate)           │
            │  → REACTION (PaperReactor) → WRITE ~/.hermes/quant/{signals.jsonl,         │
            │     ticks.db, proposals.db, executions.jsonl, state.json}                  │
            └────────────────────────────────────────────────────────────────────────--┘
```

- **PERCEPTION** (scan + analysis): the universe-scan / catalyst-ingest / watchlist-evolve crons select symbols, fetch bars, build regime, stage semantic packets. ADR-0079's future `PerceptionFrame` is the typed carrier here (default-OFF, not built).
- **DECISION** (deliberation + risking): the advisor / playbook-tick / autonomous-tick crons run analysts → `BMAAggregator` (under `require_ensemble`) → optional risk-debate committee → the deterministic `DefaultRiskGate` (the FINAL authority).
- **REACTION** (acting): the `PaperReactor` fills the gated Action (paper only). Live execution is out of scope until the ADR-0077 fidelity foundation + a separate live-promotion decision.

**The tools are read-only *views* into the state the crons write.** The chat/Discord surface is where the operator reads briefs (`/quant status`, `quant_pending`) and runs the HITL loop (`approve <ID>` → `quant_approve` → PaperReactor). This is the seam ADR-0079 D79.2 describes: `recommend()` gains an optional `perception_frame` kwarg (None = today's behavior), so a future frame threads identically through the tool path and the cron path.

### 4.1 The 16 quant crons (live `~/.hermes/cron/jobs.json`, all enabled)

Schedules are UTC-stored cron exprs; the operator thinks in PT/ET. `no_agent=True` = the deployed script does everything and Hermes just delivers its stdout; `no_agent=False` = an LLM-driven prompt brief.

| Cron name | Schedule (expr) | Driver | Mode | PDR role |
|---|---|---|---|---|
| `quant-universe-scan-daily` | `15 3 * * 1-5` | `quant-universe-scan.py` | no_agent | PERCEPTION / scanning |
| `quant-watchlist-evolve-daily` | `30 3 * * 1-5` | `quant-watchlist-evolve.py` | no_agent | PERCEPTION / universe evolution |
| `quant-catalyst-coverage-daily` | `45 3 * * 1-5` | `quant-catalyst-coverage.py` | no_agent | PERCEPTION / coverage watchdog |
| `quant-halts-watchdog-daily` | `0 5 * * *` | `quant-halts-watchdog.py` | no_agent | safety / orphan-halt watchdog |
| `quant-daily-premarket-interim` | `30 5 * * 1-5` | LLM prompt → `quant-daily-interim.py` | agent | DECISION / advisor brief |
| `quant-playbook-tick-daily` | `0 6 * * 1-5` | `quant-playbook-tick-armed.sh` | no_agent | DECISION / playbook tick |
| `quant-playbook-weekly` | `30 6 * * 1` | `quant-playbook-weekly.py` | no_agent | DECISION / weekly rebalance |
| `quant-playbook-quarterly` | `30 6 1-7 1,4,7,10 1` | `quant-playbook-quarterly.py` | no_agent | review / quarterly |
| `quant-proposals-ttl-watchdog-daily` | `30 6 * * 1-5` | `quant-proposals-ttl-watchdog.py` | no_agent | HITL / TTL watchdog |
| `quant-catalyst-ingest-30min` | `0,30 6-13 * * 1-5` | `quant-catalyst-ingest.py` | no_agent | PERCEPTION / semantic packets |
| `quant-hourly-market-tick` | `0 7-13 * * 1-5` | `quant-hourly-tick-armed.sh` | no_agent | DECISION / hourly tick |
| `quant-daily-midday-interim` | `0 8 * * 1-5` | LLM prompt → `quant-daily-interim.py` | agent | DECISION / midday advisor |
| `quant-autonomous-tick-30min` | `30 6-13 * * 1-5` | `quant-autonomous-tick-armed.sh` | no_agent | DECISION+REACTION / autonomous PDR tick |
| `quant-daily-eod-interim` | `30 12 * * 1-5` | LLM prompt → `quant-daily-interim.py` | agent | DECISION / EOD advisor |
| `quant-portfolio-daily-eod` | `5 13 * * 1-5` | `quant-portfolio-daily.py` | no_agent | reporting / portfolio snapshot |
| `quant-strategy-retro-weekly` | `0 13 * * 0` | `quant-strategy-retro-weekly.py` | no_agent | feedback / weekly retro |

### 4.2 How the trading crons get registered on the Hermes host

**Crons are NOT registered by `register(ctx)` and NOT by cron(8).** They are registered through the Hermes **`cronjob` agent/MCP tool** (`action=create | list | delete`), which writes the DB-backed scheduler registry at `~/.hermes/cron/jobs.json`. The Hermes gateway (not crontab) fires them on schedule. Do **not** use a Claude-session `CronCreate` — that is a different scheduler and will not register a Hermes host cron.

A cron job record carries (fields confirmed from `jobs.json`): `id, name, prompt, skill, model, provider, script, no_agent, schedule {kind:"cron", expr}, repeat, enabled, deliver, origin, enabled_toolsets, workdir, last_status, last_run_at, next_run_at`.

- `no_agent=True` + `script="quant-X.py"` → the gateway runs the deployed script directly and delivers its stdout (the tiered emit shape from the ops skill: per-fire delta / summary / silent heartbeat).
- `no_agent=False` + `prompt=...` → the gateway runs an LLM turn with that prompt and `enabled_toolsets` (the advisor briefs).
- The **armed wrapper pattern**: `script="quant-X-armed.sh"` is a thin shell wrapper that sets autonomy env (`HERMES_QUANT_AUTONOMY=paper`, `_ARMED=1`) + `flock` before calling the `.py`. Reverting to dry-run = point `script:` back at the bare `.py`. This is the reversible autonomy toggle.

Inspect / verify:
```bash
~/.hermes/hermes-agent/venv/bin/hermes cron list 2>&1 | grep -E "quant-|Schedule:"
# or read the registry directly:
~/.hermes/hermes-agent/venv/bin/python3 -c \
  "import json; d=json.load(open('$HOME/.hermes/cron/jobs.json')); \
   print([j['name'] for j in d['jobs'].values() if 'quant' in j['name']])"
```

### 4.3 CRITICAL: deployed scripts drift from the repo

The crons run the **deployed** `~/.hermes/scripts/quant-*.py`, which **drifts** from the repo `ops/scripts/quant-*.py` and is **not git-tracked** (e.g. the live `quant-daily-interim.py` is ~875 lines with proposal-creation + auto-approve + open-guard wiring; the repo copy has been a 247-line stub). When changing cron behavior: **edit the deployed copy** (it is what runs), but put reusable logic in the importable `hermes_quant.*` package (tracked + tested) and keep the script a thin caller. First diagnostic on unexpected behavior:
```bash
md5sum ~/.hermes/scripts/quant-X.py /mnt/e/CS/github/hermes-quant/ops/scripts/quant-X.py
```

---

## 5. LIVE BLOCKER + manifest / version mismatch checklist

### 5.1 [BLOCKER] hermes-quant is not enabled → it never loads

`standalone` + entry-point plugins are **opt-in**. The loader gate:
```python
is_enabled = (enabled is not None and (lookup_key in enabled or manifest.name in enabled))
if not is_enabled:   # -> LoadedPlugin(enabled=False), "not enabled in config"
```

**Live probe (today):**
```
$ ~/.hermes/hermes-agent/venv/bin/python3 -c "import yaml,os; \
    print(yaml.safe_load(open(os.path.expanduser('~/.hermes/config.yaml')))['plugins']['enabled'])"
['discord-session-link', 'discord-triage', 'disk-cleanup', 'hermes-discord-plugin', 'hermes-s2s']
```
`hermes-quant` is **absent** → none of the 16 tools, the `/quant` slash, the `hermes quant` CLI, the skill, or the `pre_gateway_dispatch` hook are active in the running gateway right now.

**Fix:**
```bash
~/.hermes/hermes-agent/venv/bin/hermes plugins enable hermes-quant   # then restart the gateway
```
Note: the config-schema grandfathering rule only auto-enables plugins already present under `~/.hermes/plugins/`. **Entry-point plugins are never grandfathered** — this opt-in is required and expected.

> Caveat: the **crons are already live and firing** (they are registered directly in the cron scheduler and run the deployed scripts, independent of plugin-enable state). So the PDR pipeline runs today; what is dark is the **chat/Discord tool + slash surface** until the plugin is enabled.

### 5.2 Manifest / version drift checklist

- [ ] **[MINOR] `provides_tools` undercount.** `plugin.yaml` lists **15**; `register()` registers **16** (`quant_recipes` is missing from the YAML). Introspection-only field; no functional impact. **Fix:** add `quant_recipes` to `provides_tools`.
- [ ] **[MINOR] `provides_hooks` over-declares.** `plugin.yaml` declares `on_session_start`, but `register()` never calls `register_hook("on_session_start", ...)`. **Fix:** either wire an `on_session_start` callback or drop it from `provides_hooks`. (`pre_gateway_dispatch` is correctly wired.)
- [ ] **[ADVISORY] `version: "0.4.4"` lags the feature work.** The brief's premise "system at v0.6.x" refers to the **plugin's own** internal v0.6.x feature line (regime-in-state, bull/bear debate, fundamentals, ADR-0079 PDR), **not** the host. There is **no host-version compatibility field** in `PluginManifest`, so no handshake can fail — the host is `hermes-agent` **v0.15.1** and is unrelated. The drift is purely *release hygiene*: `plugin.yaml` `version` and `hermes_quant.__version__` both say `0.4.4` while the design docs are at v0.6.x. **Fix (non-blocking):** bump both `plugin.yaml:version` and `__init__.py:__version__` to the current v0.6.x line for honesty, and keep them in sync.
- [ ] **[NON-ISSUE] `manifest_version: 1`.** Not a recognized field; silently dropped. Keep or remove freely.
- [ ] **[GOOD — keep]** `optional_env` (not `requires_env`); no per-tool `requires_env`. Correct: install is never gated, yfinance bootstrap needs zero credentials.

---

## 6. "Verify install" smoke section

### 6.1 register() MockCtx smoke (from AGENTS.md — proves register() wires cleanly)

```bash
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
**Expected:** 16 `tool quant_*` lines, `cmd quant`, `cli quant`, `hook pre_gateway_dispatch`, `skill hermes-quant`. If any tool is missing or a method raises, fix before enabling.

### 6.2 Discovery + load-state probe

```bash
# 1. Hermes discovers us as an entry-point plugin
~/.hermes/hermes-agent/venv/bin/python3 -c "import importlib.metadata as m; \
  print('discovered:', any(e.name=='hermes-quant' for e in m.entry_points().select(group='hermes_agent.plugins')))"

# 2. Are we enabled? (the §5.1 blocker check)
~/.hermes/hermes-agent/venv/bin/python3 -c "import yaml,os; \
  en=yaml.safe_load(open(os.path.expanduser('~/.hermes/config.yaml')))['plugins']['enabled']; \
  print('enabled:', 'hermes-quant' in en)"

# 3. After enabling + restart: is the plugin loaded with tools live?
~/.hermes/hermes-agent/venv/bin/hermes plugins list 2>&1 | grep -i quant
```

### 6.3 Live data-flow probe (tools/CLI work end-to-end)

```bash
~/.hermes/hermes-agent/venv/bin/hermes quant doctor      # CLI control plane: data feeds, calibration, halts
~/.hermes/hermes-agent/venv/bin/hermes quant status      # last signal time, mode, halts
ls -la ~/.hermes/quant/                                  # signals.jsonl / ticks.db / proposals.db exist?
```
Per the ops skill's production-readiness lesson: **tests passing ≠ system works tomorrow.** The MockCtx smoke verifies *wiring*; running the actual CLI + cron scripts verifies *data flow*. Both are required before relying on the install.
