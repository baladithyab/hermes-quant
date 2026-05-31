# R: Hermes Agent plugin system — does hermes-quant's plugin.yaml + register(ctx) match?

- **Date:** 2026-05-30
- **Author:** research subagent (deep-work-loop)
- **Status:** confirmed against three independent sources
- **Scope:** confirm hermes-quant's `plugin.yaml` + `register(ctx)` are correct against how Hermes actually discovers and loads plugins.

## Sources (cite key)

| Key | Source | Trust |
|-----|--------|-------|
| **[LOCAL-PM]** | `~/.hermes/hermes-agent/hermes_cli/plugins.py` — the **installed** PluginManager / PluginContext on THIS machine, hermes-agent **v0.15.1** | authoritative for this deployment |
| **[LOCAL-CFG]** | `~/.hermes/config.yaml` (`plugins.enabled`) | authoritative — live config |
| **[LOCAL-INIT]** | `/mnt/e/CS/github/hermes-quant/hermes_quant/__init__.py` (`register(ctx)`) | the actual impl |
| **[LOCAL-YAML]** | `/mnt/e/CS/github/hermes-quant/plugin.yaml` + `pyproject.toml` | our manifest + entry points |
| **[LOCAL-AGENTS]** | `/mnt/e/CS/github/hermes-quant/AGENTS.md` "Plugin authoring constraints" | authoritative project constraints |
| **[DOCS]** | https://hermes-agent.nousresearch.com/docs (Plugins, Build a Hermes Plugin, Hooks, Tools Runtime, Model Provider) — REACHABLE | public docs, match LOCAL-PM |
| **[GH]** | `NousResearch/hermes-agent/blob/main/hermes_cli/plugins.py`, `website/docs/.../hooks.md`, PR #1555 | public source, matches LOCAL-PM |
| **[DW]** | deepwiki `ask_question` on `NousResearch/hermes-agent` | corroborating |

