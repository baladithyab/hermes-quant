# Plan: PDR-2 · TrendVelocity perception producer (GAP-A / DETECT)

> **Status:** implementation-ready · **Date:** 2026-05-31 · **Wave:** PDR-2 (self-evolving-researcher PERCEPTION layer)
> **Flag:** `HERMES_QUANT_TREND_VELOCITY` (default-OFF) · **Eval gate:** D74.7 directional-precision ≥0.6 hit-rate on velocity-sourced magnitude vs realized forward returns
> **Grounded in:** `docs/adr/ADR-0079-perception-decision-reaction-architecture.md` (§D79.3, §D79.5, Rollout PDR-2), `docs/design/pdr-unified-architecture.md` §3.1, `docs/research/2026-05-31-r-pdr234-seams.md` §1,§2,§4,§5
> **Depends-on:** PDR-1 (SHIPPED — `perception/{frame,adapter,builder}.py`, `frame.trend_velocity` slot empty); B08 (real Reddit/Trends producers) is the LIVE-INFLUENCE gate, NOT a build blocker — see §8.

A fresh agent can build this with no further research. Every seam is cited `file:line` against HEAD.

---

## 0. One-paragraph thesis (what we are building and why)

The Camillo DETECT primitive is *trend velocity* — interest **accelerating** week-over-week far above its own
baseline — and the edge is in the **slope, not the severity** (design §3.1). Today the packet `magnitude` is sourced
from `classify_headline`'s static keyword severity (`synthesize.py:116`, `classify.py:128-129`): a max single-term
lexicon weight on ONE title, with zero acceleration logic anywhere (audit GAP-A). PDR-2 adds a pure perception
**producer** `perception/velocity.py` that turns an interest series (item counts per entity/symbol per period) into a
`{velocity, baseline_z, asof}` score, attaches it to `frame.trend_velocity` in `builder.py`, and — **only when
`HERMES_QUANT_TREND_VELOCITY=1`** — sources `packet.magnitude` from the velocity score instead of severity. With the
flag OFF the producer never runs and `magnitude` stays severity-based → **byte-identical** to today. The mechanism +
its unit eval (D74.7 `run_precision` over the versioned GN-RSS/Camillo corpus) build NOW; only the *live-influence*
flip waits on B08 data volume (§8).

---

## 1. The rails this primitive preserves (restate — non-negotiable, ADR-0079 Rollout "Rails check")

1. **Default-OFF; gate read at CALL time.** `HERMES_QUANT_TREND_VELOCITY` read inside the producer call and inside the
   one `synthesize.py` swap line — never cached at import. Flag-OFF ⇒ `magnitude` stays `round(float(cls.severity),4)`
   ⇒ byte-identical. (Property-tested, §6 T2.)
2. **PERCEPTION-layer evidence ONLY.** Velocity changes only the *magnitude* a packet carries (the quality of the
   evidence). It NEVER touches stance/direction, confidence, the `require_ensemble` guard, the discrete ladder, or the
   gate. `PerceptionFrame` is a **container, not an authority** (frame.py:24).
3. **`asof` honesty / no-lookahead.** The velocity score is computed from *only* observations with timestamp ≤ the
   series asof, and the score itself stamps `asof`. The producing-path no-lookahead gate (Wave S5/M21,
   `tests/test_no_lookahead.py:743-857`) is EXTENDED to the velocity attach point (§6 T4).
4. **Magnitude vs confidence never conflated (D74.3).** `synthesize.py:9` rule holds: confidence still ← propagation
   linkage × haircut (`synthesize.py:106,115`); ONLY magnitude is re-sourced.
5. **External-truth eval (forward returns), never self-graded.** Reuse `catalyst/eval.py:run_precision`
   (`eval.py:76-120`) UNCHANGED against REAL yfinance forward returns. The harness does not grade itself.
6. **Cross-SOURCE convergence (PDR-3) is COMPLEMENTARY to cross-ANALYST `require_ensemble` (PDR-4-adjacent / BMA), not a
   replacement.** PDR-2 builds neither; it only re-sources magnitude. (Stated so a fresh agent does not conflate them;
   PDR-3/PDR-4 are separate plans — §9.)

---

## 2. Architecture: where velocity is produced, attached, and consumed

