# Options + Daily Picker + Self-Critique Retrospective Loop

> **For Hermes:** This is a deep-work-loop plan. Use kanban-pipeline-laydown to dispatch waves of subagents (research → architect → review → plan → implement → review → verify), per `model-roster` family-diversity rules.

**Goal:** Evolve hermes-quant from equity-only directional advisor into a daily options + swing-trade picker for an Alpaca paper account, with a built-in self-critique retrospective loop that proposes (but does not auto-apply) architectural amendments based on its own track record.

**Architecture:** Three new ADR families layered on the existing 25-ADR foundation:

1. **Retrospective amendment loop** (ADR-0026) — **the headline change.** Per-trade postmortems (zero-LLM, deterministic, fires from `settlement_loop`) → weekly cross-family LLM scatter audit → monthly meta-retro → all proposals land in `proposed_amendments.jsonl` with HITL approval gate. None of these auto-mutate code.
2. **Options end-to-end** (ADR-0027 risk gate, ADR-0028 data layer, ADR-0029 reactor) — protocol gets `OptionContract` + `OptionLeg` types, Alpaca options chain + greeks data provider, options-aware risk gate (Greeks delta + assignment risk + margin), paper reactor that handles multi-leg orders.
3. **Daily picker recipe + cron** (ADR-0030) — pre-market and EOD cron jobs run the recipe, pipeline routes through analyst→aggregator→risk-gate, surfaces ranked plays as HITL proposals to Discord. Strategy mix priority: **covered call → CSP → wheel state machine → 30-90 DTE directional swing → LEAPS thesis**.

**Tech stack additions:** `alpaca-py` options endpoints (already a dep), `py_vollib` or `numpy` Black-Scholes for greek validation, no new heavy deps.

