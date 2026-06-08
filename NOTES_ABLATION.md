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
STACKING, committee = {2 correlated longs + 1 short dissenter}, settlement on,
  hermetic IdentityCalibrator (clean-machine simulation):
  OFF n_trades=24  ON n_trades=0   decisions differ: True
STACKING, committee = {2 correlated longs} (unanimous):
  OFF == ON  (no-op — correct per BMA vote-share math)
```

**Implication for whoever runs the eval:** to measure STACKING, the committee
fed to `AdvisorStrategy` (or the live analyst roster on the chosen window) must
actually disagree at least sometimes. A perfectly-unanimous roster will report a
true (and correct) null for STACKING.

## Hermetic by default — the calibrator-dependence trap (and how it's closed)

There is a THIRD false-null trap, subtler than the strategy one, that an
adversarial review surfaced and the harness now closes:

A stock `BMAAggregator()` loads the host's private
`~/.hermes/quant/calibrators/isotonic.pkl` if it exists. So an ablation built on
a default aggregator would produce numbers that depend on whatever fitted
calibrator the author's machine happens to have — NON-reproducible across
machines. Worse: on a CLEAN machine (CI, a fresh deploy) there is no pickle, the
cold-start fallback caps confidence at **0.375**, the deterministic risk gate
silences **every** signal, and so **zero trades fire on both legs** → a FALSE
NULL for every accumulation-biting flag. The trap just relocates from the
strategy to the calibrator dependency.

`AdvisorStrategy` closes this by building its default aggregator **hermetic**:

- **Pinned `IdentityCalibrator`** (deterministic passthrough) instead of the
  on-disk pickle, so the eval measures the FLAG's effect, not the host's
  calibrator, and confident signals can actually fire. Inject your own
  `calibrator=` (or a fully-built `aggregator=`) to evaluate against a specific
  calibrator — e.g. an operator running the release-gated real-data path who
  wants production-calibrator behavior.
- **Sandboxed posterior store** (a per-instance temp file) so ablating
  `HERMES_QUANT_L2_POSTERIOR_PERSIST` can NEVER write the production
  `~/.hermes/quant/l2_learning_posteriors/` — the "read-only" eval stays
  read-only.

**Re-verified on a simulated clean machine** (default-calibrator path pointed at
a nonexistent file, dissenting committee, settlement on):

```
HERMES_QUANT_STACKING            off_trades=24 on_trades=0  decisions differ: True
HERMES_QUANT_L2_POSTERIOR_DECAY  off_trades=24 on_trades=31 decisions differ: True
HERMES_QUANT_L2_PER_ANALYST_CALIB off_trades=24 on_trades=6 decisions differ: True
HERMES_QUANT_L2_POSTERIOR_PERSIST: production store dir NOT created, no files written
```

These hold WITHOUT any private calibrator pickle — the measurability is a
property of the harness, not the host. (Regression-guarded by
`tests/backtest/test_advisor_strategy.py::test_accumulation_l2_flags_measurable_on_clean_machine`
and `::test_posterior_persist_ablation_never_writes_real_store`.)

> **Note on absolute metric magnitudes.** Because the eval pins a passthrough
> calibrator (not a fitted one), the absolute Sharpe/return numbers are NOT a
> forecast of live performance — they are a controlled A/B where the only moving
> part is the flag. The harness reports the OFF-vs-ON *delta* and a conservative
> verdict; treat the magnitudes as relative evidence, not a P&L claim. To
> measure a flag against the production calibrator, inject it explicitly.

## ADMISSIBILITY / EVENT_RISK / BORROW_COST / GROUNDING_ENFORCE

These (ADR-0077 / 0084) are wired on the gate / advisor seams, not the BMA
fusion.

**`EVENT_RISK` is now GENUINELY MEASURABLE (C2a, 2026-06-08).** It adds a
pre-event reject condition that reads its carrier from `signal.metadata['event_risk']`
(`risk/gate.py` ~line 598). The plain `AdvisorStrategy` never populates that
carrier, so it used to be REFUSED (`NOT_MEASURABLE`) to avoid a false null.
`hermes_quant/backtest/event_risk_ablation.py` closes the gap:
`EventRiskAblationStrategy` (an `AdvisorStrategy` subclass) stamps an
asof-honest synthetic macro calendar (`synthetic_macro_calendar`, FOMC/CPI/NFP,
reusing the production `CalendarEvent` dataclass so `announced_at <= scheduled_for`
is enforced by construction) into `signal.metadata['event_risk']` before the
gate, filtered to `announced_at <= asof` (defense-in-depth no-lookahead). The
guard then bites on blackout days (verified: ON suppresses ≥1 fresh open vs OFF,
`d_n_trades < 0`). `cli/ablate.py` routes `HERMES_QUANT_EVENT_RISK` to this
strategy automatically — no operator action needed. Tests:
`tests/backtest/test_event_risk_ablation.py` (12 tests: calendar asof-honesty,
carrier filter, gate stamping, blackout-bites, env no-leakage, CLI-not-refused).

`GROUNDING_ENFORCE` acts at the views→aggregator seam and
needs a `ground_truth_block` in `ctx.extras` to drop ungrounded views — a
synthetic OHLCV ablation will show a null unless that block is supplied (still
REFUSED; same carrier-injection pattern as EVENT_RISK could close it — follow-up).
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
