# Gap map — Codeseys' framework document vs hermes-quant as built (2026-05-24)

> **Source document:** `message.txt` shared 2026-05-24 — "agent-assisted but execution-deterministic" framework outline with 4 planes, Trading Flow Contract, paper trading levels, staged promotion path, Polymarket section.
>
> **State of repo when this gap map was written:** 30 ADRs (0026–0030 still Proposed pending Phase 4 patches), interim equity-only daily picker live, options layer drafted but not implemented, multi-leg paper reactor schema verified live against Alpaca paper.
>
> **Purpose of this document:** explicit gap analysis so we don't lose the document's ideas to chat history, and so any future reader (or agent) can see what's done vs what's deferred.

## TL;DR

We have ~70% of the document's posture and ~25% of its scope.

- ✅ **Posture is identical** — agents propose, runtime executes; risk gate is deterministic; silence-by-default; HITL approval; reproducibility from disk. The document validates the architectural axis hermes-quant has been on since ADR-0001.
- ⚠️ **Three of four planes are partial** — Perception (~50%), Decision (~60%), Reaction (~30%). The fourth plane the document adds, **Governance**, is currently scattered across other planes rather than consolidated.
- 🔴 **Whole asset classes are absent** — no Polymarket, no on-chain, no order-book replay, no broker-shadow mode, no live-trading path. Equities + crypto bars only.
- 🔴 **The Trading Flow Contract abstraction does not exist yet.** ADR-0030 introduces a methodology DSL but it's a screener-spec, not a full flow-contract spanning perception sources, decision rules, reaction policy, and risk envelope.

The gap is not "we built the wrong thing"; it's "we built phase 1 of a much larger surface area, with the right posture, but the document names many concrete pieces we should now sequence in."

---

## The four planes — what's built per plane

The document proposes:

```
1. Perception   — market data, order books, news, filings, social, account state, feature/evidence store
2. Decision     — research agents + executable policy (DSL/FSM/decision tree)
3. Reaction     — pre-trade risk → OMS → EMS → broker adapter → reconciliation
4. Governance   — audit log, replay/sim, approval, compliance, kill switch
```

We have 3 sections (Perception / Decision / Reaction) — Governance is implicit and scattered. The document's reframing as a fourth explicit plane is sound and we should adopt it.

### Plane 1: Perception — ~50% built

| Doc element | hermes-quant state | Gap |
|---|---|---|
| OHLCV bars (equities) | ✅ `data/yfinance_provider.py` (just hardened today) | yfinance is research-grade, not production. Need polygon.io / alpaca primary, yfinance fallback |
| OHLCV bars (crypto) | ✅ `data/ccxt_provider.py` (ADR-0017) | No live-feed bridge yet — used for backtest only |
| Account state (Alpaca) | ✅ live-probed today, `react/paper.py` | Not unified into a `feature_store` shape — read ad-hoc |
| L1 quotes | ⚠️ Alpaca `OptionChainRequest` returns top-of-book | No L1 store, no replay |
| L2/L3 order book | 🔴 absent | Required for Polymarket + market-making + execution-realistic backtests |
| News / filings | ⚠️ ADR-0022 `semantic.py` has a perception scaffold | No SEC EDGAR connector, no filings parser |
| Reddit / X / social | 🔴 absent | Not even a connector stub |
| On-chain / prediction-market / macro | 🔴 absent | Polymarket WebSocket clients exist (TS/Py/Rust per doc) — not adopted |
| Feature store | ⚠️ analyst-internal, no shared store | `analysts/*.py` each compute features inline; nothing shared, nothing versioned |
| **Evidence store** | 🔴 absent | The doc's `evidence_ids` linkage from intent → underlying news/event is the audit trail we need but don't have |
| `available_at` semantics for replay | ⚠️ partial — bars have `as_of`, news/social path absent | Doc cites FutureSim chronological replay — exact same problem we'd hit for news-driven strategies |

