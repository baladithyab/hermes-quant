# hermes-quant founding charter — 2026-05-13

> **Provenance:** This document is the original architectural brief authored
> by the user (Codeseys) on 2026-05-13 that bootstrapped the entire
> hermes-quant project. Saved verbatim to the repo because (a) the
> reasoning behind every architecture decision traces back to it,
> (b) future contributors need to read it before changing scope, and
> (c) the explicit "what works / what does NOT work" guidance in the RL
> section is load-bearing for v0.4+ work.
>
> Originally communicated as a 7-part Discord message; reproduced as a
> single document here.

---

## TL;DR

Yes, this is buildable, and the PDR pattern maps onto trading better than it
maps onto general embodiment. But you're conflating two distinct things that
need to stay distinct:

1. A **multi-analyst ensemble framework** (your "multiple algorithms that all
   analyze independently → committee decides") — this is well-trodden ground,
   call it what it is: a mixture-of-experts trading committee.
2. An **RL auto-evolution loop** — this is the hard part, and 90% of the
   research that claims to do it is overfitting to backtest noise.

Let me take them in order.

---

## What Kronos actually is (so we don't over-index on it)

I read the paper and the repos. Sober version:

- **Kronos** (Shi et al., AAAI 2026, arxiv:2508.02739) is a decoder-only
  foundation model trained on 12B K-lines from 45 exchanges. It's a
  BSQ-quantized tokenizer (5+5 bit hierarchical, 1024 vocab) feeding an
  autoregressive Transformer. Sizes: mini (4.1M), small (24.7M), base
  (102M). Large (499M) is not open.
- It predicts the **distribution** of future OHLCV — not "the price will
  be X." This is the right framing for trading.
- **Kairos** (Shadowell) is the most honest derivative: A-shares daily is
  still negative IC after multiple variants, crypto 1-min h30 is the only
  direction with consistent alpha (rank-IC +0.076, ICIR +0.484 on BTC/ETH
  with Kronos-base). That's a useful signal but not a money printer.
- **Kronos-financial-analyzer** (junshunG) is more demo than production —
  falls back to a GBM simulator when weights aren't available.

**Key insight for your framework:** Kronos is *one analyst, not the whole
system*. It produces a probabilistic OHLCV forecast. Your ensemble should
treat it as a single feature-generator alongside others, not as the oracle.

---

## The PDR mapping is actually clean here

Eidolon's PDR (Perception–Decision–Reaction) maps onto trading almost 1:1,
and unlike vision/audio/text, the environment ticks at a known rate and the
reward signal is unambiguous (P&L). That's a much friendlier RL substrate
than autonomous embodiment.

```
PERCEIVE: market data streams (L1/L2 quotes, trades, options chain,
          fundamentals, news, on-chain, alt-data) → fused latent
          observation per asset/timeframe

DECIDE:   N independent analyst heads each emit:
            - directional view (long/short/flat)
            - magnitude (expected return)
            - confidence (calibrated)
            - horizon (timeframe over which view holds)
          Aggregator (the "decision module") combines into:
            - position sizing (Kelly-fractional or risk-parity)
            - entry/exit/hedge actions

REACT:    silence-by-default gates open only when:
            - ensemble disagreement is LOW (high confidence)
            - expected edge > transaction cost + slippage + risk premium
            - position would not violate risk limits (VaR, exposure caps)
          Otherwise: hold cash, do nothing. This is the most underrated
          property — most trading systems lose because they over-trade.
```

The silence-biased gate principle from Eidolon (-2.0 init bias, 3× FP
penalty) is exactly the right prior for trading. Most retail RL trading
agents fail because they're rewarded for action. Yours should be **rewarded
for correct inaction**.

---

## The framework I'd actually build

Call it something — let's say **Oracle-PDR** (Perception–Decision–Reaction
for markets) — and structure it as:

### Layer 1: Analyst Pool (pluggable, the part you "keep adding to")

Each analyst is a self-contained module with a uniform interface:

```python
class Analyst(Protocol):
    name: str
    timeframes: list[str]      # ['1m', '5m', '1h', '1d']
    asset_classes: list[str]   # ['equity', 'option', 'crypto', 'fx']

    def analyze(self, ctx: MarketContext) -> AnalystView:
        """Returns directional view + magnitude + confidence + horizon."""
```

Concrete analysts you'd seed it with:

- **kronos-forecaster** — wraps Kronos, returns distributional OHLCV →
  directional view via quantile shift
- **technical-classical** — RSI/MACD/Bollinger/MA-cross (cheap baseline;
  surprisingly hard to beat on liquid names)
- **microstructure** — order book imbalance, queue position, trade flow
  toxicity (VPIN)
- **options-flow** — IV skew, put/call ratio, gamma exposure, dealer
  positioning
- **fundamental-snapshot** — earnings revisions, guidance, factor exposures
- **news-llm** — headline sentiment + entity-resolved event extraction
  (you have model-roster scatter for this)
