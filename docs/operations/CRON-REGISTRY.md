# Cron Registry & Registration Runbook — hermes-quant trading crons

- **Status:** canonical. This is the single source of truth for every trading-system cron.
- **Last reconciled:** 2026-05-30 (against live `~/.hermes/cron/jobs.json`, 33 jobs total, 16 `quant-*`).
- **Grounded in:** `docs/research/2026-05-30-r-hermes-cronjob-mechanism.md` (the `cronjob` API) and
  `docs/research/2026-05-30-r-cron-and-flag-inventory.md` (the registry/flag reconciliation).

> **READ THIS FIRST — who runs these commands.**
> **This document is a runbook for the operator / the Hermes `cronjob` agent.** The agent that wrote this
> file (a hermes-quant subagent) **cannot register crons** — it has **no `cronjob` tool** in its
> environment, and crons are NOT registered via `cron(8)`/`crontab` and NOT via the Claude-session
> `CronCreate` tool. Every `cronjob action='...'` block below is a command the **operator or the
> Hermes agent** executes inside a Hermes session (interactive CLI / gateway / Discord). Treat the
> `cronjob ...` blocks as copy-paste templates, not as something this agent runs.

---

## 0. How these crons are registered (the mechanism, in one paragraph)

The trading crons are **JSON-file-backed jobs in `~/.hermes/cron/jobs.json`**, ticked by the gateway's
internal scheduler (`~/.hermes/hermes-agent/cron/scheduler.py`). The **only** supported writer is the
Hermes **`cronjob`** agent/MCP tool (`tools/cronjob_tools.py`, toolset `cronjob`, emoji ⏰),
`action ∈ {create, list, update, pause, resume, remove, run}`. It is **not** `cron(8)`/crontab (there is
no `/etc/cron.d` entry) and **not** the harness `CronCreate` (that schedules Claude sessions, a different
store). The CLI *inspection* verb is `hermes cron list` (note: `hermes cron`, **not** `hermes cronjob` —
the `cronjob` form errors with `invalid choice`); the **agent-tool** name is `cronjob`. Schedule
expressions are evaluated in **host-local wall time = PT** (`America/Los_Angeles`); see §4.

`no_agent=True` ⇒ deterministic script, stdout delivered **verbatim**, **empty stdout = SILENT** (no
Discord message). `no_agent=False` ⇒ LLM-driven brief (the prompt carries any env-var prefix). Trading
watchdogs use `no_agent=True` + the silence-by-default emit contract (§2).

---

## 1. The registry — every trading cron

