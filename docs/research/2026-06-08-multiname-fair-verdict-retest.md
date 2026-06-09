# Multi-name fair-verdict re-test — EVENT_RISK + OVERNIGHT_DRIFT (2026-06-08)

Both perception flags' first verdicts ran on **single-symbol SPY passthrough**, which
each verdict doc flagged as unrepresentative — the documented re-open condition for
BOTH was "multi-name / cohort-weighted universe." The harness
(`run_flag_ablation` → `WalkForwardEngine` → `AdvisorStrategy`) already supports a
multi-name universe via a `(field, symbol)` MultiIndex-column ohlcv frame; the
single-symbol runs were a choice, not a limit. This re-runs both on a representative
cohort via `scripts/quant-multiname-ablation.py` (committed, unit-tested).

Window: 2023-01-01 → 2024-12-31, real yfinance daily bars, full production loadout.

## OVERNIGHT_DRIFT — cohort: QQQ, ARKK, TSLA, NVDA, GME, COIN (high-retail-attention / high-beta — the research-favored cohort)

| metric | OFF | ON | Δ |
|---|---|---|---|
| Sharpe | −14.865 | −16.139 | **−1.273** |
| n_trades | 317 | 417 | **+100** |
| win_rate | 0.759 | 0.543 | −0.216 |
| max drawdown | −0.0617 | −0.0803 | worse |
| DSR | 0.000 | 0.000 | — |

**Verdict: HOLD — and now DECISIVELY so.** This is the important result: on the very
cohort the research says should *favor* the overnight premium, enabling the analyst
made performance **worse on every axis** — lower Sharpe, +100 trades, win-rate
collapsed 76%→54%, deeper drawdown. The single-SPY HOLD (`d_sharpe −0.887`) was NOT a
universe artifact; the fair test is even more damning. The LONG hold-through-close
nudge systematically adds bad conviction + turnover. **OVERNIGHT_DRIFT is conclusively
default-OFF** — the thesis does not survive a fair multi-name test. (To actually
re-open would now require a different signal construction, not just a different
universe — e.g. conditioning the nudge on a regime filter, or gating it to only the
subset of names with a statistically-significant positive trailing spread.)

## EVENT_RISK — cohort: SPY, QQQ, TLT, XLF, IWM (rate-sensitive multi-name)

| metric | OFF | ON | Δ |
|---|---|---|---|
| Sharpe | −11.662 | −11.431 | **+0.231** |
| n_trades | 269 | 264 | −5 (5 FOMC-day opens silenced) |
| sortino | −12.952 | −12.618 | +0.334 |
| max drawdown | −0.0526 | −0.0516 | better |
| DSR | 0.000 | 0.000 | — |

**Verdict: HOLD — but materially STRONGER than the single-SPY run.** The Sharpe delta
is now **+0.231, ABOVE the +0.10 promote bar** (the single-SPY run was +0.075, below
it). The blackout correctly silenced 5 FOMC-day opens, and every metric moved the
right way. It HOLDs *only* on the **DSR sub-gate**: the absolute Sharpes are so
negative (a single-cohort-passthrough harness artifact) that the deflated-Sharpe
cannot certify the ON leg as more-likely-than-not real (`DSR 0.000 ≤ 0.50`).

So the honest read: **the Sharpe-improvement gate now passes on a fair universe; the
DSR gate is the sole remaining blocker, and it is plausibly a harness-artifact of the
passthrough's deeply-negative absolute level rather than a genuine "the edge isn't
real" signal.** This makes EVENT_RISK the **leading promote candidate** — but I am NOT
flipping it on, because the DSR gate is a deliberate skeptic and the absolute-level
artifact means I cannot yet cleanly separate "real edge" from "harness noise." The
correct next step is a harness that produces a non-degenerate absolute Sharpe (a
proper multi-name portfolio backtest with position sizing, not a single-cohort
passthrough) so the DSR gate becomes meaningful — then re-judge.

## Disposition

- **OVERNIGHT_DRIFT**: HOLD, conclusive. Stays default-OFF; re-open now needs signal
  re-design, not a universe change.
- **EVENT_RISK**: HOLD, but PROMOTED to leading candidate — Sharpe gate clears on a
  fair universe; blocked only by the DSR gate, which is suspect under the passthrough
  artifact. Next: a sizing-aware multi-name harness to make DSR meaningful, then
  re-judge. Do NOT enable until DSR is trustworthy.

Both remain measured, not assumed. Zero rubber-stamps. The single→multi-name re-test
changed the picture for BOTH flags — which is exactly why fair evaluation was worth
doing before building anything new.
