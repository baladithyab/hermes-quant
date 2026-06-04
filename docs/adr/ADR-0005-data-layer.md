# ADR-0005: Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities

**Status**: Accepted (2026-05-12), implemented
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

---

## Amendment 2026-05-13: Retry layer + as_of param + symbol path safety

### Context

Three follow-ups to ADR-0005 surfaced after the original draft:

1. Phase-9e (Phase-8 synthesis P1 follow-up — Gemini flagged "transient-error handling" as missing) shipped a per-provider exponential-backoff retry layer in v0.1.1. The original ADR-0005 §Implementation notes specified a `RateLimitError → fall back to next provider` taxonomy but said nothing about retrying the same vendor first when a 429 is genuinely transient. v0.1.1 fills that gap.
2. Round-2 mining of TauricResearch/TradingAgents surfaced two P0 patterns that Phase-8 missed:
   - Pattern #1: lookahead-bias enforcement at the data-provider leaf (`as_of` param), not just at the analyst layer.
   - Pattern #2: a path-safety whitelist for any user-supplied symbol/ticker that gets interpolated into a filesystem path.

Both Round-2 patterns are P0 and are scheduled for v0.1.2. The retry layer is shipped. This amendment records all three and captures the doctrine, because the interplay between the retry layer and the existing chain-fallback is non-obvious and easy to get wrong.

### Part A — Per-provider retry-before-fallback (v0.1.1, shipped)

**What changed.** ADR-0005 §Implementation notes' error taxonomy listed `RateLimitError` as a fall-through-to-next-provider signal. v0.1.1 inserts a per-provider retry layer **before** that fallback fires.

**Implementation.** `_retry_with_backoff` helper at `hermes_quant/data/yfinance_provider.py:47-94`. Three attempts, base delay 2s, factor 2 → delay sequence `{2s, 4s}` for a worst-case wall budget of ~6s before the chain advances to the next provider. The helper retries on `RateLimitError`, `ConnectionError`, and transient `OSError`; everything else (e.g., `DataQualityError`, `ValueError`) propagates immediately.

Adapted — **not copied verbatim** — from the `yf_retry` pattern in TauricResearch/TradingAgents. Their helper is `YFRateLimitError`-specific; ours generalizes to the three transient-error classes above so it can host the ccxt and alpaca providers in v0.1.2 without rework.

**Test injectability.** `YFinanceProvider.__init__` accepts `retry_max_attempts`, `retry_base_delay_s`, `retry_factor` so unit tests can drive the helper with sub-second delays. With production defaults a unit test that exhausts the retry budget would add ~6s; tests pass `retry_base_delay_s=0.0` to keep the suite fast.

**Tests.** `tests/unit/test_yfinance_provider.py::{test_transient_rate_limit_recovers_within_retry_budget, test_persistent_rate_limit_exhausts_retry_budget}`, plus `call_count` assertions added to the existing `rate_limit` and `DataProviderError` tests so a regression that disables the retry loop fails loudly.

**Doctrine — read this before changing the retry/fallback layering.** Per-provider retry happens FIRST; chain fallback only triggers after retries are exhausted. The two layers exist to handle different failure modes and must not be conflated:

- **Per-provider retry** handles transient throttle/network blips that resolve in seconds. *Same vendor, retried.* A 429 from yfinance during a polling burst usually clears within one or two backoff cycles.
- **Chain fallback** handles persistent rate-limiting, sustained outages, or asset-not-supported errors. *Different vendor.* If yfinance is down for 20 minutes, retrying it 3 times is pointless; we want ccxt or alpaca.

Conflating them — e.g., retrying across the chain, or skipping retry and going straight to fallback on every 429 — wastes the retry budget on a vendor that's down, or burns through fallbacks on a vendor that's just throttling. Keep them layered.

### Part B — `as_of` parameter on DataProvider Protocol (v0.1.2, planned)

**What changes.** The `DataProvider` Protocol gains an `as_of: pd.Timestamp | None` keyword-only parameter on both `fetch_bars` and `fetch_latest`. Concrete providers MUST drop rows where `bar.timestamp > as_of` **before returning**. `as_of=None` means "no clamp" (live-mode reads the latest available bar).

**Round-2 provenance.** TauricResearch/TradingAgents enforces lookahead-bias-freedom at the data leaf: every `dataflows/` function (`load_ohlcv`, `get_stock_stats`, …) takes a `curr_date` argument and slices `data[data['Date'] <= curr_date_dt]` before returning. We adopt the same discipline. It is cheap to add now, expensive to retrofit once consumers depend on the current Protocol signature.

**Protocol change.**
```python
class DataProvider(Protocol):
    def fetch_bars(
        self, asset: str, timeframe: str,
        start: pd.Timestamp, end: pd.Timestamp,
        *, as_of: pd.Timestamp | None = None,
        use_cache: bool = True,
    ) -> pd.DataFrame: ...

    def fetch_latest(
        self, asset: str, timeframe: str,
        *, as_of: pd.Timestamp | None = None,
    ) -> pd.DataFrame: ...
```

**Semantics.** If `as_of` is supplied, the provider drops every row with `timestamp > as_of` before applying the existing `_validate_bars` quality gates. Caching keys are unaffected — cache stores raw fetched data; the `as_of` filter is applied on read.