Channel `discord:1508194266306969611` = `#hermes-quant` (abbreviated **#hq** below). The halts watchdog
pins thread `:1509261038879637524`. **PT** is the schedule's literal clock numbers on this host; the
**ET** column is the market time it maps to (PT = ET − 3h, stable year-round — see §4).

| # | Cron name | Schedule (PT) | = ET | Deployed script | Deliver | no_agent | Armed / flags | Purpose (1 line) | Silence contract |
|---|---|---|---|---|---|---|---|---|---|
| 1 | quant-universe-scan-daily | `15 3 * * 1-5` | 06:15 ET, Mon–Fri | `quant-universe-scan.py` | discord:#hq | ✓ | — | Premarket liquidity-universe scan | quiet unless universe delta |
| 2 | quant-watchlist-evolve-daily | `30 3 * * 1-5` | 06:30 ET, Mon–Fri | `quant-watchlist-evolve.py` | discord:#hq | ✓ | — | Evolve play-fit watchlist post-scan | quiet unless watchlist change |
| 3 | quant-catalyst-coverage-daily | `45 3 * * 1-5` | 06:45 ET, Mon–Fri | `quant-catalyst-coverage.py` | origin | ✓ | — | Change-detecting catalyst-coverage watchdog | empty unless coverage drops |
| 4 | quant-halts-watchdog-daily | `0 5 * * *` | 08:00 ET, daily | `quant-halts-watchdog.py` | discord:#hq:thread | ✓ | — | Warn on stale operator halts (>7d) | silent if no stale halt |
| 5 | quant-daily-premarket-interim | `30 5 * * 1-5` | 08:30 ET, Mon–Fri | *(LLM; runs `quant-daily-interim.py`)* | discord:#hq | ✗ (LLM) | `AUTONOMY=paper` (prompt) | Premarket advisor brief, auto-fires | brief always; "🤖 Auto-fired N/M" |
| 6 | quant-playbook-tick-daily | `0 6 * * 1-5` | 09:00 ET, Mon–Fri | `quant-playbook-tick-armed.sh` | local | ✓ | `--armed` (wrapper) | Daily 5-play decision tick | local; silent unless fire |
| 7 | quant-proposals-ttl-watchdog-daily | `30 6 * * 1-5` | 09:30 ET, Mon–Fri | `quant-proposals-ttl-watchdog.py` | discord:#hq | ✓ | — | Expire stale pending proposals | silent if none stale |
| 8 | quant-autonomous-tick-30min | `30 6-13 * * 1-5` | :30 of 09:30–16:30 ET, Mon–Fri | `quant-autonomous-tick-armed.sh` | discord:#hq | ✓ | `AUTONOMOUS`+`ARMED` (wrapper, flock) | 30-min in-market PDR loop | per-fire delta only |
| 9 | quant-catalyst-ingest-30min | `0,30 6-13 * * 1-5` | :00/:30 of 09:00–16:30 ET, Mon–Fri | `quant-catalyst-ingest.py` | local | ✓ | — | Ingest GN-RSS catalyst packets q30min | local; quiet |
| 10 | quant-hourly-market-tick | `0 7-13 * * 1-5` | 10:00–16:00 ET, Mon–Fri | `quant-hourly-tick-armed.sh` | discord:#hq | ✓ | `AUTONOMOUS`+`ARMED` (wrapper) | Hourly monitor + phase-7 propose/fire | silent unless fire/halt/err |
| 11 | quant-daily-midday-interim | `0 8 * * 1-5` | 11:00 ET, Mon–Fri | *(LLM; runs `quant-daily-interim.py`)* | discord:#hq | ✗ (LLM) | `AUTONOMY=paper` (prompt) | Midday advisor brief, auto-fires | brief always |
| 12 | quant-playbook-weekly | `30 6 * * 1` | 09:30 ET, Mon | `quant-playbook-weekly.py` | local | ✓ | (`script:` plain — not yet armed) | Mon rebalance: roll/expire near-DTE legs | local; silent unless action |
| 13 | quant-daily-eod-interim | `30 12 * * 1-5` | 15:30 ET, Mon–Fri | *(LLM; runs `quant-daily-interim.py --eod`)* | discord:#hq | ✗ (LLM) | `AUTONOMY=paper` (prompt) | EOD advisor brief, auto-fires | brief always |
| 14 | quant-portfolio-daily-eod | `5 13 * * 1-5` | 16:05 ET, Mon–Fri | `quant-portfolio-daily.py` | discord:#hq | ✓ | — | EOD portfolio snapshot (compact + MEDIA md) | silent if book empty |
| 15 | quant-strategy-retro-weekly | `0 13 * * 0` | 16:00 ET, Sun | `quant-strategy-retro-weekly.py` | discord:#hq | ✓ | — | Sun weekly trailing-7d retro | silent if 0 fills + 0 open |
| 16 | quant-playbook-quarterly | `30 6 1-7 1,4,7,10 1` | 09:30 ET, ~1st Mon of quarter | `quant-playbook-quarterly.py` | discord:#hq | ✓ | — (self-gated, §5) | 1st-Mon-of-quarter factor/rebalance review | quiet unless flag |
| **17** | **catalyst-profitability-weekly** *(NEW — not yet registered)* | `0 7 * * 1` | 10:00 ET, Mon | `quant-catalyst-profitability.py` | local | ✓ | — | Weekly: join propagation-log vs fwd returns; per-relation profitability verdict | silent until a relation crosses `MIN_SAMPLE=20` or a cleared class flips verdict |
| **18** | **calibrator-drift-weekly** *(NEW — not yet registered)* | `0 7 * * 1` | 10:00 ET, Mon | `quant-calibrator-drift.py` | local | ✓ | — (`CALIBRATOR_AUTO_REFIT` default-OFF) | Weekly raw→calibrated drift check; alert if drift > 5% | exits 0 silent unless `should_alert` (drift > threshold) |
| **19** | **quant-weekly-retro** *(NEW — not yet registered; W2 / ADR-0081)* | `30 13 * * 0` | 16:30 ET, Sun | `quant-weekly-retro.py` | local | ✓ | `HERMES_QUANT_WEEKLY_RETRO` **default-OFF** (flag-OFF = silent no-op) | Weekly CVRF pattern-mining retro: distill winners-vs-losers (by realized alpha) into bounded/decaying beliefs; emits `weekly_retro_promotion_readiness` (closes O3) | exits 0 silent unless a belief is distilled/expired, budget-cap flips, or readiness toggles (flag-OFF = empty stdout) |
| **20** | **quant-monthly-meta-retro** *(NEW — not yet registered; W3 / ADR-0080 / ADR-0081 §3)* | `0 14 1 * *` | 09:00 ET, 1st of month (after the trailing weekly retros) | `quant-monthly-meta-retro.py` | local | ✓ | `HERMES_QUANT_MONTHLY_META_RETRO` **default-OFF** (flag-OFF = byte-identical no-op) | Monthly meta-retro (T3): aggregate W2 weekly belief digests + `research_debate` audit rows (O7) + promotion records → repeating-lesson trends, persona-calibration **telemetry** (NOT applied), and novelty/dedup-gated **candidate** hypotheses registered `status="open"` (closes O8); applies the deterministic weekly→monthly belief promote/expire. PROPOSE-ONLY; zero auto-promotion. | exits 0 silent unless a candidate is proposed or a belief is promoted/expired (flag-OFF = empty stdout) |
| **21** | **research-loop-weekly** *(NEW — not yet registered; W6 / ADR-0080 §D80.6)* | `0 8 * * 1` | 11:00 ET, Mon (after the W3 meta-retro has seeded candidates) | `quant-research-loop.py` | discord:#hq | ✓ | `HERMES_QUANT_RESEARCH_LOOP` **default-OFF** (flag-OFF = byte-identical no-op) | Weekly W6 driving cron: drain W3 `open` candidate hypotheses → deterministic OOS backtest + lookahead sentinel → (clean+validated only) PromotionGate. PRODUCES reproducible Run-Cards + review-only PromotionRecords. **PROPOSER ONLY — zero auto-promotion to live; the operator promotes (ADR-0052).** | exits 0 silent unless a candidate ran / promotion recommended / contamination fired / error / halt aborted (flag-OFF = empty stdout) |

**Counts.** 16 quant crons are **live in `jobs.json`** (rows 1–16, all `enabled`). 14 of those are
`no_agent=True` watchdogs; 3 are `no_agent=False` LLM advisor briefs (#5, #11, #13). Rows **17–18 are
built and the repo scripts exist, but they are NOT yet deployed and NOT yet registered** — registering
them is the pending action in §2/§3.

> **ADR-0052 promotion crons** (`promotion-cron.py`) are operator/CI-run and are **not** in the host
> `jobs.json`; they are out of scope for this trading-cron registry.

---

## 2. Registering the 2 NEW crons (exact `cronjob action='create'` commands)

These are the commands the **operator / Hermes agent** runs (this agent cannot). **Deploy the scripts
first (§3) — `create` will error on the first tick if `~/.hermes/scripts/<script>` does not exist.**

Both new crons are pure `no_agent=True` watchdogs with `deliver='local'` (save-only, no Discord noise;
they only need to surface a line when a verdict/drift transition happens — and even then `local` keeps
it out of chat until you promote `deliver` to `discord:1508194266306969611`). Neither needs a wrapper:
they take no risk-changing env (catalyst-profitability is read-only analysis; calibrator-drift's only
flag `HERMES_QUANT_CALIBRATOR_AUTO_REFIT` is **default-OFF**, and left off the cron is alert-only and
never touches the live pickle — see `docs/research/2026-05-30-r-cron-and-flag-inventory.md` §2.2,
"SAFE-NOW").

### 2.1 catalyst-profitability-weekly

```text
cronjob action='create'
        name='catalyst-profitability-weekly'
        schedule='0 7 * * 1'                       # 07:00 PT Mon = 10:00 ET Mon
        script='quant-catalyst-profitability.py'    # relative to ~/.hermes/scripts/
        no_agent=true
        deliver='local'
```

- **Why these fields:** `no_agent=true` ⇒ `script` is required and the stdout is the message; `prompt` /
  `enabled_toolsets` would be ignored, so they are omitted. `deliver='local'` = persist only, no chat
  message (promote to `discord:1508194266306969611` only after you want the weekly verdict in #hq).
- **Silence contract:** the script prints **nothing** (empty stdout → SILENT) on a standing-state week.
  It emits a line only when a relation class first crosses `MIN_SAMPLE=20` (`… CLEARED MIN_SAMPLE …`) or
  a cleared class flips verdict — diffed against `~/.hermes/quant/catalyst/profitability-baseline.json`.
  `--verbose` forces the full table for an on-demand operator pull (not used by the cron).

### 2.2 calibrator-drift-weekly

```text
cronjob action='create'
        name='calibrator-drift-weekly'
        schedule='0 7 * * 1'                  # 07:00 PT Mon = 10:00 ET Mon
        script='quant-calibrator-drift.py'     # relative to ~/.hermes/scripts/
        no_agent=true
        deliver='local'
```

- **Why these fields:** same `no_agent=true` + `deliver='local'` rationale. Auto-refit stays OFF: do
  **not** add any env here — the `cronjob` tool has **no `env` param**, and the script reads
  `HERMES_QUANT_CALIBRATOR_AUTO_REFIT` from the process env (default-OFF = alert-only). If you ever arm
  auto-refit, do it via a wrapper `.sh` (§3.3), not by hand-editing `jobs.json`.
- **Silence contract:** `run_drift_check` returns `should_alert = drift > threshold` (threshold default
  **0.05 = 5%**); any exception or zero samples logs to stderr and **exits 0 silently**. The cron only
  surfaces text when drift exceeds 5% (or a refit is recommended); a quiet, in-tolerance week prints the
  drift-result block to stdout, so if you want true chat silence keep `deliver='local'` (the block is
  saved, not sent). Drift rows append to `~/.hermes/quant/calibrators/drift-log.jsonl`.

> **Schedule note (important).** The `quant-calibrator-drift.py` docstring (line 9) says
> *"Monday 07:00 **UTC**"* — that author comment is **superseded** by the host scheduler, which
> evaluates `0 7 * * 1` in **PT** (§4). So the real fire time is **07:00 PT = 10:00 ET Mon**, not 07:00
> UTC. Register with `0 7 * * 1` as above; do not "correct" for UTC. (See open question Q1.)

---

## 3. Deploy-sync — the script must land in `~/.hermes/scripts/` first

### 3.1 Why deploy is a prerequisite

`script` is resolved **relative to `~/.hermes/scripts/`** (absolute / `~` / `..` traversal are rejected
by `_validate_cron_script_path()`). A cron therefore runs the **deployed** copy, which **drifts from the
repo `ops/scripts/`** — the repo is source of truth, `~/.hermes/scripts/` is the runtime. There is **no
Makefile/rsync deploy target**; deploy is a manual `cp`. **As verified 2026-05-30, neither new script is
deployed yet** (`~/.hermes/scripts/` has `quant-bootstrap-calibrator.py` but not the two new files), so
this step is mandatory before §2.

### 3.2 Deploy commands (run on the Hermes host)

```bash
# 1. Deploy both new scripts repo -> runtime:
cp /mnt/e/CS/github/hermes-quant/ops/scripts/quant-catalyst-profitability.py ~/.hermes/scripts/quant-catalyst-profitability.py
cp /mnt/e/CS/github/hermes-quant/ops/scripts/quant-calibrator-drift.py        ~/.hermes/scripts/quant-calibrator-drift.py

# 2. Verify each runs standalone under the hermes venv BEFORE registering.
#    (Both scripts self-reexec into the venv via their header, so a plain python3 works too,
#    but invoking the venv python directly is the deterministic check.)
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-catalyst-profitability.py
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-calibrator-drift.py
#   expected: exit 0; quiet (no transition / no drift) on a clean run, or a single verdict/drift block.
```

Both scripts already carry the venv re-exec header
(`_VENV = ~/.hermes/hermes-agent/venv/bin/python3; os.execv(...)`), so `hermes_quant.*` imports resolve
regardless of which Python the scheduler launches. `chmod +x` is optional — the scheduler runs `.py`
files via the venv Python by extension, not by the exec bit. **Deploy first, register second** — treat
the `cp` as part of every cron change.

### 3.3 Wrapper pattern (only if you later need env/flags)

Neither new cron needs this today. For reference: because `cronjob` has no `env` param and `no_agent=True`
ignores `prompt`, the way to inject env/flags into a `no_agent` cron is a tiny wrapper `.sh` deployed
alongside the script, e.g. to arm calibrator auto-refit:

```bash
# ~/.hermes/scripts/quant-calibrator-drift-refit.sh
#!/bin/bash
set -euo pipefail
export HERMES_QUANT_CALIBRATOR_AUTO_REFIT=1
exec ~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/scripts/quant-calibrator-drift.py "$@"
```

…then `cronjob action='update' job_id='calibrator-drift-weekly' script='quant-calibrator-drift-refit.sh'`.
Revert with `script='quant-calibrator-drift.py'`. (This is the reversible, discoverable arming pattern
used by `quant-*-armed.sh`. Do **not** hand-edit `jobs.json` to set env unless absolutely required.)

---

## 4. Timezone rule (PT) + the POSIX DOM/DOW self-gating note

### 4.1 PT/ET rule

- The cron host runs **`America/Los_Angeles` (PT)**; schedule expressions are evaluated in **host-local
  wall time**. There is **no per-job TZ field** — the numbers in the schedule string are **PT clock
  numbers**.
- US equity regular session is **09:30–16:00 ET**. **PT = ET − 3h.** Mnemonic: **subtract 3 from the ET
  hour to get the PT cron hour** (09:30 ET → `30 6`; 16:00 ET → `0 13`).
- **DST is stable.** ET and PT shift in lockstep, so the **ET−PT offset is a constant 3h year-round**.
  The PT expressions stay correct across the DST boundary — **do NOT "correct" them twice a year.** Only
  the absolute UTC offset of the host moves (PDT −0700 ⇄ PST −0800). This is why both new crons register
  with `0 7 * * 1` (PT) and the calibrator-drift docstring's "07:00 UTC" comment is moot (§2.2).
- *Caveat:* this invariance holds only while the host TZ is `America/Los_Angeles`. Verify host TZ
  (`date +%Z`) as part of any cron audit; a move to a fixed-offset TZ would require rewriting the
  expressions.

### 4.2 POSIX DOM/DOW OR-bug → self-gating

POSIX cron **ORs** day-of-month (field 3) and day-of-week (field 5) when **both** are restricted
(non-`*`). So `quant-playbook-quarterly`'s `30 6 1-7 1,4,7,10 1` does **not** mean "1st-through-7th **AND**
Monday" — it fires on **every day 1–7 OR every Monday** of Jan/Apr/Jul/Oct. The cron expression stays as
the closest idiom cron can express, and the **script self-gates at runtime** with
`is_first_monday_of_quarter()` and exits early (`return 0`, empty stdout → silent under `no_agent`) on a
spurious tick (a `--force` flag bypasses for testing).

**Generalization:** any cron whose real predicate needs an AND of DOM∧DOW (or anything cron can't
express) must re-check the predicate at runtime and no-op silently on spurious ticks. The two new weekly
crons use a plain `0 7 * * 1` (DOW-only, no DOM), so they are **not** subject to the OR-bug and need no
self-gate.

---

## 5. Verification

After deploy (§3) and register (§2), confirm both new jobs are present and correctly shaped. **Operator /
Hermes-agent runs these — this agent has no `cronjob` tool.**

```text
# In a Hermes session (CLI/gateway/Discord):
cronjob action='list'                 # full registry; grep the output for the two new names
# expect to see: catalyst-profitability-weekly  and  calibrator-drift-weekly
#   each: schedule='0 7 * * 1'  no_agent=true  deliver='local'  script=<the .py>
```

```bash
# CLI inspection equivalent on the host (note: `hermes cron`, NOT `hermes cronjob`):
hermes cron list | grep -E 'catalyst-profitability-weekly|calibrator-drift-weekly|quant-'
```

```text
# Optional: fire each once now to confirm end-to-end (list first to get the id/name):
cronjob action='run' job_id='catalyst-profitability-weekly'
cronjob action='run' job_id='calibrator-drift-weekly'
```

Expected post-registration count: **16 live + 2 new = 18 `quant-*` trading crons** managed for the system.
If a name is missing from `cronjob action='list'`, the most likely cause is the
deploy step (§3) was skipped and the first tick errored. **Never guess job IDs — always `list` first**
before `remove`/`pause`/`update`.

---

## 6. Sources

- `docs/research/2026-05-30-r-hermes-cronjob-mechanism.md` — the `cronjob` API, `action='create'` field
  list, deliver modes, `no_agent` semantics, deploy-sync, PT timezone rule, DOM/DOW OR-bug.
- `docs/research/2026-05-30-r-cron-and-flag-inventory.md` — registry reconciliation (16 live + 2 new),
  per-cron silence contracts, `HERMES_QUANT_*` flag SAFE-NOW/GATED split.
- Live `~/.hermes/cron/jobs.json` (33 jobs; 16 `quant-*`, parsed read-only 2026-05-30).
- `ops/scripts/quant-catalyst-profitability.py` (transition-diff watchdog; `MIN_SAMPLE=20`;
  baseline `~/.hermes/quant/catalyst/profitability-baseline.json`).
- `ops/scripts/quant-calibrator-drift.py` (drift threshold 5%; `should_alert`; auto-refit default-OFF;
  drift-log `~/.hermes/quant/calibrators/drift-log.jsonl`).
- `~/.hermes/skills/mlops/hermes-quant-operations/SKILL.md` — tiered emit / silence-by-default contract.
