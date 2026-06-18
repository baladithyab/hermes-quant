---
status: proposed
date: 2026-06-17
deciders: [codeseys]
consulted: [deep-work-loop session 2026-06-17 (OpenBB + TradingAgents source research wf_e0a5b0b9, source-grounded)]
amends: null
supersedes: null
---

# ADR-0100: OpenBB as a host-blind Perception provider + TradingAgents source parity

> Operator asks (2026-06-17): "integrate OpenBB into AEGIS" + "pull all the information from all
> the sources mentioned in TradingAgents (the framework that inspired our PDR)." This ADR decides
> HOW: OpenBB enters as a default-OFF, **as-of-pinned** perception PROVIDER behind the existing
> `hermes_quant/data/` base — never a new seam — feeding the SAME AnalystView contract; and the
> TradingAgents source set is closed where AEGIS genuinely lacks it. The no-lookahead rail is the
> load-bearing constraint: a latest-only endpoint that can't be pinned to the decision instant is
> REJECTED at the boundary, not silently used.

**Cites:** [ADR-0002](ADR-0002-analyst-protocol.md) (the AnalystView seam — the core is blind to
which source produced a view), [ADR-0079](ADR-0079-perception-decision-reaction-architecture.md)
(the Perceive stage), [ADR-0085](ADR-0085-ledger-authority-and-state-derivation.md) +
no-lookahead invariant (asof = decision/publication time), [ADR-0095](ADR-0095-single-contract-source-of-truth.md)
(the contract the views conform to).

---

## Context and Problem Statement

A source-grounded research pass (wf_e0a5b0b9 — read the actual TradingAgents `dataflows/` toolkit
+ the OpenBB Platform docs) produced the gap analysis. Two findings reframed the work:

**AEGIS Perceive already covers MOST of TradingAgents** (verified against code): price/OHLCV
(yfinance_provider, asof-pinned), technical indicators (classical_ta — same SMA/EMA/MACD/RSI/BB/ATR
family), fundamentals (fundamentals_provider, balance/income/cashflow), ticker + macro news
(catalyst/ingest Google-News RSS), Reddit social (catalyst/social RSS), Google Trends, the macro
calendar (catalyst/calendar BLS/FOMC), AND **SEC EDGAR Form-4 insider transactions**
(evidence/adapters/form4.py). So this is gap-CLOSING, not a rebuild.

**The genuine gaps are narrow + high-value:**
- *TradingAgents sources AEGIS lacks:* FRED economic series (FEDFUNDS, DGS2/10/30, T10Y2Y yield
  curve, CPI, UNRATE, M2, VIX — a dedicated macro tool, asof-honest with a lookback), Alpha Vantage
  NEWS_SENTIMENT (per-article scored sentiment), StockTwits cashtag stream (a 2nd social source),
  Polymarket prediction-market probabilities (forward-looking event odds), and AV *structured*
  fundamentals with filter-at-source date honesty (AEGIS fundamentals is yfinance-only — 27 refs).
- *What OpenBB UNIQUELY adds (beyond both):* analyst forward **estimates** + price targets
  (obb.equity.estimates.*), **institutional 13-F ownership** (obb.equity.ownership.institutional),
  as-of-capable historical options chains (obb.derivatives.options.chains via intrinio's date param),
  broad macro (obb.economy.indicators fred/oecd/imf), and — the load-bearing one —
  **multi-provider OHLCV/fundamentals redundancy** (fmp as a 2nd tier behind the fragile
  yfinance scrape path).

The risk that governs the whole design: a provider that returns "latest" silently injects
FUTURE data into a backtest/decision (lookahead leak straight into the gate). OpenBB has both
asof-capable endpoints (take start_date/end_date/date) and latest-only ones (quote, cboe/yfinance
chains, yfinance consensus). The boundary must distinguish them.

## Decision Drivers

- **One seam, many sources** (ADR-0002): a new data source must enter behind `data/base.py` and
  produce an AnalystView the core can't distinguish from any other — never a new perception seam.
- **No-lookahead is non-negotiable.** Every call asof-pinned; latest-only endpoints rejected at
  the boundary in any non-live context. This is the rail, not a nicety.
- **Don't duplicate what AEGIS has.** Close the narrow real gaps (FRED, AV-sentiment, StockTwits,
  Polymarket, estimates, 13-F, OHLCV redundancy), not the large overlap.
- **Default-OFF + eval-gated** (repo convention); a heavy optional dependency (openbb) must not be
  imported on the live path unless the flag is on.

## Decision Outcome

**OpenBB enters as a host-blind perception PROVIDER behind `hermes_quant/data/base.py`**, default-OFF
behind `HERMES_QUANT_OPENBB`, asof-pinned on every call.

1. **`hermes_quant/data/openbb_provider.py`** implements the existing `DataProvider` Protocol:
   `fetch_bars` (OHLCV, `end_date=asof`) + a new `fetch_snapshot(symbol, data_type, asof)` for non-bar
   types. Registered in `vendor_routing.py` as an `openbb` vendor — **fmp as the 2nd OHLCV tier
   behind yfinance** (closing the scrape-reliability gap, the highest-value single win).
2. **Per-category snapshot providers** (each `fetch_snapshot()`, registered under its category):
   OpenBBFundamentals / OpenBBEstimates / OpenBBInsider / OpenBBInstitutional / OpenBBMacro /
   OpenBBNews. Each **post-filters returned rows on `date`/`period_ending`/`filing_date` ≤ asof
   unconditionally** — the leaf no-lookahead guard (mirrors what base.py already enforces for OHLCV).