**Out of scope (deferred):**
- Polymarket / prediction markets (separate ADR class entirely; user didn't ask)
- Live broker (paper-only per ADR-0015 §D5 + §D10)
- Intra-day trading (user explicit: "we are not fast enough")
- Full DSL Level-2/3 from user's blueprint doc (`recipes.py` already covers Level-1; Level-2/3 are future ADRs)
- RL aggregator (still deferred per ADR-0006)

---

## What's already in place (DO NOT rebuild)

- ✅ Analyst Protocol + 4 analysts (classical TA, microstructure, kronos, hermes-semantic)
- ✅ 3 aggregators (BMA, stacking, deliberative committee)
- ✅ Risk gate with discrete sizing, halts, ¼-Kelly (equity only)
- ✅ Advisor (`hermes quant recommend SYMBOL`) — synchronous, read-only, no daemon needed
- ✅ HITL proposal store (`proposals.jsonl` + `proposals.db`)
- ✅ Autonomous tick orchestrator (`autonomous.py`) with kill-switch
- ✅ PDR Recipe registry (YAML-loadable from `~/.hermes/quant/recipes/`)
- ✅ Settlement loop + journal (where the retro feeds from)
- ✅ Backtest harness (ADR-0020) + walk-forward CV
- ✅ No-lookahead CI gate
- ✅ Paper reactor writing to `executions.jsonl`
- ✅ Alpaca paper account verified (Level 3 options, $100k equity, $200k buying power)

## What's missing (THIS plan)

- ❌ `OptionContract` / `OptionLeg` types in `protocol.py` (`option` is a Literal but explicitly deferred per ADR-0009 §P2-options TODO)
- ❌ Options chain + greeks fetcher in data layer
- ❌ Covered-call / CSP / wheel / LEAPS / swing analysts
- ❌ Options-aware risk gate (delta limits, assignment risk, margin checks)
- ❌ Multi-leg paper reactor
- ❌ Daily picker recipe + cron schedule
- ❌ **Self-critique retrospective loop** ← architectural headline
- ❌ Methodology library (formalized from socalminh-style reels)

---

## Phase 0 — Slug probe + interim daily cron (Day 0, today)

The retro loop and options work will take days. We start the daily ping cadence Tuesday using only what already works (equity directional bias from existing `advisor.recommend()`), so the user gets a daily artifact while the bigger work lands. The interim brief gets replaced cleanly when ADR-0030 ships.

### Task 0.1: Probe OpenRouter slugs

**Objective:** Verify which of `claude-opus-4.7`, `gpt-5.5`, `gemini-3.1-pro-preview`, `deepseek-v4-pro`, `grok-4.3` are live RIGHT NOW (slugs flip live/dead intra-day per `model-roster` skill).

**Files:** none (probe-only)

**Command:**

```bash
source ~/.hermes/.env  # OPENROUTER_API_KEY
bash ~/.hermes/skills/autonomous-ai-agents/model-roster/scripts/probe-openrouter-slugs.sh \
  anthropic/claude-opus-4.7 \
  openai/gpt-5.5 \
  google/gemini-3.1-pro-preview \
  deepseek/deepseek-v4-pro \
  x-ai/grok-4.3 \
  moonshotai/kimi-k2.6
```

**Verify:** every slug printed `🟢` (or note which are dead and substitute different-family alternatives before any scatter). Record verified set in `/tmp/quant-plan/verified-slugs.json`.

### Task 0.2: Interim daily cron — equity directional bias only

**Objective:** Get a daily Discord ping flowing TUESDAY 2026-05-26 8:30 AM ET using existing `advisor.recommend()` against a curated mid-cap universe. Zero new code in hermes-quant. Pure cron + advisor calls.

**Files:**
- Create: `~/.hermes/scripts/quant-daily-interim.py` (cron payload)
- Create: `~/.hermes/scripts/quant-universe-interim.txt` (~30 mid-cap tickers + 10 large-cap "watch" tickers)

**Cron config:**
```
schedule: "30 8 * * 1-5"          # 8:30 AM ET weekdays
delivery: "discord:home"
no_agent: false                    # let LLM format the brief from advisor JSON
```

**Verify:** dry-run via `cronjob action=run` produces a Discord-formatted brief listing top 5 directional picks with confidence + Kelly fraction + invalidation level. **No options analysis yet** — that's labeled clearly in the message header so user knows the cadence is up but the options layer is still landing.

### Task 0.3: Save plan, register pipeline workspace

**Files:**
- Already creating: this file
- Create dir: `~/.hermes/kanban/pipelines/<root>/{docs/research,docs/adr,docs/design,artifacts}` once kanban root is known

---

## Phase 1 — Parallel research scatter (Day 0-1, ~30 min wallclock)

Four research subagents run in parallel via `delegate_task(tasks=[...])`. Each is family-diverse per `model-roster`. Each writes a markdown research note to a shared workspace.

### Research tasks

| # | Task | Model | Family | Output |
|---|------|-------|--------|--------|
| R1 | Alpaca options API capabilities — what's exposed in paper, what greeks come back, rate limits, multi-leg order shape, assignment simulation, contract symbol format, historical chain data availability | gpt-5.5 (or substitute if dead) | OpenAI | `docs/research/r1-alpaca-options-api.md` |
| R2 | Options-aware risk-gate prior art across LEAN, NautilusTrader, hftbacktest, Tastyworks rules, IBKR API options margin endpoints — what concrete checks should the gate run, what greeks are mandatory, how to budget assignment risk | gemini-3.1-pro-preview | Google | `docs/research/r2-options-risk-gate-prior-art.md` |
| R3 | Self-critique retrospective loop architectures — survey AutoGPT/MetaGPT/CAMEL-style critic loops, RLHF reward-modeling-from-rollouts, classical AAR (after-action review) practices, AlphaGo-style policy improvement from self-play. Constrain to: PROPOSAL-ONLY (no auto-apply), explainability-first, non-LLM postmortem layer below LLM audit layer for cost control | deepseek-v4-pro | DeepSeek | `docs/research/r3-retrospective-loop-architectures.md` |
| R4 | Screener spec literature — formal language for codifying "scanner methodologies" from sources like Minh's reel. Survey Finviz/Barchart/Tasty screener DSLs, Quantopian screener API, screener-as-code patterns. Plus: codify the socalminh-covered-call-screener.md we already extracted as a worked example | kimi-k2.6 | Moonshot | `docs/research/r4-screener-spec-literature.md` |

Each research task gets max 1800s runtime, file + web toolsets only (no terminal — research is read-heavy).

---

## Phase 2 — Architect 5 ADRs (Day 1, parallel after research)

One architect subagent (model-roster anchor: `claude-opus-4.7` via Bedrock) reads all 4 research outputs and writes 5 ADRs in sequence:

- **ADR-0026: Retrospective amendment loop** — per-trade postmortem schema, weekly audit prompt template + scatter shape, monthly meta-retro, `proposed_amendments.jsonl` schema, HITL approval CLI shape, **what the loop CANNOT do** (no code mutation, no risk-rule auto-relaxation, no halt clearing)
- **ADR-0027: Options-aware risk gate** — extends ADR-0004 with delta exposure limits, gamma exposure cap, theta budget, vega exposure, assignment risk reserve, margin requirement check, multi-leg net-greek aggregation, what the gate ENFORCES vs what it warns
- **ADR-0028: Options data layer** — `OptionContract` + `OptionLeg` + `OptionChain` dataclasses, Alpaca options chain provider, greeks normalization (since some venues return numerical, some don't), historical chain backtesting strategy, point-in-time `as_of` semantics for chains (you can't replay options without expired contracts)
- **ADR-0029: Multi-leg paper reactor** — extends `react/paper.py` for spreads/wheels, fill assumption (mid? bid for sells, ask for buys?), exercise/assignment simulation at expiry, what to do with stranded short legs
- **ADR-0030: Daily picker recipe + cron** — recipe YAML schema for daily pickers (universe, strategy mix, ranking function, max-positions cap), pre-market vs EOD vs overnight scan cadences, output contract (the Discord brief format)

Each ADR includes the standard sections: Status, Context, Decision, Consequences, Alternatives Considered, **Premortem (what's the failure mode of THIS decision)**, **Self-critique hooks** (which retro signal would invalidate this ADR).

---

## Phase 3 — Cross-family ADR review (Day 1, ~15 min)

3-way scatter on each ADR's diff:

- gpt-5.5 — security/adversarial lens (especially ADR-0026: can the retro loop be tricked into approving bad amendments?)
- gemini-3.1-pro-preview — long-context holistic lens (do the 5 ADRs contradict any of the existing 25?)
- deepseek-v4-pro — math/correctness lens (especially ADR-0027 greeks math + ADR-0028 chain replay semantics)

Each reviewer outputs `APPROVE / BLOCKING / MAJOR / MINOR` per ADR with line refs. Architect resolves blockers + majors before proceeding.

---

## Phase 4 — Wave plan + parallel implementation (Day 1-3)

Planner subagent decomposes the ADRs into bite-sized tasks per `writing-plans` skill, organized into 4 waves (sequential between waves, parallel within):

### Wave A — Foundation types + retro postmortem (no LLM in hot path)

- A1: Add `OptionContract`, `OptionLeg`, `OptionChain` to `protocol.py` (TDD)
- A2: Add `Postmortem` dataclass + `~/.hermes/quant/postmortems.jsonl` writer to `settlement_loop` (TDD, deterministic)
- A3: Add `proposed_amendments.jsonl` + `proposals.db` extension for amendment tracking
- A4: Tests for new types (no_lookahead-style, schema, atomic write)

### Wave B — Options data + risk gate (parallel with A's review)

- B1: Alpaca options chain provider extending `data/alpaca_provider.py` with options endpoints
- B2: Options-aware risk gate (`risk/options_gate.py`) extending existing gate with greek limits
- B3: 4-5 options analysts: `covered_call`, `cash_secured_put`, `wheel_state`, `swing_directional`, `leaps_thesis`
- B4: Smoke tests against Alpaca paper API (live network, marker `requires_network`)

### Wave C — Reactor + recipe + cron

- C1: Multi-leg paper reactor extending `react/paper.py`
- C2: `daily_options_picker.v1.yaml` recipe template
- C3: Daily cron orchestrator script + Discord brief formatter
- C4: Replace interim cron from Phase 0.2 with full picker

### Wave D — Retro loop activation

- D1: `hermes quant retro week` CLI command (gathers postmortems, scatters to LLMs, writes weekly retro markdown)
- D2: `hermes quant retro month` CLI command (cross-week meta-retro, drafts ADR amendments)
- D3: Sunday EOD cron for D1, monthly cron for D2
- D4: `hermes quant retro list/show/approve/reject` CLI for HITL amendment workflow
- D5: First retro fires Sunday 2026-05-31 to seed the loop with whatever paper trades have accumulated

Each task gets a code-review subagent in 2-stage review (spec compliance → code quality), per `subagent-driven-development` skill.

---

## Phase 5 — Cross-family code review + final verify (Day 3-4)

After Wave D lands, scatter the full diff to 3 reviewers (different families from the ADR-review trio to avoid blind-spot overlap):

- moonshotai/kimi-k2.6 — fresh eyes, non-Western training distribution
- minimax/minimax-m2.7 — long-context comprehensive coverage
- nvidia/nemotron-3-super-120b-a12b — independent reasoning

Convergent findings ship as follow-up MRs; solo P0s get a 60-second human sanity check before applying (per `model-roster` anti-pattern entry).

---

## Phase 6 — Smoke test + handoff (Day 4)

- Run `hermes-quant-daemon` against Alpaca paper for 1 trading day in **HITL mode** (proposes plays, requires my Discord approval before paper-firing)
- Verify postmortem records appear in `postmortems.jsonl` after each settlement
- Trigger `hermes quant retro week --dry-run` manually to confirm the LLM scatter works end-to-end
- Update `CHANGELOG.md` with v0.5.0 entry
- Update `AGENTS.md` to add a "retro loop" section

---

## Hard rules (preserved from existing AGENTS.md, NOT mutable by retro loop)

- **Silence by default** — when uncertain, hold cash
- **Money never goes through tools** — CLI + HITL approval only, never tool-calls
- **Action space stays discrete** — `{0, ±0.05, ±0.10, ±0.15, ±0.20}` of NAV (extends to options notional-equivalent)
- **No look-ahead bias** — CI gate stays on, options chains use `as_of` filter at the leaf
- **Calibrators stay calibrated** — confidence numbers must track empirical accuracy
- **Retro loop CANNOT auto-mutate** any of these. They live in this list specifically to be non-negotiable. Retro can propose changes; only `hermes quant retro approve <id>` (a CLI command requiring explicit confirmation) applies them, and even then only via a normal git commit + ADR amendment, not a runtime patch.

## Risk envelope (defaults the user accepted)

- Max 0.5% NAV daily-loss circuit breaker → halt scope `(account, asset_class, symbol?)` with `halted_until=tomorrow_open`
- Max 5% strategy drawdown halt → indefinite halt, requires `hermes quant resume` to clear
- ¼-Kelly position sizing (per ADR-0004 with the σ² fix from ADR-0009 §P0-1)
- Max concurrent open positions: 8 across all asset classes
- Max single-trade notional: 10% of NAV (= $10k on $100k paper)

These are **rules**, not amendments-loop-tunable parameters. Changing them requires the same HITL + ADR-amendment + commit flow as touching the analyst protocol.

## Success criteria (what "done" looks like)

1. Tuesday 2026-05-26 8:30 AM ET: interim daily brief fires to Discord (Phase 0)
2. ~Friday 2026-05-29: full options picker brief fires; first paper trades being proposed via HITL
3. Sunday 2026-05-31: first weekly retro fires; produces a markdown report (probably empty findings if too few trades, but the pipeline runs end-to-end)
4. By 2026-06-15: ≥30 paper trades settled; first weekly retro with substantive findings; first proposed amendment in `proposed_amendments.jsonl`
5. By 2026-06-30: first amendment approved by user, applied via PR, ADR amended; full feedback loop closed at least once

## Backlog (deferred, tracked here so we don't lose them)

- Polymarket prediction-market integration (would need its own perception/decision/reaction sub-tree; user mentioned but didn't prioritize)
- DSL Level-2 (expression DSL beyond YAML) and Level-3 (WASM/sandboxed Python plugins)
- Live broker reactor (gated to v0.3.0 minimum, requires hardware key signing per user blueprint)
- More reels → more analysts. Pipeline already prototyped (yt-dlp + dual-analysis + methodology dir). User can drop reels and the pipeline ingests them as new screener specs.
- Cross-strategy correlation analysis (when covered-call analyst and CSP analyst both fire on same name, recognize the implicit wheel intent)