```
 INTEREST SERIES (counts/period)              perception/velocity.py  (NEW, pure)
   item counts per (entity|symbol) per         compute_trend_velocity(series, asof)
   week, derived from CatalystItem.published_at        │  {velocity, baseline_z, asof, n_periods, peak_period}
   (asof <= series cutoff ONLY)                        ▼
                                              builder.py  Step 5b (NEW, after semantic slice ~line 186)
                                                if HERMES_QUANT_TREND_VELOCITY=1:
                                                  frame.trend_velocity = compute_trend_velocity(...)   (GAP-A slot, frame.py:38)
                                                       │
                                                       ▼
                                              adapter.py:51-52 (UNCHANGED) projects -> ctx.extras["trend_velocity"]
                                                       │   (only when non-None; analysts ignore unknown keys, protocol.py:16)
                                                       ▼
 synthesize.py:69-139  synthesize_packets(..., velocity_by_symbol=None)  (NEW optional kwarg)
   per touched symbol (synthesize.py:100):
     magnitude = velocity_magnitude(velocity_by_symbol[sym])   IF flag ON and entry present   (NEW)
                 else round(float(cls.severity), 4)            (synthesize.py:116, UNCHANGED default)
                                                       │   one SemanticPacket per symbol (asof = item.published_at)
                                                       ▼
                              ── unchanged downstream: BMA peer view, require_ensemble, gate is FINAL ──
```

**The clean split (do not blur):** the producer (`velocity.py`) computes a score and is pure/offline-testable; the
attach (`builder.py`) is flag-gated and stamps it into the frame; the swap (`synthesize.py:116`) is the SINGLE
load-bearing magnitude line. Velocity is *evidence quality*, never authority.

---

## 3. Exact new/modified files (seams cited `file:line` from recon §5)

### NEW · `hermes_quant/perception/velocity.py`  — the pure producer (recon §5 PDR-2: "new `perception/velocity.py`")

The core math. Pure, deterministic, offline-testable, no I/O, no flag read (the flag gates the *caller*, not the math
— mirrors how `regime/extras_builder.py` is pure and the advisor gates it).

