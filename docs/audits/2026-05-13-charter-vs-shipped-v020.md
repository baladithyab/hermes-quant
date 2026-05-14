# hermes-quant: charter-vs-shipped audit + v0.3 backlog enumeration

**Date:** 2026-05-13 (post v0.2.0 release)
**Author:** Codeseys-driven session (orchestrator: claude-opus-4.7)
**Inputs:**
- Founding charter — `docs/charter/2026-05-13-hermes-quant-charter.md` (11 architectural clauses)
- Self-honored mapping table at the bottom of that charter
- Shipped tags: `v0.1.0-alpha`, `v0.1.1`, `v0.1.2`, `v0.2.0`
- ADRs 0001–0016
- 426 tests passing (1 skipped)

This audit is the Phase-2 backlog enumeration for the v0.3 deep-work-loop.
It exists so that future contributors (and future sessions) can see exactly
which charter clauses are honored, which are partial, and which are
deferred — without re-deriving the answer by archeology.

---

## 1. Per-clause status

### ✅ Done (charter clause → shipped, fully aligned)

| Charter clause | Shipped state | Where |
|---|---|---|
| **PDR mapping clean for trading** | All three modes shipped, sharing the advisor pipeline | ADR-0014 (advise) + ADR-0015 (HITL) + ADR-0016 (autonomous) |
| **Layer 1: Analyst Pool with uniform interface** | `Analyst` Protocol (433 loc); entry-point discovery for third-party | `hermes_quant/protocol.py`, ADR-0002 |
| **Layer 2: Aggregator (Bayesian first, RL later)** | BMA Beta-binomial calibrator (266 loc) | `aggregators/bma.py`, ADR-0003 |
| **Layer 3: Risk gate (hard rules, not learned)** | 8 deterministic rules (Kelly cap quarter, drawdown breaker, daily-loss breaker, cost gate, post-loss cooldown, halt check, edge-sign guard, min-trade-size) | `risk/gate.py` (312 loc), `risk/kelly.py`, ADR-0004 |
| **Silence by default REACT gate** | 4-dim silence-bias gate (Confidence × Urgency × Voices × Salience); ALL four must pass | `gates/silence_bias.py` (379 loc), ADR-0016 |
| **"Rewarded for correct inaction"** | Tool surface `dry_run=True` default; mode default `advise`; `min_confidence=0.65` stricter than HITL; kill-switch auto-disable | autonomous.py + tools.py |
| **Money-software discipline** | Tools read-only / no trade-from-chat; CLI is the actuator; cron `--no-dry-run` is what fires | ADR-0007, ADR-0013, ADR-0014 |
| **Survivorship / point-in-time** | `as_of` plumbed leaf-to-orchestrator with TypeError fallback for older providers | `data/yfinance_provider.py`, advisor.py, ADR-0005 amendment |
| **Stocks vs options is a HUGE jump (design schema for it now)** | `AssetClass = Literal["crypto","equity","etf","fx","option"]` with options gated `# deferred to v0.2 (Greeks)` | `protocol.py`, ADR-0009 |
| **PDR scaffolding pattern, not weights** | Only the silence-bias *idea* lifted from Eidolon; all code bespoke for trading | `gates/silence_bias.py` |
| **AAAI 2026 acceptance ≠ alpha** | Three-lock live-mode gate (config + creds + arm-live ceremony) per ADR-0016 §D6; v0.2 is paper-only | autonomous.py, ADR-0016 |

### 🟨 Partial (shipped less than the charter described)

| Charter clause | What charter said | What shipped | Gap |
|---|---|---|---|
| **MVP: three-analyst BTC/USDT committee** | TA + Microstructure + **Kronos** on **BTC/USDT** | TA + MicrostructureLite (OHLCV-only proxies); **no ccxt provider** so no BTC/USDT | KronosAnalyst not built; CcxtProvider not built |
| **Microstructure analyst** | Order book imbalance, queue position, VPIN trade-flow toxicity | OHLCV-derivable subset only: Bollinger %B + Wilder ADX-lite + bar-imbalance VPIN proxy | Real L2/queue/tick microstructure deferred to v0.4+ (requires tick feed not in v0.3 providers) |
| **Continual learning loop** | Calibrator learns from realized fills; analyst weights drift | Settlement journal closes feedback loop (advisor reads lessons each call); BMA can ingest fills | `BMAAggregator.learn_from_fill()` exists but not wired to a re-training cron |

