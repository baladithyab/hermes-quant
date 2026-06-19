---
status: accepted (2026-06-18, eval-gate-pending) — code shipped DEFAULT-OFF (HERMES_QUANT_SLIPPAGE_HAIRCUT/SLIPPAGE_GATE); thresholds confirm across >=2 clean windows before arming
date: 2026-06-17
deciders: [codeseys]
consulted: [deep-work-loop session 2026-06-17 (AEGIS P/D/R options architecture + operator slippage-risk requirement)]
amends: null
supersedes: null
---

# ADR-0097: Paper-vs-live slippage haircut — make the paper track record live-realistic before it gates capital

> **Operator risk requirement (2026-06-17):** account for slippage on the RISK side
> because Alpaca PAPER may fill more optimistically than Alpaca LIVE. A track record
> built on optimistic paper fills overstates live profitability — and that record is
> exactly what the ADR-0029 evidence gate and the ADR-0125 promotion gate consume.
> This ADR makes the paper P&L *live-realistic* (conservative) so the evidence means
> something. Built DEFAULT-OFF + eval-gated; fail-CLOSED (an unknown estimate haircuts
> MORE, never less).

**Cites:** [ADR-0070](ADR-0070-paper-execution-fidelity.md) (the internal v0.2 slippage
model this complements), [ADR-0029](ADR-0029-multi-leg-paper-reactor.md) (the
evidence-before-live gate whose N>=100 outcomes must be live-realistic),
[ADR-0096](ADR-0096-pre-autonomous-decision-quality-gates.md) (decision-quality gates —
this is the execution-quality sibling), [ADR-0004](ADR-0004-risk-gate.md) (the gate the
haircut feeds, never replaces).

---

## Context and Problem Statement

There are TWO slippage worlds in the codebase and a blind spot between them (verified
2026-06-17):

1. **Internal simulator** (`hermes_quant/react/slippage_model.py` v0.2, ADR-0070):
   haircuts the SYNTHETIC fills of `PaperReactor` (`react/paper.py:431`),
   `DeterministicEquityReactor` (`react/deterministic_equity.py:537`), and
   `MultiLegPaperReactor` (`react/multileg.py:720`). Generic spread + impact +
   latency-drift + auction. It uses a FIXED bootstrap vol (0.012/day equity), so it
   already under-prices high-beta names — and it is NOT calibrated to Alpaca specifically.

2. **Real Alpaca-paper broker** (`hermes_quant/react/alpaca_paper.py`): records the
   broker-reported `filled_avg_price` RAW (line ~300) — no simulator haircut. This is
   *correct* for a real broker (you record what filled). But Alpaca's PAPER engine fills
   more optimistically than Alpaca LIVE (it fills near mid without real queue/liquidity
   pressure), so `filled_avg_price` from Alpaca-paper is itself optimistic.

**The blind spot:** when `HERMES_QUANT_ALPACA_PAPER=1` is flipped on, the system
simultaneously (a) stops applying the synthetic haircut AND (b) trusts Alpaca-paper's
optimistic fills at face value. Net: recorded P&L becomes MORE optimistic than live, not
less. The ADR-0029 evidence window (N>=100, sharpe_95ci_lower>=1.0) and the ADR-0125
promotion gate would then be built on a paper-optimistic return series — a track record
that looks promotable while measuring better-than-live execution.

**What already exists (measurement — to be CONSUMED, not reinvented):** the
`HERMES_QUANT_ALPACA_SHADOW=1` hook (`hermes_quant/react/alpaca_shadow.py`,
`SHADOW_DIVERGENCE_PATH`, `_build_divergence`) records, per fill, the divergence between
Alpaca's REAL paper fill and the synthetic PaperReactor fill to
`~/.hermes/quant/alpaca-shadow-divergence.jsonl`; `quant-alpaca-shadow-compare.py` (armed
cron) reports it. **The gap is a CONSUMER:** nothing converts the measured divergence into
a conservative risk haircut. A codebase grep for `live_slippage` / `broker_slippage` /
`paper_vs_live` / `slippage_buffer` / `execution_penalty` returns nothing.

The AEGIS architecture plan (2026-06-17, the P/D/R options epic) sequenced 14 increments
but did NOT mention slippage/paper-vs-live at all — confirming this is a real, unaddressed
risk axis, not a covered one. Options make it worse: options spreads are wider, per-contract,
and an MLEG fills leg-by-leg, so the equity v0.2 model badly under-models options execution.

## Decision Drivers

- **The evidence base must be live-realistic.** ADR-0029/0125 consume the paper record to
  gate real capital; an optimistic record is a fail-open into live money.
- **Fail-CLOSED on uncertainty.** Thin/absent divergence data must haircut MORE (assume
  worse live fills), never less — silence-by-default applied to execution cost.
- **Options need their own model.** Equity v0.2 does not cover wide options spreads / MLEG
  leg-by-leg fills; a separate, larger options penalty is required.