```python
"""hermes_quant.perception.velocity — TrendVelocity DETECT primitive (ADR-0079 PDR-2, GAP-A).

Week-over-week ACCELERATION of an interest series vs a trailing baseline. The Camillo
DETECT edge is in the SLOPE, not the severity (design pdr-unified-architecture.md §3.1).
This module is PURE: it scores a pre-built series and stamps asof. It reads NO flag and
does NO I/O — the HERMES_QUANT_TREND_VELOCITY gate lives at the call site (builder.py),
exactly as build_regime_extras is pure and the advisor gates it.

Lookahead-honest by construction: callers MUST pass a series already truncated to
observations with timestamp <= asof; the score stamps that same asof (ADR-0079 D-4).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# Magnitude floor/ceiling mirror the severity scale the packet carries today
# (classify.py weights live in ~0.0..0.06; synthesize rounds to 4dp). Keeping the
# velocity-sourced magnitude inside the SAME band means a flag flip cannot widen the
# discrete ladder or hand BMA an out-of-distribution magnitude.
VELOCITY_MAGNITUDE_FLOOR = 0.0
VELOCITY_MAGNITUDE_CEIL = 0.06          # == max lexicon weight (classify.py "bankruptcy"=0.06)
_BASELINE_MIN_PERIODS = 3               # need >= this many trailing periods or we abstain (return None)


@dataclass(frozen=True)
class VelocityScore:
    velocity: float           # week-over-week acceleration: (last - prev) normalized by baseline mean
    baseline_z: float         # z-score of the latest period vs the trailing baseline
    asof: pd.Timestamp        # the series cutoff = the score's lookahead anchor (UTC)
    n_periods: int            # how many trailing periods fed the baseline (provenance)
    peak_period: pd.Timestamp | None  # period of the series max (PDR-4 "past the velocity peak" reads this)

    def to_mapping(self) -> dict:
        return {
            "velocity": round(float(self.velocity), 6),
            "baseline_z": round(float(self.baseline_z), 6),
            "asof": self.asof.isoformat(),
            "n_periods": int(self.n_periods),
            "peak_period": self.peak_period.isoformat() if self.peak_period is not None else None,
        }


def counts_per_period(
    timestamps: Sequence[datetime | pd.Timestamp],
    *,
    asof: datetime | pd.Timestamp,
    freq: str = "W",
) -> pd.Series:
    """Bucket observation timestamps (CatalystItem.published_at) into a per-period count
    series, truncated to <= asof (NO future buckets). Empty -> empty Series. Pure."""
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    ts = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    ts = ts[ts <= asof_ts]                       # LOOKAHEAD CUT — only past observations
    if ts.empty:
        return pd.Series(dtype="int64")
    return ts.dt.to_period(freq).value_counts().sort_index()


def compute_trend_velocity(
    counts: pd.Series,            # index = period, value = item count (from counts_per_period)
    *,
    asof: datetime | pd.Timestamp,
) -> VelocityScore | None:
    """Score week-over-week acceleration vs a trailing baseline.

    velocity   = (latest_count - prev_count) / max(baseline_mean, 1.0)
    baseline_z = (latest_count - baseline_mean) / max(baseline_std, 1.0)
    baseline   = all periods BEFORE the latest (the trailing window).

    Returns None (abstain -> magnitude falls back to severity) when there are fewer than
    _BASELINE_MIN_PERIODS+1 periods — silence-by-default; never fabricate a slope from noise.
    """
    asof_ts = pd.Timestamp(asof)
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.tz_localize("UTC")
    if counts is None or len(counts) < _BASELINE_MIN_PERIODS + 1:
        return None
    vals = counts.to_numpy(dtype=float)
    latest, prev = vals[-1], vals[-2]
    baseline = vals[:-1]
    baseline_mean = float(baseline.mean())
    baseline_std = float(baseline.std(ddof=0))
    velocity = (latest - prev) / max(baseline_mean, 1.0)
    baseline_z = (latest - baseline_mean) / max(baseline_std, 1.0)
    peak_idx = int(vals.argmax())
    peak_period = pd.Timestamp(counts.index[peak_idx].start_time, tz="UTC")
    return VelocityScore(
        velocity=velocity, baseline_z=baseline_z, asof=asof_ts,
        n_periods=len(baseline), peak_period=peak_period,
    )


def velocity_magnitude(score_mapping: Mapping[str, object] | None) -> float | None:
    """Map a velocity score Mapping (frame.trend_velocity[sym]) to a packet magnitude in
    the SAME band as severity ([0, VELOCITY_MAGNITUDE_CEIL]). Returns None when there is
    no score (caller falls back to severity). A higher baseline_z -> larger magnitude.

    Bounded by construction: a flag flip can NEVER hand BMA a magnitude outside the band
    the discrete ladder was calibrated against (rail #2, design §3.1)."""
    if not score_mapping:
        return None
    z = float(score_mapping.get("baseline_z", 0.0))
    # squash z>=0 into [floor, ceil]; negative/decelerating z -> floor (no demand spike).
    if z <= 0.0:
        return VELOCITY_MAGNITUDE_FLOOR
    frac = z / (z + 2.0)                          # smooth, monotone, in (0,1); z=2 -> 0.5
    return round(VELOCITY_MAGNITUDE_FLOOR + frac * (VELOCITY_MAGNITUDE_CEIL - VELOCITY_MAGNITUDE_FLOOR), 4)


__all__ = ["VelocityScore", "counts_per_period", "compute_trend_velocity", "velocity_magnitude"]
```

> **Design notes for the builder:** the producer scores ONE series. For the frame, `frame.trend_velocity` is a
> `Mapping[str, Any]` keyed by symbol (the slot type, `frame.py:38`): `{symbol: score.to_mapping()}`. A series per
> symbol is built from the `CatalystItem` set's `published_at` timestamps grouped by the symbol(s) the item's entities
> propagate to. In the **unit/eval phase** (no live producers, B08 pending) the series is supplied from the versioned
> fixture (§5); in the **live phase** the series comes from the real Reddit/Trends counts (B08).

### MODIFIED · `hermes_quant/perception/builder.py`  — attach (recon §1 builder.py:191-203; recon §5 "~builder.py:168")

