# Hermes-Quant Feature Flags — Inventory & Promotion Plan

> **Authoritative flag SoT = [`docs/operations/FLAG-INVENTORY.md`](operations/FLAG-INVENTORY.md)**
> (GENERATED — `python ops/scripts/quant-flag-inventory.py --write`; a `--check` test gate
> in `tests/ops/test_flag_inventory_drift.py` fails the build on drift). That generated table
> is the single source of truth for *every* `HERMES_QUANT_*` flag READ in `hermes_quant/`
> and its CODE default. **THIS file (FLAGS.md) is a human-curated decision/promotion sheet**
> for the capability subset — it carries the *judgement* (keep / promote / retire / stays-config)
> the generated table cannot. When the two disagree on whether a flag EXISTS or its DEFAULT,
> FLAG-INVENTORY.md wins; fix this sheet, don't fork it.
>
> Scope note: FLAG-INVENTORY.md scans `hermes_quant/` only and lists flags that supply a
> literal/`None` default, so it omits (a) **cron-script-side** flags read in `ops/scripts/*.py`
> — e.g. `HERMES_QUANT_AUTONOMOUS` / `_AUTONOMOUS_ARMED` (read in `quant-hourly-tick.py`); and
> (b) flags read WITHOUT an inline default (`environ.get("X") == "1"` membership/path style)
> — e.g. `PORTFOLIO_CAPS`, `DISSENT_CAP`, `BROKER_BACKEND`, `REGIME_HMM`, `IC_DEDUP_AT_INGEST`,
> the `*_DIR`/`HOME` path flags. Those are still real flags; they just live outside the
> generated table's capture and are tracked here. (Expanding the scanner to capture the
> no-default reads is filed as a follow-up — it is NOT a doc-drift bug.)
>
> Originally generated 2026-06-05 from a ground-truth scan of `os.environ.get(...)` reads +
> flag-constant definitions in `hermes_quant/` (excluding the stale `build/` mirror),
> cross-referenced against what is currently set in `~/.hermes/.env` and exported in
> the `~/.hermes/scripts/quant-*-armed.sh` cron wrappers.
>
> **Why this doc exists:** the flag surface grew past the point of being memorable.
> Not everything should stay a flag forever. This is the decision sheet for which
> flags are load-bearing config, which have earned promotion to always-on defaults,
> and which are dead weight to retire.

## How flags reach the running system (read this first)

There are **two** delivery paths, and they are NOT the same:

1. **`~/.hermes/.env`** — read by the **gateway process** (interactive chat, agent-mode
   crons). A flag here affects anything the gateway runs in-process. Requires a
   `systemctl --user restart hermes-gateway.service` to take effect.
2. **`~/.hermes/scripts/quant-*-armed.sh` `export` lines** — the `no_agent` cron
   subprocesses (the autonomous tick, playbook tick, hourly tick) get a **clean env**
   and only see what the wrapper `export`s. **A flag in `.env` alone does NOT reach
   these cron subprocesses.** The money-path firing loop reads the wrapper exports.

**Consequence:** to flip behavior for the live autonomous trading tick you must edit
the wrapper export, not just `.env`. To flip behavior for chat/agent-mode you edit
`.env`. Several flags need BOTH (e.g. `HERMES_QUANT_DETERMINISTIC_EQUITY`).

Promotion-to-default means: change the code default from `"0"`→`"1"` (or remove the
gate entirely) so neither `.env` nor the wrapper needs the line. That is the end-state
for any flag that has proven itself — a flag that is always set in every environment
is not a flag, it's a default with extra steps.

---

## TIER A — Currently LIVE (set in `.env` and/or wrapper). Validate, then promote.

These are ON in production right now. The job is to confirm each has baked long enough,
then bake it into the code default and delete the flag line.