3. **Latest-only endpoints HARD-REJECTED at the boundary** in any non-live context
   (obb.equity.price.quote, obb.derivatives.options.chains via cboe/yfinance, obb.equity.estimates.consensus
   via yfinance) — an explicit guard in openbb_provider.py, not a silent pass.
4. **Feeds the EXISTING analysts** (fundamentals gets an AV/fmp filter-at-source 2nd vendor; news
   feeds catalyst/ingest; insider feeds evidence/adapters beside the EDGAR form4 path) and **enables
   new ones** (InsiderAnalyst, EstimatesAnalyst) that produce the SAME AnalystView — the core stays
   source-blind.
5. **The non-OpenBB TradingAgents gaps** (FRED macro tool, AV NEWS_SENTIMENT, StockTwits, Polymarket)
   are closed as direct providers where OpenBB lacks an asof-honest equivalent — FRED via OpenBB's
   obb.economy where pinnable, the rest as small RSS/REST providers mirroring catalyst/social.py.

### Consequences

- **Positive:** AEGIS Perceive reaches TradingAgents source parity + adds estimates/13-F/redundancy,
  all behind one seam producing one contract — the committee sees richer evidence without the core
  knowing the source changed.
- **Positive:** the multi-provider OHLCV/fundamentals redundancy removes the single-point yfinance
  scrape fragility (a real reliability win independent of the new data types).
- **Positive:** the asof-pin + latest-only rejection makes the no-lookahead rail explicit per source.
- **Negative / accepted:** openbb is a HEAVY dependency with a large transitive footprint + a
  provider-credential (PAT) model; it is imported ONLY when HERMES_QUANT_OPENBB=1 (lazy import in
  the provider), pinned, and the free-provider path (yfinance/fmp-free) is the default so no paid key
  is required to start.
- **Negative / accepted:** each new provider is a new lookahead-leak surface; the unconditional
  asof post-filter + the latest-only-reject guard are mandatory per provider, with a test each.
- **Neutral:** this widens Perceive; it does not change Decide/React or any rail.

### Confirmation

Satisfied by: (1) a no-lookahead test per provider — a row dated after asof is dropped; (2) a
latest-only-reject test — quote/cboe-chain/yfinance-consensus raise at the boundary in a non-live
context; (3) a vendor-routing test — fmp serves OHLCV as the 2nd tier when yfinance fails; (4) a
default-OFF test — with HERMES_QUANT_OPENBB unset, openbb is never imported and the providers are
inert (byte-identical); (5) an AnalystView-contract test — a view built from OpenBB data validates
against the canonical contract (ADR-0095). Each provider is eval-gated before its analyst influences
a live decision.

## More Information

- Raw research (TradingAgents source map + OpenBB coverage + the gap): `docs/research/2026-06-17-aegis-data-sources-research-raw.json`.
- Build order (seeds): `aegis-ob00` epic + `ob1` openbb_provider+OHLCV-redundancy (highest value first),
  `ob2` fundamentals/estimates, `ob3` insider/institutional, `ob4` macro (FRED) + news, `ob5` the
  direct non-OpenBB providers (StockTwits, Polymarket, AV-sentiment). Each default-OFF + asof-pinned + eval-gated.
- TradingAgents' own honesty mechanism (date-pinned online/offline cache + a >10-day staleness guard)
  validates the asof-pin approach — adopt the same staleness guard at the AEGIS boundary.

### Consumer cutovers (c7a9 + 2f33, 2026-06-18)

The `ob1` provider + `ob2`/`ob3` analysts existed but were never WIRED into the live
advisor path (provider chain registered the openbb vendor in `vendor_routing.VENDOR_LIST`
but `advisor._get_default_provider` returned a bare `YFinanceProvider`; the estimates/insider
analysts were not in `advisor._build_default_analysts`). Two default-OFF cutovers close that gap:

- **c7a9 — OHLCV live tier.** A NEW flag `HERMES_QUANT_OPENBB_LIVE` (distinct from
  `HERMES_QUANT_OPENBB`, which gates the SDK/provider itself) makes
  `advisor._get_default_provider` return a `_ChainedProvider([yfinance, openbb])` for
  equity/etf when set — yfinance PRIMARY, openbb a SILENT no-op FALLBACK consulted only when
  yfinance fails (it still requires `HERMES_QUANT_OPENBB` to actually fetch). DEFAULT-OFF =>
  a bare `YFinanceProvider` (byte-identical live path). The `as_of` cutoff threads into
  `fetch_with_chain` unchanged, so the no-lookahead rail holds across both tiers.
- **2f33 — EstimatesAnalyst / InsiderAnalyst committee registration (option a).**
  Both analysts are now registered in `advisor._build_default_analysts` AND the canonical
  `advisor.recommend()` inline roster, each gated on BOTH its per-analyst flag
  (`HERMES_QUANT_ESTIMATES_ANALYST` / `HERMES_QUANT_INSIDER_ANALYST`) AND
  `HERMES_QUANT_OPENBB`. With those flags off the analysts are never constructed (roster
  byte-identical), satisfying the analysts' "intentionally not registered while default-OFF"
  note: the two-flag registration gate IS the operator's enable switch. Their no-lookahead
  fence is the source provider's `data.date`/`filing_date <= asof` post-filter (not the bar
  window), so they stay out of `tests/test_no_lookahead.py`'s bar-temporal enumeration —
  same category as `HermesSemanticAnalyst`.
  Each analyst remains eval-gated before it influences a live decision.
