# ADR-0017: CcxtProvider for crypto OHLCV bars

**Status**: Accepted (2026-05-13), implemented
**Date**: 2026-05-13
**Target**: v0.3.0
**Cross-cuts**: ADR-0005 (data layer Protocol), ADR-0006 (lookahead enforcement), ADR-0009 §P0-A (as_of leaf filter), founding charter §"What I'd build first" (BTC/USDT MVP)
**Research**: [`docs/research/06-ccxt-provider-patterns.md`](../research/06-ccxt-provider-patterns.md)

---

## Context

The founding charter's MVP recipe is a *"three-analyst committee on liquid crypto — BTC/USDT, because clean data, 24/7, no halts"* paper-traded for 4-8 weeks before any RL aggregator work begins. v0.2.0 ships only `YFinanceProvider` so the entire MVP is gated on a crypto data path. ADR-0005 reserved the slot.

The lookahead-safety contract (ADR-0006 §invariant + ADR-0009 §P0-A) makes this non-trivial: ccxt's `binance.fetch_ohlcv()` returns rows whose **timestamp = bar OPEN time** (verified via [ccxt issue #21783](https://github.com/ccxt/ccxt/issues/21783) — the variable is misleadingly named `closeTime` in some internal code paths but is actually open time). For a 1h bar timestamped `2026-05-13T14:00:00Z`, the bar is in-flight from 14:00 to 14:59:59. A backtest with `as_of=2026-05-13T14:30:00Z` MUST exclude that bar — only bars closing at or before 14:00 count.

This is the canonical lookahead bug class for OHLCV systems. It's an implementation detail at the leaf, not a contract concern at the analyst layer (analysts trust their inputs are filtered already, per ADR-0006).

## Decision

### D1: Adapter shape

`CcxtProvider` implements the `DataProvider` Protocol from `hermes_quant/protocol.py`. Single class, multi-exchange via `exchange_id` param (default `"binance"`).

```python
class CcxtProvider:
    def __init__(self, exchange_id: str = "binance", *,
                 sandbox: bool = False, rate_limit: bool = True):
        import ccxt
        self.exchange_id = exchange_id
        self._ex = getattr(ccxt, exchange_id)({
            "enableRateLimit": rate_limit,
            "options": {"defaultType": "spot"},
        })
        if sandbox:
            self._ex.set_sandbox_mode(True)

    def fetch_bars(
        self, symbol: str, asset_class: str, timeframe: str, *,
        lookback_bars: int = 200,
        as_of: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        ...   # see D3
```

### D2: Symbol normalization

ccxt uses **unified symbols** with slash: `"BTC/USDT"`, `"ETH/USDT"`. Internally ccxt maps to wire form (`"BTCUSDT"` for Binance). Operators MUST pass slash form; provider rejects no-slash with `ValueError("symbol must be unified format like 'BTC/USDT'")` rather than silently retrying — silent normalization hides config typos that surface as `BadSymbol` 50 fetches later.

### D3: Pagination + as_of filter — THE money-software-critical path

```python
since_ms = int((as_of - timedelta(seconds=tf_seconds * lookback_bars)).timestamp() * 1000)
all_bars = []
while True:
    chunk = self._ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
    if not chunk:
        break
    all_bars.extend(chunk)
    since_ms = chunk[-1][0] + 1   # canonical ccxt idiom

df = pd.DataFrame(all_bars,
                  columns=["timestamp", "open", "high", "low", "close", "volume"])
df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

df = validate_bars(df)   # drop NaN, dedupe, sort, drop zero-volume

# CRITICAL: as_of filter at the leaf, AFTER validate_bars (per ADR-0009 §P0-A)
if as_of is not None:
    tf_ns = pd.Timedelta(timeframe).value   # ns
    # bar's OPEN time + timeframe = bar CLOSE time; require close <= as_of
    df = df[df["timestamp"] + pd.Timedelta(timeframe) <= as_of]

return df.tail(lookback_bars)
```

The `+ tf_ms` shift converts open time to close time. **A bar is admitted only if it closed at or before `as_of`.** The in-flight bar at `as_of` is dropped. This is the entire lookahead-safety contract for the ccxt path.

