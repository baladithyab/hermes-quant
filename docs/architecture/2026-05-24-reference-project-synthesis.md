# Reference-project synthesis: patterns to steal, anti-patterns to avoid

**Date:** 2026-05-24
**Scope:** consolidates findings from 6 parallel research workers across 5 reference projects + 1 paper
**Workers / lenses:**

| ID | Source | Lens | Reviewer | Words |
|---|---|---|---|---|
| R1 | TauricResearch/TradingAgents | role decomp + bull/bear debate | Opus 4.7 | 2,298 |
| R2 | HKUDS/AI-Trader | skill registry + signal marketplace + copy-trading | Gemini-3.1-Pro | 832 |
| R3 | HKUDS/Vibe-Trading | Run Cards + research/execution split + memory | Grok-4.3 | 1,304 |
| R4 | FutureSim (arXiv 2605.15188) | evidence-store schema | DeepSeek-V4-Pro | 3,484 |
| R5 | TauricResearch/TradingAgents (graph layer) | orchestration + state flow + error handling | Codex (gpt-5.5) | 583 |
| R6 | yolojewjitsu/moon-dev-ai-agents | cautionary anti-patterns | Opus 4.7 | 1,641 |

**Total:** 10,142 words, 6 distinct vantage points.

Inputs at: `/tmp/quant-research/outputs/r{1..6}-*.{md,txt}`. Cloned source repos at `/tmp/quant-research/sources/`.

---

## Convergent findings — patterns that landed across multiple workers

These are the items where ≥2 reviewers, looking through different lenses, converged on the same recommendation. Convergence = high signal.

### CV1: Structured analyst output with explicit `evidence_ids` linkage (R1 + R4 + R5)

R1 noted that TradingAgents' analysts emit free-text markdown blobs with no AnalystView schema; structured output appears only at the three decision roles (`ResearchPlan`, `TraderProposal`, `PortfolioDecision`). R4 proposed a 22-field `EvidenceRecord` Pydantic model with `evidence_ids: tuple[UUID, ...]` linking analyst views back to underlying evidence. R5 confirmed TradingAgents' state flow — reports are read by downstream agents but no audit chain exists from final decision back to the bars/news/filings that informed it.

**Convergent recommendation:** add `evidence_ids: tuple[UUID, ...]` (default empty tuple for backward-compat) to hermes-quant's existing `AnalystView` (ADR-0002) and `AggregatedSignal`. Every claim an analyst emits must cite the evidence records it used. This is **the single highest-leverage change** in the whole synthesis — it operationalizes both the audit trail and the FutureSim chronological-replay invariant.

**Lands in:** new ADR-0033 (Evidence Store) + amendment to ADR-0002 (Analyst Protocol) for the new field.

### CV2: The `available_at` invariant is universal (R4 + R6)

R4 formalized FutureSim's chronological-replay mechanism into a three-timestamp model (`published_at`, `ingested_at`, `available_at`) that every evidence kind must carry. R6 documented that moon-dev has zero look-ahead-prevention discipline — agents pull live data with no `as_of` clamp anywhere — exactly the failure mode FutureSim was designed to prevent.

**Convergent recommendation:** the `available_at` invariant is non-negotiable for any analyst that consumes news/filings/social. CI must be extended (`tests/test_no_lookahead.py`) to assert that no analyst at backtest tick T returns any evidence with `available_at > T`. Currently we enforce this for OHLCV bars only.

**Lands in:** ADR-0033 (Evidence Store) makes `available_at` a required field; CI invariant in test suite.

### CV3: String-grep control flow is the cardinal anti-pattern (R1 + R6)

R1 flagged TradingAgents' Trader role: it emits markdown ending with the literal `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` and downstream code grep-matches that line. R6 found moon-dev does the same thing but worse — `lines[0].strip()` is the action verb, `int(''.join(filter(str.isdigit, line)))` extracts confidence, JSON portfolio allocation comes from `response.find('{')` / `response.rfind('}')`. Any LLM output containing example JSON in its reasoning corrupts the parse.

