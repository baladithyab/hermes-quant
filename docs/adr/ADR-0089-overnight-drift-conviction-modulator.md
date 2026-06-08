# ADR-0089: OvernightDriftAnalyst — a zero-turnover conviction modulator on hold-through-close daily positions

**Status:** Proposed (2026-06-08) — design pinned, NOT yet implemented
**Date:** 2026-06-08
**Wave:** Perception extension (overnight-drift awareness; default-OFF, eval-gated)
**Supersedes:** nothing
**Cites:** [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate — FINAL IMMUTABLE authority; this analyst is upstream perception, never touches the ladder), [ADR-0079](ADR-0079-perception-decision-reaction-architecture.md) (analyst-pool → BMA → gate; PerceptionFrame extras; consumers ignore unknown fields), [ADR-0084](ADR-0084-scheduled-event-calendar-and-pre-event-guard.md) (the default-OFF + eval-gated + asof-honest pattern this clones)
**Grounded in:** `docs/research/2026-06-08-r-overnight-drift-anomaly.md` (the study) + the 2026-06-08 read-only spike (`SPY/AAPL/TSLA/GME/KO`, 2023–2024 real bars — see Consequences §Spike findings).

> **Default-OFF and eval-gated.** With `HERMES_QUANT_OVERNIGHT_DRIFT` absent/`0`, no `overnight_drift` view is produced, the analyst is not in the loadout, and behavior is byte-identical to today. Promotion requires a flag-ablation that clears the standard bar (`d_sharpe ≥ +0.10` AND `DSR > 0.50`), measured via the C2a-style carrier harness.

---

## Context

