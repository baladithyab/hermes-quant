# Operator Action Packet — 2026-06-17 (PDR profitability + safety session)

> **What this session did:** answered "how far are we from profitably running PDR" with
> a measured verdict (NOT profitable — the book lost -4.64%, 0/11 winners, one ASTS
> -20.9% blowup), diagnosed WHY (the portfolio safety rails are running DISARMED), built
> the genuinely-missing controls, and built tooling to observe the system. 14 commits on
> `docs/rearchitecture-shared-pdr-core`, nothing pushed. Every code change is flag-gated
> DEFAULT-OFF and byte-identical when off. **The owed actions below are yours — the agent
> never edits live `.env`, cron wrappers, or money state.**

## The one-line verdict

PDR already trades paper autonomously; it is NOT a plumbing problem anymore. The book
loses money because **all three portfolio rails are blind to a single open position's
unrealized loss** (the drawdown breaker measures a synthetic flat $100k → always 0%; the
kill-switch is realized-only; no per-position stop existed). This session built the
per-position stop + wired the dead cooldown + gave you the runbook to arm the rest. **Do
not promote to live** — the strategy must prove green on a clean window first (the now-
functional promotion gate, ar125, will correctly refuse a thin/losing book).

## What landed this session (agent-side, committed)

| Commit | What | Flag (default-OFF) |
|---|---|---|
| `0dc5f14` | ar127 — daily+weekly reports exclude+warn the corrupt AAPL=510 row (was faking +$70k) | always-on defense |
| `00c9c04` | ar125 — promotion gate's 2 always-block floors now derive from the settlement ledger | n/a (gate) |
| `b7d1b68` | ar128 — `quant-reset-paper-book.py` (safe wipe to flat $100k) | `--apply` required |
| `5058263` | per-position unrealized-loss stop monitor (the ASTS fix; 8% position-level) | `HERMES_QUANT_PER_POSITION_STOP` |
| `b53d497` | register `per_position_stop_fired` audit kind (review caught my own ar28-pattern miss) | n/a |
| `c17a4ec` | wire post-loss-cooldown sidecar so gate Rule 4 is live across ticks | `HERMES_QUANT_POST_LOSS_COOLDOWN` |
| `340dcc9` | `quant-cron-harness.py` — observe/drive the 26 crons locally | observe-by-default |
| `1634bd2`/`7618151` | ADR-0093 — host-neutral product name (fresh codename pending your pick) | n/a |
| + research/arch docs | 16 curated stop-loss vault notes; hierarchical architecture diagrams | n/a |

252 tests pass across all touched trees; full session is byte-identical with every new flag OFF.

## YOUR OWED ACTIONS (ordered — order matters)

The full detail + per-flag risk is in **`docs/operations/SAFETY-RAILS-ARMING-RUNBOOK.md`**.
Summary sequence:

1. **Deploy the fixed scripts** (ar115 deploy-drift — the live crons still run 2026-05-28 copies):
   ```bash
   cp ops/scripts/quant-portfolio-daily.py        ~/.hermes/scripts/
   cp ops/scripts/quant-strategy-retro-weekly.py  ~/.hermes/scripts/
   cp ops/scripts/quant-reset-paper-book.py        ~/.hermes/scripts/
   cp ops/scripts/quant-cron-harness.py            ~/.hermes/scripts/   # optional (observe tool)
   ```
2. **Reset the paper book** (clears the corrupt row + loss history; also REQUIRED before flag #3 below):
   ```bash
   python3 ops/scripts/quant-reset-paper-book.py --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl          # dry-run, inspect
   python3 ops/scripts/quant-reset-paper-book.py --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl --apply --yes
   ```
3. **Arm the disarmed rails** (add to `~/.hermes/scripts/quant-autonomous-tick-armed.sh`):
   ```bash
   export HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1   # P0 — arms the real-equity drawdown breaker
   export HERMES_QUANT_PER_POSITION_STOP=1           # P1 — the new 8% per-position stop
   export HERMES_QUANT_TAKE_PROFIT_SWEEP=1           # P1 — required TP exit rail for a clean AG-EQ-1 window
   export HERMES_QUANT_DELTA_NORMALIZER=1            # P1 — MUST follow step 2 (phantom-sell guard)
   export HERMES_QUANT_POST_LOSS_COOLDOWN=1          # P1 — Rule 4 re-entry cooldown now live
   export HERMES_QUANT_ACCOUNT_LOCK=1                # P2 — closes the cross-symbol cap race
   ```
   Optionally in `~/.hermes/config.yaml` under `quant.autonomous`:
   `per_position_stop_loss_pct: 0.08`, `require_stop_loss: true`.
4. **Let a clean window run**, then read the now-honest daily/weekly reports + the
   `per_position_stop_fired` audit events. Only a green book should walk the promotion gate to live.
5. **Pick the core codename** (ADR-0093): slate is Quorum (lead rec) / Keel / Escapement /
   Fulcrum, or a write-in. One-word reply is enough; I'll propagate it through the docs next session.

## Observe the suite without waiting (the new harness)

```bash
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-cron-harness.py --list          # the day's 26 jobs + next fire
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-cron-harness.py --run all        # safe dry sweep (skips -armed wrappers)
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-cron-harness.py --run all --armed # real fires (use against a fresh book)
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-cron-harness.py --daemon         # 24/7 mirror of the live cron cadence
```
First observe-run already surfaced: universe-scan + watchlist-evolve need minutes (network/LLM),
playbook-weekly reports `errors:1` in dry-run, and the deployed retro reads stale state (ar115).

## Backlog state

`.seeds` open = **8**, all operator/data/governance/out-of-scope-gated (verified unchanged
vs live state this session). The real open work this session was the profitability/safety
axis above, not `.seeds`. Resume hints for next session: iteration-3 defensive hunt (slippage
calibration for high-vol names, the synthetic-portfolio gate path, cross-symbol cap race),
or exercise the harness armed against a reset book, or propagate the chosen codename.
