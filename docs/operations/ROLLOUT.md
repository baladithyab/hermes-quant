# ROLLOUT: Production Paper-Trading Activation Playbook (v0.2 LLM Surfaces)

**Audience:** Operator (Codeseys) — single-human production paper-trading rollout.
**Scope:** Activating the four feature-flagged LLM surfaces (TraderNodeLLM, RiskCommittee v0.2,
Reflector v0.2, HMM regime classifier) one at a time, with documented verification, rollback,
and kill-switch procedures.
**Status:** Canonical. Any deviation MUST be recorded as an ADR amendment.
**Related ADRs:** ADR-0031, ADR-0041, ADR-0042, ADR-0054, ADR-0056, ADR-0057, ADR-0058,
ADR-0062 (this playbook).

> **Silence-by-default contract (ADR-0031):** Every LLM surface in this playbook is wrapped in
> a fallback path. If the LLM call fails, times out, or violates schema, the surface silently
> reverts to its v0.1 deterministic behaviour. **You can therefore activate flags safely** —
> the worst case is "no LLM enrichment," not "system crash." This playbook exists so you can
> see when fallbacks fire and decide whether to leave a flag on.

---

## 0. Pre-Flight Checklist

Run this section in order. **Do not advance to §1 until every box is ticked.**

- [ ] **Event stores writable.** Confirm the four append-only stores exist and are writable
  by the current user:
  ```bash
  ls -ld ~/.hermes/quant/governance/ \
         ~/.hermes/quant/memory/     \
         ~/.hermes/quant/research/   \
         ~/.hermes/quant/factors/
  test -w ~/.hermes/quant/governance/audit_log.jsonl || echo "WARN: audit log not writable"
  ```
- [ ] **state.db permissions.** The halt-state SQLite database must be owned by the current
  user with mode `0600`:
  ```bash
  stat -c '%U %a' ~/.hermes/quant/state.db
  # Expect: <your-user> 600
  ```
  If wrong: `chmod 600 ~/.hermes/quant/state.db`.
- [ ] **LLM provider configured.** Either set `OPENROUTER_API_KEY` (or whichever provider
  Hermes is configured for) **or** confirm a Hermes provider config is present at
  `~/.hermes/config.yaml`. Do **not** put the key in this repo. To smoke-test the caller in
  isolation:
  ```bash
  python -c "from hermes_quant.agents.llm_caller import LLMCaller; c=LLMCaller(); print('available=', c.available())"
  ```
- [ ] **Silence-by-default verified.** The fallback probe (ADR-0060) is the canonical
  pre-flight check; it intentionally fails the LLMCaller with timeout, rate-limit, server-error,
  malformed-JSON, schema-invalid, and empty modes against every v0.2 surface and confirms each
  one falls back to its deterministic v0.1 path:
  ```bash
  python scripts/quant-fallback-probe.py --surface all --failure-mode all
  # Expect: RESULT: PASS — silence-by-default holds for all surfaces.
  # Exit code 0 = safe; exit code 1 = DO NOT activate v0.2 LLM in production.
  ```
- [ ] **Status report clean.** Run `hermes quant status` (or the equivalent CLI you have
  wired). The `StatusReport.warnings` list MUST be empty. If a `state_reconstruction_failed`
  warning appears, stop and triage before proceeding.
- [ ] **Tests pass at baseline.** `pytest -q --timeout=60` on the current branch must show
  **at least 991 passing** (the v0.4 baseline). If the count drops, do not roll out — the
  test contract is the contract.
- [ ] **No open governance halts.**
  ```bash
  python -c "from hermes_quant.daemon.halt_state import HaltStateSQLite; \
             from hermes_quant.daemon import halt_state as h; \
             s=HaltStateSQLite(db_path=h.DEFAULT_STATE_DB, mirror_path=h.DEFAULT_HALT_JSON_MIRROR); \
             print('active halts:', list(s.active_halts()))"
  # Expect: active halts: []
  ```
- [ ] **Cron jobs healthy.** `hermes cronjob list | grep quant-` should show all `quant-*`
  jobs as enabled, last-run-at recent, and last-status `ok`.
- [ ] **Audit log size reasonable.** `~/.hermes/quant/governance/audit_log.jsonl` should be
  under ~100 MB. Rotation is future work; if larger, archive manually:
  ```bash
  du -h ~/.hermes/quant/governance/audit_log.jsonl
  # If >100M:
  mv ~/.hermes/quant/governance/audit_log.jsonl{,.$(date +%Y%m%d)}.bak
  ```