| Flag | Where set | Gates | Default in code | Promotion call |
|---|---|---|---|---|
| `HERMES_QUANT_SEMANTIC_ENABLED` | .env=1 | Catalyst/semantic analyst in the advisor pool | `"1"` | **PROMOTED** (2026-06: code default flipped to `1` — advisor.py:379/1017, autonomous.py:555, perception/builder.py:176). Off-switch: set `=0`. |
| `HERMES_QUANT_REFLECTION` | .env=1, wrapper | Per-trade reflections → weekly retro | `"1"` | **PROMOTED** (code default `1`; feeds the learning loop). Off-switch: set `=0`. |
| `HERMES_QUANT_MEMORY_INJECT` | .env=1 | Inject memory lessons into LLM committee | `"1"` | **PROMOTED** (code default `1`; with `MEMORY_SPLIT` — see Tier C). Off-switch: set `=0`. |
| `HERMES_QUANT_GRAPH_MINING` | .env=1 | Weekly catalyst-graph mining cron | `"0"` | **KEEP** — cheap, but it's a cron-cadence toggle, not a hot-path default. Low priority either way. |
| `HERMES_QUANT_RESEARCH_LOOP` | .env=1 | Weekly research-loop cron | `"0"` | **KEEP** — same: cron-gated batch job, fine as a flag. |
| `HERMES_QUANT_WEEKLY_RETRO` | .env=1 | Weekly strategy retro LLM pass | `"1"` | **PROMOTED** (ra09 2026-06-15: code default is now `1` per FLAG-INVENTORY.md — `llm_committee.py:350`). Off-switch: set `=0`. |
| `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` | wrapper=v0.2 | Realistic paper fill slippage (`v0.1` passthrough vs `v0.2` model) | `"v0.2"` | **PROMOTED default → `v0.2`** (ra09 2026-06-15: code default is now `v0.2` per FLAG-INVENTORY.md — `deterministic_equity.py:432`; v0.2 is strictly more honest, passthrough understated cost). |
| `HERMES_QUANT_PORTFOLIO_CAPS` | wrapper=1 | The 200/100/20 gross/net/cash clip band-aid | (presence) | **RETIRE-ON-PATH** — superseded by BP enforcement (DeterministicEquityReactor + Alpaca). Keep until the deterministic-equity + Alpaca cutover both prove out, then DELETE. It was always a stand-in for broker BP rejection. |
| `HERMES_QUANT_DETERMINISTIC_EQUITY` | .env=1, wrapper=1 | Route synthetic equity through the BP-enforcing reactor | `"0"` | **NEW (2026-06-05) — bake-then-promote.** Just flipped. After ~1 week of clean ticks, promote default → `1` and retire `PORTFOLIO_CAPS` on this path. |
| `HERMES_QUANT_ALPACA_SHADOW` | .env=1 | Record Alpaca-vs-synthetic fill divergence (record-only) | (presence) | **TRANSIENT** — a proving-window instrument, not a permanent feature. Turn OFF once the Alpaca cutover completes; do not promote. |
| `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER` | .env=1 | Factor-weight proposer | `"0"` | **KEEP for now** — newer; let it accrue evidence before promoting. |
| `HERMES_QUANT_MONTHLY_META_RETRO` | .env=1 | Monthly meta-retro cron | `"0"` | **KEEP** — cron-cadence toggle. |
| `HERMES_QUANT_SOCIAL_INGEST` | .env=1 | Social-signal ingest (cron-script read) | n/a (script) | **KEEP** — data-source toggle; fine as config. |

---

## TIER B — Must STAY a flag (config / safety / cost knobs, not bake-able)

These are NOT "unproven features waiting to graduate." They are genuine configuration —
promoting them to always-on would be wrong. They stay forever, by design.

