# Hermes-Quant Deep Review — Synthesis (2026-06-07)

Reviewer: Codex (gpt-5.5, ChatGPT auth), 4 parallel read-only facets.
Trigger: post-restart validation + $4k ASTS paper loss post-mortem.
Branch: `fix/nan-fail-open-and-signal-guards`

## Convergent P0 — NaN-fail-open defect class (3 of 4 facets independently)

The codebase's #1 known defect class, found in 4+ distinct files. Pattern:
a guard written `if x < threshold: reject` silently PASSES when `x` is NaN
because `NaN < threshold` is False. Worse, some NaN values get laundered into
finite `0.0` UPSTREAM (protocol.py `max(0.0, NaN)`) so the gate's own finite
checks never fire.

| # | File:line | Finding | Sev |
|---|---|---|---|
| 1 | `protocol.py:284,290` | `max(0.0, NaN)` masks NaN equity → finite 0.0 drawdown → gate EMITS +0.20 instead of flatten (confirmed by execution) | HIGH |
| 2 | `admissibility/oracle.py:202,227` | `None` checks but no `math.isfinite`; NaN equity/ask/limit → ACCEPTED | HIGH |
| 3 | `gates/silence_bias.py:190,211,237,278` | confidence/magnitude/vol/threshold not finite-checked → NaN can reach FIRE | HIGH |
| 4 | `react/slippage_model.py:178,223` | rejects `price<=0` but not non-finite → `fill_price=NaN` corrupts P&L ledger | HIGH |
| 5 | `react/paper.py:447` | NaN existing exposure fails-open portfolio-cap headroom (flag-gated) | MED |
| 6 | `risk/gate.py:612` | `pd.NaT` → NaN cooldown elapsed → cooldown skipped | MED |
| 7 | `admissibility/order_state.py:75` | non-amplification assertion finite-input-only | LOW |

**Fix strategy:** a single shared `_is_finite_number()` helper already exists in
`risk/gate.py`. Promote/duplicate it to each guard site and fail CLOSED
(reject/silence/flatten) on non-finite. The protocol.py `max(0.0, NaN)` is the
keystone — fix that and several downstream fail-opens become unreachable.

## Other HIGH findings (single-facet, real)

- **`react/_alpaca_exec.py:184` (broker):** timeout-cancel is best-effort; if cancel
  fails, a 0-fill `unfilled_timeout` is written while a real Alpaca paper order may
  still be working → later fills invisible locally → phantom position. *(v0.2 live
  path; paper-default unaffected, but this is the live-broker safety seam.)*
- **`options/recipes.py:147` + `multileg.py` (broker):** option producer only emits
  `sell_to_open` legs for CC/CSP; no `buy_to_close`/`sell_to_close` producer →
  option opens are locally uncloseable except manually.
- **`aggregators/bma.py:1049` (calibration):** BMA can emit `confidence_raw=1.0` from
  ≥2 same-direction *correlated* voices; single-source guard exists (`:1016`) but
  multi-source unanimity uses `vote_share + agreement_bonus` clipped to 1.0.
  **This is the mechanism behind the ASTS loss** (see post-mortem).

## Control-plane gaps (autonomous facet)

- **`max_concurrent_positions` is NOT enforced** — read into rails (`autonomous.py:94`)
  but never checked before `_react()`. Only `max_per_tick_opens` is. The status
  display SHOWS a cap that does nothing.
- **`kill_switch_pct` is effectively dead code** — tick only honors an already-tripped
  file (`autonomous.py:355`); no live cumulative-PnL computation compares to the
  threshold. `_read_kill_switch()` fail-opens (`tripped=False`) on parse error.
- **`quant_reject` reports `calibrator_will_learn=true` but only appends a journal
  override** (`tools.py:930`) — misleading.

## $4k ASTS loss post-mortem (proposal prop_20260603T193506_ASTS_371e27)

NOT a bug — a known-weak signal fired anyway. Mechanism:
- Panel was contradictory: classical-ta +1 (weak 0.47), microstructure +1 (0.33,
  1 sub-signal), **kronos −1 SHORT at 0.85 conviction (25/30 paths)**, semantic +1
  (0.895) on a **Blue Origin/New Glenn headline propagated to ASTS via sector_member**.
- BMA outvoted the highest-conviction analyst 3:1 with equal 0.5 weights →
  emitted conf 0.688 long. (Cousin of bma.py:1049 over-confidence.)
- Risk committee conservative persona voted **SILENCE**: "no stop_loss … unbounded
  losses." Overruled to a 0.5× size cut. Fired at 20% NAV with **stop_loss: None**.
- edge=0.0114 (1.1%) → Kelly 0.20. ASTS $118→$93. Realized −$4,186 (paper).

**Root causes:** (a) BMA confidence overstates a contradictory panel;
(b) `stop_loss: None` is allowed to fire at full size; (c) sector-contagion semantic
signal treated as primary ASTS evidence.
