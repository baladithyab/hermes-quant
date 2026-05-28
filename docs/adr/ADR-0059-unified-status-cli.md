# ADR-0059: Unified `quant status` CLI for single-pane observability across event stores

**Status:** Accepted
**Date:** 2026-05-27
**Author:** Hermes-Quant Subagent (v0.4-1)
**Supersedes:** none
**Superseded-By:** none

---

## Context

By v0.3 the system writes to **six** independent append-only JSONL event stores
plus the `state.db` sqlite cache:

| # | Store | Path (under `~/.hermes/quant/`) | Producer | ADR |
|---|---|---|---|---|
| 1 | governance audit log | `governance/audit_log.jsonl` | every gate / kill switch / promotion | ADR-0031 |
| 2 | committee decisions | `memory/decisions.jsonl` | DecisionLog | ADR-0042 |
| 3 | post-trade reflections | `memory/reflections.jsonl` | Reflector | ADR-0042 / ADR-0057 |
| 4 | hypothesis registry | `research/hypotheses.jsonl` | HypothesisRegistry | ADR-0048 |
| 5 | run cards | `research/run_cards.jsonl` | RunCardLog | ADR-0048 |
| 6 | factor verdicts | `factors/factor_verdicts.jsonl` | FactorOracle | ADR-0055 |

Plus `state.db` (positions/cash) and a smaller `promotion_decisions.jsonl`.

These stores are the canonical source of truth for everything from "did
the risk gate reject anything in the last hour" to "how many open hypotheses
do we have right now". But there is **no single command** that lets an
operator look at all of them at once. The existing surfaces are scattered:
`hermes quant status` (process status), `hermes quant signals` (signal
records), `hermes quant doctor` (calibration), but none of them touch the
event stores. Every store currently requires its own ad-hoc shell pipeline
of `tail … | jq …`.

For a v0.4 production rollout we need observability that is:

1. **Read-only and side-effect free** — never mutate the stores; the audit
   trail must remain canonical.
2. **Crash-proof** — bad/partial JSONL lines must not raise; missing files
   must not raise. Per ADR-0031 silence-by-default, errors degrade into
   structured warnings, never exceptions.
3. **Memory-bounded** — these files grow unboundedly. We cannot load
   `audit_log.jsonl` fully into memory the way an interactive `cat` would.
4. **Low-dependency** — stdlib only. No new packages, no new daemons.

## Decision

Ship a single `quant status` command with a tail-read implementation:

* **Module:** `hermes_quant.cli.status`. Public surface:
  * `quant_status(asof_window: timedelta = 24h, quant_home: Path | None = None) -> StatusReport`
  * `format_status_human(report) -> str` — multi-section text view
  * `format_status_json(report) -> str` — machine-consumable JSON
  * `run_cli(argv) -> int` — argparse entry point
* **Script:** `scripts/quant-status.py` — thin wrapper around `run_cli`,
  always exits 0 (read-only).
* **Tail-read semantics.** Each JSONL is opened in binary mode. If the file
  is `<= 256 KiB` it is read whole; otherwise we `seek(size - 256 KiB)`,
  read the last window, and **discard the first (potentially partial) line**.
  Each line is JSON-decoded individually with `json.JSONDecodeError`
  per-line handling — malformed lines are skipped and a warning is added to
  `StatusReport.warnings`. We never raise.
* **Time handling.** `datetime.now(timezone.utc)` is the reference clock.
  Naive ISO timestamps in JSONL are treated as UTC. Events are bucketed
  in/out-of-window with a single `now - asof <= window` test.
