# ADR-0028: Options data layer — `OptionContract`, `OptionChain`, provider abstraction, greek completion

**Status**: Accepted (2026-05-24), implemented
**Date**: 2026-05-24
**Target**: v0.5.0 (Wave A + Wave B of `docs/plans/2026-05-23-options-daily-retro.md`)
**Extends**: ADR-0005 (data layer Protocol + provider chain)
**Related**: ADR-0002 (analyst protocol — option-aware analyst views), ADR-0027 (options-aware risk gate — consumer of greeks), ADR-0029 (multi-leg paper reactor — consumer of contract metadata)
**Cross-cuts**: ADR-0001 (no look-ahead), ADR-0021 (recipe-level provider selection)

---

## Context

ADR-0005 defines the `DataProvider` Protocol for OHLCV bars across yfinance, ccxt, and alpaca-py. ADR-0009 reserves `asset_class="option"` for v0.2 work. The plan doc lands options as v0.5.0 and the user's Alpaca paper account is Level-3-enabled. R1 (`docs/research/2026-05-23-r1-alpaca-options-api.md`) surveyed Alpaca's options API capabilities, greek coverage, multi-leg order shape, paper assignment behavior, and OCC symbol format.

The options data layer must answer four orthogonal questions:

1. **Type shape** — what `OptionContract`, `OptionLeg`, `OptionChain`, `OptionSnapshot` look like, and how they extend (not parallel) the existing `MarketContext` shape from ADR-0002.
2. **Provider abstraction** — how the existing `DataProvider` Protocol generalizes (or specializes) to options endpoints without forcing equity-only providers (yfinance, ccxt) to implement irrelevant methods.
3. **Greek completion** — Alpaca returns delta/gamma/theta/vega/IV in snapshots when available. Some are stale, some are missing (notably rho per R1 §2). The data layer must guarantee that every leg consumed by the risk gate (ADR-0027) has all required greeks, computing missing values via Black-Scholes (`py_vollib`) when necessary.
4. **Historical chain replay** — Alpaca does NOT provide historical option chain snapshots in any tier (R1 §6). Backtesting against options requires either a paid provider (Polygon.io, ORATS, ThetaData) or synthetic chains. We need an explicit decision on the path forward without blocking v0.5.0 paper-trade.

### Constraints inherited from prior ADRs

