---
description: Show the paper book — positions, NAV, pending proposals, halts, calibration
allowed-tools: ["Read", "Bash", "AskUserQuestion"]
---

Run `python -m quantcore.cli status --state-dir <workspace>/quant-state`
(install quantcore first if needed; see the `quant-core` skill) and present:

- Paper NAV, drawdown from peak, today's P&L (day_start vs now).
- Open positions: asset, signed % NAV, entry price, age.
- Pending proposals awaiting approval/rejection (offer to resolve each).
- Halt state — if halted, say WHY. Resuming requires the user to explicitly
  ask AND confirm via AskUserQuestion; only then run
  `python -m quantcore.cli resume --state-dir <quant-state> --note "<their reason>"`.
  Never resume in the same breath as reporting the halt.
- Ledger integrity result. If the hash chain fails, warn prominently and
  recommend investigating quant-state/ledger.jsonl before trusting any number.
- Calibration summary per analyst (n, ECE) when data exists.

Read-only apart from a human-confirmed resume: this command records no
marks, settles, or decisions.
