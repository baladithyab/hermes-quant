# ADR-0079: Unified Perception → Decision → Reaction architecture + signal-source unification

**Status:** Proposed
**Date:** 2026-05-30
**Wave:** Capstone (organizing architecture; ratifies the PDR model, builds nothing here)
**Supersedes:** nothing
**Cites:** [ADR-0002](ADR-0002-analyst-protocol.md) (analyst protocol contract — the PERCEPTION→DECISION peer-view shape), [ADR-0003](ADR-0003-aggregator.md) (BMA aggregator — deterministic fusion), [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate, silence-by-default — the FINAL authority), [ADR-0063](ADR-0063-regime-in-marketcontext-extras.md) (regime as a perception conditioner via `extras`), [ADR-0074](ADR-0074-catalyst-sense-semantic-fusion.md) (catalyst semantic-numerical fusion — semantic enters BMA as a peer), [ADR-0076](ADR-0076-social-arbitrage-integration.md) (social-arbitrage consumer-trend class, ×0.5 haircut, profitability-verification loop), [ADR-0077](ADR-0077-pretrade-admissibility-shortability.md) (pre-trade admissibility = the REACTION-layer fidelity gate, REJECT-only, monotonically non-increasing)
**Grounded in:** `docs/design/pdr-unified-architecture.md` (the detailed spec this ADR ratifies); `docs/research/2026-05-30-r-pdr-current-pipeline-audit.md`, `docs/research/2026-05-30-r-pdr-ai-hedge-fund.md`, `docs/research/2026-05-30-r-pdr-vibe-trading.md` (the three PDR research notes); `docs/research/2026-05-30-understanding-codebase.md` (live pipeline) and `-understanding-wiki.md` (vision/posture).

> **This ADR ratifies an organizing architecture; it does not build it.** The four new abstractions it implies (`PerceptionFrame`, `TrendVelocity`, `ConvergenceValidator`, `SaturationScore`) are scoped as *future waves* in Rollout §, each default-OFF and eval-gated. With every new flag OFF, the pipeline is bit-for-bit today's 3-analyst numerical ensemble → BMA → deterministic gate → silence-bias → paper. The detailed spec is `docs/design/pdr-unified-architecture.md`.

---

## Context

The operator's stated north-star (2026-05-30) is **one coherent Perception → Decision → Reaction (PDR) pipeline** that unifies *all* signal sources — social-arbitrage, semantic/catalyst, numerical (classical TA / microstructure), Kronos (foundation model), and fundamentals — as data points entering a single pipeline, with the Hermes engine helping orchestrate. The operator names five stages that group into the three PDR phases: **SCANNING + ANALYSIS = Perception**, **DELIBERATION + RISKING = Decision**, **ACTING = Reaction** (paper for now).

### How the system actually got here

hermes-quant grew **signal-by-signal**, each new source bolted on as an incremental BMA *peer view* rather than as part of a designed perception layer:

- The DECISION and REACTION layers are already clean and rails-compliant: `BMAAggregator.aggregate` (`aggregators/bma.py:305`) fuses peer `AnalystView`s with `require_ensemble` (`n_distinct_analysts <= 1` → `silenced_single_source`, `bma.py:498-519`); `DefaultRiskGate` (`risk/gate.py`) is the final deterministic sizing authority (ADR-0004); `PaperReactor.execute` (`react/paper.py:54`) executes mechanically.
- Catalyst awareness (ADR-0074) added a semantic *producer* (ingest → classify → propagate → synthesize) feeding a semantic *consumer* analyst (`semantic.py:75`) that enters BMA as a peer behind `HERMES_QUANT_SEMANTIC_ENABLED` (default 0).
- Social-arbitrage (ADR-0076) reused that same producer path (Reddit/Trends → `CatalystItem` → classify → propagate), with a ×0.5 consumer-trend haircut (`synthesize.py:53-66`), making social-arb a deliberately *weak* peer view pending a profitability loop.

Three structural problems follow directly from this incremental, "every signal is another BMA peer" growth:

