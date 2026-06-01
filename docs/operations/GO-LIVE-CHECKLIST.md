# GO-LIVE CHECKLIST — hermes-quant Monday market-open runbook

- **Status:** operator runbook for the Monday 2026-06-01 US equity open (09:30 ET / 06:30 PT).
- **Audience:** the human operator (Codeseys) + the Hermes gateway agent. The subagent that wrote
  this **cannot** flip `.env` flags (tool-guarded), register crons (no `cronjob` tool), edit
  `config.yaml` (gateway-owned), or move money (no execution tool exists). Every command below is a
  copy-paste the **operator** runs.
- **Synthesized from (does NOT contradict):** `DEPLOY-SYNC.md`, `CRON-REGISTRY.md`,
  `SELFEVOLVE-ENABLEMENT.md`, `FEATURE-ENABLEMENT.md`, `HERMES-SELF-ONBOARDING.md`, and the three
  Monday-readiness assessments (deploy-drift, live-regression, flag-cron-state) run 2026-05-30/06-01.

> ## THE ONE INVARIANT — read before doing anything
> **Hermes PROPOSES; the deterministic gate + the human DECIDE.** The deterministic risk gate
> (8-rule sequence, halt FIRST — `risk/gate.py:377`), the discrete sizing ladder
> `{0,±0.05,±0.10,±0.15,±0.20}`, the drawdown(0.15)/daily-loss(0.05) circuit breakers, and the
> HITL-gated kill-switch sit **OUTSIDE every flag** and are immutable by the loop. Nothing in this
> runbook flips a degrading flag, fires on a non-event, or enables live broker order placement.
> The reactor is hardwired to `PaperReactor()` and `allow_live=False` (`autonomous.py:666,102`).
> **If unsure, do nothing and surface the proposed command.**

---

## 1. VERDICT

### `GO_WITH_GATES`

**The existing Monday trading loop is GO with NO hard blockers.** All three assessments converge:

- Every one of the **16 live quant crons** (`jobs.json` rows 1–16, all enabled) points at a
  **deployed** script that exists and has been running unchanged — Monday opens on the same code
  that ran last week.
- `~/.hermes/.env` carries exactly the **4 expected SAFE flags** — `SEMANTIC_ENABLED=1`,
  `REFLECTION=1`, `MEMORY_INJECT=1`, `SOCIAL_INGEST=1` — and **zero GATED money-path flags leaked
  on** (ADMISSIBILITY / BORROW_COST / OPTIONS_GATE / OPTIONS_LIVE_CHAIN / MULTILEG_REACTOR /
  STACKING / CATALYST_ONBOARDING all OFF; verified).
- **397/397 targeted live-path tests pass, 0 fail** (gate, kelly, kill-switch, reactor, lookahead,
  autonomous tick, advisor, settlement, catalyst, perception, flag-off byte-identical).
- The deterministic gate, sizing ladder, breakers, and HITL kill-switch are INTACT; reactor is
  paper-only; `paper_zero_costs` fail-closed invariant holds (`autonomous.py:667`).
- Host TZ = **PDT (America/Los_Angeles)**, so all PT cron expressions are correct as-written.
  **Do NOT UTC-correct any schedule.**

**Why `GO_WITH_GATES` and not a bare `GO`:** the optional "improve the system this Monday" actions
(deploy the AXP fix, register the 2 read-only watchdogs) each carry a GATE that must be respected.
None is required to open. If the operator does **nothing**, Monday opens safely on last week's code.

### Blockers (NONE block the open; these gate the *optional* improvements)

