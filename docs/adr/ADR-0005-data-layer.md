# ADR-0005: Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities

**Status**: proposed
**Date**: 2026-05-12

## Context

The daemon needs market data: OHLCV bars (1m, 5m, 1h, 1d), optionally orderbook tops (for microstructure analyst), optionally tick data (deferred to v0.2). It must support:

- Free / zero-cost development (yfinance)
- Crypto live data (ccxt — Binance, Kraken, Coinbase)
- US equities live data (Alpaca free tier; SIP/Polygon paid tier later)
- Historical bars for backtesting (the same providers, with caching)

Per `docs/research/02-framework-integration.md` §3-4, key constraints:
- **yfinance**: 15-min delay on equities; aggressive polling triggers IP bans; works for daily and after-hours batch only.
- **alpaca-py**: replaces deprecated `alpaca-trade-api`; free tier is IEX-only (~2-3% of US equity volume), 200 req/min, paper-trade endpoint at `paper-api.alpaca.markets`.
- **ccxt**: industry standard for crypto exchange APIs, MIT-licensed, supports 100+ exchanges with normalized symbols. CCXT Pro (paid) adds WebSockets.

## Decision

A `DataProvider` Protocol with concrete implementations per source. The daemon resolves a provider per `(asset, timeframe)` based on config.

```python
from typing import Protocol
import pandas as pd

class DataProvider(Protocol):
    name: str
    asset_classes: list[str]
    timeframes: list[str]
    requires_credentials: bool

    def fetch_bars(self, asset: str, timeframe: str,
                   start: pd.Timestamp, end: pd.Timestamp,
                   *, use_cache: bool = True) -> pd.DataFrame: ...

    def fetch_latest(self, asset: str, timeframe: str,
                     lookback: int = 500) -> pd.DataFrame: ...

    def health(self) -> dict: ...
```

Returned DataFrame schema (canonical):
```
columns: [timestamp, open, high, low, close, volume, amount?]
timestamp: pd.Timestamp UTC, ascending, no duplicates
open, high, low, close, volume: float
amount: float (optional; quote-currency volume; populated when source provides)
```

### Concrete providers shipped in v0.1

| Provider | Module | Asset classes | Timeframes | Latency | Cost |
|---|---|---|---|---|---|
| yfinance | `hermes_quant.data.yfinance_provider` | equity, etf, crypto-USD | 1m, 2m, 5m, 15m, 30m, 1h, 1d | 15min | free |
| ccxt | `hermes_quant.data.ccxt_provider` | crypto | 1m, 5m, 15m, 1h, 4h, 1d | live | free + exchange limits |
| alpaca | `hermes_quant.data.alpaca_provider` | equity, crypto | 1m, 5m, 15m, 1h, 1d | live | free (IEX) / paid (SIP) |

The daemon uses **provider chains** — config specifies a primary and fallbacks per asset class:

```yaml
quant:
  data:
    crypto:
      primary: ccxt
      fallback: [yfinance]
      ccxt:
        exchange: binance
    equity:
      primary: alpaca
      fallback: [yfinance]
      alpaca:
        feed: iex                    # iex or sip
        paper: true                  # paper account for v0.1
    cache:
      enabled: true
      dir: ~/.hermes/quant/cache
      ttl_seconds: 300               # bar cache TTL
```

If the primary fails (rate limit, network error, asset out of scope), the daemon falls back to the next provider. Failures are logged; persistent failures surface in `quant_doctor`.

### Caching strategy

Bar fetches are cached in `~/.hermes/quant/cache/<provider>/<asset>/<timeframe>.parquet`. Cache invalidation: bars older than the daemon's "freshness threshold" for the timeframe are re-fetched (1m bars: 30s freshness; 1d bars: 1 hour freshness). Backtest mode uses `use_cache=True` aggressively to avoid re-hitting providers.

Parquet (not CSV) for: smaller size, typed columns, faster read. PyArrow is a transitive dep via pandas[parquet].

### Data quality gates (per `02-framework-integration.md` §6)

