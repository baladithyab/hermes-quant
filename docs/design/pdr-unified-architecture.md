# PDR — The Unified Perception → Decision → Reaction Architecture for hermes-quant

> **Status:** design / capstone reference · **Date:** 2026-05-30 · **Author:** lead-architect synthesis pass
> **Supersedes nothing; unifies:** ADR-0002/0003/0004 (the PDR-ish contracts), ADR-0074/0076 (catalyst/social fusion),
> the four in-flight wave plans (`docs/plans/wave-{b,b2,c,c2}-*.md`), and the three PDR research notes
> (`docs/research/2026-05-30-r-pdr-*.md`).
> **This doc DESIGNS; it does not BUILD.** New abstractions it implies (TrendVelocity, ConvergenceValidator,
> SaturationScore, PerceptionFrame) are scoped as *future waves* in §6, not implemented here.

This is the single coherent picture of how *every* signal source — classical TA, microstructure, Kronos,
semantic/catalyst, social-arbitrage, fundamentals — enters one pipeline as a data point, how they fuse, and
how the Camillo social-arbitrage method (DETECT → VALIDATE → LINK → ACT → EXIT) is expressed *without weakening a
single rail.*

---

## 0. The rails (non-negotiable; every section below preserves these)

From `AGENTS.md` + the operator's stated posture. The architecture MUST keep all of these true:

1. **Silence-by-default.** Uncertainty → cash. The deterministic risk gate (`risk/gate.py`, ADR-0004) is the
   **FINAL authority**. LLM / committee / semantic / social are **EVIDENCE**: they can *silence* a signal
   (multiply confidence toward 0.0) but can **never amplify, override, or authorize**.
2. **`require_ensemble`: no signal fires alone.** `BMAAggregator` silences a single-source candidate
   (`aggregators/bma.py:498-519`, `n_distinct_analysts <= 1` → `silenced_single_source`).
3. **Discrete sizing** `{0, ±0.05, ±0.10, ±0.15, ±0.20}` of NAV. Never widened without an ADR amendment.
   Money via CLI only, never tools.
4. **Default-OFF + eval-gated.** Every new capability ships behind a `HERMES_QUANT_*` flag, default OFF, promoted
   only after it clears its eval gate and the operator audits a live side-by-side.
5. **`asof` = publication/decision time, always.** Lookahead honesty is load-bearing. All times UTC.
6. **Reproducibility.** Every signal replayable from disk; the execution record is the audit trail.

> The PDR gaps this doc closes are all in the **perception layer** the rails currently lack. None of the four
> new abstractions touches the gate's authority. With every new flag OFF, the pipeline is **bit-for-bit** the
> current 3-analyst numerical ensemble → BMA → deterministic gate → silence-bias → paper.

---

## 1. The PDR model, explicitly

The operator's north-star is one coherent **Perception → Decision → Reaction** pipeline that treats *all* signal
sources as data points. The operator names five stages; they group into the three PDR phases:

| PDR phase | Operator stage(s) | What happens | Authority |
|---|---|---|---|
| **PERCEPTION** | **SCANNING** + **ANALYSIS** | Sense the world: select symbols, fetch bars, build regime, detect trends, validate across sources, read each analyst's view. Produce *evidence*. | none (pure sensing) |
| **DECISION** | **DELIBERATION** + **RISKING** | Fuse evidence into one signal (BMA), then the deterministic gate computes the legal sizing envelope. Optional LLM committee selects *within* the envelope. | **deterministic gate is final** |
| **REACTION** | **ACTING** | Convert the gated Action into a (paper) order, fill it with live-fidelity, record it. | deterministic; paper for now |

**The defining invariant of the whole design:** authority *concentrates* monotonically from PERCEPTION (no
authority — everything is evidence) → DECISION (the gate is the single authority) → REACTION (mechanical
execution of an already-authorized Action). Evidence can only *subtract* (silence). Nothing downstream of the
gate can re-introduce a silenced signal.

### 1.1 ASCII pipeline — every stage + the object passed between them

