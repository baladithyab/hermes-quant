# PMCC shadow structure tracker (interim, against the ADR-0029 gap)

**Status:** Implemented (interim scaffold)
**Date:** 2026-05-30
**Module:** `hermes_quant/shadow/pmcc.py` · **Tests:** `tests/unit/test_shadow_pmcc.py` (8)
**Store:** `~/.hermes/quant/shadow/pmcc-positions.jsonl`
**Relates to:** [ADR-0029](../adr/ADR-0029-multi-leg-paper-reactor.md) (multi-leg paper reactor — the gap this works around), [ADR-0049](../adr/ADR-0049-shadow-account-counterfactual.md) (ShadowAccount — explicitly NOT reused, see below)

## Why this exists

The Poor-Man's Covered Call (deep-ITM LEAPS long + rolling short call) is the
capital-efficient, bounded-theta way to express a long single-name thesis (analyzed
for AMZN: 32% of the shares' capital, net-positive theta, best expected value of
shares / LEAPS-only / PMCC). But **multi-leg options cannot be executed** — the
multi-leg paper reactor is the ADR-0029 gap; `PaperReactor` is equity-only.

Rather than wait for ADR-0029 to track the idea, the PMCC is recorded as a
**marked-to-model shadow structure**: both legs are stored, and the spread is priced
on demand with Black-Scholes (net value / net delta / net theta). When the multi-leg
reactor lands, we have a documented, daily-markable counterfactual to validate the
first live fills against.

## Why a dedicated tracker, not `ShadowAccount` (ADR-0049)

`ShadowAccount`'s schema is `(ticker, quantity, avg_entry_price)` — single-leg equity.
A PMCC's economics live in the **relationship between two option legs** (net theta,
net delta, upside cap, breakeven). A single-ticker row cannot represent that without
misrepresenting the greeks the structure exists to manage. So this is a purpose-built
module, not a forced fit.

## What it is / isn't

- **Is:** a model. Records `PMCCPosition` (symbol, both `OptionLeg`s, spot-at-open),
  marks it via `mark_pmcc(pos, spot=..., asof=...)` → `PMCCMark` (long/short/net value,
  unrealized P&L vs net debit, net delta, net theta/day, DTEs). Append-only JSONL store.
- **Is NOT:** a reactor. Writes nothing to `executions.jsonl` / `state.db`. Pure shadow.

## Sign / fidelity notes

- **Net theta convention:** long-leg theta is negative (we pay); short-leg theta is
  negative for the option but we are *short* it (we collect), so
  `net_theta_day = long_theta − short_theta`. A deep-ITM-LEAPS-long + near-ATM-short
  PMCC is typically **net-positive theta** — the asserted invariant in the tests.
- **Model-vs-quote gap:** the BS mark at entry IV can differ from the quoted premium
  (American premium + IV smile). Seeded AMZN position: quoted LEAPS $86.90, BS-at-48%-IV
  marks ~$102 → a small positive "unrealized" at open. Expected; within test slack.

## Seeded position (2026-05-30)

AMZN PMCC: long Dec-2027 $205 call ($86.90, 48% IV) + short Jul-2026 $285 call ($4.88,
32% IV). Net debit **$8,202** (vs $27,064 for 100 shares); net theta **+$9.95/day**
collected; net delta 48.

## When ADR-0029 lands

Validate the first multi-leg paper fills against this shadow's daily marks (the same
fidelity discipline ADR-0070 applies to equity fills). Until then, an EOD cron marking
the shadow accrues a counterfactual track record at near-zero cost.
