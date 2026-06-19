# Prompt for Aria — adapt/upgrade/integrate the AEGIS options + watchlist work

Paste the block below to Aria (the hermes agent). It hands off everything landed in the
2026-06-18/19 backlog drive on `docs/rearchitecture-shared-pdr-core` so Aria can adapt the
integrated setup, arm what the operator approved, and run the evidence loop.

---

**Context.** A backlog-drive session on branch `docs/rearchitecture-shared-pdr-core` (base
`4bb7271`, +26 commits, 916 tests green, HEAD integrity verified) drove the agent-codeable
backlog to zero. Two independent review teams confirmed convergence over two rounds: the
agent-codeable axis is exhausted; the 18 open seeds are all operator/run-time/cowork/external-
data-gated. Everything below is **default-OFF in source** (CI/backtest/eval run clean) — arming
is wrapper-export only.

**What landed (all default-OFF, RED-proven, byte-identical-when-off):**

- **Watchlist rearchitecture (W1–W6):** profile-fit scanner replaces strategy-bucketing — score
  each ticker against ONE target-trading profile, emit ONE ranked watchlist
  (`HERMES_QUANT_PROFILE_SCAN`); the decision layer picks structure per tick. Multi-horizon
  0D/1D/7D/14D/30D (`HERMES_QUANT_ZERO_DTE`, `HERMES_QUANT_MULTI_HORIZON_TICK`). Standalone
  builder `ops/scripts/quant-profile-watchlist.py`. `market_cap` moved HARD→eviction so the
  zero-network standalone path works (decision layer still hard-gates with full data).
- **Options Perceive→Decide→React→Monitor stack:**
  - `ageq2` composite `(asset_class,symbol)` open-book keying (live in the tick).
  - `ml00b` composite rows + `option_legs` persisted at origination.
  - `agperc3` live chain-fetch body + `aegis-chain-prefetch.py` (the daily prefetch cron payload).
  - `agmon1`/`agmon2` options stop-loss + take-profit sweeps (`HERMES_QUANT_OPTIONS_MONITOR`),
    fail-CLOSED on a missing/NaN mark (HOLD, never fabricate a close).
  - `ml01b` leg-ops decompose/convert wired to a live trigger (`HERMES_QUANT_COMPOSITE_LEG_OPS`).
  - `ag01b` correlated-basket de-lever in the tick (`HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING`).
  - `bf76b` `aegis-gate2-eval.py` — the GATE-2 options-unlock evidence writer.
- **`jw1` audit-trail fix (load-bearing):** options fires now write their `SettlementEntry` —
  before this, every autonomous options trade silently lost its audit entry (the writer read
  `proposal.asset_class`/`.symbol` which `MultiLegProposal` lacks, AND `SettlementEntry`'s Literal
  rejected `multi_leg`). Without jw1, an armed options book would accrue no evidence.

**Your tasks, Aria:**

1. **Arm options per the operator's decision** (everything ON incl. origination — explicit
   ADR-0029 override, paper). Follow `docs/operations/2026-06-19-arm-options-operator-runbook.md`
   exactly and in order: protective rails + audit first, origination last. The order is a safety
   invariant — never arm `AUTONOMOUS_OPTIONS` before the `OPTIONS_MONITOR` rails (a position must
   never exist without its stop). Register the two new cron scripts (`aegis-chain-prefetch.py`
   daily before the tick; `aegis-gate2-eval.py` daily/weekly).

2. **Watch the first options fires.** Confirm each `multi_leg`/`us_option` fire in
   `executions.jsonl` writes a matching `SettlementEntry` (jw1). If an options fire has NO journal
   entry, STOP — the jw1 fix regressed. Watch the `agmon1`/`agmon2` sweeps actually mark + protect
   open options positions once `aegis-chain-prefetch.py` has populated `option_chains/*.parquet`.

3. **Run the evidence loop** (this is what arming is FOR): accrue the 30-day options window
   (`agoptev1`, N≥30 settled multi-leg outcomes), then `aegis-gate2-eval.py` writes the GATE-2
   verdict. Equity SL/TP window (`ageq1`) and the CAPS+SLIPPAGE default-ON promotion (`6bb9`)
   accrue in parallel. These are run-time gates — let the armed crons run; don't force them.

4. **Honor the rails.** Never flip a source-code flag default to `1` (it corrupts backtests/evals
   — arming is wrapper-only). Never weaken a fail-closed guard to force a trade. Never edit
   `cowork-quant/` or `ADR-0093/0094/0096`. The deterministic gate is the final authority;
   silence-by-default; no-lookahead.

5. **Remaining operator/run-time backlog (not code):** `ar115` (3 residual script redeploys),
   `6bb9`/`ageq1`/`agoptev1`/`agopt3` (run-time eval windows), `ba90`/`b67a` (data-gated),
   `d9d8` (ADR-freeze governance), `ob5` (external sentiment APIs + keys), `ac01/ac02/ac03`/`cw1`
   (cowork/packaging). The agent-codeable work under each is DONE; what remains is yours to run or
   the operator's to action.

**Branch state:** `docs/rearchitecture-shared-pdr-core`, HEAD has the full stack. Not pushed —
the operator decides merge/PR. The cowork ADRs (0093/0094/0096) are intentionally untracked; leave
them.

---
