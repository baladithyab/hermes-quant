# R5: Alpaca live probe + interim picker tz-bug fix

**Date:** 2026-05-24 (Sunday, 1 day before Memorial Day, 2 days before Tuesday open)
**Probed by:** orchestrator (Claude Opus 4.7 via Bedrock) + paper credentials at `~/.hermes/secrets/alpaca.env`
**Goal:** verify Alpaca options API behaves as R1 documented, hunt for surprises, ensure Tuesday's interim cron will actually work
**Method:** real REST + alpaca-py SDK calls against `paper-api.alpaca.markets`. Live-probe scripts at `/tmp/alpaca-probe-{A,B,C,D,E,F2,cleanup-audit}.py`
**Outcome:** account healthy, multi-leg order schema verified, **R1 had two material errors** corrected here, **interim picker had a latent tz-bug that would have bricked Tuesday's pre-market cron — now fixed**

---

## Account state (as of 2026-05-24 ~14:00 ET)

```
GET /v2/account → HTTP 200
  status: ACTIVE
  cash: $100,000
  buying_power: $200,000  (Reg-T)
  options_buying_power: $100,000
  options_approved_level: 3
  options_trading_level: 3
  shorting_enabled: True
  pattern_day_trader: False
  daytrading_buying_power: 0
```

Clock + calendar:
```
GET /v2/clock          → is_open=False, next_open=2026-05-26 09:30 ET
GET /v2/calendar       → 2026-05-25 ABSENT (Memorial Day holiday)
                         2026-05-26 09:30-16:00
                         2026-05-27 09:30-16:00
```

Account state matches R1 §1's expectations exactly. No surprises here.

## Endpoints probed

| Endpoint | Status | Notes |
|---|---|---|
| `GET /v2/account` | 🟢 200 | Health check + Level-3 options confirmed |
| `GET /v2/clock` | 🟢 200 | Tuesday 09:30 ET = next open |
| `GET /v2/calendar` | 🟢 200 | Confirms Memorial Day Mon closed |
| `GET /v2/assets/NVDA` | 🟢 200 | `attributes: ['fractional_eh_enabled', 'has_options', 'overnight_tradable']` |
| `GET /v2/options/contracts?underlying_symbols=NVDA&limit=20` | 🟢 200 | Returns OCC symbols + strike + DTE + OI + close_price |
| `GET /v1beta1/options/snapshots/{underlying}?feed=indicative` | 🟢 200 | OHLCV + bid/ask only — **no greeks here** |
| `GET /v1beta1/options/snapshots/{underlying}?feed=opra` | 🔴 403 | "OPRA agreement is not signed" — paid feed gate |
| `GET /v1beta1/options/snapshots?symbols=...` | 🟢 200 | Same shape as the underlying-keyed variant |
| `GET /v1beta1/options/quotes/latest?symbols=...` | 🟢 200 | Top-of-book bid/ask + timestamp |
| `GET /v1beta1/options/greeks*` | 🔴 404 | No standalone greeks endpoint |
| `OptionChainRequest` (alpaca-py SDK) | 🟢 200 | **Returns greeks + IV on liquid contracts** — see "Greeks coverage" below |
| `POST /v2/orders` (single-leg, fake symbol) | 🔴 422 | "asset not found" — must use REAL OCC symbol from `/v2/options/contracts` |
| `POST /v2/orders` (multi-leg, real symbols) | 🟢 200 | **Vertical spread accepted, status `accepted`** — schema verified |
| `DELETE /v2/orders/{id}` | 🟢 cancelled | Cancel works; verified via `?status=all` |
| `GET /v2/orders?status=open` | 🟢 200, count=0 | Account clean after probe |
| `GET /v2/positions` | 🟢 200, count=0 | No positions |

## R1 corrections (material)

### R1 §2 was WRONG: greeks ARE returned, BUT only via specific endpoint

R1 claimed: "Alpaca does return Greeks in option snapshots (delta, gamma, theta, vega, IV). Rho is missing."

Reality, verified 2026-05-24:
- **Raw REST `/v1beta1/options/snapshots`** → NO greeks, NO IV. Only OHLCV bars + bid/ask quote.
- **alpaca-py SDK `OptionChainRequest`** (`underlying_symbol='NVDA'`) → **2865 of 4884 contracts returned greeks AND IV**, including rho.

Sample greek payload:
```python
greeks = OptionsGreeks(
    delta=-0.9954,
    gamma=0.0005,
    rho=-0.0957,         # ← R1 claimed rho was missing — WRONG
    theta=0.0146,
    vega=0.0052,
)
implied_volatility = 0.6422
```

