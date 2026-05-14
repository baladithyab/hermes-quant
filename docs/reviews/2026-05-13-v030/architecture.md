# v0.3 architecture review

**Reviewer**: subagent (architecture + contract)
**Scope**: ADR-0017 / 0018 / 0019 + impl + charter cross-cuts
**Date**: 2026-05-13

## Verdict: MERGE_WITH_FOLLOWUPS

The three v0.3 ADRs are coherent and the impl mostly tracks them. Three
P0 contract violations need fixing in a v0.3.1 patch (or v0.4 first
sprint) before paper trading begins; the rest is amendment-grade drift
and polish. **Nothing in v0.3 should be exposed to live capital until
P0-1 and P0-2 below are resolved** — both directly weaken the
silence-bias gate the charter codifies as the autonomous-mode safety net.

---

## P0 — ADR-vs-impl drift / charter violations

### P0-1. ADR-0018 §D4 BMA confidence-filter is **NOT IMPLEMENTED**

ADR-0018 §D4 explicitly footnotes:

> *"Mitigation: BMA aggregator's `aggregate()` filters out views with
> `confidence < 0.10` BEFORE constructing the aggregated signal —
> abstainers are pruned upstream of the silence-bias gate."*

`aggregators/bma.py::aggregate()` (lines 120–208) has **no such filter**.
Every view, including KronosAnalyst's `confidence=0.0` abstain path
(`kronos.py::_abstain`), is fed into `signed_dir_terms`. The dir term
for an abstainer is `0 * w * 0 = 0` so it doesn't change the *direction*
math — but:

- It still appears in `views` and so still appears in
  `advisor_result["analyst_views"]`.
- `gates/silence_bias.py:170` then computes `n_emitted = len(views)`,
  which **counts the abstainer**. With `min_analysts_emitted=2`,
  Kronos abstaining + ClassicalTA emitting + MicrostructureLite
  emitting = 3 voices → gate green-lights a single-real-voice signal.

This is the **exact failure mode ADR-0018 D4 calls out and claims to
mitigate**. Right now the gate is silently weaker than v0.2 was when
KronosAnalyst is installed-but-failing-to-load (the common case on
boxes without HF connectivity / without `[kronos]` extra).

**Fix** (one of):

1. **Implement the filter in BMA** (canonical per ADR-0018):
   ```python
   # at top of aggregate()
   views = [v for v in views if v.confidence >= 0.10]
   if not views: return self._flat_signal(context)
   ```
   And mirror in advisor's `analyst_views` materialization so the gate
   sees the post-filter list.
2. **OR** filter in `silence_bias_gate` directly:
   ```python
   active = [v for v in views if (v.get("confidence") or 0) >= 0.10]
   n_emitted = len(active)
   ```
   Cheaper but bypasses ADR-0018's stated contract location.

Either way, **add a regression test**: KronosAnalyst with
`_predictor_factory=lambda: raise OSError` should make a 3-analyst
autonomous tick produce `SILENCE_INSUFFICIENT_VOICES`, not FIRE.

### P0-2. Advisor's hard-coded analyst list **does not include Kronos**

`advisor.py:507-514` falls back to `[ClassicalTAAnalyst(),
MicrostructureLite()]` when `analysts is None`. The autonomous tick
(`autonomous.py:310-315`) calls `advisor_recommend(...)` with no
`analysts=` injection. Result: **KronosAnalyst is never instantiated
in the live tick path**, despite shipping in `pyproject.toml` entry
points and the CHANGELOG announcing "ALL THREE shipped".

The MVP recipe charter clause "three-analyst committee on liquid
crypto" is not actually three voices in v0.3 — it's still two, the
same as v0.2.

**Fix**: in `advisor.py:507-514`, also try-import KronosAnalyst:

```python
analysts = [ClassicalTAAnalyst()]
try:
    from hermes_quant.analysts.microstructure import MicrostructureLite
    analysts.append(MicrostructureLite())
except ImportError: pass
try:
    from hermes_quant.analysts.kronos import KronosAnalyst
    analysts.append(KronosAnalyst())   # lazy-loads weights on first analyze()
except ImportError: pass
```

KronosAnalyst's lazy-load pattern (D4) is designed for exactly this
— construction is cheap, abstain-on-failure is clean. The cost of
including it is zero on boxes without kronos installed.