Hermes docs **ARE publicly reachable** (contrary to the brief's hedge), and the
installed source on disk **[LOCAL-PM]** matches them. Where they agree I cite both;
where the installed copy is authoritative for OUR deployment I lead with [LOCAL-PM].

---

## TL;DR (5 bullets, <200 words)

1. **Manifest + register() are SHAPE-CORRECT.** `plugin.yaml` fields (`name, version, description, author, kind, provides_tools, provides_hooks, requires_env/optional_env`) and all five `ctx.register_*` calls match the installed `PluginManifest`/`PluginContext` exactly [LOCAL-PM]. `kind: standalone` is the right kind. Our register() runs clean in the smoke test.
2. **We ship as a PIP entry-point plugin** (`[project.entry-points."hermes_agent.plugins"] hermes-quant = "hermes_quant"`), not a `~/.hermes/plugins/` dir. That is a first-class, supported discovery source (source #4) [LOCAL-PM, GH].
3. **LIVE BLOCKER:** `standalone` + entry-point plugins are **opt-in** — they only load if their name is in `config.yaml`'s `plugins.enabled`. `hermes-quant` is **NOT** in that list today, so it is discovered but **never loaded**. Fix: `hermes plugins enable hermes-quant`.
4. **Both hooks are valid** (`on_session_start`, `pre_gateway_dispatch` ∈ `VALID_HOOKS`). All 16 tools register read-only; money stays CLI-only — compliant with ADR-0007.
5. **`manifest_version: 1` is IGNORED (harmless); `version: "0.4.4"` is fine** — it is the *plugin's* version, unrelated to hermes-agent v0.15.1. The brief's "system at v0.6.x" is wrong: host is **v0.15.1**.

---

## 1. How Hermes discovers + loads a plugin

**Manifest file name + schema.** The manifest is **`plugin.yaml`** [DOCS, GH, DW]. The
installed `PluginManifest` dataclass [LOCAL-PM] has exactly these fields:

```python
name: str
version: str = ""
description: str = ""
author: str = ""
requires_env: List[Union[str, Dict[str, Any]]] = []   # strings OR rich dicts
provides_tools: List[str] = []
provides_hooks: List[str] = []
source: str = ""            # "user" | "project" | "entrypoint" (set by loader)
path: Optional[str] = None
kind: str = "standalone"
key: str = ""               # path-derived registry key; falls back to name
```

**`kind: standalone` meaning** [LOCAL-PM docstring]: "hooks/tools of its own; opt-in via
`plugins.enabled`." The valid kinds are `{standalone, backend, exclusive, platform,
model-provider}`. standalone is the correct kind for hermes-quant — it adds its own
tools/hooks/CLI/skill, it is not a backend for a core tool, not a memory provider, not a
gateway adapter, not an LLM provider.

**Where plugins live (4 discovery sources, later overrides earlier on name collision)**
[LOCAL-PM module docstring, GH, DW]:
1. Bundled — `<repo>/plugins/<name>/`
2. User — `~/.hermes/plugins/<name>/`
3. Project — `./.hermes/plugins/<name>/` (gated by `HERMES_ENABLE_PROJECT_PLUGINS`)
4. **Pip — packages exposing the `hermes_agent.plugins` entry-point group** ← hermes-quant uses THIS.

Each directory plugin needs `plugin.yaml` **and** `__init__.py` with `register(ctx)`.
Entry-point plugins skip the dir requirement: the manifest is synthesized from the EP
(`name=ep.name, source="entrypoint", path=ep.value, key=ep.name`) and the module is loaded
via `ep.load()` [LOCAL-PM `_scan_entry_points` / `_load_entrypoint_module`, lines 1375–1528].

**`manifest_version`** — NOT a field on `PluginManifest`. The YAML parser keeps only the
known fields; `manifest_version: 1` is silently ignored. It is **harmless but inert** — it
does not gate or version-check anything. Leave it or drop it; no behavior change.

**Verified on THIS machine:**
```
$ python -c "import importlib.metadata as m; ... entry_points ..."
hermes_agent.plugins :: hermes-quant = hermes_quant          # ← EP registered ✓
console_scripts      :: hermes-quant-daemon = hermes_quant.daemon.main:main
console_scripts      :: hermes-quant-trainer = hermes_quant.training.main:main
```
So Hermes **will discover** hermes-quant as an entry-point plugin. Whether it **loads** is §3 below.

---

## 2. The `register(ctx)` contract — methods, signatures, and whether we match

`register(ctx)` is called **exactly once at startup**; if it raises, that plugin is disabled
and Hermes continues fine [DOCS, GH, LOCAL-PM]. Matches [LOCAL-AGENTS] "called ONCE at
gateway startup; no daemon spawning." Our impl spawns nothing and does lazy imports
(`register()` runs in <50 ms per its own docstring) — compliant.

Installed `PluginContext` methods [LOCAL-PM, exact signatures from disk]:

| Method | Installed signature | Our call [LOCAL-INIT] | Match |
|--------|---------------------|------------------------|-------|
| `register_tool` | `(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="", override=False)` | `name=, toolset="quant", schema=, handler=` ×16 | ✓ (positional/kw subset; required 4 supplied) |
| `register_command` | `(name, handler, description="", args_hint="")` — handler is `fn(raw_args: str) -> str \| None`, sync or async | `("quant", handler=, description=)` | ✓ |
| `register_cli_command` | `(name, help, setup_fn, handler_fn=None, description="")` — setup_fn gets an argparse subparser | `(name="quant", help=, setup_fn=, handler_fn=)` | ✓ |
| `register_hook` | `(hook_name, callback)` — unknown names warn but are still stored (forward-compat) | `("pre_gateway_dispatch", install_quant_slash_on_pre_dispatch)` | ✓ |
| `register_skill` | `(name, path: Path, description="")` — resolvable as `'<plugin>:<name>'` via `skill_view()`; NOT in flat `~/.hermes/skills/` tree | `("hermes-quant", skill_md)` | ✓ |
| `ctx.llm` (property) | host-owned LLM facade (`PluginLlm`) | not used | n/a |
| `ctx.dispatch_tool(name, args)` | invoke any tool with parent ctx | not used | n/a |
| `ctx.inject_message(content, role)` | inject into active conversation | not used | n/a |

**Return contract:** `register()` returns `None`. Tool handlers return a **string** (Hermes
treats the return as the tool-result string; JSON-as-string is the idiom — the example
`hello_world` does `return json.dumps({...})`) [DOCS, GH]. Our [LOCAL-AGENTS] rule "tools
return JSON-serializable dicts/strings only" is consistent; confirm handlers return
`json.dumps(...)` (string), not a bare dict, to be safe across versions. Tool registration
collisions: same name in a different toolset is **rejected** unless `override=True`
[LOCAL-PM register_tool docstring]. Our 16 `quant_*` names are unlikely to collide.

**Verdict: our `register(ctx)` matches the installed contract on all five methods.** The
AGENTS.md MockCtx smoke test in §"Smoke test the plugin" exercises exactly these five.

---

## 3. Tools vs CLI vs hooks — the read-only-tools / CLI-only-money rule

**How Hermes routes them** [DOCS Tools Runtime, LOCAL-PM]:
- **Tools** (`register_tool`) enter the global `ToolRegistry`; the LLM can call any of them
  in chat. Plugin tools **bypass the toolset filter** (always visible once the plugin loads)
  [GH PR #1555: "Plugin tools bypass the toolset filter automatically"].
- **Slash command** (`register_command`) → `/quant ...` in CLI + gateway sessions.
- **CLI subcommand** (`register_cli_command`) → `hermes quant ...` — a control-plane surface
  reachable only from a shell, never from the chat LLM.
- **Hooks** (`register_hook`) fire in both CLI and gateway.

**The money rule is enforced by US, not by Hermes.** Hermes does **not** know "money" from
"read-only"; it will happily let the LLM call any registered tool. So the [LOCAL-AGENTS]
invariant — *plugin tools are read-only views, live trading is CLI-only with confirmation* —
is an architectural discipline we enforce by **never registering an execution tool**, only
read/propose/approve tools, and putting `start/stop/backtest` under `register_cli_command`
(ADR-0007). The HITL surface (`quant_propose/approve/reject`) writes to a pending-proposal
store, not a broker — the actual fill path is CLI-gated.

**Do our 16 tools + 2 hooks comply?**
- **16 tools** registered in [LOCAL-INIT], all `toolset="quant"`: `quant_status`,
  `quant_show_signals`, `quant_show_views`, `quant_recommend`, `quant_recipes`,
  `quant_propose`, `quant_approve`, `quant_reject`, `quant_pending`, `quant_proposal`,
  `quant_autonomous_tick`, `quant_autonomous_status`, `quant_watchlist_add`,
  `quant_watchlist_remove`, `quant_watchlist_list`, `quant_doctor`.
  - **Manifest/impl MISMATCH (minor):** `plugin.yaml` `provides_tools` lists **15** —
    it **omits `quant_recipes`** (registered in `__init__.py` at lines 114–119). Manifest
    `provides_tools` is **declarative/introspection only** — the loader does NOT cross-check
    it against actual `register_tool` calls, so the tool still works. But the manifest is
    now inaccurate (15 declared vs 16 registered). **Add `quant_recipes` to `provides_tools`.**
  - None of the 16 are execution tools — compliant with the read-only rule. `quant_approve`
    approves a *proposal record*; it does not itself place a broker order.
- **2 hooks declared** (`on_session_start`, `pre_gateway_dispatch`).
  - Both ∈ `VALID_HOOKS` [LOCAL-PM] ✓.
  - **MISMATCH (declared-vs-registered):** `register()` [LOCAL-INIT] registers **only**
    `pre_gateway_dispatch` (line 224). It does **NOT** call
    `ctx.register_hook("on_session_start", ...)` anywhere. The manifest declares a hook the
    code never wires. Harmless (manifest is introspection-only; an undeclared hook still
    fires, and a declared-but-unregistered hook simply never fires), but it makes the
    manifest lie. **Either wire an `on_session_start` callback or drop it from `provides_hooks`.**

---

## 4. The two hooks — what they're for, when they fire

- **`pre_gateway_dispatch`** [LOCAL-PM exact comment]: *"Fired once per incoming MessageEvent
  after the internal-event guard but BEFORE auth/pairing and agent dispatch."* Kwargs:
  `event: MessageEvent, gateway: GatewayRunner, session_store`. A plugin may return
  `{"action":"skip","reason":...}` / `{"action":"rewrite","text":...}` / `{"action":"allow"}`
  or `None`.
  - **Our use:** `install_quant_slash_on_pre_dispatch(**kwargs)` [LOCAL discord_slash.py]
    uses it purely as a **first-message latch** to lazily install the Discord `/quant`
    app-command on a now-live `gateway.adapters["discord"]` client and force `tree.sync()`
    (the adapter's fingerprint-skip would otherwise hide a late-added slash). It is
    idempotent (module-global sentinel), reads `kwargs.get("gateway")`, returns `None`
    (never influences flow). This matches the documented kwargs (`gateway` is passed) — ✓.
    Gateway-only effect; in CLI mode `gateway` is absent and it no-ops. Correct.
- **`on_session_start`** [LOCAL-PM / DOCS hooks.md]: fires when a **new session is created
  (first turn only)**; return value ignored (fire-and-forget observer). Typical use:
  per-session init / banner / warm-up. **hermes-quant declares it but does not register it**
  (see §3). If we want it (e.g., to surface "quant daemon is/ isn't running" once per
  session, or warm the signals reader), wire it; otherwise remove the declaration.

All hook callbacks must accept `**kwargs` for forward-compat [DOCS hooks.md]; ours does.

---

## 5. Mismatches between our plugin.yaml / register() and what Hermes expects

Ordered by operational severity:

1. **[BLOCKER — not a bug, a config gap] hermes-quant is not enabled, so it never loads.**
   `standalone` + entry-point plugins are **opt-in**: the loader gate [LOCAL-PM lines
   1166–1186] is
   ```python
   is_enabled = (enabled is not None
                 and (lookup_key in enabled or manifest.name in enabled))
   if not is_enabled:  # -> LoadedPlugin(enabled=False), error="not enabled in config..."
   ```
   `config.yaml` `plugins.enabled` today = `[discord-session-link, discord-triage,
   disk-cleanup, hermes-discord-plugin, hermes-s2s]` [LOCAL-CFG] — **`hermes-quant` is
   absent.** Therefore none of the 16 tools, the `/quant` slash, the `hermes quant` CLI, the
   skill, or the `pre_gateway_dispatch` hook are active in the running gateway right now.
   - **Fix:** `hermes plugins enable hermes-quant` (or add `- hermes-quant` under
     `plugins.enabled` in `~/.hermes/config.yaml`) and restart the gateway. Note the
     [DOCS] grandfathering rule (config schema v21+) only auto-enables plugins already
     present under `~/.hermes/plugins/`; **entry-point plugins are never grandfathered** —
     so this opt-in is required and expected.

2. **[MINOR] `provides_tools` undercount.** Manifest lists 15; code registers 16
   (`quant_recipes` missing from YAML). Introspection-only field, no functional impact, but
   inaccurate. **Add `quant_recipes`.**

3. **[MINOR] `provides_hooks` over-declares.** Manifest declares `on_session_start` but
   `register()` never calls `register_hook("on_session_start", ...)`. **Either wire it or
   drop it.** (`pre_gateway_dispatch` is correctly wired.)

4. **[NON-ISSUE] `version: "0.4.4"`.** This is the **plugin's** version and is independent of
   the host. The brief's premise ("system is at v0.6.x") is **incorrect** for this machine —
   `hermes-agent` is **v0.15.1** (`importlib.metadata.version('hermes-agent')` and
   `~/.hermes/hermes-agent/pyproject.toml`). There is **no host-version compatibility field**
   in `PluginManifest`, so no version handshake exists to fail. Bumping plugin `version` is a
   release-hygiene choice (it lags repo work that's at HITL/autonomous/catalyst features),
   not a load requirement. Recommend bumping for honesty, but it is not blocking.

5. **[NON-ISSUE] `manifest_version: 1`.** Not a recognized field; silently dropped by the
   parser. Still "valid" in the sense that it does not break parsing. No behavior. Keep or
   remove freely.

6. **[GOOD] `optional_env` not `requires_env`.** [LOCAL-YAML] uses `optional_env`.
   `PluginManifest` only has `requires_env` (the field that, when set, is prompted during
   `hermes plugins install` and gates availability). By using `optional_env` we (a) avoid the
   install-time gate — matching [LOCAL-AGENTS] "NEVER requires_env (the latter blocks
   install)" — and (b) `optional_env` is itself an **unrecognized field** that the parser
   ignores, so it documents keys for humans without creating any Hermes-side requirement.
   This is exactly the intended behavior: yfinance bootstrap needs zero credentials.

7. **[OK] No `requires_env` on the tools either.** `register_tool` supports a per-tool
   `requires_env`; we pass none, so all 16 tools are always available once the plugin loads.
   Correct for read-only views that degrade gracefully (yfinance default).

### One-line confirmation
> hermes-quant's `plugin.yaml` and `register(ctx)` are **structurally correct and load-safe**
> for hermes-agent v0.15.1. The only thing standing between "installed" and "live" is the
> opt-in: **`hermes plugins enable hermes-quant`**. Two cosmetic manifest drifts
> (missing `quant_recipes`, over-declared `on_session_start`) should be cleaned up but do not
> affect loading.

---

## Appendix — exact discovery → load path for an entry-point plugin (from [LOCAL-PM])

1. `discover_and_load()` scans 4 sources; `_scan_entry_points()` (line 1375) reads
   `importlib.metadata.entry_points().select(group="hermes_agent.plugins")` → finds
   `hermes-quant = hermes_quant` → synthesizes `PluginManifest(name="hermes-quant",
   source="entrypoint", path="hermes_quant", key="hermes-quant")`.
2. For each manifest: model-provider → recorded, not imported; bundled backend/platform →
   auto-load; **everything else (us) → gated on `plugins.enabled`** (line 1171).
3. If enabled → `_load_plugin()` → `_load_entrypoint_module()` → `ep.load()` imports
   `hermes_quant` → builds `PluginContext(manifest, manager)` → calls
   `hermes_quant.register(ctx)` once.
4. `register_tool` → `tools.registry.register(...)` + tracks name in `_plugin_tool_names`;
   `register_hook` → appends to `_hooks[hook_name]`; invoked later via
   `invoke_hook(name, **kwargs)`.
