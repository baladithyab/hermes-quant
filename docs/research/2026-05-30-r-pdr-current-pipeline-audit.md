# Research: Current hermes-quant pipeline audited against the PDR model (2026-05-30)

A **code-grounded** audit (every claim cites `file:line`, read from source, not docs) of
the live hermes-quant pipeline mapped onto the operator's Perception → Decision →
Reaction (PDR) north-star and the Chris Camillo social-arbitrage method
(DETECT → VALIDATE → LINK → ACT → EXIT). Purpose: feed the architecture doc a precise
"what we have / what each PDR stage is missing" picture.

> Posture (AGENTS.md): money-software, silence-by-default, deterministic risk gate is
> FINAL authority, LLM/semantic/social are EVIDENCE that can only silence, never amplify.
> Nothing below proposes weakening that — the gaps are about the **perception layer** the
> rails currently lack.

---

## TL;DR (the stage map + top-5 PDR gaps)

- **Current stage map.** PERCEPTION = `provider.fetch_bars` (`advisor.py:766`) + regime
  extras (`advisor.py:861`) + N analysts in `_build_default_analysts()`
  (`advisor.py:337`), each emitting **one point-in-time `AnalystView` per call**
  (`analyze(ctx)` at `classical_ta.py:236`, `microstructure.py:270`, `kronos.py:205`,
  `semantic.py:75`). DECISION = `BMAAggregator.aggregate` (`bma.py:305`) → `DefaultRiskGate`
  (`advisor.py:991`) → (autonomous) `silence_bias_gate` (`autonomous.py:386`). REACTION =
  `PaperReactor.execute` via `_react` (`autonomous.py:503`); live is a type-gated stub
  (`react/live.py:97`).
- **GAP-A — no trend-VELOCITY primitive.** `classify_headline` is severity-on-keywords on a
  single headline (`classify.py:112-139`); `social.py` only *frames* synthetic headlines
  with velocity words ("trending/surging", `social.py:181`) that still flow into the same
  severity classifier. No week-over-week acceleration is ever computed (grep: zero
  `velocity`/`accelerat` logic in catalyst code).
- **GAP-B — no data-CONVERGENCE validation at perception.** `require_ensemble` lives at the
  **decision** layer (cross-analyst, `bma.py:498-519`); there is no cross-SOURCE
  corroboration of a trend (social + Trends + traffic) at the perception layer. A lone
  Reddit packet and a lone Trends packet are deduped (`social.py:247`) and collapsed to one
  packet (`synthesize.py:220-232`), not *validated against each other*.
- **GAP-C — no information-SATURATION / edge-decay concept.** Sizing is purely
  confidence×Kelly; nothing models "remaining edge-time" or "Wall Street has caught up."
  No `saturation`/`edge_decay`/`information_parity` field exists anywhere
  (grep: 0 hits in `hermes_quant/`).
- **GAP-D — semantic packets reach 1 of 3 live decision paths.** `load_packets_for` is
  wired ONLY in `quant-daily-interim.py:127-141`. `autonomous.tick` calls `advisor_recommend(...)`
  with **no `market_extras`** (`autonomous.py:355-360`) and `quant-playbook-tick.py:465`
  calls `recommend(... )` with no `market_extras` either. With `HERMES_QUANT_SEMANTIC_ENABLED=1`
  the analyst silently abstains `no_semantic_packets` on 2 of 3 paths (`semantic.py:156`).
- **GAP-E — no unified `PerceptionFrame`.** Signals enter as side-channels:
  numerical via `bars`, regime via `ctx.extras["regime"]`, semantic via
  `ctx.extras["semantic_packets"]`. There is no single provenance-carrying object that
  represents "everything perceived about this symbol at asof" flowing into the decision
  layer; the decision layer reconstructs it implicitly from `views` + `extras`.

---

## 1. PERCEPTION today — where data enters, per signal source

The canonical decision entry is `recommend()` (`advisor.py:631`). Perception happens in two
unconnected ways: (1) **bars** fetched once and handed to all analysts via `MarketContext`,
(2) **extras** dicts (regime, semantic packets) merged into `ctx.extras`. Every analyst is a
**point-reading function** `analyze(ctx) -> AnalystView | None` (`protocol.py:428`) — it sees
the current bar window and emits a single (direction, magnitude, confidence, horizon). No
analyst returns a *time-series of a perception quantity* (e.g. trend acceleration).

