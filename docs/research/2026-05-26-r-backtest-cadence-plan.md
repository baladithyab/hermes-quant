# Backtest + Cadence Plan for the 5-Play Playbook

**Date:** 2026-05-26
**Author:** subagent (research phase)
**Scope:** What it takes to backtest *and* run the 5 plays (covered_call, csp, wheel, leaps, swing) on **daily / weekly / quarterly** cadences, given:

- Existing walk-forward backtester (`hermes_quant/backtest/walk_forward.py`, `replay.py`)
- Newly vendored option-pricing kernel (`hermes_quant/options/pricing/gbs.py` + `greeks.py`)
- Evolving watchlist (`hermes_quant/playbook/watchlist_evolution.py`) and per-play scorers (`hermes_quant/playbook/scorers.py`, `profiles.py`)
- ADRs already in flight: 0027 (options-aware risk gate), 0028 (options data layer), 0029 (multi-leg paper reactor), 0030 (daily picker recipe), 0032 (trading-flow contract), 0033 (evidence store)

The user has explicitly redirected away from intraday: the system is *daily decisions + weekly maintenance + quarterly review*.

---

## 1. What the existing backtest infrastructure already does

`replay.py` walks an OHLCV `DataFrame` chronologically, calls `advisor.recommend()` at each bar with an `as_of` cutoff, applies the resulting Action through `PaperPortfolio.apply_target()` (single-symbol, signed % of NAV), and accumulates:

- per-bar equity curve + buy-and-hold baseline
- Sharpe, deflated Sharpe (PSR n=1), max drawdown
- pending-settlement queue that feeds episode outcomes back into the BMA aggregator

`walk_forward.py` wraps that in `PurgedWalkForward` (ADR-0019) and runs an independent `replay()` per out-of-sample fold, then aggregates excess return / Sharpe delta / positive-fold rate.

**Public API today**: `walk_forward_replay(bars, *, symbol, asset_class, timeframe, n_splits, ...) → WalkForwardBacktestResult`. `replay(bars, *, symbol, asset_class, timeframe, ...) → BacktestResult`.

This is **single-symbol, single-instrument, equity-only**. Position is one signed scalar in % of NAV. There is no notion of an option leg, an expiration, or a position-tag.

## 2. Gap analysis: backtesting the 5 plays

| Play | Mechanic | What backtester needs that doesn't exist |
|---|---|---|
| **covered_call** | Long 100 shares + short 1 OTM call, 21–36 DTE, hold to expiry or roll. P&L = premium + min(spot, strike) − entry_spot. | (a) per-symbol option entry/exit at a synthetic strike on a date; (b) ability to hold *both* underlying and a short call leg; (c) closed-form premium price using GBS kernel + realized-vol-30d as σ; (d) settlement at expiration (assignment / OTM expiry). |
| **csp** | Cash-secured short put, 21–45 DTE, 0.20–0.30 delta. P&L = premium − max(0, strike − spot). | Same as CC but with cash collateral, no underlying leg. Assignment converts the play into long-shares (start of wheel cycle). |
| **wheel** | Stitched CSP → assigned shares → CC → called away → CSP. State machine over option cycles. | A position-state-machine on top of CSP/CC backtests. Needs play-tag persistence across cycles. |
| **leaps** | Long deep-ITM call (delta ≈ 0.80), 12–18 month DTE, hold ≥6m. | Single-leg long-call valuation across many months. Cleanest path: replicate via delta-weighted underlying exposure (delta × spot) with theta drag → falls back to existing equity replay with a delta multiplier. Closed-form GBS gives exact reprice each bar. |
| **swing** | 30–90 DTE directional underlying, profit-target / stop. No options. | This is **already** representable with `replay()` today — it's just an equity backtest with a longer hold and a different exit rule. Only gap: stop/target exit logic (current actuator is target-pct-only). |

**The cross-cutting gap: option mark-to-market without historical chain data.** Alpaca explicitly does not provide historical option chain snapshots in any tier (per ADR-0028 §4 and `docs/research/2026-05-23-r1-alpaca-options-api.md`). yfinance has the same gap. Paid providers (Polygon, ORATS, ThetaData) cost. The vendored optlib kernel is the bridge: at every bar we **synthesize** the option price closed-form from `(spot, strike, dte, σ_realized, r)`, no chain history needed. This is the v0.5 path; an ORATS pull stays NICE-TO-HAVE.

### Concrete extensions needed in `backtest/`