Insert a flag-gated **Step 5b** AFTER the semantic slice (`builder.py:168-186`) and BEFORE the `PerceptionFrame(...)`
construction (`builder.py:191-203`). Copy the flag idiom verbatim from `builder.py:175`.

```python
    # ---- Step 5b: PDR-2 TrendVelocity (GAP-A) — flag-gated, default-OFF ----
    # Mirrors the Step-5 flag idiom (builder.py:175). OFF -> stays None -> adapter
    # writes nothing (adapter.py:51) -> synthesize keeps severity -> byte-identical.
    frame_trend_velocity: Mapping[str, Any] | None = None
    if os.environ.get("HERMES_QUANT_TREND_VELOCITY", "0") == "1":
        try:
            from hermes_quant.perception.velocity import compute_trend_velocity, counts_per_period
            from hermes_quant.perception.velocity_source import interest_timestamps_by_symbol  # see below

            vel_asof = decision_asof or datetime.now(UTC)
            ts_by_symbol = interest_timestamps_by_symbol(symbol, vel_asof, horizon=timeframe)
            scores: dict[str, Any] = {}
            for sym, tss in ts_by_symbol.items():
                counts = counts_per_period(tss, asof=vel_asof, freq="W")
                sc = compute_trend_velocity(counts, asof=vel_asof)
                if sc is not None:
                    scores[sym] = sc.to_mapping()
            if scores:
                frame_trend_velocity = scores
        except Exception as exc:  # noqa: BLE001 — never block frame build on velocity
            logger.debug("build_perception_frame(%s): velocity build failed: %s", symbol, exc)
```

Then change the `PerceptionFrame(...)` call (`builder.py:198`) `trend_velocity=None,` → `trend_velocity=frame_trend_velocity,`.
(`import os` is already present in builder.py — the Step-5 semantic block does `import os` at `builder.py:173`; reuse it.)

> **`velocity_source.py` (NEW, small):** `interest_timestamps_by_symbol(symbol, asof, horizon) -> dict[str, list[datetime]]`
> reads the interest-series source and returns, per touched symbol, the list of observation timestamps ≤ asof.
> **Unit/eval phase:** reads the versioned fixture JSONL (§5) or the existing catalyst packet store
> (`synthesize._DEFAULT_STORE`) item timestamps. **Live phase (B08):** reads real Reddit/Trends counts. Keep it tiny
> and injectable so tests monkeypatch it like `synthesize._DEFAULT_STORE` is patched (`test_no_lookahead.py:788`).
> Silence-by-default: any error → `{}` → no velocity → severity fallback.

### MODIFIED · `hermes_quant/catalyst/synthesize.py`  — the SINGLE magnitude swap (recon §2, §5: "the load-bearing swap is synthesize.py:116")

Add ONE optional kwarg to `synthesize_packets` (signature at `synthesize.py:69-77`) and gate the magnitude source at
`synthesize.py:116`. This is the only behavioral change in the catalyst pipeline.

```python
def synthesize_packets(
    items: list[CatalystItem],
    *,
    horizon: str = "1d",
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
    propagation_log: list[dict] | None = None,
    model: str = "catalyst-sense:v1",
    velocity_by_symbol: dict[str, dict] | None = None,   # NEW: PDR-2 frame.trend_velocity, keyed by symbol
) -> list[SemanticPacket]:
```

At the per-symbol loop (currently `synthesize.py:116` inside the `for sym, res in results.items():` body):

```python
            # PDR-2 (GAP-A): source magnitude from trend VELOCITY when the flag is ON
            # and a score exists for this symbol; else keep the severity default (byte-
            # identical when flag OFF). Magnitude vs confidence never conflated (D74.3).
            magnitude = round(float(cls.severity), 4)
            if (
                velocity_by_symbol is not None
                and os.environ.get("HERMES_QUANT_TREND_VELOCITY", "0") == "1"
            ):
                from hermes_quant.perception.velocity import velocity_magnitude
                vmag = velocity_magnitude(velocity_by_symbol.get(sym))
                if vmag is not None:
                    magnitude = vmag
            # ... then in the packet dict:  "magnitude": magnitude,
```

