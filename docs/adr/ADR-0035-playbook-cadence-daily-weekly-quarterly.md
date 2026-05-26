# ADR-0035: Playbook Cadence — Daily / Weekly / Quarterly (NOT Intraday)

**Status:** Proposed
**Date:** 2026-05-26
**Wave:** Cadence shift — interday/interweek/interquarter scheduler
**Supersedes:** Implicit assumption in `ops/scripts/quant-hourly-tick.py` and any
  intraday-flavored design hints scattered in earlier session arcs.
**Related:** ADR-0015 (HITL propose/decide/react), ADR-0016 (autonomous mode),
  ADR-0020 (backtest harness), ADR-0021 (PDR recipe runtime), ADR-0029 (multi-leg
  paper reactor), ADR-0030 (daily picker recipe).
**Cost:** $0.

---

## Context

The 5-play playbook (`covered_call`, `csp`, `wheel`, `leaps`, `swing`) operates
on **multi-day to multi-quarter** holding periods:

| Play | DTE / hold |
|---|---|
| covered_call | 21–36 DTE, hold to expiry |
| csp | 21–45 DTE, hold to expiry/assignment |
| wheel | rotating CSP→assignment→CC→called-away cycle, ≥30 days/leg |
| leaps | 12–18 month DTE, hold ≥6 months |
| swing | 30–90 DTE directional underlying, exit on target/stop |

