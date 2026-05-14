# 07 — Paper-book P&L attribution

**Date**: 2026-05-13
**Status**: research note, not an ADR
**Target**: v0.3 dogfood (paper-trade Sharpe verification, 4–8wk window)
**Authors**: subagent under ARIA deep-work-loop
**Cross-cuts**: ADR-0010 (settlement journal), ADR-0011 (portfolio reconstruction sign convention), ADR-0015 (executions.jsonl), ADR-0016 (autonomous + kill-switch)

## TL;DR

- **Realized P&L** = Σ over closed legs of `(exit_avg − entry_avg) × signed_qty − fees`. Already computed by `daemon/portfolio_loader.reconstruct_portfolio()` (full-close path only in v0.1.1; partial-close raises `NotImplementedError` until v0.1.2). **Reuse it; do not re-derive.**
- **Kill-switch** must use **dollar-weighted (money-weighted) cumulative return** on `equity_total` since autonomous-start, NOT time-weighted. Bank account experience > GIPS performance. The `reconstruct_portfolio().equity_total / initial_equity − 1` floor is the only number that matters at the gate.
- **Calibrator cadence** = on-settlement (Phase-B journal resolve), NOT per-fill. ADR-0010's two-phase loop already gives us the natural trigger; per-fill is noisy and conflates entry/exit. Aggregator's `learn_from_fill()` should be renamed `learn_from_settlement(SettlementEntry)` to match.

## Realized P&L formula (per-trade → cumulative)

Canonical lot-matching. For each fill in `executions.jsonl` filtered by `(account_id, asset_class)` (ADR-0011 partition key):

```
signed_qty   = +qty   if side=="buy" else −qty
notional     = signed_qty × fill_price
old_qty      = positions_qty[asset]
old_cost     = positions_cost[asset]    # running signed cost-basis sum

# Three cases (v0.1.2 must support all three; v0.1.1 supports 1 + 3 only):
# 1. Open / add (sign(old_qty) == sign(signed_qty), or old_qty == 0):
#       new_cost = old_cost + notional
#       realized = 0
# 2. Partial close (sign-flip-reduce, |signed_qty| < |old_qty|):
#       avg_old  = old_cost / old_qty
#       realized = (fill_price − avg_old) × (−signed_qty) × sign(old_qty)
#       new_qty  = old_qty + signed_qty
#       new_cost = avg_old × new_qty
# 3. Full close (new_qty == 0 within 1e-12):
#       avg_old  = old_cost / old_qty
#       realized = (fill_price − avg_old) × (−signed_qty) × sign(old_qty)
#       new_cost = 0
# 4. Direction flip (|signed_qty| > |old_qty|):
#       split into (full close of old_qty) + (open of remainder) — two passes.

cash               −= notional + fees
realized_pnl_total += realized          # already computed; this IS attribution
realized_fees_total += fees
```

This is exactly what `portfolio_loader.reconstruct_portfolio()` does, modulo the v0.1.1 partial-close gate. **Cumulative realized P&L is the file-replay invariant**: same `executions.jsonl` → same `realized_pnl_total`. Reproducibility test: `tests/integration/test_portfolio_replay_idempotent.py` (does not exist yet — P0 add).

## Unrealized P&L (mark-to-market)

For each open position after the fill replay:

```
avg_entry  = positions_cost[asset] / positions_qty[asset]
mark       = mark_prices[asset]                    # from latest bar close
unrealized = (mark − avg_entry) × positions_qty[asset]   # signed
equity_total = cash + Σ_assets (qty × mark)
```

`reconstruct_portfolio()` already returns this. Two gotchas:

- **Mark source.** Pass current bar close from the data provider; the loader's fallback (`positions_last_fill[asset]`) makes unrealized identically zero — fine for unit tests, **wrong for kill-switch**. Wire `mark_prices=ctx.last_close_for(symbols)` in `autonomous.tick()`.
- **Stale mark on halt.** Crypto = always-on; equities have overnight + halt. Use `bar.ts == asof_tick` else flag `mark_stale=True` and DO NOT trip kill-switch on stale unrealized (silence-by-default per AGENTS.md §1).

## Time-weighted vs dollar-weighted — which for kill-switch

| | TWR (time-weighted) | MWR / IRR (dollar-weighted) |
|---|---|---|
| What | Geometric link of period returns; cancels external cash flows | NPV-zero discount rate over the cash-flow stream |
| Use | GIPS reporting, manager-skill comparison | Investor's actual lived experience |
| Pro | Strategy-pure (cash-flow-invariant) | What your bank account does |
| Con | Hides timing of capital | Sensitive to deposit/withdrawal timing |