Better still: drive analyst discovery from the
`hermes_quant.analysts` entry-point group (already declared in
pyproject) instead of hard-coding. Smaller change is fine for v0.3.1.

### P0-3. `pyproject.toml` advertises `KairosAnalyst` that **does not exist**

```toml
[project.entry-points."hermes_quant.analysts"]
kairos_btc = "hermes_quant.analysts.kronos:KairosAnalyst"
```

`hermes_quant/analysts/kronos.py` has no `KairosAnalyst` class
(only `KronosAnalyst` and `_DistributionalKronosPredictor`). Any
discovery code that walks the entry-point group will `ImportError` /
`AttributeError` on this row.

The AGENTS.md repo layout comment also says `kronos.py` ships *"both
KronosAnalyst and KairosAnalyst"* — both lying.

**Fix** (one of):
1. Remove the `kairos_btc` entry-point (cleanest for v0.3 scope).
2. Add a `KairosAnalyst` subclass with the crypto-1m-h30 calibration
   the research note alludes to (charter-aligned but real work).

Recommendation: option 1 + amend ADR-0018 §"Provenance" to defer Kairos
to v0.4.

### P0-4. ADR-0017 §D6 cassettes **not committed**

ADR-0017 §D6 promises:

> *"Integration: pytest-recording cassettes (VCR-style) committed to
> `tests/fixtures/ccxt_cassettes/`"*

Search of the repo: zero files matching `*cassette*`, no
`tests/fixtures/ccxt_cassettes/` directory, no `pytest-recording` in
`[dev]` extras. The unit tests rely on `_exchange_factory` injection
(good — D6 row 1) but the integration row of the matrix is unshipped.

**Fix**: either
1. Add `pytest-recording>=0.13` to `[dev]`, generate one canonical
   BTC/USDT 1h cassette (mainnet GET, redact API key), commit it,
   write one integration test exercising the full as_of-filter path.
2. OR amend ADR-0017 §D6 to defer cassettes to v0.4 + leave the
   manual `@pytest.mark.live` smoke as the only network-touching test.

The ADR currently overpromises; either close the gap or downgrade the
claim. Most useful as v0.3.1 patch.

---

## P1 — cross-cut consistency / schema

### P1-1. Reproducibility: KronosAnalyst is **not deterministically replayable**

Charter spine + AGENTS.md §"Reproducibility": *"Every signal is
replayable from disk. Backtest = run the daemon against historical
bars, capture the signal log, replay through freqtrade's backtester."*

`_DistributionalKronosPredictor.predict_distributional()` calls
`self._base.predict(df=..., sample_count=1)` in a Python loop
(`kronos.py:368-374`). Kronos's stochastic sampling uses upstream
PyTorch RNG with **no seed plumbed**:

- `KronosConfig` exposes `model`, `device`, `pred_len`,
  `sample_count`, but no `seed` field.
- The wrapper does not call `torch.manual_seed` / `np.random.seed`
  before the loop.
- Two replays of the same `(bars, asof)` will produce different
  `direction/magnitude/confidence` because the sample paths differ.

This breaks the "every signal replayable from disk" invariant exactly
once a Kronos signal is in the chain. v0.2-only signals (TA +
microstructure) replay fine; v0.3 Kronos signals don't.

**Fix**:
1. Add `seed: int | None = 42` to `KronosConfig`.
2. In `_predict_paths()`, call `torch.manual_seed(seed); np.random.seed(seed)`
   (and `torch.cuda.manual_seed_all` if `device=="cuda"`) BEFORE the
   sample-path loop.
3. Surface the seed in `AnalystView.metadata["seed"]` so the signal
   log records what was used.
4. Add a regression test: same bars + same seed = identical
   `(direction, magnitude, confidence)` across two runs.

This is upstream-amendment-grade — ADR-0018 §D2 should explicitly
mention seed plumbing as a money-software invariant. Currently it
silently violates the charter.

### P1-2. `pyproject.toml [kronos]` extras drift from ADR-0018 §D7 + research note

ADR-0018 D7:
```
kronos = ["torch>=2.0", "huggingface_hub>=0.20"]
```

ADR-0018 context §point 5: *"Dependencies: `torch`, `numpy`, `pandas`,
`huggingface_hub`, `tqdm`. **NO** `transformers`, **NO** `einops`."*