1. **There is no explicit PDR contract.** Signals enter the decision layer through three disjoint side-channels — numerical via `MarketContext.bars`, regime via `ctx.extras["regime"]` (ADR-0063), semantic via `ctx.extras["semantic_packets"]`. There is no single provenance-carrying object that *is* "everything perceived about this symbol at asof." The perception boundary — the PDR phase the operator most wants to make first-class — has no typed name (audit GAP-E).

2. **Semantic packets reach only 1 of 3 live decision paths (a decoupling bug).** `load_packets_for` is wired only into `quant-daily-interim.py:127-141`; `autonomous.tick` (`autonomous.py:355-360`) and `quant-playbook-tick.py:465` call `recommend()` with **no `market_extras`**, so with the flag ON the semantic analyst silently abstains `no_semantic_packets` (`semantic.py:156`) on 2 of 3 paths. The flag and the wiring are decoupled (audit GAP-D).

3. **Social-arb's real edge is not captured, because it was bolted on as "another analyst" rather than as a perception-layer method.** The Camillo social-arbitrage method (DETECT → VALIDATE → LINK → ACT → EXIT, ~77%/yr verified) is fundamentally a *perception discipline* — how you sense a real consumer trend before Wall Street. Today the system only does LINK (the curated propagation graph). It lacks:
   - **DETECT = trend VELOCITY** — interest *accelerating* week-over-week above its own baseline. `classify_headline` is severity-on-keywords on a single headline (`classify.py:128-139`); `social.py` only *frames* synthetic headlines with velocity words that still flow into the same severity classifier — zero acceleration logic exists (audit GAP-A).
   - **VALIDATE = data CONVERGENCE** — a trend is real only across MULTIPLE independent sources. `require_ensemble` lives only at the *decision* layer (cross-ANALYST, `bma.py:498-519`); at perception a lone Reddit packet and a lone Trends packet are merely deduped/collapsed, never validated against each other (audit GAP-B).
   - **EXIT on information SATURATION** — sell when Wall Street catches up; the edge is *time-decaying* information asymmetry. The system sizes purely on confidence × Kelly and has no concept of remaining edge-time (grep: 0 hits for `saturation`/`edge_decay`/`information_parity` in `hermes_quant/`, audit GAP-C).

The operator wants ONE coherent PDR pipeline that treats every source as a data point and expresses the social-arbitrage method honestly — **without weakening a single rail.** This ADR ratifies the architecture that does so; the design doc specifies it.

---

## Decision drivers

- **D-1 Silence-by-default; the deterministic gate is the FINAL authority.** Uncertainty → cash. `DefaultRiskGate` (ADR-0004) decides the legal discrete size; LLM / committee / semantic / social are **EVIDENCE** that can only *silence* (multiply confidence toward 0.0), never amplify, override, or authorize. The architecture must keep authority *concentrated* at the gate.
- **D-2 `require_ensemble`: no signal fires alone.** No single-source candidate fires (`bma.py:498-519`). The architecture must preserve this and ideally make the social-arbitrage edge *strengthen* this discipline (a second, cross-SOURCE ensemble requirement at perception), never relax it.
- **D-3 Default-OFF + eval-gated.** Every new capability ships behind a `HERMES_QUANT_*` flag, default OFF, promoted only after it clears its eval gate and the operator audits a live side-by-side. With all flags OFF, behavior is byte-identical to today.
- **D-4 Lookahead honesty (`asof` = publication/decision time, always; all times UTC).** Every perception primitive must stamp `asof` and read only past observations; the no-lookahead gate must extend to any new perception path.
- **D-5 Additive, rails-preserving evolution.** The analyst Protocol (ADR-0002), BMA (ADR-0003), and the gate (ADR-0004) must not change shape. `protocol.py` versioning is add-only; new abstractions compose additively (a new optional kwarg, a new typed container projected into the existing `MarketContext`), never a rename or removal.
- **D-6 Discrete sizing is untouched.** The `{0, ±0.05, ±0.10, ±0.15, ±0.20}` × NAV ladder is not widened. New perception evidence can only move conviction *down* the existing ladder (including to 0 = silence/flatten), never introduce a new sizing surface. Money via CLI only, never tools.
- **D-7 Reproducibility / provenance.** Every signal must be replayable from disk; the execution record is the audit trail. A unified perception boundary should carry provenance (ADR-0033/0041) so evidence-store linkage flows from one place.
- **D-8 One coherent picture over many side-channels.** The operator wants signal-source unification, not five disjoint entry points. The architecture should give the perception boundary a single typed contract so the flag/wiring decoupling (GAP-D) becomes structurally impossible to reintroduce.

