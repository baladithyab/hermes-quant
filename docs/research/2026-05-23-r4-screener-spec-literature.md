# R4 — Screener-Spec Literature Survey + Methodology DSL Recommendation

> **Anchor model:** minimax/minimax-m2.7 (re-dispatch after kimi-k2.6 timed
> out at 570 s on multi-target web iteration). Goal: feed ADR-0030 (daily
> picker recipe + cron) and the methodology library that codifies
> reel-extracted scanners (e.g. socalminh covered-call) as drop-in YAML for
> `recipes.py::PDRRecipe`.
>
> Wave: Phase-1 R4 of the options + daily-picker + retro-loop plan
> (`docs/plans/2026-05-23-options-daily-retro.md`). Sibling notes: R1
> Alpaca options API, R2 options-aware risk-gate prior art, R3
> retrospective-loop architectures.

## TL;DR

1. None of the major screener DSLs (Finviz URL params, Barchart fields,
   Tasty option-screener fields, TradingView Pine Screener, Quantopian
   Pipeline, EDGAR full-text) is a clean fit on its own. Finviz encodes
   filters as opaque param tokens (`cap_largeover`, `fa_pe_u20`); Pine
   Screener requires an actual Pine script per universe; Pipeline is dead
   library, but its Factor/Filter algebra is the right *shape*; Tasty
   exposes a flat field list (POP, IVR, BPR) but no DSL; EDGAR is purely
   keyword + form-type, useless for numeric thresholds.
2. The right move for hermes-quant is a **minimal declarative
   methodology DSL** — a flat list of typed predicates plus a small set
   of compose-ops — embedded inside the existing `PDRRecipe` YAML under
   a new `screener` key. Predicates are JSON-schema-validated and
   compile to deterministic Python over a normalized `MarketContext +
   FundamentalSnapshot + OptionChainSnapshot` row.
3. Composability uses the **existing BMA/stacking aggregator pattern**
   (ADR-0003): each methodology becomes a `MethodologyAnalyst` that
   emits an `AnalystView` (`direction`, `confidence`, `horizon_days`,
   `evidence`). Multiple methodologies firing on the same ticker
   aggregate identically to existing analysts — *silence by default*
   when they disagree.
4. The reel-ingestion pipeline is codified as: yt-dlp → dual analysis
   (traditional STT/OCR + Gemini-multimodal) → diffed
   methodology markdown → DSL YAML draft → architect review → recipe
   registration. Audio-only (no UI on screen) is the documented
   degenerate case — see §6 failure-mode `F-AUDIO-ONLY`.

---

## 1. Screener-DSL survey (one search; sources cited inline)

### 1.1 Finviz URL params

Filters compile to opaque slot tokens concatenated into a `f=` query
string:
`cap_midover,fa_pe_u20,sh_avgvol_o500,ta_rsi_os30,sec_technology`.
Each token is `<group>_<predicate><threshold>` where predicates are
enumerated sets (`u` = under, `o` = over, `b2to10` = between 2 and 10).
~70 fields ([finviz.com/help/screener](https://finviz.com/help/screener)):
exchange, sector, industry, country, market cap, P/E, forward P/E, PEG,
P/S, P/B, EPS-growth (this/next FY, 5y past/future, q/q), sales growth,
dividend yield, ROA/ROE/ROIC, current/quick ratio, debt/equity, gross/
operating/net margin, payout ratio, insider ownership/transactions,
institutional ownership/transactions, short float, analyst recommendation,
**option/short** (boolean: optionable, shortable, both), earnings date,
performance windows, volatility, RSI(14), gap, SMA-20/50/200,
high/low windows, candlestick patterns, beta, ATR, average volume,
relative volume, current volume, price.

Strengths: dense, copy-pasteable URLs, well-known. Weaknesses: closed
enumeration; no compound predicates (no `AND/OR/NOT` beyond implicit
AND); **no options-Greek fields**, **no put/call ratio**, no DTE filter.
The `fin_o0.1` / `o0.5` style is a bucket-set, not a true range — you
get tier choices, not arbitrary thresholds.

### 1.2 Barchart screener fields

Barchart's covered-call / put-write / iron-condor screeners expose:
strike, DTE, bid/ask, IV, IV-rank, delta, theta, vega, gamma, premium,
premium %, return-if-flat, return-if-assigned, breakeven, OI, volume,
P/C ratio (volume + open-interest variants), earnings-before-expiry
flag, dividend-before-expiry flag. URL is form-encoded
`field=op,value` triplets, not a DSL — you can't compose, only set
field-by-field.

Critical: this is the most likely "Parture / Bartchart" tool from
socalminh's reel — the four filters he names (mid-cap, monthly-premium-%,
DTE band, P/C ratio) all map to first-class Barchart fields.

### 1.3 Tastytrade screener fields

Tastytrade's web/desktop screener focuses on: liquidity (volume, open
interest, bid-ask width), IV-rank (IVR%), IV-percentile (IVP%),
probability-of-profit (POP), buying-power-reduction (BPR), expected
move, earnings flag, ex-div flag, beta-weighted-delta, theta/day,
ROC (return-on-capital), ROC-annualized. No persistent URL DSL — config
lives in the desktop client. POP and BPR are the only fields you don't
get cleanly anywhere else; both are derivable from chain + risk-free
rate.

