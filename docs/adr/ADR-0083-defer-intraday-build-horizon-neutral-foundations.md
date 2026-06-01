# ADR-0083: Defer long-horizon intraday; build the horizon-neutral foundations first

- **Status:** proposed
- **Date:** 2026-05-31
- **Deciders:** operator (Codeseys), hermes-quant architect
- **Relates to:** ADR-0068 (decision-time vs bar-time), ADR-0069 (still-forming-bar discipline),
  ADR-0072 (advisor intraday open-guard), ADR-0010/0049 (settlement journal)
- **Context study:** [docs/research/2026-05-31-r-strategy-openness-and-horizon.md](../research/2026-05-31-r-strategy-openness-and-horizon.md)

## Context and problem statement

hermes-quant is fundamentally INTERDAY: every production cron hardcodes the `1d` timeframe, idempotency
is per-ET-day, and the risk/cost/vol model is dimensioned per-day. A partial multi-horizon fan-out exists
but extends only UPWARD (1d→1w/1M/1Q), never to intraday (`data/horizon_cache.py:44` — resample-only,
no downsample). Intraday plumbing exists in three places (advisor lookback constants, three analysts'
timeframe lists, the timeframe-agnostic no-lookahead chain) but is never wired into the crons.

The question: should we build a long-horizon intraday mode (multi-hour holds on 1h/15m bars)? If so,
what exactly must change — `open_guard`, the gate's per-day budgets, bar cadence, settlement, no-lookahead?

External evidence: hermes' edge (catalyst + social-arb) is naturally multi-day; the *intraday* slice of
that alpha decays in minutes (Context Analytics; tweet half-life ~80 min), and the hourly tick (read-only,
delegates to 1d) would enter after it has decayed. A flat-by-close mode would forfeit the overnight risk
premium the daily system already harvests (Concretum). PDT is irrelevant (paper-only + sunsetting
2026-06-04). Most importantly, **the eval gate cannot currently measure a multi-hour hold** — settlement
v0.1.1 computes per-fill slippage only (`settlement_loop.py:40-48`), with no exit-fill joining — so an
eval-gated intraday rollout can never legitimately pass.

## Decision drivers

- **Measurability:** an eval-gated capability that cannot be measured cannot be rolled out. Settlement is
  the binding constraint.
- **Economic motivation:** the system's edge is interday; intraday targets alpha it cannot reach fast
  enough and would forfeit the overnight premium.
- **Rails:** the deterministic gate, the discrete sizing ladder, and the kill-switch are immutable; only
  the Kelly/cost INPUTS feeding the ladder would ever need horizon scaling.
- **Latent correctness hole:** `drop_still_forming_bar` early-returns for non-daily TFs
  (`bar_alignment.py:117`) — a no-lookahead honesty bug that bites the instant any intraday read happens,
  worth fixing regardless of intraday.

## Considered options

### Option 1 — Build intraday now (BUILD)
Wire an intraday cron, re-key idempotency, scale the sizing inputs, ship behind a flag.
- Good: capability now.
- Bad: **unmeasurable** (settlement can't score a multi-hour hold → eval can never pass); economically
  unmotivated; high-turnover surface with no demonstrated edge; risks the timeframe-switching pathology.
  **Rejected.**

### Option 2 — Never build intraday (DO_NOT)
Declare interday-only permanently.
- Good: simplest posture.
- Bad: forecloses a legitimate "finer-entry, multi-session-hold" variant that could harvest the same
  multi-day edge with better entries, should a measurable edge ever appear. Over-commits. **Rejected.**

### Option 3 (CHOSEN) — DEFER intraday; build the two horizon-neutral foundations now
Phase 0 (now): the still-forming-bar fix + settlement v0.1.2 exit-fill join. Phase 1 (only if a
measurable edge later justifies it, behind a default-OFF flag): bar-time idempotency, horizon-scaled
sizing inputs, intraday session model, Kronos horizon labeling, an intraday cron host.
- Good: ships real correctness value now (the honesty fix + the measurement instrument every eval depends
  on); keeps the door open without committing turnover; rails-respecting (Phase 0 touches no rail).
- Bad: intraday capability is deferred (acceptable — it is unmeasurable and unmotivated today).

## Decision

**DEFER.** Adopt Option 3.

**Phase 0 (DO NOW — horizon-neutral, ship regardless of intraday):**
1. Fix `drop_still_forming_bar` (`bar_alignment.py:117`) with a per-timeframe bar-boundary/session-close
   cutoff for intraday TFs instead of the `non_daily_timeframe` early-return.
2. Land settlement v0.1.2 exit-fill join + horizon-return math (`settlement_loop.py:29-48`), un-gating
   calibrator updates once direction-over-horizon is computed (`_calibration_quality` →
   `horizon_return`).

**Phase 1 (ONLY IF a measurable, demonstrated edge later justifies it — default-OFF flag, eval-gated):**
re-key `open_guard`/`fired_today`/`fired_today_pairs` on `(symbol, direction, bar_ts)`; horizon-scale the
Kelly/cost INPUTS via √(horizon) (never the ladder); intraday session model for the daily-loss breaker +
`_next_session_open`; fix Kronos horizon labeling (`kronos.py:107,747`); stand up a true intraday cron.
Design stance: hold ACROSS the close when the daily thesis is long (a multi-session-hold variant, not
flat-by-EOD).

## Consequences

**Positive:**
- Phase 0 closes a latent no-lookahead bug and delivers the settlement measurement instrument every
  eval gate (daily AND any future intraday) depends on.
- A clean, rail-respecting reason to defer (unmeasurable + unmotivated) rather than over-commit either way.

**Negative / risks:**
- Intraday entries are deferred; the system continues to enter on daily bars.
- Phase 1, if ever built, is non-trivial (touches `open_guard` ADR-0072 core, two tick journals, the
  sizing inputs, Kronos, and a new cron). Mitigation: it is gated on a measurable edge that cannot be
  demonstrated until Phase 0 settlement lands — so the sequencing is self-enforcing.

## Rails preserved

- The deterministic gate, the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`, and the kill-switch
  are untouched in Phase 0 and immutable in Phase 1 (horizon scaling applies only to the Kelly/cost
  inputs that FEED the ladder, never the ladder).
- Phase 0 is pure correctness (no behavior change on the daily path beyond honest still-forming-bar
  handling and a richer-but-equivalent settlement record).
- Any Phase-1 intraday mode is default-OFF, off-state byte-identical, and eval-gated — and cannot pass
  the eval until Phase 0's settlement v0.1.2 ships.
