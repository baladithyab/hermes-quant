# ADR-0090: Sizing-aware multi-name portfolio backtest mode for trustworthy flag verdicts

**Status:** Proposed (2026-06-08) — design + rationale pinned; build DEFERRED to operator greenlight (substantial new harness mode)
**Date:** 2026-06-08
**Wave:** Evaluation honesty (make the promote/HOLD gate trustworthy)
**Supersedes:** nothing
**Related:** ADR-0074 (flag-ablation harness), ADR-0089 (OvernightDriftAnalyst), the C2 EVENT_RISK verdict, and `docs/research/2026-06-08-multiname-fair-verdict-retest.md`

## Context

The flag-ablation harness (`run_flag_ablation` → `WalkForwardEngine` → `AdvisorStrategy`)
runs the advisor's raw per-symbol decisions as an **equal-notional single/multi-cohort
passthrough**: each BUY allocates `nav * size_fraction` at the asof close, with cost
applied, and there is **no portfolio construction** — no target-weight optimization, no
rebalancing schedule, no cross-name risk budgeting, no cash/leverage accounting beyond a
flat notional. As a result every leg's **absolute Sharpe is deeply negative** (≈ −11 to
−16 in the 2023-24 multi-name runs), regardless of the flag.

This is fine for the OFF-vs-ON **delta** (identical window/strategy/universe, so the
delta isolates the flag). But it breaks the **DSR gate**: DSR is the Probabilistic
Sharpe Ratio of the *observed leg Sharpe* — `P(true Sharpe > 0 | observed)`. With an
observed Sharpe of −11.43, PSR ≈ 0.000 **correctly** (an observed leg that negative is
overwhelmingly a real loser). So the DSR gate can NEVER pass on a passthrough leg, no
matter how good the flag's delta is.

This concretely blocks a real decision: **EVENT_RISK's multi-name fair re-test cleared
the Sharpe-delta gate (+0.231 > +0.10) with fewer trades and better drawdown — the
leading promote candidate — but HOLDs solely because the DSR gate can't certify a
−11.43-Sharpe leg.** We cannot promote (DSR un-passable by construction) and we cannot
honestly call it HOLD-on-merit (the Sharpe-delta gate passed). The verdict is stuck on a
harness limitation, not on the flag.

## Decision (proposed)

Add a **sizing-aware portfolio backtest mode** to the engine (or a sibling engine) that
turns advisor decisions into a real, investable book:

1. **Target-weight construction** — map per-name `(direction, confidence, magnitude)`
   into target portfolio weights (e.g. confidence-weighted, vol-scaled, capped per name
   and gross). Reuse the existing `target_weight.py` machinery where possible (ADR-0035
   wave already has resolve-target-weight logic).
2. **Rebalancing schedule** — rebalance to targets on a cadence (daily/weekly), with
   turnover + cost charged on the *delta* to targets (not a fresh full notional each
   bar), so costs are realistic and a zero-turnover modulator (ADR-0089) is correctly
   rewarded.
3. **Cash + leverage accounting** — track a real NAV with cash drag, gross/net exposure
   caps, so the absolute return/Sharpe is a plausible investable number.
4. **Same ablation surface** — `run_flag_ablation` gains a `portfolio_mode=True` (or a
   parallel `run_flag_ablation_portfolio`) so every existing flag can be re-judged with a
   non-degenerate absolute Sharpe → the DSR gate becomes meaningful.

With a plausible absolute Sharpe, the existing promote bar (`d_sharpe ≥ +0.10 AND
DSR > 0.50`) finally works as designed for ALL flags, not just deltas.

## Why deferred (not built in the same pass that surfaced it)

This is a **substantial new harness mode**, not a bug-fix: target-weight mapping,
rebalancing, turnover-accurate costing, and exposure accounting each have real design
choices with money-software consequences (a wrong cost model flatters every flag; a
wrong weight map can manufacture or destroy an edge). It deserves its own branch, its
own test suite (turnover correctness, cost-on-delta, no-lookahead under rebalancing,
zero-turnover-modulator reward), and an operator checkpoint — NOT a silent mid-session
embark. The single→multi-name re-test that surfaced this was a clean, self-contained
eval-honesty improvement; this is the next, larger step and should be greenlit
deliberately.

## Acceptance gate (when built)

1. Portfolio engine produces a non-degenerate absolute Sharpe on a flat buy-and-hold
   benchmark (sanity: SPY buy-and-hold ≈ its real Sharpe, not −11).
2. Turnover + cost charged on rebalance deltas; a zero-turnover modulator (OVERNIGHT_DRIFT)
   shows ≈0 added turnover vs OFF (regression-locks ADR-0089's invariant under sizing).
3. No-lookahead holds under rebalancing (the asof guard already enforced in the engine).
4. `run_flag_ablation` portfolio mode re-judges EVENT_RISK; the verdict is rendered on a
   trustworthy DSR. (Whatever it says — promote or HOLD — it is then a real decision.)
5. Re-judge OVERNIGHT_DRIFT for completeness (expected: still HOLD; the multi-name
   passthrough was already decisively negative).

## Consequences

- **Positive:** every default-OFF flag finally gets a trustworthy promote/HOLD; the
  EVENT_RISK decision (currently stuck) unblocks; the zero-turnover modulator design
  gets a sizing-aware regression lock.
- **Negative / risk:** a portfolio backtest has more degrees of freedom = more ways to
  fool yourself. Mitigated by the benchmark-sanity gate (item 1), turnover/cost tests,
  and keeping the OFF-vs-ON delta as the primary signal even once absolute Sharpe is
  plausible.
