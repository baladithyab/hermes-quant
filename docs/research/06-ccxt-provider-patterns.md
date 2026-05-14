# CcxtProvider research (for hermes-quant v0.3)

Sources: ccxt v4 wiki + `python/ccxt/binance.py` + `python/ccxt/base/errors.py`,
ccxt issue [#21783](https://github.com/ccxt/ccxt/issues/21783) (timestamp
semantics), Binance spot kline REST docs, official example
`examples/py/binance-fetch-ohlcv-pagination.md`.

## TL;DR

- `binance.fetch_ohlcv()` returns rows whose **timestamp = bar OPEN time
  (ms UTC)**. The tail row is the **in-flight bar** when fetching up to
  "now". The leaf MUST drop rows where `open_ts + tf_ms > as_of_ms` —
  this is the lookahead-safe close-time filter required by ADR-0006 /
  ADR-0009 §P0-A.
- Symbol = unified `'BTC/USDT'`. Pagination = official ccxt loop:
  `since = ohlcvs[-1][0] + 1` until empty. Spot kline cap = 1000/call
  (futures = 1500). Use `enableRateLimit=True` and let ccxt throttle —
  Binance spot allots 6000 weight/min/IP; klines cost 1–10 weight by `limit`.
- Retry on `NetworkError` subtree (`DDoSProtection`/`RateLimitExceeded`,
  `RequestTimeout`, `ExchangeNotAvailable`/`OnMaintenance`); surface
  fatally on `ExchangeError` subtree (`BadSymbol`, `AuthenticationError`).

## Canonical fetch_ohlcv pattern

Direct port of ccxt's `binance-fetch-ohlcv-pagination.md` plus our as_of guard:

```python
import ccxt
import pandas as pd

def fetch_binance_ohlcv(symbol: str, timeframe: str,
                       since_ms: int, until_ms: int) -> pd.DataFrame:
    ex = ccxt.binance({"enableRateLimit": True, "timeout": 20_000})
    ex.load_markets()                              # hydrates markets/timeframes
    tf_ms = ex.parse_timeframe(timeframe) * 1000   # '1h' -> 3_600_000
    limit = 1000                                   # Binance spot kline cap
    out: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, cursor, limit)
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1][0] + 1                  # +1ms past last open_ts
        if len(batch) < limit:
            break                                  # exchange has no more
    df = pd.DataFrame(out, columns=["ts","open","high","low","close","volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    # 'ts' is OPEN time. Bar covers [ts, ts+tf_ms); CLOSED at ts+tf_ms:
    df = df[df["ts"] + tf_ms <= until_ms]          # strictly closed bars
    return df
```

Gotchas:

- Without `since`, fetch returns "most recent N bars **including** the
  in-flight bar". Official example masks this by terminating when
  `len(batch) < limit`; we mask explicitly via close-time filter.
- `ex.parse_timeframe('1h')` returns **seconds** (3600). Multiply by 1000.
- Binance treats `startTime` as **inclusive** of any bar with
  `openTime >= startTime`.
- `load_markets()` once per process; cache `ex.markets`. Don't call in
  hot loops — wastes weight.

## as_of semantics — bar OPEN vs CLOSE alignment ⚠️ (money-software bug class)

A 1h bar with `ts = 2026-05-13T14:00:00Z` spans `[14:00, 15:00)` and is
only KNOWN at `15:00:00.000Z`. With `as_of = 14:30`, that bar is
in-flight; including it leaks the next 30 min of price action into the
analyst's feature vector.

ccxt issue **#21783** confirms what `binance.parse_ohlcv` does — despite
a misleading code comment naming the field `closeTime`, it returns
`safe_integer_2(ohlcv, 0, 'closeTime')`: index 0 of Binance's array,
which the API docs explicitly mark as OPEN time:

```
[ 1591478520000,   # open time  ← ccxt 'ts' (index 0)
  "0.02501300", ..., 1591478579999, ... ]   # close time at index 6 (ignored)
```

Rule for `CcxtProvider.fetch_bars`:

```python
tf_ms = self._exchange.parse_timeframe(timeframe) * 1000
asof_ms = int(as_of.timestamp() * 1000)
df = df[df["ts"] + tf_ms <= asof_ms]   # bar CLOSED at or before as_of
```

