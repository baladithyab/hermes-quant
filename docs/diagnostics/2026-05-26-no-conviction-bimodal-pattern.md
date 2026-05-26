# Diagnosis: bimodal-conviction pattern in the daily brief

**Date**: 2026-05-26
**Author**: subagent (architecture diagnostician)
**Scope**: `~/.hermes/quant/daily-briefs/20260526T123039Z-interim.json`
**Status**: Diagnosis only — fixes tracked in parallel work-streams (§5)
**Cross-cuts**: ADR-0002 (Analyst Protocol), ADR-0016 §D2 (silence-bias gate), ADR-0018 (Kronos), ADR-0023 (deliberative committee), `docs/reviews/2026-05-13-v030/architecture.md` §P0-1/P0-2

---

## 1. Executive summary

The 28-symbol watchlist in the 2026-05-26 12:30Z interim brief produced a strictly bimodal output — **9 symbols at `confidence=0.8`** (gate-pass, `kelly_fraction=±0.20`) and **19 symbols at `confidence=0.0`** (`silenced_by_gate`). No symbol in the 0.05–0.79 band. That is the signature of one analyst doing all the work; the ensemble has collapsed to a unanimous-vote of one.

- **BUG 1 — Kronos missing.** `KronosAnalyst._lazy_load()` cannot `import kronos`; every call returns `_abstain(reason="kronos package not installed")`. The 3-voice ensemble is structurally a 2-voice one.
- **BUG 2 — Microstructure abstains on most names.** `MicrostructureLite` emits a view only when at least one of {Bollinger, trend-quality, toxicity} sub-signals trips a threshold; otherwise it returns `None` and is silently dropped from `analyst_views[]`.
- **BUG 3 — ClassicalTA permanently emits zero confidence.** `analysts/classical_ta.py:276-279` cold-start branch calibrates `confidence = max(0.0, raw - 0.20)`. Typical raw ≈ 0.5 × 0.4 = 0.20 floors to **0.0**. Holds until 200 settled trades — which the system has never accumulated, because it has never traded.

Net effect: ClassicalTA contributes literally zero, Kronos abstains, so **MicrostructureLite is the only voice**. When it fires the ensemble jumps to ~0.8; when it abstains the ensemble emits 0.0. The gap is the bug.

---

## 2. Verified observations

### 2.1 Brief-level counts

```
universe_size : 28
actionable    : 9   (NET, DDOG, MDB, CRWD, ZS, S, GTLB, SMCI, MRNA)
silent        : 19  (PLTR, IOT, PATH, ESTC, ANET, …)
data_blocked  : 0
failed        : 0
is_eod        : false
```

All 28 symbols had `data_ok=true` and `bars_received=275`. Data is **not** the issue.

### 2.2 Per-symbol entries (from the interim brief)

The advisor's `analyst_views[]` array is consumed upstream and not echoed into the brief; the brief surfaces only the post-aggregation per-symbol record.

```json
// NET — fires
{"symbol": "NET", "data_ok": true, "data_quality": {"bars_received": 275, "gaps": []},
 "direction": 1, "confidence": 0.8, "magnitude": 0.02683612637524796,
 "horizon": "4h", "kelly_fraction": 0.2,
 "gate_pass": true, "gated_reason": null, "recommended_action": "long_with_stop"}

// MRNA — fires (short)
{"symbol": "MRNA", "data_ok": true, "data_quality": {"bars_received": 275, "gaps": []},
 "direction": -1, "confidence": 0.8, "magnitude": 0.024023428271182215,
 "horizon": "4h", "kelly_fraction": -0.2,
 "gate_pass": true, "gated_reason": null, "recommended_action": "short_with_stop"}

// PLTR — silent (representative of all 19 silenced; IOT, ANET identical-shape)
{"symbol": "PLTR", "data_ok": true, "data_quality": {"bars_received": 275, "gaps": []},
 "direction": 0, "confidence": 0.0, "magnitude": 0.0,
 "horizon": "0m", "kelly_fraction": 0.0,
 "gate_pass": false, "gated_reason": "silenced_by_gate", "recommended_action": "gated"}
```

### 2.3 Inferred upstream `analyst_views` (per advisor pipeline)

Every symbol every tick produces three views; on this run the structure is:

