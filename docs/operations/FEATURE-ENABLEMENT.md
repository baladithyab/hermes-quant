# FEATURE-ENABLEMENT: HERMES_QUANT_* Flag-Flip Runbook

**Audience:** Operator (Codeseys) — single-human paper-trading host.
**Scope:** how to safely turn ON each *new* `HERMES_QUANT_*` feature flag landed in the
2026-05-30 deep-work loop. One flag at a time, with a dry-run + audit-log diff, an exact
rollback, and a SAFE-NOW vs GATED verdict.
**Status:** Canonical runbook for the 9 new flags. Complements (does not replace)
[`ROLLOUT.md`](ROLLOUT.md) — that playbook covers the four v0.2 LLM surfaces
(TraderNode / RiskCommittee / Reflector / HMM). This file covers the
admissibility/options/catalyst/calibrator wave.
**Ground truth:**
- Flag inventory + SAFE-NOW/GATED split: [`docs/research/2026-05-30-r-cron-and-flag-inventory.md`](../research/2026-05-30-r-cron-and-flag-inventory.md)
- Why the GATED flags are gated (the convergent reviewer findings → task #11):
  [`docs/reviews/2026-05-30-codex-deep-work-loop/synthesis.md`](../reviews/2026-05-30-codex-deep-work-loop/synthesis.md)

---

## 0. Read this first — who flips, and the .env tool-guard

**The agent cannot flip any flag.** `~/.hermes/.env` is tool-guarded: a coding agent
(ARIA / Claude / Codex) has no write path to it by design — it holds the credentials and the
`HERMES_QUANT_*` autonomy flags, so write access is reserved to the human operator. Every
"enable" step in this runbook ends in a one-liner **the operator runs by hand**. The agent's
role is limited to: running the dry-run probe, diffing the audit log, and reading the result
back to the operator.

Flags are read **at runtime** by the deployed `~/.hermes/scripts/` copy (which drifts from the
repo `ops/scripts/` — `md5sum` both before editing; see the cron registry research note). A
flag added to `~/.hermes/.env` takes effect on the **next cron invocation** — no restart of a
long-running daemon is needed, because the trading crons are short-lived `no_agent` scripts
re-launched per schedule by the DB scheduler.

Currently SET in deployed `~/.hermes/.env`: **only `HERMES_QUANT_SEMANTIC_ENABLED=1`.** Every
flag below is default-OFF in code (grep-verified, synthesis §"Verification evidence"). Landing
the 2026-05-30 wave altered **zero** live behavior.

### The universal probe — dry tick + audit-log diff

Every "enable" step uses the same blast-radius-zero probe: the autonomous-tick script's
**default `--dry-run` mode** runs the full pipeline but places **no orders** — it logs every
would-be fire as `gate=DRY_RUN_FIRE`, which does **not** count toward idempotency
(`quant-autonomous-tick.py:23,160`). So you can run it as many times as you like, before and
after a flag flip, and compare.

```bash
AUDIT=~/.hermes/quant/governance/audit_log.jsonl
PY=~/.hermes/hermes-agent/venv/bin/python3
TICK=~/.hermes/scripts/quant-autonomous-tick.py

# 1. Baseline: snapshot the audit log tail, run a dry tick with the flag OFF.
wc -l "$AUDIT"
HERMES_QUANT_<FLAG>=0 "$PY" "$TICK" --dry-run --json > /tmp/tick-before.json 2>&1
tail -50 "$AUDIT" > /tmp/audit-before.jsonl

# 2. Candidate: same tick with the flag ON (inline env, NOT in .env yet).
HERMES_QUANT_<FLAG>=1 "$PY" "$TICK" --dry-run --json > /tmp/tick-after.json 2>&1
tail -50 "$AUDIT" > /tmp/audit-after.jsonl

# 3. Diff. A SAFE-NOW flag must only turn FIREs into abstains/silences — never the reverse.
diff <(jq -c '{symbol,gate,gated_reason}' /tmp/tick-before.json 2>/dev/null) \
     <(jq -c '{symbol,gate,gated_reason}' /tmp/tick-after.json  2>/dev/null)
diff /tmp/audit-before.jsonl /tmp/audit-after.jsonl
```

**Acceptance for a SAFE-NOW flag:** the only deltas are *additional* `abstain` /
`DIRECTION_BIAS_MISMATCH` / silence rows, or new alert/log lines. If the diff shows a *new
`gate=FIRE`* or a *widened* size that wasn't there with the flag OFF, **stop** — that flag is
not behaving abstain-only; do not write it to `.env`.

> Inline `HERMES_QUANT_<FLAG>=1 <cmd>` sets the var for that one process only. It never touches
> `~/.hermes/.env`. This is how the agent probes without needing write access to the guarded file.

---

## 1. ORDER OF ENABLEMENT (the actionable answer)

### SAFE-NOW — flip today, after one dry-run + clean audit diff (§0 probe)

These are **abstain-only / registry-only / alert-only**: they can only make the system quieter
or reject more, never fire, widen, or flip a position. No money-path correctness dependency.

| Order | Flag | Why safe today |
|---|---|---|
| 1 | `DIRECTION_BIAS_GATE` | Neutralizes an advisor reco whose direction can't route to any eligible play (the AXP-SHORT-via-CSP fix). Can only abstain MORE; never fires/widens/flips (`quant-autonomous-tick.py:319-322`). |
| 2 | `IC_DEDUP_AT_INGEST` | Rejects a redundant alpha factor at *registration*. Registry-only; raises `RedundantFactorError` BEFORE any append/money-path (`alpha_zoo.py:305`). |
| 3 | `CALIBRATOR_AUTO_REFIT` | Lets the weekly drift cron auto-refit the isotonic calibrator instead of alert-only. The drift cron alerts regardless; flip after a clean dry-run + a drift-log review. |

**Recommended sequence:** `DIRECTION_BIAS_GATE` first (run the §0 probe, confirm only
`DIRECTION_BIAS_MISMATCH` rows appear), then `IC_DEDUP_AT_INGEST`, then `CALIBRATOR_AUTO_REFIT`.
Flip one, observe one full trading day, then the next. Do not batch.

### GATED — WAIT on task #11 (pre-go-live hardening) + a fidelity eval

These touch the money path (or its restatement) and every one of them is implicated in a
convergent reviewer finding folded into **task #11** (synthesis §"Convergent findings"). They
are built behind a flag precisely so the bugs *cannot execute* until someone flips them — and
the flip is the operator's gate. **Do not flip any of these until #11 lands and the
relevant eval passes.**

| Flag | Blocked on | The specific #11 finding |
|---|---|---|
| `ADMISSIBILITY` | #11 + a live-broker fidelity eval | `effective_size` (NAV fraction) is passed as share `qty` to the oracle → every short rejects as `FRACTIONAL_SHORT` on flip (`autonomous.py:473-478`); oracle fails-open on missing account context (`oracle.py:167-181`). |
| `BORROW_COST` | #11 (pairs with ADMISSIBILITY) | PIL debited for every supplied dividend, not only held-across-ex-div (`borrow_pnl.py:67`); needs borrow-aware P&L restatement first. |
| `OPTIONS_GATE` | #11 + multi-leg reactor (does not exist) | naked short bypasses no-naked check (`options_gate.py:154-158`); `min_dte=None` fails OPEN (`:457`); greeks scaled by 1 lot not `order_qty` (`data.py:272-279`); CSP under-collateralized (`:171`). |
| `OPTIONS_LIVE_CHAIN` | #11 + options data eval | live-chain fetch feeds the same un-hardened gate; needs the options data layer validated end-to-end. |
| `MULTILEG_REACTOR` | **stays OFF — no live order rail exists** | ADR-0029 reactor is an inert scaffold; reviewers confirmed it "writes nothing while its flag is unset." There is nowhere for a multi-leg fire to go. |
| `CATALYST_ONBOARDING` | AND-gated on `SEMANTIC_ENABLED=1` + ADR-0075 | onboarding flips universe membership from catalyst signal; ADR-0075 promotion path not built. |

The agent **cannot** flip these even if asked. If the operator asks to enable a GATED flag,
the correct response is: "blocked on task #11 + eval — see FEATURE-ENABLEMENT.md §1."

---

## 2. Per-flag runbook

Each entry: what it enables · PRECONDITION · the exact `.env` one-liner (operator-run) · the
side-by-side audit step · rollback · classification.

> **The `.env` one-liner is the same shape for every flag** (operator runs it; the agent
> cannot):
> ```bash
> echo 'HERMES_QUANT_<FLAG>=1' >> ~/.hermes/.env
> ```
> Rollback is always: remove that line (e.g. `sed -i '/HERMES_QUANT_<FLAG>=/d' ~/.hermes/.env`)
> — also an operator action. The flag is re-read on the next cron invocation; there is no state
> to migrate (the OFF path reads/writes the same stores).

---

### 2.1 `DIRECTION_BIAS_GATE` — SAFE-NOW

- **Enables:** neutralizes an advisor recommendation whose *direction* cannot route to any
  eligible play (e.g. a SHORT reco on a name where the only available structure is a CSP). The
  loop tags it `gate=DIRECTION_BIAS_MISMATCH` rather than a generic silence.
- **Precondition:** none beyond the §0 probe. It can only abstain MORE — never fires, widens,
  or flips (`ops/scripts/quant-autonomous-tick.py:319-322`). Reversible by design.
- **Enable (operator):**
  ```bash
  echo 'HERMES_QUANT_DIRECTION_BIAS_GATE=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit:** run the §0 probe with `<FLAG>=DIRECTION_BIAS_GATE`. **Expected delta:**
  one or more recos that were `FIRE`/`DRY_RUN_FIRE` flip to `gate=DIRECTION_BIAS_MISMATCH`;
  the `direction_bias_mismatch` counter in the tick summary goes up. **Reject if** any symbol
  gains a fire it didn't have before.
- **Rollback:** `sed -i '/HERMES_QUANT_DIRECTION_BIAS_GATE=/d' ~/.hermes/.env`
- **Classification: SAFE-NOW.**

### 2.2 `IC_DEDUP_AT_INGEST` — SAFE-NOW

- **Enables:** rejects a redundant alpha factor at registration time (B38) when its return
  series is too IC-correlated with an already-registered factor (threshold = `IC_DEDUP_THRESHOLD`).
- **Precondition:** none. Registry-only; raises `RedundantFactorError` **before** any JSONL
  append or gate-library mutation (`hermes_quant/factors/alpha_zoo.py:305-313`). Never touches
  the order path. Only active when a return series is supplied — bit-identical to the prior
  two-gate path otherwise.
- **Enable (operator):**
  ```bash
  echo 'HERMES_QUANT_IC_DEDUP_AT_INGEST=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit:** the audit-log probe in §0 won't show a diff here (this fires at
  factor registration, not at tick). Instead verify on the next factor-registration path:
  ```bash
  PY=~/.hermes/hermes-agent/venv/bin/python3
  wc -l ~/.hermes/quant/factors/alpha_zoo.jsonl    # before
  # run the registration path / weekly factor cron, then:
  wc -l ~/.hermes/quant/factors/alpha_zoo.jsonl    # after — a rejected dup must NOT add a line
  ```
  **Expected:** a redundant factor is rejected with `RedundantFactorError` and adds **no** line
  to `alpha_zoo.jsonl`. **Reject if** a rejected factor still appears in the JSONL.
- **Rollback:** `sed -i '/HERMES_QUANT_IC_DEDUP_AT_INGEST=/d' ~/.hermes/.env`
- **Classification: SAFE-NOW.**

### 2.3 `CALIBRATOR_AUTO_REFIT` — SAFE-NOW

- **Enables:** the weekly `quant-calibrator-drift` cron *auto-refits* the isotonic calibrator
  when raw→calibrated drift exceeds 5%, instead of alert-only (ADR-0009 §P0-2).
- **Precondition:** a clean dry-run of the drift cron + a review of the drift log. The drift
  cron alerts regardless of this flag (it exits 0 silent unless `should_alert`); auto-refit only
  changes whether it *acts* on the alert. Read at the cron layer (`training/calibrator_drift.py`,
  env `HERMES_QUANT_CALIBRATOR_AUTO_REFIT`).
- **Enable (operator):**
  ```bash
  echo 'HERMES_QUANT_CALIBRATOR_AUTO_REFIT=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit:** run the deployed drift cron in both states and compare its output —
  the refit must be a no-op when drift is under threshold:
  ```bash
  PY=~/.hermes/hermes-agent/venv/bin/python3
  HERMES_QUANT_CALIBRATOR_AUTO_REFIT=0 "$PY" ~/.hermes/scripts/quant-calibrator-drift.py > /tmp/drift-off.txt 2>&1
  HERMES_QUANT_CALIBRATOR_AUTO_REFIT=1 "$PY" ~/.hermes/scripts/quant-calibrator-drift.py > /tmp/drift-on.txt 2>&1
  diff /tmp/drift-off.txt /tmp/drift-on.txt
  ls -la ~/.hermes/quant/calibrators/isotonic.pkl   # mtime changes ONLY if a refit actually ran
  ```
  **Expected:** identical output when drift < 5%; when drift ≥ 5% the ON run reports a refit and
  the `.pkl` mtime updates. **Reject if** a refit fires on sub-threshold drift.
- **Rollback:** `sed -i '/HERMES_QUANT_CALIBRATOR_AUTO_REFIT=/d' ~/.hermes/.env`
  (the next drift cron reverts to alert-only; the last refitted `.pkl` stays — restore from
  `git` / a backup if you want the pre-refit calibrator).
- **Classification: SAFE-NOW.**

---

### 2.4 `ADMISSIBILITY` — GATED

- **Enables:** ADR-0077 pre-trade shortability/borrow gate. With the flag OFF the oracle is the
  `NullShortabilityOracle` (bit-identical to today). ON, it can only **REJECT** a short.
- **Precondition (DO NOT FLIP):** **task #11** must land first. The `effective_size`-as-share-`qty`
  unit bug (3 reviewers) means every short rejects as `FRACTIONAL_SHORT` on flip
  (`autonomous.py:473-478`), and the oracle fails-open on missing account context
  (`oracle.py:167-181`). It also needs a **live-broker fidelity eval** — Codex reviewers cannot
  hit Alpaca, so the predicate vs the real `easy_to_borrow`/`shortable_shares` API response is
  **unverified** (synthesis §"Reviewer framing"). Flipping mid-book with the 38 synthetic shorts
  restates P&L; that restatement is itself wrong until the #11 unit fix
  (`quant-admissibility-restate.py:116`).
- **Enable (operator, ONLY after #11 + eval):**
  ```bash
  echo 'HERMES_QUANT_ADMISSIBILITY=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit (after #11):** §0 probe; expected delta is *only* new `gate=REJECT`
  rows tagged `NOT_ETB`/`FRACTIONAL_SHORT` on shorts. Run
  `tests/integration/test_admissibility_alpaca_live.py` against paper creds first.
- **Rollback:** `sed -i '/HERMES_QUANT_ADMISSIBILITY=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.5 `BORROW_COST` — GATED

- **Enables:** daily borrow-carry (PIL) accrual on short positions (/360 stock-loan basis).
- **Precondition (DO NOT FLIP):** pairs with `ADMISSIBILITY`; needs the same #11 work plus a
  borrow-aware P&L restatement. The PIL-debit finding (`borrow_pnl.py:67` — debits for every
  supplied dividend, not only those held across ex-div) is folded into #11.
- **Enable (operator, ONLY after #11):**
  ```bash
  echo 'HERMES_QUANT_BORROW_COST=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit (after #11):** compare EOD P&L on a short-bearing book with the flag
  OFF vs ON over one settlement; the only delta should be a small negative carry on names held
  across ex-div, never on names not held across ex-div.
- **Rollback:** `sed -i '/HERMES_QUANT_BORROW_COST=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.6 `OPTIONS_GATE` — GATED

- **Enables:** the ADR options risk gate (raises `OptionsGateDisabled` when OFF;
  `risk/options_gate.py:374`).
- **Precondition (DO NOT FLIP):** **task #11** (four convergent options-gate findings: naked-short
  bypass, `min_dte=None` fail-open, greek single-lot scaling, CSP under-collateralization) **and**
  the multi-leg reactor (ADR-0029) — which **does not exist**. The gate has no execution rail to
  protect yet.
- **Enable (operator, ONLY after #11 + reactor + eval):**
  ```bash
  echo 'HERMES_QUANT_OPTIONS_GATE=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit (after #11):** dedicated options-gate fixture suite + §0 probe; expected
  delta is *only* new options rejects, never a new fire.
- **Rollback:** `sed -i '/HERMES_QUANT_OPTIONS_GATE=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.7 `OPTIONS_LIVE_CHAIN` — GATED

- **Enables:** live options-chain fetch (inert unless `=1` **AND** credentials present;
  `options/data.py:413`).
- **Precondition (DO NOT FLIP):** the options data layer must be validated end-to-end and the
  options gate (#11) hardened — live-chain data feeds the same un-hardened gate. No eval exists yet.
- **Enable (operator, ONLY after #11 + options data eval):**
  ```bash
  echo 'HERMES_QUANT_OPTIONS_LIVE_CHAIN=1' >> ~/.hermes/.env
  ```
- **Side-by-side audit (after #11):** confirm a live chain fetch returns well-formed
  greeks/DTE/strikes for a known liquid name and that the gate consumes them; compare against the
  fixture chain.
- **Rollback:** `sed -i '/HERMES_QUANT_OPTIONS_LIVE_CHAIN=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.8 `MULTILEG_REACTOR` — GATED (stays OFF)

- **Enables:** multi-leg paper execution. Reviewers confirmed the reactor is a **disabled
  scaffold** — it "writes nothing while its flag is unset" (synthesis §"What the critique
  VALIDATED"). The docstring is explicit: "set NOWHERE."
- **Precondition (DO NOT FLIP — there is no live order rail yet):** the ADR-0029 multi-leg
  reactor does not exist as a real execution path. 6/6 reviewers say fidelity-first. **This flag
  stays OFF until the reactor is actually built and evaluated.**
- **Enable (operator):** *Do not enable.* The one-liner is intentionally omitted — there is
  nowhere for a multi-leg fire to route.
- **Side-by-side audit:** n/a (no execution path).
- **Rollback:** n/a (never on).
- **Classification: GATED.**

### 2.9 `CATALYST_ONBOARDING` — GATED

- **Enables:** catalyst-driven universe onboarding — admits a symbol into the tradeable universe
  off a high-confidence catalyst packet (`catalyst/onboarding.py:76`).
- **Precondition (DO NOT FLIP):** **AND-gated** on `HERMES_QUANT_SEMANTIC_ENABLED=1` (it returns
  `[]` if either flag is off; `onboarding.py:76-78`). The ADR-0075 promotion path is **not built**.
  Onboarding changes universe membership, so it must wait on the catalyst eval + ADR-0075.
- **Enable (operator, ONLY after ADR-0075 + eval):**
  ```bash
  echo 'HERMES_QUANT_CATALYST_ONBOARDING=1' >> ~/.hermes/.env
  ```
  (`SEMANTIC_ENABLED=1` is already set in `.env`.)
- **Side-by-side audit (after ADR-0075):** run `quant-watchlist-evolve.py` with the flag OFF vs
  ON; the only delta should be *additional* universe entries tagged `admitted_via=catalyst`,
  each having passed the `tradeable()` gate. Confirm none widen exposure on a name that wouldn't
  otherwise be eligible.
- **Rollback:** `sed -i '/HERMES_QUANT_CATALYST_ONBOARDING=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.10 `WEEKLY_RETRO` — GATED (W2 / ADR-0081, advisory-plane only)

- **Enables:** the weekly CVRF pattern-mining retro (`memory/weekly_retro.py`). The
  `quant-weekly-retro` cron distills winners-vs-losers (split by realized **alpha**, not raw
  P&L) from `reflections.jsonl` into a bounded, decaying, Oracle-tagged set of verbal
  belief-deltas in `beliefs.jsonl`, and — on a successful under-budget pass — emits the
  `weekly_retro_promotion_readiness` producer that un-blocks the gate field at
  `governance/promotion.py:158` (closes O3). Under the flag, the PM `lessons_block` is
  prepended with the role-selective beliefs digest.
- **Advisory-plane only / propose-only:** writes ONLY `beliefs.jsonl` + a `promotion_event`
  audit row. It NEVER imports or mutates the risk gate, the hard limits, the discrete sizing
  ladder `{0,±0.05,±0.10,±0.15,±0.20}`, or the kill-switch (regression-guarded by
  `tests/memory/test_weekly_retro_eval_gate.py::test_propose_only_never_touches_gate_or_ladder`).
- **Precondition (the eval gate, necessary-not-sufficient):** the W2 eval gate must be green —
  `pytest tests/memory/test_weekly_retro.py tests/memory/test_weekly_retro_eval_gate.py
  tests/governance/test_promotion.py -q`. The held-out OOS digest must not regress
  hit-rate/alpha vs the no-digest baseline; belief count under cap; half-life plateau-stable
  under ±20% jitter. Operator promotion stays the sole path to live (ADR-0052).
- **Enable (operator, after the eval gate + an audit-log diff):**
  ```bash
  echo 'HERMES_QUANT_WEEKLY_RETRO=1' >> ~/.hermes/.env
  echo 'HERMES_QUANT_MEMORY_INJECT=1' >> ~/.hermes/.env   # required for the digest to reach the PM prompt
  ```
- **Off-state:** flag unset/`0` is a bit-for-bit no-op — no `beliefs.jsonl` read, no digest,
  the cron exits 0 with empty stdout. (`test_weekly_retro_injection.py::test_flag_off_is_byte_identical_noop`.)
- **Rollback:** `sed -i '/HERMES_QUANT_WEEKLY_RETRO=/d' ~/.hermes/.env`
- **Classification: GATED.**

### 2.11 `MONTHLY_META_RETRO` — GATED (W3 / ADR-0080 / ADR-0081 §3, advisory-plane only)

- **Enables:** the monthly meta-retro (the T3 tier, `memory/meta_retro.py`). The
  `quant-monthly-meta-retro` cron aggregates the trailing W2 weekly belief digests
  (`beliefs.jsonl`), the `research_debate` audit rows (O7, write-only today —
  `agents/research_debate/stage.py:345`), and `promotion_event` records into three advisory
  artifacts: (1) a meta-retro **report** (`memory/meta_retros.jsonl`, recommendations-only),
  (2) novelty/dedup-gated **candidate** hypotheses registered `status="open"`,
  `author="quant-monthly-meta-retro"` (closes O8 — a human/`HypothesisRunner` W6 must move
  them `open→running`), and (3) **persona-calibration telemetry** carried inside the report
  (`telemetry_only=true`). It also applies the deterministic weekly→monthly belief
  promote/expire (ADR-0081 §4).
- **Advisory-plane only / propose-only:** writes ONLY `meta_retros.jsonl`, `beliefs.jsonl`,
  and `status="open"` rows in the hypothesis registry. The proposed persona weights are
  **TELEMETRY-ONLY** — `aggregators/deliberative.py` and `.../bma.py` never read
  `persona_calibration` (grep-guarded). It NEVER imports or mutates the risk gate, the hard
  limits, the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`, the kill-switch, or the
  seed catalyst YAML. **Zero auto-promotion to live** — candidate→live still requires W6 +
  `PromotionOrchestrator` + operator sign-off (ADR-0052).
- **Precondition (the W3 eval gate — necessary-not-sufficient):** run the hard gate and
  confirm `GATE: ✅ PASS`:
  ```bash
  python ops/scripts/quant-monthly-meta-retro-eval-gate.py
  pytest tests/memory/test_meta_retro.py tests/research/test_hypothesis_novelty.py \
         tests/unit/test_monthly_meta_retro_offstate.py -q
  ```
  The four conditions: (1) reproduces byte-identically given `config_hash`; (2) every
  candidate passes the novelty/dedup gate; (3) persona deltas are telemetry-only and
  `|delta| ≤ 0.10` with no aggregator reading them; (4) Oracle provenance preserved + the
  debate-row `asof<asof` guard + byte-identical off-state + nothing-live-mutated.
- **Enable (operator, after the eval gate + an audit-log diff):**
  ```bash
  echo 'HERMES_QUANT_MONTHLY_META_RETRO=1' >> ~/.hermes/.env
  ```
- **Off-state:** flag unset/`0` is a bit-for-bit no-op — the cron returns 0 with empty stdout,
  no `meta_retros.jsonl` write, no candidate registered.
  (`test_monthly_meta_retro_offstate.py::test_offstate_is_noop`.)
- **Rollback:** `sed -i '/HERMES_QUANT_MONTHLY_META_RETRO=/d' ~/.hermes/.env`
- **Classification: GATED.**

---

## 3. Quick reference

| Flag | Class | Enable when | Probe |
|---|---|---|---|
| `DIRECTION_BIAS_GATE` | **SAFE-NOW** | today, after 1 dry-run | §0 dry-tick; expect `DIRECTION_BIAS_MISMATCH` only |
| `IC_DEDUP_AT_INGEST` | **SAFE-NOW** | today | factor-registration; rejected dup adds no JSONL line |
| `CALIBRATOR_AUTO_REFIT` | **SAFE-NOW** | today, after drift-log review | drift cron OFF vs ON diff; sub-threshold = no-op |
| `ADMISSIBILITY` | GATED | after #11 + live eval | §0; expect REJECT-only |
| `BORROW_COST` | GATED | after #11 | EOD P&L carry-only delta |
| `OPTIONS_GATE` | GATED | after #11 + reactor | options-fixture suite |
| `OPTIONS_LIVE_CHAIN` | GATED | after #11 + data eval | live vs fixture chain |
| `MULTILEG_REACTOR` | GATED (stays OFF) | **not yet — no order rail** | n/a |
| `CATALYST_ONBOARDING` | GATED | after ADR-0075 (+ SEMANTIC already on) | watchlist-evolve OFF vs ON |
| `WEEKLY_RETRO` (+`MEMORY_INJECT`) | GATED | after the W2 eval gate (held-out + plateau) | weekly-retro test suite; flag-OFF = byte-identical no-op |
| `MONTHLY_META_RETRO` | GATED | after the W3 eval gate (4 conditions) | `quant-monthly-meta-retro-eval-gate.py` → `GATE: ✅ PASS`; flag-OFF = byte-identical no-op |

**Reminder:** the agent cannot flip any of these. Every `echo ... >> ~/.hermes/.env` is an
operator action on a tool-guarded file. The agent runs the probes and reports; the human flips.

---

## 4. Cross-references

- [`ROLLOUT.md`](ROLLOUT.md) — the four v0.2 LLM-surface flags (`REGIME_HMM`,
  `REFLECTOR_LLM`, `RISK_COMMITTEE_LLM`, `TRADER_LLM`), their dwell/KPI/kill-switch playbook.
- [`docs/research/2026-05-30-r-cron-and-flag-inventory.md`](../research/2026-05-30-r-cron-and-flag-inventory.md)
  — the full 45-flag inventory and the SAFE-NOW vs GATED split this runbook implements.
- [`docs/reviews/2026-05-30-codex-deep-work-loop/synthesis.md`](../reviews/2026-05-30-codex-deep-work-loop/synthesis.md)
  — why each GATED flag is gated; the 10 convergent findings folded into task #11.
- Kill-switch for any flag that turns out wrong: `hermes quant halt '*' --reason "..."`
  (ROLLOUT.md §5) — halts at the gate, downstream of every flag.
