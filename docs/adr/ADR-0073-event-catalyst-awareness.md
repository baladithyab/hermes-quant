# ADR-0073: Event/catalyst awareness — universe onboarding, semantic analyst activation, intraday cadence

**Status:** Accepted (2026-05-29) — semantic-analyst activation + intraday cadence implemented; universe-onboarding axis remains Proposed (gated OFF behind default-OFF flag per ADR-0075 until the eval-gate is green)
**Date:** 2026-05-29
**Wave:** E (signal-surface expansion — beyond technical/liquidity)
**Supersedes:** nothing
**Amends:** [ADR-0014](ADR-0014-portfolio-context-deferred.md) (single-symbol scope), [ADR-0018](ADR-0018-analyst-loadout.md) (fixed TA loadout), the Alpaca liquidity universe scanner
**Cites:** `hermes_quant/universe/alpaca_scanner.py`, `hermes_quant/advisor.py:_build_default_analysts`, `hermes_quant/analysts/semantic.py`, `hermes_quant/semantic.py` (SemanticPacket), `hermes_quant/grounding/current_clear.py`

---

## Context

On 2026-05-28 ~21:00 ET, Blue Origin's New Glenn exploded during a hotfire test at Cape Canaveral. The space sector sold off sharply the next session — ASTS −17%, RKLB and LUNR both down materially (a competitor-failure + SpaceX-IPO-de-risking move). The operator asked why the system had no relation to LUNR/RKLB and didn't react to the catalyst.

Forensic answer: the system is **structurally blind to event/catalyst information at every layer.** Three independent gaps, all confirmed in code:

### G1. The universe is a pure liquidity screen — no catalyst onboarding path

`hermes_quant/universe/alpaca_scanner.py` selects: tradable + fractionable on Alpaca, last close ∈ [$5, $500], 30-day avg dollar volume ≥ threshold, ranked by dollar volume, capped at `max_symbols`. The live universe is 153 names; **LUNR and RKLB are not in it** (only ASTS, BA, RTX touch space/aero, and ASTS only incidentally via liquidity). There is no mechanism to onboard a symbol *because something happened to it* — no news scanner, no unusual-volume detector, no sector watchlist, no event-driven expansion. Zero signals and zero executions on LUNR/RKLB, ever. A name outside the top-N by dollar volume is invisible.

### G2. The analyst loadout is 100% price/volume; the semantic analyst exists but is dark

`_build_default_analysts()` (ADR-0018) runs exactly three: ClassicalTA + MicrostructureLite + Kronos — all pure OHLCV time-series models. `hermes_quant/analysts/semantic.py` (a news/sentiment analyst) is in the repo but **not in the default loadout** — it never instantiates in the daily run. So even for ASTS (which *was* in the universe and *did* move −17%), the system saw only "price dropped" via TA after the fact, with no link to the headline and no ability to anticipate from it.

Worse: `SemanticAnalyst.analyze()` is deliberately model-free — it **consumes precomputed `SemanticPacket`s from `MarketContext.extras`** that an upstream job must write ahead of time. That upstream news-ingestion job **does not exist**. The semantic pipeline is scaffolded end-to-receiver but headless: no producer writes packets, so even wiring the analyst into the loadout would no-op against empty `extras`.

### G3. Daily-bar cadence misses post-close catalysts entirely

The advisor runs on `1d` bars off the prior close (the same stale-`bar_ts` surface ADR-0072 touched). The explosion was Thursday 21:00 ET — after Thursday's close. The next look was Friday premarket on Thursday's daily bar, which didn't contain the move. By the time a daily bar reflected it, the −17% had already printed. There is no intraday or event-triggered tick.

**Common cause:** the system is a *technical, liquidity-universe, daily-cadence* trader by design. Event/catalyst awareness is a signal surface it has never crossed. A named, sector-moving catalyst is precisely the class of signal it cannot see — not a bug, a scope boundary.

---

## Decision

Cross the boundary in three phases, smallest-blast-radius first. Each phase is independently shippable and default-OFF until validated.

### D73.1 Phase 1 — Catalyst onboarding into the universe (highest leverage)

Add a second universe contributor alongside the liquidity scanner: a **catalyst/unusual-activity onboarder** that can pull a symbol in regardless of baseline dollar volume.

Two complementary sources, OR'd into the universe:

1. **Unusual-volume / unusual-range scanner.** Daily pass over a broad reference list (e.g. Alpaca's full tradable set, or a sector-tagged seed list) flagging names whose today (or last-session) volume or true-range is ≥ N σ above their trailing baseline. Catches catalyst-driven moves *mechanically* without needing to know the news — a rocket explosion shows up as anomalous volume on RKLB/LUNR even if no text feed exists yet. This is the cheapest path to "we'd have at least *seen* LUNR move."
2. **Sector-watchlist seed.** A curated, operator-editable map of thematic baskets (space/aero, biotech, semis, …) whose members are always scanned regardless of liquidity. Makes the universe deliberately thematic rather than purely top-N-liquid. LUNR/RKLB/ASTS/RDW live in a `space` basket.

Onboarded names are tagged with provenance (`source: liquidity | unusual_volume | sector_watchlist`) so downstream sizing/risk can treat catalyst-onboarded names more conservatively (they're outside the liquidity comfort zone the gate was tuned on). A separate, tighter per-symbol cap for catalyst-onboarded names is recommended (they're often thinner and gappier).

**Phase-1 alone would have surfaced LUNR/RKLB** as anomalous-volume movers the morning after the explosion, putting them in front of the (technical) analysts even with no news feed.

### D73.2 Phase 2 — Activate the semantic analyst + build the missing packet producer

Two pieces, in order:

1. **Build the news-ingestion job** that writes `SemanticPacket`s into a store keyed by `(symbol, asof)`, which the advisor loads into `MarketContext.extras` at recommend-time. Source options (escalating cost): a free headline/RSS aggregator → a paid news API → an LLM-summarized sentiment packet per symbol. The packet schema already exists (`hermes_quant.semantic.SemanticPacket`, with `validate_semantic_packet`); the producer is the gap. Honor the ordered-tool-call + `current_clear()` purge discipline documented in `semantic.py` (tool calls complete before synthesis; purge stale tool messages between analyst stages).
2. **Wire `SemanticAnalyst` into `_build_default_analysts()`** behind `HERMES_QUANT_SEMANTIC_ENABLED=1` (default OFF, same pattern as the ADR-0064 FundamentalsAnalyst gate). It degrades gracefully to neutral when no packet is present for a symbol, so it's safe to enable before packet coverage is complete.

This is what connects "Blue Origin exploded" → "bearish stance on the space basket" as a *first-class analyst view* feeding the BMA aggregator, rather than waiting for price to confirm.

**Lookahead caution:** the semantic packet's `asof` MUST be the headline's publication time, and the advisor must only consume packets with `asof <= decision_time` (the ADR-0068 decision-time-honesty discipline applies to news exactly as to bars). A packet built from a 21:00 ET headline must not leak into a 16:00 ET same-day backtest bar. This is the single biggest fidelity trap in news integration — build the lookahead gate (`hermes_quant/evidence/lookahead_gate.py` already exists for evidence; reuse or mirror it for packets) before trusting any semantic-driven backtest.

### D73.3 Phase 3 — Event-triggered / intraday cadence

Add a cadence that doesn't wait for the next daily bar:

1. **Intraday re-evaluation** of catalyst-onboarded names on an intraday timeframe (the autonomous-tick layer already runs every 30 min in-market and reads a watchlist — extend it to consume the catalyst-onboarded set on a shorter bar). This addresses G3 for *next-session* reaction.
2. **(Deferred) true event trigger** — a webhook/poller that fires a recommend pass on a symbol within minutes of a catalyst, including after-hours. This is a larger architectural change (the system is cron-batch today, not event-driven) and is explicitly deferred; Phase 3.1 (intraday on the catalyst set) captures most of the value.

---

## Consequences

**Positive:**
- The system gains a path to *see* catalyst-driven movers (Phase 1), *understand* why (Phase 2), and *react* faster than next-day-daily-bar (Phase 3).
- Each phase is independently valuable: Phase 1 alone closes the "we never even saw LUNR" gap with pure mechanics, no text feed required.
- Reuses existing scaffolding: `SemanticAnalyst`, `SemanticPacket`, the lookahead gate, the autonomous-tick watchlist loop. The gaps are *producers and wiring*, not new core abstractions.

**Negative / risks:**
- **News lookahead is a severe fidelity trap** (D73.2). A naively-timestamped packet poisons every backtest it touches. The lookahead gate is a hard prerequisite, not an optimization.
- Catalyst-onboarded names are thinner/gappier than the liquidity universe; the gate and slippage model (ADR-0070) were tuned on liquid names. A tighter per-symbol cap + a wider slippage assumption for catalyst names is needed, or the paper book will overstate fill quality on exactly the names most prone to gapping.
- Sector watchlists are a curation burden and can stale. Provenance tagging + a periodic review keeps them honest.
- Phase 3 true-event-trigger breaks the cron-batch model and is correctly deferred — don't let "we should react in real time" pull the whole architecture toward streaming prematurely.

**Explicitly out of scope:**
- Direction/quality of the catalyst call — Phase 2 produces a *view*, the BMA aggregator + risk gate decide whether to act. This ADR adds the input, not a new decision authority.
- Real-time/streaming execution. Paper-only, batch + intraday-tick cadence remains the rail.

---

## Rollout

1. **Phase 1 first** — highest leverage, no fidelity trap, no text feed. Ship the unusual-volume scanner + sector-watchlist seed as additional universe contributors behind `HERMES_QUANT_CATALYST_UNIVERSE=1`. Validate: after enable, confirm LUNR/RKLB-class movers appear in the universe on a high-volume sector day.
2. **Phase 2 lookahead gate BEFORE producer** — build/verify the packet lookahead gate, then the producer, then enable `SemanticAnalyst` behind `HERMES_QUANT_SEMANTIC_ENABLED=1`. Validate against a known dated catalyst (replay 2026-05-28 with the packet's true 21:00 ET asof; assert it does NOT leak into the pre-close bar).
3. **Phase 3.1** — extend the autonomous-tick watchlist to include the catalyst-onboarded set on an intraday bar. Validate: a post-close catalyst name is re-evaluated at the next in-market tick rather than waiting for the next daily brief.
4. Phase 3.2 (true event trigger) deferred to a future ADR.