```python
analyst_views = [
    # ALL symbols, every tick:
    AnalystView(analyst="kronos", direction=0, confidence=0.0, confidence_raw=0.0,
                rationale="abstain: kronos package not installed: ...",
                metadata={"abstain": True, "reason": "kronos package not installed: ..."}),

    # ALL symbols (raw>0 sometimes; calibrated always 0.0 in cold-start):
    AnalystView(analyst="classical_ta", direction=±1, confidence=0.0, confidence_raw=0.20,
                rationale="agreement=2/4 (rsi_mean_reversion,macd_cross)",
                metadata={"sub_signals": [...]}),

    # ~9 of 28 symbols emit a real view; the other ~19 return None:
    AnalystView(analyst="microstructure_lite", direction=±1, confidence≈0.8, confidence_raw≈1.0,
                rationale="[microstructure] toxicity=+1@1.00",
                metadata={"active_subsignals": ["toxicity"], ...}),
]
```

---

## 3. Root cause per bug

### 3.1 BUG 1 — Kronos not installed

**Location**: `hermes_quant/analysts/kronos.py:219-235`, `_lazy_load()`.

```python
try:
    from kronos import Kronos, KronosPredictor, KronosTokenizer  # type: ignore
except ImportError as exc:
    self._abstain_reason = (
        f"kronos package not installed: {exc}. "
        "Install with: pip install 'hermes-quant[kronos]'"
    )
```

Every subsequent `analyze(ctx)` returns `_abstain(reason=...)` (`kronos.py:260-274`) — a real `AnalystView` with `confidence=0.0, direction=0, metadata={"abstain": True}`. The view is JSON-serializable and is appended to `analyst_views` upstream of the silence-bias gate. Per ADR-0018 §D1, Kronos is *"one analyst, not the oracle"*; in this configuration the third voice is structurally absent. Confirmed via `~/.hermes/hermes-agent/venv/bin/python3 -c "import kronos"` → `ModuleNotFoundError`.

### 3.2 BUG 2 — Microstructure silently abstains on most names

**Location**: `hermes_quant/analysts/microstructure.py:266-301`, `analyze(ctx)`.

The abstain path is *not* L2-feed-related (the docstring at lines 1-37 makes clear v0.1.2 deliberately uses only OHLCV-derivable features). The gating is:

```python
sub_signals = [
    self._bollinger_signal(close),         # 0 unless %B < 0.05 or > 0.95
    self._trend_quality_signal(close, ...),# 0 unless ADX-quality > 0.6 AND |imbalance| > 0.1
    self._toxicity_signal(bars, atr_rel),  # 0 unless ATR/close > 0.005 AND |imbalance| > 0.15
]
active = [s for s in sub_signals if s.direction != 0]
if not active:
    return None                            # silence — NOT serialized as a view
# ...
if abs(net_score) < 1e-6:
    return None                            # disagreement → also silence
```

When `analyze()` returns `None`, the view is **dropped from `analyst_views[]`** (per the `Analyst` Protocol: `Optional[AnalystView]`). On the 19 silent symbols, none of the three sub-signals breached its threshold, so microstructure emits no view at all. On the 9 firing symbols (NET, DDOG, MDB, CRWD, ZS, S, GTLB, SMCI, MRNA), at least one tripped — typically `toxicity=±1@~1.0` → `raw_confidence ≈ 1.0` → calibrated ≈ 0.8.

The 9/28 hit-rate is consistent with bars >24h stale (`last_bar_age_minutes ≈ 6270 ≈ 4.4 days`); on stale tape the toxicity signal dominates because realized intraday ATR was wide, while Bollinger and trend-quality typically don't breach.

### 3.3 BUG 3 — ClassicalTA confidence floored at 0 in cold start

**Location**: `hermes_quant/analysts/classical_ta.py:270-279`.

```python
agreement = len(contributing) / n_total
mean_sub_conf = float(np.mean([s.raw_confidence for s in contributing]))
confidence_raw = float(np.clip(agreement * mean_sub_conf, 0.0, 1.0))

# Calibrate (cold-start: max(0, raw - 0.20))
try:
    calibrated = self.calibrator.calibrate(confidence_raw)
except CalibratorNotReady:
    calibrated = max(0.0, confidence_raw - 0.20)
```