```
                                   ┌─────────────────────────────────────────────────────────────┐
                                   │                      PERCEPTION  (SCANNING + ANALYSIS)         │
                                   │                      — produces EVIDENCE, holds NO authority — │
                                   └─────────────────────────────────────────────────────────────┘
  [SCAN]  universe-scan / watchlist-evolve / catalyst-onboarding (ADR-0075, future)
          quant-universe-scan.py · playbook/watchlist_evolution.py · catalyst/onboarding.py
                     │  selects WHICH symbols enter perception (symbol set, not a signal)
                     ▼
        ┌──────────────────────── per symbol, at asof ────────────────────────┐
        │                                                                       │
   [SENSE-NUM]  provider.fetch_bars(sym,tf,<=asof)        advisor.py:766        │
                drop_still_forming_bar (ADR-0069)         advisor.py:830        │
                build_regime_extras (ADR-0063)            advisor.py:861        │
        │                                                                       │
   [SENSE-TREND] TrendVelocity producer (FUTURE, GAP-A) ──┐                     │
                 ConvergenceValidator  (FUTURE, GAP-B) ───┤  social-arb         │
                 SaturationScore       (FUTURE, GAP-C) ───┘  PERCEPTION         │
                 catalyst ingest→classify→propagate→synthesize  catalyst/*.py   │
        │                                                                       │
        └──────────────────► PerceptionFrame  (FUTURE, GAP-D/E) ◄──────────────┘
                             {symbol, asof, numerical bars/ctx, regime,
                              semantic_packets, trend_velocity, convergence,
                              saturation, provenance}
                                     │   ── ONE provenance-carrying object ──
                                     ▼
   [ANALYZE] each analyst.analyze(ctx) -> AnalystView | None        advisor.py:931
             ClassicalTA · Microstructure · Kronos · Semantic(packet consumer) · Fundamentals
             (each emits direction∈{-1,0,1}, magnitude, calibrated confidence, horizon)
                                     │  list[AnalystView]
═════════════════════════════════════▼═══════════════════════════════════════════════════════════
                                   ┌─────────────────────────────────────────────────────────────┐
                                   │                       DECISION  (DELIBERATION + RISKING)       │
                                   │             — the deterministic risk gate is FINAL authority — │
                                   └─────────────────────────────────────────────────────────────┘
   [FUSE]  BMAAggregator.aggregate(views, ctx) -> AggregatedSignal     bma.py:305
           require_ensemble: n_distinct_analysts >= 2 else SILENCE      bma.py:498-519
           dissent shrinks confidence; never synthesizes unanimity
                                     │  AggregatedSignal
                                     ▼
   [SATURATE] saturation/edge-decay multiplier ∈ (0,1]  (FUTURE, GAP-C)   confidence ↓ only
                                     │  AggregatedSignal (confidence possibly reduced)
                                     ▼
   [GATE]  DefaultRiskGate.gate(signal, market, portfolio, halt) -> Action | None    risk/gate.py
           computes the LEGAL discrete sizing envelope (drawdown, cost, Kelly, caps)
           None = SILENCE.  THIS IS THE FINAL AUTHORITY.
                                     │  Action  (signed target_size_pct ∈ ladder)  or None
              ┌──────── optional, default-OFF ────────┐
              │ committee/trader LLM picks WITHIN the  │  (envelope-then-select; LLM cannot exceed)
              │ already-computed envelope only         │  HERMES_QUANT_{DELIBERATIVE,TRADER_LLM,...}
              └────────────────────────────────────────┘
                                     │
   [SILENCE-BIAS] silence_bias_gate (autonomous only)  4 dims, ALL must pass    autonomous.py:386
   [ADMIT]  pre-trade admissibility (ADR-0077, FUTURE/Wave B) — REJECT or flatten only
                                     │  Action  (survivors only)
═════════════════════════════════════▼═══════════════════════════════════════════════════════════
                                   ┌─────────────────────────────────────────────────────────────┐
                                   │                          REACTION  (ACTING)                    │
                                   │              — mechanical execution of an authorized Action —  │
                                   └─────────────────────────────────────────────────────────────┘
   [REACT]  PaperReactor.execute(Proposal)   react/paper.py:54   (equity today; options = Wave B2)
            fill model (ADR-0070) · idempotency (ADR-0078) · execution record IS the audit trail
                                     │
                                     ▼
            executions.jsonl / state.db / signals.jsonl  →  reflector (post-trade memory)
            live = type-gated stub until promotion (react/live.py:35-45)
```

**Objects on the wire (today, verified `protocol.py`):** `MarketContext` (`protocol.py:57`) → `AnalystView`
(`protocol.py:90`) → `AggregatedSignal` (`protocol.py:135`) → `Action` (the gate output, signed
`target_size_pct` in the ladder) → `Proposal` (`protocol.py:183`, the `operation` channel) → execution record.
The **one new object** this design adds is `PerceptionFrame`, threaded between SENSE and ANALYZE so the
perception boundary becomes a typed, provenance-carrying contract instead of an opaque `extras` dict (§4).