### ❌ Not yet built (deferred — not regressions)

| Charter clause | What's missing | Target version |
|---|---|---|
| **Kronos as one analyst** | `analysts/kronos.py` not yet shipped (decoder-only foundation, BSQ tokenizer wrapper). Charter explicitly says: "Kronos is one analyst, not the oracle" | **v0.3** (this loop) |
| **News-LLM analyst** | `analysts/news_llm.py` not built; would use model-roster scatter | v0.4+ via ADR-0012 LLMAnalyst protocol (already drafted as deferred) |
| **Options/perp flow analyst** | Options analyst not built; AssetClass slot exists | v0.4+ (Greeks-aware sizer is its own ADR) |
| **Fundamental snapshot analyst** | Earnings revisions, guidance, factor exposures | v0.4+ |
| **Regime classifier (HMM/change-point)** | Charter calls this "the unsolved problem" | v0.4+ |
| **Cross-asset analyst** | Lead-lag (DXY → EM, VIX term structure) | v0.4+ |
| **ccxt + Alpaca data providers** | Only yfinance shipped; ADR-0005 has placeholders | **v0.3 (ccxt)**, v0.4 (alpaca) |
| **Walk-forward CV / lookahead / DSR as `evaluation/` module** | `evaluation/` directory empty per AGENTS.md target tree, but `tests/test_no_lookahead.py` ships as a 5-test CI gate (ADR-0006) | **v0.3** — promote test fence into reusable module |
| **RL aggregator** | Charter explicitly defers; ADR-0006 documents graduation criteria | v0.4+ |
| **Population-based training, kill-and-spawn variants** | Charter says this is the auto-evolve property | v0.5+ with RL aggregator |

### 🔵 Built but NOT in original charter (scope expansions you asked for mid-stream)

These came up in conversation and got ADR'd + shipped. They strengthened the foundation rather than diluting it.

| Surface | Why it landed | Where |
|---|---|---|
| **Chat-mode advisor** | "this is going to be a plugin that anyone using hermes could install and have hermes work on quant level stuff" | ADR-0014, advisor.py (644 loc) |
| **HITL propose-approve-react** | "the trading guidance its HITL or automated not just guidance. this is part of the PDR pattern" | ADR-0015, proposals.py + react/ |
| **Settlement journal as PDR feedback loop** | Closes Decide ↔ React loop; advisor reads lessons each call | ADR-0010, journal/ (5 files, 349 loc writer) |
| **Three-mode taxonomy locked into config** | `quant.pdr.mode` flag, no caching, flip without restart | ADR-0015 §D7, ADR-0016 |
| **Watchlist as autonomous-mode contract** | Implied by "watch for opportunities" | ADR-0016 §D5, watchlist.py (284 loc) |
| **Kill switch + per-tick caps** | Charter principle: "AAAI 2026 acceptance ≠ alpha" → defensive layering | ADR-0016 §D9, autonomous.py (462 loc) |
| **`safe_symbol_component` path-safety guard** | Round-2 TradingAgents research found this gap | utils/symbol_safety.py |
| **Monotonic-clock heartbeat** | Round-2 TradingAgents research; survives wall-clock jumps | daemon/heartbeat.py |

---

## 2. Aggregate metrics (post-v0.2.0)

| Metric | Value |
|---|---|
| **ADRs shipped** | 16 (charter implied ~8) |
| **Modules shipped** | 19 of ~27 in target tree (8 deferred to v0.3+) |
| **LOC (impl + tests)** | ~6,200 impl + ~3,000 tests |
| **Tools registered** | 15 (was 0 at session start) |
| **PDR modes** | 3 of 3 (advise + hitl + autonomous) |
| **Tests passing** | 426 / 1 skip — zero regressions across 4 releases |
| **Tags** | v0.1.0-alpha → v0.1.1 → v0.1.2 → v0.2.0 |
| **Charter clauses honored** | 11 of 11 architecturally; 8 of 11 fully implemented; 3 partial; 0 regressions |

---

## 3. Honest read

**You asked for** an Eidolon-PDR-patterned multi-analyst trading framework that watches markets and trades autonomously, with money-software discipline.

