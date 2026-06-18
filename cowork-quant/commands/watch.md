---
description: Unattended autonomous turn — settle, mark, scan watchlist, queue gate-approved proposals (no approvals, no fills)
allowed-tools: ["Read", "Write", "Bash", "Glob", "Agent", "ToolSearch"]
disallowed-tools: ["AskUserQuestion"]
---

<!-- B-33: `allowed-tools` only GRANTS; it does not restrict. `disallowed-tools`
makes AskUserQuestion uncallable here, so an unattended turn physically cannot
ask for (or fabricate) human approval — fail-closed. The plugin-root PreToolUse
deny-hook (hooks/hooks.json) is the second layer: it denies order/transfer tools
always, and approve/fill/resume CLI verbs when COWORK_QUANT_UNATTENDED is set. -->


This is the AUTONOMOUS PERCEPTION-DECISION turn (hermes-quant ADR-0016/0024
port). It is designed to run UNATTENDED from a scheduled task — therefore it
MUST NOT use AskUserQuestion, record approvals, record fills, resume halts,
or ask anything. It perceives, decides, queues, and reports. The human
reacts later.

Read the `quant-core` and `analysts` skills first. State dir:
`<workspace>/quant-state`. Install quantcore if needed.

Sequence:

1. **Hygiene**: run `verify` — on integrity failure, write the failure to
   `quant-state/briefs/<date>-watch.md` and STOP (never write past a broken
   chain except this note).
2. **Expire stale proposals**: run `python -m quantcore.cli expire
   --state-dir <quant-state>` (deterministic TTL sweep; the CLI also refuses
   approval of stale proposals outright). Queued proposals are decision-time
   artifacts; yesterday's gate run is not today's truth.
3. **Settle + mark**: fetch current prices for open positions, record `mark`
   events, run `settle`. Note outcomes for the report.
4. **Scan**: run the full committee (>= 2 rubrics + event_risk collection) on
   each watchlist ticker, exactly as /scan, aggregating with the `aggregate`
   CLI. For high-conviction signals (>= 0.65) run the opposing debate agent
   and apply its haircut — unattended turns get MORE dissent, not less,
   because nobody is watching.
5. **Queue**: for each committee signal with direction != 0, run
   `propose`. Gate silence -> record in the report (it is already in the
   ledger as gate_decision). Gate action -> the proposal is now PENDING in
   the ledger awaiting the user's interactive review.
6. **Report**: write `quant-state/briefs/<date>-watch.md` — regime line, per-
   ticker committee table, settlements, newly queued proposals, halt state.
   End the session response with a 5-line digest: "N proposals queued, M
   settled, book at X% NAV gross — review with /status".

Hard rails for unattended mode:
- NEVER approve, reject (except via the expire sweep), fill, or resume a halt.
- NEVER loosen config to make a proposal pass; config.json is read-only here.
- If a circuit breaker fires during this turn, the report LEADS with it.
- If data is unavailable, skip and say so; an empty watch turn is valid.