---

## Considered options

### Option A — Ratify an explicit PDR architecture: a unified `PerceptionFrame` + perception-layer trend/convergence/saturation primitives, signal sources mapped to stages, migrated in default-OFF increments (CHOSEN)

Adopt **Perception → Decision → Reaction** as the *organizing architecture* of the whole system, and ratify the `docs/design/pdr-unified-architecture.md` spec:

- **Perception** (SCANNING + ANALYSIS): senses the world and produces *evidence* with no authority. It emits a typed, provenance-carrying `PerceptionFrame` — "everything perceived about one symbol at asof" — that is projected into the existing `MarketContext` so every analyst reads it unchanged. Three new perception primitives express the Camillo method honestly: `TrendVelocity` (DETECT, GAP-A), `ConvergenceValidator` (VALIDATE = cross-SOURCE `require_ensemble` at perception, GAP-B), `SaturationScore` (the EXIT-on-parity edge-decay estimate, GAP-C).
- **Decision** (DELIBERATION + RISKING): analysts → BMA → optional LLM committee → the deterministic gate, which remains the FINAL authority. A `[SATURATE]` step applies the saturation estimate as a confidence multiplier `m ∈ (0, 1]` *before* the gate — silence-only by construction (post-saturation confidence ≤ pre, pinned by a property test).
- **Reaction** (ACTING): paper react plus the ADR-0077 admissibility / fidelity gate (REJECT-or-flatten only).
- Social-arbitrage is recognized as a **perception-layer method whose scored output enters Decision as a capped peer view** — its two roles (perception producer vs decision analyst) kept explicitly distinct so authority never moves to the social signal; only the *quality of the evidence it carries* improves.
- Everything migrates in **default-OFF, eval-gated increments**. The in-flight waves (catalyst-wiring fix, admissibility, options foundation) are recognized as early PDR steps; the four new abstractions are *future waves*, not built here.

- **Pros:** Gives the operator the single coherent PDR pipeline they asked for, with all sources mapped to a stage. Makes the perception boundary a first-class typed contract (kills GAP-E) and makes GAP-D *structurally* impossible (one populated input, not three side-channels). Captures social-arb's real edge (velocity / convergence / saturation) as honest evidence *without* weakening a rail — the gate stays final, the ladder is untouched, `require_ensemble` is *strengthened* into two layers. Additive and reversible: with flags OFF, byte-identical to today. Cohere with — does not contradict — the in-flight waves. Strong external precedent for the safe seams (envelope-then-select from ai-hedge-fund; grounding + cite-or-die from Vibe-Trading; the 5-phase deliberation map from TradingAgents).
- **Cons:** Refactor risk: threading a new central object through `recommend()` and three crons is a real change surface, even when default-OFF. `PerceptionFrame` becomes a new central contract that could *ossify* (every future signal must fit it). The three perception-layer scores add eval surface (each needs its own gate before it can influence anything). The architecture is a multi-wave commitment, not a one-shot fix; it must be paced so it never blocks the agreed fidelity-first sequencing (ADR-0077 before options).

### Option B — Keep the current ad-hoc "every signal is a BMA peer view" approach; just fix the wiring bug

Leave the architecture implicit. Fix GAP-D tactically with one shared `semantic_market_extras` helper called by all three decision paths (Wave C2-2). Continue adding any future signal (velocity, convergence) as more BMA peer views or more `extras` keys, as needed, with no overarching contract.

- **Pros:** Cheapest and lowest-risk; the wiring fix alone closes the most acute live bug. No new central abstraction to maintain or ossify. Zero refactor of `recommend()`. Keeps optionality — nothing is committed architecturally.
- **Cons:** Leaves the perception boundary an opaque, growing `extras` dict (GAP-E persists); the flag/wiring decoupling can recur the next time a signal is added on a new path. Crucially, it does **not** capture social-arb's real edge: velocity/convergence/saturation have no natural home as "another BMA peer," so the method stays severity-on-keywords — exactly the bolted-on framing that the audit identified as the root cause. The operator's stated north-star (one coherent PDR pipeline, sources unified) is not delivered. Technical debt compounds signal-by-signal.