`yfinance` returns NaN rows and zero-volume rows for halted tickers. The provider wrapper applies these gates **before** returning:

```python
def _validate_bars(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    df = df.dropna(subset=["open", "high", "low", "close"])     # null OHLC
    df = df[df["volume"] > 0]                                    # halted ticker
    df = df[~df.duplicated(subset=["timestamp"], keep="last")]   # dedupe
    df = df.sort_values("timestamp").reset_index(drop=True)
    if len(df) < 2:
        raise DataQualityError(f"{asset}: < 2 valid bars after gating")
    return df
```

### Credentials

Alpaca and (paid) ccxt providers need API keys. Config schema:
```yaml
quant:
  data:
    alpaca:
      api_key: ${ALPACA_API_KEY}            # env var substitution
      api_secret: ${ALPACA_API_SECRET}
      base_url: https://paper-api.alpaca.markets
    ccxt:
      exchange: binance
      api_key: ${BINANCE_API_KEY}            # only required for higher rate limits
      api_secret: ${BINANCE_API_SECRET}
```

Env vars are listed in `plugin.yaml::optional_env` (not `requires_env` — per the hermes-s2s plugin-authoring lesson, `requires_env` blocks install for users who only want crypto). `quant_doctor` reports which credentials are present and what's blocked without them.

### Backtest data path

Backtests run against cached parquet files at `~/.hermes/quant/cache/<provider>/<asset>/<timeframe>.parquet`. `hermes quant backtest --download` pre-fetches a date range; `hermes quant backtest` runs against whatever's in cache.

For the public-replicable test suite, fixture data lives at `tests/fixtures/bars/<asset>-<timeframe>-<from>-<to>.parquet` (committed to repo, ~5MB for BTC 1h × 1 year, AAPL 1d × 5 years, SPY 1h × 2 years). Tests never hit live providers.

## Consequences

### Positive

- Provider abstraction means swapping yfinance → Polygon → Tiingo is one import change.
- Provider chains gracefully degrade — yfinance fallback for crypto means the user can demo without exchange API keys.
- Caching makes backtests reproducible; the same date range pulls from disk on the second run.
- Data validation at the provider boundary prevents downstream NaN-prop bugs in analysts.
- Fixture data in repo means CI and contributors don't need API keys to run tests.

### Negative

- yfinance is only suitable for daily-frequency strategies; intraday users MUST configure alpaca or ccxt before claiming meaningful results. README must be loud about this.
- Cache invalidation is a perpetual source of subtle bugs. Mitigated by versioning the cache schema (cache-v1, cache-v2 paths) so a code change can trigger a clean cache.
- Parquet adds a transitive PyArrow dep (~30MB). Acceptable; pandas + ML stack pulls it anyway.
- ccxt has 150+ exchanges with quirky symbol formats. v0.1 supports binance, kraken, coinbase explicitly; others "may work" but are untested.

## Implementation notes

- Provider entry points: `[project.entry-points."hermes_quant.data_providers"]`.
- The daemon resolves providers at startup based on config; no dynamic resolution per tick.
- Rate limiting per provider: a token-bucket limiter at the provider level (yfinance: 60/min; alpaca: 200/min; ccxt: per-exchange).
- All time handling is UTC end-to-end. Display layer (`quant_show_signals`) localizes for the user.
- The provider's `fetch_latest()` is what the daemon calls on each tick; `fetch_bars()` is the backtest path.
- Error taxonomy: `DataProviderError` (transient — retry), `DataQualityError` (don't retry — log + skip tick), `RateLimitError` (back off + fall back to next provider in chain).

## References

- `docs/research/02-framework-integration.md` §3, §4 — alpaca-py and yfinance specifics
- `docs/research/02-framework-integration.md` §6 — yfinance silent-corruption gotcha
- alpaca-py: https://github.com/alpacahq/alpaca-py
- ccxt: https://github.com/ccxt/ccxt
- yfinance: https://github.com/ranaroussi/yfinance
