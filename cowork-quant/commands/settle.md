---
description: Settle horizon-expired positions, record fills, update calibration
argument-hint: "[fill: <proposal-id> <price>]"
allowed-tools: ["Read", "Write", "Bash", "AskUserQuestion"]
---

Settlement pass. Read the `quant-core` skill first.

1. If `$ARGUMENTS` contains a fill confirmation (proposal id + price), build
   the Fill JSON (validate: the proposal exists, was approved, price is a
   plausible number near recent market) and record via
   `python -m quantcore.cli fill --state-dir <workspace>/quant-state --fill-json <tmp>`.
   If the CLI refuses (no approval), explain — never force.
2. Fetch current prices for all open positions and record `mark` events.
3. Run `python -m quantcore.cli settle --state-dir <workspace>/quant-state`.
4. Report each settlement: asset, horizon, entry -> exit, realized return,
   whether the committee direction was correct.
5. Show the updated calibration table (from `status`) and call out any
   analyst whose accuracy is drifting from its confidence (ECE > 0.10) —
   that analyst's future confidence should be shrunk toward 0.5.