Add `import os` at the top of `synthesize.py` (currently absent). Record provenance in `metadata`:
`"magnitude_source": "velocity" if vmag is not None else "severity"`, and when velocity-sourced,
`"velocity_score": velocity_by_symbol.get(sym)` so the JSONL store carries the slope used (reproducibility rail).

> **Threading the kwarg:** the production path that feeds packets is the catalyst ingest cron
> (`ops/scripts/quant-catalyst-ingest.py`), which calls `synthesize_packets`. The cron computes
> `velocity_by_symbol` via the same `compute_trend_velocity` over the ingested `CatalystItem` set (group by propagated
> symbol) and passes it in. The advisor/frame path does NOT re-synthesize — packets are pre-built and stored; the
> frame's `trend_velocity` slot carries the score for observability/PDR-4. **The eval (`run_precision`) calls
> `synthesize_packets` directly (`eval.py:93`)** — see §4 for how the eval passes `velocity_by_symbol`.

### MODIFIED · `hermes_quant/catalyst/eval.py`  — let `run_precision` exercise the velocity path (recon §4: "PDR-2 reuses this UNCHANGED" + thread the score)

The harness shape is unchanged (still synthesizes packets, checks stance vs realized forward return). Add ONE optional
kwarg so the eval can feed velocity scores and prove the velocity-sourced magnitude clears ≥0.6:

```python
def run_precision(
    cases: list[EvalCase],
    *,
    min_hit_rate: float = 0.6,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
    velocity_by_symbol: dict[str, dict] | None = None,   # NEW: pass-through to synthesize_packets
) -> PrecisionResult:
    ...
        packets = synthesize_packets(
            [case.item], graph=graph, aliases=aliases, velocity_by_symbol=velocity_by_symbol,
        )
```

> **Important:** `run_precision` checks DIRECTION (stance) vs realized return, not magnitude. Velocity sources
> *magnitude*, not stance. So the D74.7 gate proves the velocity-sourced packets still clear the directional bar (i.e.
> re-sourcing magnitude did not *break* precision) AND lets the analyst phase later weight on the better magnitude. The
> magnitude's predictive lift is measured by the magnitude-vs-forward-return correlation assertion in §6 T1b (an
> additional, velocity-specific assertion layered on the same external truth).

---

## 4. The eval gate (D74.7) as pytest-verifiable acceptance criteria

Reuse `catalyst/eval.py` UNCHANGED in shape (recon §4). The gate: **directional-precision ≥0.6 hit-rate on
velocity-sourced magnitude vs realized forward returns.** Build it against the versioned Camillo/GN-RSS fixture (§5),
not `/tmp` (N13).

```python
# tests/unit/test_perception_velocity_eval.py  (NEW)
import json
from pathlib import Path
import pytest
from hermes_quant.catalyst.eval import EvalCase, eval_gate, run_precision
from hermes_quant.catalyst.ingest import CatalystItem
# fixture loaders mirror ops/scripts/quant-catalyst-socialarb-eval.py (graph/aliases/lexicon)

FIXT = Path(__file__).parent.parent / "fixtures" / "socialarb"

def _load_cases():
    labels = json.loads((FIXT / "camillo_labels.json").read_text())  # versioned, NOT /tmp
    ...

def test_d747_velocity_sourced_magnitude_clears_precision_bar():
    """ADR-0079 Rollout PDR-2 eval gate: with velocity-sourced magnitude, directional
    precision is >= 0.6 hit-rate vs REAL forward returns (D74.7). External truth."""
    cases, vel = _load_cases()                       # vel = {sym: velocity_score_mapping}
    res = run_precision(cases, min_hit_rate=0.6, graph=GRAPH, aliases=ALIASES,
                        velocity_by_symbol=vel)
    assert res.passed, f"D74.7 FAIL: hit_rate={res.hit_rate} scored={res.n_scored} misses={res.misses}"

def test_negative_control_zero_packets_with_velocity_on():
    """Benign headlines still produce ZERO packets with velocity ON (cry-wolf guard)."""
    passed, neg, prec, sign = eval_gate(BENIGN, cases, sign_cases=SIGN, graph=GRAPH, aliases=ALIASES)
    assert neg.passed
```

The fixture's velocity scores are computed by `compute_trend_velocity` over a per-symbol synthetic interest series
stored alongside the labels (§5) so the eval is fully deterministic/offline (no yfinance, no network) — the labels'
realized returns were captured once by the labels script (§5).

