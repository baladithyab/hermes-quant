---
description: Full PDR turn — committee scan, deterministic gate, sized proposal, human approval
argument-hint: <ticker>
allowed-tools: ["Read", "Write", "Bash", "Glob", "AskUserQuestion", "Agent", "ToolSearch"]
---

Run a full propose flow for `$ARGUMENTS`. Read the `quant-core` and
`analysts` skills first. Ensure quantcore is installed (see skill).

1. Run the same perceive + decide steps as /scan for the single ticker.
2. Adversarial pass: if committee confidence >= 0.65, spawn the opposing
   agent (`bear-analyst` for longs, `bull-analyst` for shorts) with the
   committee's rationale and data summary. Incorporate its strongest point
   into `dissent`; if it lands a material blow, reduce confidence before
   gating (be honest, not stubborn).
3. Compute `costs`: volatility = stdev of log returns (20 periods at the
   horizon timeframe) from the fetched bars; spread/slippage estimates from
   the quote if available, else 0.0005/0.0005; commission 0. Populate
   `signal.event_risk` from `python -m quantcore.cli events --window-days 7`
   plus any earnings dates found for the ticker (kind "earnings", impact
   "high", verified date only).
4. Write the signal JSON to a temp file and run:
   `python -m quantcore.cli propose --state-dir <workspace>/quant-state --signal-json <tmp>`
5. If the verdict is silence or flatten_halt: report the gate's rule and
   reason plainly ("gate held cash: <reason>"). STOP — do not retry with
   tweaked numbers; that is the system working.
6. If a proposal is returned: present it (asset, direction, target % NAV,
   delta, edge, gate reason, dissent) and use AskUserQuestion:
   "Approve" / "Reject" / "Approve at one rung smaller".
   - Approve -> `decide --decision approval`
   - Reject -> `decide --decision rejection --note <their reason>`
   - Smaller rung -> record approval with note "human sized down to X"; the
     fill is then recorded at X. The CLI enforces this deterministically:
     fills must be on the 0.05 ladder, in the target's direction, and never
     larger in magnitude than the approved target.
7. On approval: remind the user to execute in their own broker, then bring
   back the fill price. When they do (now or any later session), validate it
   and record via `fill`. Never record a fill the user didn't confirm.

You never execute trades. The gate's sizing is a ceiling — humans may size
down, never up.