### D4: Error taxonomy

ccxt's exception hierarchy is two-tree:

| ccxt exception | hermes_quant decision |
|---|---|
| `NetworkError` (incl. `RequestTimeout`, `ExchangeNotAvailable`) | retry with exponential backoff, max 3 attempts |
| `RateLimitExceeded` | retry after `Retry-After` header or backoff (handled by `enableRateLimit=True`) |
| `OnMaintenance` | retry once after 30s; if still down, raise `RetryableError` |
| `ExchangeError` (incl. `BadSymbol`, `BadRequest`) | NO retry; raise `FatalError` |
| `AuthenticationError` | NO retry; raise `FatalError` |

We define our own `RetryableError` and `FatalError` in `data/base.py` so callers (advisor, autonomous tick) can distinguish without depending on ccxt directly.

### D5: Rate limiting

`enableRateLimit=True` is the canonical ccxt knob. Binance spot allots ~6000 weight/min/IP; klines cost 1-10 weight depending on `limit`. We do NOT implement our own rate limiter on top — ccxt's is correct and battle-tested.

### D6: Test strategy (unit + integration)

| Test type | Strategy | Live network? |
|---|---|---|
| Unit | `FakeCcxtExchange` injected via constructor | NO |
| Integration | `pytest-recording` cassettes (VCR-style) committed to `tests/fixtures/ccxt_cassettes/` | NO (replays cassette) |
| Live smoke | `tests/integration/test_ccxt_live_smoke.py` marked `@pytest.mark.live`; default-skipped in CI | YES — opt-in |

Testnet is rejected as a unit-test source: testnet has no historical-data guarantees, returns can be wildly different from mainnet, and our as_of arithmetic must validate against real-world bar boundaries. Mainnet calls in CI are forbidden — geo-blocking and rate-ban risk.

### D7: Timeframe canonical set

Exposed: `{"1m", "5m", "15m", "30m", "1h", "4h", "1d"}`. Anything outside this set raises `ValueError` at fetch time. This is a deliberate subset of ccxt's larger range — the hermes-quant cadence story (15-min cron tick, 1h analyst lookback, 1d backtests) is fully covered.

### D8: Default exchange = Binance

Charter says crypto-first. Binance has the deepest BTC/USDT order book and the cleanest klines API. Future exchanges (`okx`, `bybit`, `coinbase`) are one-line additions via the `exchange_id` constructor param — no schema changes needed.

### D9: Optional-extras install

`pyproject.toml` gains a `ccxt` extra:
```
[project.optional-dependencies]
ccxt = ["ccxt>=4.0,<5.0"]
```

Operators install as `pip install 'hermes-quant[ccxt]'`. The `CcxtProvider` import is lazy (inside `__init__`) so the provider module loads on Python boxes that didn't `pip install` ccxt — they just can't *instantiate* it.

## Consequences

### Positive
- Charter's MVP recipe ("BTC/USDT, 24/7, clean") is unblocked
- Lookahead-safety contract honored at the leaf — analysts can't cheat even by accident
- Same Protocol as YFinanceProvider so the autonomous orchestrator and advisor don't care which is plugged in
- Rate limiting + retry shape decoupled from upstream policy (binance can change weight costs and we adapt)

### Negative
- ccxt is a 200KB+ dependency; mitigated by optional-extra install
- Binance API drift is a real risk — mitigated by ccxt's update cadence and the `RetryableError`/`FatalError` split (drift typically becomes a `BadRequest`, surfaces as fatal, operator pins ccxt version)
- Testnet rejection means live-smoke tests must be manual; mitigated by the cassette layer for CI
- v0.3 ships Binance only; OKX/Bybit/Coinbase are v0.4

## Cross-references
- ADR-0005 §"crypto provider deferred"
- ADR-0006 §"lookahead invariant"
- ADR-0009 §P0-A "as_of filter at the leaf, after validate_bars"
- ADR-0017 follow-up: implementation in `hermes_quant/data/ccxt_provider.py`
- Charter §"What I'd build first" — BTC/USDT MVP recipe