---

## 2. The signal-entry matrix

For each signal source: which PDR stage it enters, whether it is a **PERCEPTION primitive** (a raw sense) or a
**DECISION analyst** (a view), how it's scored, what flag gates it, and how it fuses. All `file:line` are
verified against HEAD.

| Source | Enters at | Primitive or Analyst? | How scored | Flag | How it FUSES |
|---|---|---|---|---|---|
| **Classical TA** | PERCEPTION→DECISION | **Analyst** (always on) `advisor.py:345` | point reading of indicators at asof → `AnalystView(dir,mag,conf)` `classical_ta.py:236` | none | peer view in BMA, track-record weighted `bma.py:432` |
| **Microstructure** | PERCEPTION→DECISION | **Analyst** (ImportError-soft) `advisor.py:347` | order-flow/imbalance proxy on bar window `microstructure.py:270` | none | peer view in BMA |
| **Kronos** (foundation model) | PERCEPTION→DECISION | **Analyst** (abstains conf=0 w/o dep) `advisor.py:352` | point forecast at asof `kronos.py:205` | optional dep | peer view; abstains < `ABSTAIN_THRESHOLD` are dropped pre-fuse `bma.py:128` |
| **Semantic / Catalyst** | PERCEPTION (produce) → DECISION (consume) | **BOTH** — see §2.1 | producer: severity-on-keywords + propagation linkage×agreement `synthesize.py:115`; consumer: packet stance/conf → `AnalystView` `semantic.py:75` | `HERMES_QUANT_SEMANTIC_ENABLED=1` (default 0) `advisor.py:377` | enters BMA as a **peer** view; a disagreeing high-conf packet *reduces* aggregate confidence, never overrides |
| **Social-arbitrage** | PERCEPTION (produce) → DECISION (consume) | **BOTH**, fundamentally a PERCEPTION method — see §2.1 | producer: Reddit/Trends → `CatalystItem` → same classify→propagate path `social.py:88,182`; **haircut ×0.5** before BMA `synthesize.py:53-66` | (same `HERMES_QUANT_SEMANTIC_ENABLED`) | deliberately **weak** peer view (haircut) until B06/B07 profitability earns weight |
| **Fundamentals** | PERCEPTION→DECISION | **Analyst** (OFF) `advisor.py:362` | fundamental ratios at asof `fundamentals.py:406` | `HERMES_QUANT_FUNDAMENTALS_ENABLED=1` (default 0) | peer view in BMA |
| **Regime** | PERCEPTION (context) | **Primitive** (not a view) | `build_regime_extras` → `ctx.extras["regime"]` `advisor.py:861` | none (HMM behind `_REGIME_HMM`) | a *conditioner* on analyst confidence / BMA weights (ADR-0047), not a vote |

### 2.1 Social-arb is a PERCEPTION method whose OUTPUT becomes a DECISION view — the two roles, kept distinct

This is the central conceptual correction the architecture makes explicit. The Camillo method is, at its core, a
**perception discipline** — *how you sense a real consumer trend before Wall Street*: detect acceleration,
validate it across independent sources, then link it to a ticker. That is all PERCEPTION. It produces a piece of
evidence (a `SemanticPacket`). Only *then* does that evidence enter the DECISION layer, where it is treated like
any other analyst's view — a peer in BMA, bounded by `require_ensemble`, haircut, and the gate.

- **Role 1 — PERCEPTION method (the part the system lacks).** DETECT (trend velocity), VALIDATE (cross-source
  convergence), LINK (trend→ticker). Today only LINK exists (the propagation graph). DETECT and VALIDATE are
  GAP-A and GAP-B (§3).
- **Role 2 — DECISION analyst (the part that already works).** The `HermesSemanticAnalyst` *consumes* a finished
  packet and emits one `AnalystView` (`semantic.py:75-141`). It is a peer; `require_ensemble` means a lone
  semantic view cannot fire; the haircut makes social-arb a *weak* peer.

Conflating these two roles is the source of the audit's GAP-A ("velocity is comment-only") and GAP-B ("no
cross-source validation"). The fix is to build the *perception* side honestly (a velocity detector + a
convergence validator that feed the packet producer) **without** changing the *decision* side (it stays a peer
view, bounded by every rail). Authority never moves to the social signal; only the *quality of the evidence it
carries* improves.

