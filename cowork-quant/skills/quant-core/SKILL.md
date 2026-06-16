---
name: quant-core
description: >
  Core methodology for cowork-quant: the PDR (Perception-Decision-Reaction)
  multi-analyst trading committee, the deterministic risk gate, the discrete
  sizing ladder, the paper ledger, and the quant-state directory layout.
  Use whenever the user asks about trading analysis, stock/crypto proposals,
  portfolio status, the risk gate, position sizing, paper trading, or runs
  /brief /scan /propose /settle /status /doctor. ALWAYS consult before any
  trading-related workflow in this plugin.
---

# quant-core — methodology and rails

cowork-quant is an ADVISOR. Claude perceives and reasons; deterministic Python
decides sizing and admissibility; the HUMAN executes in their own broker.
Never place, simulate placing, or offer to place an order anywhere.

## The PDR loop

1. **Perceive** — gather data with asof-honesty: record `asof_decision` (now,
   UTC) and use only CLOSED bars (`bar_ts` <= last completed bar). Data
   sources: yahoo-finance / coingecko / sec-edgar MCP tools, or Python
   (yfinance) in the sandbox. Never use a still-forming bar.
2. **Decide** — run >= 2 distinct analyst rubrics (see the `analysts` skill),
   each emitting an `AnalystView` JSON. Aggregate into a `CommitteeSignal`:
   confidence-weighted agreement; on real disagreement REDUCE confidence and
   record the dissent verbatim in `signal.dissent`. Run the bear-analyst or
   risk-skeptic subagent against any high-conviction long (and bull-analyst
   against high-conviction shorts) — dissent is load-bearing.
3. **React** — pass the signal through the gate via the quantcore CLI. The
   gate's verdict is FINAL. Silence is a correct, reportable outcome — say
   "the gate held cash because <reason>", never re-prompt around it.

## Driving quantcore

State lives in `<workspace>/quant-state/` (created on first use). All CLI
calls emit JSON on stdout; non-zero exit = abstain and report, never repair.

```bash
cd <plugin>/scripts/quantcore && pip install -e . --quiet   # once per session
python -m quantcore.cli propose --state-dir <workspace>/quant-state \
    --signal-json /tmp/signal.json
```

`signal.json` shape:

```json
{
  "signal": {
    "asset": "AAPL", "asset_class": "equity", "direction": 1,
    "magnitude": 0.03, "confidence": 0.65, "horizon": "5d",
    "asof_decision": "2026-06-09T14:00:00+00:00",
    "views": [ <AnalystView>, ... ],
    "dissent": "bear: valuation stretched vs 5y median",
    "event_risk": [{"kind": "fomc", "impact": "high", "scheduled_for": "..."}]
  },
  "costs": {
    "commission": 0.0, "spread": 0.0005, "slippage_estimate": 0.0005,
    "volatility": 0.018
  }
}
```

`costs.volatility` = stdev of per-period log returns over ~20 periods at the
signal's horizon timeframe — compute it from fetched bars, never guess.

Subcommands: `gate` (dry-run decision), `propose` (gate + ledger proposal),
`decide --decision approval|rejection`, `fill`, `mark`, `settle`, `status`,
`verify`.

## The approval flow (HITL — never skip)

1. `propose` returns a proposal (or a silence decision — report it and stop).
2. Present the proposal with AskUserQuestion: Approve / Reject / Modify down
   (modify = reject + new smaller manual target, still on the ladder).
3. Record the outcome with `decide`. On approval, tell the user to execute in
   their broker, then confirm the fill price back; record it with `fill`.
4. NEVER record a fill without a recorded approval (the CLI enforces this).

## Rails (verbatim, non-negotiable)

- Sizing ladder {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV; the gate snaps to it.
- Confidence is calibrated P(direction correct): 0.55 = weak lean, 0.65 =
  solid committee agreement, >0.75 = exceptional (rare; expect the
  risk-skeptic to attack it). Check `status` calibration output — if an
  analyst's ECE is drifting, shrink its confidence toward 0.5.
- Silence by default: ambiguous data, stale data, single-analyst committees,
  and negative-edge signals all hold cash. The gate enforces this; your job
  is to not fight it.
- Every number shown to the user must come from fetched data or quantcore
  output — never invent prices, returns, or P&L.
