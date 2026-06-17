# Safety-Rails Arming Runbook (2026-06-17)

**Audience: the operator.** Every command here is operator-run — the agent never
edits `~/.hermes/.env`, the armed cron wrappers, or live money state. This runbook
explains WHY the paper book lost money and the EXACT, ORDERED steps to arm the
protective rails that are currently disarmed.

## Why this exists

The autonomous paper book lost **-4.64% realized** over 11 settled round-trips
(0/11 winners). One trade dominated it: **ASTS** bought \$118.17 (2026-06-04),
exited \$93.44 (2026-06-07) = **-20.9% position-level = -4.19% of NAV by itself**.

The deep-work review (2026-06-17) found the loss was NOT bad luck — it was that
**the portfolio safety rails are running disarmed**:

| Rail | Status | Why it didn't catch ASTS |
|---|---|---|
| ADR-0016 kill-switch (realized P&L) | armed | **Realized-only by design** (`autonomous.py:578`) — an OPEN position bleeding -20% contributes 0.0 until closed. |
| Gate drawdown / daily-loss breaker | **DISARMED** | Runs against a **synthetic flat \$100k portfolio** (flag `HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE` unset) → `drawdown_pct ≡ 0.0` at gate time → never fires. The cs86/cs01 real-equity store is built+tested, just not armed. |
| Per-position unrealized stop | **did not exist** | **Now built** (`HERMES_QUANT_PER_POSITION_STOP`, default-OFF) — the only rail that sees a single open position decline. |
| `require_stop_loss` entry backstop | DISARMED | Gates ENTRY SIZE only; never watches an open position. |

**Even with all portfolio rails armed, none would have caught ASTS** — a -4.19%
NAV single-position loss is under the 5% daily-loss and 15% drawdown thresholds.
The **per-position stop** (8% position-level = 1.6% NAV) is the only control that
catches this failure mode. That is why it was built.

> NOTE on runtime env: the live flags are set by the armed cron WRAPPER
> (`~/.hermes/scripts/quant-autonomous-tick-armed.sh`), NOT `~/.hermes/.env`. The
> wrapper currently exports `HERMES_QUANT_REFLECTION=1`, `HERMES_QUANT_PORTFOLIO_CAPS=1`,
> `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2`, `HERMES_QUANT_DETERMINISTIC_EQUITY=1`.
> It does NOT export the four protective flags below. Arming them means adding the
> `export` lines to that wrapper (or `~/.hermes/.env` if you prefer a global default).

## The ordered arming sequence (DO IN THIS ORDER)

The order matters — one flag (`DELTA_NORMALIZER`) has a hard guard that REFUSES to
apply against a state.db built under the other regime (it would phantom-sell). So
the book reset comes first.

### Step 0 — (prerequisite) deploy the fixed report + reset scripts

From the prior `make-pdr-tradeable` work (still owed per ar115):
```bash
cp ops/scripts/quant-portfolio-daily.py        ~/.hermes/scripts/quant-portfolio-daily.py
cp ops/scripts/quant-strategy-retro-weekly.py  ~/.hermes/scripts/quant-strategy-retro-weekly.py
cp ops/scripts/quant-reset-paper-book.py        ~/.hermes/scripts/quant-reset-paper-book.py
```

### Step 1 — reset the paper book to a clean flat \$100k (ar128)

This clears the corrupt AAPL=510 row, the realized-loss history, AND lets Step 2's
`DELTA_NORMALIZER` flip be safe (the fresh DB is rebuilt under the new regime).
```bash
# dry-run FIRST — inspect the counts, confirm only paper-default is touched:
python3 ops/scripts/quant-reset-paper-book.py \
    --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl
# then apply (backs up state.db/bus/proposals first, BEGIN IMMEDIATE atomic):
python3 ops/scripts/quant-reset-paper-book.py \
    --state-db ~/.hermes/quant/state.db --bus ~/.hermes/quant/executions.jsonl --apply --yes
```

### Step 2 — arm the four protective flags

Add these to `~/.hermes/scripts/quant-autonomous-tick-armed.sh` (alongside the
existing `export` lines):

```bash
# P0 — arms the real-equity drawdown/daily-loss breaker (vs the synthetic flat 100k
#      that always reads 0% drawdown). The cs86/cs01 DrawdownBaselineStore is built+tested.
export HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1

# P1 — the new per-position unrealized-loss stop (8% position-level default = 1.6% NAV).
#      The ONLY rail that catches a single open position bleeding (the ASTS failure mode).
export HERMES_QUANT_PER_POSITION_STOP=1

# P1 — the absolute-target normalizer fold (ADR-0091 Option E). MUST come AFTER the
#      Step-1 reset: flipping it against a flag-OFF-built state.db is HARD-REFUSED
#      (phantom-sell guard, portfolio_state.py:226). A freshly-reset DB rebuilds under
#      the ON regime and is stamped, so it is safe post-reset.
export HERMES_QUANT_DELTA_NORMALIZER=1

# P2 — close the cross-symbol portfolio-cap TOCTOU race. Only needed because
#      PORTFOLIO_CAPS is already armed; without the lock two symbols can both pass the
#      cap pre-fire. Safe to add.
export HERMES_QUANT_ACCOUNT_LOCK=1
```

Optionally also set the per-position stop threshold in `~/.hermes/config.yaml`
under `quant.autonomous` (defaults to 0.08 if absent; finite-guarded):
```yaml
quant:
  autonomous:
    per_position_stop_loss_pct: 0.08   # 8% position-level; 1.6% NAV at the 20% max position
    require_stop_loss: true            # entry-size backstop (defense-in-depth, complements the stop)
```

### Step 3 — verify on a clean window

Let the armed crons run a fresh paper window. Then read the (now-honest) daily +
weekly reports. The per-position stop emits a `per_position_stop_fired` governance
audit event each time it fires — visible on `cli/status.py`. Confirm:
- a position that bleeds past -8% gets force-exited (no more -20% ASTS runs);
- the drawdown breaker now sees real equity (non-zero `drawdown_pct` when underwater);
- the daily P&L report shows no fabricated figures.

### Step 4 — only then consider live

Promotion to live is still operator-gated and now has a **functional** gate (ar125,
settlement-derived `paper_outcomes_count` + `sharpe_95ci_lower`). Only a paper book
that proves green over a clean window should walk through it.

## Risk notes per flag

- `DURABLE_DRAWDOWN_BASELINE=1`: lowest risk — it makes a blind breaker see reality.
  Fail-CLOSED if NAV is unreadable (`durable_baseline_nav_unavailable`), never fail-open.
- `PER_POSITION_STOP=1`: default 8% is research-grounded (caps fat-tail blowups,
  leaves the small mean-reverting bleed names untouched). Risk = whipsaw if set too
  tight; do NOT go below ~5-6%. Stop is measured from entry, not trailing.
- `DELTA_NORMALIZER=1`: **must follow a reset** (Step 1) or the phantom-sell guard
  refuses the apply. This is the documented AAPL-12x / BA-6x accumulation fix.
- `ACCOUNT_LOCK=1`: only matters with `PORTFOLIO_CAPS=1` (already on). Closes the
  cross-symbol cap race. Low risk; advisory flock with a bounded wait.