### 1.4 TradingView — two distinct things

- **Standard Screener:** drop-down field picker, ~150 fields, AND-only
  boolean. No DSL per se; the URL encodes filter state but isn't
  user-facing.
- **Pine Screener (Beta, 2025+):** runs a Pine Script over a watchlist;
  the script's `plot()` outputs become sortable columns and the
  script's `if cond` blocks become filter passes. This is essentially
  *full Turing-complete code as DSL*. Powerful, but: requires writing
  Pine; only AND-composes filter conditions; watchlist-bounded
  (no full-market scan).

### 1.5 Quantopian Pipeline (defunct service, surviving algebra)

The shape worth stealing. Pipeline's API was:

```python
from quantopian.pipeline import Pipeline
from quantopian.pipeline.factors import SimpleMovingAverage, Returns
from quantopian.pipeline.filters import StaticAssets

mid_cap = (USEquityPricing.close.latest * shares_outstanding.latest)
            .between(2e9, 1e10)
sma_50  = SimpleMovingAverage(inputs=[USEquityPricing.close], window_length=50)
above_sma = USEquityPricing.close.latest > sma_50
universe = mid_cap & above_sma
pipe = Pipeline(columns={"sma50": sma_50}, screen=universe)
```

Two abstractions: **Factor** (numeric series per asset per date) and
**Filter** (boolean series). Both compose with `&`, `|`, `~`, `>`, `<`,
`between`, `top(n)`, `bottom(n)`, `rank()`, `zscore()`, `percentile_between`.
This is the right algebra: **declarative, point-in-time-correct
(no look-ahead), composable**, and trivially backtestable because every
factor/filter is a pure function of `(asof, asset)`.

Hermes-quant's no-look-ahead CI gate is exactly the property Pipeline's
`asof` semantics gives for free. Steal the algebra; reject the heavy
runtime.

### 1.6 SEC EDGAR full-text search