Matches `yfinance_provider`'s semantics. As_of filter is applied
**after** `validate_bars()` per ADR-0009 §P0-A. Mandatory unit:
`test_ccxt_provider_excludes_inflight_bar` — fixture where the last
bar's `ts == as_of - 30min` for `1h` must be **excluded**.

## Symbol normalization

- ccxt unified: **`'BTC/USDT'`** (slash). Internally maps to
  `market['id'] == 'BTCUSDT'` for the wire call.
- `fetch_bars` accepts unified form; if caller passes `'BTCUSDT'`,
  resolve via `ex.markets_by_id`. Validate against `ex.markets` else
  raise `DataQualityError`.
- `'BTC-USDT'` is Coinbase/KuCoin form — reject; do not silently
  cross-translate.
- `asset_class='crypto'` MVP canonical pair: `BTC/USDT` (charter).

## Rate limiting + pagination + retry shape

- **`enableRateLimit=True`** activates ccxt's leaky-bucket throttle
  using `ex.rateLimit` (ms between requests; Binance spot ≈ 50ms).
  Don't roll our own.
- Binance spot kline weight per `/api/v3/klines`:
  `[1,100)→1`, `[100,500)→2`, `[500,1000)→5`, `1000→10`. IP cap
  6000 weight/min spot, 2400/min UM-futures. With `limit=1000` we
  burn 10/call ⇒ 600 calls/min headroom.