1. **`OptionLegPortfolio`** alongside `PaperPortfolio` — tracks `(occ_symbol, side, qty, strike, expiration, entry_premium)` plus the underlying share leg for CC / wheel. NAV mark = cash + shares × spot + Σ(leg_qty × theoretical_price). MUST-HAVE.
2. **`option_replay()`** — variant of `replay()` that, when the advisor's Action is an option proposal (not a signed-pct equity target), opens/closes a leg via the GBS kernel. Reuses the existing equity warmup, settlement-feedback, and walk-forward composition. MUST-HAVE.
3. **Synthetic chain at each bar** — given the realized-vol-30d already computed by `compute_play_snapshot()`, build a sparse local chain (5 strikes × 3 expirations) on demand using GBS. No I/O. MUST-HAVE.
4. **Expiration handler** — at each bar, scan open legs for `expiration <= as_of`, settle to either `expiry_otm`, `expiry_itm`, or `assignment` per ADR-0029 §D3 outcome types. MUST-HAVE.
5. **Stop/profit-target actuator** — extend `PaperPortfolio.apply_target` (or wrap it) to honour `(stop_pct, target_pct, max_dte)` exits for swing. NICE-TO-HAVE for daily-cron MVP (the gate already silences losing positions on fresh signals).
6. **Multi-symbol portfolio** — currently `replay()` is single-symbol. The 5 plays operate on a watchlist of ~30 symbols simultaneously. Either run `replay()` per symbol and aggregate (cheap) or build a `MultiSymbolPaperPortfolio` (correct). v0.5 = per-symbol replays composed; v0.6 = multi-symbol portfolio. MUST-HAVE (per-symbol composition); FUTURE (multi-symbol unified).

## 3. Cadence schedule (PT cron expressions, host TZ)

Cron infrastructure already exists via `hermes cron create <cadence> --script ...` (see `hermes_quant/cli/__init__.py:_create_autonomous_cron_job`). We piggy-back on it.

| Cadence | When (PT) | When (ET) | Crontab | Owner script |
|---|---|---|---|---|
| Universe scan | 03:30 PT Mon–Fri | 06:30 ET | `30 3 * * 1-5` | `hermes quant universe scan` |
| Watchlist evolution | 04:00 PT Mon–Fri | 07:00 ET | `0 4 * * 1-5` | `hermes quant watchlist evolve` (chains off universe) |
| **Daily decision** (premarket) | 06:00 PT Mon–Fri | 09:00 ET (1 hr before open) | `0 6 * * 1-5` | `hermes quant playbook tick --plays all` |
| **Weekly rebalance** | 06:30 PT Mon | 09:30 ET | `30 6 * * 1` | `hermes quant playbook weekly` |
| **Quarterly review** | 06:30 PT first Mon Jan/Apr/Jul/Oct | 09:30 ET | `30 6 1-7 1,4,7,10 1` | `hermes quant playbook quarterly` |
| NTA reconciliation | 06:00 PT Tue–Sat | 09:00 ET | `0 6 * * 2-6` | `hermes quant settle reconcile-options` (ADR-0029 §D3) |

The first-Monday-of-quarter expression (`30 6 1-7 1,4,7,10 1`) is standard cron idiom: month-of-year in {Jan, Apr, Jul, Oct}, day-of-month in 1..7, day-of-week=Monday.

## 4. Data flows by cadence

### 4.1 Daily decision (06:00 PT, premarket)

```
universe (alpaca-daily.json)
  → watchlist_evolution.evolve_watchlist()  [04:00 cron, already exists]
       └── per-play active list (~5–10 symbols × 5 plays)
  → for each (symbol, play) in active list:
       compute_play_snapshot(symbol)  [yfinance, exists]
       → score_<play>(snapshot)        [exists]
       → if eligible AND no existing position for that play:
            advisor.recommend(symbol, recipe=play)
            → silence_bias_gate
            → if pass: build_proposal(symbol, play, leg_spec)
            → fire (paper, mleg)        [ADR-0029 reactor — IN-FLIGHT]
       → if existing position for that play AND signal flips: queue exit
  → write executions.jsonl, NTA-aware journal
```

The watchlist evolution already runs separately. The new piece is the playbook tick that converts (symbol, play, snapshot) into a leg specification and fires it through the multi-leg reactor.

### 4.2 Weekly rebalance (06:30 PT Mon)

