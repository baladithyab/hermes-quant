# ADR-0069: Still-forming-bar discipline for daily timeframe mid-session

**Status:** Accepted (2026-05-28), implemented
**Date:** 2026-05-28
**Wave:** D (paper-trading fidelity)
**Supersedes:** nothing
**Cites:** [ADR-0008](ADR-0008-signal-record-schema.md) (signal record schema), [ADR-0068](ADR-0068-decision-time-vs-bar-time-honesty.md) (`bar_ts` semantics), ADR-0009 (deterministic replay), [ADR-0050](ADR-0050-alpha-zoo-with-ast-purity-and-lookahead-gate.md) (lookahead gate)

---

## Context

When the BMA runs mid-session at the daily (`1d`) timeframe, yfinance returns an OHLCV frame whose **last row is today's still-forming bar**. The `close` of that row is the latest intraday print, not a settled close. `MarketContext.last_close = bars["close"].iloc[-1]` therefore captures whatever happened to be the last tick at the moment of the call.

This produces three concrete problems:

### P1. Non-determinism in replay

Two BMA invocations on the same bar boundary can produce **different** `decision_price`, `last_close`, and downstream Kelly sizes — because the still-forming bar's close has changed between calls. This breaks the ADR-0009 §replay-equality property: "replaying the same market bar produces the same decision."

### P2. Lookahead-adjacent semantics

Strictly speaking, using a 14:00 ET intraday print at 14:00 ET wall-clock is **not** lookahead — that information is contemporaneously available to any market participant. But several downstream code paths assume `last_close` is a *settled* daily-bar close:

- Volatility estimators (`ATR / last_close`, log-return σ from daily closes) compute biased numbers when one of the "closes" is a partial-bar tick.
- Calibration training reads historical settled closes; at inference time, the same field name carries a different distribution.
- Reflector replay reads `decision_price` and assumes it's the bar's settled close for return attribution.

The mismatch is silent, not loud — every estimator still returns a number, just a number that drifts away from its calibration distribution by intraday-noise size (typically 30–80 bps for liquid equities, larger for small caps).

### P3. Downstream slippage attribution corruption

A fill at `decision_price = $84.01` (intraday tick) "fills exactly" if PaperReactor uses the same intraday tick. So `slippage = fill_price - decision_price = 0`. That number is correct *if* you also model the fact that live execution would target the market price seconds later, not this fleeting intraday tick. Today PaperReactor models neither, so the chain `intraday-tick → exact-fill-at-tick` produces a fake-precision number that lets us claim 0 bps slippage on every fill (see [ADR-0070](ADR-0070-paper-execution-fidelity.md)).

### Empirical surface

5/28 forensic on 85 fills with `bar_ts = 04:00 UTC`:

```
decision_price falls inside today's intraday H/L range:  80 / 85   (94%)
decision_price == today's CLOSE:                           4 / 85   (5%)
decision_price == yesterday's close:                       1 / 85   (1%)
```

