---
description: Render the quant-state dashboard — static HTML snapshot of book, P&L, calibration, queue
allowed-tools: ["Read", "Write", "Bash", "Glob"]
---

Render `<workspace>/quant-state/dashboard.html` from live state. Read-only:
this command records NOTHING to the ledger (no marks, no settles, no events).
Every number comes from the CLI or ledger.jsonl — never from memory.

1. Gather (quantcore installed per the `quant-core` skill):
   - `python -m quantcore.cli status --state-dir <workspace>/quant-state`
   - `python -m quantcore.cli hyp summary --state-dir <workspace>/quant-state`
   - `python -m quantcore.cli events --window-days 7`
2. Read `<workspace>/quant-state/ledger.jsonl` directly (one JSON object per
   line) and extract:
   - all `mark` events, in file order → `marks: [{ts, nav}]`
   - all `settle` events → `settles: [{ts, asset, horizon, entry_price,
     exit_price, realized_return, direction_correct}]`
3. Assemble one DATA JSON object matching the schema documented in the HTML
   comment at the top of `assets/dashboard_template.html` (read it). Mapping:
   - `nav`, `peak_nav`, `halted`, `halt_reason`, `halt_until`, `positions`
     ← `status.portfolio`; `drawdown_pct` = (peak_nav − nav) / peak_nav.
   - `pending_proposals` ← `status.pending_proposals`, flattened to
     `{proposal_id, asset: signal.asset, direction: signal.direction,
     target_position_pct, created_at, gate_reason}`.
   - `ledger_integrity` ← `status.ledger_integrity`; `risk_profile` ←
     `status.risk_profile`; `calibration` ← `status.calibration` reduced to
     `{<analyst>: {n, ece}}`.
   - `hypotheses` ← the `hyp summary` output (`{overall: {mean_brier,
     count}}`), or null if the call fails.
   - `events` ← the `events` output list; `generated_at` = now (UTC ISO).
   - `regime`: only if a regime read already exists from this session (e.g.
     today's /brief); never fetch data just for the dashboard — set null.
4. Read `assets/dashboard_template.html` (plugin-root relative path), replace
   the exact string `/*__DATA__*/null` with the JSON blob (it occurs exactly
   once), and write the result to `<workspace>/quant-state/dashboard.html`.
   Do not edit the template file itself.
5. Present the written file to the user with `mcp__cowork__present_files`,
   plus a one-line summary (NAV, drawdown, pending count, halt state).

If ledger integrity fails: still render — the template shows a red FAILED
banner first — and tell the user to investigate quant-state/ledger.jsonl
before trusting any number. Empty state is fine: the template renders
"no data yet" per section.