---

## 3. The three things social-arb adds that the system lacks (and where they live in PDR)

The current pipeline fires on *the presence of a severe keyword on one headline* — `classify_headline` returns
`severity = max single-term lexicon weight` (`classify.py:128-139`), a static word-boundary match on ONE title.
That is **not** the Camillo DETECT primitive. Three primitives are missing; each maps to a precise PDR stage.

### 3.1 Trend VELOCITY detection — **PERCEPTION (SCANNING/ANALYSIS), GAP-A**

- **What Camillo does:** DETECT = *exploding trends* — interest **accelerating** week-over-week far above its own
  baseline. The edge is in the *slope*, not the severity.
- **What hermes-quant does today:** `social.py` *frames* synthetic headlines with velocity words ("trending /
  surging", `social.py:181`) that still flow into the *same* severity classifier. There is zero week-over-week
  acceleration logic anywhere in catalyst code.
- **Where it lives in PDR:** a new **perception producer** `TrendVelocity` that computes week-over-week
  acceleration of an interest series (Trends / Reddit-mention counts) against its own trailing baseline, emitting
  a *velocity score* + *baseline z-score* instead of a keyword severity. It sits **upstream of**
  `synthesize_packets`; the packet `magnitude` is then sourced from velocity, not from the lexicon. It is
  lookahead-honest by construction (only past observations, stamped `asof`). Behind `HERMES_QUANT_TREND_VELOCITY`,
  eval-gated by the existing D74.7 harness.

### 3.2 DATA CONVERGENCE validation — **PERCEPTION (require_ensemble at the SOURCE level), GAP-B**

- **What Camillo does:** VALIDATE = a trend is real only when it shows across **MULTIPLE independent sources**
  (social + Google Trends + web traffic + Amazon/credit-card). This is `require_ensemble` applied at the
  *perception* layer, **cross-SOURCE**.
- **What hermes-quant does today:** `require_ensemble` lives only at the **decision** layer — **cross-ANALYST**
  agreement (`bma.py:498-519`, TA + Kronos + semantic must concur). At perception, a lone Reddit packet and a
  lone Trends packet are merely `dedupe_items`-merged (`social.py:247`) and `load_packets_for` collapses to
  best-per-stance (`synthesize.py:220-232`) — they are never *validated against each other*. A single Reddit
  headline gets the same confidence ceiling as a true multi-source convergence.
- **Where it lives in PDR:** a new **perception validator** `ConvergenceValidator`, a pure function over the *set*
  of `CatalystItem`s for a (trend, symbol): require **≥2 independent `source` families** (reddit / google_trends /
  news / web-traffic) before a packet is emitted at full confidence; single-source → haircut or drop. This is
  `require_ensemble` *relocated to perception (cross-SOURCE)*, **complementary** to BMA's cross-ANALYST guard and
  exactly the Camillo VALIDATE step. Wired into `synthesize_packets` before the packet is written. Behind
  `HERMES_QUANT_CONVERGENCE`.

> **The clean distinction to hold:** *cross-SOURCE convergence at perception* (is this trend real?) is a
> different question from *cross-ANALYST agreement at decision* (do my independent models concur?). The system
> needs **both** for a social-arb signal to fire: convergence proves the trend exists, then the trend's packet
> must still find a numerical corroborator in BMA. Two independent ensemble requirements, at two different layers.

### 3.3 Information SATURATION / edge-decay — **DECISION → REACTION, GAP-C**

- **What Camillo does:** EXIT on **information parity** — sell when Wall Street catches up (earnings /
  credit-card data confirms). The edge is *time-decaying information asymmetry*; TickerTrends models this as an
  *Investor-Saturation Score* (trend peaking → edge gone).
- **What hermes-quant does today:** sizing is purely confidence × Kelly; there is no concept of *remaining
  edge-time*. No `saturation` / `edge_decay` / `information_parity` field exists anywhere (grep: 0 hits in
  `hermes_quant/`). The system can perceive a trend but has no idea whether it is early or already mainstream.
- **Where it lives in PDR:** a `saturation` estimate added to `SemanticPacket.metadata` at PERCEPTION (e.g. trend
  age past its velocity peak, or "earnings / credit-card confirm date has passed"), then consumed at the
  **DECISION→REACTION boundary** as a **confidence multiplier that can only shrink toward 0.0**.

  **How it interacts with the deterministic gate WITHOUT becoming authority** (this is the load-bearing design
  point):
  - It is applied as a multiplier `m ∈ (0, 1]` on the `AggregatedSignal.confidence` **before** the gate, in the
    `[SATURATE]` step of the §1.1 diagram. `m=1` early in the trend; `m→0` as it saturates.
  - It is **silence-only by construction**: it can pull conviction toward 0 (decaying edge → quieter → eventually
    silenced by the gate's cost/edge floor) but it can **never raise** confidence. A property test pins this
    forever: post-saturation confidence ≤ pre-saturation confidence, for every input. This is the *exact* same
    authority boundary as the catalyst-as-evidence-never-authority rail and the ADR-0077 admissibility boundary
    (target magnitude monotonically non-increasing).
  - It is **not** a new sizing ladder. The discrete ladder is untouched; saturation only moves conviction down
    the *existing* ladder (e.g. a `0.20` candidate decays to `0.10`, then to `0` = silence) and can trigger a
    flatten on a held position when the trend has fully saturated — the Camillo exit-on-parity, expressed as
    "the evidence for this position has decayed to nothing, so silence/flatten." The gate still decides the
    final number; saturation only feeds it weaker evidence.

  Behind `HERMES_QUANT_SATURATION`, eval-gated.

---

## 4. The unified `PerceptionFrame` contract (GAP-D + GAP-E)

**The bug it kills (GAP-D, structurally):** semantic packets reach **only 1 of 3 live decision paths today** —
`load_packets_for` is wired into `quant-daily-interim.py:127-141` only; `autonomous.tick` and
`quant-playbook-tick.py:465` call `recommend()` with **no `market_extras`**, so with the flag ON the analyst
silently abstains `no_semantic_packets` (`semantic.py:156`) on 2 of 3 paths. The flag and the wiring are
decoupled. **Wave C2-2 fixes the immediate symptom** (one shared `semantic_market_extras` helper called by all
three paths — `wave-c2-catalyst.md` §C2-2). `PerceptionFrame` is the *structural* fix: if perception is built
**once** into a typed object and threaded into `recommend()`, the decoupling becomes impossible to reintroduce —
there is one populated input, not three ad-hoc side-channels.

**The deeper problem it kills (GAP-E):** signals enter today via three disjoint side-channels — numerical via
`bars`, regime via `ctx.extras["regime"]`, semantic via `ctx.extras["semantic_packets"]`. There is no single
provenance-carrying object that *is* "everything perceived about this symbol at asof." The decision layer
reconstructs it implicitly. That is the PDR perception boundary itself, missing a contract.

### 4.1 Proposed shape — minimal, additive, rails-preserving

```python
# hermes_quant/perception/frame.py   (FUTURE — design only)
from __future__ import annotations
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any
import pandas as pd

@dataclass(frozen=True)
class PerceptionFrame:
    """Everything perceived about one symbol at one asof — the typed PERCEPTION
    boundary. A CONTAINER, never an authority: it carries evidence into the
    decision layer; BMA still treats each carried signal as a peer view, and the
    deterministic gate is still final. Built ONCE per symbol so the catalyst
    flag/wiring decoupling (GAP-D) is structurally impossible.
    """
    symbol: str
    asof: pd.Timestamp                       # decision time, UTC (lookahead anchor)
    bars: pd.DataFrame                       # canonical OHLCV (the numerical sense)
    last_close: float
    regime: Mapping[str, Any] | None = None  # ADR-0063 regime extras
    semantic_packets: tuple[Any, ...] = ()   # finished catalyst/social packets (already validated)
    # --- the three new social-arb perception primitives (§3), all default-empty ---
    trend_velocity: Mapping[str, Any] | None = None   # GAP-A: {score, baseline_z, asof}  (HERMES_QUANT_TREND_VELOCITY)
    convergence: Mapping[str, Any] | None = None       # GAP-B: {n_sources, families, validated}  (HERMES_QUANT_CONVERGENCE)
    saturation: Mapping[str, Any] | None = None        # GAP-C: {score, decay_multiplier}  (HERMES_QUANT_SATURATION)
    provenance: tuple[str, ...] = ()         # evidence_ids / source URLs / fetch run-ids (ADR-0033/0041)
    extras: Mapping[str, Any] = field(default_factory=dict)  # forward-compat escape hatch
```

### 4.2 How it composes with the existing shapes (no rename, no removal)

The versioning rule in `protocol.py:14` is *add-only*. `PerceptionFrame` composes **additively**:

1. **It is built, then projected into the existing `MarketContext`.** A pure adapter
   `frame_to_context(frame) -> MarketContext` populates `MarketContext.bars/last_close/asof/extras` exactly as
   today, with `extras = {"regime": ..., "semantic_packets": ..., "trend_velocity": ..., "convergence": ...,
   "saturation": ..., "decision_asof": ...}`. **Every existing analyst reads it unchanged** — `MarketContext` is
   untouched. The frame is a *superset producer* of today's `ctx.extras`, not a replacement of the analyst
   contract. (This is why it is rails-preserving: the analyst Protocol, BMA, and the gate never change.)
2. **`recommend()` gains one optional kwarg.** `recommend(..., perception_frame: PerceptionFrame | None = None)`.
   When `None` (every backtest + today's default), `recommend` builds the frame internally from `fetch_bars` +
   `build_regime_extras` exactly as now — **byte-identical behavior**. When provided (the live paths), all three
   crons hand in one frame built by one loader → GAP-D cannot recur. This mirrors the existing
   `market_extras: dict | None = None` no-op pattern already in `recommend` (`advisor.py`), so it is a known-safe
   additive change.
3. **It carries provenance for the evidence store.** `frame.provenance` + per-packet `evidence_ids` give the
   decision layer one provenance-carrying input (ADR-0033/0041), so `AnalystView.evidence_ids` /
   `AggregatedSignal.evidence_ids` (`protocol.py:115,162`) can be populated from a single place.
4. **The new social-arb fields are read by the new primitives only.** `trend_velocity`/`convergence` are
   *produced* during frame construction (the perception producers of §3) and *consumed* by the catalyst packet
   builder before packets land in `semantic_packets`; `saturation` is consumed by the `[SATURATE]` confidence
   multiplier just before the gate. Analysts that don't know these fields ignore them (the `extras` Mapping
   contract already says "consumers ignore unknown fields", `protocol.py:16`). **Nothing in the default path
   reads them until its flag flips.**

**What `PerceptionFrame` is NOT:** it is not an authority, not a new gate, not a sizing input. It is the typed
name for "the perception phase's output." BMA still fuses peer views; the deterministic gate is still final.

---

## 5. What we ADOPT from each reference repo (mapped onto PDR) — and what we explicitly do NOT

The architecture is grounded in four studied repos. Each contributes to a *specific* PDR stage; each has a
documented anti-pattern we reject (`AGENTS.md` "Anti-patterns from reference projects we explicitly reject").

| Repo | PDR stage we borrow into | What we ADOPT | What we explicitly REJECT (cite) |
|---|---|---|---|
| **TauricResearch/TradingAgents** (5-phase: Analyst Team → Researcher debate → Trader → Risk-Mgmt → Portfolio Mgr) | DECISION (deliberation) | The **5-phase shape as a mental map** for the deliberation sub-stages (analysts → debate → trader → risk → portfolio), and the structured-output discipline (`bind_structured` w/ retry, ADR-0044). The bull/bear debate already shipped (ADR-0065/66) as default-OFF *evidence*. | **Trader/PM LLM as final decision authority** (research 04:206 "Don't add LLMs to the action path. Ever."); **free-text `position_sizing` prose** (AGENTS.md #4); **`FINAL TRANSACTION PROPOSAL: **BUY/SELL**` string-grep contract** (AGENTS.md #5). Our `Action` is purely deterministic from the gate; the 3-LLM risk debate is *evidence beside* the gate, never the gate. |
| **HKUDS/Vibe-Trading** (Plan→Ground→Execute→Validate→Deliver) | PERCEPTION (grounding) + the evidence ledger | **Pre-reasoning Data-Grounding Block + Citation HARD RULE** — splice fetched ground-truth (`asof`-stamped) into every analyst/LLM prompt *before* reasoning; forbid any number not traceable to a current-run fetch, the ground-truth block, or cited upstream. Operationalizes the lookahead rail at the prompt layer. **Cite-or-die verification** (SHA256 artifact hash or valid `run_id`; a tool-call-id alone is insufficient) → mirror as a deterministic admissibility check feeding the gate. **Hypothesis Registry + `link_backtest` + status lifecycle** (exploring→testing→validated/rejected→monitoring) as the proposal→evidence→verdict ledger. | **LLM-agent-as-decision-maker / ReAct loop as the executor**, and **qualitative summary-passing fusion** (`SwarmTask.input_from`) instead of a deterministic weighted aggregator. We keep fusion in `BMAAggregator`. (Vibe-Trading is the lone reference that draws the boundary at "no live execution" — that part we respect.) |
| **HKUDS/AI-Trader** | PERCEPTION (sentiment scoring) + the channel discriminator | The **message-kind channel separation** (`operation`/`strategy`/`discussion`, already in `protocol.py:34-43`) and the idea of **cross-source sentiment scoring** as input to the convergence validator (§3.2). | **1:1 blind copy-trading cascade** (`services.py::_update_position_from_signal`, AGENTS.md #6) and **single-token read+execute auth** (AGENTS.md #7). We keep per-order HITL and read-only-tools vs CLI-only-execution capability separation (ADR-0007/0015). |
| **virattt/ai-hedge-fund** | DECISION (risk-before-decision + envelope-then-select) | The **deterministic-envelope-then-LLM-select pattern** (`compute_allowed_actions`): the gate computes the *legal* discrete sizing envelope **first**; any LLM/committee may only *pick within* it and literally cannot exceed it. The **explicit risk-limit object handed downstream** (their `remaining_position_limit`) strengthens the ADR-0004 contract. The **uniform peer-analyst signal envelope** `{signal, confidence, reasoning}` — all sources write to one map as peers (our `AnalystView` + BMA). Their **v2 quant stack** (signals normalized −1..+1, CPCV/PBO overfitting guards, PIT backtest, txn-cost modeling) as a validation north-star matching our eval-gated rails. | **LLM as the fusion/arbitration authority** (their PM lets an LLM choose direction+size from raw signals — no deterministic aggregator, no `require_ensemble`, LLM can amplify a lone signal, AGENTS.md convergent-failure note). Borrow the *envelope contract*, not the arbitration. Also reject **`short`/`cover` as first-class actions** (paper-only long/flat posture today). |

**Convergent rejection (AGENTS.md):** every reference repo lets the LLM be the final execution authority
somewhere (TradingAgents at the trader role; AI-Trader at the copy cascade; moon-dev at the override boundary).
Our deterministic-gate + HITL pattern is the **inverse** of that failure mode. The envelope-then-select pattern
(ai-hedge-fund) is the cleanest external precedent for *how* to use an LLM safely: it selects inside an already
deterministic, capped envelope.

---

## 6. Migration path — today → PDR-clean, in DEFAULT-OFF, eval-gated increments

Everything below is additive and default-OFF. With every new flag OFF, behavior is bit-for-bit today's pipeline.
The first PDR step is already in flight; the four new abstractions are scoped as *future waves* — **not built
here**.

### 6.1 The PDR steps already in flight (cohere with these; do not contradict)

| In-flight work | PDR role it plays | Status / plan |
|---|---|---|
| **Wave C2-2** — shared `semantic_market_extras` helper wired into all 3 decision paths | **The first PDR step**: makes the PERCEPTION→DECISION wiring uniform (the tactical fix for GAP-D that `PerceptionFrame` later makes structural). | `wave-c2-catalyst.md` §C2-2, P1 correctness — **do this first.** |
| **Wave C2-3** — SemanticAnalyst no-lookahead assertions (future_packet abstain + `<=` boundary) | PERCEPTION honesty gate completeness (release-blocking). | `wave-c2-catalyst.md` §C2-3 |
| **Wave C2-1** — catalyst profitability cron (change-detecting watchdog) | PERCEPTION→DECISION feedback: earns the social-arb haircut up (B07) once `brand_self` clears `MIN_SAMPLE`. | `wave-c2-catalyst.md` §C2-1 |
| **Wave C2-4** — ADR-0075 catalyst-driven universe onboarding | **SCANNING**: admits out-of-universe names a fresh catalyst targets (perceive-but-can't-act gap, B05). Admission-only; the gate stays final. | `wave-c2-catalyst.md` §C2-4 |
| **Wave C2-5** — learned-graph mining (design only) | PERCEPTION/LINK: learns corrected edge signs from the propagation-log corpus. Proposes, never auto-mutates the curated graph. | `wave-c2-catalyst.md` §C2-5 (design) |
| **Wave B** — ADR-0077 pre-trade admissibility / ShortabilityOracle | **The REACTION-layer fidelity gate**: a hard precondition *upstream* of the gate; can only REJECT a short or flatten an inadmissible held short. Never amplifies (property test: target magnitude monotonically non-increasing). | `wave-b-admissibility.md`, default-OFF behind `HERMES_QUANT_ADMISSIBILITY` |
| **Wave B2** — options data layer + options-aware gate + reactor scaffold | **REACTION** capability (multi-leg fills), default-OFF, not live-wired. | `wave-b2-options.md` (`HERMES_QUANT_OPTIONS_GATE`) |
| **Wave C** — observability (`render_X`, `bind_structured` consolidation, calibrator drift, IC dedup) | Cross-cutting: makes every PDR stage auditable; `bind_structured` is the structured-output discipline borrowed from TradingAgents. | `wave-c-observability.md` |

### 6.2 NEW backlog items this architecture implies (future waves — NOT built here)

These are the four new abstractions from §3–§4. Each is a perception-layer, evidence-only, default-OFF,
eval-gated addition. **Recommended order:** PerceptionFrame first (it is the carrier the other three fill), then
TrendVelocity → ConvergenceValidator → SaturationScore.

| New item | Fixes | PDR stage | Flag | Eval gate before flip | Depends-on |
|---|---|---|---|---|---|
| **PDR-1 · `PerceptionFrame` carrier + `frame_to_context` adapter** | GAP-D (structural) + GAP-E | PERCEPTION boundary | new `recommend(perception_frame=...)` kwarg; no behavior flag (None = today) | replay test: frame-built path byte-identical to today on all fixtures; no-lookahead gate green | **Wave C2-2** (proves the 3-path wiring first) |
| **PDR-2 · `TrendVelocity` perception producer** | GAP-A | PERCEPTION / DETECT | `HERMES_QUANT_TREND_VELOCITY` | D74.7 directional-precision ≥0.6 hit-rate on velocity-sourced magnitude vs forward returns | PDR-1 (fills `frame.trend_velocity`); B08 (real Reddit/Trends producers) |
| **PDR-3 · `ConvergenceValidator` at perception** | GAP-B | PERCEPTION / VALIDATE | `HERMES_QUANT_CONVERGENCE` | larger labeled social-arb set (B09) clears a HIGHER bar with the ≥2-source requirement on | PDR-1; B08; B09 |
| **PDR-4 · `SaturationScore` + `[SATURATE]` decay multiplier** | GAP-C | DECISION→REACTION / EXIT | `HERMES_QUANT_SATURATION` | property test (post-saturation conf ≤ pre); backtest shows decay improves social-arb Sharpe on a labeled exit set | PDR-1; PDR-2 (saturation is "past the velocity peak") |

**Rails check for all four (restate):** default-OFF; gate read at call time; perception-layer evidence only;
`PerceptionFrame` is a container not an authority; `SaturationScore` is silence-only (can only shrink confidence,
never raise — same boundary as catalyst-as-evidence and ADR-0077 admissibility); the discrete ladder is
untouched; `asof` honesty preserved (every primitive stamps `asof`, validated by the no-lookahead gate which
PDR-1's replay test extends to the frame path). With all four flags OFF, the pipeline is the current 3-analyst
numerical ensemble → BMA → deterministic gate → silence-bias → paper.

---

## 7. One-paragraph synthesis (the capstone claim)

hermes-quant already has a clean DECISION and REACTION layer — `BMAAggregator` fuses peer views with
`require_ensemble`, the `DefaultRiskGate` is the final deterministic authority, and the `PaperReactor` executes
mechanically. What it lacks is a **first-class PERCEPTION layer**: a typed boundary (`PerceptionFrame`) and the
three social-arbitrage perception primitives — trend **velocity** (DETECT), cross-source **convergence**
(VALIDATE), and information **saturation** (the EXIT-on-parity edge-decay). The Camillo method is fundamentally a
*perception* discipline; the architecture expresses it by **strengthening the quality of the evidence** that
flows into the existing decision layer, never by giving any signal authority. Build the carrier first
(PerceptionFrame, after Wave C2-2 proves the 3-path wiring), then fill its fields with the three default-OFF,
eval-gated producers. Every rail is preserved: silence-by-default, the deterministic gate as final authority,
`require_ensemble` at *two* layers (cross-source at perception, cross-analyst at decision), the discrete ladder,
and `asof` honesty end-to-end.
