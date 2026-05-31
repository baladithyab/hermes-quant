# R: The Hermes `cronjob` mechanism — exact registration API for the trading crons

- **Date:** 2026-05-30
- **Author:** research subagent (deep-work-loop)
- **Status:** Ground-truth verified against installed hermes-agent source on this host
- **Task:** backlog #14 — Cron registry + registration runbook (all trading crons)
- **Scope:** how the hermes-quant trading crons get registered on the Hermes host, the exact
  `cronjob action='create'` field list, deliver modes, no_agent semantics, PT↔ET timezone
  conversion, the deploy-sync requirement, and the POSIX DOM/DOW OR-bug.

> **Authority note.** Everything below is cited from the *installed* hermes-agent source and the
> live cron config on this machine, not from memory. Primary sources:
> - `~/.hermes/hermes-agent/tools/cronjob_tools.py` — the `cronjob` tool definition + JSON schema (THE API).
> - `~/.hermes/hermes-agent/cron/scheduler.py` — the tick loop, script runner, timeout resolution.
> - `~/.hermes/hermes-agent/cron/jobs.py` — job storage + timezone normalization.
> - `~/.hermes/cron/jobs.json` — the live persisted registry (33 jobs, 16 of them `quant-*`).
> - `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md` + `references/` — operator-curated runbooks.

---

## 0. TL;DR

1. **The tool is `cronjob`** (an agent/MCP tool registered in `tools/cronjob_tools.py`, toolset `cronjob`,
   emoji ⏰). It is the *only* supported way to register a job — NOT `cron(8)`/crontab, NOT the
   Claude-session `CronCreate`. Storage is `~/.hermes/cron/jobs.json` (JSON-file scheduler ticked by the
   gateway). `action ∈ {create, list, update, pause, resume, remove, run}`. **Only `action` is strictly
   required; `create` additionally requires `schedule`, and requires `script` when `no_agent=True`.**
2. **`action='create'` field list** (all snake_case kwargs on the tool):
   `action='create'`, **`schedule`** (cron expr / `"30m"` / `"every 2h"` / ISO ts — REQUIRED),
   `name`, `prompt` (required UNLESS `no_agent`), `script` (relative to `~/.hermes/scripts/`, REQUIRED when
   `no_agent=True`), `no_agent` (bool), `deliver` (`origin`/`local`/`all`/`platform:chat_id:thread_id`),
   `enabled_toolsets` (list, e.g. `["terminal","file"]`), `repeat` (int), `skills` (list), `model` (obj),
   `provider`, `base_url`, `context_from` (list of upstream job_ids), `workdir`, `profile`.
   **There is NO `timeout`, NO `env`, NO `enabled` create param** (see §1.3, §6).
   `action='list'` (optional `include_disabled`); `action='remove'`/`pause`/`resume`/`run`/`update` take `job_id`
   (resolves by id OR name).
3. **deliver + no_agent:** trading watchdogs use `no_agent=True` (script's stdout is the message verbatim;
   **empty stdout = SILENT, no message**) + a tiered emit shape (silence-by-default). Use `deliver='local'`
   for save-only, `deliver='origin'` to bounce to the creating chat, `deliver='discord:<chan>[:<thread>]'`
   to pin a destination. Use `no_agent=False` (LLM-driven) only when output needs reasoning/formatting
   (the two `quant-daily-*-interim` advisor crons).
4. **Timezone: the cron host runs PT** (`America/Los_Angeles`, currently PDT −0700). Cron expressions are
   evaluated in **host-local wall time** (`cron/jobs.py` interprets naive times as system-local). So
   **ET market times convert −3h to PT**: 09:30 ET open → `30 6 …`; 16:00 ET close → `0 13 …`. During
   EDT (Mar–Nov) PT=ET−3; during EST/PST (Nov–Mar) it's still ET−3 because both shift together. **The
   −3h offset is stable year-round**; only the absolute UTC offset moves.