**Highest-leverage gaps to close:**
1. SEC EDGAR connector + filings parser (low effort, high evidence-store value)
2. Polymarket data-only adapter (read-only, sets us up for §"Polymarket strategy classes")
3. Reddit/X scaffolding (most LLM analyst frameworks need this; ours doesn't have it)
4. **Unified feature store** with `available_at` provenance per row — this is the single biggest refactor; it touches every analyst

### Plane 2: Decision — ~60% built

| Doc element | hermes-quant state | Gap |
|---|---|---|
| Research agents (TradingAgents-style fundamentals/technical/sentiment/news/macro/risk/bull/bear/portfolio) | ⚠️ 3 analysts (`classical_ta`, `microstructure`, `kronos`) + `semantic.py` | We have 4 of the document's ~10 analyst roles. Missing: fundamentals, sentiment, news, macro, on-chain, bull-researcher, bear-researcher, portfolio-reviewer |
| Structured analyst output schema | ✅ ADR-0002 `AnalystView` | Matches doc's `{claim, direction, horizon, confidence, evidence, counterarguments}` shape almost 1:1 |
| Aggregator | ✅ ADR-0003 BMA + stacking + ADR-0023 deliberative committee | This is actually ahead of the document — the document just says "ensemble voting"; we have BMA + stacking + planned RL slot (ADR-0006) |
| Decision FSM | 🔴 absent | The doc's FSM example (FLAT → ENTER_PENDING → LONG_UP → EXIT_PENDING) maps to a clean module we don't have |
| Strategy DSL | ⚠️ ADR-0030 Proposed — covers screener-spec, not full flow | Need to extend ADR-0030's three-namespace DSL into the document's fuller "Trading Flow Contract" YAML |
| Target portfolio output | ⚠️ Single-symbol `recommend()` only | Multi-symbol portfolio targeting is absent — no `target_state` dict construction |
| Trade Intent dataclass | ⚠️ Have `Proposal` (single-symbol) + Proposed `MultiLegProposal` (ADR-0029) | Doc's `TradeIntent` is more abstract — covers strategy_id, intent type, evidence_ids, valid_until, risk_tags |

**Highest-leverage gaps to close:**
1. Add fundamentals + news + sentiment analysts (each is a small file once feature store exists)
2. Define `TradeIntent` dataclass per doc §2 spec — then current `Proposal` becomes a refinement of it
3. Extend ADR-0030 DSL beyond methodology-screener into full flow contract (perception sources + decision rules + reaction policy + risk envelope)

### Plane 3: Reaction — ~30% built

| Doc element | hermes-quant state | Gap |
|---|---|---|
| Pre-trade risk gate | ✅ ADR-0004 deterministic gate, ADR-0027 options extension Proposed | Strong here — this is the part of the doc most aligned with what we have |
| Position sizing | ✅ `risk/kelly.py`, ¼-Kelly discrete action space | Per doc P0-1 from synthesis review — the CC denominator bug needs fixing |
| Order construction | ⚠️ `react/paper.py` does Alpaca single-leg; ADR-0029 multi-leg Proposed | Multi-leg verified live today; not yet implemented |
| OMS state machine | 🔴 absent | Doc lists 12 states (PROPOSED → RISK_CHECKED → ... → RECONCILED); we don't track them explicitly |
| EMS / broker adapter routing | ⚠️ Alpaca-only, hardcoded paths | No adapter abstraction; switching brokers means rewriting |
| Cancel/replace logic | 🔴 absent | Live probe today canceled an order; no production cancel/replace logic |
| Fill reconciliation | ⚠️ `react/paper.py` reads Alpaca activities | ADR-0029 D3 calls for 06:00 ET next-day NTA reconcile; not implemented |
| Kill switch | ⚠️ ADR-0004 implies it via DD halts, but no explicit `kill_switch` module | Doc names it as a first-class component; ours is implicit |
| Order rate / cancel rate caps | 🔴 absent | Doc lists these in pre-trade risk; we have notional/loss caps but not rate caps |
| Borrow / margin checks | 🔴 absent | We're cash-only |
| News blackout / earnings blackout | 🔴 absent | Mentioned in ADR-0027 D4 soft warnings but not implemented |

**Highest-leverage gaps to close:**
1. Promote `react/` into a real OMS — formalize the 12-state lifecycle as a state machine
2. Broker adapter abstraction (currently `react/paper.py` is Alpaca-coupled; tomorrow's IBKR/CCXT-broker work will collide otherwise)
3. Explicit `kill_switch` module with deterministic trigger conditions (ADR-0004 already specifies; just needs a home)
4. Earnings/news blackout calendar adapter (read-only data layer — easy unlock for ADR-0027 D4 soft warnings)

### Plane 4: Governance — ~15% built (mostly scattered, not consolidated)

This is the document's most novel addition. We have pieces but no `governance/` module.

| Doc element | hermes-quant state | Gap |
|---|---|---|
| Immutable audit log | ⚠️ `journal/` has settlement-journal markdown; signal-bus JSONL append-only | Settlement journal ≠ full audit log. No append-only "what did the system know, when, and what did it decide" log spanning all four planes |
| Backtest / replay / paper | ✅ `backtest/`, `react/paper.py`, ADR-0019 evaluation, ADR-0020 backtest harness | Solid here, but we lack the doc's "Run Card" YAML artifact emitted per backtest |
| Human approval / kill switch | ✅ ADR-0015 HITL propose-decide-react, ADR-0016 autonomous-mode silence-bias | Approval flow exists; kill-switch is implicit (see Reaction gap above) |
| Compliance / permissions | 🔴 absent | Doc cites SEC/FINRA Rule 15c3-5 pattern; we have no permissions layer at all |
| Promotion policy | 🔴 absent | Doc's staged promotion (Backtest → Walk-forward → Paper → Shadow → Canary → Limited Live → Scaled Live) with deterministic rules has no equivalent yet |
| Run Card artifact | 🔴 absent | Each backtest should emit a versioned YAML with strategy_hash, data_snapshot_hash, code_version, fees_model, latency_model, etc. |

**Highest-leverage gaps to close:**
1. Create `hermes_quant/governance/` module — consolidate audit_log + kill_switch + approvals (currently scattered) into one plane
2. Add Run Card emission to the backtest harness (`backtest/run_card.py`) — small file, big audit-trail unlock
3. Promotion policy YAML — formalize the doc's `promotion_policy:` block as a checked invariant that `react/` consults before promoting a strategy from one stage to the next
4. **Audit log spanning all 4 planes** — currently each plane logs to its own files; one append-only event log across the system is the FutureSim-style replay invariant

---

## Trading Flow Contract — the missing core abstraction

The document's biggest single addition is the **Trading Flow Contract** YAML. ADR-0030 introduces a methodology DSL (three namespaces: fundamentals / options_chain / event_flags), but the doc's Flow Contract is broader:

```yaml
flow_id: ...
mode_allowed: [backtest, paper, live_guarded]   # ← we don't have this
universe: ...                                    # ← partial via ADR-0030
perception:
  feeds: [...]                                   # ← we don't enumerate feeds per flow
  features: [...]                                # ← per-flow features (not in ADR-0030)
decision:
  type: finite_state_machine                    # ← FSM doesn't exist in our codebase
  states: [...]
  rules: [...]
reaction:
  order_policy: ...                              # ← we have global, not per-flow
  risk: ...                                      # ← we have global, not per-flow
```

**Recommendation:** ADR-0030 currently scopes a screener-spec DSL. Extend it (or add ADR-0031) to a full Trading Flow Contract that wraps a screener PLUS an FSM PLUS a per-flow risk envelope PLUS a `mode_allowed` enum. The document's YAML is well-shaped; we can adopt it almost verbatim with our existing analyst protocol as the underlying `feature: ...` reference target.

This becomes the artifact format every downstream piece (validator, compiler, simulator, runtime, agent SDK) consumes.

---

## Polymarket — entirely deferred

The document has a substantial Polymarket section (5min/15min crypto, CLOB API, WebSocket feed shape, fee curve, jurisdiction restrictions, 5 strategy classes). hermes-quant has zero Polymarket support today. This is a wholly net-new direction.

**Important blocker — jurisdiction restrictions** (cited in doc §"Polymarket / Polymarket Agents README"): Polymarket's terms prohibit U.S. persons from trading via UI or API including agents developed by persons in restricted jurisdictions. **This must be checked and documented before ANY Polymarket connector is built**, even read-only data ingestion, even though "data is viewable globally." If you're a U.S. person, the live-trade path is closed; the read-only data + research-paper path may still be open. This is a posture/compliance gate we haven't navigated.

If/when Polymarket lands, the right sequencing is:
1. **Compliance check first** — confirm what's permissible from your jurisdiction
2. **Data-only adapter** — read-only ingestion into the perception plane (`perception/connectors/polymarket/`)
3. **Backtest-only strategy templates** — the doc's 5 strategy classes (directional / market-neutral / latency-arb / copy / event-tremor) implemented as backtest-only flow contracts
4. **Paper trading via shadow mode** — generate orders internally, never submit
5. Live submission gated by jurisdiction confirmation + Phase 4 of doc's deployment path

This is plausibly its own epic (3–6 months of work) — defer until equity options are stable.

---

## Paper Trading Levels — we're at L0 + half of L1

The document defines 5 levels of paper trading fidelity:

| Level | Description | hermes-quant state |
|---|---|---|
| L0 | Synthetic fills from OHLCV bars | ✅ have this in `react/paper.py` |
| L1 | Quote-aware fills (live bid/ask, marketable limits, spread, fees, slippage) | ⚠️ partial — Alpaca paper does this for us via real-time simulation, but we don't model it ourselves for backtests |
| L2 | Order-book replay (queue position, latency) | 🔴 absent — required for Polymarket and HFT-like strategies |
| L3 | Broker shadow mode (live data, generate but don't submit, drift report) | 🔴 absent — doc names this as the bridge to live; we don't have it |
| L4 | Tiny live canary | 🔴 absent — explicit non-goal until 60+ days paper per ADR-0029 D7 |

**Recommendation:** L3 (shadow mode) is the highest-leverage missing rung — it lets us measure paper-vs-live drift without putting real money on the line, which is exactly what the document's promotion policy needs to graduate strategies from paper to canary.

---

## Staged promotion — entirely absent

The document's promotion policy (Research → Proposal → Schema-validated → Backtest → Walk-forward → Paper → Shadow → Canary → Limited Live → Scaled Live, with deterministic rules at each gate) has no equivalent in hermes-quant. We have ADR-0029 D7's "60 days paper before live" gate, but no broader promotion machinery.

**Recommendation:** add this as a new ADR (ADR-0031 candidate) — the promotion policy is a deterministic governance artifact that consumes Run Cards + paper-vs-shadow drift reports + risk-breach counts and emits promote/reject decisions. Maps cleanly onto ADR-0026's retrospective amendment loop (which can propose promotion-policy parameter tweaks but not changes to the framework itself).

---

## Reference projects — what to copy from each

The document cites 7 projects and 1 paper. Quick map of where each fits in our roadmap:

| Project | Role in our framework | Status |
|---|---|---|
| **TradingAgents** (TauricResearch) | Reference for the missing analyst roles (fundamentals / sentiment / news / macro / bull / bear / portfolio-reviewer). Per the doc, do NOT let the trader-agent decide trade size — keep our existing aggregator + risk gate as the authority | 📚 Reference only — copy role decomposition, not execution path |
| **Vibe-Trading** (HKUDS) | Reference for "research workspace" pattern — natural-language research, run cards, shadow account analysis. Their explicit "does not execute live trades" boundary IS our boundary | 📚 Reference only — copy run card format + research-vs-execution split |
| **AI-Trader** (HKUDS) | Reference for agent-skill onboarding + signal marketplace — useful for ADR-0030 from-reel methodology pipeline if we ever expose methodologies as a registry | 📚 Reference only — defer agent-marketplace surface to v0.7+ |
| **QuantDinger** | Reference for separation of services (AI / backtest / strategy / execution as separate processes) | 📚 Reference only — our sidecar architecture already mirrors this |
| **Kronos** (shiyu-coder) | One specific perception/forecasting model. ADR-0018 already integrates Kronos as one analyst voice | ✅ Done — use as designed (one of N analysts, not the oracle) |
| **moon-dev-ai-agents** (yolojewjitsu) | Not yet examined in any ADR — should review for posture lessons (it's an "AI does the trading" project per name; treat as cautionary) | 📚 Read once for lessons-learned |
| **FutureSim** (arXiv 2605.15188) | The `available_at` chronological-replay principle. Should formalize as an invariant across the perception plane | 📚 Reference — adopt the timestamp-discipline as a hard rule (not just a comment in `validate_bars`) |
| **NautilusTrader** | Strong candidate for L2 paper-trading + execution-realistic backtest engine. Same core engine spans backtest + live, which matches our ADR-0001 sidecar posture | 📚 Reference — evaluate as backtest engine when L2 paper lands |
| **vectorbt** | Already in pyproject? Used for fast research backtests; complementary to Nautilus's event-driven approach | 📚 Reference — use for research backtest, not execution-realistic |
| **hftbacktest** | Best fit for Polymarket / order-book-replay strategies. Defer until Polymarket lands | 📚 Reference — defer |
| **LEAN (QuantConnect)** | Reference for asset/broker-specific fill / margin / slippage / fee models | 📚 Reference — adopt model-shape patterns (FillModel, MarginModel etc.) when broker abstraction lands |

---

## What I'd actually sequence

If we want to move toward the document's full vision while staying executable:

### Wave A — Governance plane consolidation (low effort, high posture-correctness)

Single new module + ADR. Purely consolidates what's scattered:

- `hermes_quant/governance/audit_log.py` — append-only event log spanning all 4 planes
- `hermes_quant/governance/kill_switch.py` — explicit module with documented trigger conditions
- `hermes_quant/governance/approvals.py` — refactor of HITL flow currently in `proposals.py` + `cli/`
- `hermes_quant/governance/promotion.py` — new, encodes the doc's promotion policy
- ADR-0031 (Governance plane consolidation)

Estimate: 2-4 days of work. Doesn't add new features; just makes the architecture honest about what the doc names as a fourth plane.

### Wave B — Trading Flow Contract (the core abstraction)

- Extend ADR-0030 OR add ADR-0032: full Trading Flow Contract YAML schema
- `hermes_quant/contracts/{schema.py, validator.py, compiler.py}` — Pydantic models + validation + compilation to runtime objects
- Migrate existing recipes (`recipes.py`) to be flow-contract-compliant
- Run-card emission tied to flow-contract execution

Estimate: 1-2 weeks. This is the load-bearing abstraction the rest of the document hangs on.

### Wave C — Perception plane round-out

- SEC EDGAR connector (`perception/connectors/edgar/`)
- Reddit + X minimal connectors (read-only, scaffolding for ADR-0022 semantic perception)
- Unified feature store with `available_at` provenance per row
- Evidence store (intent → underlying event_id linkage)

Estimate: 2-3 weeks. Highest-leverage sequence for new analysts.

### Wave D — Decision plane fill-out

- Fundamentals analyst, news analyst, sentiment analyst, macro analyst (each a small file once feature store exists)
- Bull-researcher / bear-researcher (debate variant of ADR-0023 deliberative committee)
- Portfolio-reviewer (coordinator across all analysts on multi-symbol target state)
- FSM module for explicit decision state transitions
- `TradeIntent` dataclass (generalization of current `Proposal`)

Estimate: 2-3 weeks. Mostly compositional once Wave C lands.

### Wave E — Reaction plane formalization

- 12-state OMS state machine in `react/oms.py`
- Broker adapter abstraction (move Alpaca-specific code out of `react/paper.py`)
- IBKR adapter (cite-only — implement when needed)
- Earnings/news blackout calendar adapter
- Order-rate / cancel-rate caps in risk gate

Estimate: 2-3 weeks.

### Wave F — Paper Level 3 (shadow mode)

- New `react/shadow.py` — generates orders, never submits, captures hypothetical fills
- Drift report comparing shadow output to live market opportunities
- Promotion-policy hook (paper → shadow gate)

Estimate: 1-2 weeks.

### Wave G — Polymarket (deferred until compliance + Wave A-F)

- Compliance check and documentation FIRST
- Read-only data adapter
- Strategy templates (5 classes from doc)
- Order-book replay backtest engine (`hftbacktest` or homegrown)

Estimate: 1-3 months once unblocked.

### Wave H — Live trading guarded path

- Tiny canary deploys
- Signed deployment approvals
- Hardware key / vault for order signing
- Live-paper drift kill conditions

Estimate: months. Not a near-term goal.

**Total: A through F is realistically 3-4 months of focused work, and gets us to ~75% of the document's full vision with the right posture for the rest.**

---

## What's NOT in the document but should be on our radar

Things hermes-quant has decided that the document doesn't address:

- **Retro loop architecture (ADR-0026)** — the document doesn't mention self-improvement loops; ours does. Keep this.
- **Methodology from-reel ingestion (ADR-0030 D3)** — the document doesn't cover the case where the strategy comes from a video/audio source with verbal-only rules. This is hermes-quant-specific and worth keeping.
- **Calibrator immutability** — the document doesn't explicitly call out that calibrators cannot be touched by the retro loop; ours does (ADR-0026).
- **Cross-family adversarial review for ADRs** — the doc's promotion policy mentions "no_lookahead_findings: true" but doesn't propose multi-model adversarial review of ADRs themselves. Our Phase 3 cross-family scatter is hermes-quant-specific governance worth keeping.

---

## Open questions for the user

1. **Polymarket compliance** — what's your jurisdiction? If U.S., the live-trade path is closed; only data + research is permissible. If non-U.S., the full path is open subject to verification.
2. **Wave priority** — Wave A (Governance consolidation) is low-cost and high-posture-correctness; should I do it before Phase 4 of the original options plan, or after? My instinct is BEFORE because Phase 4's ADR patches will benefit from a Governance plane to anchor in.
3. **Trading Flow Contract scope** — extend ADR-0030 (which is screener-only) into the full contract, or write a new ADR-0031 that supersedes? My instinct is new ADR; ADR-0030 stays as the methodology-screener variant which is one component of a Flow Contract.
4. **Reference projects to actually clone & study** — the doc cites 7. We've already been informed by TradingAgents (ADR-0023 deliberative committee patterns) and Kronos (ADR-0018). Worth pulling vibe-trading + AI-trader source for a single architecture-doc pass to lift run-card and broker-adapter patterns? ~4 hours of focused reading.

---

## Files this gap-map references

- `docs/plans/2026-05-23-options-daily-retro.md` — the Phase-2 options + retro plan (different scope, narrower)
- `docs/research/2026-05-24-r5-alpaca-live-probe.md` — verifies the Reaction-plane Alpaca path
- `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` — Phase-3 review of options ADRs
- This file: `docs/architecture/2026-05-24-framework-doc-gap-map.md`

---

*Authored by orchestrator (Claude Opus 4.7 via Bedrock), 2026-05-24. Source document at `~/.hermes/cache/documents/doc_1a46a8507598_message.txt`.*