**Call-site routing.**
- *Backtest replay path:* `tick_loop` forwards `MarketContext.asof` as `as_of` to every DataProvider call, end-to-end. This is the change that makes backtest reproducibility actually safe at the provider boundary.
- *Live daemon path:* `as_of=None` (or equivalently `pd.Timestamp.utcnow()`). No clamp; latest bar wins.

**Pairs with.** v0.1.2 `tests/test_no_lookahead.py` CI gate (per the ADR-0006 amendment). The CI gate currently shuffle-tests analysts; the `as_of` parameter is what makes the gate enforceable at the provider boundary too — an analyst can no longer accidentally see future bars even if its own code is lookahead-clean, because the data simply isn't in the DataFrame it receives.

**Cross-link.** ADR-0002 (analyst Protocol / `MarketContext.asof`) — the Protocol change moves with this one; the analyst contract already carried `asof`, the data layer now honors it.

### Part C — `safe_symbol_component` for cache/JSONL path safety (v0.1.2, planned)

**What changes.** v0.1.2 adds `hermes_quant/utils/path_safety.py::safe_symbol_component(symbol: str) -> str`. Every code path that interpolates a user-supplied ticker, pair, or asset string into a filesystem path MUST route through it.

**Round-2 provenance.** TauricResearch/TradingAgents ships `safe_ticker_component(t)` — a whitelist regex (`A-Z0-9.-_^`, length ≤ 32, rejects `..`, `/`, leading `.`, whitespace, NUL) used at every call site that builds a filesystem path from a ticker. Without something equivalent, a malicious or mistyped portfolio entry can write to `../../etc/passwd`-style paths or cache-collide across symbols.

**Why we need it.** We embed asset/pair strings directly into:
- per-symbol parquet caches under `~/.hermes/quant/cache/<provider>/<asset>/<timeframe>.parquet` (per ADR-0005 §Caching strategy)
- per-symbol log paths in `hermes_quant/cli/`
- portfolio-loader-derived paths in `hermes_quant/daemon/portfolio_loader.py`

Crypto pairs like `BTC/USDT` are not even path-legal as written — the slash creates an unintended subdirectory. The global `signals.jsonl` is symbol-agnostic and is **not** affected (see Cross-cuts).

**Whitelist.** A regex that accepts the realistic universe — `BTC`, `BTC/USDT`, `^GSPC`, `BRK-B`, `ES=F` — and rejects everything else. Length cap 32. Rejects `..` (anywhere), leading `.`, raw `/` (after canonical transforms below), NUL, control chars.

**Canonical transforms** (applied before validation, then validation rejects anything still unsafe):

| Input | Output | Reason |
|---|---|---|
| `BTC/USDT` | `BTC_USDT` | slash → underscore: path-legal canonical form |
| `^GSPC` | `_GSPC` | caret → underscore: caret reserved on some filesystems |
| anything else failing the whitelist | — | raise `ValueError` |

**Routing.** Every cache, JSONL, and log path that takes a symbol gets routed through `safe_symbol_component`:
- `hermes_quant/data/yfinance_provider.py` — OHLCV cache path (becomes the v0.1.2 caching change too)
- `hermes_quant/data/ccxt_provider.py`, `alpaca_provider.py` — same
- `hermes_quant/daemon/portfolio_loader.py` — portfolio rehydration paths
- `hermes_quant/cli/` — per-symbol log destinations

**Tests.** `tests/unit/test_path_safety.py`, `pytest.mark.parametrize` over:
- *Pass:* `BRK-B`, `^GSPC` → `_GSPC`, `BTC/USDT` → `BTC_USDT`, `ES=F`, `BTC`, `AAPL`
- *Reject (`ValueError`):* `../etc`, `foo\x00bar`, `'a' * 64`, `.hidden`, `foo bar`, `foo\nbar`, `` (empty)

### Cross-cuts

- **ADR-0002 (analyst Protocol):** Part B is a Protocol change; cross-link required when ADR-0002 is touched. `MarketContext.asof` is now load-bearing past the analyst boundary — the data provider honors it too.
- **ADR-0006 (RL graduation criteria + lookahead test gate amendment):** Part B's `as_of` parameter is what makes `tests/test_no_lookahead.py` enforceable at the data-provider boundary, not only at the analyst layer. An analyst can no longer leak future data through a permissive provider.
- **ADR-0001 (sidecar reproducibility):** Part B is a precondition for genuine backtest replay. Without `as_of` honored at the leaf, an analyst's signal log can be byte-identical between runs and still be wrong, because the provider returned different "latest" data on replay.
- **ADR-0008 (signal bus):** Part C affects per-symbol cache and log paths only. The global `signals.jsonl` is symbol-agnostic (it carries the symbol as a JSON field, not in the path) and is unaffected — but any future per-symbol JSONL split would need to route through `safe_symbol_component`.

### Provenance

- Part A: Phase-8 synthesis P1 follow-up — Gemini flagged "transient-error handling" as a gap in the original ADR-0005 error taxonomy. Implementation at `hermes_quant/data/yfinance_provider.py:47-94`. Pattern adapted (generalized beyond `YFRateLimitError`) from TauricResearch/TradingAgents' `yf_retry`.
- Parts B and C: Round-2 mining of TauricResearch/TradingAgents surfaced these as P0 gaps not present in Phase-8 synthesis. Cite `dataflows/load_ohlcv()` and the `curr_date` slicing convention (Part B), and `dataflows/utils/ticker_safety.py::safe_ticker_component` (Part C).