The 5% "== today's close" is most likely a yfinance timing artifact (the still-forming bar's close transiently matched the 5/28 settled close shortly before market close), not a real lookahead — but the surface exists *because* we read still-forming bars as if they were settled.

---

## Decision

### D69.1 Daily-timeframe semantics: snap to the last completed bar mid-session

For `timeframe = "1d"` (and any other "session-aligned" timeframe whose bar boundary is a session close, not wall-clock), the data layer **drops the still-forming bar** if the wall-clock time is between the session open and session close of that bar's date.

Concretely: when `bars` is fetched at 14:00 ET on 2026-05-28, the row labeled `2026-05-28` is dropped. `last_close` becomes the 5/27 settled close. The BMA runs on 5/27's bar, not on a partial 5/28 bar.

Mechanics live in a new helper, `hermes_quant.data.bar_alignment.drop_still_forming_bar(bars, timeframe, asset_class, now=None)`:

- Determine the session-close cutoff for `bars.iloc[-1]['timestamp']` and `asset_class` (US equities = 16:00 ET; crypto = no cutoff, bars are continuous).
- If `now < that_cutoff`, drop the last row.
- Return the (possibly-shorter) DataFrame plus a flag `still_forming_dropped: bool` for observability.

The advisor wraps every `1d`-timeframe data fetch with this call before constructing `MarketContext`. Crypto and intraday-aligned timeframes (5m, 15m, 1h) are pass-through — their bars close on a wall-clock boundary, not a session boundary, and a "still-forming 5m bar" is genuinely transient.

### D69.2 Don't conflate "snap to settled" with "lookahead-prevention"

This decision **is not** a lookahead fix. It's a semantic-clarity fix. Lookahead prevention is what [ADR-0050](ADR-0050-alpha-zoo-with-ast-purity-and-lookahead-gate.md) does (AST purity + lookahead-sentinel CI gate). This ADR is about ensuring that the *meaning* of `last_close` matches what every consumer assumes (a settled bar close), so that:

- Replay equality holds (two runs on the same `bar_ts` produce the same numbers).
- Calibration distributions match between training and inference.
- Reflector return attribution uses settled prices (correct) instead of intraday-mid prices (drift-prone).

For real-time intraday signal generation that *should* use the latest tick, the right path is a different timeframe (e.g. `15m` or `1h`), not abusing the daily timeframe's bar contract.

### D69.3 Preserve the option to opt in to still-forming bars

Some analysts may legitimately want the still-forming-bar tick (e.g. a "session momentum" analyst that explicitly conditions on intraday motion). For those, expose:

```python
ctx = MarketContext(..., extras={
    "still_forming_close": float | None,    # the dropped bar's tick close, if dropped
    "still_forming_high":  float | None,
    "still_forming_low":   float | None,
})
```

Analysts that need it can pull from extras explicitly. The default `last_close` stays clean.

### D69.4 Backtest/replay correctness

Backtests already use settled closes (they iterate over a fixed historical OHLCV frame, no still-forming bars exist there). After D69.1, **live decisioning matches backtest decisioning** for the daily timeframe — currently it doesn't, and that gap is silent.

The shadow-account counterfactual ([ADR-0049](ADR-0049-shadow-account-counterfactual.md)) gains correctness on the same axis: counterfactual replay through historical bars now produces the same `last_close` distribution that live decisioning sees post-fix.

---

## Consequences

**Positive:**
- Replay equality is restored on the daily timeframe.
- Volatility estimators, calibration distributions, and slippage attribution stop drifting against their training-time assumptions.
- Backtest/live parity becomes a verifiable property.
- The 4/85 "decision_price == today's close" surface in 5/28 forensic disappears (the still-forming bar is no longer in the frame).

**Negative / risks:**
- The system reacts to *yesterday's* settled close, not today's intraday tape. For a daily-timeframe strategy this is the right behavior — the universe of strategies that genuinely need intraday awareness should run on intraday timeframes.
- Daily-timeframe trades fired mid-session before market close will use yesterday's data. Reading: that's fine; daily strategies were never supposed to react to intraday motion.
- Crypto: no change. Crypto bars don't have a session-close concept.

**Out of scope:**
- Adding intraday timeframes to the BMA. The current playbook is daily-only. Intraday is a separate roadmap question.
- Whether yfinance is the right data source for production. ADR-0019 et seq. cover that.

---

## Implementation hooks

- New module: `hermes_quant/data/bar_alignment.py` with `drop_still_forming_bar(bars, timeframe, asset_class, now=None) -> tuple[pd.DataFrame, dict]`.
- Wire into `hermes_quant.advisor:recommend` and any other code path that constructs `MarketContext` for daily-timeframe equity data. Search: `MarketContext\(` with `timeframe=.*1d` nearby.
- `MarketContext.extras` populated with `still_forming_*` fields when a row was dropped.
- `Reactor`-side: no change. The reactor reads decisioning output, not raw bars.
- Test: round-trip on a 5/28 14:00 ET fixture, assert `last_close == 5/27 settled close`, not `5/28 still-forming tick`.

---

## Verification

```python
from datetime import datetime, timezone
import pandas as pd
from hermes_quant.data.bar_alignment import drop_still_forming_bar

# Fixture: 5/27 settled, 5/28 still-forming, called at 14:00 ET (= 18:00 UTC)
bars = pd.DataFrame({
    "timestamp": [pd.Timestamp("2026-05-27", tz="UTC"), pd.Timestamp("2026-05-28", tz="UTC")],
    "open":  [100.0, 102.0],
    "high":  [101.5, 103.0],
    "low":   [ 99.5, 101.0],
    "close": [101.0, 102.5],   # 102.5 is intraday tick
    "volume":[1e6, 5e5],
})
now_intraday = datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)   # 14:00 ET

trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=now_intraday)
assert len(trimmed) == 1
assert float(trimmed["close"].iloc[-1]) == 101.0    # 5/27 settled
assert info["still_forming_dropped"] is True
assert info["still_forming_close"] == 102.5

# After 5/28 16:00 ET (post-close), the bar is settled — keep it.
now_post_close = datetime(2026, 5, 28, 21, 0, tzinfo=timezone.utc)  # 17:00 ET, settled
trimmed, info = drop_still_forming_bar(bars, "1d", "equity", now=now_post_close)
assert len(trimmed) == 2
assert info["still_forming_dropped"] is False
```

Cross-check on live behavior post-fix: re-run today's BMA on a sample symbol, confirm `r["decision_price"]` matches yesterday's yfinance settled close (within 1 cent), not the current intraday tick.
