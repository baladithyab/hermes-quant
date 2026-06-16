---
description: Daily PDR market brief — regime, watchlist deltas, open book, event proximity
allowed-tools: ["Read", "Write", "Bash", "Glob", "ToolSearch"]
---

Produce the daily brief. Read the `quant-core` skill first.

1. Open book: `python -m quantcore.cli status --state-dir <workspace>/quant-state`.
   Surface positions, paper NAV, pending proposals, halt state, and any
   ledger-integrity failure (integrity failure = lead the brief with it).
2. Regime read (deterministic): fetch >= 300 daily SPY closes (or BTC for
   crypto-weighted watchlists), write them to a temp JSON and run
   `python -m quantcore.cli regime --closes-json <tmp>` — report its label
   and evidence verbatim. "unknown" is a valid, reportable regime.
3. Morning universe scan (hermes-quant ADR-0075/watchlist-evolution port):
   pull the candidate pool from the user's broker watchlists (read-only MCP)
   plus the current config watchlist, fetch 45d daily bars, and run
   `quantcore.universe.scan_universe` + `journal_scan` (price band, 30d avg
   dollar-volume floor, ranked, capped at 8 equities + 2 crypto). If the
   admitted set differs from config.json's watchlist, PROPOSE the change to
   the user (show adds/drops with reasons) — config.json is only edited on
   their confirmation; in unattended turns, report the diff, never apply it.
4. Watchlist deltas: for each watchlist ticker, last close vs prior, %move,
   and whether yesterday's committee view (if any is in the ledger) was
   directionally right.
5. Event proximity: run `python -m quantcore.cli events --window-days 3`
   (ships the verified 2026 FOMC/CPI/NFP seed; report any freshness warning —
   it means the seed needs its annual refresh). Add earnings dates for
   watchlist names within 7 days from the data tools. Flag anything inside
   the gate's blackout window, and pass the events into any same-day
   /propose signal's event_risk.
6. Mark NAV: if positions exist, fetch current prices and record
   `mark` events so drawdown/daily-loss breakers see fresh NAV.
7. Run `settle` to close out any horizon-expired entries; report outcomes.
8. End with at most 3 candidate actions (e.g. "AAPL scan looks worth a
   /propose") — candidates, not proposals. If nothing qualifies, say
   "nothing today" — silence is a valid brief.

Keep it under ~30 lines. Every number from data, none from memory.
