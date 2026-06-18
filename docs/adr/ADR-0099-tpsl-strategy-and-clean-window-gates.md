---
status: accepted (2026-06-18, eval-gate-pending) — TP/SL + clean-window gates shipped DEFAULT-OFF; GATE-0..3 thresholds confirm across >=2 clean windows before arming
date: 2026-06-17
deciders: [codeseys]
consulted: [deep-work-loop session 2026-06-17 (AEGIS strategy research wf_0f064078, academic-first)]
amends: null
supersedes: null
---

# ADR-0099: TP/SL strategy (tranche/trailing/greeks), stock-options parity, and the clean-window gates

> Operator asks resolved here: (1) the risk system properly sets TP and SL; (2) TP/SL may
> be all-at-once OR tranched OR trailing OR greeks-based — "leave room for trailing so we
> don't get liquidated early"; (3) stock AND options plays are EQUALLY considered in Decide
> but properly differentiated by the risk/deliberation agents; (4) a proper clean-window
> equity-edge heuristic. Every rule default-OFF + eval-gated; numbers are starting points,
> flagged eval-gate-pending until confirmed on AEGIS's own data.

**Cites:** [ADR-0098](ADR-0098-options-strategy-taxonomy-and-two-level-multileg.md) (the
structures + 2-level model these exits operate on), [ADR-0097](ADR-0097-paper-vs-live-slippage-haircut.md)
(live-realistic cost the edge metric nets out), [ADR-0029](ADR-0029-multi-leg-paper-reactor.md)
(the N>=100 options evidence gate), [ADR-0031](ADR-0031-governance-plane-consolidation.md) (the sharpe_95ci
floor these gates feed), [ADR-0096](ADR-0096-pre-autonomous-decision-quality-gates.md)
(the decision-quality gates this composes with), [ADR-0034](ADR-0034-run-cards.md) (the waiver record).

---

## Decision Outcome — Part A: TP/SL operates at the right LEVEL

**Composite-level exits (options multi-leg structures)** act on net composite MtM P&L and
net composite greeks (refreshed each tick from live leg prices via `aggregate_net_greeks`,
never the stale parent snapshot), firing a single atomic buy/sell-to-close MLEG order:
1. **50%-of-credit TP** — close when net MtM P&L ≥ 50% of initial credit (Huang et al. 2025
   optimal-τ + tastylive practitioner; EVAL-GATE-PENDING).
2. **2×-credit loss cap** — HARD fail-CLOSED rule: close when net loss ≥ 2× initial credit,
   regardless of DTE/delta, BEFORE the structural max-loss wings; wired autonomous.
3. **21-DTE time close** — force-close at 21 DTE for 40–45 DTE entries (Huang [50–75%] of
   duration); tighten the short-delta-breach threshold from 0.40 → 0.30 inside 21 DTE.
4. **Delta-breach close** — close when any short delta magnitude > 0.40 (pre-21-DTE) / 0.30
   (inside); EVAL-GATE-PENDING.
5. **Extrinsic-value floor (assignment prevention)** — close any short leg whose extrinsic
   value ≤ $0.10 or 5% of original credit; wired autonomous.

A defined-risk structure's max-loss wing IS its structural stop; the 2×-credit cap fires
earlier as the active stop.

