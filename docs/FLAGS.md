# Hermes-Quant Feature Flags — Inventory & Promotion Plan

> Generated 2026-06-05 from a ground-truth scan of `os.environ.get(...)` reads +
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
| `HERMES_QUANT_SEMANTIC_ENABLED` | .env=1 | Catalyst/semantic analyst in the advisor pool | `"0"` | **PROMOTE** — core to the advisor; on for weeks. Flip default → `1`, keep an off-switch for one release. |
| `HERMES_QUANT_REFLECTION` | .env=1, wrapper | Per-trade reflections → weekly retro | `"0"` | **PROMOTE** — feeds the learning loop; stable. |
| `HERMES_QUANT_MEMORY_INJECT` | .env=1 | Inject memory lessons into LLM committee | `"0"` | **PROMOTE** (with `MEMORY_SPLIT` — see Tier C). |
| `HERMES_QUANT_GRAPH_MINING` | .env=1 | Weekly catalyst-graph mining cron | `"0"` | **KEEP** — cheap, but it's a cron-cadence toggle, not a hot-path default. Low priority either way. |
| `HERMES_QUANT_RESEARCH_LOOP` | .env=1 | Weekly research-loop cron | `"0"` | **KEEP** — same: cron-gated batch job, fine as a flag. |
| `HERMES_QUANT_WEEKLY_RETRO` | .env=1 | Weekly strategy retro LLM pass | `"0"` | **PROMOTE** — part of the standing retro cadence. |
| `HERMES_QUANT_PAPER_SLIPPAGE_MODEL` | wrapper=v0.2 | Realistic paper fill slippage (`v0.1` passthrough vs `v0.2` model) | `"v0.1"` | **PROMOTE default → `v0.2`** — v0.2 is strictly more honest; passthrough understates cost. This is the clearest promote on the list. |
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
This is where the sprawl actually lives — ~25 flags that are pure latent surface.

**Likely RETIRE (experiments that never graduated — confirm no recent commits reference them as active):**
`HERMES_QUANT_STACKING`, `HERMES_QUANT_SATURATION`, `HERMES_QUANT_SHADOW_RULE_MINING`,
`HERMES_QUANT_TREND_VELOCITY`, `HERMES_QUANT_CONVERGENCE`, `HERMES_QUANT_TRADER_LLM`,
`HERMES_QUANT_RESEARCH_DEBATE`(+`_ROUNDS`), `HERMES_QUANT_REDTEAM_TURN`,
`HERMES_QUANT_L2_*` (posterior decay/persist, per-analyst calib, lesson haircut — the
whole L2 experiment cluster), `HERMES_QUANT_SNAPSHOT_V2`, `HERMES_QUANT_STRUCTURE_SELECT`,
`HERMES_QUANT_MEMORY_SPLIT`, `HERMES_QUANT_ANALYST_ADMISSION`, `HERMES_QUANT_ANALYSTS_USE_REGIME`,
`HERMES_QUANT_TREND_VELOCITY`, `HERMES_QUANT_WATCHLIST_CAP_TRIM`, `HERMES_QUANT_WATERMARK_ENABLED`,
`HERMES_QUANT_REGIME_HMM`, `HERMES_QUANT_PREWARM_WORKERS`, `HERMES_QUANT_LOAD_TEST`,
`HERMES_QUANT_CALIBRATOR_AUTO_REFIT`, `HERMES_QUANT_HORIZONS`, `HERMES_QUANT_PLAYS_OPEN`,
`HERMES_QUANT_CATALYST_ONBOARDING`, `HERMES_QUANT_IC_DEDUP_*`, `HERMES_QUANT_GROUNDING_ENFORCE`,
`HERMES_QUANT_DATA_FALLBACK`, `HERMES_QUANT_ADMISSIBILITY`, `HERMES_QUANT_EVENT_RISK`,
`HERMES_QUANT_BORROW_COST`, `HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK`, `HERMES_QUANT_MCP_READS_ENABLED`,
`HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD`.

> **Do NOT mass-delete.** Each needs a one-line check: is it referenced by a recent ADR
> as "the path forward" (→ keep, trial it) or is it a stale spike (→ retire)? Several of
> these (`ADMISSIBILITY`, `EVENT_RISK`, `BORROW_COST`, `GROUNDING_ENFORCE`) are
> *risk/honesty* features that arguably SHOULD be on — they're Tier-C only because nobody
> flipped them. Those are promotion candidates, not retirement candidates. The retire-vs-
> adopt triage is the real follow-up work this doc surfaces.

---

## Recommended next actions (in order)

1. **Promote the proven hot-path defaults** (one PR): flip code defaults for
   `PAPER_SLIPPAGE_MODEL`→`v0.2`, `SEMANTIC_ENABLED`→`1`, `REFLECTION`→`1`,
   `MEMORY_INJECT`→`1`, `WEEKLY_RETRO`→`1`. Keep a single-release off-switch each, then
   delete the `.env`/wrapper lines. Removes 5 lines of config drift.
2. **Bake DETERMINISTIC_EQUITY** after ~1 week of clean autonomous ticks → default `1`,
   then **delete PORTFOLIO_CAPS** from the wrapper (BP enforcement replaces it).
3. **Triage Tier C**: for each flag, grep the ADRs; adopt the risk/honesty ones
   (`ADMISSIBILITY`, `EVENT_RISK`, `BORROW_COST`, `GROUNDING_ENFORCE` are the prime
   suspects), retire the stale spikes. This is where the count drops from ~50 to ~20.
4. **Leave Tier B alone** — those are config and safety switches that correctly stay flags.

End-state target: ~15–20 genuine config/safety flags (Tier B) + a handful of
deliberate capability gates. Everything proven becomes a default; everything unused
gets deleted.