| # | Blocker | Blocks | Why |
|---|---|---|---|
| B1 | **Deploy-sync is DIRTY** — `quant-deploy-audit.py` exits 1: `{REPO_ONLY_NEW:16, DRIFT:8, SAME:4, DEPLOYED_ONLY:4}`. | Blocks **registering any owed cron** (§2 Step C/D). Does NOT block the running loop. | A `cronjob create` errors on first tick if the script is absent from `~/.hermes/scripts/`. Owed scripts must be deployed + re-audited first. |
| B2 | **A blind `cp ops/scripts/* ~/.hermes/scripts/` is DESTRUCTIVE.** | Blocks any naive "redeploy everything". | It would REGRESS the live Wave-1d equity-halt-filter safety fix in deployed `playbook-weekly.py` (cron #12, fires THIS Monday 09:30 ET) + `playbook-quarterly.py`, the deployed tiered-emit formatters in `playbook-tick`/`hourly-tick`/`universe-scan`, and the deployed headline counters in `watchlist-evolve`. Every DRIFT must be a per-script three-way reconciliation (DEPLOY-SYNC §49), never a copy. |
| B3 | **The AXP-SHORT-via-CSP bug is STILL firing in prod** and a script-only redeploy is INERT. | Does NOT block the open (it is a pre-existing, contained behavior; the gate still governs sizing). | Enabling the B04 `DIRECTION_BIAS_GATE` fix is a TWO-step coupled change (DEPLOY-SYNC M04), NOT a copy: reconcile B04 into the deployed `autonomous-tick.py` AND add `export HERMES_QUANT_DIRECTION_BIAS_GATE=1` to the armed wrapper. See §2 Step E. |

---

## 2. THE EXACT ORDERED STEPS FOR MONDAY

> Order matters. Run top-to-bottom; **stop** at any gate that does not go green. Steps A–B are
> read-only verification (do these first, always). Steps C–E are optional improvements, each
> independently skippable; if you skip everything from C on, Monday still opens safely.

Set these once at the top of your session:
```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
REPO=/mnt/e/CS/github/hermes-quant
```

### Step A — Pre-open verification (READ-ONLY; agent may run these) — REQUIRED

```bash
# A1. Plugin loads + data flow works end-to-end (HERMES-SELF-ONBOARDING A.3):
$PY -c "import yaml,os; en=yaml.safe_load(open(os.path.expanduser('~/.hermes/config.yaml')))['plugins']['enabled']; print('enabled:', 'hermes-quant' in en)"
~/.hermes/hermes-agent/venv/bin/hermes quant doctor
~/.hermes/hermes-agent/venv/bin/hermes quant status     # mode=paper, halts, kill-switch state

# A2. Confirm .env is EXACTLY the 4 SAFE flags (no GATED money-path flag leaked on):
grep -E '^HERMES_QUANT_' ~/.hermes/.env
#   EXPECT: SEMANTIC_ENABLED=1, REFLECTION=1, MEMORY_INJECT=1, SOCIAL_INGEST=1 — and nothing else.
#   STOP + investigate if you see ADMISSIBILITY / BORROW_COST / OPTIONS_* / MULTILEG_REACTOR /
#   STACKING / CATALYST_ONBOARDING / CONVERGENCE / SATURATION set to 1.

# A3. Confirm host TZ is PT (so the PT cron exprs are correct — do NOT correct them):
date +%Z      # EXPECT: PDT (or PST off-season). If anything else, the schedules need rewriting.

# A4. Read the live cron registry; confirm 16 quant-* enabled:
~/.hermes/hermes-agent/venv/bin/hermes cron list | grep -E 'quant-'
```
**Gate:** plugin enabled+loaded, `.env` = 4 SAFE flags only, TZ=PDT, 16 quant crons enabled.
**If any fail → STOP and resolve before open.** **Rollback:** none (read-only).

### Step B — Deploy-drift audit (READ-ONLY; agent may run) — REQUIRED

```bash
$PY $REPO/ops/deploy/quant-deploy-audit.py        # exit 1 is EXPECTED (drift present)
```
**Gate (informational, NOT a stop):** expect `{REPO_ONLY_NEW:16, DRIFT:8, SAME:4, DEPLOYED_ONLY:4}`,
exit 1. catalyst-ingest, catalyst-coverage, catalyst-eval-gate show **SAME**. This confirms the
drift is the documented "repo fixes not yet live + provenance debt" state, not a broken-cron gap.
**The running loop is fine regardless** — every live cron invokes its deployed copy.
**Rollback:** none (read-only).

> **If you want to do NOTHING further: STOP HERE. Monday opens safely.** Steps C–E are strictly
> additive improvements, each gated.

### Step C — (Optional, recommended) Deploy + register the 2 read-only watchdogs

These are pure `no_agent=True`, `deliver='local'` analysis crons. They take **no risk-changing env**,
ran `rc=0` SILENT in the assessment, and cannot touch the money path. `calibrator-drift`'s only flag
(`CALIBRATOR_AUTO_REFIT`) is **default-OFF and stays off the cron** (alert-only — see §3).

```bash
# C1. Deploy (REPO_ONLY_NEW → safe to copy; nothing to clobber — DEPLOY-SYNC §49 step 3):
cp $REPO/ops/scripts/quant-catalyst-profitability.py ~/.hermes/scripts/quant-catalyst-profitability.py
cp $REPO/ops/scripts/quant-calibrator-drift.py        ~/.hermes/scripts/quant-calibrator-drift.py

# C2. Verify each runs standalone under the venv BEFORE registering (CRON-REGISTRY §3.2):
$PY ~/.hermes/scripts/quant-catalyst-profitability.py   # expect exit 0, quiet (no transition)
$PY ~/.hermes/scripts/quant-calibrator-drift.py         # expect exit 0, quiet (drift < 5%)

# C3. Re-audit; confirm both flip to SAME (not DRIFT, not REPO_ONLY_NEW):
$PY $REPO/ops/deploy/quant-deploy-audit.py
```
**GATE before registering:** both scripts exit 0 in C2 AND show `SAME` in C3.
**If not green → STOP** (a `create` errors on first tick if the script is absent — B1).

```bash
# C4. Register (operator / Hermes-agent runs in a Hermes session — NOT this subagent, NOT CronCreate):
cronjob action='create' name='catalyst-profitability-weekly' schedule='0 7 * * 1' script='quant-catalyst-profitability.py' no_agent=true deliver='local'
cronjob action='create' name='calibrator-drift-weekly'       schedule='0 7 * * 1' script='quant-calibrator-drift.py'        no_agent=true deliver='local'

# C5. Verify registration:
cronjob action='list'                                  # grep for both new names
#   expect each: schedule='0 7 * * 1'  no_agent=true  deliver='local'
```
Schedule `0 7 * * 1` = 07:00 PT Mon = 10:00 ET Mon. **Do NOT "correct" for UTC** — the
calibrator-drift docstring's "07:00 UTC" comment is superseded by the PT scheduler (CRON-REGISTRY §2.2).

**Rollback (Step C):**
```bash
cronjob action='delete' name='catalyst-profitability-weekly'
cronjob action='delete' name='calibrator-drift-weekly'
rm ~/.hermes/scripts/quant-catalyst-profitability.py ~/.hermes/scripts/quant-calibrator-drift.py
```
No `.env` change, no money-path touch — fully reversible.

### Step D — (Optional) Deploy the W2 weekly-retro cron (DEPLOY ONLY — do NOT flip the flag Monday)

W2 (`HERMES_QUANT_WEEKLY_RETRO`) is the next item in the `W1→W2→W3→W6` self-evolve spine (W1 keystone
is already ON). **This Monday: deploy + register the cron only. Hold the flag flip** — per the
flag-flip-decision doc, flipping `WEEKLY_RETRO=1` now is **inert** (no `beliefs.jsonl` corpus exists
yet; the cron must run first to produce it), and per FEATURE-ENABLEMENT §2.10 it is **GATED** behind
its held-out eval. Deploying the cron OFF-flag is a byte-identical no-op (it exits 0 silent until a
belief is distilled), so it is safe to land now and lets the corpus start accruing.

```bash
# D1. Deploy (REPO_ONLY_NEW — safe copy):
cp $REPO/ops/scripts/quant-weekly-retro.py ~/.hermes/scripts/quant-weekly-retro.py
$PY ~/.hermes/scripts/quant-weekly-retro.py             # flag-OFF: expect exit 0, empty stdout (byte-identical no-op)
$PY $REPO/ops/deploy/quant-deploy-audit.py              # confirm SAME

# D2. Register the cron (schedule per CRON-REGISTRY row 19 — the canonical registry):
cronjob action='create' name='quant-weekly-retro' schedule='30 13 * * 0' script='quant-weekly-retro.py' no_agent=true deliver='local'
```
> **Schedule reconciliation note:** CRON-REGISTRY row 19 (the canonical single-source-of-truth for
> trading crons) and HERMES-SELF-ONBOARDING C.2 both specify `30 13 * * 0` (Sun 16:30 PT). The
> SELFEVOLVE-ENABLEMENT §W2 snippet says `0 6 * * 6` (Sat 06:00 PT) — that is an internal doc
> inconsistency; **use `30 13 * * 0` (CRON-REGISTRY wins for cron scheduling).**

**The flag flip stays OFF this Monday.** It is unlocked later (NOT Monday) only after BOTH:
(a) the W2 held-out eval gate is green —
`pytest tests/memory/test_weekly_retro.py tests/memory/test_weekly_retro_eval_gate.py tests/governance/test_promotion.py -q`
(digest must not regress hit-rate/alpha vs no-digest baseline; belief count ≤ budget; half-life
plateau-stable), AND (b) the deployed cron has run ≥1 cadence so a `beliefs.jsonl` corpus exists.
Only then: `echo 'HERMES_QUANT_WEEKLY_RETRO=1' >> ~/.hermes/.env` (`MEMORY_INJECT` is already ON).
Observe one week before considering W3.

**Rollback (Step D):**
```bash
cronjob action='delete' name='quant-weekly-retro'
rm ~/.hermes/scripts/quant-weekly-retro.py
# (if the flag was ever later set): sed -i '/HERMES_QUANT_WEEKLY_RETRO=/d' ~/.hermes/.env
```

### Step E — (Optional, behavior-changing) The AXP-SHORT-via-CSP fix (B04 / `DIRECTION_BIAS_GATE`)

This is the ONLY behavior-affecting deploy worth doing, and it is the one place a redeploy alone is
**inert** (M04 coupling). It is a TWO-step coupled change on the **live fire path**
(`quant-autonomous-tick.py`, cron #8). **If you do not want to touch live fire-path code over a
weekend, SKIP this — the bug persists but nothing regresses; the gate still governs all sizing.**

`DIRECTION_BIAS_GATE` is **SAFE-NOW** (abstain-only: it can only turn a FIRE into a
`DIRECTION_BIAS_MISMATCH` abstain — never fires, widens, or flips; FEATURE-ENABLEMENT §2.1).

```bash
# E1. RECONCILE (three-way merge, NOT a copy — DEPLOY-SYNC §49 step 2): merge ONLY the repo's
#     B04 direction-bias gate + ADR-0075 admitted_via attribution + corrupt-row guard INTO the
#     deployed ~/.hermes/scripts/quant-autonomous-tick.py. Do NOT clobber the deployed file
#     (it carries live wiring). Diff first:
diff $REPO/ops/scripts/quant-autonomous-tick.py ~/.hermes/scripts/quant-autonomous-tick.py

# E2. PROBE the flag OFF→ON with the §0 dry-tick (blast-radius zero; places NO orders):
AUDIT=~/.hermes/quant/governance/audit_log.jsonl
TICK=~/.hermes/scripts/quant-autonomous-tick.py
HERMES_QUANT_DIRECTION_BIAS_GATE=0 $PY "$TICK" --dry-run --json > /tmp/tick-before.json 2>&1
HERMES_QUANT_DIRECTION_BIAS_GATE=1 $PY "$TICK" --dry-run --json > /tmp/tick-after.json  2>&1
diff <(jq -c '{symbol,gate,gated_reason}' /tmp/tick-before.json) \
     <(jq -c '{symbol,gate,gated_reason}' /tmp/tick-after.json)
```
**GATE (FEATURE-ENABLEMENT §2.1 acceptance):** the ONLY delta is recos flipping
`FIRE`/`DRY_RUN_FIRE` → `gate=DIRECTION_BIAS_MISMATCH`. **If ANY symbol gains a fire it didn't have,
or a size widens → STOP, do NOT proceed; the merge is wrong.**

```bash
# E3. ARM the flag in the wrapper (the coupling — a redeploy alone is inert; DEPLOY-SYNC M04).
#     Add this line to ~/.hermes/scripts/quant-autonomous-tick-armed.sh:
#         export HERMES_QUANT_DIRECTION_BIAS_GATE=1
```
**Note on timing:** prefer doing E over the weekend or pre-open, NOT mid-session, so the next
:30 in-market tick (cron #8) picks up a verified change cleanly.

**Rollback (Step E):**
```bash
# Remove the export line from the armed wrapper, and restore the pre-merge deployed script
# from your backup (take one before E1: cp ~/.hermes/scripts/quant-autonomous-tick.py{,.pre-b04.bak}).
cp ~/.hermes/scripts/quant-autonomous-tick.py.pre-b04.bak ~/.hermes/scripts/quant-autonomous-tick.py
# Removing the wrapper export alone also disables the fix (flag-OFF = abstain path inert).
```

### Step F — Post-change verification (if you ran C/D/E)

```bash
cronjob action='list' | grep -E 'catalyst-profitability-weekly|calibrator-drift-weekly|quant-weekly-retro'
$PY $REPO/ops/deploy/quant-deploy-audit.py     # reconciled scripts should now show SAME
~/.hermes/hermes-agent/venv/bin/hermes quant status      # mode still paper; no unexpected halt
```

---

## 3. WHAT STAYS DEFAULT-OFF PENDING ITS GATE (do NOT flip Monday)

Every item below is correctly OFF. None may be flipped this Monday. Each line is the gate that must
clear first. **The deterministic gate + HITL remain the final authority regardless of any of these.**

### B.1 Money-path capability flags — GATED on task #11 + named evals (FEATURE-ENABLEMENT §1)
| Flag | Stays OFF until |
|---|---|
| `ADMISSIBILITY` | task #11 (`effective_size`-as-`qty` unit bug) + live-broker fidelity eval |
| `BORROW_COST` | task #11 (PIL-debit bug); pairs with `ADMISSIBILITY` |
| `OPTIONS_GATE` | task #11 (4 options-gate findings) + the multi-leg reactor (does not exist) |
| `OPTIONS_LIVE_CHAIN` | task #11 + options-data end-to-end eval |
| `MULTILEG_REACTOR` | **PERMANENTLY OFF — no live order rail exists**; inert scaffold, do not enable |
| `CATALYST_ONBOARDING` | AND-gated on `SEMANTIC_ENABLED=1` + ADR-0075 promotion path (not built) |
| `STACKING` | settlement v0.1.2 (exit-fill joining) + eval-gate the stack vs baseline |

### ADR-0082/0083/0084 capability flags — NOT YET BUILT (default-OFF when they land)
- **`STRUCTURE_SELECT`** (ADR-0082 Part B): deliberation `structure_intent` → deterministic
  stance×IV-regime table → options producer → gate. Stays OFF until the `OPTIONS_GATE` hardening
  (#11) **and** an as-of-honest IV-regime eval pass.
- **ADR-0083** DEFERs the intraday flag — **there is no intraday flag to flip.**
- These are listed default-OFF when built; nothing to enable Monday.

### B.2 Perception PDR extras — one-at-a-time after each shadow eval (HERMES-SELF-ONBOARDING B.2)
| Flag | Stays OFF until |
|---|---|
| `TREND_VELOCITY` | shadow-eval vs forward returns; currently decision-inert + costly (ingest doesn't pass `velocity_by_symbol`); flipping adds a full store read per recommend for zero decision effect |
| `CONVERGENCE` | **multi-source accumulation** — the structural multi-source feed exists, but the measured kept-vs-dropped check shows flipping now would DROP the exact consumer-trend names (CELH/CROX/TPR) the thesis needs. Stays OFF until an ORGANIC same-symbol news∩social convergence on a tradeable name appears in-window (data/market-gated, per the flag-flip-decision Addendum 5). Flipping it now silences real single-source news signal. |
| `SATURATION` | **B09** side-by-side audit on live data — the builder would decay live semantic confidence on the next recommend with no B09 vetting. Stays OFF pending B09. |

### B.3 Self-evolve W-flags — dependency-ordered, each gated (SELFEVOLVE-ENABLEMENT)
- **W3 `MONTHLY_META_RETRO` / W6 `RESEARCH_LOOP`:** order-blocked behind the W2 observation window
  (need ≥1 month of W2 output) even though their unit gates pass today.
- **W4 `FACTOR_WEIGHT_PROPOSER`:** held-out WalkForward strict-beat; BMA auto-learn additionally
  blocked until settlement v0.1.2 (O6).
- **W5 `GRAPH_MINING`:** corpus volume + `MIN_SAMPLE=20`/`MIN_HIT_RATE=0.6` held-out gate.
- **W7 `REDTEAM_TURN`:** needs `RESEARCH_DEBATE=1` (a cost decision) + the 3/3 dissent gate.
- **W2 `WEEKLY_RETRO`:** flag stays OFF Monday even if its cron is deployed (§2 Step D) — flip only
  after its held-out eval is green AND the cron has produced a `beliefs.jsonl` corpus.

### Cron-level
- **`CALIBRATOR_AUTO_REFIT` stays OFF the cron** — the `calibrator-drift-weekly` cron is alert-only;
  never add the flag to the cron. If ever armed, do it via a wrapper `.sh` (CRON-REGISTRY §3.3), never
  by hand-editing `jobs.json`.

### Order-capable MCPs
- The 4 keyless MCP servers added (coingecko, tradingview, yahoo-finance, sec-edgar) are **read-only
  data bridges** — none carries KEY/TOKEN/SECRET/API env, none is order-capable. No order-placement
  MCP is enabled. **Live broker order placement is NOT enabled and is not in scope for Monday.**
  Worst case if a keyless server fails to spin up is a single data tool being unavailable to
  deliberation — it cannot break gateway load or the trading path.

---

## 4. ROLLBACK — quick reference (per step)

| Step | What it changed | Rollback |
|---|---|---|
| A, B, F | nothing (read-only) | n/a |
| C (watchdogs) | deployed 2 scripts + registered 2 crons | `cronjob action='delete' name='catalyst-profitability-weekly'`; `cronjob action='delete' name='calibrator-drift-weekly'`; `rm ~/.hermes/scripts/quant-{catalyst-profitability,calibrator-drift}.py` |
| D (weekly-retro cron) | deployed 1 script + registered 1 cron (flag stays OFF) | `cronjob action='delete' name='quant-weekly-retro'`; `rm ~/.hermes/scripts/quant-weekly-retro.py`; if flag ever set: `sed -i '/HERMES_QUANT_WEEKLY_RETRO=/d' ~/.hermes/.env` |
| E (AXP / B04 fix) | reconciled deployed `autonomous-tick.py` + wrapper export | restore `~/.hermes/scripts/quant-autonomous-tick.py` from the `.pre-b04.bak` backup; remove the `export HERMES_QUANT_DIRECTION_BIAS_GATE=1` line from `quant-autonomous-tick-armed.sh` (removing the export alone disables the fix) |
| Any flag flipped later | `.env` line | `sed -i '/HERMES_QUANT_<FLAG>=/d' ~/.hermes/.env` (re-read on next cron tick; no state to migrate) |
| **EMERGENCY (any change misbehaves)** | — | `hermes quant halt '*' --reason "..."` — halts at the deterministic gate, **downstream of every flag**. Clearing requires a `HumanApprovalToken` (scope `kill_switch_clear`); it cannot self-clear. |

---

## 5. SAFETY FRAME (holds for every step above)

1. **Hermes proposes; the deterministic gate + human decide.** Every enable ends in an operator command.
2. **The deterministic gate, hard limits, the ladder `{0,±0.05,±0.10,±0.15,±0.20}`, and the
   kill-switch sit OUTSIDE every flag** and are immutable by the loop (test-asserted per wave).
3. **Every flag is default-OFF, off-state byte-identical, eval-gated, reversible.** Enabling is
   strictly additive — flip one, observe ≥1 cadence, confirm the gate, then the next. **Never batch.**
4. **Silence-by-default is the safe failure mode.** A failed eval gate → buffer, not promote.
   Schema deviation / missing context → abstain, not fire.
5. **Live broker order placement is NOT enabled.** Reactor is hardwired to `PaperReactor()`,
   `allow_live=False`. No execution/order MCP is registered. Nothing in this runbook changes that.
6. **Never run a blind `cp ops/scripts/* ~/.hermes/scripts/`** — every DRIFT is a three-way
   reconciliation (DEPLOY-SYNC §49). The dangerous action is the blind copy, not leaving drift alone.