`pyproject.toml`:
```toml
kronos = [
    "torch>=2.0",
    "transformers>=4.35",      # ← ADR says NO
    "huggingface_hub>=0.20",
    "einops>=0.7",             # ← ADR says NO
    "safetensors>=0.4",        # ← undocumented addition
]
```

Either the ADR is wrong (research note was incomplete) or the
pyproject is wrong (over-installs 50-100MB of transitive deps the
upstream Kronos package doesn't need). Resolve before v0.3.1.

Recommendation: confirm against upstream `NeoQuasar/Kronos`
`requirements.txt`. If transformers/einops are required, **amend
ADR-0018 §D7** to admit it. If not, trim pyproject. Either way, the
ADR and the package metadata should agree.

### P1-3. `[all]` extras coherent ✓ (with caveats)

`[all] = ["hermes-quant[yfinance,ccxt,alpaca,kronos,stacking,backtest,mlflow]"]`
correctly references every other extra. `pip install
'hermes-quant[all]'` does pull yfinance + ccxt + kronos.

Caveats noted but not blocking:
- `transformers` is auto-pulled via `[kronos]` (P1-2 above);
  if you trim the kronos extra, recheck `[all]`.
- No `[dev]` reference in `[all]` is correct (test-only deps shouldn't
  be in user installs).

### P1-4. KronosAnalyst metadata vs 1024-char protocol cap (theoretical)

`protocol.py:94-95` says metadata is *"JSON-serialized + capped at
1024 chars when written to signal bus"*. KronosAnalyst's metadata is:

```python
{"model": "base", "device": "cpu", "n_paths": 30,
 "pred_len": 12, "raw_confidence_clipped": False}
```

JSON-serialized ≈ 95 chars. **Way under cap. Safe.** Rationale string:

```python
f"Kronos-{self.config.model}: median return {magnitude:.4f} "
f"({direction:+d}); {n}/{N} paths agreed"
```

Worst case ~80 chars. **Way under 256 cap. Safe.**

No fix needed; flagged for completeness because the cap is mentioned
nowhere in `kronos.py` — if a future maintainer adds a path-by-path
breakdown to metadata, the cap is silent and they won't notice the
truncation. Add a one-line comment at the metadata construction site
referencing protocol.py's cap.

### P1-5. DSR formula matches Bailey & López de Prado eq.4 ✓

`evaluation/dsr.py:90-94`:
```python
variance_term = (
    1.0
    - skew * observed_sharpe
    + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2
)
```

Matches Bailey & LdP 2014 eq.4: `σ²(SR) = (1/(n-1)) × (1 - γ₃·SR +
((γ₄-1)/4)·SR²)` where γ₃ is skew, γ₄ is **non-excess** kurtosis.
The docstring even calls out "this is NOT excess kurtosis; normal
distribution has kurtosis=3" (`dsr.py:43-45`). **Faithful
implementation, not a paraphrase.** No fix needed.

Minor polish: the eq.7 (`expected max under null`) approximation uses
the Euler-Mascheroni hybrid formula (`dsr.py:74-79`), which is the
standard Bailey-LdP form — also correct.

### P1-6. Lessons / silence-bias loop intact for Kronos ✓

`autonomous.py:308-340` calls `advisor_recommend(include_lessons=True)`,
extracts `advisor_result["lessons"]`, passes to `silence_bias_gate(...,
journal_lessons=lessons)`. KronosAnalyst doesn't break this — it
emits an `AnalystView` like everyone else, the advisor packages views
+ lessons + signal, the gate consumes lessons via `_count_recent_rejections`.

**The path is intact.** The only break is P0-1 (abstainer pollution
of `n_emitted`); the lesson channel itself is fine.

### P1-7. Charter clause check: "no learned components in the gate" ✓

`silence_bias.py` is pure-function thresholds + a recent-rejections
counter. No model loading, no calibrators consumed in the gate path.
Charter-clean.

### P1-8. Charter clause check: "AAAI 2026 acceptance ≠ alpha" ✓

ADR-0018 §D3 `[0.30, 0.85]` clip is implemented at
`kronos.py:325-330` with both ends. ColdStart shrinkage applies on
top via `self.calibrator.calibrate(raw_confidence)` at `kronos.py:146`.
Both layers active. Charter-respecting.

---

## P2 — polish

### P2-1. `evaluation/cv.py` `Iterator` import warning

`cv.py:14` uses `from typing import Iterator`. Python 3.11+ wants
`from collections.abc import Iterator` (typing.Iterator is deprecated
under PEP 585). One-line fix; no behavior change.

### P2-2. `ccxt_provider.py` double-localize `as_of` (lines 203-204)

```python
as_of = pd.Timestamp(as_of).tz_convert("UTC") if as_of.tzinfo else \
        pd.Timestamp(as_of).tz_localize("UTC")
```

This block runs AFTER `as_of` was already localized at lines 148-153.
Harmless but redundant. Drop the second block (the `bar_close_time
<= as_of` comparison works on the already-UTC `as_of`).

### P2-3. `ccxt_provider.py` symbol-validation order

Asset class check happens before symbol-format check
(`ccxt_provider.py:130-143`). If an operator passes `asset_class="equity"`
+ `symbol="BTC/USDT"`, they get the `asset_class!='crypto'` error
first, which is fine but slightly less actionable than the
"symbol must be unified format" error. Cosmetic; not worth a patch
on its own.

### P2-4. ADR-0018 D8 says "Kronos NEVER trains" — `update_calibrator` is no-op stub

`kronos.py:172-179`: `update_calibrator()` is a "no-op stub honoring
the hook contract". Comment is honest, but the calibrator IS
referenced at `kronos.py:146` (`self.calibrator.calibrate(raw_confidence)`)
— meaning the calibrator path IS active for shrinkage but never
fitted. Cold-start shrinkage applies forever. ADR-0018 §"Negative" §4
calls this out explicitly, so this is documented expected behavior
for v0.3, **not a bug** — but the calibrator-from-fills wiring is
the V03-5 P0 deferred to v0.4 per CHANGELOG. Track in v0.4 backlog.

### P2-5. `evaluation.lookahead.shuffle_timestamps_test` p-value semantics

The docstring (`lookahead.py:90-92`) says *"Pass if p_value > alpha
(the analyst's signal IS statistically distinguishable from
shuffled)"* but the p-value is computed as
`(n_at_or_above + 1) / (n_shuffles + 1)` — fraction of shuffles ≥
real. So `p_value > 0.05` means "many shuffles equaled or beat real",
which is the **failure** case (analyst couldn't distinguish from
noise → potentially lookahead OR no signal).

The "INVERSION CHECK" comment (`lookahead.py:94-99`) tries to
explain this and gets the logic right in spirit, but the
`@property passed: return self.p_value > self.alpha` is **inverted
relative to how a non-financial reader would expect**. Add a worked
example in the docstring. ADR-0019 §D3 doesn't actually pin which
direction the p-value goes; clarify in the ADR amendment.

(This is P2 because v0.1.2's `tests/test_no_lookahead.py` already
ships and passes — the convention is locked even if confusing.)

---

## Notes

- **Test count claim**: CHANGELOG says "+68 tests, 494 passed". Not
  verified in this review (no test execution); just reading the
  source. If the tests don't actually catch P0-1 (BMA filter
  missing) and P0-2 (advisor doesn't load Kronos), then the test
  suite has the same blind spots. Suggest one new e2e test:
  `test_autonomous_tick_kronos_abstain_silences_gate.py`.

- **ADR cross-references hygiene**: ADR-0018 §D4 cites *"ADR-0016 §D2
  dim 1: `min_confidence=0.65`"* — verified, correct citation.
  ADR-0017 §D3 cites *"ADR-0009 §P0-A"* — not opened in this review,
  trusting the cross-link.

- **Charter spine remains uncompromised at the design level**: the
  ADRs all cite + acknowledge the charter explicitly. The drift is
  in implementation, not architecture. This is the easier failure
  mode to recover from — fix the impl, the design stays.

- **Recommended v0.3.1 cherry-pick set**:
  1. P0-1 BMA filter (or gate filter)
  2. P0-2 advisor includes Kronos
  3. P0-3 remove KairosAnalyst entry-point
  4. P1-1 seed plumbing (charter invariant)
  5. P1-2 reconcile `[kronos]` extras
  6. P0-4 cassettes OR ADR amendment
  Everything else can ride v0.4.