**Kill-switch picks dollar-weighted.** Specifically: `cumulative_pnl_pct = (equity_total − initial_equity) / initial_equity` since autonomous-start, where `initial_equity = the equity_total snapshotted on the autonomous-on transition`.

Reasoning:
1. Paper-book has **no external cash flows after t=0** → MWR ≡ TWR over the live window. So picking MWR costs nothing and matches operator intuition.
2. The kill-switch is a circuit-breaker, not a performance attribution. The question is "is the account down ≥X%?" not "is the strategy underperforming?" Those diverge if/when we add deposits; we're not adding deposits in paper.
3. TWR requires period-boundary chaining (`Π (1+r_t) − 1`) — extra state. Dollar-weighted is a single subtraction.

Sharpe / Sortino reporting (separate concern, daily snapshot) **does** want TWR — daily equity → daily return → annualized. Two numbers, two purposes; do not collapse.

## Per-trade journal-entry append schema (extension to ADR-0010)

ADR-0010 Phase-B already has `raw_return` (log return), `alpha_return` (vs benchmark), `hold_minutes`. Three fields missing for P&L attribution as a first-class source:

```python
class SettlementEntry(BaseModel):
    # ... existing Phase-A + Phase-B fields ...

    # ── Phase B P&L attribution (v0.3, additive) ────────────────────
    realized_pnl_quote: Optional[float] = None   # in quote ccy (USDT/USD), signed
    realized_fees_quote: Optional[float] = None  # ≥0, all-in (entry+exit fills)
    slippage_bps: Optional[float] = None         # |fill − decision| / decision × 1e4
    fill_count: Optional[int] = None             # n executions tied to this entry_id
    exit_reason: Optional[Literal["target","stop","timeout","reverse","operator"]] = None
```

Backwards-compatible: all `Optional[None]`. The journal renderer's summary line gains `[+$12.40 / 8bps slip / 247m]`. The reader (used by `get_recent_lessons` for v0.3 LLMAnalyst RAG) gets attribution data without a separate query.

Settlement loop computes these from `executions.jsonl` records joined on `signal_id == entry_id`. **ADR-0010 §9 still holds**: the journal is observability, never read back by the daemon's gate. Kill-switch reads `equity_total` from the loader, not the journal.

## Calibrator retrain cadence + reasoning

Three candidate cadences:

| Cadence | Sample rate | Noise | Compute | Verdict |
|---|---|---|---|---|
| Per-fill | every exec | 🔴 high — entry+exit conflated, partial fills double-count | trivial | ❌ |
| Per-settlement (Phase-B) | every closed trade | 🟡 medium — clean entry→exit lot, signed PnL is the label | trivial | ✅ **pick** |
| Daily/weekly | EoD/EoW | 🟢 low — but loses sample-efficiency in low-volume paper window | trivial | ❌ for v0.3 |

**Pick: per-settlement.** Reasoning grounded in ADR-0010:

- ADR-0010 §Lifecycle Phase B already fires once per closed trade with `(direction, raw_return, alpha_return, reflection)` — that's the exact `(prediction, outcome)` pair the calibrator wants. No new trigger needed.
- ADR-0009 §P1-12 sets `n_min_observations=200` for analyst stat → calibrated weights. Per-fill triggers blow through this in days but on noisy labels (fee-only flips, partial fills); per-settlement keeps the label clean.
- 4–8wk paper window × ~5 closed trades/day × 5 symbols = 100–800 settlements. Enough for ColdStart→Isotonic crossover at 200, **only if** label is per-trade not per-fill.

Implementation: rename `BMAAggregator.learn_from_fill(fill)` → `learn_from_settlement(entry: SettlementEntry)`. Hook it from `daemon/settlement_loop.py` immediately after `journal.resolve(...)`. **One write site, deterministic, replayable.**

## Cribbed code (numpy/pandas, ~30 lines)

We reject vectorbt/backtrader/freqtrade backtester as runtime deps — too OOP, too much hidden state. We crib **two concepts**, written ourselves:

- **vectorbt** `Portfolio.from_orders()` — the *idea* of pure-array fill replay. `qty[]` and `price[]` arrays in, equity curve out. We already do this in `portfolio_loader`; call out the pattern.
- **freqtrade** `optimize/backtesting.py::Backtesting._enter_trade` lot-matching. Defensive: it explicitly rejects partial fills mid-leg (matches ADR-0011 partition discipline).

