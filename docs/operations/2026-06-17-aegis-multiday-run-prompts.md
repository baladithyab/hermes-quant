# AEGIS multi-day paper run — install/arm prompts + runbook (2026-06-17)

> **Goal:** arm the safety rails, reset to a clean book, run autonomous PAPER for a few
> days, and capture an HONEST performance record. This produces the first trustworthy
> number to judge the strategy against. **It does not promise profit** — paper at zero
> capital is where we *discover* whether there is edge (today: realized −4.64%, 0/11
> winners — almost certainly no edge yet). The point of the window is an honest record,
> not a good one.

## Where logs go

| Sink | What | Purpose |
|---|---|---|
| **Agent platform** (Discord, via each cron's `deliver=origin`) | live heartbeat: fires, halts, errors, the daily/weekly report | real-time eyeballing |
| **`~/.hermes/quant/*.jsonl`** (existing) | `executions.jsonl` (the ledger), `autonomous-tick.jsonl`, `proposals.jsonl`, `daily-portfolio-snapshots/` | the durable money record |
| **`~/.hermes/quant/aegis-runs/<run-id>/`** (NEW, this run) | `run-card.json` (which rails were armed + start time), `perf.jsonl` (one honest daily perf line) | the reviewable run journal — `grep`-able, self-contained, survives the window |

The daily perf line comes from `ops/scripts/aegis-run-snapshot.py`, which reuses the
canonical kill-switch/promotion P&L basis — so the number you review is the same number
the safety rails and the promotion gate see.

---

## PROMPT A — for **hermes-agent** (the live in-place host)

> Context: hermes-quant is ALREADY installed and `pdr.mode: autonomous` is already set —
> the tick is firing, but against DISARMED rails (the protective flags are not exported by
> the cron wrapper). So this is **arm + reset + observe**, not a fresh install. The arming
> is strictly protective. Paste the block below to hermes-agent.

```
You are operating the live hermes-quant (AEGIS Hermes shell) on this machine. Do the
following EXACTLY and in order. This is money-state work: dry-run and show me output
before any mutation, and STOP and report if any step's verification fails.

REPO: /mnt/e/CS/github/hermes-quant   PY: ~/.hermes/hermes-agent/venv/bin/python3
RUN_ID: pick today's date, e.g. 2026-06-18-paper-window

STEP 1 — deploy the fixed cron scripts (they are stale at ~/.hermes/scripts/, dated
2026-05-28). cp these from the repo, then `diff -q` each pair to confirm they match:
  cp ops/scripts/quant-portfolio-daily.py        ~/.hermes/scripts/
  cp ops/scripts/quant-strategy-retro-weekly.py  ~/.hermes/scripts/
  cp ops/scripts/quant-reset-paper-book.py        ~/.hermes/scripts/
  cp ops/scripts/aegis-run-snapshot.py            ~/.hermes/scripts/
  cp ops/scripts/quant-cron-harness.py            ~/.hermes/scripts/

STEP 2 — reset the paper book to a clean flat $100k (clears the corrupt AAPL row + the
loss history + the cs67 repeat-fire lots). DRY-RUN FIRST and show me the counts; only on
my confirmation run --apply:
  ~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-reset-paper-book.py \
      --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl
  # after I confirm the counts look right:
  ~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-reset-paper-book.py \
      --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl --apply --yes

STEP 3 — arm the safety rails. Edit ~/.hermes/scripts/quant-autonomous-tick-armed.sh and
ADD these export lines next to the existing ones (DELTA_NORMALIZER MUST come after the
Step-2 reset — flipping it against a non-reset book is hard-refused by a phantom-sell guard):
  export HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1
  export HERMES_QUANT_PER_POSITION_STOP=1
  export HERMES_QUANT_TAKE_PROFIT_SWEEP=1
  export HERMES_QUANT_POST_LOSS_COOLDOWN=1
  export HERMES_QUANT_DELTA_NORMALIZER=1
  export HERMES_QUANT_ACCOUNT_LOCK=1
Also confirm ~/.hermes/config.yaml has under quant.autonomous:
  per_position_stop_loss_pct: 0.08
  require_stop_loss: true

STEP 4 — write the run-card (records that the rails are now armed). Run it WITH the armed
env so the card captures the flags; it must print the 6 flags as armed, NOT "NONE":
  source ~/.hermes/scripts/quant-autonomous-tick-armed.sh 2>/dev/null  # or export the 6 flags inline
  ~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/aegis-run-snapshot.py \
      --run-id <RUN_ID> --write-run-card

STEP 5 — confirm the existing crons are enabled (they already are; do NOT create new ones).
Verify with the harness, then let them run on their normal schedule — the 30-min
autonomous tick is the heartbeat:
  ~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-cron-harness.py --list

STEP 6 — capture a daily perf snapshot. Either run it once now (post-reset baseline), and
once per day for the next ~3-5 trading days (you may add it as a daily cron at ~13:10 PT,
right after the EOD portfolio cron, delivering to the same Discord origin):
  ~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/aegis-run-snapshot.py --run-id <RUN_ID>
It appends one honest line to ~/.hermes/quant/aegis-runs/<RUN_ID>/perf.jsonl and prints a
Discord-friendly summary (realized P&L on the kill-switch basis, settled round-trips,
win-rate, open book).

STEP 7 — after the window, report: paste the contents of
~/.hermes/quant/aegis-runs/<RUN_ID>/perf.jsonl (the daily progression) + run-card.json,
and the latest weekly retro. Do NOT promote to live regardless of the number — this window
only establishes whether the strategy has any edge on a clean, armed book.

RAILS / CONSTRAINTS: never disable a safety flag mid-window; if the kill-switch trips or a
per_position_stop_fired event appears, that is the rails WORKING — report it, don't suppress
it. If any verification (diff, dry-run counts, run-card armed check) fails, STOP and show me.
```

---

## PROMPT B — for **claude-cowork** (the plugin host)

> Context: cowork-quant is a Claude Cowork PLUGIN (slash commands `/scan /propose /settle
> /watch /retro /status /schedule`), installed differently from hermes. It is HITL-first
> (no order execution, ever — stricter than hermes) and its committee runs in-session. The
> cowork session owns its own repo/contract; this prompt drives the run, not a code change.
> Paste to claude-cowork.

```
You are operating cowork-quant (the AEGIS Cowork shell). Set up and run a multi-day paper
observation window. cowork-quant is HITL/advisory only — you NEVER execute orders; you
produce gate-approved proposals and record human-confirmed fills.

STEP 1 — health check: run /doctor and /status. Confirm the plugin is installed, the
ledger (state_dir) is reachable, and the rails are green. If /doctor reports the quantcore
schemas are not yet re-pointed to the AEGIS core canonical contract (ADR-0095 / seed
aegis-ac01), note it but proceed — the run still produces an advisory record.

STEP 2 — set the schedule: use /schedule to register the daily watch turns (scan ->
committee -> gate -> propose) at your market-open and EOD cadence for the next ~3-5
trading days. These are SCHEDULED ADVISORY turns, not autonomous execution.

STEP 3 — each scheduled turn: /scan the universe, run the committee, /propose the
gate-approved trades. Because cowork is HITL, the proposals wait for the human; record any
the operator confirms via /settle (manual or read-only broker readback).

STEP 4 — logs: the cowork platform captures the turn transcripts. ALSO write a daily
one-line perf summary (realized/unrealized on the ledger fold, proposal count, approval
rate) to the run journal so the record is reviewable alongside the hermes run — use the
same shape as hermes' aegis-runs/<run-id>/perf.jsonl (asof, realized_pnl_frac_nav,
n_settled_roundtrips, win_rate, open_positions). If a shared aegis-runs dir is reachable,
write there; otherwise the plugin's own state_dir/aegis-runs/<run-id>/.

STEP 5 — after the window: run /retro and report the shadow-vs-real gap (system-unfiltered
vs human-approved — this is ADR-0096 Gate 2), plus the daily perf progression. Frame it
honestly: this is an advisory record at zero capital, measuring decision quality, not profit.

CONSTRAINTS: HITL only (rail #4 — no order execution); free text never drives state
(validate every model output, ABSTAIN on a bad parse); silence-by-default.
```

---

## After the window — how to read it (the review checklist)

1. **Was it armed?** `run-card.json` must show the 6 required flags set. A window run disarmed is
   not a valid test (it measures the old, unsafe behavior).
2. **Is the book clean?** First post-reset snapshot should show a flat/near-flat book —
   no AAPL +14.15 zombie, no carried loss history.
3. **The honest number:** read `perf.jsonl` day-by-day. Expect modest/negative — the
   strategy has shown no edge yet. The win is that the number is now *trustworthy* (armed
   rails, clean book, canonical basis).
4. **Did the rails fire correctly?** A `per_position_stop_fired` or kill-switch event is
   the system WORKING, not failing.
5. **Decide next:** if still no edge (likely), the next work is **strategy/alpha**, not
   plumbing — and the ADR-0096 gates (seeds aegis-ag01..04) make that record interpretable
   before any thought of live capital.

## Honest scope

This window makes the record TRUSTWORTHY (armed + clean + canonical basis). It does not
create edge. Per the charter: "lose money slowly, not catastrophically." Paper builds the
forward-only track record that *might*, much later, justify real capital — it does not
itself make money.