**Leg-level exits (equity tranching on the NAV-fraction ladder):** tranche steps reduce the
position by exactly ONE 0.05 ladder rung (never fractional). For a 0.10-NAV position:
Tranche-1 exits 0.05 at +1R and moves the hard stop to BREAKEVEN on the residual; Tranche-2
exits the residual 0.05 at +2R OR when the chandelier ATR trailing stop fires (whichever
first). The trailing stop **activates only after +3% unrealized gain** (so it does not
liquidate early — the operator's explicit ask), trailing distance 5–7% full / 3–5% residual,
chandelier N≈2.5×ATR(14, 30-min). All these numbers are EVAL-GATE-PENDING (Li et al. 2026
gave crypto-derived values that are too tight for equities; the ranges here are conservative
upward adjustments).

> Build note: today's `HERMES_QUANT_TAKE_PROFIT_SWEEP` (AG-EQ-1, shipped) is the all-at-once
> base. Tranche + trailing are increments ON it (`HERMES_QUANT_TP_TRANCHE`,
> `HERMES_QUANT_TRAILING_STOP`), each default-OFF + eval-gated; greeks-based exits are the
> options-monitor increment.

## Decision Outcome — Part B: stock-options parity in Decide (the common edge metric)

**PTRAR (per-trip risk-adjusted return)** = `realized_pnl / committed_capital_at_risk`,
where capital-at-risk = `entry_price × abs_position_usd` for equity and `max_loss` (finite,
per ADR-0029 D5) for defined-risk options. An equity trade returning +2% of position and an
options trade returning +2% of max-loss have IDENTICAL PTRAR and identical committee weight.
`PTRAR_sharpe = mean(PTRAR)/std(PTRAR) × sqrt(annual_freq)` is the COMMON Sharpe across both.

Differentiation happens AFTER PTRAR, as DOWNSTREAM gates, not as committee preference:
- the **risk agent** applies options-specific confidence penalties (expiry/gamma risk, BPR
  uncertainty) before the vote;
- the **deliberation agent** enforces BLOCKING gates (finite-max-loss per ADR-0027, net-delta
  cap, BPR budget).
The silence asymmetry holds: the LLM may silence either class but NEVER picks between a stock
play and an equivalent options play — that selection is the deterministic `structure_select`
table's. Options proposals stay in a dry-run queue (not surfaced to the committee) until
GATE-2 clears; after that they enter the SAME queue with no a-priori order/weight advantage.

## Decision Outcome — Part C: the clean-window gate hierarchy (operator decision #1)

The prior 0/11 (−4.64%) record is **pre-GATE-0 and carries zero statistical weight.**

- **GATE-0 (operator, before the clock starts):** `quant-reset-paper-book --apply` to flat
  $100k; arm the four protective flags (DURABLE_DRAWDOWN_BASELINE, PER_POSITION_STOP,
  DELTA_NORMALIZER, ACCOUNT_LOCK); verify slippage model v0.2 + the ADR-0097 haircut on;
  one clean tick completes. `clean_window_start.json` is the t0 anchor; any metric before
  GATE-0 is poisoned and discarded.
- **GATE-1 (survival, N≥20 round-trips):** win_rate ≥ 0.40 (Wilson CI lower bound clears 0),
  zero kill-switch fires, rolling-30d max drawdown ≤ 8% NAV. Failure → strategy review, NOT
  options unlock.
- **GATE-2 (unlocks options HITL-paper origination, N≥50 AND ≥60 calendar days):
  profit_factor ≥ 1.3, win_rate ≥ 0.50, rolling-90d Sharpe ≥ 0.8, max consecutive losses ≤ 8,
  drawdown ≤ 3%. Point estimates only at N=50 (CI too wide) → labeled provisional in retro.
- **GATE-3 / options-live (N_options≥100 settled multi-leg outcomes, bootstrap
  sharpe_95ci_lower ≥ 1.0, drawdown ≤ 1%, no kill-switch in 14d):** the existing
  `LiveTradingApproval` Pydantic model + ADR-0029 D7 thresholds are FINAL — do not weaken.

All thresholds are EVAL-GATE-PENDING (calibrated from Lo 2002 asymptotic theory + Bailey/
López de Prado DSR/PBO on a thin forward-only sample) and must be confirmed across ≥2 clean
windows before being treated as fixed policy; an operator waiver cites the run-card (ADR-0034).

### Consequences

- **Positive:** TP/SL fires at the correct level (composite for options, leg for equity
  tranches); trailing activates only after a profit cushion so it does not liquidate early.
- **Positive:** stock and options compete on ONE risk-normalized metric (PTRAR) — neither
  privileged — while the risk/deliberation agents still differentiate via downstream gates.
- **Positive:** the clean-window gates give an objective, statistically-honest unlock path
  from "reset book" → equity edge → options paper → options live, each gated on evidence.
- **Negative / accepted:** many numbers are starting points; the design's value is the
  STRUCTURE (which rule, at which level, gated by which metric), not the specific constants —
  those are explicitly eval-gate-pending and listed for calibration.

### Confirmation

Satisfied by: (1) tests that the composite TP/2×-loss-cap/delta/extrinsic rules fire on net
composite state (not stale snapshot); (2) a tranche test that exits one 0.05 rung and moves
the residual stop to breakeven; (3) a trailing test that the stop does NOT activate before
+3% gain; (4) a PTRAR test that an equity and an options trade with equal risk-normalized
return get equal committee weight; (5) the GATE-0..3 metric computations + a test that pre-GATE-0
data is excluded. Each is eval-gated; constants are tracked as eval-gate-pending.

## More Information

- The eval-gate-pending calibration list (every constant + its evidence basis) is in the raw
  research `docs/research/2026-06-17-aegis-strategy-research-raw.json` (synthesis.open_calibrations).
- Build order (GATE-0 → equity foundation → bull-put-spread → … → iron condor → options-live)
  is filed as the `aegis-ao*`/`aegis-tp*` seeds.
