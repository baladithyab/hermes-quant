# SELFEVOLVE-ENABLEMENT — turning on the self-evolving researcher (operator runbook)

**Status:** authoritative enablement order for the self-evolution waves W1–W7
**Date:** 2026-05-31
**Grounds:** [ADR-0080](../adr/ADR-0080-self-evolution-framework.md) (advisory-plane framework + tier table),
[ADR-0081](../adr/ADR-0081-belief-store-and-distillation-tiers.md), the capability map
(`docs/research/2026-05-30-selfevolve-capability-map.md`), and the per-wave plans `docs/plans/selfevolve-W*.md`.

> **The one invariant.** Every flag below turns on an **advisory-plane** component: it writes beliefs /
> hypotheses / candidate-weights / candidate-edges / telemetry. **None of them can touch the deterministic
> risk gate, the hard risk limits, the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`, or the
> kill-switch** — those sit outside the loop and are immutable by it (ADR-0080 §5; test-asserted per wave).
> The only path from an evolved idea to live policy runs through the outer standard-of-truth
> (deterministic OOS backtest + promotion gate + **operator sign-off, which never automates**).
> So: enabling these makes the system *propose* better; a human still ships every policy change.

> **The agent cannot do any of the steps below.** `.env` writes are tool-guarded, cron registration is the
> Hermes `cronjob` mechanism, and `config.yaml` is the gateway's. These are the exact operator commands.
> Every flag is default-OFF and its off-state is byte-identical to today (verified per wave), so enabling is
> strictly additive and each flag is independently reversible (`sed -i '/HERMES_QUANT_X=1/d' ~/.hermes/.env`).

> **⚠️ SCHEDULE PRECEDENCE (2026-06-14, seed `9048`): `CRON-REGISTRY.md` wins for cron schedules + deliver.**
> The `cronjob action='create'` blocks in this doc and the CRON-REGISTRY row table disagree on the W2/W3/W6
> schedules (and W6 deliver). Neither cron is registered on the host yet, so there is no live value to
> adjudicate from — and `CRON-REGISTRY.md` is the declared single source of truth for every trading cron.
> **Register using the CRON-REGISTRY schedule, then make this doc match.** Known deltas to reconcile at
> registration time (CRON-REGISTRY row → this doc's create block):
>
> | Cron | CRON-REGISTRY (authoritative) | This doc (stale) |
> |---|---|---|
> | W2 `quant-weekly-retro` (row 19) | `30 13 * * 0` (Sun 16:30 ET), `deliver=local` | `0 6 * * 6` (Sat 06:00 PT) |
> | W3 `quant-monthly-meta-retro` (row 20) | `0 14 1 * *` (1st 09:00 ET) | `0 6 1 * *` (1st 06:00 PT) |
> | W6 `quant-research-loop` (row 21) | `0 8 * * 1` (Mon 11:00 ET), `deliver=discord:#hq` | `0 7 * * 6` (Sat), `deliver=local` |
>
> The operator confirms the intended cadence at registration; until then the CRON-REGISTRY value is canonical.
> (W4 `quant-factor-weight-propose` and the graph-mine cron `0 6 * * 0` are consistent across both docs.)

---

## Dependency graph (enable in this order)

```
W1 (REFLECTION) ──┬─► W2 (WEEKLY_RETRO) ──► W3 (MONTHLY_META_RETRO) ──► W6 (RESEARCH_LOOP)
                  ├─► W4 (FACTOR_WEIGHT_PROPOSER)        [parallel after W1]
                  ├─► W5 (GRAPH_MINING)                  [parallel after W1]
                  └─► W7 (REDTEAM_TURN)                  [parallel after W1]
```

W1 is the keystone — it produces the `decisions.jsonl` corpus everything else learns from. W2→W3→W6 is the
distillation→meta→research spine. W4/W5/W7 are independent once W1 is live. Flip one, observe ≥1 cadence
period (a trade for W1; a week for W2/W4/W5; a month for W3/W6), confirm the eval gate, then the next.

---

## Per-flag runbook

For each: what it enables, the **precondition/eval gate** that must pass first, the exact `.env` one-liner,
the cron (if any) to register, and the rollback.

### W1 — `HERMES_QUANT_REFLECTION` (+ `HERMES_QUANT_MEMORY_INJECT`) — the keystone
- **Enables:** per-trade reflection loop — a `pending` decision is recorded on every opening paper fill
  (`08326e1`), resolved + reflected-on at close; the lesson (Oracle-guarded) reaches the PM prompt under
  `MEMORY_INJECT`. This is the source-water for W2/W3.
