---
status: proposed
date: 2026-06-01
deciders: [codeseys]
amended_by: ADR-0086
---

> **Amended-by ADR-0086 (2026-06-02):** the projection's *accounting math* (units,
> mark-to-market equity, short sign) is tightened by ADR-0086. The event-sourced
> authority decision in this ADR (executions.jsonl authoritative, state.db a
> derived projection) is **preserved in full** — only how the projection folds the
> log changes.


# ADR-0085: executions.jsonl is the authoritative event log; state.db is a derived projection

## Context and Problem Statement

On 2026-06-01 the end-of-day portfolio report announced **+$167,279 open P&L** — almost
entirely fictional. Investigation (memory: `state-db-test-isolation-leak`) traced it to
**test fixtures leaking into the live paper ledger** `~/.hermes/quant/state.db`: positions
like `NVDA 2200@$150.00`, `GME -15.4@$200.00`, `BTC/USDT -10@$200.00`, and an option showing
−2380%, none of which have any backing fill in `executions.jsonl`. Their `processed_fills`
rows carry test-fixture proposal_ids (`prop_2026-05-30T00:00:00_GME_abc123`,
`prop_test_AAPL_abc123`, a canned-timestamp loop `prop_20260601T160000_NVDA_<hex>`).

The mechanism: `PortfolioState.__init__` (`portfolio_state.py:198`) defaults its DB path to
the live `DEFAULT_STATE_DB`, and `tests/conftest.py` isolates governance/evidence/kill-switch
state but **not** `state.db` nor the `get_portfolio_state()` module singleton. So any test
that constructs `PortfolioState()` without a `tmp_path` writes the live ledger.

The deeper problem this exposed: hermes-quant has **three “sources of truth” for paper
state that have drifted apart**, and no rule says which wins:

| Ledger | What it holds | Today |
|---|---|---|
| Alpaca paper sandbox | what the broker actually holds | $100K equity, 2 real positions (BA long, CRWD), ≈ +$80 |
| `~/.hermes/quant/executions.jsonl` | append-log of every fill the system decided (HITL/paper/replay) | 176 real fills, 7 today |
| `~/.hermes/quant/state.db` `positions` | materialized current positions | 55 positions, fictional +$167K (test-polluted) |

