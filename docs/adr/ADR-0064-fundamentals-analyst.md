# ADR-0064 — FundamentalsAnalyst Integration

**Status:** Accepted (2026-05-27)
**Wave:** v0.6.1
**Full design:** [docs/design/v0.6.1-fundamentals-analyst.md](../design/v0.6.1-fundamentals-analyst.md) (956 lines)

## Context

Per TauricResearch gap analysis G4: hermes-quant has zero balance-sheet/earnings/cashflow input for equities. The 4 existing analysts (ClassicalTA, Microstructure, Semantic, Kronos) all read OHLCV-derived signals. For a frontier-model-driven equities trader, this is a structural blind spot.

TauricResearch's `FundamentalsAnalyst` reads yfinance fundamentals (P/E, debt-to-equity, FCF, revenue, EPS) and emits a directional view alongside the technical analysts.

## Decision

Add `hermes_quant/analysts/fundamentals.py:FundamentalsAnalyst` conforming to the existing `Analyst` Protocol. Reads yfinance fundamentals via a parquet snapshot cache (24h soft TTL, 7d hard staleness). Emits `AnalystView` with 6 sub-signals reduced to a single direction+confidence:

**Sub-signals (3-of-6 minimum for non-abstain):**
1. **P/E TTM vs sector median** — P/E < sector × 0.7 → BUY signal
2. **P/E forward direction** — forward < trailing → improving earnings → BUY
3. **Debt-to-equity** — D/E < 1.0 → healthy → BUY tilt
4. **Free Cash Flow margin** — positive + growing → BUY
5. **Revenue YoY growth** — > sector median → BUY
6. **EPS surprise** — last 4 quarters beat consensus by >5% → BUY (proxy via forward vs prior consensus when explicit surprise data unavailable)

**Asset-class abstain (Protocol-None, NOT zero-confidence):**
- Crypto: structural N/A → return `None`
- ETFs: aggregate, no balance sheet → return `None`
- FX: no fundamentals → return `None`
- Options: trade-the-underlying → return `None`
- Equity: proceed with sub-signal scoring

**Confidence calibration:**
- Each sub-signal contributes calibrated confidence ∈ [0, 1]
- Aggregated confidence clipped to **[0.20, 0.80]** (looser floor than Kronos's [0.30, 0.85] because partial-data failure is less obviously wrong than path-disagreement)
- BMA filters views with confidence < 0.10 — abstainers don't pollute

## Consequences

**Positive:**
- Closes the largest structural blind spot for equities trading
- yfinance is already wired (no new vendor dependency)
- Snapshot cache keeps hot path fast (~50ms read vs ~3s yfinance fetch)
- Cron-driven cache refresh: fundamentals are quarterly data; daily refresh is more than adequate
- Asset-class abstain is clean Protocol-None (cleaner than Kronos's zero-confidence-view pattern)

**Negative:**
- yfinance fundamentals can be stale or wrong (vs SEC EDGAR ground truth — deferred to v0.7)
- Sector classification needed for sector-median lookups (use yfinance.Ticker.info["sector"])
- Earnings-season concurrency: fundamentals can change mid-tick (mitigation: cache by (ticker, as_of_date))
- Cold-start: first call per ticker takes ~3s (mitigated by daily cron prewarm)

## Implementation Plan

1. `hermes_quant/data/fundamentals_provider.py` — `FundamentalsProvider.fetch(ticker, asof)` reads yfinance, writes parquet to `~/.hermes/quant/cache/fundamentals/yfinance/<TICKER>.parquet`
2. `hermes_quant/analysts/fundamentals.py` — `FundamentalsAnalyst` class with `_classify_symbol_universe`, `_score_pe_ratio`, `_score_de`, `_score_fcf`, `_score_revenue_growth`, `_score_eps_surprise`, `analyze`, `update`, `health`
3. `scripts/quant-fundamentals-prewarm-daily.py` — daily cron at 02:00 PT prewarms cache for all watchlist tickers
4. Cron job registration: `quant-fundamentals-prewarm-daily` schedule `0 2 * * 1-5`
5. Register in `advisor.py:351-353` (default analyst list) and `advisor.py:826-830` (chat-mode)
6. Sector-median sibling cache: `~/.hermes/quant/cache/fundamentals/sector_medians/<SECTOR>.parquet` (refreshed weekly)

## Test Plan

12 unit tests + 2 integration tests + 2 backtest ablations:
- `test_equity_happy_path` (full 6-of-6 sub-signals)
- `test_equity_partial_data_3_of_6_minimum` (abstain on <3 sub-signals)
- `test_etf_abstain_returns_none`
- `test_crypto_abstain_returns_none`
- `test_fx_abstain_returns_none`
- `test_cache_hit_skips_yfinance`
- `test_cache_miss_fetches_yfinance`
- `test_cache_stale_24h_soft_fetches_fresh`
- `test_cache_stale_7d_hard_fetches_fresh`
- `test_yfinance_failure_returns_none_with_health_log`
- `test_sector_median_lookup_works`
- `test_confidence_clipping_to_0_20_0_80`
- `test_e2e_bma_integration` (FundamentalsAnalyst view aggregated alongside others)
- `test_charter_d8_compliance` (no `model.train()` calls; no parameters mutate during inference)
- (backtest) ablation: hermes-quant w/ FundamentalsAnalyst vs w/o on 1Y SPY universe — measure Sharpe delta
- (backtest) ablation: charge fundamentals-only Sharpe (lower bound for whether the signal has any alpha)

## Migration

- v0.6.1: Ship `FundamentalsAnalyst` behind `HERMES_QUANT_FUNDAMENTALS_ENABLED=1` flag, default OFF
- v0.6.2: Flip flag default ON after observation week
- v0.7: Add SEC EDGAR ground-truth path; switch to ground-truth as primary, yfinance as fallback

## Alternatives Considered

- **SEC EDGAR Form 10-Q/K parsing**: deferred to v0.7. EDGAR is slow + complex parsing; yfinance is adequate for v0.6.1 ship.
- **Per-tick fetch (no cache)**: rejected. Fundamentals are quarterly data; per-tick fetches waste API quota and add 3s latency.
- **Trained calibrator (statistical learning of P/E quantiles)**: rejected per ADR-0018 §D8 charter (analyst pool NEVER trains). Use class-constant calibration table as warm-start.

## Related

- ADR-0002 (Analyst Protocol) — the contract this analyst implements
- ADR-0018 (Kronos) — analyst-as-frozen-inferer template
- TauricResearch G4 (gap analysis) — origin of this work