---

## 5. Promote the labeled eval set to a versioned fixture (N13 — NOT /tmp)

**Current N13 violation to fix:** `ops/scripts/quant-catalyst-socialarb-labels.py:67` writes
`/tmp/phase0_labels.json` and `ops/scripts/quant-catalyst-socialarb-eval.py:80` reads it. Per N13 (versioned fixture,
not `/tmp`), promote to `tests/fixtures/socialarb/`:

- `tests/fixtures/socialarb/camillo_labels.json` — the 5 Camillo cases (CELH/CROX/DIIBF/TPR/NWL) with REAL yfinance
  forward returns, the exact list from `quant-catalyst-socialarb-labels.py:18-29`, captured ONCE and committed (so the
  unit test is deterministic/offline — no live yfinance in CI).
- `tests/fixtures/socialarb/interest_series.json` — per-symbol weekly interest counts (the input to
  `compute_trend_velocity`) for each Camillo case, hand-derived from the GN-RSS corpus / documented trend timelines so
  the velocity score is reproducible. Each entry: `{ "CELH": {"freq":"W", "counts":[{"period":"2021-W05","n":2}, ...],
  "asof":"2021-03-01T00:00:00Z"} , ... }`.
- `tests/fixtures/socialarb/README.md` — provenance: how the labels were captured (the labels script + date), how the
  series were derived, and the N13 note ("regenerate via `quant-catalyst-socialarb-labels.py` → commit, never /tmp").

**Modify the two scripts** to write/read `tests/fixtures/socialarb/camillo_labels.json` (a repo-relative path resolved
from `__file__`), deleting the `/tmp/phase0_labels.json` coupling. The scripts remain a measurement harness; the
fixture is the committed artifact the unit test consumes.

---

## 6. Test files + the rails as pytest-verifiable acceptance criteria

### NEW · `tests/unit/test_perception_velocity.py`  — the producer math + the magnitude band

- **T1a · velocity/baseline_z math** — a known counts series (e.g. `[1,1,1,8]` weekly) yields the expected
  `velocity` and positive `baseline_z`; a flat series yields `velocity≈0`; `< _BASELINE_MIN_PERIODS+1` periods → `None`
  (abstain). Assert `peak_period` points at the max bucket (PDR-4 reads this).
