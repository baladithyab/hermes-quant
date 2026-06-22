# Research: time-series foundation models as cowork-quant analysts (2026-06-09)

> Lens: cowork-quant runs in a **sandboxed Cowork environment without GPU and without torch**.
> Every candidate is judged on (a) hosted-API accessibility, (b) honest evidence of predictive
> value on financial bars, (c) fit as ONE analyst emitting an `AnalystView {direction, magnitude,
> confidence, horizon}` into the Bayesian aggregator. LLMs and FMs stay out of the money path;
> the deterministic risk gate owns execution. Advisor/paper-only.
>
> Baseline: hermes-quant `docs/research/05-kronos-integration.md` + `ADR-0018-kronos-analyst.md`
> (both 2026-05-13). This doc records what changed since then and what is new.

## TL;DR

- **Kronos is unchanged since the May notes** — repo at ~27.8k stars, README news still ends at
  the 2025-11-10 AAAI-2026 acceptance; HF weights (`NeoQuasar/Kronos-{mini,small,base}`) last
  pushed 2025-09-09. No releases, no API surface changes. Everything in ADR-0018 still holds —
  but **local torch inference is now off the table for cowork-quant**, so the v0.x integration
  path is a *hosted wrapper* (HF Space / self-hosted FastAPI), not `pip install torch`.
- **Kairos (Shadowell) pivoted to crypto-only and shipped public checkpoints + a serve mode.**
  README now reports three honest IC results (best: BTC/ETH 2y spot on Kronos-base, h30
  rank-IC **+0.076**, ICIR **+0.484** — exactly the number in our May notes). A-share results
  are gone from the README (consistent with the negative IC we recorded). Two checkpoints are
  public on HF (`Shadowell/Kairos-{small,base}-crypto`, updated 2026-04-21) and the repo ships
  `kairos-serve`, a FastAPI `/predict` endpoint that accepts caller-supplied OHLCV bars — the
  closest thing to a ready-made hosted-analyst contract in this whole space.
- **kronos-financial-analyzer (junshunG) is a 1-star MVP wrapper** (an "OpenClaw Skill") whose
  headline anti-pattern is a **silent fallback from Kronos to a GBM Monte-Carlo simulator**
  when weights fail to load. Useful only as a cautionary example; do not depend on it.
- **New since mid-2025, the only true hosted-API analyst is Nixtla TimeGPT** (API-only product,
  quantile forecasts, finance in the training corpus; TimeGPT-2 family announced in private
  preview). Chronos-2 (Amazon, 2025-10-20) is the strongest new open generalist (multivariate +
  covariates, quantile output, Apache-2.0) but is torch-local unless wrapped. FinCast (CIKM'25)
  is the first finance-specific billion-parameter FM with open Apache-2.0 weights, but no
  hosted endpoint and modest independent validation.
- **Recommendation for cowork-quant v0.x:** one `FoundationModelAnalyst` slot behind an HTTP
  client. Primary backend: a self-hosted/HF-Space **Kronos or Kairos `/predict` service**
  (keeps the distributional path-agreement confidence design from ADR-0018 server-side).
  Optional backend: **TimeGPT API** (quantile spread → confidence). Defer Chronos-2/FinCast
  until someone hosts them or we get a torch-capable worker.

---

## 1. Kronos (shiyu-coder, AAAI 2026, arXiv:2508.02739)

**Repo state (checked 2026-06-09):** ~27.8k stars / 4.8k forks, 76 commits, no releases or
tags. Top-level dirs: `examples/`, `figures/`, `finetune/`, `finetune_csv/`, `model/`,
`tests/`, `webui/`. README "News" section's latest entry remains **2025-11-10 (AAAI 2026
acceptance)**; before that 2025-08-17 (fine-tuning scripts) and 2025-08-02 (arXiv). No
substantive feature announcements since the May research pass.
Source: https://github.com/shiyu-coder/Kronos

**Model sizes (unchanged):**

| Model | Params | Context | Tokenizer | Open |
|---|---|---|---|---|
| Kronos-mini | 4.1M | 2048 | Kronos-Tokenizer-2k | yes |
| Kronos-small | 24.7M | 512 | Kronos-Tokenizer-base | yes |
| Kronos-base | 102.3M | 512 | Kronos-Tokenizer-base | yes |
| Kronos-large | 499.2M | 512 | — | **no** (closed) |