The originating directive (Codeseys, Discord #hermes-quant, 2026-06-08) ingested three trading-content reels and asked to build overnight-drift awareness into the pipeline. Recon (backlog audit C1) confirmed hermes-quant has **no overnight/intraday return decomposition** — the advisor reads `MarketContext.bars` close-to-close only.

The research note settles the empirics: the overnight-vs-intraday split is **real and persistent** (close→open grew $1→$17 over 30y while open→close grew $1→$1.20 in SPY; positive overnight in 20/23 years), BUT the headline cross-sectional long-short (~38% gross) is **destroyed by cost** (1bp/side ≈ −5%/yr; AlphaArchitect +717% gross → −32% net). The ONLY harvestable form is **structural, for free**: any interday system that holds through the close already earns the overnight drift as part of total return.

## Decision drivers

- **D-1 The deterministic gate (ADR-0004) is untouched.** This is an upstream PERCEPTION analyst that emits an `AnalystView` into the BMA pool like any other. It never sizes, never touches the `{0,±0.05,…,±0.20}` ladder, never adds a gate rule. It can only shift conviction within the existing aggregation.
- **D-2 Zero added turnover.** The signal modulates conviction on positions the system would hold through the close ANYWAY. It must NOT introduce an open/close round-trip — that is the cost-killed form the research explicitly rejects.
- **D-3 Asof-honest, no new feed.** Overnight = `open[t]/close[t-1]−1`; intraday = `close[t]/open[t]−1`. Both are in `MarketContext.bars` (open+close columns), all ≤ asof by the engine's no-lookahead contract. No new data dependency.
- **D-4 Adaptive, not assumed.** The spike (below) shows the per-name overnight tilt is REGIME/PERIOD-dependent, not a stable "meme = overnight" constant. The signal MUST be a trailing rolling spread recomputed per name, never a hardcoded cohort assumption.
- **D-5 Default-OFF + eval-gated (clone ADR-0084).** Byte-identical when OFF; promoted only after a real-data flag-ablation clears the bar.

## Considered options

### Option A — Do nothing
Leaves the directive's C1 unbuilt. Rejected: the operator asked for it and the structural (free) form is real.

### Option B — Dedicated overnight/intraday long-short sleeve
Trade the cross-sectional spread (long high overnight-minus-intraday, short converse). **Rejected** — this is the exact form the research proves dies to cost (2× daily full-book turnover; net Sharpe ≈ 0-to-negative). Building it would manufacture gross-return illusions. The research note's guardrail (c): treat L/S as a *diagnostic*, never a book.

### Option C — CHOSEN: zero-turnover conviction modulator
An `OvernightDriftAnalyst` that, per name, computes the trailing rolling overnight-minus-intraday spread and emits an `AnalystView` whose direction/confidence NUDGES the existing daily long thesis: a name that earns its return overnight is a *better* hold-through-close candidate (flattening into the close would forfeit it); an intraday-driven name gets no nudge. Enters BMA as a PEER view (never an override; subject to the same dissent-aware capping as every analyst). Zero added turnover by construction — it modulates conviction on holds, it does not propose round-trips.

## Decision

**Adopt Option C.** It is the only form the research supports as net-harvestable (structural/free), it respects the immutable gate (D-1), adds no turnover (D-2) and no feed (D-3), is adaptive per the spike (D-4), and ships default-OFF + eval-gated (D-5). The dedicated L/S sleeve (Option B) is a "reopen ONLY if a fat net edge ever appears" deferral, not this ADR.

## Consequences

### Positive
- Captures the documented overnight premium at zero incremental cost on positions already held through the close.
- Pure perception addition; the deterministic authority (ADR-0004) is unchanged; the analyst is one peer vote among many.
- No new data dependency — reuses `bars` open+close.

### Spike findings (2026-06-08, read-only, real bars, ZERO writes)
`SPY/AAPL/TSLA/GME/KO`, 2023-01-01→2024-12-31, annualized:

| symbol | overnight | intraday | on−id (signal) | hold-through |
|---|---|---|---|---|
| SPY | +12.2% | +10.6% | **+1.6%** | +22.8% |
| KO | +2.8% | −2.6% | **+5.5%** | +0.1% |
| TSLA | +35.8% | +48.6% | −12.7% | +84.9% |
| AAPL | −3.5% | +41.6% | −45.1% | +37.6% |
| GME | +39.5% | +77.4% | −37.9% | +94.8% |

**Two load-bearing findings:** (1) the signal is computable, finite, and discriminates names (design confirmed). (2) **CRITICAL nuance** — in this window the *intraday* leg dominated for the high-beta names (AAPL/TSLA/GME), the OPPOSITE of the longer-horizon meme-cohort overnight tilt the research note cites. The split is REGIME/PERIOD-dependent. This RATIFIES D-4: the analyst must use a trailing rolling spread (adaptive per name + period), never a static "meme=overnight" assumption. It also reinforces the modulator (not sleeve) stance — the sign is not a stable tradeable constant.

### Negative / risks
- The exploitable edge is fragile (known, award-winning, retail-driven; decay risk). Mitigated by default-OFF + eval-gate + modulator-only stance (no capital at risk on the signal directly).
- Risk of scope-creep into Option B. Mitigated by D-2 (zero-turnover is a hard design invariant; a round-trip proposal is out of scope by ADR).

## Acceptance gate (must be green before status → Accepted)
1. `OvernightDriftAnalyst` emits an asof-honest `AnalystView` (trailing rolling spread; reads only bars ≤ asof). Unit-tested for no-lookahead.
2. Wired into the loadout behind `HERMES_QUANT_OVERNIGHT_DRIFT=1` ONLY; flag-OFF is byte-identical (test).
3. Zero-turnover invariant test: enabling the analyst changes conviction/weights but introduces NO new open/close round-trip vs the flag-OFF book.
4. A real-data flag-ablation (C2a-style carrier harness, the OvernightDrift flag) clears `d_sharpe ≥ +0.10` AND `DSR > 0.50` on a representative window. Until then it stays default-OFF — measured, not assumed (the C2 EVENT_RISK HOLD is the precedent).
