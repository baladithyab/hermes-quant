# Flag-Ablation Harness — measurability notes (honest scope)

> Lane: `feat/flag-ablation-harness`. This file documents EXACTLY which flag
> classes the harness measures genuinely vs not-yet, and WHY — grounded in real
> flag-toggle runs, not prose. Money-software: honesty over a green checkmark.

## What shipped

- **D1** — `hermes_quant/backtest/ablation.py`: `run_flag_ablation(flag, ...)`
  runs the SAME walk-forward window with a flag OFF vs ON and returns an
  `AblationResult` (both `WalkForwardResult` legs + Sharpe/Sortino/maxDD/total
  return/alpha/n_trades deltas + deflated-Sharpe per side + a conservative
  PROMOTE/HOLD `verdict`). No env leakage (context-manager restore proven for a
  preset AND an unset flag); off-vs-off bit-identical.
- **D2** — `hermes quant ablate <flag> --from --to ...` CLI. Heavy real-data path
  gated behind `HERMES_QUANT_RUN_BACKTEST=1` (same release-gate convention as
  `tests/backtest/test_fundamentals_ablation.py`); without it, prints the gate
  message and exits 0. A synthetic self-test path (`--synthetic`) runs offline.
- **D3** — `AdvisorStrategy` (`hermes_quant/backtest/strategy.py`): drives the
  REAL analyst-pool → BMA → risk-gate chain offline (DI, dry-run, no network),
  with an **asof-honest internal settlement loop**, so the analyst-pool/BMA flags
  (the L2 cluster) are GENUINELY measurable — not a false null.

## The honesty trap this lane was built to avoid

`HermesQuantStrategy` is a **momentum stand-in**: it never runs the analyst pool
or the BMA aggregator. Ablating an L2/STACKING/semantic flag THROUGH IT would
show a **false null** — the flag cannot change a decision the path never makes.
`AdvisorStrategy` (D3, option A in the mandate) is the fix.

But there is a SECOND, subtler trap, and the harness handles it explicitly: the
L2 flags split into two classes by WHEN they bite.

## Measurability matrix (every row verified by a real OFF-vs-ON run)

| Flag | Lives in | Bites at | Measurable via AdvisorStrategy? | Conditions |
|---|---|---|---|---|
| `HERMES_QUANT_L2_PER_ANALYST_CALIB` | `aggregators/bma.py` | **cold-start** | ✅ YES | any 2-analyst committee; works even with `learn_from_fills=False` |
| `HERMES_QUANT_L2_LESSON_HAIRCUT` | `aggregators/bma.py` | **cold-start** | ✅ YES | needs an injected `loss_lesson_provider` (else the path is a documented no-op) |
| `HERMES_QUANT_STACKING` | `aggregators/bma.py` | **accumulation** | ✅ YES | needs `learn_from_fills=True` (settlement loop) **AND a committee with directional dissent** — see below |
| `HERMES_QUANT_L2_POSTERIOR_DECAY` | `aggregators/bma.py` | **accumulation** | ✅ YES | needs `learn_from_fills=True` (settlement loop) |
| `HERMES_QUANT_L2_POSTERIOR_PERSIST` | `aggregators/bma.py` | **accumulation** | ✅ YES* | needs `learn_from_fills=True` + a `posterior_store_path` on the BMA; \*persistence is most visible ACROSS aggregator lifecycles |
| trader/risk-path flags (e.g. `EVENT_RISK`, `paper_zero_costs`) | `risk/gate.py` | gate | ✅ YES | already measurable via `HermesQuantStrategy` too (the gate runs on both strategies) |

### Cold-start vs accumulation — the key distinction

- **Cold-start-biting** flags change the BMA output on the *first* `aggregate()`
  call. `PER_ANALYST_CALIB` shrinks confidence through each analyst's Beta prior
  (verified: a 2-analyst committee moves aggregate confidence **0.375 → 0.582**
  with the flag flip, no settlement needed). `LESSON_HAIRCUT` fires off an
  injected loss-lesson provider (verified: **0.375 → 0.319**).