Symptoms of the same disease elsewhere: `quant_status` reports a stale 2026-05-13 halt from
the deprecated `signals.jsonl` while the authoritative `halt_state.json` is empty (ADR-needed
fix #49); `quant_autonomous_status` reports watchlist `size:0` while the live tick scans 117
symbols from `play-fit.json` (#50). Each is a “which file is authoritative?” question with no
documented answer. **No rail was breached** — `HERMES_QUANT_AUTONOMY` is unset and the armed
tick placed 0; this is a data-integrity and reporting problem, not a trading-authority one.

## Decision Drivers

- **Decidability.** "Is this position real?" must be answerable mechanically. Today it isn't.
- **Auditability / asof-honesty.** The paper track-record is the eval signal the whole
  default-OFF rollout depends on (ADR-0080). Fictional P&L corrupts that signal.
- **Test-pollution resistance.** A test must be physically unable to mutate live state.
- **Reconcilability with the broker.** When orders route to Alpaca, the local view and the
  broker view must be diffable, with a defined winner on conflict.
- **Silence-by-default / least surprise.** A reporting tool must never present unverifiable
  numbers as fact.

## Considered Options

- **A. Alpaca sandbox is the single source of truth** for positions; drop `state.db` as a
  position store.
- **B. `state.db` is authoritative** (status quo); fix isolation and move on.
- **C. Event-sourcing: `executions.jsonl` is the authoritative event log; `state.db` is a
  derived projection reconstructable from it; Alpaca sandbox is external reconciliation.**

## Decision Outcome

Chosen option: **C — event-sourced ledger with a derived projection**, because it makes
"is this position real?" decidable (a position is valid iff it folds from backing executions),
keeps the fast `state.db` read path, and survives both test pollution and broker drift.

Concretely:

1. **`executions.jsonl` is the authoritative, immutable, append-only event log** of every
   decided fill (decision-truth). Positions are *defined as* the fold of this log per account.
2. **`state.db` `positions`/`cash` is a derived projection (a cache)** — it must be
   reconstructable by replaying `executions.jsonl`. Any position with **no backing execution
   is invalid by definition** and may be purged. A `quant-ledger-reconcile` tool rebuilds the
   projection from the log and reports/heals divergence.
3. **The Alpaca paper sandbox is external reconciliation** (broker-truth for what is actually
   held there). A reconcile step diffs projection-vs-broker; on conflict for a live-routed
   account the **broker wins** (it holds the real position), and the divergence is logged, not
   silently overwritten.
4. **Reporting tools (`quant_status`, `quant-portfolio-daily`, `quant_autonomous_status`)
   read the authoritative source for each fact and never present a derived/stale value as
   current**: halts from the live halt registry (not `signals.jsonl`); positions from the
   reconciled projection with any execution-unbacked row excluded or flagged; watchlist from
   the one declared live source (`play-fit.json` for the autonomous tick).
5. **Tests may never touch live state.** An autouse `conftest` fixture points
   `DEFAULT_STATE_DB` at a `tmp_path` and resets the `get_portfolio_state()` singleton per
   test. (Implementation: task #50/#52 fix layer “PREVENT”.)

### Consequences

- **Positive**: "real position?" is decidable (folds from the log or it doesn't). The EOD
  report can no longer present test pollution as profit. The fast `state.db` read path stays.
- **Positive**: tests are physically sandboxed from live state — this class of leak cannot
  recur. The paper track-record (the eval signal) regains integrity.
- **Positive**: a documented conflict-resolution rule (broker wins for live-routed accounts)
  replaces silent drift.
- **Negative**: a reconcile/rebuild step is now required tooling and a periodic cron — new
  surface to maintain, and a full replay of a large `executions.jsonl` has a cost.
- **Negative**: `executions.jsonl` becomes load-bearing — if it is ever truncated/rotated
  without an archived continuation, the projection cannot be rebuilt. It must be treated as
  durable, append-only, and backed up (note the existing `.bak-dupfire` rotation).
- **Neutral**: existing polluted `state.db` rows must be purged once (a one-time cleanup,
  backed up first) before the projection is trustworthy.

## Pros and Cons of the Options

### A. Alpaca sandbox is the single source of truth

- Good, because it is the genuine broker truth for what is held.
- Good, because it eliminates a local position store entirely.
- Bad, because it only reflects orders that actually routed to the broker — today the
  propose→HITL pipeline approves few, so the broker holds almost nothing while the system has
  a rich decision history. The decision-truth (what the system chose) would be lost.
- Bad, because it makes the system depend on a live external service for every status read
  (offline/replay/backtest break), violating the offline-deterministic posture.

### B. state.db is authoritative (status quo)

- Good, because it is the fast existing read path and needs the least change.
- Bad, because it is exactly what failed — a mutable materialized view with no rule tying it
  to backing fills, corruptible by any test. "Is this real?" stays undecidable.
- Bad, because it cannot be reconciled against the broker without a separate truth.

### C. Event-sourced log + derived projection (chosen)

- Good, because positions become a pure function of an immutable log → decidable, auditable,
  rebuildable, test-pollution-proof.
- Good, because it keeps the fast `state.db` cache while making it disposable/rebuildable.
- Good, because it composes with broker reconciliation (diff projection vs Alpaca).
- Bad, because it adds a reconcile/rebuild tool + cron and makes `executions.jsonl` durability
  critical.

## More Information

- Provenance + fingerprints: memory `state-db-test-isolation-leak`; investigation tasks #52.
- Implements/governs the fixes: #49 (halt-read), #50 (watchlist + ledger reconcile),
  the EOD-report execution-backed-only change, and the conftest isolation fixture.
- Relates to ADR-0004 (deterministic gate — unaffected; this is below the gate, on
  bookkeeping), ADR-0015 (HITL), ADR-0080 (the paper track-record is the eval signal).