* **Aggregations per store:**
  * audit_log → counts by `kind`; in-window counts of
    `proposed_today`/`approved_today`/`rejected_today`; top-3 rejection
    reasons sorted by count desc.
  * decisions → last 5 (newest first), `kind="decision"` rows only.
  * reflections → last 3 (newest first).
  * hypotheses → open count + 3 most-recent registrations; status is
    derived from initial `kind="hypothesis"` rows mutated by subsequent
    `kind="status_change"` rows.
  * run_cards → last 3 with `verdict="falsified"` highlighted in human
    output.
  * factor_verdicts → tier counts (premium/standard/experimental/rejected),
    with **latest-per-`factor_id` semantics** so an upgrade-then-rejection
    is not double-counted.
  * state.db → opened with `mode=ro` URI, `positions` and `cash` tables
    surfaced as typed dataclass views.

### Non-decisions (explicitly out of scope)

* No mutation surface. No "clear", "compact", "rotate" commands here.
  Append-only invariants per ADR-0031 are preserved.
* No real-time follow mode. `--follow` exists on `hermes quant signals` for
  signal records; we do not extend it to event stores in this ADR.
* No web UI or HTTP server. See "Alternatives" below.

## Consequences

**Positive:**

* Operators get a one-command snapshot of the entire write surface,
  exactly the kind of pre-trade / pre-promotion sanity check we want
  before flipping rollout flags in v0.4.
* Bounded memory: the implementation reads at most ~256 KiB per file plus
  state.db. Verified with a >1 MiB synthetic fixture.
* JSON output makes it trivial to wire this into any external dashboard
  (`watch -n 30 'quant-status --format json | jq …'`) without committing
  to a particular UI yet.
* New module, no risk to existing tests. The full suite remains green.

**Negative / accepted trade-offs:**

* We only see the **tail** of each file. If an operator suspects an
  anomaly older than the tail window, they still need to fall back to
  ad-hoc tooling. This is fine because those investigations are rare
  and require deeper queries anyway (e.g.
  `hermes_quant.governance.audit_log_query`).
* Latest-per-factor_id collapsing for factor verdicts means a viewer can't
  distinguish "1 factor ever evaluated" from "1 factor evaluated 50 times
  with the same final tier" from this surface alone. The audit log is the
  authoritative replay record; this is a summary.
* We do not parse `promotion_decisions.jsonl` here — it is folded into the
  `audit_log.jsonl` `promotion_event` kind and listing both would cause
  double counting. Explicit non-goal.

## Alternatives Considered

### A. Web dashboard (rejected)

A small Flask/FastAPI dashboard that polls these files and renders
HTML+charts. Rejected as **scope creep for v0.4**: it pulls in a server
process, a port to manage, an auth question, an HTML toolchain, and a
deploy story. None of those are blockers for "an operator wants to know
what's in the event log right now" — a CLI delivers 100% of that signal
with 0% of the operational surface. We may revisit a dashboard once the
canonical aggregations are stable and externally consumed.

### B. Per-store CLIs (rejected as fragmented)

We could ship `quant audit`, `quant decisions`, `quant hypotheses`,
`quant verdicts`, `quant runs` each with its own filters. Rejected
because:

* The whole point of "single-pane observability" is that the operator
  doesn't have to remember six command names.
* Each per-store CLI would still re-implement the same JSONL tail-read,
  warnings-list, JSON formatter — multiplying maintenance.
* The `--store` filter on the unified command gives operators the
  same per-store focus when they want it, without forcing the
  fragmented model on the default path.

### C. Inline aggregation in the existing `hermes quant status` (rejected)

The existing `hermes quant status` reports daemon process state. Folding
event-store aggregations into it would conflate "is the daemon running"
with "what has it written". Different audiences, different SLAs. We keep
them separate; the new command has its own script entry point.

## Pointers

* Implementation: `hermes_quant/cli/status.py`
* Script: `scripts/quant-status.py`
* Tests: `tests/cli/test_status.py`
* Related ADRs: ADR-0031 (audit log invariants), ADR-0042 (memory),
  ADR-0048 (hypotheses + run cards), ADR-0055 (factor verdicts), ADR-0057
  (Reflector v0.2), ADR-0041 (state.db sign convention).