- **regime-classifier** — HMM or change-point detector that gates which
  other analysts get listened to
- **cross-asset** — correlations, lead-lag (e.g., DXY → EM equities, VIX
  term structure)

The key is **uniform output schema** so the aggregator doesn't care which
analyst is which.

### Layer 2: Aggregator (the "decision" module)

Two complementary approaches, stacked:

**(a) Static priors:** Bayesian model averaging or stacking with held-out
validation weights. Each analyst gets a credibility score per regime.

**(b) Learned aggregator:** A small Transformer head that takes the N
AnalystView vectors + market context → action distribution. This is what
RL trains, **not the analysts themselves**.

Why separate the analysts from the aggregator? Because:

- Analysts can be retrained/swapped without touching the aggregator.
- The aggregator's input is low-dimensional (N × analyst_dim), so RL is
  tractable.
- You preserve interpretability — when a trade goes wrong, you can ask
  "which analyst voted for this?"

### Layer 3: Risk/Execution gate (the "reaction" silence layer)

**Hard rules, not learned:**

- Kelly cap (e.g., quarter-Kelly with floor at 0)
- Max position % NAV, max sector exposure, max correlation
- Drawdown circuit breakers (kill switch at -X% daily/weekly)
- Transaction-cost-aware threshold:
  `|expected_edge| > 2 × (commission + half_spread + estimated_slippage)`

If the gate says no, the system holds cash. Silence by default.

---

## The RL loop — where most projects die

This is where I want to be careful with you, because this is where it goes
wrong.

### What works:

- **Walk-forward training with embargo windows.** Train aggregator on
  `[t-N, t-K]`, validate on `[t-K, t-K/2]`, test on `[t-K/2, t]`. Slide
  forward. Never train on data more recent than your test set.
- **Reward = log-return after transaction costs and slippage**, with risk
  penalties (drawdown, turnover). Plain P&L reward overfits to lucky tails.
- **PPO or recurrent SAC for the aggregator**, with the analyst pool frozen
  most of the time. Re-train analysts on a slow cycle (monthly/quarterly)
  on actual realized data.
- **Multi-asset / multi-regime training** — train on diverse market
  conditions, not just the bull run. Include 2008, 2020-Mar, 2022, ideally
  with synthetic data from Kronos itself.
- **Pessimistic offline RL (CQL, IQL)** for the initial bootstrap before
  live paper-trading.

### What DOESN'T work (don't fall for these):

- **Single-asset RL on 5 years of data.** You get one episode. RL needs
  distribution.
- **Reward = portfolio value.** You'll evolve into a max-leverage gambler.
- **Self-play between analysts.** Markets aren't zero-sum games between
  your own modules.
- **Letting the analysts evolve independently against backtest performance.**
  They'll all collapse onto the same overfitted regime.

### The auto-evolve property comes from:

- **Population-based training** of analyst hyperparams (a la DeepMind PBT)
  on rolling validation.
- **Continual learning of the aggregator** with experience replay weighted
  toward recent regimes.
- **Periodic kill-and-spawn of analyst variants** — kill the bottom 20% by
  walk-forward Sharpe over the last K windows, spawn mutated copies of the
  top 20%. **Critical:** the kill criterion is robust validation Sharpe,
  not raw return.

---

## Mapping to existing infra

You already have most of what's needed:

| Need | What you have |
|---|---|
| Compute for training | Yggdrasil cluster (Huginn 4×V100, Muninn 2×3050), MLflow in muspelheim |
| Job orchestration | Kueue queues, k8s/jobs templates pattern from eidolon |
| Experiment tracking | MLflow (frozen W&B for paper figures) |
| Storage | SeaweedFS PVCs (datasets/checkpoints/artifacts/cache) |
| ADR pattern | 70 ADRs already, this would slot in cleanly |
| Multi-model scatter | model-roster + parallel-critique skill (use this for news-llm analyst!) |

The PDR scaffolding code in `pdr_lwm/` is also reusable — silence-biased
gates, surprise signal, the architectural pattern. You'd **fork the
pattern, not the trained weights** (vision/audio/text encoders aren't
relevant; market data needs market-data encoders).

---

## What I'd build first (concrete next step)

Don't try to build the whole framework. Build the **vertical slice that
proves the architecture**:

### MVP — "Three-analyst committee on liquid crypto"

1. One asset (BTC/USDT, because clean data, 24/7, no halts).
2. Three analysts: Kronos-base (forecaster), classical-TA (baseline),
   microstructure (order book imbalance). All wrapped to the uniform
   `Analyst` interface.
3. Bayesian aggregator (no RL yet) — just calibrated probability fusion.
4. Strict risk gate.
5. **Paper-trade for 4-8 weeks logging every decision + every analyst's
   contribution.**
6. Then introduce the RL aggregator and see if it beats the Bayesian
   baseline out-of-sample.