| Flag | Gates | Why it stays |
|---|---|---|
| `HERMES_QUANT_ALPACA_PAPER` | Route fills to REAL Alpaca paper orders | **Safety switch.** Off = simulator, On = real broker submission. Must remain an explicit, deliberate opt-in. Never a silent default. |
| `HERMES_QUANT_BROKER_BACKEND` | Explicit backend override (`deterministic`/`alpaca`) | Config selector; the whole point is per-env choice. |
| `HERMES_QUANT_MULTILEG_REACTOR` | Enable options/multi-leg execution | **Safety + capability gate** (ADR-0029 D7). Options trading is a deliberate capability opt-in. |
| `HERMES_QUANT_OPTIONS_GATE` / `_OPTIONS_LIVE_CHAIN` | Options risk gate / live chain fetch | Capability + data-source gates; pair with the multileg reactor. |
| `HERMES_QUANT_PAPER_INITIAL_CASH` | Bootstrap paper NAV | Pure config value (a number), not a feature. |
| `HERMES_QUANT_LLM_BUDGET*` (per-tick/decision USD/tokens, dir) | LLM spend ceilings | Cost-control config; environment-specific. |
| `HERMES_QUANT_*_LLM` (`TRADER_LLM`, `REFLECTOR_LLM`, `RISK_COMMITTEE_LLM`) | Use an LLM for that stage vs deterministic | Cost/latency tradeoff knobs — deterministic is the cheap default on purpose. Keep. |
| `HERMES_QUANT_EVIDENCE_DIR` / `_JOURNAL_PATH` / `_ALPHA_ZOO_DIR` / `_LLM_BUDGET_DIR` | Path overrides | Config paths. |
| `HERMES_QUANT_KNOWLEDGE_CUTOFF` | Backtest as-of cutoff (lookahead guard) | **Backtest-honesty knob** — must be settable per run. |
| `HERMES_QUANT_PIT_UNIVERSE` | Point-in-time universe (survivorship-bias guard) | Backtest-rigor toggle; on for backtests, off for live. |
| `HERMES_QUANT_INSIDER_ENABLED` / `_CALENDAR_ENABLED` / `_FUNDAMENTALS_ENABLED` | Optional evidence adapters | Data-source opt-ins (some hit rate-limited / cloud-blocked APIs). Keep as toggles. |
| `HERMES_QUANT_*_THRESHOLD` / `_HAIRCUT` / `_ROUNDS` / `_DECAY` / `_REPORTING_LAG` | Numeric tuning params | Tunables, not features. Keep. |
| `HERMES_QUANT_OPEN_GUARD` | Disable the open-guard (note: this one is a *disable* switch) | Safety override; keep. |
| `HERMES_QUANT_AUTONOMOUS` / `_AUTONOMOUS_ARMED` | Cron-side: enable autonomous phase / arm it | The two-knob arm pattern (shadow vs live). Safety. Keep. |

---

## TIER C — Default-OFF, never set anywhere. Decide: adopt, or retire as dead weight.

These are read in code, default `"0"`, and are set in NEITHER `.env` NOR any wrapper.
They are doing nothing right now. Each is either (a) an experiment that was never
adopted → **retire**, or (b) a real feature awaiting a decision → **trial then adopt**.

> **VERIFIED 2026-06-06 — RETIRE set is EMPTY. Nothing here is safe to delete.**
> A 34-flag audit was cross-checked by TWO independent reviewers reading the actual
> read-sites (Hermes/Opus-4.8 and OpenAI/Codex, different training distributions).
> **Both independently returned RETIRE = 0.** Convergent finding: every flag's ON-path
> is live, wired, and behavior-changing — there are no unreachable/superseded no-op
> spikes. The codebase is fresh (HEAD 2026-06-05; nearly every flag touched within the
> prior ~2 weeks), so the "experiments that never graduated" premise does not hold here.
> The flags split cleanly into two buckets, NEITHER of which is "delete":
>
> **KEEP (15) — already SET in deploy env/wrappers OR a genuine config/safety/cost/test knob (mis-tiered as C):**
> `CONVERGENCE`, `CALIBRATOR_AUTO_REFIT`, `HORIZONS`, `CATALYST_ONBOARDING`, `SATURATION`,
> `ADMISSIBILITY` (all SET in env/wrappers — live config, not Tier-C); plus the legitimate
> knobs `TRADER_LLM`, `RESEARCH_DEBATE_ROUNDS`, `PREWARM_WORKERS`,
> `LOAD_TEST` (test/CI-only, 0 src files), `PLAYS_OPEN`, `RESEARCH_RISK_TIER_BLOCK`,
> `MCP_READS_ENABLED`, `HYPOTHESIS_NOVELTY_THRESHOLD` (config/safety/cost — Tier B by nature).
> _(ra09 2026-06-15: `WATERMARK_ENABLED` was dropped from this KEEP list — it has been
> REMOVED from `hermes_quant/` entirely and no longer appears in FLAG-INVENTORY.md. Count 16→15.)_
>
> **PROMOTE-CANDIDATE (16–18) — real default-OFF features that change the decision/risk
> core and need a backtest/replay EVAL before default-on (the flag-ablation harness gates these):**
> the L2 learning-loop cluster (`STACKING`, `L2_POSTERIOR_DECAY`, `L2_PER_ANALYST_CALIB`,
> `L2_LESSON_HAIRCUT`, `L2_POSTERIOR_PERSIST`), the ADR risk/honesty rails (`EVENT_RISK`,
> `BORROW_COST`*, `GROUNDING_ENFORCE`), and the analyst/perception features `TREND_VELOCITY`,
> `RESEARCH_DEBATE`, `REDTEAM_TURN`, `MEMORY_SPLIT`, `ANALYST_ADMISSION`, `ANALYSTS_USE_REGIME`,
> `REGIME_HMM`, `SNAPSHOT_V2`*, `SHADOW_RULE_MINING`*, `STRUCTURE_SELECT`*.
> (* = reviewers split KEEP-vs-PROMOTE-CAND or flagged INVESTIGATE; immaterial — none is RETIRE.)
>
> **Net: the flag count does NOT drop via deletion.** The reduction path is PROMOTION
> (proven features → code defaults, deleting the flag) gated on the ablation eval — not
> retirement. Do not dispatch a "retire dead flags" pass; there are none. Re-audit only if
> a future flag's ON-path is genuinely orphaned (run the same two-reviewer check first).