### Option C — Heavier full agent-DAG (SwarmRuntime) rewrite

Replace the deterministic pipeline with a multi-agent DAG executor in the style of Vibe-Trading's `SwarmRuntime` / TradingAgents' LangGraph: agents as graph nodes, topological layers run in parallel, fusion by qualitative summary-passing (`SwarmTask.input_from`), an LLM trader/PM as the arbitration node.

- **Pros:** Maximal flexibility and parallelism; trivially extensible to new signal "agents"; matches the most-studied external architectures; would make the deliberation phase visually explicit.
- **Cons:** **Directly violates the core rails.** It replaces the deterministic `BMAAggregator` with qualitative summary-passing (no weighted fusion, no `require_ensemble`), and it tends to make an LLM the fusion/arbitration authority — the *convergent failure mode* every reference repo exhibits and that AGENTS.md explicitly rejects ("Don't add LLMs to the action path. Ever."). Enormous rewrite risk for a single-operator paper system; throws away the clean, rails-compliant DECISION/REACTION layers that already work. Rejected on rails grounds alone.

---

## Decision

**Ratify Perception → Decision → Reaction as the organizing architecture of hermes-quant (Option A).** The `docs/design/pdr-unified-architecture.md` spec is the detailed design; this ADR is its ratification.

### D79.1 The three PDR phases and the authority invariant

| PDR phase | Operator stage(s) | What happens | Authority |
|---|---|---|---|
| **PERCEPTION** | SCANNING + ANALYSIS | Sense the world: select symbols, fetch bars, build regime, detect trends, validate across sources, read each analyst's view. Emit a `PerceptionFrame` of *evidence*. | none (pure sensing) |
| **DECISION** | DELIBERATION + RISKING | Fuse evidence into one signal (BMA), apply the saturation multiplier, then the deterministic gate computes the legal discrete sizing envelope. An optional LLM committee may select *within* the envelope. | **deterministic gate is FINAL** |
| **REACTION** | ACTING | Convert the gated Action into a (paper) order, fill it with live fidelity (incl. ADR-0077 admissibility), record it. | deterministic; paper for now |

**The defining invariant:** authority *concentrates monotonically* from PERCEPTION (no authority — everything is evidence) → DECISION (the gate is the single authority) → REACTION (mechanical execution of an already-authorized Action). Evidence can only *subtract* (silence). Nothing downstream of the gate can re-introduce a silenced signal. This invariant is the architecture's contract; it is the formal statement of D-1, D-2, D-6.

### D79.2 Perception emits a typed `PerceptionFrame`; Decision and Reaction are unchanged in authority