- **T1b · magnitude band (rail #2)** — `velocity_magnitude` output is ALWAYS in
  `[VELOCITY_MAGNITUDE_FLOOR, VELOCITY_MAGNITUDE_CEIL]` for any z (property test over a z-sweep incl. negatives → floor,
  huge z → < ceil). Proves a flag flip cannot hand BMA an out-of-band magnitude / widen the ladder.
- **T1c · lookahead cut in `counts_per_period`** — a series with timestamps straddling `asof` buckets ONLY the
  ≤-asof observations (no future bucket). External-truth/asof rail at the producer.

### NEW · `tests/unit/test_perception_velocity_eval.py`  — the D74.7 eval gate (§4 above). **THE PROMOTION GATE.**

### EXTEND · `tests/unit/test_catalyst_integration.py` (or a new `test_velocity_flag_byte_identical.py`) — **flag-OFF byte-identical (rail #1)**

```python
def test_velocity_flag_off_is_byte_identical(monkeypatch):
    """HERMES_QUANT_TREND_VELOCITY unset/0 -> magnitude stays severity-based and the
    synthesized packets are BYTE-IDENTICAL to the no-velocity baseline. The single most
    important rail: default path is bit-for-bit today's."""
    monkeypatch.delenv("HERMES_QUANT_TREND_VELOCITY", raising=False)
    items = [_camillo_item()]
    base = synthesize_packets(items, graph=GRAPH, aliases=ALIASES)
    # passing a velocity map but with the flag OFF must change NOTHING:
    withv = synthesize_packets(items, graph=GRAPH, aliases=ALIASES,
                               velocity_by_symbol={"CELH": {"baseline_z": 9.0}})
    assert [p.to_dict(include_hash=True) for p in base] == [p.to_dict(include_hash=True) for p in withv]

def test_velocity_flag_on_changes_only_magnitude(monkeypatch):
    """Flag ON: magnitude moves to velocity-sourced; stance + confidence UNCHANGED
    (D74.3 magnitude/confidence never conflated)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    base = synthesize_packets([_camillo_item()], graph=GRAPH, aliases=ALIASES)
    on = synthesize_packets([_camillo_item()], graph=GRAPH, aliases=ALIASES,
                            velocity_by_symbol={"CELH": {"baseline_z": 9.0}})
    assert base[0].stance == on[0].stance
    assert base[0].confidence == on[0].confidence
    assert on[0].magnitude != base[0].magnitude          # magnitude (and ONLY magnitude) moved
    assert on[0].metadata["magnitude_source"] == "velocity"
```

### EXTEND · `tests/test_no_lookahead.py` — **asof / no-lookahead on the PRODUCING path (rail #3, Wave S5/M21 pattern)**

Mirror `test_build_perception_frame_excludes_future_asof_packet` (`test_no_lookahead.py:777-824`) for velocity:

```python
def test_velocity_score_excludes_future_observations(monkeypatch):
    """A velocity series at asof=T must score from ONLY observations <= T. A future
    observation must NOT inflate the slope (producing-path lookahead leak class, M21)."""
    from hermes_quant.perception.velocity import compute_trend_velocity, counts_per_period
    asof = pd.Timestamp("2026-01-15T00:00:00Z")
    past = [pd.Timestamp(f"2026-01-{d:02d}T00:00:00Z") for d in (1,2,8,9,14)]
    future = [pd.Timestamp("2026-01-20T00:00:00Z")] * 50    # LOUD future spike
    counts = counts_per_period(past + future, asof=asof, freq="W")
    sc = compute_trend_velocity(counts, asof=asof)
    assert sc is None or sc.asof == asof
    # the future spike's bucket (W of 2026-01-20) must be absent:
    assert all(pd.Timestamp(p.start_time, tz="UTC") <= asof for p in counts.index)

def test_build_perception_frame_velocity_honors_decision_asof(monkeypatch, tmp_path):
    """build_perception_frame(..., decision_asof=T) with the flag ON must only feed the
    velocity producer observations <= T (frame.trend_velocity stamps asof <= T)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    # monkeypatch velocity_source.interest_timestamps_by_symbol to return a past+future mix,
    # build the frame at decision=T, assert frame.trend_velocity[sym]["asof"] <= T.
```

### Property-test summary (acceptance bar — all must pass):

| # | Test | Rail it pins |
|---|---|---|
| T1a/b/c | producer math, magnitude band, lookahead cut | evidence-only, no ladder-widen, asof |
| T2 | `test_velocity_flag_off_is_byte_identical` | **default-OFF byte-identical (rail #1)** |
| T3 | `test_velocity_flag_on_changes_only_magnitude` | magnitude/confidence never conflated (D74.3) |
| T4 | `test_velocity_score_excludes_future_observations` + frame variant | **no-lookahead producing path (rail #3, M21)** |
| T5 | `test_d747_velocity_sourced_magnitude_clears_precision_bar` | **D74.7 eval gate ≥0.6 (external truth)** |
| T6 | `test_negative_control_zero_packets_with_velocity_on` | cry-wolf / negative control |

---

## 7. The perception-layer safety frame (restate for the builder — DO NOT skip)

- **Evidence-only.** PDR-2 changes ONLY packet `magnitude`. It never sets stance/direction, never touches confidence,
  never bypasses `require_ensemble`, never adds a sizing surface. The deterministic gate stays the FINAL authority
  (ADR-0079 D79.1). A loud velocity spike on a lone packet still cannot fire — BMA `require_ensemble`
  (`bma.py:498-519`) silences a single-source candidate regardless of magnitude.
- **`PerceptionFrame` is a container, not an authority** (`frame.py:24`). `frame.trend_velocity` is read by the
  velocity-magnitude swap and (later) PDR-4; analysts that don't know the key ignore it (`protocol.py:16`,
  `adapter.py:51` writes it ONLY when non-None).
- **PDR-4 is silence-only with TWO property tests (separate plan).** PDR-2 builds the *peak_period* PDR-4 will read
  ("past the velocity peak", design §3.3). Do NOT add the saturation multiplier here. When PDR-4 lands, its two
  property tests are: (a) post-saturation semantic confidence ≤ pre (never raises); (b) every NON-semantic view is
  bit-identical sat-on-vs-off (never vetoes another analyst, ADR-0079 D79.4). PDR-2 must leave those guarantees
  build-able — i.e. velocity is view-agnostic and touches only the semantic packet's magnitude.
- **Cross-SOURCE convergence (PDR-3) is COMPLEMENTARY to cross-ANALYST agreement (BMA), not a replacement.** A
  social-arb signal must clear BOTH (design §3.2 "two independent ensemble requirements, at two layers"). PDR-2 builds
  neither; do not collapse them.

---

## 8. Default-OFF ships now; live-influence may need B08/B09 — the mechanism + unit eval build NOW

- **Build NOW (this plan):** the producer (`velocity.py`), the flag-gated attach (`builder.py`), the single magnitude
  swap (`synthesize.py`), the eval pass-through (`eval.py`), the versioned fixture (§5), and all of §6's tests. All run
  offline against the committed GN-RSS/Camillo corpus — **no live Reddit/Trends required.**
- **Eval gate to FLIP the flag (default-influence):** D74.7 `run_precision` ≥0.6 on velocity-sourced magnitude
  (T5). This is the UNIT gate the build clears now on n=5.
- **LIVE-INFLUENCE caveat (explicit, ADR-0079 Rollout PDR-2 "Depends-on: B08"):** the *full* live-influence promotion
  needs B08 (real Reddit/Trends producers feeding `velocity_source.interest_timestamps_by_symbol` with real counts) for
  series volume, and a larger labeled set (B09-adjacent) for statistical confidence. The PRIMITIVE + its unit eval do
  NOT wait on B08/B09 — they are built and gated now; only the operator's flip-to-live decision waits on data volume
  (Rollout "Promotion discipline" §: default-OFF construction → eval-gate → operator audit → flip the cron wrapper).
- **Flag flip one-liner (when armed, operator-run, NOT by the agent):**
  `HERMES_QUANT_TREND_VELOCITY=1` in the catalyst-ingest cron env (the producing path) — reversible, one line.

---

## 9. Out of scope (separate plans — do not build here)

- **PDR-3 ConvergenceValidator** (`HERMES_QUANT_CONVERGENCE`, recon §5): cross-SOURCE `require_ensemble` gating packet
  EMISSION at `synthesize.py:100-138`. Separate plan.
- **PDR-4 SaturationScore + `[SATURATE]`** (`HERMES_QUANT_SATURATION`, recon §3): the silence-only confidence
  multiplier at `analysts/semantic.py:130-132`. Separate plan. (PDR-2 only *prepares* `peak_period`.)
- **B08 real producers / B09 larger labeled set:** data-volume waves; PDR-2 consumes their output via the injectable
  `velocity_source` seam but does not build them.

---

## 10. Build order (for the executing agent)

1. `perception/velocity.py` (pure) + `tests/unit/test_perception_velocity.py` (T1a/b/c) — TDD the math first.
2. Promote the fixture (§5): move labels off `/tmp` → `tests/fixtures/socialarb/`, add `interest_series.json` +
   `README.md`, repoint the two `ops/scripts/quant-catalyst-socialarb-*.py` scripts.
3. `synthesize_packets` kwarg + the single magnitude swap (`synthesize.py:116`) + `import os` + provenance metadata.
4. `eval.py` `run_precision` velocity pass-through + `tests/unit/test_perception_velocity_eval.py` (T5/T6) — clear ≥0.6.
5. `builder.py` Step 5b flag-gated attach + `velocity_source.py` (injectable) + `trend_velocity=frame_trend_velocity`.
6. Flag-OFF byte-identical (T2) + flag-ON magnitude-only (T3) + no-lookahead producing-path (T4).
7. Full suite green; lint clean. Verify byte-identity: `HERMES_QUANT_TREND_VELOCITY= python -m pytest tests/` matches
   baseline on the catalyst/perception fixtures.

**Acceptance = all of §6's T1–T6 green, flag-OFF byte-identical proven, no-lookahead gate extended and green, fixture
promoted off /tmp.**