- **Eval gate:** loop-liveness — `tests/memory/test_w1_decision_loop_liveness.py` green (open→close→reflect→
  readback yields a non-empty lessons block; the `tau_observable < asof` Oracle guard still excludes future
  reflections). No alpha claim — W1 is plumbing.
- **Enable:**
  ```bash
  echo 'HERMES_QUANT_REFLECTION=1'   >> ~/.hermes/.env
  echo 'HERMES_QUANT_MEMORY_INJECT=1' >> ~/.hermes/.env   # for raw per-trade lessons to reach the PM prompt
  ```
- **Cron:** none (fires inline in the reactor). **Rollback:** delete the two lines; loop goes dark, no data loss.

### W2 — `HERMES_QUANT_WEEKLY_RETRO` — weekly distillation (T2)
- **Enables:** weekly pattern-mining — distills the reflection corpus (winners/losers by realized **alpha**)
  into ≤N decaying, Oracle-tagged belief-deltas (`beliefs.jsonl`) that prepend to the PM prompt's lessons
  block; also writes `weekly_retro_promotion_readiness` (closes the dangling promotion gate, O3).
- **Eval gate (held-out):** `tests/memory/test_weekly_retro_eval_gate.py` green — the digest-injected prompt
  does NOT regress hit-rate/alpha vs the no-digest baseline on an OOS window the distiller never read
  (checkpoint-fallback: regress → keep OFF); belief count ≤ budget; half-life plateau-stable (not the peak).