Perception is made first-class via a single `PerceptionFrame` — `{symbol, asof, bars, last_close, regime, semantic_packets, trend_velocity, convergence, saturation, provenance, extras}` — built **once** per symbol, then projected into the existing `MarketContext` by a pure `frame_to_context` adapter. The analyst Protocol (ADR-0002), `BMAAggregator` (ADR-0003), and `DefaultRiskGate` (ADR-0004) **do not change**: BMA still fuses peer `AnalystView`s under `require_ensemble`; the gate is still final. `recommend()` gains one optional `perception_frame=None` kwarg that, when `None` (every backtest + today's default), builds the frame internally — byte-identical to today; when provided (the three live crons hand in one frame from one loader), GAP-D cannot recur. `PerceptionFrame` is a **container, never an authority**.

### D79.3 Social-arbitrage is a PERCEPTION method whose scored output enters DECISION as a capped peer

The two roles are kept explicitly distinct:

- **Role 1 — PERCEPTION method (the part the system lacks):** DETECT (`TrendVelocity`), VALIDATE (`ConvergenceValidator` = cross-SOURCE `require_ensemble`), LINK (the existing propagation graph). These *improve the quality of the evidence*.
- **Role 2 — DECISION analyst (the part that already works):** the `HermesSemanticAnalyst` consumes a finished packet and emits one `AnalystView` — a peer bounded by `require_ensemble`, the ×0.5 consumer-trend haircut (ADR-0076), and the gate.

Conflating these is the root cause of GAP-A/GAP-B. The fix builds the *perception* side honestly **without** changing the *decision* side. **Two independent ensemble requirements result, at two layers:** cross-SOURCE convergence at perception (*is this trend real?*) and cross-ANALYST agreement at decision (*do my independent models concur?*). A social-arb signal must clear *both* to fire. Authority never moves to the social signal.

### D79.4 Information-saturation is silence-only, applied before the gate

The Camillo EXIT-on-parity is expressed as a `SaturationScore` confidence multiplier `m ∈ (0, 1]` applied to `AggregatedSignal.confidence` in a `[SATURATE]` step **before** the gate. It can only pull conviction toward 0 (decaying edge → quieter → eventually silenced/flattened by the gate's cost/edge floor) and can **never raise** confidence — pinned forever by a property test (post-saturation ≤ pre-saturation, for every input). It is **not** a new sizing ladder: it only moves conviction *down* the existing discrete ladder (e.g. `0.20` decays to `0.10`, then to `0` = silence/flatten). This is the *exact* same authority boundary as catalyst-as-evidence-never-authority and as the ADR-0077 admissibility monotonicity (target magnitude monotonically non-increasing).

### D79.5 Migration is default-OFF and eval-gated; nothing new is built in this ADR

The four new abstractions (`PerceptionFrame`, `TrendVelocity`, `ConvergenceValidator`, `SaturationScore`) are scoped as future waves in Rollout §. Each is a perception-layer, evidence-only, default-OFF, eval-gated addition. With every flag OFF, the pipeline is bit-for-bit the current 3-analyst numerical ensemble → BMA → deterministic gate → silence-bias → paper.

### D79.6 What we adopt from the studied repos (and explicitly reject)

- **ai-hedge-fund:** adopt the **deterministic-envelope-then-LLM-select** pattern (`compute_allowed_actions`) and the **explicit risk-limit object handed downstream** — the cleanest external precedent for "gate first, LLM only selects inside the capped envelope." **Reject** the LLM as fusion/arbitration authority and `short`/`cover` as first-class actions.
- **Vibe-Trading:** adopt the **pre-reasoning Data-Grounding Block + Citation HARD RULE** (operationalizes the lookahead rail at the prompt layer) and the **Hypothesis Registry / `link_backtest` ledger**. **Reject** the LLM-agent-as-decision-maker / qualitative summary-passing fusion (keep fusion in `BMAAggregator`).
- **TradingAgents:** adopt the **5-phase shape as a mental map** for the deliberation sub-stages and the structured-output discipline. **Reject** the trader/PM LLM as final authority and the free-text / string-grep decision contracts.

**Convergent rejection:** every reference repo lets an LLM be the final execution authority somewhere; the deterministic-gate + HITL pattern is the inverse of that failure mode, and this ADR makes that inversion the explicit, ratified architecture.

---

## Consequences

**Positive:**
- The operator gets the stated north-star: one coherent PDR pipeline with every signal source mapped to a stage and unified as data points.
- The perception boundary becomes a first-class typed contract (`PerceptionFrame`, GAP-E), and the catalyst flag/wiring decoupling (GAP-D) becomes *structurally* impossible once the frame lands.
- Social-arb's real edge becomes expressible as honest evidence: velocity (DETECT), cross-source convergence (VALIDATE), saturation (EXIT) — *strengthening* the ensemble discipline into two layers rather than weakening any rail.
- Every rail is preserved and made explicit: silence-by-default, the deterministic gate as final authority, `require_ensemble` at two layers, the discrete ladder, `asof` honesty end-to-end.
- The in-flight waves are given a coherent place in the architecture (early PDR steps), so the team stops growing the system signal-by-signal without a map.
- The architecture is additive and reversible; with all flags OFF it is byte-identical to today.

**Negative / risks (real downsides):**
- **Refactor risk.** Threading `PerceptionFrame` through `recommend()` and the three crons is a real change surface even when default-OFF; a bug in the frame-builder could silently diverge a live path from the backtest. Mitigated by the PDR-1 replay test (frame path byte-identical on all fixtures) and the no-lookahead gate, but the risk is not zero.
- **The `PerceptionFrame` is a new central contract that could ossify.** Once every live path threads one frame, the frame's shape becomes load-bearing; a poorly chosen field set could force awkward future signals to contort to fit it, or invite a churn of schema migrations. Mitigated by the add-only versioning rule and the `extras` escape hatch, but central contracts are sticky by nature.
- **Perception-layer scores add eval surface.** Each of velocity / convergence / saturation needs its own eval gate, labeled data, and a property/replay test before it can influence anything. This is more validation work than "one more BMA peer," and the gates (e.g. PDR-3 needs a larger labeled social-arb set, B09) may take time to clear — so the honest social-arb edge is *latent* until the eval bars are met.
- **Multi-wave commitment.** This is an organizing architecture, not a single fix; it must be paced so it never blocks the agreed fidelity-first sequencing (ADR-0077 admissibility before the options rail). If under-prioritized, the architecture risks being ratified-but-not-realized — a doc without code.
- **Saturation is a genuinely hard estimate.** "Past the velocity peak" / "Wall Street has caught up" is noisy; a mis-calibrated decay multiplier could silence good positions early (lost edge) even though it can never *over*-size. The silence-only boundary bounds the *direction* of the error, not its cost.
- **Cross-source convergence depends on real producers.** `ConvergenceValidator` is only as honest as the independence of its sources; if two "independent" feeds actually share an upstream, convergence is illusory. Source-family taxonomy must be policed.

**Out of scope (future amendments):**
- The concrete implementations of all four abstractions (each its own future wave with its own ADR or wave plan).
- Real Reddit/Trends/web-traffic producers behind the velocity/convergence primitives (B08/B09).
- Any change to the discrete sizing ladder, the analyst Protocol, the BMA contract, or the gate's authority — explicitly *not* in scope; the architecture preserves them.
- Live execution; REACTION stays paper until the ADR-0077 fidelity foundation and a separate live-promotion decision.

---

## Rollout

Everything below is additive and default-OFF. **No new abstraction is built in this ADR.** The first PDR step is already in flight; the four new abstractions are scoped as future waves.

### The PDR steps already in flight (cohere with these; do not contradict)

| In-flight work | PDR role | Status |
|---|---|---|
| **Wave C2-2** — shared `semantic_market_extras` helper wired into all 3 decision paths | The first PDR step: makes the PERCEPTION→DECISION wiring uniform (the tactical fix for GAP-D that `PerceptionFrame` later makes structural). **Do this first.** | `wave-c2-catalyst.md` §C2-2 (P1) |
| **Wave C2-3** — SemanticAnalyst no-lookahead assertions | PERCEPTION honesty-gate completeness (release-blocking). | `wave-c2-catalyst.md` §C2-3 |
| **Wave C2-1** — catalyst profitability cron | PERCEPTION→DECISION feedback: earns the social-arb haircut up once `brand_self` clears `MIN_SAMPLE` (ADR-0076 B07). | `wave-c2-catalyst.md` §C2-1 |
| **Wave C2-4** — ADR-0075 catalyst-driven universe onboarding | SCANNING: admits out-of-universe names a fresh catalyst targets; admission-only, the gate stays final. | `wave-c2-catalyst.md` §C2-4 |
| **Wave B** — ADR-0077 pre-trade admissibility / ShortabilityOracle | The REACTION-layer fidelity gate: a hard precondition upstream of the gate; REJECT-or-flatten only, monotonically non-increasing. | `wave-b-admissibility.md`, `HERMES_QUANT_ADMISSIBILITY` (default-OFF) |
| **Wave B2** — options data layer + options-aware gate + reactor scaffold | REACTION capability (multi-leg fills), default-OFF, not live-wired. | `wave-b2-options.md`, `HERMES_QUANT_OPTIONS_GATE` |
| **Wave C** — observability (`render_X`, `bind_structured` consolidation, calibrator drift) | Cross-cutting: makes every PDR stage auditable. | `wave-c-observability.md` |

### NEW backlog items this architecture implies (future waves — NOT built here)

Each is a perception-layer, evidence-only, default-OFF, eval-gated addition. **Recommended order:** `PerceptionFrame` first (it is the carrier the other three fill), then `TrendVelocity` → `ConvergenceValidator` → `SaturationScore`.

| New item | Fixes | PDR stage | Flag | Eval gate before flip | Depends-on |
|---|---|---|---|---|---|
| **PDR-1 · `PerceptionFrame` carrier + `frame_to_context` adapter** | GAP-D (structural) + GAP-E | PERCEPTION boundary | new `recommend(perception_frame=...)` kwarg; no behavior flag (None = today) | replay test: frame-built path byte-identical to today on all fixtures; no-lookahead gate green | Wave C2-2 (proves the 3-path wiring first) |
| **PDR-2 · `TrendVelocity` perception producer** | GAP-A (DETECT) | PERCEPTION / DETECT | `HERMES_QUANT_TREND_VELOCITY` | D74.7 directional-precision ≥ 0.6 hit-rate on velocity-sourced magnitude vs forward returns | PDR-1; B08 (real Reddit/Trends producers) |
| **PDR-3 · `ConvergenceValidator` at perception** | GAP-B (VALIDATE) | PERCEPTION / VALIDATE | `HERMES_QUANT_CONVERGENCE` | larger labeled social-arb set (B09) clears a HIGHER bar with the ≥2-source requirement on | PDR-1; B08; B09 |
| **PDR-4 · `SaturationScore` + `[SATURATE]` decay multiplier** | GAP-C (EXIT-on-parity) | DECISION→REACTION / EXIT | `HERMES_QUANT_SATURATION` | property test (post-saturation conf ≤ pre); backtest shows decay improves social-arb Sharpe on a labeled exit set | PDR-1; PDR-2 |

**Rails check for all four (restate):** default-OFF; gate read at call time; perception-layer evidence only; `PerceptionFrame` is a container not an authority; `SaturationScore` is silence-only (can only shrink confidence, never raise — same boundary as catalyst-as-evidence and ADR-0077 admissibility); the discrete ladder is untouched; `asof` honesty preserved (every primitive stamps `asof`, validated by the no-lookahead gate which PDR-1's replay test extends to the frame path). With all four flags OFF, the pipeline is the current 3-analyst numerical ensemble → BMA → deterministic gate → silence-bias → paper.

### Promotion discipline (per future wave)

1. **Default-OFF construction.** Ship the abstraction behind its flag; with the flag OFF, behavior is bit-for-bit today's. Property/replay tests assert the rails (container-not-authority for the frame; monotonic-non-increasing for saturation; byte-identical flag-OFF).
2. **Eval-gate.** Clear the per-item eval bar in the table above before any live influence.
3. **Operator audit.** A side-by-side live comparison; arming is a separate, explicit human decision, never bundled with the build.
4. **Flip on the cron wrapper.** One reversible line; promote to default only after a clean burn-in.

---

## Verification

```python
# The authority invariant: perception evidence can only subtract (silence), never amplify.
# PerceptionFrame is a container, not an authority — projecting it into MarketContext
# leaves BMA and the gate unchanged; with the frame absent, recommend() is byte-identical.
from hermes_quant.advisor import recommend
r_today = recommend(symbol="AAPL", timeframe="1d", asof=t)            # no perception_frame
r_frame = recommend(symbol="AAPL", timeframe="1d", asof=t,
                    perception_frame=frame_to_context_inverse(...))   # FUTURE PDR-1
# assert r_today == r_frame on all backtest fixtures (replay test, PDR-1 eval gate)

# Saturation is silence-only (FUTURE PDR-4 property test):
# for every input, the post-[SATURATE] confidence is <= the pre-[SATURATE] confidence.
assert saturated_signal.confidence <= signal.confidence

# Two-layer require_ensemble: a social-arb signal must clear BOTH
#   cross-SOURCE convergence at perception (>= 2 independent source families)  [PDR-3]
#   AND cross-ANALYST agreement at decision (n_distinct_analysts >= 2)         [bma.py:498-519]
```

```bash
# With every new PDR flag OFF, the pipeline is bit-for-bit today's.
HERMES_QUANT_TREND_VELOCITY= HERMES_QUANT_CONVERGENCE= HERMES_QUANT_SATURATION= \
  ~/.hermes/hermes-agent/venv/bin/python3 -m hermes_quant.backtest --replay-fixtures
# Expect: identical outputs to the pre-ADR-0079 baseline on all fixtures.
```