> **CORRECTION (2026-06-05):** an earlier draft of this list wrongly bucketed the **L2 learning-loop
> cluster** (`HERMES_QUANT_STACKING`, `HERMES_QUANT_L2_POSTERIOR_DECAY`, `_L2_PER_ANALYST_CALIB`,
> `_L2_LESSON_HAIRCUT`, `_L2_POSTERIOR_PERSIST`) as dead spikes. They are NOT — they shipped
> **2026-06-04 in PR #49** ("close the learning loop": per-analyst calibration + persisted Beta
> posteriors + reflection→decision haircut), all in `aggregators/bma.py`, default-OFF pending eval.
> These are **PROMOTION candidates after an eval pass**, not retirements — flipping them on changes how
> the BMA decision core weights analysts on the live path, so they need a backtest/shadow eval before
> default-on, not a blind flip. Treat `ADMISSIBILITY`/`EVENT_RISK`/`BORROW_COST`/`GROUNDING_ENFORCE`
> (ADR-0077/0084) the same way: ADR-backed, live-path-wired, promote-after-eval.

> **Do NOT mass-delete.** Each needs a one-line check: is it referenced by a recent ADR/PR as the
> path forward (→ keep, eval, promote) or is it a stale spike (→ retire)? The git-blame recency check
> (`git log -1 -S HERMES_QUANT_<flag> -- <file>`) is the fast triage: anything touched in the last
> ~2 weeks is fresh work, not a spike. Several Tier-C flags (`ADMISSIBILITY`, `EVENT_RISK`,
> `BORROW_COST`, `GROUNDING_ENFORCE`, the whole L2 cluster) are promotion candidates, not retirement
> candidates. The retire-vs-adopt triage is the real follow-up work this doc surfaces.

---

## Recommended next actions (in order)

1. **Promote the proven hot-path defaults** — DONE in code (ra09 2026-06-15, verified vs
   FLAG-INVENTORY.md): `PAPER_SLIPPAGE_MODEL`=`v0.2`, `SEMANTIC_ENABLED`=`1`, `REFLECTION`=`1`,
   `MEMORY_INJECT`=`1`, `WEEKLY_RETRO`=`1` are now the code defaults (each retains a single
   off-switch via `=0`/`=v0.1`). Remaining cleanup: delete the now-redundant `.env`/wrapper
   lines that re-assert these (removes 5 lines of config drift). The code-default flip itself
   is closed.
2. **Bake DETERMINISTIC_EQUITY** after ~1 week of clean autonomous ticks → default `1`,
   then **delete PORTFOLIO_CAPS** from the wrapper (BP enforcement replaces it).
3. **Triage Tier C** — DONE (2026-06-06, two-reviewer audit): **RETIRE set is empty.** Every
   Tier-C flag is either live config mis-tiered as C (→ KEEP) or a real default-OFF feature
   needing eval (→ PROMOTE-CANDIDATE). The count does NOT drop via deletion. Reduction comes
   from PROMOTION gated on the flag-ablation eval harness (run `hermes quant ablate <flag>`,
   read the Sharpe/DSR delta, promote only what earns it).
4. **Leave Tier B alone** — those are config and safety switches that correctly stay flags.

End-state target: ~15–20 genuine config/safety flags (Tier B) + a handful of
deliberate capability gates. Everything proven becomes a default; everything unused
gets deleted.