HF weights last modified **2025-09-09**; downloads are enormous (Kronos-base 3.7M,
Kronos-small 3.4M). MIT license. Sources: https://hf.co/NeoQuasar/Kronos-base ·
https://hf.co/NeoQuasar/Kronos-small · https://hf.co/NeoQuasar/Kronos-mini

**API notes vs May doc:** README also documents `predict_batch` for multi-asset parallel
prediction (same lookback/pred_len constraints). Distributional caveat from 05-kronos
research stands: `predict()` averages `sample_count` paths internally — a wrapper service
must expose the pre-mean cube (or at least per-path terminal returns) to compute
path-agreement confidence.

**Hosted-API angle (new, matters for cowork-quant):** there is **no official hosted
endpoint**, and HF serverless inference does not cover the custom `time-series-forecasting`
pipeline. However, ~30 community HF Spaces wrap Kronos, several explicitly as APIs (e.g.
`yingfeng64/kronos-api`, `omaraj22/kronos-trading-signals-api`, `guestros/kronos-trading-api`
— listed on the model card at https://hf.co/NeoQuasar/Kronos-base). Community Spaces are
unpinned and untrusted; for anything beyond a demo we should deploy **our own** Space or
container pinned to a Kronos SHA + weights revision.

**Lens verdict:** (a) hosted only via DIY wrapper; (b) AAAI acceptance ≠ alpha — charter
stance unchanged; only third-party IC evidence is Kairos (below); (c) slots in cleanly if
the wrapper returns per-path returns so the plugin-side adapter can compute direction =
sign(median r), magnitude = clip(|median r|), confidence = path agreement (ADR-0018 D5),
clipped to [0.30, 0.85] (D3).

## 2. Kairos (Shadowell) — the crypto Kronos fine-tune

**Repo:** https://github.com/Shadowell/Kairos — 44 commits, 1 star, MIT. Described as a
"Multi-market Kronos fine-tuning toolkit"; README (current) says scope is now **crypto only:
spot + USDT-margined perpetuals** on OKX-compatible data. The A-share track has been dropped
from the README — consistent with the negative A-share IC our May notes recorded. Vendors a
Kronos source snapshot (`kairos/vendor/`), adds a fixed **32-dim exogenous channel** (24
generic OHLCV factors + 8 crypto factors: funding rate, OI change, basis, market ret/vol,
hour-of-day sin/cos) via a `KronosWithExogenous` model class with a quantile return head.

**Honest IC results (from README "当前状态" table; horizon 30 on 1-min bars):**

| Experiment | Universe | h30 Rank-IC | h30 ICIR |
|---|---|---|---|
| BTC/ETH 2y spot (Kronos-small base) | 2 coins | +0.050 | +0.325 |
| Top-100 1y spot | 100 coins | +0.030 | +0.454 |
| BTC/ETH 2y spot on **Kronos-base** | 2 coins | **+0.076** | **+0.484** |

These match the project's May numbers exactly (crypto 1-min h30 rank-IC +0.076, ICIR +0.484).
The README itself warns to judge results as **increments over the un-fine-tuned baseline,
not absolute IC**, and flags the perp multi-channel path as experimental ("第一次 Top10 30 天
永续实验应视为复盘样本" — replay sample, not production). That is the right epistemic posture
and worth preserving in our own docs. Source: https://github.com/Shadowell/Kairos

**Public checkpoints (new since May):**

- `Shadowell/Kairos-small-crypto` — 25.4M params, updated 2026-04-21, MIT.
  https://hf.co/Shadowell/Kairos-small-crypto
- `Shadowell/Kairos-base-crypto` — 104.0M params, updated 2026-04-21, MIT, ~1.6K downloads.
  https://hf.co/Shadowell/Kairos-base-crypto

**Serve mode (new, directly relevant):** `kairos-serve` is a FastAPI service whose
`/predict` endpoint takes a JSON body of `{symbol, market_type, freq, bars[{datetime, open,
high, low, close, volume, amount?}]}` — the service does **not** fetch exchange data itself.
That contract maps almost 1:1 onto what a cowork-quant hosted analyst needs: plugin sends
bars from its own data layer, receives a forecast, adapts to AnalystView locally.

**Lens verdict:** (a) hosted via self-deployed `kairos-serve` (no public endpoint); (b) the
only Kronos-family artifact with published rank-IC/ICIR on crypto — small but real, and
honest about baselines; A-shares remain a documented failure mode; (c) best-fit backend for
a crypto-scoped FoundationModelAnalyst, with the caveat that the exogenous features assume
1-min OKX-style data — our 5m/15m timeframes need the 5/15/30 unified-window factor schema
the repo only *proposes* (docs/CRYPTO_5_15_30_FACTOR_SCHEMA_PROPOSAL.md). Confidence must
still be computed on our side (path agreement or quantile spread), not trusted from the
checkpoint.

## 3. kronos-financial-analyzer (junshunG)

**Repo:** https://github.com/junshunG/kronos-financial-analyzer — 48 commits, 1 star, 0
forks, MIT, 1 tag. Self-described as an MVP "OpenClaw Skill" (SKILL.md + manifest.json)
wrapping Kronos with yfinance/ccxt/akshare data fetchers, plotly reports, a toy backtester,
and an `api_server/`. v0.3.0 loads real `kronos-small` from HF by default.

**Status & assessment:** hobby-grade, single-author, near-zero adoption. Two design choices
are instructive **as anti-patterns** for cowork-quant:

1. **Silent model substitution:** if Kronos weights fail to download it "automatically falls
   back to `SimplePredictor`" — a geometric-Brownian-motion Monte-Carlo *simulator* — while
   continuing to emit BUY/SELL/HOLD signals and confidences. A money-adjacent system must
   never swap an alpha model for a random-walk simulator silently; hermes' lazy-load design
   (abstain with zero confidence, surface `load_error` in health) is the correct contrast.
2. **Heuristic confidence:** `confidence = 1/(1 + relative_std × 10)` over 100 MC samples —
   uncalibrated, regime-blind, and presented as a 0-1 certainty score.

The skill-packaging idea (Kronos as a conversational skill with a manifest) is mildly
interesting precedent for a Cowork plugin, but nothing here should be a dependency.

**Lens verdict:** (a) only as DIY-hosted, no advantage over wrapping Kronos directly;
(b) no IC evidence at all; (c) do not integrate — cite as cautionary example in ADRs.

## 4. New / updated TSFMs since mid-2025 (hosted-API lens)

### Nixtla TimeGPT — the only true hosted-API option
- Closed-weights, **API-only** foundation model ("TimeGPT-1", trained on 100B+ points incl.
  finance); Python SDK `nixtla`, REST API, quantile & prediction-interval outputs, anomaly
  detection. Free trial via dashboard API keys; paid beyond. **TimeGPT-2 family (Mini/2/Pro)
  announced in private preview** with self-hosted options. No torch needed client-side —
  pure HTTP. Sources: https://github.com/Nixtla/nixtla · https://www.nixtla.io/ ·
  https://www.nixtla.io/blog/timegpt-2-announcement ·
  https://www.nixtla.io/docs/forecasting/timegpt_quickstart
- Evidence: strong on general benchmarks (vendor-reported); **no published finance IC**.
  Treat as an uncalibrated generalist voice; quantile spread → confidence; cold-start
  shrinkage mandatory.
- Fit: cleanest v0.x integration (API key in config, ~50 lines). Cost and vendor dependency
  are the tradeoffs; also weakest finance-specific prior of the candidates.

### Chronos-2 (Amazon, released 2025-10-20) — strongest new open generalist
- 119.5M-param encoder-only model; univariate, **multivariate, and covariate-informed**
  zero-shot forecasting; native **quantile** outputs; Apache-2.0; SOTA on fev-bench,
  GIFT-Eval, Chronos Benchmark II; ~300 series/sec on one A10G. 85.4M HF downloads; card
  updated 2026-06-05. Sources: https://hf.co/amazon/chronos-2 ·
  https://www.amazon.science/blog/introducing-chronos-2-from-univariate-to-universal-forecasting ·
  https://arxiv.org/abs/2510.15821 · https://github.com/amazon-science/chronos-forecasting
- Chronos-Bolt remains available (`amazon/chronos-bolt-base`, 205M, T5, 45.6M downloads,
  CPU-fast): https://hf.co/amazon/chronos-bolt-base
- Hosted: **no serverless API**; runs via AutoGluon/SageMaker or self-hosted. The covariate
  channel is attractive (could feed funding/OI like Kairos does, zero-shot, no fine-tune).
- Evidence on finance: general-domain benchmarks only; the Kronos paper claims large RankIC
  gains *over* TSFMs of the Chronos-1 generation on price series — finance-specific
  pretraining appears to matter. No public Chronos-2 finance IC yet.
- Fit: best "second FM voice" *if* we stand up our own inference container; quantile head
  maps directly to direction/magnitude/confidence. Defer to v0.x+1.

### TimesFM 2.5 (Google, weights updated 2025-10-02)
- 200M (231M actual) decoder-only, Apache-2.0, max context 16k, **quantile head** (optional
  30M-param add-on), pytorch port: https://hf.co/google/timesfm-2.5-200m-pytorch
- Hosted angle: TimesFM powers **BigQuery `AI.FORECAST`** (managed, SQL-call, no torch) —
  a real hosted path but awkward for per-tick OHLCV calls (data must transit BigQuery), and
  the managed surface exposes univariate point/interval forecasts, not OHLCV semantics.
- No finance-specific evidence. Fit: possible, but BigQuery plumbing is heavier than
  TimeGPT for the same generalist quality. Low priority.

### FinCast (CIKM 2025, arXiv:2508.19609) — first big finance-specific FM after Kronos
- 1B-param decoder-only with Mixture-of-Experts and PQ-Loss (joint point + quantile);
  pretrained on **20B finance time points** (crypto, forex, futures, stocks, macro).
  Claims ~20% MSE / 10% MAE zero-shot improvement over general TSFMs. Weights public,
  Apache-2.0: https://hf.co/Vincent05R/FinCast (0 downloads, 7 likes — essentially unused).
  Sources: https://arxiv.org/abs/2508.19609 · https://github.com/vincent05r/FinCast-fts
- Caveats: 1B params = GPU-class inference; no hosted endpoint; no third-party IC
  replication; single-author code release. Watch-list, not v0.x.

### Others worth one line each
- **Sundial** (`thuml/sundial-base-128m`, ICML 2025, Apache-2.0, updated 2026-03-09):
  *generative* — natively produces sample paths, so path-agreement confidence works without
  subclass hacks; transformers `AutoModelForCausalLM` interface; torch-local though.
  https://hf.co/thuml/sundial-base-128m
- **TiRex** (NX-AI, xLSTM-based, ~35M, **ONNX artifacts on the hub**, updated 2026-02-05):
  top GIFT-Eval zero-shot scores; ONNX could matter later for a no-torch local path, but
  license is "other" (NXAI community license — review before commercial advisory use).
  https://hf.co/NX-AI/TiRex
- **Moirai 2.0** (Salesforce, updated 2026-01-29): **CC-BY-NC-4.0 — non-commercial, ruled
  out.** https://hf.co/Salesforce/moirai-2.0-R-small
- **Toto** (Datadog, 151M, Apache-2.0, updated 2026-05-14): observability-tuned, not
  finance. Skip. https://hf.co/Datadog/Toto-Open-Base-1.0
- **Cisco Time Series Model** (Nov 2025, arXiv:2511.19841): observability-domain; skip.
  https://hf.co/papers/2511.19841
- **TabPFN-TS** (Prior Labs): strong GIFT-Eval; Prior Labs offers a hosted API for TabPFN —
  niche but a conceivable hosted analyst later. https://hf.co/papers/2501.02945
- Benchmark hygiene: recent work documents **information-leakage / temporal-overlap risks in
  TSFM zero-shot evaluations** (https://hf.co/papers/2510.13654) and new benchmarks
  TempusBench (https://hf.co/papers/2604.11529) / TIME (https://hf.co/papers/2602.12147).
  Reinforces the charter rule: benchmark wins ≠ alpha; only our own calibrated IC from
  RealizedOutcomes counts.
- Name collision: an unrelated academic TSFM also called "Kairos" (adaptive tokenization)
  circulated in late 2025 — don't confuse it with Shadowell/Kairos when citing.

## What changed since 2026-05

| Item | May 2026 state (hermes notes) | Now (2026-06-09) |
|---|---|---|
| Kronos repo | AAAI 2026, fine-tune scripts, ~MIT/HF weights | Unchanged; ~27.8k stars; no new releases; weights still 2025-09-09; `webui/`, `finetune_csv/`, `predict_batch` present |
| Kairos | Charter notes: crypto 1m h30 IC +0.076/ICIR +0.484, A-shares negative | README pivoted to crypto-only (spot+perps); same best IC published; **public HF checkpoints (2026-04-21)** + FastAPI `/predict` serve mode; 32-dim exogenous channel |
| junshunG analyzer | (not previously assessed) | 1-star MVP; silent GBM-simulator fallback; avoid |
| Landscape | Kronos effectively the only finance FM considered | Chronos-2 (Oct'25), TimesFM 2.5 (Oct'25), FinCast weights (Aug'25/CIKM Nov'25), Sundial, TiRex, Moirai 2.0 (NC), TimeGPT-2 preview |
| Integration constraint | local torch acceptable (hermes daemon) | **no GPU/torch in Cowork sandbox** → hosted-API wrapper becomes the only viable path |

## Recommendation table (cowork-quant v0.x)

| Model | API-accessible without local torch? | Finance evidence | Verdict for v0.x |
|---|---|---|---|
| Kronos base/small | Only via DIY HF Space / container (community spaces exist but untrusted) | Paper benchmarks; Kairos IC indirectly | **Primary backend** behind our own pinned `/predict` service; reuse ADR-0018 math server-side |
| Kairos-base-crypto | Yes, via self-deployed `kairos-serve` FastAPI | Rank-IC +0.076 / ICIR +0.484 (crypto 1m h30); A-shares negative | **Primary for crypto timeframes**; adopt its bars-in/forecast-out HTTP contract |
| kronos-financial-analyzer | N/A | None | **Reject**; cite GBM-fallback as anti-pattern |
| Nixtla TimeGPT | **Yes — native hosted API** (key + HTTP) | None published (generalist) | **Optional second voice**; easiest to ship; quantile-spread confidence; paid |
| Chronos-2 | No (self-host or SageMaker) | General benchmarks only | Defer; revisit when we have an inference container; covariates attractive |
| TimesFM 2.5 | Partially (BigQuery AI.FORECAST) | None | Low priority; plumbing-heavy |
| FinCast 1B | No; weights open but GPU-class | Paper-only, no replication | Watch list |
| Sundial 128M | No | None | Watch list (native sample paths) |
| TiRex | No (ONNX exists; license "other") | None | Watch list pending license review |
| Moirai 2.0 | — | — | Reject (CC-BY-NC) |
| Toto / Cisco TSM | — | Wrong domain | Reject |

**Concrete v0.x shape:** one `FoundationModelAnalyst` with a `backend: {kairos_serve_url | timegpt}`
config. It POSTs bars, receives either sample paths (Kairos/Kronos wrapper) or quantiles
(TimeGPT), and computes AnalystView locally: direction = sign(median return), magnitude =
clipped |median return|, confidence = path-agreement or quantile-sharpness, clipped
[0.30, 0.85] per ADR-0018, then through the cold-start calibrator. Timeouts/HTTP errors →
abstain (zero-confidence view pruned in `aggregate()`), never a fallback model.

## Open questions

1. **Who hosts the Kronos/Kairos wrapper?** A free-tier HF Space (CPU) likely fits
   Kronos-small at 15-min cadence for a few symbols, but cold starts and Space sleep need
   measurement; a paid Space or small VM is the robust option. Decide ownership + pinning
   (Kronos SHA, weights revision) before v0.x ships it.
2. **Can a wrapper expose the pre-mean sample cube cheaply?** Payload of S×pred_len terminal
   returns is tiny; full OHLCV cubes are not. Define the wire schema (per-path terminal
   return is sufficient for AnalystView math).
3. **Kairos at 5m/15m:** published IC is 1-min h30 only. Does the edge survive at our
   timeframes? The repo's 5/15/30 factor schema is still a proposal — needs our own paper
   IC measurement before granting the analyst non-floor weight.
4. **TimeGPT cost/limits at our cadence** (per-call pricing, rate limits, data egress of
   sending bars to a third party — check operator privacy posture for advisor product).
5. **TimeGPT-2 availability:** private preview now; if/when GA with self-host, does it
   change the build-vs-buy calculus?
6. **Chronos-2 with crypto covariates** (funding, OI, basis) zero-shot vs fine-tuned Kairos:
   a cheap experiment once any torch-capable worker exists; could replace fine-tuning
   maintenance entirely.
7. **Negative-IC detector placement:** with a hosted backend, rolling IC per
   (asset_class, timeframe, horizon) must live plugin-side (the service is stateless) —
   confirm this lands in the calibrator design doc.