**You got:**
- The architecture is fully laid down (16 ADRs)
- The three PDR surfaces are all live (advise + hitl + autonomous)
- The safety rails are in place (silence-by-default at multiple layers, kill switch, three-lock live gate, dry-run-by-default tool surface, paper-only v0.2)
- The analyst pool / aggregator / risk gate scaffolding is the canonical shape
- The plugin is installable today and an operator can run autonomous paper-trading on equities on a 15-min cadence

**The honest gap:** the **MVP recipe you actually wrote down** — *"three-analyst committee on BTC/USDT"* — isn't fully met because:
1. **KronosAnalyst isn't wired** (one of the three analysts you specified)
2. **CcxtProvider isn't shipped** (so we can't fetch BTC/USDT bars; we're stuck on yfinance equities)

With yfinance only, we're stuck on equities/ETFs, which is the second-class citizen the charter said *crypto-first* would beat. **v0.3's job is to ship `KronosAnalyst` + `CcxtProvider`, then dogfood the actual MVP recipe on BTC/USDT** — exactly the path the charter specified before any RL aggregator work.

The scope expansions (advisor + HITL + journal + autonomous) were not in the charter; they came from your mid-stream clarifications. They make autonomous-mode richer than the charter described — it's wired to the same advisor pipeline operators use interactively, and the journal is a learning surface the charter didn't explicitly call for.

---

## 4. v0.3 backlog (this loop's targets)

Priority ordered. P0 = blocks "true MVP" status per the charter; P1 = unlocks dogfooding; P2 = nice-to-have.

| ID | Item | Charter clause | Priority | ADR needed | Complexity |
|---|---|---|---|---|---|
| **V03-1** | `CcxtProvider` for crypto bars | "MVP — three-analyst BTC/USDT committee" | **P0** | ADR-0017 | M |
| **V03-2** | `KronosAnalyst` lazy-load wrapper, OHLCV-only fallback when weights absent | "Kronos as one analyst, not oracle" | **P0** | ADR-0018 | L |
| **V03-3** | `evaluation/` module promotion: `cv.py` (PurgedWalkForward), `lookahead.py` (`shuffle_timestamps_test`), `dsr.py` (Deflated Sharpe placeholder) | "Walk-forward CV + lookahead + DSR" | **P1** | ADR-0019 | M |
| **V03-4** | `hermes quant autonomous start` writes the cron job automatically (currently it just prints the command) | "Cadence via Hermes cron per ADR-0013 §D4" | **P1** | amendment to ADR-0016 | S |
| **V03-5** | Calibrator-from-fills wired to a re-training cron (closes the v0.1.2 partial) | "Continual learning loop" | **P1** | amendment to ADR-0010 | M |
| **V03-6** | `BMAAggregator.calibration_quality()` surfaced in `quant_doctor` output (operators see calibrator drift) | "Calibration drift auto-detected" (per AGENTS.md) | **P2** | none | S |
| **V03-7** | Add `OHLCVCache` per ADR-0009 §P2 (file cache layer; reduces yf rate-limit pressure for backtests) | TradingAgents pattern, "money-software reproducibility" | **P2** | amendment to ADR-0005 | S |
| **V03-8** | `portfolio_loader.py` rewrite per ADR-0011 (gates calibrator-learn-from-fills lift to v0.4+) | ADR-0011 portfolio reconstruction | **P2** | amendment to ADR-0011 | M |

### Out of scope for v0.3 (defer)

| Item | Why deferred |
|---|---|
| RL aggregator | Charter says paper-trade 4-8 weeks first; we don't have BTC/USDT data flowing yet |
| News-LLM, fundamental, regime, cross-asset analysts | All require Kronos slot to land first as the third-voice template |
| Options analyst + Greeks sizer | Discrete v0.4+ scope per ADR-0009 §P2-options |
| Real microstructure (L2/tick/VPIN proper) | Requires tick feed; v0.3 providers don't expose |
| `AlpacaReactor` for live equity, `CcxtReactor` for live crypto | Three-lock live gate per ADR-0016 §D6 has more design work; paper-only stays canonical |
| Discord buttons for HITL approve/reject | Text-mode is the v0.2 baseline; UI polish defers |

---