EDGAR EFTS API: `https://efts.sec.gov/LATEST/search-index?q=...&forms=10-K`.
Operators: phrase search (`"..."`), boolean `AND/OR/NOT`, form-type
filter, date range, CIK filter. **Not numeric.** Only useful as a
qualitative analyst (e.g. "filings mentioning 'going concern' in last 90
days") — emits `AnalystView` events but is not a screener-DSL primitive.
File under "future analyst", not Q-DSL operator.

### 1.7 Synthesis — what the unioned operator set looks like

| Operator                              | Source        | Q-DSL form                          |
|---------------------------------------|---------------|-------------------------------------|
| Range filter `lo ≤ x ≤ hi`            | Finviz, BC    | `between: [2e9, 1e10]`              |
| Threshold `x ≥ k`, `x ≤ k`            | all           | `gte`, `lte`                        |
| Set membership `x ∈ {…}`              | Finviz        | `in: [Technology, Healthcare]`      |
| Negated set `x ∉ {…}`                 | Finviz        | `not_in:`                           |
| Comparison to rolling stat            | Pipeline, TV  | `gt_sma: {window: 50}`              |
| Ratio constraint `a / b ∈ [lo, hi]`   | BC, Tasty     | `ratio: {num, den, between}`        |
| Top-N / bottom-N rank                 | Pipeline      | `top: {by, n}`                      |
| Percentile band                       | Pipeline      | `percentile_between: [70, 100]`     |
| Event flag (earnings, ex-div soon)    | Finviz, BC    | `flag: earnings_within_days: 14`    |
| Time-decay weighted recency           | (custom)      | `recency_weight: {halflife_days}`   |
| Boolean compose (AND/OR/NOT)          | Pipeline, EDGAR| `all_of`, `any_of`, `none_of`      |
| Universe seed (presets, watchlists)   | TV, Pipeline  | `universe: sp1500_optionable`       |

Hermes-quant DSL has to support all 12. Notably missing from every
public screener: a clean way to encode option-Greek thresholds *at a
chosen DTE band* — i.e. "find stocks where the 21–36-DTE ATM call
premium / spot ≥ 0.10". This is the socalminh case, and it's the place
hermes-quant *adds value* over Finviz.

---

## 2. Recommended DSL for hermes-quant

### Shape

A new `screener` block on `PDRRecipe` (alongside `analysts`, `aggregator`,
`risk_gate`):

```yaml
screener:
  universe: sp1500_optionable          # named seed; resolves to ticker set
  filters:                              # implicit AND
    - field: market_cap
      between: [2.0e9, 1.0e10]
    - field: option_chain.put_call_ratio
      between: [0.2, 0.7]
      reject_if_above: 0.7              # explicit hard skip
    - ratio:
        num: option_chain.atm_call_premium
        den: spot
        between: [0.10, null]
        at:
          dte_between: [21, 36]
        annotation: "10%/month premium — denominator ambiguous; see notes"
  rank:
    by: option_chain.atm_call_premium_pct
    desc: true
    top: 25
  emit:
    direction: long_underlying           # methodology emits 'covered-call long-underlying'
    horizon_days: 30
    confidence_floor: 0.55
```

### Field namespace

Three top-level namespaces (only the ones the methodology actually uses
need to be wired):

- `fundamentals.*` — `market_cap`, `pe`, `eps_ttm`, `revenue_ttm`,
  `sector`, `industry`, `country`, `dividend_yield`, …
- `bars.*` — `close`, `volume`, `sma_50`, `sma_200`, `rsi_14`, `atr_14`,
  `relative_volume`, `gap_pct`, …
- `option_chain.*` — `put_call_ratio` (volume + OI variants),
  `atm_call_premium`, `atm_put_premium`, `iv_rank`, `iv_percentile`,
  `delta_at`, `theta_at`, `gamma_at`, `vega_at`, `bid_ask_pct_at`,
  parameterized by `at: {dte_between, strike_offset_pct}`.

All fields evaluated point-in-time at `MarketContext.asof`. CI gate
`shuffle_timestamps_test` runs against the compiled screener exactly as
it does against analysts (per AGENTS.md "No look-ahead bias" rule).

### Compose ops

```yaml
filters:
  - all_of:
      - field: market_cap
        between: [2e9, 1e10]
      - field: option_chain.put_call_ratio
        between: [0.2, 0.7]
  - any_of:
      - field: bars.relative_volume
        gte: 1.5
      - field: fundamentals.eps_growth_qtr_yoy
        gte: 0.20
  - none_of:
      - flag: earnings_within_days: 7    # avoid earnings landmines
```

### Emit semantics

A `screener` block IS NOT itself an analyst. It is run by a **wrapper
analyst** named `methodology_screener` that loads the YAML, evaluates
filters at tick time, and emits one `AnalystView` per surviving ticker.
`direction` is taken from the `emit:` block; `confidence` is computed
from how many soft-bands the candidate sits inside vs the hard floor
(see §4).

### Why YAML-only, not embedded Python

Money-software, AGENTS.md "Reproducibility" principle: every recipe
diff has to be a literal text diff. YAML hashes into `config_hash` for
tick-DB joins. A Python lambda would defeat this. If a methodology
genuinely needs computation (e.g. socalminh-style "premium / spot at
chosen DTE"), the *named operators* (`ratio`, `at: {dte_between}`) cover
it; if a future methodology needs something exotic, that's a new named
operator + ADR amendment, not a code-blob escape hatch.

---

## 3. Worked examples (drop-in YAMLs)

### 3.1 socalminh covered-call (full §5 deliverable; see §5 for ingestion provenance)

```yaml
# ~/.hermes/quant/recipes/socalminh-covered-call.yaml
id: socalminh-covered-call
description: >-
  Covered-call screener extracted from @socalminh IG reel DYe45Cxi5ju
  (2026-05-18). Mid-cap underlying with high monthly premium, short
  enough DTE to recycle monthly, neutral-to-bullish put/call sentiment.
  See methodology/socalminh-covered-call-screener.md for provenance and
  open ambiguities.
symbols: ["UNIVERSE:sp1500_optionable_midcap"]   # resolved at load time
asset_class: equity_options
timeframe: 1d
data_provider: alpaca
data_provider_config:
  options_chain: true

# Perceive — methodology screener acts as a single-analyst input
analysts: ["methodology_screener"]
analyst_config:
  methodology_screener:
    screener:
      universe: sp1500_optionable
      filters:
        # Filter 1: mid-cap window (STATED, IG caption + audio)
        - field: fundamentals.market_cap
          between: [2.0e9, 1.0e10]

        # Filter 2: 10%/month premium (STATED audio, INFERRED denominator)
        # ANNOTATION: Minh says "we want to make 10% every month on premium".
        # He does NOT specify denominator. Best-guess interpretation:
        #   monthly_call_premium_at_chosen_DTE / spot >= 0.10
        # Caveat re-run with whisper-large to confirm; until then treat as
        # the *target yield bar*, not a hard filter — soft-band only.
        - ratio:
            num: option_chain.atm_call_premium
            den: spot
            at:
              dte_between: [21, 36]
              strike: atm           # alternative: first_otm
            gte: 0.10
            soft: true              # missed-bar contributes to confidence loss, not rejection
            annotation: "Audio-only; denominator ambiguous. See open-question 2 in methodology/socalminh-covered-call-screener.md."

        # Filter 3: DTE band (STATED)
        - field: option_chain.target_dte_band
          between: [21, 36]

        # Filter 4: put/call ratio (STATED, both bounds explicit)
        - field: option_chain.put_call_ratio
          between: [0.2, 0.7]
          reject_if_above: 0.7      # explicit verbal hard-skip
          # 0.2 floor is INFERRED — Minh doesn't justify it; likely
          # "ensure liquid put market exists / not a meme call-frenzy".
          # Keep as hard floor pending re-listen; flag in retro if rejection rate spikes.

        # Hermes-Quant additions NOT in the reel — sanity layer per AGENTS.md
        - field: option_chain.bid_ask_pct_at_atm
          lte: 0.05                  # ≤5% spread on the call we'd write
        - field: option_chain.avg_option_volume_30d
          gte: 100
        - none_of:
            - flag: earnings_within_days: 7    # avoid earnings landmines

      rank:
        by: option_chain.atm_call_premium_pct
        desc: true
        top: 25

      emit:
        direction: long_underlying_short_call    # "covered call" composite signal
        horizon_days: 30
        confidence_floor: 0.55
        confidence_model: band_distance          # see §4

# Decide
aggregator: bma
risk_gate: options_default                       # ADR-0027
risk_gate_config:
  max_short_call_delta_per_position: 0.30
  max_assignment_risk_pct_nav: 0.10

# React
reactor: paper
supported_modes: [advise, hitl, backtest]
live_allowed: false
min_decisions_for_charter_gate: 30
min_settlements_for_charter_gate: 30
notes: >-
  Methodology source: @socalminh IG reel DYe45Cxi5ju (2026-05-18).
  Pipeline: traditional Whisper+OCR. Tool-name still ambiguous
  ("Parture" → most likely Barchart). Re-run with whisper-large-v3
  before promoting to live.
```

### 3.2 Generic momentum-breakout swing screener

```yaml
id: momentum-breakout-swing
screener:
  universe: russell_3000
  filters:
    - field: bars.close
      gt_sma: {window: 50}
    - field: bars.close
      gt_sma: {window: 200}
    - field: bars.relative_volume
      gte: 1.5
    - field: bars.close
      pct_above_n_day_high: {n: 20, gte: -0.02}   # within 2% of 20d high
    - field: fundamentals.market_cap
      gte: 1.0e9
    - none_of:
        - flag: earnings_within_days: 5
  rank:
    by: bars.rs_rating_3m
    desc: true
    top: 30
  emit:
    direction: long_underlying
    horizon_days: 10
    confidence_floor: 0.60
```

### 3.3 CSP — stocks you'd be happy to own at strike

```yaml
id: csp-quality-discount
screener:
  universe: sp500
  filters:
    - field: fundamentals.pe
      between: [5, 25]
    - field: fundamentals.debt_to_equity
      lte: 1.0
    - field: fundamentals.roe
      gte: 0.15
    - field: fundamentals.dividend_yield
      gte: 0.015
    - ratio:
        num: option_chain.atm_put_premium
        den: spot
        at:
          dte_between: [30, 45]
          strike_offset_pct: -0.05        # 5% OTM put
        gte: 0.012                         # ≥1.2% premium for the month
    - field: option_chain.put_iv_rank
      gte: 30
  emit:
    direction: short_put
    horizon_days: 35
    confidence_floor: 0.60
```

### 3.4 LEAPS-thesis screener

```yaml
id: leaps-thesis
screener:
  universe: ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "BRK.B", "UNH", "V", "JPM"]
  filters:
    - field: fundamentals.market_cap
      gte: 5.0e10
    - field: fundamentals.revenue_growth_5y_cagr
      gte: 0.10
    - field: bars.close
      gte_pct_below_52w_high: 0.10        # at least 10% below 52w high
    - field: option_chain.deep_itm_call_iv
      lte: 0.40                            # avoid IV crush risk on long-dated
    - ratio:
        num: option_chain.itm_call_delta
        den: 1.0
        at:
          dte_between: [365, 730]
          strike_offset_pct: -0.20         # 20% ITM
        gte: 0.80
  emit:
    direction: long_call
    horizon_days: 365
    confidence_floor: 0.65
```

---

## 4. Composability — when methodologies disagree

Two cases:

**Case A — same direction, different methodologies.**
socalminh-covered-call fires LONG-UNDERLYING-SHORT-CALL on TICKER X;
momentum-breakout-swing fires LONG-UNDERLYING on TICKER X. Both are net-long
underlying. **These compose into an implicit wheel intent** (see backlog
in plan doc) — but at v0.5 we treat them as independent analysts feeding
the existing **BMA aggregator** (ADR-0003). Confidence-weighted
posterior; tie-break to flat per "Silence by default" (AGENTS.md).

**Case B — opposed directions.**
socalminh fires LONG-UNDERLYING; csp-quality-discount fires SHORT-PUT
(also bullish, fine); but a hypothetical bear-flag-momentum analyst
fires SHORT-UNDERLYING. **BMA disagrees-flat rule applies**: aggregator
returns flat, no signal. Risk-gate downstream is irrelevant.

**Vote-aggregation rule (lift from ADR-0003):**

```
posterior_long  = Σ w_i · p_i · 𝟙[direction_i ∈ {long, long_call, long_underlying}]
posterior_short = Σ w_i · p_i · 𝟙[direction_i ∈ {short, short_call, short_underlying}]
posterior_neutral = Σ w_i · p_i · 𝟙[direction_i ∈ {short_put, long_underlying_short_call}]
                                          # neutral-to-bullish cluster

if max(posterior_*) - second_max(posterior_*) < margin_threshold:
    return FLAT          # silence by default
else:
    return arg-max
```

`w_i` is the analyst's BMA posterior weight (from track-record;
calibration drift surfaced in `quant_doctor` per AGENTS.md). `margin_threshold`
defaults to 0.15, configurable per recipe. `confidence_model: band_distance`
means: confidence = 1 - (#soft-bands missed) / (#soft-bands total),
clipped to `[confidence_floor, 1.0]`.

This is intentionally the **same** machinery the existing 4 analysts use.
A `MethodologyAnalyst` is just an analyst whose source-of-truth happens
to be a YAML file rather than Python code. ADR-0030 should explicitly
state this so we don't end up with a parallel aggregator.

**One new-rule recommendation:** when two methodologies agree on
*direction* but disagree on *strategy* (e.g. covered-call vs naked-long),
emit BOTH as separate `AnalystView`s with `evidence.composite_intent =
"wheel"` and let the risk gate see them together — it's the gate's job
to recognize "covered call + CSP on same name" = wheel and budget
margin/buying-power once, not twice.

---

## 5. socalminh covered-call — full provenance + drop-in

The full YAML drop-in is §3.1 above. Key annotations preserving the
ambiguities documented in `methodology/socalminh-covered-call-screener.md`:

- **F1 mid-cap [2e9, 1e10]:** STATED. Hard filter. No annotation needed.
- **F2 10 % / month premium:** STATED audio, INFERRED numeric denominator.
  Marked `soft: true` so it weights confidence rather than hard-rejecting.
  Annotation in YAML quotes the open question. Best-guess: ATM call
  premium / spot at the 21–36-DTE strike.
- **F3 21 ≤ DTE ≤ 36:** STATED. Hard filter.
- **F4 0.2 ≤ P/C ≤ 0.7:** STATED. Hard filter with explicit
  `reject_if_above: 0.7` matching Minh's verbal "we don't want to buy
  that stock". 0.2 floor INFERRED, kept as hard until retro shows
  excessive rejection.
- **Hermes-Quant additions:** liquidity sanity layer (bid-ask ≤ 5 %,
  avg option vol ≥ 100, no earnings within 7 days). Not in the reel.
  Documented in `notes:` so they're discoverable in `quant_show_recipe`.

The audio-only "Parture / Bartchart" tool-name ambiguity is a
**methodology-level** open question, not a DSL-level one — the YAML is
provider-agnostic. If we eventually confirm Barchart, we can target its
API directly for the option-chain feed; until then, Alpaca options
chain + a P/C ratio source (Cboe daily file or yfinance options volume
aggregate) is sufficient.

---

## 6. From-reel ingestion pipeline

Standard process for new methodologies dropped as TikTok / IG reels:

```
┌─────────────────────────────────────────────────────────────────┐
│ Stage 0:  user drops URL in Discord                             │
│   → ingestion-watcher pulls via yt-dlp                          │
│   → save to /tmp/reels/<short-id>.mp4 + .info.json              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 1:  DUAL ANALYSIS (parallel scatter, family-diverse)      │
│                                                                 │
│  Branch A — TRADITIONAL (deterministic, reproducible):          │
│   • whisper-large-v3 STT → transcript_traditional.txt           │
│     (fall back to whisper-base only for cost-control flag)      │
│   • ffmpeg keyframes @ 1 fps → frames/                          │
│   • RapidOCR-ONNX over keyframes → ocr_per_frame.txt            │
│   • targeted vision_analyze on suspicious frames                │
│   • Output: methodology/<id>-traditional.md                     │
│                                                                 │
│  Branch B — MULTIMODAL (LLM, pattern-finding):                  │
│   • Gemini-3.1-Pro multimodal: video + audio + frames           │
│   • Output: methodology/<id>-gemini.md                          │
│                                                                 │
│  Diff branches → methodology/<id>.md                            │
│   (canonical extraction; flag DISAGREEMENTS as open questions)  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 2:  DSL YAML DRAFT                                        │
│   subagent reads methodology/<id>.md →                          │
│   produces ~/.hermes/quant/recipes-staging/<id>.yaml            │
│   (NOT yet in active recipes/ dir)                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 3:  ARCHITECT REVIEW (single subagent, claude-opus-4.7)   │
│   • Validate YAML schema, field-namespace correctness           │
│   • Check no operator outside DSL spec is used                  │
│   • Cross-check against AGENTS.md hard rules                    │
│     (e.g. action-space discreteness, no-look-ahead semantics)   │
│   • Flag if a methodology requires a NEW operator → ADR amend   │
│   • Output: review verdict + suggested edits                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 4:  CROSS-FAMILY REVIEW (3-way scatter, ADR-0023 lift)    │
│   • gpt-5.5: adversarial — "what's the worst this YAML does?"   │
│   • deepseek-v4-pro: math — premium/spot semantics, P/C source  │
│   • minimax-m2.7: long-context — does it conflict with existing │
│     recipes' universe / aggregator weights?                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 5:  HITL APPROVAL                                         │
│   • `hermes quant methodology approve <id>` (CLI, confirm prompt│
│     per AGENTS.md "money never goes through tools")             │
│   • Moves YAML from recipes-staging/ to recipes/                │
│   • Runs charter-gate backtest (≥30 sim trades) before live     │
│   • Logs amendment-style entry in proposed_amendments.jsonl     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Stage 6:  LIVE PAPER, RETRO LOOP TAKES OVER                     │
│   ADR-0026 retro loop watches settlement outcomes and feeds     │
│   findings back as proposed amendments to the YAML (numeric     │
│   thresholds only — schema changes still need ADR amendment).   │
└─────────────────────────────────────────────────────────────────┘
```

### Documented failure modes

| ID                | Description                                        | Handling                                                                                               |
|-------------------|----------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| `F-AUDIO-ONLY`    | Reel is talking-head; no UI/screener visible (socalminh case) | Methodology lifted from audio + caption only. Tool/source identity becomes an *open question*, not a blocker. YAML goes provider-agnostic. |
| `F-ASR-AMBIG`     | Whisper-base mishears critical token ("Parture" → "Barchart"?) | Mandatory whisper-large-v3 re-run before Stage 4 promotion. Until then, mark methodology `confidence: extraction_low`. |
| `F-NUMERIC-AMBIG` | Author states a percentage without denominator (10%/month) | Encode as `soft: true` band, document interpretation in `annotation:`, flag for retro tracking         |
| `F-NO-OPERATOR`   | Methodology requires DSL operator we don't have (e.g. cross-asset correlation filter) | Stage 3 architect blocks; opens ADR amendment for new operator before unblocking the YAML              |
| `F-NO-EXAMPLES`   | Author cites no tickers; can't sanity-check filter pass-rate | Backtest against a known-good period (Stage 5 charter gate) before live; rely on retro after ≥30 settled trades |
| `F-LIQUIDITY-GAP` | Author specifies edge filters but no liquidity floor (socalminh case) | Hermes-Quant *always* injects sanity layer (bid-ask, vol, earnings-near). Documented in `notes:` so it's auditable. |
| `F-RIGHT-OUTCOME-WRONG-RULE` | Reel sounds plausible but methodology is overfit / cherry-picked | Charter gate (≥30 sim trades) + retro-loop track-record gating before live; aggregator weight starts low |
| `F-PROVIDER-LOCK` | Methodology only works against a specific data vendor's idiosyncratic field (e.g. Barchart's specific P/C variant) | DSL is provider-agnostic; data layer translates. If translation is lossy, mark methodology `requires_provider: ...` and refuse to load on other providers |

### Roles in the pipeline

- **User:** drops URL, approves at Stage 5.
- **Subagent (kanban-pipelined):** Stages 1–4.
- **Hermes daemon:** Stage 6 (settlement + retro).
- **Code (one-time):** the `methodology_screener` analyst wrapper that
  loads YAML and emits `AnalystView`s. Lands in Wave B of ADR-0030.

---

## 7. Recommendations summary (for ADR-0030)

1. **Add a `screener` block to `PDRRecipe`** with the 12-operator DSL
   spec from §1.7. JSON-schema-validated, point-in-time-correct, hashes
   into `config_hash`.
2. **Single new analyst — `methodology_screener`** — wraps the YAML and
   emits `AnalystView`s the existing aggregator already understands.
   Zero changes to ADR-0003 aggregator math.
3. **Compose via existing BMA**, with the explicit silence-by-default
   margin threshold from §4. Add the "wheel-intent" composite-flag rule
   when covered-call + CSP fire on the same ticker.
4. **Promote `methodology/socalminh-covered-call-screener.md` →
   `~/.hermes/quant/recipes/socalminh-covered-call.yaml`** as the first
   worked example. YAML in §3.1.
5. **Codify the 6-stage reel-ingestion pipeline** as an ADR-0030
   appendix. Failure modes table from §6 goes verbatim.
6. **No DSL escape hatches** — no embedded Python, no eval, no Pine.
   New methodologies that require new operators trigger an ADR
   amendment (small bar; we already have the mechanism).
7. **Backlog flag (already in plan doc):** the cross-strategy
   correlation case (covered-call + CSP = wheel) gets first-class
   support in the risk gate, not the aggregator. Risk gate is where
   margin/BPR is budgeted.

This keeps the codified-methodology pipeline aligned with hermes-quant's
three discipline principles: silence-by-default (BMA disagrees-flat),
hard-rules-over-learned-policy (DSL is declarative, retro can only
propose threshold changes, schema changes need ADR), and reproducibility
(YAML-only, hashable, no code-blob escape).