**Convergent recommendation:** structured outputs only — Pydantic / `bind_structured` / equivalent. Free-text output of a stochastic generator must never be a structured command channel. If a model fails to produce valid structured output, retry once then route to silence-by-default (gate the proposal, don't guess). hermes-quant already does this via ADR-0002 `AnalystView`; the lesson is to keep doing it and never regress.

**Lands in:** existing posture, but worth restating in a Phase-4 ADR amendment for visibility.

### CV4: Two-tier LLM (quick + deep) for cost discipline (R1)

R1 identified that TradingAgents uses a `quick_thinking_llm` for analysts and bull/bear debaters (cheap, fast, throwaway prose) and a `deep_thinking_llm` for the Research Manager and Portfolio Manager (structured, judgmental). The quick tier handles high-volume narrative; the deep tier handles the few decision points where structure matters.

This was reinforced indirectly by R3 (Vibe-Trading uses LLM-light templates for shadow-rule natural-language translation, falling back to template strings when no LLM available — same cost-discipline pattern, different shape).

**Convergent recommendation:** ADR-0023 (deliberative committee) currently assumes one LLM tier. Adopt the two-tier split: cheap reviewer for high-volume per-tick analysis, frontier-tier reviewer for weekly/monthly retro audits and gate-rejection postmortems. This already partially landed in ADR-0026 (cost ceiling for retro layer) but isn't formalized in the per-tick path.

**Lands in:** amendment to ADR-0023.

### CV5: The trader → portfolio-approval boundary is where every reference project fails (R1 + R2 + R6)

R1: TradingAgents' Trader role can specify `position_sizing: Optional[str]` as **free-text prose**; the Portfolio Manager rubber-stamps. R2: AI-Trader has 1:1 blind copy-trading — once Agent B subscribes to Agent A, all of A's trades cascade into B's account with no per-trade confirmation. R6: moon-dev's `risk_agent.py:319` has the worst version — `self.override_active = "OVERRIDE" in response_text.upper()` lets an LLM substring-match disable the daily loss limit for 15 minutes.

**Convergent recommendation:** ALL THREE reference projects let the LLM be the final execution authority somewhere. hermes-quant's ADR-0004 deterministic risk gate + ADR-0015 HITL approval is the inverse of this pattern, and the synthesis confirms it's the right call. **Reinforce this in our docs** — anyone reading TradingAgents/AI-Trader/moon-dev for inspiration will hit these patterns and might import them. The defensive doc layer should explicitly call them out.

**Lands in:** new section in `AGENTS.md` ("Anti-patterns from reference projects we explicitly reject") + ADR-0026 already mostly says this; a forward-reference would help.

---

## Unique findings — items only one reviewer surfaced but worth keeping

### U1: TradingAgents' bull/bear debate has no convergence detection (R1, R5)

The debate runs `Bull → Bear → Bull → Bear → ...` until `count >= 2 * max_debate_rounds`. Pure turn-cap. No semantic similarity check, no "they agreed" early-stop, no sentiment crossing. R5 confirms this is a feature, not a bug — putting deliberation limits in routing (deterministic) rather than in prompts (model-dependent) is intentional.

**Recommendation:** when hermes-quant adds a bull/bear stage to ADR-0023, copy the routing-level limit pattern verbatim. **Don't** ask the LLM to detect convergence — that's a prompt-injection vector and a non-determinism leak.

**Lands in:** ADR-0023 amendment.

### U2: Vibe-Trading's `run_card.json` is the artifact format we need (R3)

Exact schema captured in R3. Key fields: `schema_version`, `generated_at`, `reproducibility.config_hash` (SHA-256 of deterministic JSON serialization), `reproducibility.strategy_hash` (SHA-256 of strategy file), `data_sources`, `metrics`, `validation` (monte_carlo, walk_forward), `warnings`, `artifacts` (full SHA-256 manifest). Already has companion `run_card.md` for human reading.

**Recommendation:** port `agent/backtest/run_card.py` directly into `hermes_quant/backtest/run_card.py` as the canonical artifact for every backtest. ~200 LOC, MIT-compatible per Vibe-Trading's licensing. This is essentially shrink-wrapped — minimal adaptation needed.

**Lands in:** new ADR-0034 (Run Cards) or amendment to ADR-0020 (Backtest Harness).

### U3: Vibe-Trading's shadow-account analysis is novel — generates `ShadowProfile` from real trade journals (R3)

Their `extractor.py` parses broker journals (同花顺, 富途, generic CSV), pairs trades FIFO, filters to profitable roundtrips, KMeans-clusters them, extracts decision-tree paths (max depth 3) into structured `entry_condition` / `exit_condition` dicts, then translates to natural language. Output is an immutable `ShadowProfile` with 3-5 `ShadowRule`s, used to compare an agent's actual trades against "rules the trader appears to follow."

This is the doc's "Paper Level 3 Broker Shadow" rung from the inverse direction — not "what would the system have done with live data," but "what does the human's actual trading history imply about their rules?"

**Recommendation:** keep this on the radar but **defer**. It's a valuable feature for self-introspection but solves a problem we don't yet have (no realized trades to mine). When hermes-quant has 60+ days of paper history per ADR-0029 D7, lift this pattern to generate self-shadow-profiles.

**Lands in:** future ADR (post-Wave-F).

### U4: AI-Trader's signal type taxonomy (R2)

Separates `operation` (executed trade) from `strategy` (idea/thesis) from `discussion` at the `message_type` field level. hermes-quant currently conflates `AnalystView` with execution intent — there's no separate "thesis" channel.

**Recommendation:** add a `kind: Literal["operation", "strategy", "discussion"]` discriminator to our signal bus contract (ADR-0008). The pre-trade risk gate (ADR-0004) only consumes `operation` signals; `strategy` and `discussion` route to advisor surface (ADR-0014) and retro layer (ADR-0026) only.

**Lands in:** ADR-0008 amendment.

### U5: TradingAgents' "message clear" pattern keeps context windows clean (R1, R5)

Between each analyst and the next, a `Msg Clear *` node removes prior tool-call messages and replaces with `HumanMessage("Continue")`. This prevents the next analyst's context from being polluted with the previous analyst's tool-call chatter, which keeps token costs down and prevents one analyst's reasoning from anchoring the next.

**Recommendation:** when our committee runs analysts sequentially, adopt this pattern. Currently `committee_runner.py` just passes `MarketContext` to each analyst, but if we ever add ToolNode-style analysts (e.g., the LLMAnalyst from ADR-0012), we'll need this.

**Lands in:** ADR-0012 (LLMAnalyst) when implemented.

### U6: TradingAgents' graph-level checkpointing for resumability (R5)

`SqliteSaver` checkpoints at the LangGraph level, scoped by `(ticker, date)`. Failures mid-graph can resume from the last checkpoint without re-running completed analysts. Off by default; opt-in via `default_config.py:67`.

**Recommendation:** if hermes-quant ever gets a multi-step deliberative committee that takes >30s end-to-end, add a checkpoint layer. For our current single-symbol `recommend()` path, the checkpoint overhead would be net-negative. **Defer until needed**, but file as a backlog item under ADR-0023.

**Lands in:** future ADR amendment.

---

## Anti-patterns to ENCODE in our defensive doc

These are concrete file:line citations from reference projects that we should explicitly call out in `AGENTS.md` so anyone (human or agent) reading those repos for inspiration knows we reject them:

1. **moon-dev `risk_agent.py:319`** — `self.override_active = "OVERRIDE" in response_text.upper()` — LLM-disabled risk gate. The triple-stacked failure: overridable risk gate + LLM as override authority + string-grep control flow.
2. **moon-dev `trading_agent.py:253`** — `n.ai_entry(token, amount)` fired the moment LLM JSON parses. No HITL, no signed approval.
3. **moon-dev `trading_agent.py:123-124`** — `lines[0].strip()` is the action verb. Free-text → structured command via line-split.
4. **TradingAgents `agents/trader/trader.py`** — `TraderProposal.position_sizing: Optional[str]` (free-text prose sizing) + `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` string-grep contract.
5. **AI-Trader `services.py::_update_position_from_signal`** — 1:1 blind copy-trading. Subscribe-once, all subsequent leader trades cascade.
6. **AI-Trader bare-string token auth** — single token grants both read AND execute scopes; no capability separation.
7. **moon-dev no `as_of` discipline** — agents pull live data without any backtest-time clamp. Look-ahead by default.
8. **moon-dev no audit trail** — recommendations live in in-memory DataFrames reset per cycle. No replayability.

These 8 should land verbatim in `AGENTS.md` under a new section "Anti-patterns we explicitly reject (with citations)". The citations make the doc self-defending: future contributors see exactly which lines of which files we considered and rejected.

---

## Sequencing plan (concrete waves with ADR landing targets)

Mapped against the existing 8-wave plan in `2026-05-24-framework-doc-gap-map.md`. **Where the synthesis changes priorities, I've marked CHANGED.**

### Wave A — Governance plane consolidation (unchanged from gap map)

- **ADR-0031 (Governance plane consolidation)** — `hermes_quant/governance/{audit_log, kill_switch, approvals, promotion}.py`
- Lift Vibe-Trading's static security-scanner pattern (`agent/src/security/scanner.py`) — rejects code containing broker SDK symbols in research mode
- Estimate: 2-4 days

### Wave A.5 — Defensive AGENTS.md update (NEW, low cost, high signal)

- Add "Anti-patterns we explicitly reject (with citations)" section using the 8 entries above
- Include the convergent CV5 finding ("every reference project fails at the trader→approval boundary")
- Estimate: 1 hour. Should ship before Wave B so the agents working on Wave B can't regress us.

### Wave B — Trading Flow Contract (CHANGED — now smaller in scope)

The synthesis surfaces that TradingAgents already has a workable analyst-output schema. Don't reinvent — **lift their `ResearchPlan` / `TraderProposal` / `PortfolioDecision` Pydantic shapes** as the per-stage outputs in our flow contract. The contract YAML stays as gap-map proposed; the per-stage schemas don't need novelty.

- **ADR-0032 (Trading Flow Contract)** — full YAML contract per gap map
- **ADR-0002 amendment** — add `evidence_ids: tuple[UUID, ...]` to AnalystView (CV1)
- **ADR-0008 amendment** — add `kind` discriminator to signal bus (U4)
- Estimate: 1-2 weeks (gap map said 1-2; synthesis didn't move it)

### Wave B.5 — Evidence Store (NEW, was implicit in gap map; promoted)

The synthesis surfaces that evidence store is the load-bearing dependency for everything in Plane 1 (Perception). Without it, news/filings/social analysts have no consumption surface. Promote ahead of Wave C.

- **ADR-0033 (Evidence Store)** — full Pydantic schema + DuckDB+JSONL backend per R4 §6
- CI invariant: extend `tests/test_no_lookahead.py` (CV2)
- Migration sketch: minimum-viable single-table SQLite (R4 §7)
- Estimate: 3-5 days

### Wave C — Perception round-out (unchanged)

- SEC EDGAR + Reddit/X minimal connectors per gap map
- Now consumes Wave B.5's evidence store
- Estimate: 2-3 weeks

### Wave D — Decision fill-out (CHANGED — explicit role list from R1)

- Roles to add (in this order, per R1's role inventory):
  - `analysts/fundamentals.py` (TradingAgents `agents/analysts/fundamentals_analyst.py` is the template)
  - `analysts/news.py` (template: `news_analyst.py`)
  - `analysts/sentiment.py` (template: `sentiment_analyst.py`)
- **ADR-0023 amendments**:
  - Add bull/bear debate stage with routing-level turn cap (U1)
  - Two-tier LLM split: quick for analysts/debate, deep for judge (CV4)
- **ADR-0012 implementation** — LLMAnalyst protocol with message-clear pattern (U5)
- Estimate: 2-3 weeks (gap map said same)

### Wave E — Reaction formalization (unchanged)

- 12-state OMS state machine
- Broker adapter abstraction
- Estimate: 2-3 weeks

### Wave F — Paper Level 3 (shadow mode) (unchanged)

### Wave G — Polymarket (CONSTRAINED per user response)

User confirmed U.S. jurisdiction. **Live trading path closed** until they provide Mullvad VPN + wallet. Available now: data-only adapter for research/backtest. Sequencing:

1. Read-only Polymarket WebSocket adapter under `perception/connectors/polymarket/`
2. Read-only orderbook snapshots → evidence store (Wave B.5 dependency)
3. Backtest-only strategy templates (5 classes from doc)
4. **STOP at backtest** until VPN+wallet provided

This isolates the compliance question from the engineering work. Backtest-only Polymarket research is permissible from any jurisdiction; the live path waits.

### Wave G.5 — Run Cards (NEW, ports Vibe-Trading wholesale)

- **ADR-0034 (Run Cards)** — port `agent/backtest/run_card.py` ~verbatim
- Estimate: 1-2 days. Could parallel with Wave A.

### Wave H — Live trading guarded path (unchanged, deferred)

---

## Concrete artifacts to commit alongside this synthesis

To make the research durable and not "vibes":

1. **This file** at `docs/architecture/2026-05-24-reference-project-synthesis.md` ✓
2. The 6 individual research notes copied from `/tmp/quant-research/outputs/` to `docs/research/`:
   - `2026-05-24-r1-tradingagents.md`
   - `2026-05-24-r2-ai-trader.md`
   - `2026-05-24-r3-vibe-trading.md`
   - `2026-05-24-r4-futuresim-evidence-store.md`
   - `2026-05-24-r5-codex-tradingagents-graph.md` (rename from .txt)
   - `2026-05-24-r6-moon-dev-cautionary.md`
3. Updated `docs/architecture/2026-05-24-framework-doc-gap-map.md` with the wave reordering above (or addendum if we don't want to mutate the original)
4. Updated `AGENTS.md` with the 8 anti-pattern citations (Wave A.5)

---

## Open questions for the user

The synthesis surfaced two new decisions (the gap map's open Qs are still open):

1. **Wave A.5 (AGENTS.md anti-patterns) — ship now or wait?** It's 1 hour of work and prevents future agent confusion. My instinct is **ship now** — it's pure documentation, cannot regress anything, and any subagent reading the codebase tomorrow will be more disciplined.
2. **Wave G (Polymarket) — start the data-only adapter now?** Compliance answer is "data + research is fine." The data-only adapter unblocks 5 strategy templates that would otherwise just sit in the gap map. ~5-7 days of work. Or defer until after Wave A-F lands. My instinct: **defer**, because evidence store (Wave B.5) is a hard dependency anyway.

---

## Workers used (for replayability + cost accounting)

| Worker | Provider | Tool calls | Wall-clock |
|---|---|---|---|
| R1 — Opus 4.7 | OpenRouter Bedrock | 8 | 137s |
| R2 — Gemini 3.1 Pro Preview | OpenRouter | 18 | 80s |
| R3 — Grok 4.3 | OpenRouter | 12 | 78s |
| R4 — DeepSeek V4 Pro | OpenRouter | 18 | 364s |
| R5 — Codex (gpt-5.5) | ChatGPT account | 1 (codex exec) | 150s |
| R6 — Opus 4.7 (Gemini intended; gateway routed to Opus) | OpenRouter Bedrock | 8 | 106s |

**Parallelism:** R1-R4 ran concurrently (~365s); R5+R6 ran concurrently after that (~150s). Total wall-clock to produce 10K words of research: **~9 minutes**.

**Cost estimate:** 4 OpenRouter scatters at ~$0.50 + 1 Codex exec at $0 (ChatGPT account included) ≈ $2-3 in raw spend. Comparable to one human-hour at hermes-quant rates.

**Rerun:** all 6 workers' prompts are recoverable from session log. Cloned source repos at `/tmp/quant-research/sources/` are deterministic for the snapshot date.

---

*Synthesizer: Claude Opus 4.7 via Bedrock, 2026-05-24, post-Wave-1 scatter.*
