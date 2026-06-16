---
description: Run the analyst committee on a ticker (or the watchlist) — views only, no proposal
argument-hint: <ticker | "watchlist">
allowed-tools: ["Read", "Write", "Bash", "Glob", "ToolSearch"]
---

Run a committee scan. Read the `quant-core` and `analysts` skills first.

1. Resolve targets: `$ARGUMENTS` is a ticker, or "watchlist" -> read
   `<workspace>/quant-state/config.json` watchlist (empty -> ask the user).
2. Perceive (asof-honest): fetch >= 60 closed bars per target via the
   yahoo-finance/coingecko MCP tools or sandbox Python (yfinance). Record
   `asof_decision` = now UTC and `bar_ts` = last CLOSED bar. If data is
   stale (> 2 bar-periods old) or short, SKIP the target and say why.
3. Decide: run classical-ta + (equities: fundamentals; crypto: skip) +
   catalyst rubrics. Emit each AnalystView as JSON per the analysts skill.
4. Aggregate per the committee rules, including dissent.
5. Report per target: a compact table (direction, magnitude, confidence,
   horizon) + 2-3 sentences of committee reasoning + any event_risk found.
   State clearly this is a SCAN — no sizing, no proposal, nothing persisted.
6. If the user wants to act on a scan, point them to /propose <ticker>.

Never invent prices. Never proceed with a single analyst view.