## 5. v0.3 wave structure (preview — locked in Phase 5)

**Wave A** — `CcxtProvider` (V03-1) + smoke test
- File owner: `hermes_quant/data/ccxt_provider.py` + tests
- Acceptance: `python -c "from hermes_quant.data.ccxt_provider import CcxtProvider; bars = CcxtProvider().fetch_bars('BTC/USDT', timeframe='1h', start=...)"` returns ≥100 bars

**Wave B** — `KronosAnalyst` (V03-2)
- File owner: `hermes_quant/analysts/kronos.py` + tests
- Acceptance: instantiates with `model='base'` (102M); falls back to OHLCV-only signal when weights absent (`ImportError` on `kronos` package); 2-voice + Kronos triple-analyst BMA produces ranked aggregated signal

**Wave C** — `evaluation/` module (V03-3) + autonomous cron writer (V03-4)
- File owners: `hermes_quant/evaluation/{cv,lookahead,dsr}.py`, `hermes_quant/cli/__init__.py::_autonomous_start` rewrite
- Acceptance: existing `tests/test_no_lookahead.py` imports from `evaluation.lookahead` (not standalone); `hermes quant autonomous start --cadence 15m` actually creates the cron job

**Wave D** — calibrator-from-fills cron (V03-5) + doctor surfaces (V03-6)
- File owners: `hermes_quant/scripts/calibrator_retrain.sh`, `hermes_quant/tools.py::quant_doctor`
- Acceptance: cron-script reads recent fills from executions.jsonl, updates `state.db::calibrator_state`; `quant_doctor` reports calibrator drift

P2 items (V03-7, V03-8) only run if budget allows after waves A-D.

---

## 6. Risk pre-mortem (Phase-4 hook)

If v0.3 ships and fails catastrophically in 3 months, what's the post-mortem?

1. **Kronos weights download failure path** — operators install hermes-quant on a box without internet to HuggingFace, KronosAnalyst either silently disables or noisily errors at advisor.recommend() time. **Mitigation:** lazy-load at first `analyze()` call (not register-time); on failure, log + emit "abstain" view (zero confidence); document the offline-install path.
2. **ccxt-binance API drift** — Binance changes their futures klines endpoint, advisor breaks for ALL symbols on the watchlist (vs single-symbol). **Mitigation:** per-symbol error isolation already shipped in autonomous.tick(); add fetch-bars retry with exponential backoff in CcxtProvider.
3. **Kronos analyst dominates BMA** — Kronos confidence is consistently 0.95+ because the foundation model is overconfident on tokenized OHLCV; ClassicalTA and MicrostructureLite get downweighted; v0.4 RL aggregator inherits a single-voice prior. **Mitigation:** force min/max confidence clipping on Kronos output (`confidence ∈ [0.3, 0.85]`); add a calibration check in `quant_doctor` that compares Kronos confidence to realized accuracy.
4. **Walk-forward CV catches no overfitting because we have no parameter search** — `evaluation/cv.py` exists but the analysts are hand-tuned, so it's purely a future-tense gate. **Mitigation:** OK; that's the design. ADR-0019 should explicitly say "this gate is for v0.4+ RL training; v0.3 ships it as scaffolding."
5. **The "MVP per charter" is dogfoodable in v0.3 but not actually profitable** — three-analyst BTC/USDT committee + 4-8 weeks paper trading per the charter's recommendation produces a Sharpe < buy-and-hold. **Mitigation:** that IS the test. Document in v0.3 release notes that "paper-trade-and-measure" is the next required step before v0.4 RL work begins. The charter explicitly says: *"if your three-analyst committee on BTC can't beat buy-and-hold risk-adjusted on paper, more analysts won't fix it."*

These all become Phase-4 ADR amendments or wave-plan acceptance criteria.

---

## 7. Phase-2 exit criterion (this doc)

✅ Backlog enumerated with priorities + dependencies + ADR mappings
✅ Greenfield-check passed: hermes-quant is the host; nothing duplicate
✅ Pre-mortem hooks identified for Phase 4
✅ Wave structure preview (locked in Phase 5)
✅ Out-of-scope list explicit so future sessions don't relitigate

Phase 3 (parallel research) begins next: deepwiki + tavily on Kronos repos + ccxt patterns + paper-book P&L attribution.