| Source | Where it enters | Primitive or Analyst? | Flag | Velocity/convergence, or point reading? |
|---|---|---|---|---|
| **Classical TA** | `ClassicalTAAnalyst.analyze` (`classical_ta.py:236`), from `ctx.bars` | Analyst (always on, `advisor.py:345`) | none | **Point reading** of indicators at asof. (A `alpha_price_acceleration` *factor* exists in the alpha zoo `factors/starter_set.py:152` but is NOT in the live analyst loadout.) |
| **Microstructure** | `MicrostructureLite.analyze` (`microstructure.py:270`), from `ctx.bars` | Analyst (ImportError-soft, `advisor.py:347-351`) | none | Point reading (order-flow/imbalance proxy on the bar window). |
| **Kronos** (foundation model) | `KronosAnalyst.analyze` (`kronos.py:205`), from `ctx.bars` | Analyst (if `kronos` extra; abstains conf=0 otherwise, `advisor.py:352-357`) | optional dep | Point reading (forecast at asof). |
| **Semantic / Catalyst** | `HermesSemanticAnalyst.analyze` (`semantic.py:75`) consumes `ctx.extras["semantic_packets"]` (`semantic.py:148`) | Analyst, **packet CONSUMER only** — never calls a model/web inside `analyze` (`semantic.py:1-9`) | `HERMES_QUANT_SEMANTIC_ENABLED=1` default 0 (`advisor.py:377`) | Point reading of a packet's stance/confidence. Packet *production* (ingest→classify→propagate→synthesize) is the closest thing to a perception pipeline but is **severity-on-keywords**, see §below. |
| **Social-arb** | `social.py` Reddit/Trends producers → `CatalystItem` (`social.py:88,182`) → same classify→propagate→synthesize path | Producer feeding the semantic analyst | (same `HERMES_QUANT_SEMANTIC_ENABLED`) | **Point reading dressed as velocity.** Comments claim "velocity" (`social.py:18,82,152`) but the actual signal is `classify_headline` severity on a synthetic headline string. |
| **Fundamentals** | `FundamentalsAnalyst.analyze` (`fundamentals.py:406`) | Analyst, **OFF** (`advisor.py:362`) | `HERMES_QUANT_FUNDAMENTALS_ENABLED=1` default 0 | Point reading of fundamental ratios at asof. |

**Universe scanner / watchlist-evolve** (`ops/scripts/quant-universe-scan.py`,
`quant-watchlist-evolve.py`) select WHICH symbols enter perception, but they too operate on
point-in-time scoring — they are symbol selection, not a perception primitive.

### The catalyst producer pipeline is severity-on-keywords, not velocity

`synthesize_packets` (`synthesize.py:69-139`) for each `CatalystItem`:
1. `classify_headline(item.title)` → polarity + **severity = max single-term lexicon weight**
   (`classify.py:128-139`). This is a static keyword-match on ONE headline. There is no
   baseline, no week-over-week rate, no acceleration.
2. `extract_entities` (substring NER, `propagation.py:305`) → `propagate` (`propagation.py:319`).
3. Emit a packet: `confidence ← propagation linkage×agreement` (`propagation.py:377-379`),
   `magnitude ← classify severity` (`synthesize.py:115-116`), `asof = item.published_at`
   (`synthesize.py:112`, the lookahead-honesty anchor).

So the Camillo **DETECT = trend velocity / "exploding trends"** primitive is *absent*: the
system fires on the *presence of a severe keyword*, not on *interest accelerating
week-over-week above baseline*. `social.py` reframes Google-Trends "rising interest" as a
headline, but the magnitude still comes from a keyword lexicon, not from the slope of the
interest curve.

---

## 2. DECISION today — where signals fuse, and what kind of "ensemble" we have

Flow: `views = [analyst.analyze(ctx) ...]` (`advisor.py:931`) → `BMAAggregator().aggregate(views, ctx)`
(`advisor.py:972` / `bma.py:305`) → `DefaultRiskGate().gate(signal, market, portfolio, halt)`
(`advisor.py:991`) → autonomous adds `silence_bias_gate(advisor_result, ...)`
(`autonomous.py:386`). LLM committee paths (`deliberative.py`, `llm_committee.py`) are
default-OFF behind `HERMES_QUANT_DELIBERATIVE` and sit beside, not inside, `recommend()`.