5. **Two gotchas:** (a) **Deploy-sync** — a cron runs the *deployed* `~/.hermes/scripts/<x>` copy, which
   **drifts from the repo `ops/scripts/`**. You must `cp` the script into `~/.hermes/scripts/` (manual; no
   Makefile target) BEFORE `cronjob action='create'`, or the tick errors. (b) **POSIX DOM/DOW OR-bug** —
   `30 6 1-7 1,4,7,10 1` fires when day-of-month-in-1..7 **OR** weekday=Mon (not AND), so the quarterly
   cron self-gates with a runtime `is_first_monday_of_quarter()` check and `return 0` otherwise.

---

## 1. The `cronjob` registration API

### 1.1 What it is (and what it is NOT)

The mechanism is a single agent tool named **`cronjob`**, registered at
`~/.hermes/hermes-agent/tools/cronjob_tools.py:832` (`registry.register(name="cronjob", toolset="cronjob",
schema=CRONJOB_SCHEMA, …, emoji="⏰")`). It is available in interactive CLI, gateway, and messaging-platform
sessions (`check_cronjob_requirements()` gates on `HERMES_INTERACTIVE` / `HERMES_GATEWAY_SESSION` /
`HERMES_EXEC_ASK`). The scheduler is **internal** — a JSON-file-backed scheduler (`~/.hermes/cron/jobs.json`)
ticked by the gateway loop in `cron/scheduler.py`. From the docstring of `check_cronjob_requirements()`:
*"The cron system is internal (JSON file-based scheduler ticked by the gateway), so no external crontab
executable is required."*

This means:

- **NOT `cron(8)` / crontab** — there is no `/etc/cron.d` entry, no `crontab -e`. The gateway process is the
  scheduler.
- **NOT the Claude-session `CronCreate`** tool (the harness one). That schedules Claude sessions, not Hermes
  jobs, and writes to a different store.
- **The only registry is `~/.hermes/cron/jobs.json`** (`{"jobs": [...], "updated_at": ...}`), and the only
  supported writer is the `cronjob` tool (or its Python entrypoints `cron.jobs.create_job` /
  `update_job` / `remove_job`). Per AGENTS.md plugin constraints, hermes-quant must **never** hand-edit
  Hermes core SQLite; for crons the equivalent rule is: drive them through `cronjob`, do not hand-edit
  `jobs.json` unless setting a field the tool can't (see §6, the `env` gotcha).

> **CLI surface caveat.** From `references/production-readiness-verification.md:79`: the interactive CLI
> verb is **`hermes cron`** (e.g. `hermes cron list`), NOT `hermes cronjob` — *"the `cronjob` form errors
> with `invalid choice`."* The **agent-tool / MCP** name is `cronjob`; the **CLI subcommand** is `cron`.
> Don't conflate them.

### 1.2 Exact `action='create'` field list

From `CRONJOB_SCHEMA` (`cronjob_tools.py:683-804`) and the `cronjob()` create branch (`:448-523`). Every
field is a snake_case kwarg on the tool call:

| Field | Type | Required for create? | Meaning / ground-truth notes |
|---|---|---|---|
| `action` | str | **YES** (`'create'`) | One of create/list/update/pause/resume/remove/run. |
| `schedule` | str | **YES** | Cron expr (`'30 6 * * 1-5'`), interval (`'30m'`, `'every 2h'`), or ISO ts (one-shot). Parsed by `parse_schedule()`. Evaluated in **host-local TZ** (§4). |
| `name` | str | no (recommended) | Human-friendly name; also usable as a `job_id` ref later (`resolve_job_ref` matches id OR name). All quant jobs set this (`quant-…`). |
| `prompt` | str | **YES unless `no_agent=True`** | Self-contained LLM instruction. With `no_agent=True` it is *ignored*. Scanned for injection/exfil by `_scan_cron_prompt()` at create time. |
| `script` | str | **YES when `no_agent=True`** | Path **relative to `~/.hermes/scripts/`** (absolute/`~`/`C:` and `..` traversal are rejected by `_validate_cron_script_path()`). `.sh`/`.bash` → run via bash; everything else → run via the venv Python (`sys.executable`). |
| `no_agent` | bool | no (default `False`) | `True` = skip the LLM; scheduler runs `script` and delivers stdout verbatim. See §3. |
| `deliver` | str | no | `origin` / `local` / `all` / `platform:chat_id[:thread_id]` / comma-combos. Omitting = auto-deliver to creating chat. See §2. |
| `enabled_toolsets` | list[str] | no | Restrict the LLM job's tools (e.g. `["terminal","file"]`). Reduces token overhead. Ignored when `no_agent=True`. |
| `repeat` | int | no | Repeat count. Omit → forever (recurring) or once (one-shot ISO). |
| `skills` | list[str] | no | Ordered skill names to load before the prompt (LLM mode). |
| `model` | obj `{provider?, model}` | no | Per-job model override (LLM mode). Provider pinned at create if omitted. |
| `provider` / `base_url` | str | no | Lower-level model overrides. |
| `context_from` | list[str] | no | Upstream job_ids whose last output is injected as context (job chaining). Validated to exist at create. |
| `workdir` | str (abs path) | no | Run from a project dir (injects AGENTS.md/CLAUDE.md). Jobs with `workdir` run **sequentially**. |
| `profile` | str | no | Run under a named Hermes profile (loads that profile's config/.env). Jobs with `profile` run **sequentially**. |

**Validation order in the create branch** (`cronjob_tools.py:448`): (1) `schedule` present; (2) if
`no_agent` → `script` present, else `prompt`-or-`skills` present; (3) `prompt` injection scan; (4)
`script` path validated within `~/.hermes/scripts/`; (5) `context_from` ids exist; (6) `create_job(...)`.
Returns JSON: `{"success": true, "job_id": "...", "name": ..., "schedule": ..., "next_run_at": ..., "job": {...}}`.

### 1.3 What is NOT a create parameter (common mistakes)

- **No `timeout`.** Script timeout is resolved globally by `_get_script_timeout()`
  (`scheduler.py:818`): precedence is module-patched `_SCRIPT_TIMEOUT` → env
  `HERMES_CRON_SCRIPT_TIMEOUT` → config `cron.script_timeout_seconds` → `_DEFAULT_SCRIPT_TIMEOUT`. To give a
  slow quant cron more budget you set the **config / env**, not a per-job field. (This is exactly the
  `quant-watchlist-evolve-daily` 120s-timeout incident — the fix was a timeout bump + perf fix, not a
  per-job timeout param.)
- **No `env`.** The `cronjob` tool has no env param (`references/firing-layers-and-autonomy.md:56`:
  *"the `cronjob` tool doesn't take an `env` param directly"*). To inject env vars you either (a) bake them
  into a **wrapper `.sh`** (the chosen pattern, §3.2) or (b) for LLM jobs, prefix them in the `prompt`'s
  shell command (`HERMES_QUANT_AUTONOMY=paper … python3 …`).
- **No `enabled` flag on create.** New jobs are scheduled+enabled. Use `action='pause'`/`'resume'` to toggle.

### 1.4 `action='list'`, `'remove'`, and the rest

```text
cronjob action='list'                       # all jobs (add include_disabled=true to include paused)
cronjob action='list' include_disabled=true
cronjob action='remove' job_id='<id-or-name>'   # ALWAYS list first to get the id; never guess
cronjob action='pause'  job_id='<id>' reason='...'
cronjob action='resume' job_id='<id>'
cronjob action='run'    job_id='<id>'            # fire once now (aliases: run_now, trigger)
cronjob action='update' job_id='<id>' <field>=<new>   # any create field; skills=[] / script='' clears
```

`job_id` accepts the 12-hex id (`a6c52aeafc76`) **or** the name (`quant-playbook-tick-daily`);
`resolve_job_ref()` does the lookup and raises `AmbiguousJobReference` (returned as JSON with `matches`) if a
name is non-unique. **The tool's own guidance:** *"Never guess job IDs — always list first."*

CLI equivalent for inspection: `hermes cron list` (see the §1.1 caveat).

---

## 2. Deliver modes (origin vs local vs home-channel) + no_agent vs agent

### 2.1 `deliver` values (from the schema, `cronjob_tools.py:729-731`)

| Value | Behavior |
|---|---|
| *(omitted)* | Auto-deliver back to the **creating chat + topic** (preserves thread). Recommended default for interactive creation. |
| `origin` | Same as omitting — deliver to the job's captured origin (`_origin_from_env()` snapshots `HERMES_SESSION_PLATFORM/CHAT_ID/THREAD_ID` at create time). |
| `local` | **No delivery** — run + persist output only, no message anywhere. Used by save-only quant crons (`quant-playbook-tick-daily`, `quant-playbook-weekly`, `quant-catalyst-ingest-30min`). |
| `all` | Fan out to **every connected home channel**. Resolved at fire time (a job created before a channel is wired picks it up later). |
| `platform:chat_id[:thread_id]` | Pin a specific destination, e.g. `discord:1508194266306969611` or `discord:1508194266306969611:1509261038879637524`. **WARNING (schema):** `platform:chat_id` *without* `:thread_id` loses topic targeting. |
| combos | Comma-join, e.g. `origin,all`. Lists are flattened to a comma-string by `_normalize_deliver_param()`. |

**Live quant deliver usage** (from `~/.hermes/cron/jobs.json`): most quant crons deliver to the
`#hermes-quant` Discord channel `discord:1508194266306969611`; the halts watchdog pins a thread
(`discord:1508194266306969611:1509261038879637524`); save-only crons use `local`; the catalyst-coverage
probe uses `origin`.

### 2.2 `no_agent=True` (script-only) vs `no_agent=False` (LLM-driven)

From the schema's `no_agent` description (`cronjob_tools.py:757-773`) and the create-branch requirements:

**`no_agent=True` — the watchdog pattern (what the trading crons use):**
- The scheduler runs `script` on schedule and delivers its **stdout verbatim**. **No tokens, no agent loop,
  no model override.** `prompt`/`skills`/`enabled_toolsets` are ignored.
- **`script` MUST be set** (create errors otherwise: *"create with no_agent=True requires a script — the
  script is the job."*).
- **Delivery semantics (schema, verbatim):**
  - (a) non-empty stdout → sent verbatim as the message;
  - (b) **EMPTY stdout → SILENT** — nothing is sent and the user sees nothing happened. *Design the script
    to stay quiet when there's nothing to report* (this is the silence-by-default contract);
  - (c) non-zero exit / timeout → an **error alert** is delivered (a broken watchdog can't fail silently).
- **When to use:** recurring script-only pings whose script produces the exact message text (watchdogs,
  threshold alerts, heartbeats, pollers with a fixed output shape).

**`no_agent=False` (default) — LLM-driven:**
- The agent runs the `prompt` each tick (loading `skills`, restricted to `enabled_toolsets`), and the LLM's
  final response is auto-delivered. Use only when output needs reasoning/formatting/conditional logic
  (e.g. "run the brief script, format for Discord, add 1-line market context").
- Only **2 of 16** quant crons are LLM-driven: `quant-daily-premarket-interim` and `quant-daily-eod-interim`
  (and `quant-daily-midday-interim`) — they wrap `quant-daily-interim.py` and add a market-context line.

### 2.3 The trading-watchdog emit contract (silence-by-default)

`no_agent=True` + empty-stdout-is-silent is the kernel of the tiered emit shape codified in
`SKILL.md:10-35` ("Cron output formatting standard, 2026-05-28+"). Every `no_agent` quant script answers
"did anything happen, good or bad?" in line 1:

- **Tier 1 — hard fail (loud):** halt_aborted / errors>0 → lead with 🚨/⚠️.
- **Tier 2 — actionable success (loud-positive):** a fire happened → lead with 📈, name symbol+side+play.
- **Tier 3 — actionable-but-silent (terse):** signals processed, nothing fired → single 🔕 line.
- **Tier 4 — silent heartbeat:** nothing to say → **`print("")` / empty stdout → no Discord message at all.**

Mode tag: `📦 paper` or `🧪 dry-run`. **Wrapper anti-pattern:** don't let the wrapper pass `--json` — it
bypasses the human formatter and dumps raw JSON to Discord (the 2026-05-28 `quant-autonomous-tick-armed.sh`
incident). The exit-code convention (`cron-resilience-patterns.md:30`): `0=ok`, `1=stale-input`,
`2=upstream-broken`, `3=internal-bug` — but note delivery only branches on **non-empty stdout vs empty vs
non-zero-exit**, so a script that wants to stay silent on a soft-abort must `return 2` (error alert) OR
`print("")` + `return 0` (silent) deliberately.

---

## 3. The deploy-sync requirement (script must be in `~/.hermes/scripts/` first)

### 3.1 Why deploy is a prerequisite

`script` paths are **relative and resolved under `~/.hermes/scripts/`** (`_validate_cron_script_path()`
rejects absolute/`~`/traversal; `_run_job_script()` re-validates containment). A cron therefore runs the
**deployed** copy at `~/.hermes/scripts/<x>`, which **drifts from the repo** `ops/scripts/<x>` —
the repo is the source of truth, `~/.hermes/scripts/` is the runtime. (Confirmed empirically: e.g.
`quant-watchlist-evolve.py` exists in both `ops/scripts/` and `scripts/` in the repo AND in
`~/.hermes/scripts/`, and they are *separate files* that must be kept in sync.)

**Consequence:** a `cronjob action='create' … script='quant-foo.py'` will error on the first tick if
`~/.hermes/scripts/quant-foo.py` doesn't exist. **Deploy first, register second.**

### 3.2 How deploy is done (manual `cp` — no Makefile target)

There is **no automated deploy/sync target** (checked: no `Makefile` deploy/rsync rule; `ops/` has no
deploy script). Deploy is a manual copy into the runtime scripts dir:

```bash
# 1. Deploy the script (+ any wrapper) into the runtime dir
cp /mnt/e/CS/github/hermes-quant/ops/scripts/quant-foo.py ~/.hermes/scripts/quant-foo.py
chmod +x ~/.hermes/scripts/quant-foo.py            # optional; scheduler invokes via venv python anyway

# 2. (no_agent only) the script self-reexecs into the hermes venv via the header:
#    _VENV = ~/.hermes/hermes-agent/venv/bin/python3; os.execv(...) — so hermes_quant imports resolve.
#    Verify it runs standalone BEFORE registering:
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-foo.py

# 3. (armed/env crons) deploy the wrapper too, e.g. quant-foo-armed.sh, and point the cron at the wrapper.
```

The venv-reexec header (e.g. `ops/scripts/quant-catalyst-profitability.py`) makes the script run under
`~/.hermes/hermes-agent/venv/bin/python3` so `hermes_quant.*` imports resolve regardless of which Python the
scheduler launched. **The deploy step is what closes the repo↔runtime drift; treat it as part of every cron
change** (`production-readiness-verification.md`: tests verify wiring, running the *deployed* script verifies
data flow — both required).

### 3.3 The wrapper-script pattern for env/flags (autonomy toggle as a filesystem artifact)

Because `cronjob` has no `env` param and `no_agent=True` ignores `prompt`, the way to pass env vars / CLI
flags to a `no_agent` cron is a tiny wrapper `.sh` deployed alongside the script
(`full-day-trading-cadence.md:55-86`):

```bash
# ~/.hermes/scripts/quant-foo-armed.sh
#!/bin/bash
set -euo pipefail
export HERMES_QUANT_AUTONOMOUS=1
export HERMES_QUANT_AUTONOMOUS_ARMED=1
# flock to prevent overlapping long ticks (silent skip if previous still running):
LOCK_FILE="$HOME/.hermes/quant/locks/foo.lock"; exec 9>"$LOCK_FILE"; flock -n 9 || exit 0
exec ~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-foo.py --armed "$@"
```

Then `cronjob action='create' … script='quant-foo-armed.sh' no_agent=true`. Reverting to dry-run is one
field flip: `cronjob action='update' job_id='<id>' script='quant-foo.py'`. Three live examples:
`quant-playbook-tick-armed.sh`, `quant-hourly-tick-armed.sh`, `quant-autonomous-tick-armed.sh`. Rationale:
reversible (one field, not a job-config edit), discoverable (`ls ~/.hermes/scripts/quant-*armed*` shows what's
armed), testable standalone, flock-able, self-documenting.

---

## 4. Timezone — the cron host runs PT; ET market hours → PT cron expressions

### 4.1 Ground truth

- **Host TZ:** `America/Los_Angeles`, currently **PDT (−0700)** (`/etc/timezone`, `timedatectl`).
- **Cron expressions are evaluated in host-local wall time.** `cron/jobs.py` (`_hermes_now()` /
  `_as_aware()`, lines ~299-314) interprets **naive** schedule times as *system-local wall time* and
  normalizes to the configured Hermes timezone. There is no per-job TZ field; the schedule string's
  numbers are **PT clock numbers** on this host.

### 4.2 The ET → PT conversion (this host)

US equity regular session is **09:30–16:00 ET**. PT = ET − 3 hours:

| Market event | ET | PT (cron `min hr`) | Used by |
|---|---|---|---|
| Pre-market brief (1h before open) | 08:30 ET | `30 5` | `quant-daily-premarket-interim` (`30 5 * * 1-5`) |
| Playbook daily tick (at-open) | 09:00 ET | `0 6` | `quant-playbook-tick-daily` (`0 6 * * 1-5`) |
| **Market open** | **09:30 ET** | **`30 6`** | autonomous-tick start (`30 6-13 * * 1-5`) |
| Hourly ticks (10:00–16:00 ET) | 10:00–16:00 ET | `0 7-13` | `quant-hourly-market-tick` (`0 7-13 * * 1-5`) |
| Midday brief | 11:00 ET | `0 8` | `quant-daily-midday-interim` (`0 8 * * 1-5`) |
| EOD brief | 15:30 ET | `30 12` | `quant-daily-eod-interim` (`30 12 * * 1-5`) |
| **Market close** | **16:00 ET** | **`0 13`** | hourly final tick / portfolio EOD (`5 13 …`) |

**Mnemonic: subtract 3 from the ET hour for the PT cron hour.** `09:30 ET = 06:30 PT`.

### 4.3 DST stability

US equity hours follow **ET**, and ET and PT observe DST in lockstep (both spring forward / fall back on the
same dates). So **the ET−PT offset is a constant 3 hours year-round** — the PT cron expressions above stay
correct across the DST boundary even though the absolute UTC offset of the host shifts (PDT −0700 ⇄ PST
−0800). The only thing that changes is the UTC timestamp a given PT cron line maps to (this is why
`signal-coverage-and-fill-dedup-forensics.md` maps PT schedules → UTC fill times for forensics, not for
scheduling). **Do not "correct" the PT expressions twice a year.**

> Caveat to watch: this invariance holds only while the host TZ stays `America/Los_Angeles`. If the host is
> ever moved to a fixed-offset TZ (e.g. UTC) the expressions would need rewriting; verify host TZ as part of
> any cron audit (`date +%Z`).

---

## 5. The POSIX DOM/DOW OR-bug (quarterly cron self-gating)

### 5.1 The bug

POSIX cron semantics: **when BOTH day-of-month (field 3) and day-of-week (field 5) are restricted
(non-`*`), they are ORed, not ANDed.** So the "first Monday of the quarter" expression

```
30 6 1-7 1,4,7,10 1     # 06:30, dom∈1..7, months Jan/Apr/Jul/Oct, dow=Mon
```

does **NOT** mean "06:30 on the 1st-through-7th *and* it's a Monday." It fires on **every day 1–7 of those
months OR every Monday of those months** — i.e. roughly 4 extra spurious fires per quarter month plus the
Mondays. (Documented in the script header `quant-playbook-quarterly.py:6-10` and `ADR-0035:141-142`.)

### 5.2 The fix: self-gating runtime check (`return 0` when not actually due)

The cron expression stays as the standard idiom (it's the *closest* cron can express), and the script guards
itself at runtime. `~/.hermes/scripts/quant-playbook-quarterly.py:146-162`:

```python
def is_first_monday_of_quarter(now: datetime | None = None) -> bool:
    """Defensive: traditional cron treats DOM and DOW as OR when both are
    restricted (POSIX), so '30 6 1-7 1,4,7,10 1' may fire on every Monday
    of those months OR every day in 1-7. We guard inside the script."""
    now = now or datetime.now(UTC).astimezone(ET)
    if now.month not in (1, 4, 7, 10):
        return False
    if now.weekday() != 0:      # 0 = Monday
        return False
    if now.day > 7:             # only the first week
        return False
    return True
```

On a spurious tick the script computes `is_first_monday_of_quarter() == False` and exits early (`return 0`
with empty/short stdout → silent under `no_agent=True`). A `--force` flag bypasses the guard for testing
(`quant-playbook-quarterly.py:610`). **Generalization:** any cron whose schedule needs an AND of DOM∧DOW
(or any predicate cron can't express) must re-check the real predicate at runtime and no-op silently on a
spurious tick. This is the cron analogue of the idempotency-journal pattern — cron *over-fires*, the script
*decides*.

---

## 6. Edge case: setting `env` on an existing `no_agent` job (the one hand-edit exception)

`references/firing-layers-and-autonomy.md:56` documents the one case where `jobs.json` gets touched directly:
the `cronjob` tool has no `env` param, and a `no_agent` job's stored `env` block can't be set via the tool.
The two supported routes are: (1) **switch to a wrapper `.sh` that exports the env** (preferred — §3.3), or
(2) **edit `~/.hermes/cron/jobs.json` and restart the scheduler** (last resort). Route (2) is the *only*
sanctioned direct edit of the cron registry, and even then the operator runbook prefers route (1) because it's
reversible and discoverable. For LLM jobs (`no_agent=False`) there's a third route: prefix the env var in the
`prompt`'s shell command via `cronjob action='update' job_id='<id>' prompt='…HERMES_QUANT_AUTONOMY=paper …'`
(how the advisor crons were armed).

---

## 7. The live quant cron registry (16 jobs, snapshot 2026-05-30)

From `~/.hermes/cron/jobs.json` (id · name · expr · no_agent · deliver · script):

| id | name | expr (PT) | no_agent | deliver | script |
|---|---|---|---|---|---|
| `b1e07e73dc41` | quant-universe-scan-daily | `15 3 * * 1-5` | True | discord:…969611 | quant-universe-scan.py |
| `82d3aa40024d` | quant-watchlist-evolve-daily | `30 3 * * 1-5` | True | discord:…969611 | quant-watchlist-evolve.py |
| `67c85edf65ee` | quant-catalyst-coverage-daily | `45 3 * * 1-5` | True | origin | quant-catalyst-coverage.py |
| `ca4f5c3b344a` | quant-halts-watchdog-daily | `0 5 * * *` | True | discord:…969611:…637524 | quant-halts-watchdog.py |
| `13b66e53eaa4` | quant-daily-premarket-interim | `30 5 * * 1-5` | **False** | discord:…969611 | *(LLM; runs quant-daily-interim.py in prompt)* |
| `a6c52aeafc76` | quant-playbook-tick-daily | `0 6 * * 1-5` | True | local | quant-playbook-tick-armed.sh |
| `630c90c20e78` | quant-catalyst-ingest-30min | `0,30 6-13 * * 1-5` | True | local | quant-catalyst-ingest.py |
| `33f250858429` | quant-proposals-ttl-watchdog-daily | `30 6 * * 1-5` | True | discord:…969611 | quant-proposals-ttl-watchdog.py |
| `291d25b942a9` | quant-playbook-weekly | `30 6 * * 1` | True | local | quant-playbook-weekly.py |
| `1bcf03c073bf` | quant-playbook-quarterly | `30 6 1-7 1,4,7,10 1` | True | discord:…969611 | quant-playbook-quarterly.py |
| `40885b88bbfb` | quant-autonomous-tick-30min | `30 6-13 * * 1-5` | True | discord:…969611 | quant-autonomous-tick-armed.sh |
| `b487b97ad4d2` | quant-hourly-market-tick | `0 7-13 * * 1-5` | True | discord:…969611 | quant-hourly-tick-armed.sh |
| `c64f4d386cc5` | quant-daily-midday-interim | `0 8 * * 1-5` | **False** | discord:…969611 | *(LLM; runs quant-daily-interim.py)* |
| `1f907df49370` | quant-daily-eod-interim | `30 12 * * 1-5` | **False** | discord:…969611 | *(LLM; quant-daily-interim.py --eod)* |
| `a02d6225bd58` | quant-portfolio-daily-eod | `5 13 * * 1-5` | True | discord:…969611 | quant-portfolio-daily.py |
| `e52c189b6582` | quant-strategy-retro-weekly | `0 13 * * 0` | True | discord:…969611 | quant-strategy-retro-weekly.py |

(`discord:…969611` = `discord:1508194266306969611`, the `#hermes-quant` channel. 14 `no_agent` watchdogs +
3 `no_agent=False` LLM advisor briefs — total 17 rows incl. midday; live `quant-*` count = 16 because midday
was the 16th add.)

---

## 8. Canonical registration commands (copy-paste templates)

**A `no_agent` watchdog (the trading-cron default):**

```text
# 0. DEPLOY FIRST (repo → runtime):
cp /mnt/e/CS/github/hermes-quant/ops/scripts/quant-foo.py ~/.hermes/scripts/quant-foo.py
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-foo.py   # verify standalone

# 1. REGISTER:
cronjob action='create'
        name='quant-foo-daily'
        schedule='30 6 * * 1-5'          # 09:30 ET = 06:30 PT, weekdays
        script='quant-foo.py'             # relative to ~/.hermes/scripts/
        no_agent=true
        deliver='discord:1508194266306969611'   # #hermes-quant; or 'local' / 'origin'
        enabled_toolsets=['terminal','file']     # (ignored under no_agent, harmless)
```

**An armed/env watchdog (via wrapper):** deploy `quant-foo.py` + `quant-foo-armed.sh`, then
`script='quant-foo-armed.sh'`. Disarm with `cronjob action='update' job_id='<id>' script='quant-foo.py'`.

**An LLM-driven brief:**

```text
cronjob action='create'
        name='quant-bar-brief'
        schedule='30 5 * * 1-5'
        no_agent=false
        deliver='discord:1508194266306969611'
        enabled_toolsets=['terminal','file']
        prompt='Run: HERMES_QUANT_AUTONOMY=paper ~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-bar.py — then format the markdown brief for Discord and add a 1-line market-context note. The script output IS the message; minimal additions.'
```

**Inspect / tear down:** `cronjob action='list'` → grab id → `cronjob action='remove' job_id='<id>'`.

---

## 9. Sources

- `~/.hermes/hermes-agent/tools/cronjob_tools.py` (tool def, `CRONJOB_SCHEMA`, create/update/list branches,
  `_validate_cron_script_path`, `_normalize_deliver_param`, `_origin_from_env`, `check_cronjob_requirements`).
- `~/.hermes/hermes-agent/cron/scheduler.py` (`_get_script_timeout`, `_run_job_script`, interpreter-by-extension,
  empty-stdout/non-zero-exit delivery branch).
- `~/.hermes/hermes-agent/cron/jobs.py` (`_hermes_now`/`_as_aware` — host-local TZ interpretation).
- `~/.hermes/cron/jobs.json` (live 33-job registry; 16 `quant-*`).
- `~/.hermes/scripts/quant-playbook-quarterly.py:1-30,146-162,606-635` (DOM/DOW guard).
- `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md:10-35` (tiered emit / silence-by-default).
- `references/firing-layers-and-autonomy.md` (no `env` param; arming via wrapper/prompt; job_ids).
- `references/full-day-trading-cadence.md` (wrapper pattern, PT cadence table, "wired-but-unscheduled" probe).
- `references/cron-resilience-patterns.md` (exit codes 0/1/2/3; watchdog patterns).
- `references/production-readiness-verification.md:79` (`hermes cron` CLI verb, NOT `hermes cronjob`).
- `references/system-health-audit-recipe.md:101-109` (silent-fail scan via `cronjob action='list'`).
- `docs/adr/ADR-0035-playbook-cadence-daily-weekly-quarterly.md:139-142` (six no_agent crons; DOM/DOW note).
- Host: `/etc/timezone` = `America/Los_Angeles`; `timedatectl` = PDT −0700.