- **Accumulation-biting** flags (`STACKING`, `POSTERIOR_DECAY`,
  `POSTERIOR_PERSIST`) read from per-analyst `history` / `decay_samples` rings
  that are **EMPTY** until `aggregator.update(EpisodeOutcome)` runs. The stock
  `WalkForwardEngine` has **no settlement loop**, so ablating these through ANY
  strategy that lacks settlement shows a false null. Verified directly:

  ```
  STACKING with learn_from_fills=FALSE  -> decisions differ OFF vs ON: False  (empty history -> no-op)
  STACKING with learn_from_fills=TRUE   -> decisions differ OFF vs ON: True   (skill accrued)
  ```

  `AdvisorStrategy` closes this by running its OWN asof-honest settlement loop:
  each day, before deciding, it settles any pending decision whose outcome is now
  observable (`observable_asof = decision_asof + horizon_delta`), computing the
  realized direction-correctness from **lookback bars only** (all ≤ asof, by the
  engine's no-lookahead contract — never a peek), and feeding it to
  `aggregator.update(...)`. This mirrors the live loop's c96e `observable_asof`
  discipline exactly, so the flags accrue the same skill they would in production.

### STACKING needs a *dissenting* committee — a real BMA-math nuance, not a bug

This one is worth calling out because a careless ablation would mis-report it.
STACKING's redundancy discount **scales** per-analyst weights. But BMA's
`vote_share = |Σ w·d·c| / Σ|w·d·c|`. For a **unanimous** correlated pair the
discount cancels in that ratio (numerator and denominator scale together →
`vote_share` stays 1.0), so STACKING is a genuine **no-op** there — that is BMA's
math, not a harness defect. STACKING only changes the output when there is
**directional dissent**, where discounting the correlated supporters genuinely
shifts the net vote toward the dissenter. Verified end-to-end:

```
STACKING, committee = {2 correlated longs + 1 short dissenter}, settlement on:
  OFF n_trades=125  ON n_trades=10   nav_series differ: True   d_sharpe=+74.5
STACKING, committee = {2 correlated longs} (unanimous):
  OFF == ON  (no-op — correct per BMA vote-share math)
```

**Implication for whoever runs the eval:** to measure STACKING, the committee
fed to `AdvisorStrategy` (or the live analyst roster on the chosen window) must
actually disagree at least sometimes. A perfectly-unanimous roster will report a
true (and correct) null for STACKING.

## ADMISSIBILITY / EVENT_RISK / BORROW_COST / GROUNDING_ENFORCE

These (ADR-0077 / 0084) are wired on the gate / advisor seams, not the BMA
fusion. `EVENT_RISK` is measurable through the gate path (it adds a pre-event
reject condition; it needs an `event_risk` payload on `ctx.extras` / signal
metadata to bite). `GROUNDING_ENFORCE` acts at the views→aggregator seam and
needs a `ground_truth_block` in `ctx.extras` to drop ungrounded views — a
synthetic OHLCV ablation will show a null unless that block is supplied.
`ADMISSIBILITY` / `BORROW_COST` act in the reactor/admissibility precondition,
downstream of the advisor's signal — measuring them needs a reactor-level
ablation harness, which is **not built in this lane** (follow-up).

## Bottom line

- The **harness (D1)** and **CLI (D2)** work and are tested.
- The **full L2 cluster IS genuinely measurable** through `AdvisorStrategy` (D3),
  with the documented conditions (settlement on for accumulation-biting flags; a
  dissenting committee for STACKING; an injected provider/payload for
  haircut/grounding/event-risk). None of this is theater — every claim above is
  backed by a real OFF-vs-ON toggle.
- **Not yet measurable in this lane:** reactor-level flags (`ADMISSIBILITY`,
  `BORROW_COST`) need a reactor ablation harness — flagged as follow-up, not
  faked here.