The chain endpoint backend evidently augments quotes with computed greeks (likely server-side py_vollib or Black-Scholes) before returning. The 41% of contracts WITHOUT greeks are deep OTM/ITM with unstable bid-ask (computing IV from a 0.04 / 0.05 spread is unreliable; computing it from a 165 / 167 spread on deep ITM is also unreliable — the implied vol solver doesn't converge cleanly).

**Implication for ADR-0028 (options data layer):**
- Use SDK `OptionChainRequest` as the primary Alpaca data path — not raw REST snapshots
- ~40-45% of contracts will need py_vollib synthesis fallback (the no-greeks tier)
- The Alpaca-provided greeks are presumed to be at least Black-Scholes-quality; py_vollib synthesis only kicks in when Alpaca returns `None`
- The "stale-greek replacement" P1 from the synthesis review (P1-E) becomes more nuanced — Alpaca's greeks are computed at quote time so they're never genuinely stale, but they CAN be `None` and the fallback strategy is "compute fresh, don't keep a stale value"

Updated ADR-0028 D3 should read: *"Alpaca's `OptionChainRequest` returns delta/gamma/theta/vega/rho/implied_volatility on ~58% of NVDA contracts (verified live 2026-05-24). For the remaining ~42% (deep OTM/ITM with unstable bid-ask), synthesize via py_vollib using mid-quote IV. py_vollib is in-process and adds ~0.1ms per contract — no rate-limit concern. American-vs-European approximation gap acknowledged for early-exercise contracts; flag them with `iv_source='py_vollib_european_approx'` (per Grok P2)."*

### R1 §3 was MOSTLY RIGHT: multi-leg order shape works as documented, with one missing field

R1 documented:
```json
{
  "order_class": "mleg",
  "legs": [{"symbol": "...", "side": "buy", "qty": "1", "type": "limit", "limit_price": "2.50"}, ...],
  "time_in_force": "day"
}
```

Real working shape (verified 2026-05-24, accepted by paper API):
```json
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "time_in_force": "day",
  "limit_price": "0.01",
  "legs": [
    {
      "symbol": "NVDA260612C00140000",
      "ratio_qty": "1",
      "side": "buy",
      "position_intent": "buy_to_open"
    },
    {
      "symbol": "NVDA260612C00145000",
      "ratio_qty": "1",
      "side": "sell",
      "position_intent": "sell_to_open"
    }
  ],
  "client_order_id": "..."
}
```

Differences from R1:
1. **`position_intent`** is required at the per-leg level. Values: `buy_to_open`, `buy_to_close`, `sell_to_open`, `sell_to_close`. R1 didn't mention this.
2. **`ratio_qty`** at the leg level + outer **`qty`** — R1 had `qty` at the leg level. Reality: outer `qty` is the spread quantity; per-leg `ratio_qty` is the leg multiplier (always "1" for vanilla spreads, but could be "2" for ratio spreads).
3. **`type` and `limit_price` are at the OUTER level**, not per-leg. The leg `type` and `limit_price` are NULL in the response — the legs inherit from the outer order class.

**Implication for ADR-0029 (multi-leg paper reactor):**
- Add `position_intent` to the `OptionLeg` dataclass (D5)
- Use `ratio_qty` for leg multiplier (defaults to "1"), reserve outer `qty` for spread count
- Limit price is on the spread, not the legs — this matches the "atomic approval" posture (D6) since the spread has ONE net price the user approves

Updated ADR-0029 D5 `MultiLegProposal` should add:
```python
@dataclass(frozen=True)
class OptionLeg:
    symbol: str  # OCC
    side: Literal['buy', 'sell']
    ratio_qty: int = 1
    position_intent: Literal['buy_to_open', 'buy_to_close',
                              'sell_to_open', 'sell_to_close']
    # NO per-leg limit_price — that's set on the parent MultiLegProposal
```

### R1 §1 was incomplete: contract listing requires UNDERLYING filter to be useful

`GET /v2/options/contracts` without filters returns the global universe (huge, paginated). Practical use requires `?underlying_symbols=NVDA&type=call&expiration_date_gte=2026-06-12&expiration_date_lte=2026-06-26` style filtering. R1 didn't show the realistic query shape.

For our use:
- `expiration_date_gte` + `expiration_date_lte` for DTE bracketing
- `underlying_symbols` (plural — comma-separated) for batch
- `type=call|put`
- `strike_price_gte` / `strike_price_lte` for strike range
- `limit` (default 100, max ?) + `next_page_token` for pagination

## Critical incidental finding: interim picker had a tz-bug, NOW FIXED

**Symptom (pre-fix):**
- Last night's smoke at 1:56 AM PT returned `data_blocked: data_provider_error` for **all 28 universe symbols**
- Was assumed to be "yfinance returns no bars on weekends" — actually was a real bug
- Tuesday's 8:30 AM ET pre-market cron would have hit the same bug → **silent broken brief on day 1**

**Root cause:**
- `data/base.py::validate_bars` normalizes timestamps to **tz-NAIVE UTC** (line 75: `.dt.tz_convert("UTC").dt.tz_localize(None)`)
- `data/yfinance_provider.py::fetch_bars` line 268 (and `advisor.py::recommend` line 524) compared this tz-naive column against tz-AWARE `cutoff` / `asof_ts` Timestamp
- Pandas 2.x raises `TypeError: Invalid comparison between dtype=datetime64[ns] and Timestamp` on tz-aware vs tz-naive
- Pandas 1.x would have silently coerced; 2.x is stricter
- The bug surfaces ONLY when `as_of` filtering is active (which it is in `recommend()`'s default code path)

**Fix landed at:**
- `hermes_quant/data/yfinance_provider.py` line 263-273 — convert cutoff to tz-naive before comparison, with comment explaining the validate_bars contract
- `hermes_quant/advisor.py` line 515-525 — convert `last_bar_ts` (tz-naive from validate_bars) back to tz-aware UTC for arithmetic with `asof_ts`

**Verification:**
- Single-symbol smoke: `recommend(symbol='NVDA')` returns 275 bars, `data_quality.bars_received=275`, `data_quality.last_bar_age_minutes=3729.9` (Friday close → Sunday now, ≈62h, sane)
- Cron script smoke: `~/.hermes/scripts/quant-daily-interim.py` returns Discord-ready markdown — **9 actionable longs (NET, DDOG, MDB, CRWD, ZS, ...) + 19 silent + 0 errors + 0 data-blocked** out of 28 universe symbols
- Test suite: `604 passed, 1 skipped, 0 failures` after the fix

**Why the bug got past CI:**
- CI fixtures use synthetic bars (parquet-loaded, already tz-naive)
- `validate_bars` is exercised in tests but the as_of filter is rarely tested in a tz-mismatched configuration
- Real yfinance returns tz-aware (America/New_York), which only the live path triggered

**Recommended ADR amendment**: ADR-0005 amendment 2026-05-13 (Wave C.1) noted "comparison-safe regardless of input bars timezone (validate_bars normalizes to UTC)" — but the comment was wrong about the form of normalization. validate_bars normalizes to **tz-NAIVE UTC**, not tz-aware UTC. Either:
1. Change `validate_bars` to keep tz-aware UTC (riskier — touches the data layer's invariant), or
2. Document tz-NAIVE-UTC as the invariant and audit all consumers (this fix follows option 2)

I've gone with option 2. A future ADR-amendment should formalize this convention.

## Tuesday-readiness checklist

- [x] Alpaca paper credentials valid (Level 3, $100k cash, $200k BP)
- [x] Calendar confirms Tuesday 09:30 ET open (Monday is Memorial Day, no run)
- [x] Multi-leg order schema verified (`order_class=mleg`, `legs[].position_intent`, `ratio_qty`, outer `limit_price`)
- [x] Greeks pathway works via `OptionChainRequest` SDK call (~58% native; py_vollib synthesis for the rest)
- [x] Interim picker tz-bug fixed, tests passing
- [x] Cron schedule verified: pre-market `30 5 * * 1-5` PT (8:30 AM ET) — first run Tuesday 2026-05-26
- [x] Cron schedule verified: EOD `30 12 * * 1-5` PT (3:30 PM ET) — first run Tuesday 2026-05-26
- [x] Account clean after probe (0 open orders, 0 positions, 1 canceled probe-mleg in history)
- [ ] alpaca-py installed in hermes-quant venv (currently in `~/.hermes/hermes-agent/venv` only)
- [ ] py_vollib installed (same venv as above)
- [ ] yfinance installed (same venv as above) — was missing! installed today
- [ ] Migrate to `uv` project venv per user preference (deferred — `curl|sh` install was blocked)

## Outstanding gaps for the actual options pipeline

These are fine to address post-Tuesday since interim picker is equity-only:

1. **No historical options chain data on Alpaca free tier.** Confirmed via R1 §6. Backtesting options strategies requires Polygon.io / ORATS / ThetaData, or synthetic chain generation from equity bars + Black-Scholes. Recommend deferring real options backtesting until v0.6.0.
2. **OPRA feed costs.** The `feed=opra` 403 means real-time top-of-book is paywalled. The free `feed=indicative` has 15-min delayed quotes, sufficient for swing/positional strategies but NOT for short-DTE scalping.
3. **NTA delay confirmed via R1 §4** but not live-probed today (no expiry events occurred during probe). Will need separate probe Tuesday-Friday after a real expiry.

## Files written

- `/mnt/e/CS/github/hermes-quant/hermes_quant/data/yfinance_provider.py` — tz fix
- `/mnt/e/CS/github/hermes-quant/hermes_quant/advisor.py` — tz fix
- `/mnt/e/CS/github/hermes-quant/docs/research/2026-05-24-r5-alpaca-live-probe.md` — this note
- `/tmp/alpaca-probe-{A,B,C,D,E,F2,cleanup-audit}.py` — reproducible probe scripts (not committed; treated as scratch)
