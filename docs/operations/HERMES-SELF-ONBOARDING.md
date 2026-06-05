# HERMES-SELF-ONBOARDING — the runbook Hermes reads to onboard hermes-quant and run the meta-loop

**Status:** canonical self-onboarding + meta-loop runbook (Hermes-facing)
**Date:** 2026-05-31
**Audience:** the Hermes Agent (ARIA / Claude / Codex running inside the gateway) — and the human operator.
**Consolidates (does not contradict):**
[HERMES-INTEGRATION.md](HERMES-INTEGRATION.md) · [FEATURE-ENABLEMENT.md](FEATURE-ENABLEMENT.md) ·
[SELFEVOLVE-ENABLEMENT.md](SELFEVOLVE-ENABLEMENT.md) · [CRON-REGISTRY.md](CRON-REGISTRY.md) ·
[DEPLOY-SYNC.md](DEPLOY-SYNC.md) · [ROLLOUT.md](ROLLOUT.md).

> ## THE ONE INVARIANT — read before doing anything
> **Hermes PROPOSES; the deterministic gate + the human DECIDE.** Every step below either reads state,
> runs a blast-radius-zero dry probe, or *produces a command the operator runs by hand*. The agent
> **cannot**: write `~/.hermes/.env` (tool-guarded), register a cron (no `cronjob` tool in the subagent
> env), edit `~/.hermes/config.yaml` (gateway-owned), or move money (no execution tool exists). The
> deterministic risk gate (ADR-0004/0079 D-1), the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`,
> and the kill-switch sit OUTSIDE every flag and are immutable by the loop. Silence-by-default is the
> safe failure mode. **If unsure, do nothing and surface the proposed command.**

---

## Part A — Discover + load the plugin

### A.1 What hermes-quant is, to Hermes
A **pip entry-point plugin** (`hermes_agent.plugins :: hermes-quant = hermes_quant`), `kind: standalone`.
Hermes discovers it from `importlib.metadata` entry points — **no `~/.hermes/plugins/` directory**. On
load, `register(ctx)` runs once at gateway startup and wires: **17 read-only tools** (`toolset="quant"`),
the `/quant` slash, the `hermes quant` CLI control plane, the `pre_gateway_dispatch` hook, and the bundled
skill. It spawns no daemon (<50 ms). See HERMES-INTEGRATION §1-2 for the full surface.

### A.2 The load gate + THE GOTCHA
`standalone` entry-point plugins are **opt-in**: they load only if their name is in
`~/.hermes/config.yaml` under `plugins.enabled`. As of 2026-05-30 `hermes-quant` **is** in that list and
the gateway confirms it loads.

> **GOTCHA — `hermes plugins enable hermes-quant` does NOT work for entry-point plugins.** That CLI only
> manages bundled/git-installed plugins; for an entry-point plugin it prints
> `"Plugin 'hermes-quant' is not installed or bundled."` (harmless, ignore). **The real enable is the
> config.yaml allow-list** (HERMES-INTEGRATION §1.3 step 2 / §5.1). Entry-point plugins are never
> grandfathered. A running gateway must be **restarted** to pick up a newly-added plugin.

### A.3 Verify load (agent may run these — read-only)
```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
# 1. discovered as an entry-point plugin?
$PY -c "import importlib.metadata as m; print('discovered:', any(e.name=='hermes-quant' for e in m.entry_points().select(group='hermes_agent.plugins')))"
# 2. enabled in config.yaml? (the §A.2 gate)
$PY -c "import yaml,os; en=yaml.safe_load(open(os.path.expanduser('~/.hermes/config.yaml')))['plugins']['enabled']; print('enabled:', 'hermes-quant' in en)"
# 3. loaded with tools live?
~/.hermes/hermes-agent/venv/bin/hermes plugins list 2>&1 | grep -i quant
# 4. data flow works end-to-end (CLI control plane):
~/.hermes/hermes-agent/venv/bin/hermes quant doctor
~/.hermes/hermes-agent/venv/bin/hermes quant status
```
If load is missing: the fix is the operator-run config.yaml idempotent insert in HERMES-INTEGRATION §1.3
step 2, then a gateway restart. **The agent surfaces that command; it does not edit config.yaml.**

### A.4 Money-via-CLI-only is structural
Hermes does not distinguish "money" from "read-only" tools — **we** enforce it: no execution tool is ever
registered; the dangerous lifecycle (`start/stop/restart/backtest`) lives under `register_cli_command`
(shell-only, never chat); `quant_approve` fires only the `PaperReactor` on an already-human-surfaced
proposal record (paper + proposal-store, never a live broker). The deterministic gate is the final
authority; no LLM is on the action path. (HERMES-INTEGRATION §3.)

---

## Part B — The COMPLETE flag inventory (every `HERMES_QUANT_*`, in dependency order)

All flags are read at runtime from the deployed `~/.hermes/.env` (tool-guarded; **operator flips, agent
probes**). A flag added to `.env` takes effect on the **next cron invocation** — no daemon restart. The
universal probe (dry tick + audit-log diff) is in FEATURE-ENABLEMENT §0; the acceptance for a SAFE flag is
"only *additional* abstain/silence rows, never a new `gate=FIRE` or widened size."

> **The one-liner shape (operator runs; agent cannot):**
> `echo 'HERMES_QUANT_<FLAG>=1' >> ~/.hermes/.env`  ·  rollback:
> `sed -i '/HERMES_QUANT_<FLAG>=/d' ~/.hermes/.env`. A `no_agent` cron reads it on the next tick.

### B.0 Already ON in deployed `~/.hermes/.env` / armed wrappers (do not re-flip)
| Flag | Where | State | Note |
|---|---|---|---|
| `SEMANTIC_ENABLED` | `~/.hermes/.env` | **ON** | the only flag set in the bare `.env`; semantic perception live |
| `PORTFOLIO_CAPS` | `*-armed.sh` wrapper | **ON (prod)** | hard-set in the deployed armed wrappers (DEPLOY-SYNC M09) |
| `PAPER_SLIPPAGE_MODEL=v0.2` | `*-armed.sh` wrapper | **ON (prod)** | hard-set in the armed wrappers |
| `AUTONOMOUS` / `AUTONOMOUS_ARMED` / `AUTONOMY=paper` | `*-armed.sh` wrapper | **ON (prod)** | the reversible autonomy arming; revert by pointing `script:` back at the bare `.py` |
| `REFLECTION` (W1) + `MEMORY_INJECT` | per SELFEVOLVE-ENABLEMENT | ON if W1 enabled | keystone reflection loop; check `.env` |

> **Posture audit must check BOTH the repo AND `~/.hermes/scripts/*-armed.sh`** — they diverge
> (DEPLOY-SYNC §"Repo ≠ deploy"). `PORTFOLIO_CAPS`/`PAPER_SLIPPAGE` are silently live in prod via the
> wrappers even though the repo shows them default-OFF.

### B.1 Trading / money-path wave (FEATURE-ENABLEMENT is authoritative)
Dependency order; **SAFE-NOW** = abstain/registry/alert-only (flip after one clean §0 probe); **GATED** =
touches the money path, wait on task #11 + the named eval.

| Order | Flag | Class | What it does | Gate / blocked on |
|---|---|---|---|---|
| 1 | `DIRECTION_BIAS_GATE` | **SAFE-NOW** | neutralizes a reco whose direction can't route to any eligible play (AXP-SHORT-via-CSP fix); abstain-only | §0 probe → only `DIRECTION_BIAS_MISMATCH` rows. **Coupled:** also needs the flag added to the armed wrapper (DEPLOY-SYNC §"Coupling") |
| 2 | `IC_DEDUP_AT_INGEST` (+`IC_DEDUP_THRESHOLD`) | **SAFE-NOW** | rejects a redundant alpha factor at *registration* (raises `RedundantFactorError` before any append) | factor-registration: rejected dup adds no JSONL line |
| 3 | `CALIBRATOR_AUTO_REFIT` | **SAFE-NOW** | weekly drift cron auto-refits the isotonic calibrator instead of alert-only | drift cron OFF vs ON diff; sub-threshold = no-op |
| — | `ADMISSIBILITY` | GATED | ADR-0077 pre-trade shortability/borrow gate; REJECT-only | #11 (`effective_size`-as-`qty` unit bug) + live-broker fidelity eval |
| — | `BORROW_COST` | GATED | daily borrow-carry (PIL) accrual on shorts | #11 (PIL-debit bug); pairs with `ADMISSIBILITY` |
| — | `OPTIONS_GATE` | GATED | the ADR-0027 options risk gate (raises `OptionsGateDisabled` when OFF) | #11 (4 options-gate findings) + the multi-leg reactor (does not exist) |
| — | `OPTIONS_LIVE_CHAIN` | GATED | live options-chain fetch (inert unless `=1` AND creds) | #11 + options-data eval |
| — | `MULTILEG_REACTOR` | GATED (**stays OFF**) | multi-leg paper execution | **no live order rail exists** — inert scaffold; do not enable |
| — | `CATALYST_ONBOARDING` | GATED | admits an out-of-universe symbol off a catalyst packet | AND-gated on `SEMANTIC_ENABLED=1` + ADR-0075 promotion path (not built) |
| — | `STACKING` | GATED | logistic-stacking BMA path (ADR-0003); accumulates correlation when `=1` | settlement v0.1.2 (Beta-posterior auto-learning blocked until exit-fill joining); eval-gate the stack vs baseline |

### B.2 Perception PDR flags (the social-arb / catalyst sense layer; default-OFF)
These shape the `PerceptionFrame` builder (`perception/builder.py:193,227,256`) and catalyst synthesis;
they are perception-evidence that can only inform the gate, never authorize.

| Flag | Default | What it does | Gate |
|---|---|---|---|
| `SOCIAL_INGEST` | per `.env` (recon: ON) | ingests social packets into the catalyst/semantic stream | social-ingest eval; pairs with `SEMANTIC_ENABLED` |
| `TREND_VELOCITY` | OFF | velocity feature in the frame builder + catalyst synthesize (`builder.py:193`, `synthesize.py:199`) | shadow-eval the velocity signal vs forward returns |
| `CONVERGENCE` | OFF | cross-source convergence feature (`builder.py:227`, `synthesize.py:124`); `catalyst/eval.py:232` flips it on for its own eval | convergence eval (`catalyst/eval.py`) |
| `SATURATION` | OFF | info-saturation / parity-exit feature (`semantic.py:137`, `builder.py:256`) | saturation eval |

> These three (`TREND_VELOCITY`/`CONVERGENCE`/`SATURATION`) are the social-arb perception method
> (velocity + convergence + info-parity-exit, per the PDR vision). They are advisory perception — enable
> after the catalyst/social eval shows the feature does not regress, one at a time.

### B.3 Self-evolve W-flags (SELFEVOLVE-ENABLEMENT is authoritative; advisory-plane only)
Dependency graph: **W1 keystone → W2 → W3 → W6**; W4/W5/W7 parallel after W1. Every one writes only
beliefs / hypotheses / candidate-weights / candidate-edges / telemetry — **none can touch the gate, the
hard limits, the ladder, or the kill-switch** (test-asserted per wave). The ONLY path to live is the
outer deterministic OOS backtest + promotion gate + **operator sign-off, which never automates**.

| Wave | Flag | What it produces | Eval gate (must pass first) |
|---|---|---|---|
| W1 | `REFLECTION` (+`MEMORY_INJECT`) | per-trade reflection corpus → PM lessons block (source-water for W2/W3) | `test_w1_decision_loop_liveness.py` green (plumbing; no alpha claim) |
| W2 | `WEEKLY_RETRO` | weekly CVRF distill (winners/losers by realized **alpha**) → bounded decaying `beliefs.jsonl`; emits `weekly_retro_promotion_readiness` (closes O3) | held-out: digest does NOT regress hit-rate/alpha; belief count ≤ budget; half-life plateau-stable |
| W3 | `MONTHLY_META_RETRO` | monthly aggregate → repeating-lesson trends, persona-calibration **telemetry-only**, novelty/dedup-gated **candidate** hypotheses `status="open"` | `quant-monthly-meta-retro-eval-gate.py` → `GATE: ✅ PASS` (4/4) |
| W6 | `RESEARCH_LOOP` | drives `HypothesisRunner → FactorOracle → PromotionOrchestrator` on W3 candidates; **review-only `PromotionRecord`s, zero auto-promotion** | 7/7 (reproducible Run-Cards; lookahead sentinel load-bearing; ZERO auto-promotion; byte-identical off-state) |
| W4 | `FACTOR_WEIGHT_PROPOSER` | weekly **candidate** BMA-weight diff → `weight-candidates.json` (proposal-only, operator applies) | proposed weights STRICTLY beat prior-best on time-ordered held-out WalkForward; plateau-selected |
| W5 | `GRAPH_MINING` | mines `propagation-log.jsonl` vs fwd returns → per-edge FLIP/DOWNWEIGHT/PRUNE → `graph-mine-candidates.json` (never auto-edits seed YAML) | `MIN_SAMPLE=20`/`MIN_HIT_RATE=0.6`; FLIP also passes `run_sign_consistency` |
| W7 | `REDTEAM_TURN` | Socratic devil's-advocate turn in the debate; fills `counterarguments`; surfaces dissent | 3/3 (dissent-rate up WITHOUT inflating false-flat; judge reco/confidence bit-identical ON vs OFF) |

### B.4 Research-plane safety + ancillary flags
| Flag | Default | What it does | Note |
|---|---|---|---|
| `RESEARCH_RISK_TIER_BLOCK` | OFF | opt-in HARD-BLOCK: a hypothesis whose text trips `classify_risk_tier` is REFUSED instead of downgraded (`research/hypothesis.py:314`) | the research plane has no trading authority anyway; this is a belt-and-suspenders refusal. SAFE-NOW class (refuse-only) — flip after confirming it only blocks flagged hypotheses |
| `RESEARCH_DEBATE` (+`_ROUNDS`) | OFF | enables the bull/bear adversarial debate stage (ADR-0065/0066); `_ROUNDS` clamped to `MAX_ALLOWED_ROUNDS=3` | pairs with `REDTEAM_TURN`; rollout per ROLLOUT.md |
| `REGIME_HMM` / `REFLECTOR_LLM` / `RISK_COMMITTEE_LLM` / `TRADER_LLM` | OFF | the four v0.2 LLM surfaces | **ROLLOUT.md is authoritative** (dwell/KPI/kill-switch); not re-covered here |
| `DELIBERATIVE` (+`_RISK`/`_QUICK_MODEL`/`_DEEP_MODEL`/`_PROMOTE`) | OFF | the deliberative committee decision layer (ADR-0023) | committee = evidence; gate still final |
| `FUNDAMENTALS_ENABLED` / `INSIDER_ENABLED` / `ANALYSTS_USE_REGIME` | per `.env` | analyst toggles (perception evidence) | abstain-only analysts; safe |
| `OPEN_GUARD` | per `.env` | the per-ET-day open-guard idempotency (ADR-0072) | keep ON; it is a safety dedup |
| `PIT_UNIVERSE` / `WATERMARK_ENABLED` / `SNAPSHOT_V2` / `DATA_FALLBACK` | per `.env` | data-honesty + provenance toggles | no-lookahead discipline |

> **Future flags this study proposes (NOT YET built; default-OFF when they land):** a new
> `STRUCTURE_SELECT` flag for ADR-0082 Part B (deliberation `structure_intent` → deterministic
> stance×IV-regime table → existing options producer → gate). It stays OFF until the `OPTIONS_GATE`
> hardening (#11) + an as-of-honest IV-regime eval pass. There is no intraday flag — ADR-0083 DEFERs it.

---

## Part C — Crons to register (CRON-REGISTRY is authoritative)

**Mechanism:** crons are DB-backed jobs in `~/.hermes/cron/jobs.json`, written ONLY by the Hermes
**`cronjob`** agent/MCP tool (`action ∈ {create,list,delete,update,pause,resume,run}`). **NOT** `cron(8)`,
**NOT** the harness `CronCreate`. The subagent that wrote this doc has no `cronjob` tool — every
`cronjob action='create'` block is a command the **operator / the Hermes gateway agent** runs in a Hermes
session. Schedules evaluate in **PT** (= ET − 3h, stable year-round). `no_agent=true` ⇒ deterministic
script, empty stdout = SILENT.

**Deploy-sync prerequisite (always):** a cron runs the **deployed** `~/.hermes/scripts/` copy, which
drifts from the repo. `cronjob create` errors on first tick if the script is absent. Before registering:
deploy the script (`cp ops/scripts/<name>.py ~/.hermes/scripts/`) and run
`python ops/deploy/quant-deploy-audit.py` to confirm `SAME`/`REPO_ONLY_NEW`, never clobbering a
`DEPLOYED_ONLY` (vendor live→repo first). See DEPLOY-SYNC.

### C.1 Live now (16 quant crons in jobs.json, all enabled) — see HERMES-INTEGRATION §4.1 / CRON-REGISTRY §1
PERCEPTION: `quant-universe-scan-daily`, `quant-watchlist-evolve-daily`, `quant-catalyst-coverage-daily`,
`quant-catalyst-ingest-30min`. DECISION: `quant-daily-{premarket,midday,eod}-interim` (LLM briefs),
`quant-playbook-tick-daily`, `quant-playbook-weekly`, `quant-playbook-quarterly`, `quant-hourly-market-tick`,
`quant-autonomous-tick-30min`. SAFETY/REPORTING: `quant-halts-watchdog-daily`,
`quant-proposals-ttl-watchdog-daily`, `quant-portfolio-daily-eod`, `quant-strategy-retro-weekly`.

### C.2 Owed (built, NOT yet deployed/registered) — register after deploy + eval
| Cron | Schedule (PT) | Script | Pairs with flag | Status |
|---|---|---|---|---|
| `catalyst-profitability-weekly` | `0 7 * * 1` | `quant-catalyst-profitability.py` | — (read-only) | owed (CRON-REGISTRY §2.1) |
| `calibrator-drift-weekly` | `0 7 * * 1` | `quant-calibrator-drift.py` | `CALIBRATOR_AUTO_REFIT` (off the cron) | owed (CRON-REGISTRY §2.2) |
| `quant-weekly-retro` | `30 13 * * 0` | `quant-weekly-retro.py` | `WEEKLY_RETRO` (W2) | owed (SELFEVOLVE §W2) |
| `quant-monthly-meta-retro` | `0 14 1 * *` | `quant-monthly-meta-retro.py` | `MONTHLY_META_RETRO` (W3) | owed (SELFEVOLVE §W3) |
| `research-loop-weekly` | `0 8 * * 1` | `quant-research-loop.py` | `RESEARCH_LOOP` (W6) | owed (SELFEVOLVE §W6) |
| `quant-factor-weight-propose` | `30 6 * * 6` | `quant-factor-weight-propose.py` | `FACTOR_WEIGHT_PROPOSER` (W4) | owed (SELFEVOLVE §W4) |
| `quant-catalyst-graph-mine` | `0 6 * * 6` | `quant-catalyst-graph-mine.py` | `GRAPH_MINING` (W5) | owed (SELFEVOLVE §W5) |

Registration template (operator runs):
```text
cronjob action='create' name='<name>' schedule='<PT expr>' script='<name>.py' no_agent=true deliver='local'
```
Verify: `cronjob action='list'` (then grep), or `hermes cron list | grep quant-`.

---

## Part D — THE META-LOOP (the periodic sequence Hermes runs ON TOP of the plugin for self-evolvability)

This is the loop that makes the system self-evolving **without ever touching the money path**. Hermes
runs it on a cadence; it reads what the advisory plane produced, runs the eval-gated distillers, reviews
candidates, and **surfaces operator-gated promotion commands**. It NEVER flips a flag, registers a cron,
or promotes a candidate itself.

### D.0 Cadence
- **Per-trade (continuous):** W1 reflection fires inline in the reactor — no action; just the source-water.
- **Weekly (run after the Sat/Sun retros have fired):** steps D.1–D.4.
- **Monthly (run after the 1st-of-month meta-retro):** steps D.1–D.6.

### D.1 READ the advisory-plane state (read-only; agent does this)
```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
Q=~/.hermes/quant
# reflections (W1 corpus), beliefs (W2/W3 distillate), meta-retros (W3), candidates (W3/W4/W5)
for f in memory/reflections.jsonl memory/beliefs.jsonl memory/meta_retros.jsonl \
         research/weight-candidates.json catalyst/graph-mine-candidates.json; do
  echo "== $f =="; [ -f "$Q/$f" ] && wc -l "$Q/$f" && tail -3 "$Q/$f" || echo "(absent)"
done
# the audit log (every gate decision + promotion_event) and current halts
tail -20 "$Q/governance/audit_log.jsonl"
~/.hermes/hermes-agent/venv/bin/hermes quant status     # mode, halts, kill-switch state
```
Surface a short digest: which beliefs are active (and their half-life), which hypotheses are `open`,
which weight/edge candidates are pending, any standing halt.

### D.2 RUN the eval-gated distillers in DRY mode (read-only; agent may run if the flags are ON)
The W-crons are change-detecting `no_agent` watchdogs — running them is safe (they only write
beliefs/candidates, never the gate). If a flag is OFF, the cron is a byte-identical no-op. To *preview*
what they would produce without enabling, run the script inline with the flag set for that one process
(this never touches `.env`):
```bash
# preview the weekly retro (does NOT enable it persistently):
HERMES_QUANT_WEEKLY_RETRO=1 "$PY" ~/.hermes/scripts/quant-weekly-retro.py
# preview the monthly meta-retro + its hard eval gate:
HERMES_QUANT_MONTHLY_META_RETRO=1 "$PY" ~/.hermes/scripts/quant-monthly-meta-retro.py
"$PY" /mnt/e/CS/github/hermes-quant/ops/scripts/quant-monthly-meta-retro-eval-gate.py   # expect GATE: ✅ PASS
```

### D.3 REVIEW the factor-weight + graph-mine candidates against their eval gates (read-only)
```bash
# W4: candidate BMA weights must STRICTLY beat prior-best on a held-out WalkForward (the proposer is blind).
HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1 "$PY" ~/.hermes/scripts/quant-factor-weight-propose.py
# W5: per-edge proposals scored on held-out forward returns (MIN_SAMPLE=20 / MIN_HIT_RATE=0.6; FLIP also
#     passes run_sign_consistency). Never auto-edits the seed YAML.
HERMES_QUANT_GRAPH_MINING=1 "$PY" ~/.hermes/scripts/quant-catalyst-graph-mine.py
```
For each candidate, report: did it pass its held-out gate? is it plateau-selected (not the in-sample
peak)? is the sample size above the floor? **A candidate that fails its gate is buffered, not promoted —
say so and stop.**

### D.4 REVIEW the research-loop output (W6) — review-only PromotionRecords
```bash
HERMES_QUANT_RESEARCH_LOOP=1 "$PY" ~/.hermes/scripts/quant-research-loop.py
```
W6 drains W3 `open` candidates → deterministic OOS backtest + lookahead sentinel → (clean + validated
only) PromotionGate, producing **review-only `PromotionRecord`s with ZERO auto-promotion to live**. The
lookahead sentinel is load-bearing: a contaminated candidate is falsified and never reaches the gate.
Report each record's verdict; **a "promote" verdict is a RECOMMENDATION, not an action.**

### D.5 SURFACE the operator-gated promotion (the only path to live; agent NEVER does it)
For anything that passed its eval gate and W6 recommends, emit the exact operator commands and STOP:
- **Apply a candidate BMA weight (W4):** a separate operator step (SELFEVOLVE §W4). Surface the
  `weight-candidates.json` diff + the apply command; do not apply.
- **Apply a graph edge (W5):** the seed YAML edit stays MANUAL regardless of the gate. Surface the
  proposed edge change; do not edit.
- **Promote a hypothesis to live (W6):** ADR-0052 `PromotionOrchestrator` + operator sign-off. Surface the
  `PromotionRecord` id + the promotion command; do not promote.
- **Enable a flag:** the `echo '...' >> ~/.hermes/.env` one-liner + its eval-gate precondition + its §0
  probe acceptance (Part B). The operator runs it; the agent runs the probe and reports the diff.

### D.6 META-RETRO synthesis (monthly) — feed reflections back as proposals
The open-loop gap the system was built to close: reflections are produced, then distilled by W2/W3 into
beliefs + candidate hypotheses, which W6 backtests, which the operator promotes. Hermes' monthly role is
to **read the meta-retro report** (`memory/meta_retros.jsonl`, recommendations-only + persona-calibration
TELEMETRY that nothing consumes), confirm the novelty/dedup-gated candidates are sound, and present the
top 1-3 promotable items with their evidence and the exact operator command. **Persona deltas are
telemetry-only (`|delta| ≤ 0.10`, no aggregator reads them) — never present them as applied.**

### D.7 SAFETY frame (the loop's contract — repeat it on every promotion)
1. **Hermes proposes; the deterministic gate + human decide.** Every D.5 item ends in an operator command.
2. **The advisory plane cannot touch the gate, the hard limits, the ladder `{0,±0.05,±0.10,±0.15,±0.20}`,
   or the kill-switch** (test-asserted per wave; ADR-0080 §5).
3. **Every flag is default-OFF, off-state byte-identical, eval-gated, reversible.** Enabling is strictly
   additive.
4. **Silence-by-default is the safe failure mode.** A failed eval gate → buffer, not promote. Schema
   deviation / missing context → abstain, not fire.
5. **The kill-switch is downstream of every flag:** `hermes quant halt '*' --reason "..."` halts at the
   gate regardless of any flag or candidate. Use it the moment a promoted change misbehaves.

---

## Part E — Quick onboarding checklist (Hermes runs top-to-bottom on first contact)
1. **A.3** verify discovery + enable + load + data-flow. If dark, surface the config.yaml fix + restart.
2. **B.0** audit what is already ON (`.env` AND the armed wrappers — they diverge).
3. **D.1** read the advisory-plane state; produce a one-screen digest.
4. **C.2** check which owed crons are deployed + registered; surface the deploy + `cronjob create` commands
   for any missing.
5. **D.2-D.4** dry-run the eval-gated distillers; report pass/fail per candidate.
6. **D.5** surface operator-gated promotions for anything that passed — and STOP.
7. Confirm the **D.7 safety frame** holds before recommending any change.