**Where semantic/social FUSE:** as one more `AnalystView` in the BMA peer-view path. The
semantic analyst emits a normal `AnalystView` (`semantic.py:132-141`) weighted by track
record like any analyst (`bma.py:432-445`). A disagreeing high-confidence semantic view
*reduces* aggregate confidence (vote-share dissent branch, `bma.py:528-529`) rather than
overriding — correct per rails. The consumer-trend (social-arb) class is haircut to 0.5
before it even enters BMA (`synthesize.py:53-66,105-106`), so social-arb is a deliberately
weak peer view.

**Cross-SOURCE vs cross-ANALYST — the core distinction:** the only "no signal fires alone"
guard is `require_ensemble` + `n_distinct_analysts >= 2` (`bma.py:498-519`). That is
**cross-ANALYST agreement at the DECISION layer** — TA + Kronos + semantic must concur. The
Camillo **VALIDATE = data convergence** primitive ("real only when the trend shows across
MULTIPLE independent sources: social + Trends + web traffic + cards") is **cross-SOURCE
agreement at the PERCEPTION layer**, and it does **not exist**. Reddit + Trends are merely
`dedupe_items`-merged (`social.py:247`) and `load_packets_for` collapses to best-per-stance
(`synthesize.py:220-232`); neither *requires or rewards* two independent sources confirming
the same trend before a packet is trusted. (A single Reddit headline mentioning a strong
keyword produces a packet with the same confidence ceiling as a multi-source convergence.)

**Sizing is portfolio-blind + saturation-blind.** The advisor gates against a *synthetic
flat 100k portfolio* (`advisor.py:128`, `_EmptyHaltState` at `:107`) and the Kelly fraction
is confidence-driven (`risk/gate.py`). There is no term for *remaining edge-time*: nothing
reads "has Wall Street caught up / has the trend saturated" — the Camillo **EXIT on
information parity** concept has no representation in `MarketState`, `AggregatedSignal`, or
the gate (grep: 0 `saturation`/`edge_decay`/`information_parity` in `hermes_quant/`).

---

## 3. REACTION today — what can / can't fire

- `autonomous.tick` (`autonomous.py:255`): mode gate (`:284`) → kill-switch (`:296`) → per
  watchlist entry `advisor_recommend` (`:355`) → `silence_bias_gate` (`:386`) → on FIRE,
  per-tick cap (`:403`), optional portfolio caps (`:428`, OFF), optional admissibility
  (`:463`, OFF) → `_react` → `PaperReactor.execute` (`:503`).
- `PaperReactor.execute` (`react/paper.py:54`) is **equity-pathed** — it special-cases
  `proposal.asset_class == "equity"` for late-session handling (`react/paper.py:55-65`) and
  there is no multi-leg/option fill path. `proposals.py` emits the proposal dicts.
- **Multi-leg options: cannot fire.** `react/live.py` is an inert type-gated stub —
  `LiveBroker.submit_mleg_order` raises `NotImplementedError` (`react/live.py:97-100`) and
  `LiveBroker` can only be constructed with a `LiveTradingApproval` whose `__init__` enforces
  ≥100 paper outcomes / Sharpe-95%CI-lower ≥1.0 / ≤1% DD (`react/live.py:35-45`). So the
  whole option-play half of the playbook (covered_call/csp/wheel/leaps) is filtered out
  upstream (`EQUITY_PLAYS` in `quant-playbook-tick.py`). Reaction today = **paper equity
  only**.

---

## 4. The gaps vs PDR + the social-arbitrage method (consolidated)

| # | Gap | Evidence (file:line) | PDR stage / Camillo step it breaks |
|---|---|---|---|
| **A** | **No trend-velocity primitive.** Severity-on-keywords on one headline; "velocity" is comment-only. | `classify.py:112-139`; `social.py:18,82,152,181`; no `velocity`/`accelerat` logic in catalyst | PERCEPTION / DETECT |
| **B** | **No data-convergence at perception.** Cross-source corroboration is never required; only cross-analyst at decision. | `bma.py:498-519` (cross-analyst); `social.py:247` + `synthesize.py:220-232` (sources merely merged/collapsed) | PERCEPTION / VALIDATE |
| **C** | **No information-saturation / edge-decay.** Sizing = confidence×Kelly; no remaining-edge-time term. | `risk/gate.py` Kelly; grep 0 `saturation`/`edge_decay` | DECISION / EXIT (information parity) |
| **D** | **Semantic packets reach 1 of 3 live paths.** Only the interim cron loads `market_extras`; the autonomous + playbook ticks don't. | wired: `quant-daily-interim.py:127-141`; NOT wired: `autonomous.py:355-360`, `quant-playbook-tick.py:465`; silent abstain at `semantic.py:156` | PERCEPTION→DECISION wiring (flag/wiring decoupled) |
| **E** | **No unified `PerceptionFrame`.** Signals enter via disjoint side-channels (`bars`, `extras["regime"]`, `extras["semantic_packets"]`); no single provenance object. | `protocol.py:57-82` (MarketContext = bars + opaque `extras`); regime merge `advisor.py:861`; packet merge `semantic.py:148` | The PDR "Perception" boundary itself |

LINK (trend→ticker) is the one Camillo step the system **already has** — the curated signed
propagation graph (`propagation.py:319`, `propagate`), with noisy-OR×agreement confidence
(`propagation.py:377-379`) and the hand-curated `effect_sign` it flags as its highest-risk
choice (the OPEC edge was removed after it mis-signed XOM/CVX, `propagation.py:96-103`).

---

## 5. Smallest set of new abstractions to make the pipeline cleanly PDR-shaped (rails-preserving)

All four are **perception-layer, evidence-only, default-OFF, eval-gated** — none touches the
deterministic gate's authority. They make the *honest* version of social-arb expressible
without amplifying any signal.

1. **`TrendVelocity` perception primitive (fixes A).** A producer that computes
   week-over-week acceleration of an interest series (Trends/Reddit-mention counts) vs its own
   trailing baseline, emitting a *velocity score* + *baseline z* rather than a keyword
   severity. Drop-in upstream of `synthesize_packets`; packet `magnitude` sourced from
   velocity, not lexicon. Lookahead-honest by construction (only past observations). Behind a
   new `HERMES_QUANT_TREND_VELOCITY` flag, gated by the existing D74.7 eval harness.

2. **`ConvergenceValidator` at perception (fixes B).** A pure function over the *set* of
   `CatalystItem`s for a (trend, symbol): require ≥2 independent `source` families
   (reddit / google_trends / news / traffic) before a packet is emitted at full confidence;
   single-source → haircut or drop. This is `require_ensemble` *relocated to the perception
   layer* (cross-SOURCE), complementary to BMA's cross-ANALYST guard — and exactly the
   Camillo VALIDATE step. Wire into `synthesize_packets` before the packet is written.

3. **`saturation` / `edge_decay` field on the packet + a gate-side decay term (fixes C).** Add
   an Investor-Saturation estimate (e.g. trend age past peak, or "earnings/credit-card
   confirm date passed") to `SemanticPacket.metadata`; the risk gate reads it as a
   *confidence multiplier that can only shrink toward 0* (silence-by-default safe — it can
   exit/silence, never enlarge). Models the time-decaying asymmetry without a new sizing
   ladder.

4. **`PerceptionFrame` carrier object (fixes D + E).** A single dataclass —
   `{symbol, asof, numerical_views, semantic_packets, regime, trend_velocity, convergence,
   provenance}` — built once per symbol and threaded into `recommend()` in place of the ad
   hoc `market_extras` dict. Centralizing perception (a) gives every live path the *same*
   loader so the flag/wiring decoupling (D) disappears — one call site populates the frame
   for autonomous, playbook, and interim ticks; (b) gives the decision layer one
   provenance-carrying input (E) for evidence-store linkage. It is a **container, not an
   authority**: BMA still treats each carried signal as a peer view, the deterministic gate
   is still final. This is the one structural change; the other three are perception
   producers that fill its fields.

These four are additive and default-OFF: with every flag off, the pipeline is bit-for-bit the
current 3-analyst numerical ensemble → BMA → deterministic gate → silence-bias → paper.