7. Then add analyst #4 (options/perp flow).
8. Then add asset #2.

This is the test: if your three-analyst committee on BTC can't beat
buy-and-hold risk-adjusted on paper, more analysts won't fix it. The
ensemble pattern only adds value if the analysts are **genuinely
decorrelated in their errors** — and you only learn that empirically.

---

## Things I want to flag before you start

- **Stocks vs options is a HUGE jump.** Options add Greeks, IV surface,
  expiry, assignment risk, pin risk, dividend timing. If you want options
  too, the analyst pool needs an IV-surface analyst and the risk gate
  needs Greek limits. Don't bolt this on later — design the schema for it
  now (the Kairos approach of fixed exogenous channels is sensible).
- **Latency matters and will dominate the architecture.** A 1-minute-bar
  strategy has ~50s budget per decision; a high-frequency strategy has
  microseconds. Pick your latency tier first — 1m is the sweet spot for
  a hobby/research framework, anything sub-second needs colocation and a
  different stack entirely.
- **Survivorship bias in backtests is brutal.** Use point-in-time
  databases (or accept the bias and discount results 30%).
- **Regime detection is the unsolved problem.** Your 2024-trained model
  will not work in 2025's regime. The continual-learning loop is what
  makes this real.
- **AAAI 2026 acceptance ≠ alpha.** Kronos is a good foundation model. It
  is NOT a profitable trading system. Anyone who tells you otherwise is
  selling something.

---

## Branch points (pick before starting)

- **Asset class focus** — crypto-only (cleaner, 24/7), equities-only
  (deeper history, regulatory clarity), or both? **Strongly recommend
  crypto-first for the MVP regardless of your end goal.**
- **Latency tier** — daily/hourly (research-friendly), 1-5min (sweet
  spot), sub-second (different stack)?
- **Where does this live?** New repo entirely, or fork eidolon's
  `pdr_lwm` pattern? **Lean new repo with the PDR scaffolding pattern
  copied** — different domain, different encoders, different reward.
- **Start with the vertical slice MVP** outlined above, or write a more
  complete design doc / ADR-style spec first?

---

## How this charter has been honored so far (post-hoc, 2026-05-13 evening)

This block is appended after-the-fact to map the bullets above to actual
shipped code:

| Charter clause | Shipped state |
|---|---|
| **PDR mapping clean for trading** | ADR-0014 (advisor surface) + ADR-0015 (HITL React) lock the three-stage pipeline |
| **Layer 1 — Analyst Pool with uniform interface** | `hermes_quant.protocol.Analyst` Protocol (ADR-0002); `analysts/classical_ta.py` shipped in v0.1.1; entry-point discovery via `[project.entry-points."hermes_quant.analysts"]` |
| **Layer 2 — Aggregator (Bayesian first, RL later)** | `aggregators/bma.py` (Beta-binomial BMA) shipped in v0.1.1; `StackingAggregator` v0.1.3; RL aggregator deferred per charter "what doesn't work" guidance |
| **Layer 3 — Risk/Execution gate (hard rules, not learned)** | `risk/gate.py::DefaultRiskGate` — 8 rules including Kelly cap (quarter-Kelly), max-position-pct, drawdown circuit breaker, daily-loss circuit breaker, cost gate `|edge| > 2× round-trip`, edge-sign alignment guard. ADR-0004. Silence-by-default by Protocol contract. |
| **Kronos is one analyst, not the oracle** | ADR-0002 + ADR-0012 (LLMAnalyst deferred) lock this. `KronosAnalyst` planned for v0.1.2; aggregator weights it like every other analyst. |
| **MVP — three-analyst BTC/USDT committee on paper** | classical-TA shipped; microstructure-lite registered as entry-point but not implemented; ccxt provider for BTC/USDT pending v0.1.2 |
| **Paper-trade 4-8 weeks before RL** | ADR-0015 HITL paper-React surface lands the paper-trade book |
| **Walk-forward + embargo + log-return-after-costs reward + multi-regime** | Deferred to v0.4+ when RL aggregator considered |
| **Stocks vs options is a HUGE jump** | `AssetClass = Literal["crypto","equity","etf","fx","option"]` with options gated `# deferred to v0.2 (requires Greeks-aware sizer)` per ADR-0009 §P2-options |
| **Survivorship + point-in-time** | `as_of` parameter on `DataProvider.fetch_bars()` plumbed for v0.1.2 (ADR-0005 amendment); leaf-level filter prevents lookahead |
| **Money-software discipline** | "LLMs out of action path" locked in ADR-0012; calibrators required for confidence; portfolio reconstruction pinned to executions.jsonl (ADR-0011) |

The "vertical slice that proves the architecture" frame — three analysts +
Bayesian aggregator + strict gate, paper-trade weeks before RL — IS the
v0.1.x roadmap. Fidelity to this charter is the architectural success
criterion.