- **Reuse the measurement.** The ALPACA_SHADOW divergence log already exists; the haircut
  estimates from it (with a conservative static prior until N fills accrue), not from scratch.
- **Default-OFF + byte-identical when off** (repo convention); the gate stays final authority.

## Considered Options

- **A — Do nothing; trust Alpaca-paper fills.** Status quo. Fails the operator requirement;
  bakes an optimistic bias into the capital-gating evidence.
- **B — Only widen the internal v0.2 model.** Helps the synthetic path but does NOT touch
  the real-broker path (where the optimism actually enters when ALPACA_PAPER=1).
- **C — A paper-vs-live execution-penalty haircut that (1) estimates a per-asset-class (and
  per-structure, for options) penalty from the ALPACA_SHADOW divergence log with a
  conservative static prior, (2) applies it as a haircut-toward-silence at the gate/sizer,
  and (3) marks paper P&L conservatively so the promotion/evidence series is live-realistic.**
  Fail-closed on thin data.
- **D — Defer until live.** Rejected: the whole point is that the PAPER record (pre-live) is
  what gates the live decision; deferring defeats the evidence gate.

## Decision Outcome

Chosen: **Option C** — a default-OFF paper-vs-live slippage haircut, `HERMES_QUANT_SLIPPAGE_HAIRCUT`.

Three components, each default-OFF + byte-identical when off:

1. **Estimator** (`hermes_quant/risk/slippage_haircut.py`): `estimate_live_penalty(asset_class,
   structure_kind, *, shadow_log=SHADOW_DIVERGENCE_PATH) -> float` (a penalty in bps/fraction).
   Reads the ALPACA_SHADOW divergence log; computes a conservative (e.g. high-percentile, not
   mean) per-(asset_class, structure) penalty. If fewer than N samples exist, returns a
   CONSERVATIVE STATIC PRIOR (equity wider than the v0.2 mid-estimate; options materially
   wider; MLEG = sum of per-leg priors) — never 0. Finite-guarded (ar08 family): a NaN/inf/
   absent estimate => the prior, never a free pass.

2. **Gate/sizer haircut-toward-silence:** the estimated live penalty is subtracted from the
   play's expected edge BEFORE the gate's admit decision (or applied as an edge haircut feeding
   the sizer). A play whose edge < estimated live slippage is SILENCED. This is additive to the
   ADR-0004 gate (the gate stays final; the haircut can only subtract/silence, never amplify).

3. **Conservative P&L marking for the evidence series:** the realized return series the
   ADR-0029 evidence gate + ADR-0125 promotion gate consume is marked with the live penalty
   applied (a `live_realistic` variant alongside the raw paper P&L), so `sharpe_95ci_lower` is
   computed on the conservative series. Raw paper P&L stays for debugging; the GATE reads the
   haircut series.

### Consequences

- **Positive:** the evidence/promotion gates see a live-realistic (conservative) record — the
  paper-to-live transition stops being a fail-open. Closes the operator-identified blind spot.
- **Positive:** consumes the existing ALPACA_SHADOW measurement (no new instrumentation).
- **Positive:** options get a structure-aware penalty far larger than the equity model — the
  AEGIS options epic inherits realistic execution cost from the start.
- **Negative / accepted:** a conservative haircut SILENCES marginal plays and lowers reported
  paper Sharpe — by design. A play that only profits at optimistic fills SHOULD be silenced.
- **Negative / accepted:** the static prior is a guess until N shadow samples accrue; it is
  deliberately pessimistic so the error is on the safe (over-haircut) side.
- **Neutral:** this is execution-quality; it pairs with ADR-0096's decision-quality gates —
  both make the paper record trustworthy, neither promises profit.

### Confirmation

Satisfied by: (1) a test that a play whose edge < estimated live penalty is SILENCED at the
gate; (2) a test that the evidence/promotion return series is the haircut (conservative) series,
not the raw paper series; (3) a fail-closed test — an empty/NaN shadow log yields the
conservative prior (a positive penalty), never 0; (4) an options test — an MLEG penalty >= sum
of per-leg equity-scale priors and materially > the equity penalty; (5) byte-identical-when-off:
with `HERMES_QUANT_SLIPPAGE_HAIRCUT` unset, the gate/sizer/evidence series are unchanged.

## More Information

- Sequence: this is a **cross-cutting risk increment** that must land BEFORE the AEGIS options
  evidence window (AG-OPT-EV-1) and before any `ALPACA_PAPER=1` flip — the evidence those gates
  collect must already be live-realistic. It also improves the equity evidence window (AG-EQ-1).
- Filed as seed `aegis-sl01` under the AEGIS epic; the options-specific penalty is `aegis-sl02`.
- Honest scope: this makes the record *conservative/trustworthy*; it does not create edge. It
  will make the reported numbers WORSE (correctly) — a strategy that only wins on optimistic
  fills is not a strategy.