- **No look-ahead bias** (AGENTS.md, ADR-0001). Historical chain data must be filtered to `as_of` semantics — at decision time T, the chain we see contains only contracts that existed at T (no future strikes added by retroactive listing, no future expirations not yet listed).
- **Bar data validation at the boundary** (AGENTS.md). The same posture applies: drop NaN greeks rows, drop zero-volume / zero-OI contracts (no liquidity), dedupe, sort. If <2 valid contracts on a chain after filter, raise `ChainQualityError` (don't return empty).
- **Reproducibility** (ADR-0001). Every snapshot replayable from disk; greek completion must be deterministic given the same `(spot, strike, dte, iv)` inputs.
- **Provider chain pattern** from ADR-0005 — fall back deterministically when a primary fails. The chain must extend cleanly to options.
- **Optional dependencies** (AGENTS.md) — `py_vollib` and any options provider sit under the `[options]` extra in `pyproject.toml`. `pip install hermes-quant` without `[options]` does not break.

---

## Decision

### D1 — Core dataclasses in `protocol.py` (extends ADR-0002)

Three new types alongside existing `MarketContext` / `AnalystView`:

```python
# hermes_quant/protocol.py (additions)
from dataclasses import dataclass, field
from typing import Literal, Optional, Mapping
from datetime import date, datetime
import pandas as pd

@dataclass(frozen=True)
class OptionGreeks:
    """Standard 5-greek bundle. All fields nullable for incremental completion."""
    delta:  Optional[float] = None
    gamma:  Optional[float] = None
    theta:  Optional[float] = None  # per-day, sign as quoted (negative for long)
    vega:   Optional[float] = None  # per 1% (=0.01) IV move
    rho:    Optional[float] = None  # per 1% rate move
    iv:     Optional[float] = None  # implied volatility, annualized
    iv_source: Optional[Literal["provider", "computed", "stale_provider"]] = None
    computed_at: Optional[datetime] = None  # when synthesized via py_vollib

@dataclass(frozen=True)
class OptionContract:
    """A single option contract — identity + static metadata, no live quote."""
    symbol: str               # OCC standard, e.g. "NVDA260526C00145000"
    underlying: str           # "NVDA"
    expiration: date          # 2026-05-26
    strike: float             # 145.00
    type: Literal["call", "put"]
    multiplier: int = 100     # standard US equity option
    style: Literal["american", "european"] = "american"
    settlement: Literal["physical", "cash"] = "physical"
    asset_class: Literal["equity_options", "index_options"] = "equity_options"

@dataclass(frozen=True)
class OptionSnapshot:
    """Contract + live quote + greeks at a point in time."""
    contract: OptionContract
    asof: datetime            # UTC; this is the as_of timestamp the snapshot is valid at
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    bid_size: Optional[int]
    ask_size: Optional[int]
    volume: Optional[int]
    open_interest: Optional[int]
    greeks: OptionGreeks
    underlying_spot: float    # for greek computation; sourced atomically with the snapshot
    risk_free_rate: float     # annualized; for greek computation

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None: return None
        return (self.bid + self.ask) / 2.0

    @property
    def bid_ask_pct(self) -> Optional[float]:
        if self.mid is None or self.mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / self.mid

    @property
    def dte(self) -> int:
        return (self.contract.expiration - self.asof.date()).days

@dataclass(frozen=True)
class OptionChain:
    """All listed contracts for one underlying at a point in time.

    Filtered for liquidity at provider boundary (no zero-OI rows, no NaN bid/ask).
    Indexed for fast lookup by (expiration, strike, type).
    """
    underlying: str
    asof: datetime
    underlying_spot: float
    risk_free_rate: float
    snapshots: tuple[OptionSnapshot, ...]   # immutable tuple; chains are read-only views

    def by_dte(self, dte_min: int, dte_max: int) -> "OptionChain": ...
    def by_strike_offset(self, pct: float, dte: int) -> "OptionChain": ...
    def atm_call(self, dte: int) -> Optional[OptionSnapshot]: ...
    def atm_put(self, dte: int) -> Optional[OptionSnapshot]: ...
    def find(self, expiration: date, strike: float, type: str) -> Optional[OptionSnapshot]: ...

@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg position/proposal.

    Used by ADR-0029 paper reactor. The greeks here are point-in-time
    at proposal/fill time; settlement_loop tracks them mark-to-market.
    """
    contract: OptionContract
    side: Literal["buy", "sell"]
    contracts: int            # always positive; sign carried by `side`
    fill_price: Optional[float] = None      # filled by reactor
    greeks_at_decision: Optional[OptionGreeks] = None
```

**Why frozen dataclasses, not Pydantic models for `OptionContract` / `OptionGreeks`?** Hot-path code (the gate, the analyst pool) hits these objects on every tick. Pydantic validation overhead matters at the daemon's tick cadence. The on-disk schemas (proposals, postmortems) use Pydantic; the in-memory hot-path types use frozen dataclasses. Same split as ADR-0002's `MarketContext` (dataclass) vs ADR-0008's signal record (Pydantic-validated JSONL).

### D2 — Provider abstraction extends `DataProvider` Protocol

ADR-0005's `DataProvider` Protocol stays unchanged. Options support is an additive `OptionsCapableProvider` Protocol:

```python
# hermes_quant/data/base.py (additions)
from typing import Protocol, runtime_checkable
from datetime import date

@runtime_checkable
class OptionsCapableProvider(Protocol):
    """Optional capability extension to DataProvider for options data.

    A DataProvider is options-capable iff it implements all methods below.
    Equity-only providers (yfinance, ccxt) do NOT implement this — the
    provider chain dispatches to options-capable providers only when
    asset_class is equity_options.
    """
    name: str
    options_supported: bool   # True; required for runtime introspection

    def list_optionable_underlyings(self) -> list[str]:
        """Underlyings with active option chains."""

    def list_contracts(self, underlying: str,
                       expiration_after: date | None = None,
                       expiration_before: date | None = None,
                       strike_min: float | None = None,
                       strike_max: float | None = None,
                       option_type: Literal["call", "put"] | None = None,
                       ) -> list[OptionContract]:
        """List contracts matching the filter. Returns metadata only, no quotes."""

    def fetch_chain(self, underlying: str, asof: datetime | None = None,
                    *, dte_min: int | None = None, dte_max: int | None = None,
                    use_cache: bool = True) -> OptionChain:
        """Fetch a full chain snapshot. asof=None means 'now'.

        For asof != None on providers without historical chains, raises
        HistoricalChainsUnavailable.
        """

    def fetch_snapshot(self, symbol: str,
                       use_cache: bool = True) -> OptionSnapshot:
        """Single contract snapshot by OCC symbol."""

    def fetch_snapshots_bulk(self, symbols: list[str],
                             use_cache: bool = True) -> list[OptionSnapshot]:
        """Bulk variant for rate-limit efficiency. Per R1 §7."""
```

Concrete implementations:

- `AlpacaOptionsProvider(AlpacaProvider, OptionsCapableProvider)` — extends the existing equity provider with the four options endpoints (R1 §1). Inherits credentials and rate-limit handling.
- `PolygonOptionsProvider(OptionsCapableProvider)` (v0.5.0 stub, v0.6.0 full) — used for historical chains (the gap Alpaca cannot fill).
- No yfinance / ccxt implementations — those providers do not register `OptionsCapableProvider`. Recipe-load fails clearly if a recipe asks for `data_provider: yfinance` with options analysts.

The provider chain (per ADR-0005) extends to options:

```yaml
quant:
  data:
    options_provider_chain:
      - alpaca           # primary (live + paper)
      - polygon          # fallback for historical / when alpaca rate-limited
```

### D3 — Greek completion strategy

R1 §2: Alpaca returns delta, gamma, theta, vega, IV in snapshots when available. Rho is not returned (typical for retail brokers). Some snapshots have stale greeks (last-trade-stale rather than refreshed-on-quote).

Strategy:

1. **Trust provider greeks when present and fresh** (`asof` of the greek block within 60s of the snapshot's `asof`).
2. **Synthesize missing greeks via py_vollib** Black-Scholes when:
   - The greek field is `None`.
   - The greek field is provider-stale (>60s old vs the snapshot quote).
   - The IV is missing entirely (compute IV first from `mid`, then derive other greeks).
3. **Fail closed on impossible inputs**: if `mid <= 0`, `dte <= 0`, or `spot <= 0`, raise `GreekComputationError`. Do not return zero-greeks; the gate's fail-closed posture (ADR-0027 D6) silences anyway.
4. **Mark provenance** in `OptionGreeks.iv_source`: `"provider"` (provider-fresh), `"computed"` (synthesized), `"stale_provider"` (provider returned but >60s old; we re-computed but kept their value as a sanity check).

```python
# hermes_quant/data/greeks.py
from py_vollib.black_scholes_merton.greeks.analytical import (
    delta as bs_delta, gamma as bs_gamma, theta as bs_theta,
    vega as bs_vega, rho as bs_rho,
)
from py_vollib.black_scholes_merton.implied_volatility import implied_volatility

def complete_greeks(snap: OptionSnapshot, *,
                    stale_threshold_seconds: int = 60) -> OptionSnapshot:
    """Returns a new OptionSnapshot with all greeks populated.

    Idempotent: if all greeks present and fresh, returns input unchanged.
    Pure: same (snap inputs) → same outputs.
    """
    # ... iv backfill, then delta/gamma/theta/vega/rho via Black-Scholes
```

The completion happens at the provider boundary — `AlpacaOptionsProvider.fetch_chain` runs `complete_greeks` over each snapshot before returning. Downstream consumers (analysts, gate) always see complete greeks.

**Determinism**: py_vollib is pure-Python with closed-form formulas. Given the same `(spot, strike, dte, iv, r, q=0)` inputs, output is byte-identical. Replay works.

**Cost**: ~20µs per leg per Black-Scholes evaluation (py_vollib in CPython). Bulk-vectorize via numpy for chains with >100 contracts.

**Caveat — American-style early exercise**: py_vollib's BSM is European. American-style options (US equity options) have early-exercise premium that BSM under-prices, especially deep ITM puts and dividend-bearing calls near ex-div. For greeks, the difference is small (<1% for typical 30–90 DTE OTM strikes); for far-ITM short-DTE positions it can matter. The data layer flags these contracts (`OptionGreeks.iv_source = "computed_european_approximation"` for American-style with `|moneyness| > 0.10` and `dte < 30`). For v0.5.0 we accept the approximation; v0.6.0 may add Bjerksund-Stensland or Barone-Adesi-Whaley if retro shows it matters.

### D4 — Historical chain replay (the big gap)

**Decision**: For v0.5.0, backtest support for options is **deferred** with an explicit fallback path documented here. Live + paper trading work end-to-end against Alpaca; the daily picker, the multi-leg reactor, the postmortem loop all function without historical chains. The gap is: we cannot run a 5-year walk-forward CV on a covered-call recipe without paid historical data.

Three options surveyed (R1 §6):

| Provider | Coverage | Cost | API | Fit |
|---|---|---|---|---|
| Polygon.io options | 2014+ NBBO + greeks; per-minute aggs | $499/mo unlimited | REST + flat files | Best fit; well-documented Python SDK; matches our DataProvider shape with light wrapping. |
| ORATS | 2007+ EOD chain + IV surface; 1m bars premium | ~$300/mo | REST + S3 flat files | Strong on IV surface analytics. Less GitHub-mindshare. Format-quirks. |
| ThetaData | 2012+ NBBO + greeks; intraday | $80/mo (basic) - $200/mo | REST + WebSocket | Cheapest. Quality reportedly comparable to Polygon. |
| **CBOE DataShop** | Full venue; intraday | $$$$$ | Flat-file delivery | Overkill; bulk-download only. |
| **Synthetic chains** | Anytime; from equity bars + IV surface assumption | $0 | In-process | Useful for sanity checks; NOT trustworthy as primary backtest input. |

**Recommendation for v0.6.0**: `PolygonOptionsProvider` as the primary historical provider. Polygon is the most-developer-friendly option, has a stable Python SDK, and its NBBO + greeks coverage is the closest match to Alpaca's live-data shape (so the same `OptionChain` schema serializes through both). Cost ~$499/mo is real but defensible once paper-trade graduates to backtest-validated parameter tuning.

**Stop-gap for v0.5.0**: A `SyntheticChainProvider` ships in `hermes_quant/data/synthetic_options_provider.py` and produces approximate chains from historical equity OHLCV plus a flat-IV-surface assumption (`iv = realized_30d_vol × 1.1` as a baseline; skew via a parametric model). This is **explicitly insufficient** for any decision-quality backtest. It exists for:

1. CI smoke tests that exercise the pipeline against any-historical-data without paid provider access.
2. Pipeline development before Polygon is wired.
3. The no-look-ahead CI gate (`shuffle_timestamps_test`) — which the synthetic provider satisfies trivially because it computes from past bars.

Recipe-level enforcement: any recipe with `live_allowed: false` AND `min_settlements_for_charter_gate >= 30` will refuse to graduate to live without a non-synthetic provider chain. The retro loop (ADR-0026) gets to flag "30 paper trades on synthetic backtest baseline" as a `MAJOR` finding requiring a real provider before live promotion.

### D5 — Point-in-time `as_of` semantics for chains (no look-ahead)

Per AGENTS.md: at decision time T, the chain returned by `fetch_chain(underlying, asof=T)` contains:

- Only contracts that were **listed at T** (`contract.created_at <= T`).
- Only contracts that had **not yet expired at T** (`contract.expiration >= T.date()`).
- Quotes (`bid`, `ask`, `last`) and greeks reflect the state at T, not later updates.
- The `OptionChain.asof` field is the canonical timestamp; downstream consumers (analysts, gate) MUST filter their reasoning to `asof`.

For Polygon's historical data, this is a flat-file/aggregation property — pull NBBO snapshots at-or-before T. For Alpaca live data, this is the natural "now" semantics. For synthetic chains, the generator deterministically computes from historical bars `<= T`.

The CI gate `tests/test_no_lookahead.py` extends `shuffle_timestamps_test` to options analysts: the methodology screener (R4) is fed shuffled-timestamp chains and must perform no better than chance. Existing equity test machinery extends with one new fixture.

### D6 — Rate-limit budgeting and caching

Per R1 §7: ~200 requests/min on paper across most endpoints. Bulk endpoints preferred when available.

```yaml
quant:
  data:
    alpaca:
      options:
        rate_limit_buffer_pct: 0.50      # use ≤50% of provider limit (~100 req/min)
        snapshot_cache_ttl_seconds: 15   # paper; live drops to 5s
        chain_cache_ttl_seconds: 60
        bulk_snapshot_threshold: 5       # batch ≥5 contracts via bulk endpoint
```

Cache layer is in-memory (not on-disk) for live snapshots — TTL bounded; replay-from-disk uses the recorded snapshots from the tick journal, not cache.

For the daily picker (ADR-0030), a single pre-market cron pull of the entire universe's chains burns ~100–300 requests (universe-size dependent); fits comfortably in the 50% budget.

### D7 — On-disk schema for chain replay

Every `fetch_chain` call appends one normalized record to `~/.hermes/quant/option_chains/<underlying>/<YYYY-MM-DD>.parquet`. Schema:

```
underlying           string
asof                 timestamp[us, tz=UTC]
underlying_spot      float64
risk_free_rate       float64
contract_symbol      string
expiration           date32
strike               float64
type                 string  # "call" | "put"
bid                  float64
ask                  float64
last                 float64
volume               int32
open_interest        int32
delta                float64
gamma                float64
theta                float64
vega                 float64
rho                  float64
iv                   float64
iv_source            string  # "provider" | "computed" | "stale_provider"
```

Parquet for compactness; one file per underlying per UTC date. Backtest replay reads from these files; provider abstraction is bypassed in replay mode.

This is the same shape Polygon flat files use, modulo IV-source tagging; migration to PolygonOptionsProvider is read-only against these files.

---

## Consequences

### Positive

- **Type shape is options-native, not equity-with-options-flag.** Code in the gate and analyst pool reasons about `OptionContract`, `OptionSnapshot`, `OptionLeg` directly — no string parsing of OCC symbols on the hot path.
- **`OptionsCapableProvider` is additive.** Equity-only providers continue working; the recipe-load layer raises a clear error if they're paired with options analysts.
- **Greek completion is a hard guarantee at the provider boundary.** Downstream code (gate, analysts) never has to defensive-check for missing greeks. Fail-closed on impossible inputs.
- **`as_of` semantics enforced.** The no-look-ahead CI gate extends to options. Replay determinism is preserved.
- **On-disk parquet schema matches Polygon's flat files.** When we land Polygon in v0.6.0, migration is reading from S3 instead of recording from Alpaca; no schema rework.

### Negative

- **Historical chain backtest is deferred.** v0.5.0 paper-trades against Alpaca live; backtests use synthetic chains (insufficient for decision-quality CV). This is a real gap; the retro loop is expected to flag it.
- **py_vollib is pure-Python.** ~20µs per leg per greek synthesis. For 200-contract chains × 5 greeks × 50 underlyings = ~50ms compute budget per pre-market scan. Acceptable; vectorize-with-numpy if it grows.
- **American-style early-exercise approximation.** BSM under-prices American-style premium near ex-div / deep-ITM. Acceptable for v0.5.0's strategy mix (typical 30–60 DTE, OTM); flagged.
- **One more optional dep (`py_vollib`).** Sits under `[options]` extra. Acceptable.
- **Polygon at $499/mo is a real budget commitment.** ThetaData at $80/mo is the cheaper alternative; v0.6.0 spike will A/B-test data quality before committing.

---

## Alternatives Considered

### A1: Treat options as just-another-asset-class within `MarketContext`

I.e., reuse the existing `MarketContext` shape and stuff option metadata into `extras`. Rejected. The hot-path types should make options first-class. The gate evaluates net-Greeks; treating that data as untyped `extras` creates 50+ runtime KeyError sites.

### A2: Always synthesize greeks from scratch (ignore provider greeks)

Rejected. Provider greeks are calibrated to broker-quoted IV, which is what the broker's risk system uses. Always-synthesize would create discrepancies between our internal risk view and the broker's BPR calculation. Trust-with-staleness-check is the right balance.

### A3: Always trust provider greeks (no synthesis)

Rejected. Per R1 §2: rho is not returned. Stale rows happen. Some chains return partial greek payloads (delta only). Fail-closed on missing data is the gate's posture; the data layer must give the gate complete inputs.

### A4: Skip `OptionLeg`, just use lists of `OptionContract` + side info

Rejected. Multi-leg reasoning (verticals, iron condors, wheel state) needs `(contract, side, contracts, fill_price, greeks_at_decision)` as a unit; representing that as parallel arrays is bug-prone.

### A5: Use Polygon as the live provider too (not just historical)

Tempting (one provider, simpler abstraction). Rejected for v0.5.0: the user's broker is Alpaca; using Polygon for live data while ordering through Alpaca creates a quote/fill discrepancy at fill time. Alpaca-for-live, Polygon-for-historical is the correct split.

### A6: Build a custom IV surface model and skip py_vollib

Rejected. Py_vollib is field-tested, MIT-licensed, deterministic. A custom IV surface would buy us nothing at v0.5.0 and be a calibration footgun. Deferred to v0.7.0 (vol-surface analyst class) at earliest.

### A7: SQLite for chain storage instead of parquet

Rejected. Chain data is wide (50+ contracts × 15 columns × N-snapshots-per-day) and read-mostly. Parquet's columnar format and on-disk size dominate SQLite for this access pattern. Parquet also matches Polygon's distribution format.

---

## Open Questions

1. **What's the right TTL for snapshot cache during a pre-market scan?** R1 §7 suggested 5–15s. Too short and we hit the rate limit on universe scans; too long and we trade on stale quotes. Default 15s in `options_default`; retro can propose tuning.

2. **Does Alpaca return historical end-of-day option bars?** R1 says "no" for chain snapshots, but EOD-aggregated historical data may exist for individual contracts (after their expiration). Worth a `r1-followup` probe before committing to Polygon.

3. **How do we handle option splits / adjustments?** US equity options adjust on splits and special dividends; the OCC symbol changes (`AAPL` may temporarily list `AAPL1` post-split). Provider should return adjusted contracts; the data layer needs to recognize and not treat post-adjustment contracts as new strikes. Defer to v0.5.1 patch.

4. **Index options (SPX, RUT) vs equity options (AAPL, NVDA)?** Settlement is cash vs physical, AM vs PM expiration, european vs american style. Schema supports both via `OptionContract.settlement` + `OptionContract.style`; analyst implementations may need explicit gating. Defer until first index-options analyst lands.

5. **Should `complete_greeks` cache its synthesis output?** Same `(spot, strike, dte, iv)` tuple recurs across nearby snapshots. ~20µs is cheap but on a 200-contract chain × 5 greeks it's 20ms. LRU cache by hashed input may help; defer until profiling shows it matters.

6. **`OptionChain.snapshots` is a tuple — should we also expose a pandas DataFrame view?** Some analysts (methodology screener) think in tabular ops. Provide a `chain.to_frame()` helper. Land if R4 methodology screener finds it useful in implementation.

---

## Implementation Sketch

```
hermes_quant/
├── protocol.py                    # additions: OptionContract, OptionGreeks, OptionSnapshot,
│                                  #            OptionChain, OptionLeg
├── data/
│   ├── base.py                    # additions: OptionsCapableProvider Protocol
│   ├── alpaca_provider.py         # existing + extends OptionsCapableProvider
│   ├── alpaca_options.py          # NEW: chain/snapshot fetchers, OCC symbol helpers
│   ├── greeks.py                  # NEW: complete_greeks via py_vollib
│   ├── synthetic_options_provider.py  # NEW: stop-gap for backtest pre-Polygon
│   ├── polygon_options_provider.py    # NEW (stub): historical-chain provider for v0.6.0
│   └── option_chain_replay.py     # NEW: parquet read/write for backtest

pyproject.toml:
  [project.optional-dependencies]
  options = [
    "py_vollib>=1.0.4",
    "pyarrow>=15.0",     # parquet
  ]
  options-historical = [
    "hermes-quant[options]",
    "polygon-api-client>=1.13",
  ]
```

OCC symbol helpers in `alpaca_options.py`:

```python
def occ_symbol(underlying: str, expiration: date, type: Literal["call", "put"],
               strike: float) -> str:
    """OCC standard format: TICKERYYMMDD{P/C}STRIKE (zero-padded ×1000).

    >>> occ_symbol("NVDA", date(2026, 5, 26), "call", 145.00)
    'NVDA260526C00145000'
    """
    yymmdd = expiration.strftime("%y%m%d")
    pc = "C" if type == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{yymmdd}{pc}{strike_int:08d}"

def parse_occ_symbol(symbol: str) -> OptionContract:
    """Inverse: parse an OCC symbol back into typed metadata."""
```

Recipe wiring:

```yaml
# ~/.hermes/quant/recipes/socalminh-covered-call.yaml
data_provider: alpaca
data_provider_config:
  options_chain: true
  options_provider_chain: ["alpaca", "polygon"]   # primary, fallback
```

---

## Test Plan

### Unit tests (deterministic)

1. **OCC round-trip** — `parse_occ_symbol(occ_symbol(u, e, t, s))` returns equivalent `OptionContract` for ≥50 fixture cases.
2. **Greek completion idempotence** — `complete_greeks(complete_greeks(snap)) == complete_greeks(snap)`.
3. **Greek completion against py_vollib reference** — for 100 fixture `(spot, strike, dte, iv)` tuples, verify all 5 greeks match py_vollib closed-form within 1e-6.
4. **Greek fail-closed on impossible inputs** — `complete_greeks` raises `GreekComputationError` for `mid <= 0`, `dte <= 0`, `spot <= 0`. Does NOT return zero-greeks.
5. **Stale-greek detection** — provider returns greeks with `provider_asof - snapshot_asof > 60s` → `iv_source = "stale_provider"`, recomputed values used.
6. **Chain filtering** — `chain.by_dte(21, 36)` returns only snapshots with DTE in `[21, 36]`. `chain.by_strike_offset(0.05, 30)` returns ATM±5% strikes for the closest-to-30-DTE expiration.
7. **`as_of` filter** — feeding `fetch_chain(underlying, asof=T)` against a parquet replay returns only contracts where `created_at <= T` and `expiration >= T.date()`.

### Provider-shape conformance

8. **`OptionsCapableProvider` Protocol** — `isinstance(AlpacaOptionsProvider(), OptionsCapableProvider) is True`. `isinstance(YfinanceProvider(), OptionsCapableProvider) is False`.
9. **Recipe-load failure on mismatch** — recipe with `data_provider: yfinance` and `analysts: [methodology_screener]` (an options analyst) fails to load with a clear `RecipeProviderMismatchError`.

### Integration (live network, marker `requires_network`)

10. **Alpaca paper smoke** — `fetch_chain("NVDA")` returns ≥20 contracts; all snapshots have complete greeks (post-completion); `bid_ask_pct` median ≤10% on liquid expirations; sample contract round-trips through `parse_occ_symbol`.
11. **Bulk vs single performance** — `fetch_snapshots_bulk(["NVDA260526C00145000", ...×10])` is faster than 10 sequential `fetch_snapshot` calls AND consumes ≤2 rate-limit units (vs ≤10 sequentially).

### No-look-ahead CI gate

12. **Methodology screener on shuffled chains** — `shuffle_timestamps_test(MethodologyScreenerAnalyst, fixture_chain)` produces accuracy within `[0.45, 0.55]` (chance level).

### Parquet replay

13. **Round-trip replay** — record 10 chain snapshots via `record_chain(chain)`, read back via `replay_chain(underlying, asof=T)` for each T, verify byte-equal `OptionChain` returned.
14. **Replay respects `as_of`** — recorded snapshots at T1, T2, T3; `replay_chain(underlying, asof=T2)` returns the T2 snapshot only, not T3 even if it's on disk.

### Synthetic provider (sanity, not decision-quality)

15. **Synthetic chain shape** — `SyntheticChainProvider().fetch_chain("AAPL", asof=date(2024, 6, 1))` returns ≥20 strikes per expiration, 4–8 expirations covering 7–365 DTE, all greeks present and within sanity ranges (`0 <= |delta| <= 1`, `gamma >= 0`, etc.).
16. **Synthetic IV-surface determinism** — same inputs → byte-identical chain.

---

## References

- `docs/research/2026-05-23-r1-alpaca-options-api.md` — endpoints inventory (R1 §1), greeks coverage (§2), multi-leg shape (§3), assignment behavior (§4), OCC format (§5), historical-chain gap (§6), rate limits (§7). **This ADR is the engineering response to R1.**
- ADR-0005 — `DataProvider` Protocol; `OptionsCapableProvider` is the additive extension.
- ADR-0027 — gate consumes complete greeks; this ADR is upstream of that consumption.
- ADR-0029 — paper reactor consumes `OptionLeg` and `OptionContract`.
- ADR-0001 §Reproducibility — chain replay-from-parquet requirement.
- AGENTS.md "Bar data validation at the boundary" — extended verbatim to chain validation.
- AGENTS.md "No look-ahead bias" — extended to options via `as_of`-filtered chains and `shuffle_timestamps_test` on methodology analysts.
- `py_vollib` — Black-Scholes-Merton greek library; MIT-licensed.
- Polygon.io options: <https://polygon.io/docs/options> (recommended for v0.6.0 historical).


---

## Amendment 2026-05-24 -- D5/D7 parquet replay must enforce fetched_at <= asof at load time

**Source**: `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` P0-5
**Reviewer**: DeepSeek-V4-Pro
**Status**: Adopted

### What changed

D5 (this ADR, lines 268-275) defines `as_of` semantics for live `fetch_chain` calls but does not constrain what the parquet replay layer (D7, lines 300-327) returns. A row whose greeks were computed from chain data fetched AFTER `asof` (e.g., backfill-fix run that re-stamped `asof` but kept the original `fetched_at`) IS look-ahead-contaminated, and the existing CI gate `shuffle_timestamps_test` does not catch it because the gate operates on bar timestamps, not provenance timestamps. The replay layer must enforce the strict ordering `row.fetched_at <= row.asof` at load time.

D5 amendment -- replay-time filter:

```python
def replay_chain(underlying: str, asof: datetime, ...) -> OptionChain:
    """Read a snapshot from parquet for `underlying` at `asof`.

    Drops any row where `fetched_at > asof`. This is the load-time
    enforcement of the no-look-ahead invariant on stored greeks.
    Dropped rows are counted via the `quant_doctor` counter
    `option_chain_replay.lookahead_drops`; non-zero counts at
    daemon-runtime indicate a data integrity defect that should be
    investigated before any decision uses the chain.
    """
    df = pa.parquet.read_table(_path_for(underlying, asof.date())).to_pandas()

    # Hard filter: contracts and greeks visible at `asof` only.
    pre_count = len(df)
    df = df[df["fetched_at"] <= asof]
    df = df[df["asof"] <= asof]                 # belt + suspenders
    dropped = pre_count - len(df)
    if dropped > 0:
        _doctor_counter("option_chain_replay.lookahead_drops").inc(dropped)
        log.warning(
            "replay_chain dropped %d look-ahead rows for %s asof=%s",
            dropped, underlying, asof,
        )

    return _rows_to_chain(df, asof=asof)
```

D7 amendment -- parquet writer must stamp BOTH columns:

The on-disk parquet schema (lines 300-306) currently lists `asof` (the snapshot's logical timestamp). Add an explicit `fetched_at` column whose semantics are "the wall-clock UTC instant when the provider returned this row's greeks/IV/quote." For Alpaca live: `fetched_at = response.headers['Date']` parsed to UTC. For synthetic provider: `fetched_at = chain.asof` (no asymmetry possible). For replay-of-replay (a replay output written back to a new parquet for downstream): `fetched_at` is preserved from the source row, NOT updated to the re-write time.

Updated schema fragment:

```
asof                 timestamp[us, tz=UTC]   # logical decision time the row is valid at
fetched_at           timestamp[us, tz=UTC]   # wall-clock when provider returned this row
provider             string
underlying           string
... (rest unchanged from D7)
```

Invariant: `fetched_at <= asof` MUST hold for every row at write time. Writer asserts this before flushing; assertion failure is a `DataIntegrityError` (not silently dropped) so the operator finds out at write time, not silently at replay time. Replay-time filter is defense in depth.

`quant_doctor` exposes:

- `option_chain_replay.lookahead_drops` (counter, per-underlying) -- non-zero indicates contaminated parquet on disk.
- `option_chain_writer.lookahead_assertions` (counter) -- non-zero indicates a writer bug or provider-clock skew.

Unit tests (additions to existing parquet replay block, lines 485-488):

- `test_replay_drops_lookahead_rows`: write a parquet with 5 rows where row 3 has `fetched_at = asof + 60s` (synthetic violation); call `replay_chain(asof=...)` requesting the asof of row 3; assert row 3 is filtered, doctor counter incremented by 1, returned chain has 4 rows.
- `test_writer_rejects_lookahead`: attempt to call `record_chain` with a row where `fetched_at > asof`; assert `DataIntegrityError` raised at write time, parquet not flushed.
- `test_replay_filter_is_idempotent`: replay-of-replay-write preserves `fetched_at`, doesn't update to wall-clock.
- `test_doctor_exposes_lookahead_counter`: `quant_doctor` JSON output includes the `option_chain_replay.lookahead_drops` key with a non-negative integer value.

### Why

The synthesis (P0-5) flagged this as a no-look-ahead-bias bypass: the existing CI gate operates on bar timestamps and doesn't see the parquet replay's stored greeks. A backfill or repair script that updates `asof` but not `fetched_at` (or vice versa) silently injects contamination, and downstream backtests then "validate" strategies on data they would not have had at decision time. The fix is two layers: writer-time assertion (catches it early) and replay-time filter (catches anything that slipped past). The doctor counter surfaces residual contamination so an operator notices before live capital sees the bad signal.

### Affected sections of this ADR

- D5 "Point-in-time as_of semantics for chains" (lines 268-275) -- the canonical-`asof` rule extends with a load-time filter.
- D7 "On-disk schema for chain replay" (lines 300-306) -- schema gains `fetched_at` column with strict-ordering invariant.
- Test plan "Parquet replay" block (lines 485-488) -- adds the four tests above.
- `quant_doctor` surface (cross-cutting; ADR-0007) -- exports two new counters.

---

## Amendment 2026-05-24 -- Alpaca live probe corrections (greeks endpoint, rho present, coverage 58.7 percent)

**Source**: `docs/research/2026-05-24-r5-alpaca-live-probe.md` R1 corrections section
**Reviewer**: Live probe (orchestrator, paper credentials)
**Status**: Adopted

### What changed

R1 (`docs/research/2026-05-23-r1-alpaca-options-api.md`) made two factual claims about Alpaca's options greeks API that the 2026-05-24 live probe falsified. This ADR's D3 (provider greeks rule) and D4 (historical chain replay) reference R1 and inherited those errors. Corrections:

**Correction 1 -- greeks endpoint identity:**

R1 said greeks are returned via `OptionSnapshotRequest` (the snapshot endpoint). Verified live 2026-05-24: that endpoint returns OHLCV bars and bid/ask quotes only, NO greeks, NO IV. The greeks are returned via `OptionsChainRequest` (alpaca-py SDK) -- `GET /v1beta1/options/snapshots/{underlying}` augments quotes with computed greeks server-side, but only when invoked via the chain endpoint (underlying-keyed), not the per-symbol snapshots endpoint.

Implication: ADR-0028 D3 implementation language must reference `OptionsChainRequest` (the SDK class) or the underlying-keyed REST path, NOT `OptionSnapshotRequest`. The provider implementation must construct chain requests, not aggregate per-contract snapshot calls (the latter would not return greeks even in principle).

**Correction 2 -- rho IS in the greeks dict:**

R1 said rho was missing. Verified live 2026-05-24 with NVDA chain:

```python
greeks = OptionsGreeks(
    delta=-0.9954,
    gamma=0.0005,
    rho=-0.0957,           # present
    theta=0.0146,
    vega=0.0052,
)
implied_volatility = 0.6422
```

Implication: ADR-0028 D3 should not list rho as a "synthesize-always" field. Greeks fan-out is now `delta, gamma, theta, vega, rho, implied_volatility` from Alpaca's chain endpoint, falling back to py_vollib only when the provider returns `None` for a given contract.

**Correction 3 -- coverage is 58.7 percent (not "all liquid"):**

Live probe of NVDA's 4884-contract chain returned greeks on 2865 contracts = 58.7%. The remaining 41.3% are deep OTM/ITM contracts with unstable bid-ask where the provider's IV solver does not converge. ADR-0028 D3 must therefore retain a mandatory py_vollib fallback for the 41.3% no-greeks tier -- this is not optional. The fallback uses mid-quote IV and Black-Scholes-Merton; the resulting `iv_source` is `py_vollib_european_approximation` (per Grok P2 nit and the existing P1-E discussion).

Updated D3 paragraph (replaces lines 210 and surrounding):

> Greeks are sourced from the Alpaca chain endpoint (`OptionsChainRequest` via alpaca-py SDK, or `GET /v1beta1/options/snapshots/{underlying}` via REST). Coverage observed live 2026-05-24 against NVDA: 58.7% of contracts returned `(delta, gamma, theta, vega, rho, implied_volatility)`; 41.3% returned `None` for the greek block (deep OTM/ITM with unstable bid-ask). For the no-greeks tier, synthesize via py_vollib using mid-quote IV and Black-Scholes-Merton; tag the synthesized rows with `iv_source = "py_vollib_european_approximation"`. py_vollib is in-process (~0.1ms/contract); no rate-limit concern. American-vs-European approximation gap is acknowledged for early-exercise-eligible contracts; the flag distinguishes them from provider-native greeks for downstream consumers (the gate, the backtester, `quant_doctor`).

### Why

The original ADR-0028 was authored against R1's documentation pass, which was not live-verified. The 2026-05-24 probe (paper credentials, real API) found two material errors and one quantitative correction. Leaving the original wording would cause the implementation wave to point at the wrong endpoint and to omit a mandatory fallback for 41.3% of the chain -- both would manifest as silent decision-quality regressions (gate makes choices on missing greeks) before any live capital is at risk, but the test fixtures might not catch it if they only exercise the liquid tier.

### Affected sections of this ADR

- D3 "Trust provider greeks when present and fresh" (line 210) -- endpoint identity corrected; rho moved out of synthesize-always; coverage reality (58.7%/41.3%) added; py_vollib mandatory for the no-greeks tier.
- D5 "Point-in-time as_of semantics" -- unaffected by this correction.
- Test plan "Stale-greek detection" (line 467) -- clarify that "stale" applies to the 41.3% tier only; the 58.7% native-greek tier is computed at quote time and not "stale" in the wall-clock sense.
- References block -- `docs/research/2026-05-24-r5-alpaca-live-probe.md` is added as the live-verification source for R1's corrections.
