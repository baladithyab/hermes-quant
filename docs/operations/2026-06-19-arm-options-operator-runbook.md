# Operator runbook — arm AEGIS options (origination + protective rails)

**Date:** 2026-06-19 · **Decision:** operator chose "everything options ON, including origination" (explicit ADR-0029 evidence-before-live override; paper book). This runbook gives the exact, ordered arming steps. The agent produces these commands; **the operator runs them** — `~/.hermes/scripts/` and the cron registry are operator-owned and tool-guarded.

## Why an order (safety)

Arming origination (`AUTONOMOUS_OPTIONS`) makes the tick **open** options positions. A position must never exist without its protective stop/TP rails, and an options track record is worthless without its audit trail. So arm in this order — **protective + audit first, origination last**:

```
jw1 (audit trail)  →  protective rails  →  mark source  →  origination
   [already in code]    [OPTIONS_MONITOR…]   [LIVE_CHAIN+cron]  [AUTONOMOUS_OPTIONS]
```

The source-code defaults stay OFF (CI/backtest/eval must run clean) — arming is **only** the wrapper export below. Nothing is armed by merging code.

## Prerequisites (already landed in code this session — no action needed)

- `jw1` — options fires now write their `SettlementEntry` audit entry (was silently lost). **This is what makes the armed options track record actually accrue evidence.** ✅ committed.
- `agmon1`/`agmon2` — options stop-loss + take-profit sweeps (protective). ✅
- `ml00b` — composite rows + legs persisted at origination (so the sweeps can mark). ✅
- `agperc3` + `aegis-chain-prefetch.py` — live-chain fetch body + the prefetch script. ✅ (script built; needs creds+cron to run)
- `aegis-gate2-eval.py` (bf76b) — the GATE-2 evidence-unlock writer. ✅ (script built; needs cron)

## Step 1 — confirm creds present (live chain needs them)

```bash
# Alpaca paper creds must be in the env the armed wrapper sources:
grep -q APCA_API_KEY_ID ~/.hermes/secrets/alpaca.env && echo "creds present" || echo "MISSING — add APCA_API_KEY_ID / APCA_API_SECRET_KEY"
```

## Step 2 — add the arming exports to the armed wrapper

Append to `~/.hermes/scripts/quant-autonomous-tick-armed.sh` (it already exports `PER_POSITION_STOP`, `TAKE_PROFIT_SWEEP`, `PORTFOLIO_CAPS`, `SLIPPAGE_HAIRCUT`):

```bash
# --- AEGIS options: protective rails (act on existing positions) ---
export HERMES_QUANT_OPTIONS_MONITOR=1            # agmon1/agmon2 options SL + TP sweeps
export HERMES_QUANT_COMPOSITE_LEG_OPS=1          # ml01b decompose/convert leg management
export HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING=1  # ag01b correlated-basket de-lever
export HERMES_QUANT_MULTILEG_REACTOR=1           # the broker MLEG order path (execution)

# --- AEGIS options: live chain marks (perceive + the sweeps read these parquets) ---
export HERMES_QUANT_OPTIONS_LIVE_CHAIN=1         # enables fetch_chain_live (needs creds, above)

# --- AEGIS options: origination chain (decide to OPEN options) ---
export HERMES_QUANT_OPTIONS_PERCEIVE=1           # iv_rank into the perception frame
export HERMES_QUANT_STRUCTURE_SELECT=1           # stance x IV-regime -> structure
export HERMES_QUANT_OPTIONS_GATE=1               # the options risk gate (BP floor etc.)
export HERMES_QUANT_AUTONOMOUS_OPTIONS=1         # MASTER origination switch — arm LAST

# Optional: keep the evidence gate ON so origination still requires GATE-2 cleared.
# Leave UNSET to let origination fire immediately (your "arm everything" choice);
# SET to require the aegis-gate2-eval marker first (the more conservative path):
# export HERMES_QUANT_OPTIONS_EVIDENCE_GATE=1
```

> Note: with `OPTIONS_EVIDENCE_GATE` **unset** (your choice), origination fires as soon as `AUTONOMOUS_OPTIONS=1` + a usable IV rank + an admissible structure. The `bf76b` GATE-2 marker then becomes an *observability* signal rather than a hard pre-gate.

## Step 3 — register the two new cron scripts

```bash
# Daily chain prefetch (populates option_chains/*.parquet for perceive + the sweeps).
# Run BEFORE the tick each session:
#   ~/.hermes/hermes-agent/venv/bin/python3 <repo>/ops/scripts/aegis-chain-prefetch.py

# GATE-2 evidence eval (writes options_unlock.json; only matters if you set
# OPTIONS_EVIDENCE_GATE=1). Run daily/weekly over the settled book:
#   ~/.hermes/hermes-agent/venv/bin/python3 <repo>/ops/scripts/aegis-gate2-eval.py
```

Register both in the Hermes cron registry (`~/.hermes/cron/cron.db` / jobs.json) the same way the other `quant-*` crons are registered. (Deploy the scripts to `~/.hermes/scripts/` first if your crons run by basename from there.)

## Step 4 — deploy the residual ar115 scripts (independent, owed)

```bash
# 3 scripts drift live-vs-source (6/9 already deployed). Redeploy:
for s in aegis-gate0-start.py aegis-run-snapshot.py quant-watchlist-evolve.py; do
  cp <repo>/ops/scripts/$s ~/.hermes/scripts/$s
done
```

## Step 5 — start the clean window + watch

```bash
# Stamp the GATE-0 t0 anchor AFTER sourcing the armed wrapper (records the armed snapshot):
source ~/.hermes/scripts/quant-autonomous-tick-armed.sh
~/.hermes/hermes-agent/venv/bin/python3 <repo>/ops/scripts/aegis-gate0-start.py
```

Then let the armed crons run. Track with `aegis-run-snapshot.py` and watch:
- `proposals.jsonl` / `executions.jsonl` for the first **options** fires (asset_class `multi_leg`/`us_option`).
- The journal — each options fire now writes a `SettlementEntry` (jw1). If you see options fires with **no** journal entry, stop and page the agent (the jw1 fix regressed).
- The 30-day options-evidence window (`agoptev1`): N≥30 settled multi-leg outcomes → run `aegis-gate2-eval.py` → the GATE-2 verdict.

## Rollback

Every flag is independent and default-OFF in source. To disarm: remove the `export` lines from the wrapper (or set `=0`) and restart the cron. No code change, no redeploy. The next tick is byte-identical to pre-arming.