- Pagination: loop above. For `lookback_bars=N` with no explicit
  window: `since_ms = now - (N + 1) * tf_ms` (one bar of slack so the
  in-flight drop doesn't undercount).
- Retry shape (mirrors yfinance provider's `tenacity` policy):
  - 3 retries, exponential backoff 1s/2s/4s, on `ccxt.NetworkError`
    (covers `RateLimitExceeded`, `RequestTimeout`,
    `ExchangeNotAvailable`, `OnMaintenance`, `DDoSProtection`).
  - On `RateLimitExceeded`, sleep ≥ `ex.rateLimit/1000` before retry.
  - **Never** retry `ExchangeError` subtree.

## Timeframe canonical set

`binance.timeframes` (per ccxt's `describe()`):
`1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M`.

hermes-quant v0.3 exposes a strict subset:

```python
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
```

Daemon ticks at `1h` for crypto MVP, `1d` for EOD batch backtests, the
others cover analyst feature windows. Anything else → `ValueError` at
the boundary. Extending later is additive, no ADR amendment needed.

## Error taxonomy (Retryable vs Fatal split)

ccxt v4 hierarchy (verified `python/ccxt/base/errors.py`):

```
BaseError
├── NetworkError          ← retryable
│   ├── DDoSProtection → RateLimitExceeded
│   ├── RequestTimeout
│   ├── ExchangeNotAvailable → OnMaintenance
│   └── …
└── ExchangeError         ← fatal — surface
    ├── BadRequest → BadSymbol, ArgumentsRequired
    ├── AuthenticationError → PermissionDenied
    ├── InsufficientFunds
    ├── InvalidOrder → OrderNotFound
    └── InvalidNonce      (irrelevant for fetch_ohlcv)
```

Mapping:

```python
RETRYABLE       = (ccxt.NetworkError,)        # whole subtree
FATAL_DATA      = (ccxt.BadSymbol,)           # → DataQualityError
FATAL_AUTH      = (ccxt.AuthenticationError,) # → ConfigError
# all other ExchangeError → DataQualityError + log
```

`fetch_ohlcv` is unauth — `AuthenticationError` should never fire here;
if it does, our config is broken (e.g. wrong testnet keys hitting
mainnet endpoint). Treat as fatal config error.

## Test fixtures — recorded cassettes vs testnet vs mocks

**In-memory fakes for unit; recorded JSON cassettes for integration;
no live calls in CI.**

- **Unit:** Inject a `FakeCcxtExchange` with deterministic synthetic
  OHLCV. Tests as_of filter, pagination, error mapping, symbol
  validation. `tests/unit/test_ccxt_provider.py`. Same shape as
  yfinance unit tests.
- **Integration** (`@pytest.mark.network`): One JSON cassette per
  (symbol, timeframe, window) via `pytest-recording`/`vcrpy`. Replays
  from disk in CI. Re-record manually with `--record-mode=once`.
  Storage: `tests/fixtures/ccxt_cassettes/*.yaml`.
- **Why not Binance testnet** (`exchange.set_sandbox_mode(True)`) for
  unit tests? Testnet has no historical data guarantees — bars are
  often missing or shifted. Unsuitable as a determinism source. Testnet
  IS used for the trade-execution path (separate, not v0.3).
- **Mainnet calls in CI = forbidden.** Geo-blocking (US IPs hit 451)
  + rate-limit risk + non-determinism.

## Adapter shape for `DataProvider` Protocol

```python
# hermes_quant/data/ccxt_provider.py
import ccxt
import pandas as pd
from .base import DataProvider, DataQualityError, validate_bars

class CcxtProvider:                            # implements DataProvider
    def __init__(self, exchange_id: str = "binance",
                 sandbox: bool = False) -> None:
        klass = getattr(ccxt, exchange_id)
        self._ex = klass({"enableRateLimit": True, "timeout": 20_000,
                          "options": {"adjustForTimeDifference": True}})
        if sandbox:
            self._ex.set_sandbox_mode(True)
        self._ex.load_markets()

    def fetch_bars(self, symbol: str, asset_class: str, timeframe: str,
                   *, lookback_bars: int,
                   as_of: pd.Timestamp | None = None) -> pd.DataFrame:
        if asset_class != "crypto":
            raise ValueError(f"CcxtProvider only handles crypto, got {asset_class}")
        if symbol not in self._ex.markets:
            raise DataQualityError(f"unknown symbol {symbol!r}")
        if timeframe not in self._ex.timeframes:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        tf_ms = self._ex.parse_timeframe(timeframe) * 1000
        end_ms = int((as_of or pd.Timestamp.utcnow()).timestamp() * 1000)
        since_ms = end_ms - (lookback_bars + 1) * tf_ms     # +1 bar slack
        rows = self._paginate(symbol, timeframe, since_ms, end_ms)
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = validate_bars(df)                              # NaN/zero-vol/dedupe/sort
        if as_of is not None:                               # CRITICAL as_of filter
            close_ts = df["ts"] + pd.Timedelta(milliseconds=tf_ms)
            df = df[close_ts <= as_of]
        return df.tail(lookback_bars).reset_index(drop=True)
```

Mirrors `yfinance_provider.py`'s contract: same Protocol, same
post-`validate_bars()` as_of filter ordering.

## Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Binance API drift (payload reorder, weight rule change) | Med | High | Pin `ccxt>=4.x,<5`; re-record cassettes on bump; watch ccxt CHANGELOG |
| Geo-blocking (Binance.com 451 in US/UK) | High in US | Total outage | ADR-0005 provider chain falls back to `coinbase`/`kraken`; `BINANCE_BLOCKED=1` env override |
| Exchange maintenance | Periodic | Hours of downtime | `OnMaintenance` caught → daemon enters silence-by-default per ADR-0001 |
| In-flight bar leaked into features | Med if forgotten | **Catastrophic** — invalidates backtests | Mandatory unit `test_ccxt_provider_excludes_inflight_bar` + CI `shuffle_timestamps_test`; as_of filter at leaf |
| Rate-limit ban (418/429) | Low w/ `enableRateLimit` | 1h–24h IP ban | `enableRateLimit=True` always; 1 provider/exchange/process; backoff, never tight-retry |
| Symbol delisting mid-backtest | Low | Empty DataFrame | `validate_bars` raises `DataQualityError` if <2 rows |
| Clock skew vs Binance servers | Low | Off-by-one in as_of edge | `adjustForTimeDifference: True` in exchange config |
| ccxt semantic change between versions | Low | Subtle off-by-one | Version pin + integration canary `test_known_btc_bar_2024_01_01` |

## References

- ccxt manual `fetchOHLCV`:
  https://github.com/ccxt/ccxt/wiki/Manual#ohlcv-candlestick-charts
- ccxt pagination example (Binance, Python):
  https://github.com/ccxt/ccxt/blob/master/examples/py/binance-fetch-ohlcv-pagination.md
- ccxt issue #21783 — Binance `parse_ohlcv` returns OPEN time at index 0:
  https://github.com/ccxt/ccxt/issues/21783
- Binance kline REST docs (weight tiers, max=1000 spot / 1500 futures):
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api#klinecandlestick-data
- ccxt exception hierarchy:
  https://docs.ccxt.com/#/README?id=exception-hierarchy
- This repo: ADR-0005 (data layer), ADR-0006 (lookahead defenses),
  ADR-0009 §P0-A (data validation ordering).
