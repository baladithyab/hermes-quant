# Wave 1d — Cron Diagnostics (2026-05-27)

Three crons were flagged as broken / silent in the Wave 1d roadmap entry.
After live verification on branch `feat/wave1-observability-and-state` they
are all functional; this doc records the diagnosis so future operators don't
re-walk the same trail.

## 1. quant-watchlist-evolve-daily (cron 82d3aa40024d)

**Symptom:** prior run at `2026-05-27_03-32-12` logged `script timed out
after 120s`. One-off log file in `~/.hermes/cron/output/82d3aa40024d/`.

**Root cause:** the original script iterated the full `alpaca-daily.json`
universe (~500 symbols) sequentially via yfinance (~1.5s/symbol →
~750s worst-case, far exceeding the cron's 120s budget).

**Status:** already fixed in the repo (header comment "PERFORMANCE FIX
(Wave 1d, 2026-05-27)"). Two changes:

1. Switched the input universe to `alpaca-daily-top100.json` (covers >90%
   of dollar volume, 100 symbols).
2. Added a `ThreadPoolExecutor(max_workers=20)` snapshot prefetch.

**Verification (this run):**

```
$ time python scripts/quant-watchlist-evolve.py
as_of=2026-05-27T19:06:33Z events=1
  covered_call   active=  0 + 0 - 1
  csp            active=  5 + 0 - 0  top5: AAPL:0.87, GOOGL:0.87, MSFT:0.87, AMZN:0.73, NVDA:0.73
  leaps          active=  5 + 0 - 0
  swing          active=  4 + 0 - 0
real    0m17.828s
```

17.8s end-to-end on a cold run — comfortably inside the 120s cap with ~6×
headroom.

## 2. quant-playbook-weekly (cron 291d25b942a9)

**Symptom:** `last_run_at=null` in the cron-store snapshot.

**Root cause:** the cron schedule is `30 6 * * 1` (Mondays 06:30 PT).
`last_run_at=null` simply means it has not fired yet on this branch — not
that the script crashes. The script already has graceful empty-portfolio
handling (`scripts/quant-playbook-weekly.py:397-404` writes a
`weekly_empty_portfolio` journal record and returns a zeroed summary
without scanning).

**Verification:**

```
$ python scripts/quant-playbook-weekly.py --json
{"event": "weekly_summary", "run_id": "2026-05-27T19:06:05Z",
 "monday_et": "2026-05-25", "armed": false, "scanned": 0,
 "closes_proposed": 0, "closes_fired": 0, "holds": 0,
 "options_skipped": 0, "skipped_idempotent": 0, "errors": 0,
 "halt_aborted": false}
```

No exception, exits 0, journal entry written.

## 3. quant-playbook-quarterly (cron 1bcf03c073bf)

**Symptom:** `last_run_at=null`.

**Root cause:** schedule is `30 6 1-7 1,4,7,10 1` — first Monday of
Jan/Apr/Jul/Oct only. We are not on a quarter-boundary Monday, so no fire
yet. Empty-portfolio handling is in place (`scripts/quant-playbook-quarterly.py:499`,
"Portfolio is empty" markdown branch).

**Verification:**

```
$ python scripts/quant-playbook-quarterly.py --dry-run
# Quarterly Portfolio Review — 2026Q2
...
_No rebalance actions proposed this quarter._
```

Clean dry-run on the empty-positions execution path.

## Conclusion

No code changes required for Wave 1d cron triage. The diagnostics log
above should be the first stop next time a `last_run_at=null` triggers a
roadmap entry — silence in a not-yet-due window is not the same as
brokenness, and the watchlist-evolve perf fix is already on this branch.

The deployed scripts at `~/.hermes/scripts/` (size 9849, identical SHA to
repo) confirm the fix is live in the cron environment.