- [ ] **Branch is clean.** `git status -s` is empty and you are on a known-good commit
  (not in the middle of a merge). The four-flag activation is a **production action**,
  not a debugging activity.

---

## 1. Activation Order — One Flag at a Time

**Rule:** Activate one flag, observe for the stated dwell time, then activate the next.
Do not parallelise. The order is chosen to **start with the lowest blast-radius surface**
(read-only signal) and end with the highest (proposal-shaping). Each flag is a single
environment variable read by the surface at runtime; restart any long-running daemon /
cron worker after toggling.

### Step 1 — `HERMES_QUANT_REGIME_HMM=1` (lowest blast-radius)

The HMM classifier (ADR-0058) is **advisory only**: it labels each bar with a regime and
the BMA aggregator weights downstream votes accordingly. It does not directly approve or
reject any trade.

**Activate:**
```bash
export HERMES_QUANT_REGIME_HMM=1
hermes cronjob restart quant-daemon  # or whichever scheduler runs the loop
```

**Verify (within 1 hour):**
- `~/.hermes/quant/governance/audit_log.jsonl` should show `regime` field on new entries
  with values from `{BULL, BEAR, VOLATILE, UNKNOWN}` (not just `UNKNOWN`).
- Logs should contain the line `regime: HERMES_QUANT_REGIME_HMM=1 — HMMClassifier wired (v0.2)`.
- No `HMMClassifier failed` warnings in logs.

**Dwell:** **24 hours.** During this window, watch for any spike in audit-log warnings or any
regression in approval rate (see §4 KPIs). If anything drifts >10% from the v0.1 baseline,
roll back this step (§3) and stop the rollout.

### Step 2 — `HERMES_QUANT_REFLECTOR_LLM=1` (read-only enrichment)

Reflector v0.2 (ADR-0057) replaces the templated post-trade reflection text with an
LLM-generated structured reflection. It writes only to
`~/.hermes/quant/memory/reflections.jsonl` — it does **not** affect any open or future
proposal directly. The retriever surfaces these reflections as memory hints, but the
gate logic is unchanged.

**Activate:**
```bash
export HERMES_QUANT_REGIME_HMM=1               # keep prior step on
export HERMES_QUANT_REFLECTOR_LLM=1
hermes cronjob restart quant-daemon
```

**Verify (after the next closed trade):**
- `tail -1 ~/.hermes/quant/memory/reflections.jsonl | jq .reflection_text` should show a
  non-templated, non-empty string (more than ~80 chars, contains specifics about the trade).
- No `reflector: LLM call failed, falling back to v0.1` warnings in logs (occasional fallback
  is acceptable — sustained fallback means the LLM provider is misconfigured).

**Dwell:** **24 hours.**

### Step 3 — `HERMES_QUANT_RISK_COMMITTEE_LLM=1` (affects approval, CV5 invariant preserved)

Risk Committee v0.2 (ADR-0056) replaces each persona's deterministic vote with an
LLM-generated structured vote. The 3-of-5 consensus invariant from ADR-0043 is **preserved**:
the gate still requires a quorum, and silence-by-default still rejects on schema violation.
This is the first flag that can change approval outcomes, so the dwell is longer.

**Activate:**
```bash
export HERMES_QUANT_REGIME_HMM=1
export HERMES_QUANT_REFLECTOR_LLM=1
export HERMES_QUANT_RISK_COMMITTEE_LLM=1
hermes cronjob restart quant-daemon
```

**Verify (within 4 hours):**
- `grep risk_committee_v0_2_used ~/.hermes/quant/governance/audit_log.jsonl | tail -5` should
  show entries with `"risk_committee_v0_2_used": true`.
- `approval_rate_24h` (see §4) should remain within **±10%** of the v0.1 baseline. A bigger
  swing in either direction means the LLM is materially shifting decisions — investigate
  before continuing.
- No `RiskCommittee: HERMES_QUANT_RISK_COMMITTEE_LLM=1 but ...` warnings in logs.

**Dwell:** **48 hours.** Approval-rate stability needs a longer window than upstream signal
checks because committee outputs are conditioned on rarer market regimes.

