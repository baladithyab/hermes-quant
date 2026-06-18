---
name: analysts
description: >
  Analyst rubrics for the cowork-quant committee: classical technical analysis,
  fundamentals, and catalyst/event analysis, each emitting a uniform
  AnalystView JSON. Use when running /scan or /propose, when the user asks for
  a stock or crypto analysis, or when building a CommitteeSignal. Always run
  at least two distinct rubrics — single-voice committees are gated to silence.
---

# Analyst rubrics

Every rubric ends in the SAME schema (the committee is analyst-agnostic):

```json
{
  "analyst": "classical-ta",
  "asset": "AAPL", "asset_class": "equity",
  "direction": 1, "magnitude": 0.03, "confidence": 0.62,
  "horizon": "5d",
  "asof_decision": "<now, UTC ISO>",
  "bar_ts": "<last CLOSED bar, UTC ISO>",
  "rationale": "<=2 sentences, cite the numbers used",
  "evidence_ids": ["yf:AAPL:1d:2026-06-08"]
}
```

`direction` ∈ {-1, 0, 1}. `magnitude` = expected |move| over the horizon as a
fraction (NOT annualized). `confidence` = calibrated P(direction correct) —
0.5 means "no view"; emit direction 0 instead of confidence < 0.55.
Emitting 0 (flat) is a first-class, often correct, output.

## classical-ta (port of hermes-quant ClassicalTAAnalyst)

Compute from >= 60 closed bars at the decision timeframe:
- Trend: 20/50 SMA relation + price location; ADX-style strength read.
- Momentum: RSI(14) — overbought >70 / oversold <30 only counts AGAINST the
  trend signal, not as a standalone reversal call.
- Volatility context: Bollinger(20,2) position; squeeze = lower conviction.
- Volume confirmation: rising volume on trend moves raises confidence ~0.05.

Scoring: all three of trend/momentum/volume aligned -> confidence 0.60-0.68;
two aligned -> 0.55-0.60; mixed -> direction 0. magnitude = ATR(14)-projected
move over the horizon / price, capped at 0.10.

## fundamentals (equities/ETFs only; port of FundamentalsAnalyst intent)

From yahoo-finance MCP (financials, estimates) and/or sec-edgar (latest 10-Q/K):
- Valuation vs own 5y history (P/E, EV/EBITDA percentile).
- Earnings trajectory: revision direction, last-quarter surprise.
- Balance-sheet red flags: leverage spike, FCF negative turn.

This is a SLOW signal: horizon >= 20d, magnitude <= 0.08, confidence <= 0.65.
Crypto: skip this rubric (emit nothing, not a guess).

## catalyst (port of catalyst-sense, lean)

- Scheduled: earnings date proximity, FOMC/CPI/NFP within horizon — these go
  in `signal.event_risk` (kind/impact/scheduled_for), impact "high" for
  FOMC/CPI/NFP/earnings. The gate's blackout rule consumes them.
- Unscheduled: 8-K filings (sec-edgar), major news. A fresh catalyst can
  justify direction but caps confidence at 0.60 unless price has already
  confirmed (gap + hold).
- Form-4 insider clusters (sec-edgar): >= 3 distinct insider buys in 30d is a
  weak long modifier (+0.03 confidence to an existing long view, never a
  standalone view).

## Committee aggregation (deterministic — never in-prompt arithmetic)

Write the views to a temp JSON `{"views": [...]}` and run:

```bash
python -m quantcore.cli aggregate --state-dir <workspace>/quant-state \
    --views-json <tmp>
```

It applies calibration-based ECE shrinkage per analyst, Beta-binomial
accuracy weights (cold-start analysts fall back to an unweighted committee),
the >= 0.10 margin rule (below margin -> direction 0), the 0.03 unanimity
bonus capped at 0.75, and captures losing-side rationales as `dissent`.
Copy its output fields verbatim into the CommitteeSignal — never recompute
or "adjust" them in prose. Then ALWAYS attach every AnalystView to
`signal.views` — the gate counts distinct analysts and the settlement loop
scores each one's calibration.