With 4 sub-signals (RSI, MACD, BB, MA-cross), typical agreement = 2/4 = 0.5 and typical sub-confidence ≈ 0.4, yielding `confidence_raw ≈ 0.5 * 0.4 = 0.20`. The cold-start shrinkage subtracts 0.20, flooring at **0.0**. The view is still emitted (not `None`) but its post-calibration confidence is identically zero.

`ColdStartCalibrator` becomes ready only after ≥200 settled trades have been ingested via `update(outcome)`. The system has not traded, so the cold-start branch is the only branch ever taken. ClassicalTA's contribution to BMA is therefore **structurally null**: it provides a signed direction but a multiplicative-zero weight in the confidence-weighted aggregator. BMA's aggregated confidence ≡ microstructure confidence (when microstructure fires) or zero (when it doesn't). Bimodality follows by construction.

---

## 4. Why this maps to ADR-0016 §D2 `min_analysts_emitted=2`

ADR-0016 §D2 codifies the silence-bias gate's *Compute Budget* dim as `min_analysts_emitted: int = 2`, with the rationale *"a single-voice signal is never enough in autonomous mode"*. The gate (`hermes_quant/gates/silence_bias.py:170-182`):

```python
n_emitted = len(views)
if n_emitted < cfg.min_analysts_emitted:
    return GateResult(decision=GateDecision.SILENCE_INSUFFICIENT_VOICES, ...)
```

Two failure modes here, both flagged in `docs/reviews/2026-05-13-v030/architecture.md` §P0-1/P0-2:

1. **Abstainers count as voices.** ADR-0018 §D4 specifies BMA must filter `confidence < 0.10` views before aggregation, so Kronos's abstain stub is pruned before the gate sees it. `bma.py::aggregate()` does **not** implement that filter (review §P0-1). Kronos abstain + ClassicalTA zero-conf + microstructure real-fire = `len(views)=3`, which trivially passes `min_analysts_emitted=2`. The gate is then evaluating a single-voice signal it believes to be three-voice.

2. **Zero-confidence ClassicalTA is a phantom voice.** Even with the ADR-0018 §D4 filter, a `confidence=0.0` ClassicalTA view is below the 0.10 floor and would be pruned — leaving only microstructure (when it fires) or nothing (when it doesn't). The gate's single-voice safeguard *should* trigger `SILENCE_INSUFFICIENT_VOICES` on every actionable row in this brief. It is currently not, because the filter is not implemented and abstain-stubs are still being counted.

The bimodality the brief shows is the *visible symptom* of the silence-bias gate's voice-count dim being inert. The 9 actionable rows fire on a single voice; the 19 silenced rows are silenced because even that single voice is missing.

---

## 5. Remediation — parallel work-streams

This document is diagnosis only. Fixes are tracked elsewhere:

| Bug | Work-stream | Status |
|---|---|---|
| **Bug 3 (smoking gun)** | `classical-TA-warm-start-fix` — replace cold-start `max(0, raw - 0.20)` with a calibrated prior (e.g., shrink toward 0.30 baseline; or Beta(α=2, β=5) prior approaching the live calibrator as samples accumulate) | In progress |
| **Bug 1 (Kronos)** | `kronos-install-or-skip` — pin and install `[kronos]` extra in the daemon venv, or skip Kronos in advisor's analyst registry when `import kronos` fails so it never appears in `analyst_views[]`. ADR-0018 §D4 BMA `confidence<0.10` filter is the canonical location. | In progress |
| **Bug 2 (Microstructure)** | `microstructure-fractional-improvements` — broaden sub-signal coverage beyond OHLCV proxies (real L2 via Alpaca/IBKR, or per-symbol adaptive thresholds). | **Deferred** — not blocking; the bimodal pattern resolves once Bugs 1 and 3 are fixed because ClassicalTA will then contribute a real second voice on most names. |

Order matters: **Bug 3 must land first.** Fixing Bug 1 alone (pruning Kronos abstainers) without fixing Bug 3 leaves a single-voice ensemble on most symbols → `SILENCE_INSUFFICIENT_VOICES` everywhere, which is the *correct* behaviour but produces zero actionable output until calibrator warm-up — which never happens because the system never trades. Deadlock.

---

## 6. Verification — before/after metrics

### Baseline (this brief, 2026-05-26 12:30Z)

| Metric | Value |
|---|---|
| universe_size | 28 |
| actionable (`gate_pass=true`) | 9 (32%) |
| silenced_by_gate | 19 (68%) |
| confidence distribution | bimodal: 9 @ 0.80, 19 @ 0.00 |
| symbols with `0.05 < confidence < 0.65` | **0** |
| symbols with `confidence > 0.05` from ≥2 distinct analysts (post-filter) | **0** |

### Target (post-fix)

| Metric | Target |
|---|---|
| symbols with `confidence > 0.05` | **>50% of universe (≥14 of 28)** |
| symbols where `≥2` analysts each emit `confidence ≥ 0.10` | **>50% of universe** |
| confidence distribution | continuous over `[0, 1]`, no bimodal cliff |
| `SILENCE_INSUFFICIENT_VOICES` rate | drops materially (currently masked by abstain-stub-counting) |

### How to measure

Add to the daily brief writer (or a follow-up diagnostic script) a per-symbol `analyst_voice_count` column counting non-abstain views with `confidence ≥ 0.10`. Re-run advisor over the same watchlist and compare the histograms. Lock the assertion in `tests/integration/test_brief_voice_distribution.py`:

```python
def test_brief_has_continuous_confidence_distribution(brief):
    confs = [s["confidence"] for s in brief["actionable"] + brief["silent"]]
    nonzero = [c for c in confs if c > 0.05]
    assert len(nonzero) >= 0.5 * len(confs), \
        "post-fix universe must produce conf > 0.05 on >50% of symbols"
    midband = [c for c in nonzero if 0.05 < c < 0.65]
    assert len(midband) >= 0.2 * len(nonzero), \
        "ensemble should produce midband signals, not only 0 or 0.8"
```

---

## 7. Promotion checklist for autonomous mode

Per ADR-0016, autonomous-mode promotion has the following prerequisites. Status as of 2026-05-26:

- [ ] **ADR-0016 §D2.1 — Confidence dim live.** Currently degenerate: ensemble confidence is 0 or ~0.8 with nothing in between. **Blocked by Bug 3.**
- [ ] **ADR-0016 §D2.2 — Urgency dim (`signed_edge / volatility`) meaningful.** Currently meaningless on rows where ClassicalTA is multiplicative-zero. **Blocked by Bug 3.**
- [ ] **ADR-0016 §D2.3 — Voices dim (`min_analysts_emitted=2`) enforced honestly.** Currently inert because abstain-stubs count as voices. **Blocked by Bug 1 and ADR-0018 §D4 filter (P0-1).**
- [x] **ADR-0016 §D2.4 — Salience dim.** `gates/silence_bias.py` has the `max_recent_rejections` machinery wired.
- [x] **ADR-0016 §D6 — Three independent locks for live.** Config flag / env-creds / `arm-live --confirm` ceremony all implemented; we remain paper-only.
- [x] **ADR-0016 §D7 — PaperReactor reused.** Settlement loop feeds calibrator updates per ADR-0010.
- [x] **ADR-0016 §D9 — Per-tick safety rails.** `max_per_tick_opens=1`, `max_concurrent_positions=5`, `kill_switch_pct=0.10` all configurable.
- [ ] **ADR-0018 §D4 — BMA confidence-filter.** Not implemented in `aggregators/bma.py::aggregate()`. **Tracked in v0.3 review §P0-1.**
- [ ] **ADR-0018 §D1 — Three-voice ensemble actually three-voice.** Currently effectively one (microstructure-only). **Blocked by Bugs 1 + 3.**
- [ ] **Calibrator warm-start path.** ClassicalTA's `CalibratorNotReady → max(0, raw - 0.20)` deadlocks when the system never trades. Needs a non-zero floor or non-trivial prior. **Tracked in `classical-TA-warm-start-fix`.**
- [x] **Kill switch.** `halt_state.json` operator-emergency-stop path proven (entry from 2026-05-13T18:15:12Z still in `signals.jsonl`).

**Until the four unchecked Bug-1/Bug-3 items are green, autonomous mode must remain paper-only and operator-supervised.** This brief is exactly the "rewarded for correct inaction" charter principle failing in the *opposite* direction: the system isn't over-trading on noise — it's *under-emitting structure* and presenting a false-confidence vector (0.8) on the few signals that do leak through.