```python
# hermes_quant/daemon/pnl_attribution.py — to add (P0)
from __future__ import annotations
import numpy as np, pandas as pd

def equity_curve(execs: pd.DataFrame, initial_cash: float,
                 marks: pd.DataFrame) -> pd.DataFrame:
    """execs: cols [asof, asset, side, qty, fill_price, fees].
       marks: cols [asof, asset, close]. Returns equity time-series."""
    e = execs.sort_values("asof").copy()
    e["signed_qty"] = np.where(e.side == "buy", e.qty, -e.qty)
    e["notional"]   = e.signed_qty * e.fill_price
    e["cash_delta"] = -(e.notional + e.fees)
    # cumulative cash + position arrays per asset
    cash = initial_cash + e.cash_delta.cumsum()
    pos  = e.groupby("asset")["signed_qty"].cumsum()
    # snapshot at every mark timestamp (left-join nearest backward)
    snap = marks.merge(
        e[["asof","asset","signed_qty"]].assign(pos=pos).groupby("asset").last(),
        on="asset", how="left").fillna(0)
    snap["mtm"] = snap["pos"] * snap["close"]
    eq = snap.groupby("asof")["mtm"].sum() + cash.iloc[-1]
    return eq.to_frame("equity").assign(
        ret=lambda d: d.equity.pct_change().fillna(0),
        cum_ret=lambda d: d.equity / initial_cash - 1.0,   # ← kill-switch input
        drawdown=lambda d: d.equity / d.equity.cummax() - 1.0,
    )

def sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    r = returns.dropna()
    return float(np.sqrt(periods_per_year) * r.mean() / (r.std(ddof=1) + 1e-12))
```

Replayable: same `executions.jsonl` + same `marks` parquet → byte-identical `equity` series. CI gate: `tests/test_pnl_replay_byte_identical.py` (P0).

## Risks

- **Slippage attribution.** `decision_price` (signal-time) vs `fill_price` (broker-reported) is already in `ExecutionRecord`. Paper-book defaults them equal — so paper Sharpe is **upward-biased** vs live by ~5–15bps/trade depending on venue. Document: paper-Sharpe is a **necessary** but not sufficient bar; live promotion must show ≥0.5 Sharpe NET of a configured `paper_slippage_bps` (default 5).
- **Overnight gaps (equities only).** Mark = last close, but the next bar's open can gap ±5%. Kill-switch reading unrealized at 09:30 ET on a gap-down can fire before any human can intervene. Mitigation: `kill_switch_pct` evaluates on **realized + unrealized**, but `trip_kill_switch()` requires **two consecutive ticks** below threshold (debounce). Crypto: not applicable.
- **Halt periods.** Equities can halt mid-session; mark goes stale. Per §Unrealized above: flag `mark_stale=True`, do not trip on stale. For paper this is academic (yfinance returns last valid close); for live it's a real failure mode and must be explicit before the first live cent.
- **Multi-leg trades.** Direction-flip fills are two attribution events, not one. Don't lose the close-leg P&L into the open-leg cost basis (the v0.1.1 bug ADR-0011 gates off). Test fixture: BTC long 0.1 → short 0.1 (flip via 0.2 sell) must produce two journal entries or one entry with two `realized_pnl_quote` lots; pick one and write the test first.
- **Fee model drift.** `fees` field is broker-truth in live, but in paper we *compute* it from a fee schedule. If the schedule diverges from venue reality, paper Sharpe is biased. Pin the fee schedule per `account_id` in `~/.hermes/quant/state.json::fees_schema` and version it (ADR amendment if changed mid-run).

## P0 / P1 implementation order for v0.3

**P0 (blocks dogfood — cannot paper-trade 4wk without these):**
1. `daemon/pnl_attribution.equity_curve()` + `sharpe()` (the 30 lines above).
2. Wire `autonomous.tick()` → `reconstruct_portfolio(mark_prices=…)` → real `cumulative_pnl_pct` (replace stub).
3. Two-tick debounce on `trip_kill_switch()`.
4. Land `portfolio_loader` v0.1.2 partial-close + flip cases (already gated; unblocks).
5. CI gate: `test_pnl_replay_byte_identical` over a recorded `executions.jsonl` fixture.

**P1 (needed for V03-5 calibrator loop, not for paper-trade itself):**
6. Add `realized_pnl_quote / realized_fees_quote / slippage_bps / fill_count / exit_reason` to `SettlementEntry` (additive, optional).
7. Settlement-loop join: match `executions.jsonl` records on `signal_id` to fill these fields at `journal.resolve()` time.
8. Rename `BMAAggregator.learn_from_fill` → `learn_from_settlement(SettlementEntry)`; hook from settlement loop.
9. Daily snapshot writer (`~/.hermes/quant/equity_daily.parquet`) for TWR Sharpe reporting — separate from kill-switch dollar-weighted path.

**Out of scope for v0.3** (call out so they don't sneak in):
- Vectorbt/backtrader as runtime deps. Crib concepts; do not import.
- DSR (Deflated Sharpe) — placeholder only per ADR-0006; revisit at v0.2 RL graduation.
- LLM-driven reflection over P&L lots — ADR-0012 territory.