There is no play in the playbook that operates on a sub-daily horizon. The
existing `hermes_quant/analysts/*` and `hermes_quant/advisor.py` already default
to `timeframe="1d"`, the universe scanner runs daily premarket, and the
watchlist evolves at a daily cadence. The codebase shape was already correct;
only the cron-scheduling and orchestration pieces had drifted toward an
intraday assumption (`ops/scripts/quant-hourly-tick.py` with `0 7-13 * * 1-5`,
and a stale "30-min tick" idea from yesterday's planning).

The user (2026-05-26 17:30Z) explicitly redirected:

> Idk if we want to do intra day trading when we should try to focus on
> interday trading. Maybe even interweek or inter quarter.

The hourly-tick is **read-only monitoring** (snapshots Alpaca state, alerts on
threshold breaches) and that posture is fine to keep. What we add here is the
**three positive-action cadences** at which the system actually decides and
fires.

## Decision

Adopt three **deterministic, non-overlapping** action cadences, fired by the
existing Hermes cron scheduler with `no_agent=true` so each tick is reproducible
script execution, not LLM reasoning. All cron expressions are in the gateway
host's local timezone (Pacific, with DST applied automatically).

| Cadence | Trigger | PT crontab | ET equivalent | Owner script |
|---|---|---|---|---|
| **Universe scan** | daily, premarket | `15 3 * * 1-5` | 06:15 ET | `quant-universe-scan.py` (already scheduled) |
| **Watchlist evolve** | daily, post-scan | `30 3 * * 1-5` | 06:30 ET | `quant-watchlist-evolve.py` (already scheduled) |
| **Daily decision** | daily, 1 hr pre-open | `0 6 * * 1-5` | 09:00 ET | `quant-playbook-tick.py` (NEW) |
| **Weekly rebalance** | Mondays, 30 min post-open | `30 6 * * 1` | 09:30 ET | `quant-playbook-weekly.py` (NEW) |
| **Quarterly review** | first Mon of Jan/Apr/Jul/Oct | `30 6 1-7 1,4,7,10 1` | 09:30 ET | `quant-playbook-quarterly.py` (NEW) |
| **NTA reconcile** | daily, post-fill | `0 6 * * 2-6` | 09:00 ET (Tue-Sat) | `quant-settle-reconcile.py` (NEW per ADR-0029 §D3) |
| **Hourly health monitor** | every hour, market hours | `0 7-13 * * 1-5` | every hour 10–16 ET | `quant-hourly-tick.py` (already scheduled, **read-only**) |

The hourly monitor is explicitly **read-only**: it snapshots account state,
detects threshold breaches, emits alerts. **It never proposes, never fires.**
All firing happens at the four named action cadences above.

## Per-cadence semantics

### Daily decision (06:00 PT, 09:00 ET, 1 hour before market open)

1. Load the evolved watchlist (`~/.hermes/quant/watchlist/play-fit.json`).
2. For each `(symbol, play)` with `state="active"` AND no existing position
   tagged with that play:
   - `compute_play_snapshot(symbol)` (yfinance, cached for the day)
   - Run the existing PDR pipeline: `advisor.recommend(symbol, recipe=play)`
   - Apply `silence_bias_gate` (existing)
   - Apply the options-aware risk gate from ADR-0027 (when landed)
   - If approved, build a multi-leg proposal via ADR-0029 reactor (when landed)
   - Fire as paper-mleg
3. For each `(symbol, play)` with an existing position whose signal **flips**,
   queue an exit through the same path.
4. Append a tick summary to `~/.hermes/quant/playbook/tick-journal.jsonl`.

**Override / silence rules** (must run before firing):
- Overnight gap: if `|spot_now − prior_close| / prior_close > 1.5 × ATR-14`,
  silence all proposals for that symbol this tick (gap risk).
- `days_until_earnings < 5`: silence covered_call and csp proposals (earnings
  surprise risk; mirrors the existing `days_since_earnings >= 5` rule).

### Weekly rebalance (Mondays 06:30 PT, 09:30 ET, 30 min after market open)

1. Reconstruct portfolio from execution journal (existing `portfolio_loader`).
2. For each option leg with `expiration ≤ today + 5 trading days`:
   - Decide: roll (sell-to-close + open new far-dated leg), let-expire, close
   - Use ADR-0030 recipe rules to choose
3. For each swing position:
   - If `days_held > 60` and `pnl_pct < 0` → close
   - If `pnl_pct > 3 × ATR-14_at_entry` → take profit
4. Fire batch through HITL queue (low urgency — these are MAINTENANCE actions,
   not silence-gated).
5. Append journal.

### Quarterly review (first Monday of Jan/Apr/Jul/Oct, 06:30 PT, 09:30 ET)

1. Read all positions; compute portfolio metrics:
   - NAV, sector breakdown, beta-weighted delta, theta/day, vega/$ NAV
2. Factor-exposure check:
   - Any sector > 30% of NAV → flag
   - Portfolio beta < 0.5 or > 1.5 → flag
   - Net delta > 0.6 × NAV → flag (overweight directional)
3. Emit a quarterly markdown report (reuse `to_markdown_report` shape).
4. Propose a rebalance batch (close-overweight, scale-underweight) through the
   same HITL queue.

## Consequences

### Positive

- **Cron schedule is now an artifact**, not tribal knowledge. `hermes quant
  playbook install-crons` (NEW CLI) sets up all six entries idempotently.
- **No intraday firing path exists** — the only firing surfaces are the four
  named cadences. Any future "fire on tick" temptation requires a new ADR.
- **Hourly monitor stays read-only**, preserving its safety-net role for
  drawdown / regime / position alerts without introducing new failure modes.
- **Backtest cadences match production cadences** — `option_replay()` (ADR-0020
  amendment) and `walk_forward` runs at the same daily/weekly/quarterly grain.
- **Calibrator training is consistent**: every decision the calibrator sees has
  the same horizon as the production fills, eliminating an
  intraday-vs-daily-mismatch source of label noise.

### Negative

- **Gap risk is real and unmitigated mid-day**. If a position blows up at 11:00
  ET, no script will close it until the weekly cron next Monday. Mitigations:
  (a) the hourly monitor's `PER_POSITION_STOP_PCT` alert at -10% surfaces it to
  the operator immediately; (b) per-position stops are NICE-TO-HAVE on the
  weekly cron and can be promoted to a separate "stop-loss watcher" cron at any
  time without re-arguing this ADR.
- **Six new crons** to operate, but they're all `no_agent=true` deterministic
  scripts. Operational burden is a single `cronjob list` line per cadence.
- **Calendar arithmetic**. The first-Monday-of-quarter expression
  (`30 6 1-7 1,4,7,10 1`) is correct standard-cron idiom, but easy to misread.
  Documented and tested.

### Neutral

- The hourly monitor's `0 7-13 * * 1-5` schedule covers **10:00–16:00 ET**
  market hours during DST (PDT = UTC-7). Outside DST (PST = UTC-8) it shifts to
  09:00–15:00 ET, which still covers the close. This is acceptable because the
  monitor is read-only and its job is alerting, not firing — a one-hour shift in
  the boundary doesn't change correctness.

## Out of scope

- **True intraday tick reactor.** The user has explicitly de-scoped this. Any
  future revisit must propose a new ADR, name the specific play that justifies
  sub-daily horizons (none in the current playbook do), and quantify the
  marginal Sharpe vs. the operational cost. Until then, intraday is a NO.
- **Sub-daily realized-vol calculation.** Rejected for the same reason — no play
  consumes it.
- **Sub-second order routing / TWAP / VWAP.** Live execution stays at
  market-on-open / limit-day for the daily decision tick.

## Implementation notes

- The cron writer (`hermes quant playbook install-crons`) is the contract surface
  between this ADR and the operator. Tests exercise `idempotent re-install`,
  `crontab parse`, and `dry-run plan output`.
- All four action scripts MUST consult the halt state at start and exit silently
  if any halt is active for the relevant scope. Halt clearance is operator-only
  (existing pattern).
- Deliver mode is `local` for daily/weekly/quarterly (silent unless something
  fires). The hourly monitor stays `discord:#hermes-quant` because it's the
  alert channel.
- The chained `quant-universe-scan-daily` (03:15 PT) → `quant-watchlist-evolve-daily`
  (03:30 PT) → `quant-playbook-tick-daily` (06:00 PT) sequence has 2.5 hours of
  slack. If any link fails, the next runs anyway with stale state and emits a
  warning; failure is detected by `last_status` in `cronjob list`.

## Decision summary

We commit to **daily + weekly + quarterly** action cadences and explicitly
reject sub-daily firing. The codebase is already mostly aligned with this
posture; this ADR makes it the binding contract.