### Step 4 — `HERMES_QUANT_TRADER_LLM=1` (proposal text, P&L math unchanged)

TraderNodeLLM (ADR-0054) replaces the templated proposal-text path with an LLM-derived one.
The deterministic helpers (stop-loss, target-price, alpha-return) are **always recomputed
deterministically** — the LLM produces narrative text and structured fields, but P&L math
does not depend on the LLM's numeric outputs. This is the highest-visibility surface but
not the highest-risk for portfolio outcomes.

**Activate:**
```bash
export HERMES_QUANT_REGIME_HMM=1
export HERMES_QUANT_REFLECTOR_LLM=1
export HERMES_QUANT_RISK_COMMITTEE_LLM=1
export HERMES_QUANT_TRADER_LLM=1
hermes cronjob restart quant-daemon
```

**Verify (after the next proposal is generated):**
- Latest entry in `~/.hermes/quant/proposals.jsonl` should include a non-templated rationale
  field and structured `stop_loss` / `target_price` keys.
- Compute `alpha_return_predicted` from the proposal and compare to the deterministic
  helper — they should match exactly (the LLM's number is overridden if it disagrees).
- No `trader: LLM path failed, falling back to v0.1` sustained warnings.

**Dwell:** **7 days minimum.** Do not declare this surface "production-stable" until you
have observed at least one full trading week of LLM-shaped proposals with no sustained
fallback events and no anomalous P&L attribution.

---

## 2. Smoke-Test Sequence

Run **before** activating each flag. The smoke test is the gate between "I think this is
safe" and "I have evidence this is safe."

### 2.0 Repository sanity (run once, before §1 Step 1)

```bash
cd /mnt/e/CS/github/hermes-quant
source venv/bin/activate
git rev-parse --abbrev-ref HEAD            # confirm correct branch
git status -s                              # confirm clean tree
pytest -q --timeout=60                     # confirm baseline (>=991 passing)
pytest -q tests/docs/                      # confirm this playbook is consistent
```

### 2.1 Pre-Step-1 (HMM regime)

```bash
pytest -q tests/regime/test_hmm_classifier.py
HERMES_QUANT_REGIME_HMM=1 python -c "
from hermes_quant.regime.detector import RegimeDetector
d = RegimeDetector()
print('hmm wired:', d.status())
"
```
Expect: HMM status reports `hmm_wired=True`, `model_loaded=True`.

### 2.2 Pre-Step-2 (Reflector)

```bash
pytest -q tests/memory/test_reflector.py
HERMES_QUANT_REFLECTOR_LLM=1 python -c "
import os
print('reflector LLM enabled:', os.environ.get('HERMES_QUANT_REFLECTOR_LLM') == '1')
from hermes_quant.memory.reflector import Reflector
r = Reflector()
print('reflector instantiated OK:', type(r).__name__)
"
```

### 2.3 Pre-Step-3 (Risk Committee)

```bash
pytest -q tests/agents/risk_committee/
HERMES_QUANT_RISK_COMMITTEE_LLM=1 python -c "
import os
from hermes_quant.agents.risk_committee.committee import RiskCommittee, _LLM_FLAG_ENV_VAR
print('committee llm flag set:', os.environ.get(_LLM_FLAG_ENV_VAR) == '1')
print('committee instantiable:', type(RiskCommittee()).__name__)
"
```

### 2.4 Pre-Step-4 (Trader)

```bash
pytest -q tests/agents/test_trader.py
HERMES_QUANT_TRADER_LLM=1 python -c "
from hermes_quant.agents.trader import _trader_llm_enabled
print('trader llm enabled:', _trader_llm_enabled())
"
```

If any smoke test fails, **do not advance**. Fix or revert before proceeding.

---

## 3. Rollback Procedure

Rollback for every surface is **the same shape**: unset the env var, restart the cron worker,
verify the next event used the v0.1 path. There is no state to migrate; the v0.1 path
reads/writes the same event stores.

| Surface              | Unset                                                   | Verify after restart                                                                              |
|----------------------|---------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| TraderNodeLLM        | `unset HERMES_QUANT_TRADER_LLM`                         | Next proposal in `proposals.jsonl` lacks LLM-only fields; rationale is templated.                 |
| Risk Committee v0.2  | `unset HERMES_QUANT_RISK_COMMITTEE_LLM`                 | Next audit-log entry shows `risk_committee_v0_2_used=false` (or absent).                          |
| Reflector v0.2       | `unset HERMES_QUANT_REFLECTOR_LLM`                      | Next reflection has the deterministic templated `reflection_text` shape.                          |
| HMM regime           | `unset HERMES_QUANT_REGIME_HMM`                         | Next audit-log entries use the v0.1 rule-based regime; logs report rule-based detector.           |

**General rollback shape:**
```bash
unset HERMES_QUANT_<SURFACE>
hermes cronjob restart quant-daemon
# wait for next loop iteration, then:
tail -5 ~/.hermes/quant/governance/audit_log.jsonl
```

**Full rollback (all four flags):**
```bash
unset HERMES_QUANT_TRADER_LLM HERMES_QUANT_RISK_COMMITTEE_LLM \
      HERMES_QUANT_REFLECTOR_LLM HERMES_QUANT_REGIME_HMM
hermes cronjob restart quant-daemon
```

The system is now back in 100% v0.1 deterministic mode with no LLM calls anywhere in the loop.

---

## 4. Monitoring KPIs

Track these continuously during rollout. Each KPI has a clear "investigate" threshold; a
threshold breach means "open the audit log and look," not "panic."

1. **`approval_rate_24h` vs baseline.** Rolling 24-hour proposal-approval ratio. Investigate
   if it shifts more than ±10% from the pre-rollout baseline.
2. **`rejection_reason_top3` (drift detection).** Top three rejection reasons over the last
   24h. A new reason appearing in the top three, or a reason climbing more than 2 places,
   is a drift signal.
3. **`alpha_return_realized` vs `alpha_return_predicted`.** Per-trade comparison between the
   proposal's predicted alpha and the realised alpha at close. The reflector consumes both;
   investigate sustained divergence (>2σ over 10 trades).
4. **`factor_verdict_count` by tier.** Count of factor verdicts at `premium`, `accepted`,
   `rejected` tiers per day (from `~/.hermes/quant/factors/factor_verdicts.jsonl`).
   A sudden flood of either `premium` or `rejected` is a drift signal.
5. **`state_reconstruction_failed` count.** Number of times the daemon could not reconstruct
   state from the SQLite mirror in the last 24h. **Should be zero.** Any non-zero count is
   an immediate kill-switch trigger.
6. **`fallback_event_count` per surface.** Count of times each surface fell back from v0.2 to
   v0.1 (look for `falling back to v0.1` log lines per surface). Sustained non-zero is a
   provider/config issue.
7. **`llm_latency_p95`.** 95th-percentile latency of LLM calls per surface. A doubling from
   the smoke-test baseline is a signal that provider performance is degrading.

---

## 5. Kill-Switch

The kill-switch is **the halt mechanism** documented in ADR-0031. It works at the gate level,
which is downstream of every LLM surface — silence-by-default fallback already protects
against schema-level failures, but the halt protects against logical / strategic failures
(e.g. "the LLM has been approving garbage for the last hour and I want everything stopped").

**Halt all gates immediately:**
```bash
hermes quant halt '*' --reason "manual halt: <one-line reason>"
# or, equivalently with the underlying CLI:
python -m hermes_quant.cli.halts halt '*' --reason "manual halt: <one-line reason>"
```

The halt is checked by `gate.py` before any approval. Once installed, **no proposal is
approved** regardless of LLM output, until you resume. The halt is durable in SQLite
(`~/.hermes/quant/state.db`) and survives daemon restarts.

**Resume after the underlying issue is resolved:**
```bash
hermes quant resume '*' --reason "<why are you resuming?>"
```

**Emergency stop (halt + bus signal + intent-to-cancel):**
```bash
hermes quant emergency-stop
```

The emergency-stop ordering (per ADR-0009 §P0-4 and synthesis-v2 §P0-D):
1. Insert durable halt FIRST so even if the next daemon tick races with broker cancel, the
   halt is committed and entries cannot resume on their own.
2. Update `halt_state.json` mirror atomically.
3. Emit halt signal to the bus (consumers flatten on next read).
4. Broker cancel — currently intent-only; manual force-exit required in the freqtrade UI
   until ccxt/alpaca consumers gain auto-cancel.

**Silence-by-default IS the kill-switch for schema-level failures.** A malformed LLM
response triggers the v0.1 fallback automatically without operator action. The manual halt
is for cases where the LLM is structurally well-formed but operationally wrong.

---

## 6. Cross-References

- **ADR-0031 — Governance plane consolidation.** Defines the silence-by-default contract
  and the gate that every surface in this playbook is checked against.
- **ADR-0041 — Signal provenance & audit-trail observability.** Defines `audit_log.jsonl`,
  the primary observability surface used in §4.
- **ADR-0042 — Persistent memory & deferred reflection layer.** Defines the reflection
  schema and the memory store that Reflector v0.2 writes to.
- **ADR-0054 — LLM-Caller foundation & TraderNode v0.2.** Activated by §1 Step 4.
- **ADR-0056 — RiskCommittee v0.2 LLM wiring.** Activated by §1 Step 3.
- **ADR-0057 — Reflector v0.2 LLM-wired structured reflection.** Activated by §1 Step 2.
- **ADR-0058 — HMM Regime Classifier v0.2.** Activated by §1 Step 1.
- **ADR-0062 — This playbook (rollout playbook architectural decision).**

---

## 7. Append-Only Event Stores Reference Table

Every store in this table is **append-only** by contract. Operators must never edit or
truncate these files except via the documented rotation procedure (which does not yet
exist — manual archive only, see §0).

| Store                                       | Path                                               | Producer(s)                                        | Schema source        | Retention            |
|---------------------------------------------|----------------------------------------------------|----------------------------------------------------|----------------------|----------------------|
| Audit log                                   | `~/.hermes/quant/governance/audit_log.jsonl`       | `gate.py`, `react/paper.py`, daemon loop           | ADR-0031 / ADR-0041  | Forever (no rotation)|
| Approval tokens                             | `~/.hermes/quant/governance/approval_tokens.jsonl` | `governance/approvals.py`                          | ADR-0031             | Forever              |
| Decisions log                               | `~/.hermes/quant/memory/decisions.jsonl`           | `memory/decisions.py`                              | ADR-0042             | Forever              |
| Reflections                                 | `~/.hermes/quant/memory/reflections.jsonl`         | `memory/reflector.py`                              | ADR-0042 / ADR-0057  | Forever              |
| Promotion decisions                         | `~/.hermes/quant/research/promotion_decisions.jsonl` | `eval/promotion_orchestrator.py`                | ADR-0052             | Forever              |
| Hypotheses                                  | `~/.hermes/quant/research/hypotheses.jsonl`        | `research/hypothesis.py`                           | ADR-0048             | Forever              |
| Run cards                                   | `~/.hermes/quant/research/run_cards.jsonl`         | `research/run_card.py`                             | ADR-0034 / ADR-0048  | Forever              |
| Factor verdicts                             | `~/.hermes/quant/factors/factor_verdicts.jsonl`    | `factors/factor_oracle.py`                         | ADR-0055             | Forever              |
| Alpha zoo                                   | `~/.hermes/quant/factors/alpha_zoo.jsonl`          | `factors/alpha_zoo.py`                             | ADR-0050             | Forever              |
| Watchlist journal                           | `~/.hermes/quant/watchlist/journal.jsonl`          | `playbook/watchlist_evolution.py`                  | ADR-0035             | Forever              |
| Proposals                                   | `~/.hermes/quant/proposals.jsonl`                  | `proposals.py` (TraderNode / TraderNodeLLM)        | ADR-0044 / ADR-0054  | Forever              |
| Executions                                  | `~/.hermes/quant/executions.jsonl`                 | `react/paper.py`                                   | ADR-0010             | Forever              |
| Signal bus                                  | `~/.hermes/quant/signals.jsonl`                    | `daemon/signal_bus.py`                             | ADR-0008 / ADR-0017  | Daemon-managed       |
| Halt state (durable)                        | `~/.hermes/quant/state.db` (`halts` table)         | `daemon/halt_state.py`                             | ADR-0031 / ADR-0009  | Forever              |
| Halt state (JSON mirror)                    | `~/.hermes/quant/halt_state.json`                  | `daemon/halt_state.py`                             | ADR-0031             | Atomic mirror        |

**Why append-only?** Every approve/reject/halt/resume must be reconstructable from event
history alone. Mutating a past event would invalidate the audit chain that ADR-0041 depends
on. If you need to "undo" something, write a compensating event — never edit the store.