```
read open positions (portfolio_loader.reconstruct_portfolio)
  → for each option leg:
       if expiration ≤ today + 5 trading days:
          decide: roll (sell-to-close + open new far-dated leg) | let-expire | close
       compute new theoretical from GBS kernel; if mid-price moved >2σ from entry → propose adjustment
  → for each swing position:
       if days_held > 60 OR p&l < −2*ATR → close
       if p&l > +3*ATR → take profit
  → fire batch through HITL queue (low urgency, not silence-gated since these are MAINTENANCE actions)
```

### 4.3 Quarterly review (06:30 PT first Mon of Jan/Apr/Jul/Oct)

```
read all positions
  → portfolio summary: NAV, sector breakdown, beta-weighted delta, theta/day, vega/$ NAV
  → factor exposure check:
       sector concentration > 30% → flag
       portfolio beta > 1.5 or < 0.5 → flag
       net delta > 0.6 × NAV → flag (overweight directional)
  → emit quarterly markdown report (reuse to_markdown_report shape)
  → propose rebalance batch (close-overweight, scale-underweight)
```

## 5. Missing pieces (gap analysis)

| # | Gap | Cadence affected | Status | Tag |
|---|---|---|---|---|
| 1 | `OptionLegPortfolio` + multi-symbol composition in `backtest/portfolio.py` | backtest | not started | **MUST** |
| 2 | `option_replay()` with GBS-priced legs + expiration handler | backtest | not started | **MUST** |
| 3 | Position schema with `play_tag` field (`covered_call` / `csp` / `wheel` / `leaps` / `swing`) | daily, weekly | partial (proposals carry recipe; positions don't) | **MUST** |
| 4 | Roll detection: `for leg in open: if (leg.expiration - today).days ≤ 5: …` | weekly | not started | **MUST** |
| 5 | Multi-leg paper reactor wired through HITL queue + `mleg` order shape | daily | ADR-0029 in-flight, not landed | **MUST** |
| 6 | Options-aware risk gate (defined-risk, max_loss, net_delta) | daily | ADR-0027 in-flight | **MUST** |
| 7 | Synthetic-chain helper (`synthesize_chain(symbol, asof, σ_30d)`) using GBS | backtest, daily-fallback | not started, gbs.py vendored | **MUST** |
| 8 | Factor-exposure calculator (sector, beta, net delta, gross theta) | quarterly | not started | NICE-TO-HAVE |
| 9 | Stop / profit-target actuator | weekly | not started; weekly maintenance can compensate | NICE-TO-HAVE |
| 10 | Real historical option chains (Polygon / ORATS) for true OOS option backtests | backtest | not started; closed-form is the v0.5 path | FUTURE |
| 11 | Cron-script wrappers `hermes quant playbook {tick,weekly,quarterly}` | all three | scaffolding exists for autonomous tick; need the 3 new commands | **MUST** |
| 12 | NTA reconciliation already specified in ADR-0029 §D3 | daily | not landed | **MUST** |
| 13 | `walk_forward_replay` that supports the synthetic option chain (basically calls `option_replay` instead of `replay`) | backtest | trivial wrapper after #1 + #2 | **MUST** |

## 6. Risks of daily-only firing

1. **Gap risk.** Decision at 06:00 PT uses prior-day close. A 4% overnight gap on earnings or macro news invalidates the signal before the order fills at 06:30 PT open. **Mitigation**: snapshot computed at 06:00 PT must re-fetch the most recent post-market quote (Alpaca paper API can supply a 4 AM-5 AM PT pre-market last). If gap > 1.5 × ATR-14, silence the proposal. Hard rule, MUST-HAVE.
2. **Limit-on-open rejection.** Paper order with stale limit price can sit unfilled all day. **Mitigation**: use `time_in_force: day`, `type: limit`, limit price = mid + slippage budget (50 bps). If unfilled by 10:00 PT (13:00 ET), cancel + resubmit at fresh mid. NICE-TO-HAVE.
3. **Earnings-day surprise.** `days_since_earnings >= 5` hard rule already covers backward window. Need a `days_until_earnings >= 5` mirror to avoid opening a CC the day before earnings. MUST-HAVE; trivial extension to scorer (yfinance `tk.calendar` already pulled).
4. **Option chain liquidity.** Synthetic GBS pricing won't fail, but live fills can. `bid_ask_spread / mid > 0.10` → silence the proposal. Defined in ADR-0028; MUST-HAVE.
5. **Stale realized-vol.** Backtest uses GBS with σ = `realized_vol_30d`. In a vol regime change (Mar 2020, Aug 2024) realized-vol lags implied vol by weeks → backtest *over-states* premium. Document explicitly in the report; consider a 30/60/90 vol cone in v0.6. NICE-TO-HAVE caveat.
6. **Settlement-day discrepancy.** Paper assignment NTAs surface T+1; weekly script must reconcile *yesterday's* expirations before today's roll decisions. Sequence the 06:00 NTA-reconcile cron *before* the 06:30 weekly cron. MUST-HAVE; sequencing is the cron design, not new code.
7. **Calibrator poisoning from option fills.** The BMA aggregator's `learn_from_fills` loop today expects scalar returns; option outcomes are bimodal (max premium / large loss). Settlement horizon must use `realized_pnl / max_loss` ratio not raw return. MUST-HAVE adjustment to `_settle_episode` in replay.

## 7. Tagged work breakdown for tomorrow's architect phase

**MUST-HAVE for v0.5 paper-trade activation:**

- ADR draft for **`option_replay`** (extend ADR-0020, gate on ADR-0028+0029 landing).
- ADR draft for **position-tag schema** (`play_tag: Literal["covered_call","csp","wheel","leaps","swing"]` on `Proposal`, `Execution`, and `Position`).
- ADR draft for **cadence cron writer** (`hermes quant playbook install-crons`) with the 5 expressions above.
- Implement `synthesize_chain()` helper in `hermes_quant/options/synthetic_chain.py`.
- Implement `OptionLegPortfolio` + `option_replay()`.
- Wire `playbook tick` / `weekly` / `quarterly` CLI subcommands.
- Add `days_until_earnings` to `compute_play_snapshot` + hard rule.
- Add overnight-gap silence rule.
- Adjust `_settle_episode` for option-bimodal outcomes.

**NICE-TO-HAVE:**

- Stop / profit-target actuator.
- Factor-exposure calculator.
- Limit-resubmit logic at 10:00 PT.
- Vol-cone caveat in backtest report.

**FUTURE:**

- Polygon / ORATS historical chain integration for real OOS option backtests.
- Multi-symbol unified `PaperPortfolio` (replace per-symbol composition).
- True intraday tick reactor (explicitly de-scoped per user direction).

---

## Appendix A — Why closed-form GBS is acceptable for backtest in v0.5

The user's playbook focuses on **vanilla single-leg or two-leg defined-risk structures** (CC, CSP, LEAPS, swing-as-equity). For these:

- **Pricing error** between closed-form GBS (with σ = realized-vol-30d) and observed market premium is empirically 5–15% on liquid names (SPY/QQQ/AAPL); larger on illiquid names. Within the same magnitude as the bid-ask spread we'd cross live.
- **Path realism** is preserved: spot follows the actual historical path, σ is the actual historical realized vol — only the IV-vs-RV wedge is missing. That wedge biases premium *down* (RV ≤ IV typically), which makes the backtest **conservative** for short-premium plays (CC, CSP, wheel) — exactly the safety direction we want.
- For LEAPS, GBS reprices each bar exactly, so theta decay and delta drift across 12 months are accurate given σ.
- For swing, no options at all — GBS isn't even invoked.

This justifies skipping the Polygon/ORATS dependency for v0.5 paper-trade activation. The closed-form path becomes the *primary* backtest infrastructure, and a real-chain backtest is a v0.6 OOS validation gate.

## Appendix B — File-level deliverables anticipated for the architect phase

```
hermes_quant/
  options/
    synthetic_chain.py        # NEW — GBS-priced sparse chain at any (symbol, asof)
    multileg.py               # NEW — StrategyBuilder per ADR-0029 §D2
  backtest/
    option_portfolio.py       # NEW — OptionLegPortfolio
    option_replay.py          # NEW — option-aware replay()
    walk_forward_options.py   # NEW — wraps walk_forward_replay around option_replay
  playbook/
    tick.py                   # NEW — daily decision orchestrator
    weekly.py                 # NEW — weekly rebalance orchestrator
    quarterly.py              # NEW — quarterly review orchestrator
  cli/
    playbook.py               # NEW — CLI subcommands + cron installer
docs/adr/
  ADR-0034-option-replay-and-synthetic-chain.md        # NEW
  ADR-0035-position-tag-and-play-state-machine.md      # NEW
  ADR-0036-playbook-cadence-cron-schedule.md           # NEW
```

End of plan.