- **Enable:** `echo 'HERMES_QUANT_WEEKLY_RETRO=1' >> ~/.hermes/.env`  *(needs W1's corpus first)*
- **Cron (register after deploying the script to `~/.hermes/scripts/`):**
  ```
  cronjob action='create' name='quant-weekly-retro' schedule='0 6 * * 6' script='quant-weekly-retro.py' no_agent=true deliver='local'
  ```
  (Sat 06:00 PT; no_agent change-detecting — silent unless beliefs change.)
- **Rollback:** delete the `.env` line + `cronjob action='delete' name='quant-weekly-retro'`.

### W3 — `HERMES_QUANT_MONTHLY_META_RETRO` — monthly meta-retro (T3, the missing tier)
- **Enables:** monthly aggregation of weekly digests + debate/risk-committee audit rows + promotion records
  → repeating lesson-categories, persona-calibration **telemetry-only** (nothing reads it yet), and
  novelty/dedup-gated **candidate hypotheses** for W6. Recommendations-only.
- **Eval gate:** `quant-monthly-meta-retro-eval-gate.py` PASS (4/4) — reproduces (config_hash); candidates
  pass novelty/dedup; persona deltas telemetry-only (`|delta|≤0.10`, no consumer); Oracle provenance + debate
  `asof<asof` guard + byte-identical off-state.
- **Enable:** `echo 'HERMES_QUANT_MONTHLY_META_RETRO=1' >> ~/.hermes/.env`  *(needs W2)*
- **Cron:**
  ```
  cronjob action='create' name='quant-monthly-meta-retro' schedule='0 6 1 * *' script='quant-monthly-meta-retro.py' no_agent=true deliver='local'
  ```
  (1st of month 06:00 PT.)
- **Rollback:** `.env` line + `cronjob action='delete'`.

### W6 — `HERMES_QUANT_RESEARCH_LOOP` — hypothesis→backtest→promote driving cron
- **Enables:** drives `HypothesisRunner → FactorOracle → PromotionOrchestrator` on W3's candidate hypotheses.
  **Produces review-only `PromotionRecord`s — ZERO auto-promotion to live** (independently traced). The
  committee is the inner cheap judge; the deterministic OOS backtest + promotion gate is the outer truth;
  the operator promotes.
- **Eval gate (7/7):** reproducible Run-Cards (strategy_config_hash); lookahead sentinel load-bearing (a
  contaminated candidate is falsified and never reaches the gate); ZERO auto-promotion (a validated+promote
  candidate yields only a review-only record, no live transition, no flag flipped); byte-identical off-state;
  external-truth advancement; bounded per cycle; halt fail-closed.
- **Enable:** `echo 'HERMES_QUANT_RESEARCH_LOOP=1' >> ~/.hermes/.env`  *(needs W3)*
- **Cron:**
  ```
  cronjob action='create' name='quant-research-loop' schedule='0 7 * * 6' script='quant-research-loop.py' no_agent=true deliver='local'
  ```
- **Rollback:** `.env` line + `cronjob action='delete'`. Promotion to live is always a separate operator act.

### W4 — `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER` — factor-verdict → BMA-weight proposer (parallel)
- **Enables:** a weekly run of `FactorOracle.evaluate_all` emitting a **candidate** weight diff (premium↑
  within a cap, rejected→silence-toward-0) to `weight-candidates.json` — proposal-only, operator applies.
- **Eval gate:** the proposed weight set must STRICTLY beat prior-best on a **time-ordered held-out OOS**
  WalkForward (the proposer is blind to the holdout — `tests/unit/test_factor_weight_holdout_lookahead.py`),
  plateau-selected (not the in-sample peak), checkpoint-fallback, rejected→buffer.
- **Enable:** `echo 'HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1' >> ~/.hermes/.env`  *(needs W1)*
- **Cron:**
  ```
  cronjob action='create' name='quant-factor-weight-propose' schedule='30 6 * * 6' script='quant-factor-weight-propose.py' no_agent=true deliver='local'
  ```
- **Note:** applying a candidate weight to the live BMA is a SEPARATE operator step (and BMA Beta-posterior
  auto-learning, O6, stays blocked until v0.1.2 entry+exit fill-joining lifts the `settlement_loop`
  `slippage_only` gate). **Rollback:** `.env` line + `cronjob action='delete'`.

### W5 — `HERMES_QUANT_GRAPH_MINING` — catalyst learned-graph miner (parallel)
- **Enables:** `mine_graph()` joins `propagation-log.jsonl` vs forward returns and proposes per-edge
  FLIP/DOWNWEIGHT/PRUNE to `graph-mine-candidates.json`. **Never auto-edits the seed YAML**; silence-only
  `confidence_multiplier` clamped `0..1`.
- **Eval gate:** edge proposals scored on held-out forward returns (`MIN_SAMPLE=20`/`MIN_HIT_RATE=0.6`, same
  bar as the proven `profitability.py`); a FLIP must additionally pass `eval.run_sign_consistency`.
- **Enable:** `echo 'HERMES_QUANT_GRAPH_MINING=1' >> ~/.hermes/.env`  *(needs W1 + corpus volume)*
- **Cron:**
  ```
  cronjob action='create' name='quant-catalyst-graph-mine' schedule='0 6 * * 6' script='quant-catalyst-graph-mine.py' no_agent=true deliver='local'
  ```
- **Rollback:** `.env` line + `cronjob action='delete'`; seed-YAML edits stay manual regardless.

### W7 — `HERMES_QUANT_REDTEAM_TURN` — Socratic devil's-advocate (parallel)
- **Enables:** a standing red-team turn in the research debate that attacks the *reasoning* of the leading
  view (distinct from the bear's *position*), fills the ADR-0002 `counterarguments` field, and surfaces
  dissent instead of collapsing to consensus. Aggregation stays deterministic (no vote-counting).
- **Eval gate (3/3):** in shadow, the red-team turn changes the dissent-surfaced rate WITHOUT inflating the
  false-flat rate; the judge recommendation/confidence is bit-identical ON vs OFF (no vote-counting).
- **Enable:** `echo 'HERMES_QUANT_REDTEAM_TURN=1' >> ~/.hermes/.env`  *(needs W1; pairs with `RESEARCH_DEBATE=1`)*
- **Cron:** none (fires inline in the debate when `RESEARCH_DEBATE` is on). **Rollback:** delete the line.

---

## Deploy-sync prerequisite (shared with the trading crons)

The W2–W6 crons run the **deployed** `~/.hermes/scripts/` copies. Deploy the new scripts there before
registering (they are all `REPO_ONLY_NEW`, safe to copy — nothing to clobber):
```bash
cp ops/scripts/quant-{weekly-retro,monthly-meta-retro,research-loop,factor-weight-propose,catalyst-graph-mine}.py ~/.hermes/scripts/
python ops/deploy/quant-deploy-audit.py   # confirm they show SAME, not DRIFT
```
See `DEPLOY-SYNC.md` for the full reconciliation discipline.

## Pre-flip hardening still owed (tracked, non-blocking)

These do NOT block enabling, but sharpen the components (task list #31):
- W4 cron holdout `≥30` vs `MIN_OBSERVATIONS=30` off-by-one (shift(-1)) — currently fails-safe (never
  promotes a 30-bar boundary), harmless on real ~375-bar SPY holdouts.
- W4 `_composite_position` full-window z-score is an in-window centering (NOT a train→holdout leak; the gate
  is proven proper) — switch to expanding/causal if the holdout DSR is ever read as an absolute Sharpe.
- The `isinstance(row, dict)` silence guard applied to graph_mining + profitability should extend to the
  ~30 other `no_agent` JSONL readers for repo-wide silence-by-default.
