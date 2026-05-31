# PDR-4 · SaturationScore + `[SATURATE]` — implementation-ready plan

> **Status:** plan / ready-to-build · **Date:** 2026-05-31 · **Wave:** self-evolve / PDR perception
> **Closes:** ADR-0079 GAP-C (EXIT-on-information-parity — the Camillo exit)
> **Flag:** `HERMES_QUANT_SATURATION` (default-OFF; flag-OFF → `m=1.0`, byte-identical)
> **Eval gate:** TWO property tests (post≤pre; non-semantic views bit-identical sat-on-vs-off)
> **+** a backtest showing the decay improves social-arb Sharpe on a labeled exit set.
> **Depends:** PDR-1 (`PerceptionFrame` — SHIPPED, commit 8c8cb1d). **Soft-depends:** PDR-2
> (`TrendVelocity` fills `frame.trend_velocity` with the velocity peak). PDR-4's mechanism +
> unit eval build **now**; with PDR-2 absent it falls back to packet-metadata confirm-date / age.
>
> **Ground truth (read before building):** `docs/adr/ADR-0079-perception-decision-reaction-architecture.md`
> (§D79.1 authority invariant, §D79.4 the load-bearing view-level-not-aggregate correction),
> `docs/design/pdr-unified-architecture.md` §3.3, the seam recon
> `docs/research/2026-05-31-r-pdr234-seams.md` §3 + §5. Sibling plans:
> `docs/plans/selfevolve-PDR-2-trend-velocity.md` (the `trend_velocity` producer PDR-4 soft-reads).
>
> A fresh agent can build this with no further research. Every seam is cited `file:line` against HEAD.

---

## 0. What this primitive is (one paragraph)

`SaturationScore` is the Camillo **EXIT-on-parity** signal: the social-arbitrage edge is
*time-decaying information asymmetry*, so a trend that has run past its velocity peak — or whose
earnings / credit-card confirm date has passed — is no longer an edge. PDR-4 estimates that
saturation as a **confidence multiplier `m ∈ (0, 1]`** and applies it to the
`HermesSemanticAnalyst`'s **own** `AnalystView.confidence` **BEFORE BMA fuses the views**. `m=1.0`
early in the trend (no decay); `m → 0` as it saturates (the social peer goes quiet and, under
`require_ensemble`, simply stops corroborating). It is **silence-only** by construction and
**view-local** by construction. It is *not* a new sizing surface; the discrete ladder is untouched;
the deterministic gate is still the final authority — it just receives a quieter peer.

---

## 1. The load-bearing safety frame (read first — this is WHY, not just WHAT)

These are the invariants the build must preserve. Each maps to a test in §6.

1. **Perception-layer evidence only / `PerceptionFrame` is a CONTAINER, never an authority.**
   The saturation score rides in `frame.saturation` (the EMPTY slot at `perception/frame.py:40`)
   and projects to `ctx.extras["saturation"]` (`perception/adapter.py:55-56`). Analysts ignore
   unknown extras keys (`protocol.py:16`). The frame carries evidence; BMA still fuses peers; the
   gate is still final.

2. **Silence-only (D79.4): `m ∈ (0, 1]`, so post ≤ pre for EVERY input.** A saturation estimate
   can only *shrink* the semantic view's confidence, never raise it. This is the *same* authority
   boundary as catalyst-as-evidence-never-authority and ADR-0077 admissibility monotonicity
   (target magnitude non-increasing). Pinned by **Property Test A** (§6.1).

3. **View-LOCAL, not aggregate-level (D79.4 — the Codex Facet-5 P1 correction, the single most
   load-bearing design point).** The multiplier applies to the `HermesSemanticAnalyst`'s OWN
   `AnalystView.confidence` *before* it enters `BMAAggregator.aggregate(views, ctx)`
   (`advisor.py:1010`) — **NOT** to the post-BMA `AggregatedSignal`. Reason: `SaturationScore` is
   derived from *social/catalyst* evidence; if applied to the aggregate, a stale *social* trend
   could shrink/flatten a TA+Kronos+fundamentals consensus that has nothing to do with that trend —
   perception silently vetoing unrelated analysts. The `post ≤ pre` property alone does **not**
   rule this out (non-amplification ≠ no-cross-view-veto). Pinned by **Property Test B** (§6.2):
   for every NON-semantic view, the contribution is bit-identical with saturation on vs off.

4. **Cross-SOURCE convergence (PDR-3) is COMPLEMENTARY to cross-ANALYST `require_ensemble`
   (BMA), not a replacement.** PDR-4 does not touch either ensemble guard. A saturated social
   signal becomes a quieter peer and, under BMA's `n_distinct_analysts <= 1 → silenced_single_source`
   (`bma.py:498-519`), simply stops corroborating — the gate sees one fewer effective voice, never
   a forced flatten of unrelated analysts.

5. **Default-OFF, flag read at CALL time, flag-OFF byte-identical.** `HERMES_QUANT_SATURATION`
   default `"0"`. With the flag OFF the multiplier is `m=1.0` and the semantic view is
   bit-identical to today. Pinned by the **flag-OFF-byte-identical test** (§6.3).

6. **`asof` honesty (D-4).** The saturation estimate stamps `asof` and reads only past
   observations (the packet's own `asof`, the velocity-peak week `<= asof`, the confirm-date `<=
   asof`). No future data may set `m`. Pinned by the **no-lookahead test** (§6.4) and the
   no-lookahead gate, which already extends to the producing path (Wave S5).

7. **Discrete ladder untouched (D-6).** Saturation moves conviction *down the existing ladder*
   (e.g. a `0.20` candidate decays toward `0.10` → `0` = silence) only by feeding the gate weaker
   evidence. No new sizing surface, no widened ladder.

8. **Live-influence gate may need data volume (B08/B09); the MECHANISM + unit eval build NOW.**
   The full social-arb-Sharpe backtest (§6.5) wants a labeled *exit* set; a richer set is a B09
   follow-up. The primitive ships default-OFF behind its flag with the two property tests + the
   flag-OFF + no-lookahead tests passing immediately; the operator flips it only after the
   backtest clears on the versioned fixture and a side-by-side audit.

---

## 2. Exact seams (file:line against HEAD)

| # | Seam | File:line | Role in PDR-4 |
|---|---|---|---|
| S1 | `frame.saturation` EMPTY slot | `hermes_quant/perception/frame.py:40` | typed home for the score `Mapping[str,Any] \| None` — **no shape change** |
| S2 | adapter projects slot → extras | `hermes_quant/perception/adapter.py:55-56` | `if frame.saturation is not None: extras["saturation"] = ...` — **already present**, delivers the score to the analyst |
| S3 | builder hardcodes `saturation=None` | `hermes_quant/perception/builder.py:200` | **the PRODUCE point** — compute the score here and pass it instead of `None` |
| S4 | builder semantic slice (packets loaded) | `hermes_quant/perception/builder.py:172-185` | the score is computed from these packets + `frame.trend_velocity`; produce **after** this slice |
| S5 | regime multiplier on semantic view | `hermes_quant/analysts/semantic.py:124-130` | the **precedent** — an existing silence-only pre-view confidence adjustment on this same view |
| S6 | `AnalystView` construction | `hermes_quant/analysts/semantic.py:132-141` | **the APPLY point** — multiply `confidence` immediately after line 130, before line 132 |
| S7 | flag idiom precedent | `hermes_quant/catalyst/wiring.py:40`, `regime_aware_confidence.py:25-26` | copy the `os.environ.get("HERMES_QUANT_*", "0") == "1"` read-at-call-time idiom |
| S8 | BMA fuse (the boundary PDR-4 must NOT cross) | `hermes_quant/advisor.py:1010` (`aggregator.aggregate(views, ctx)`) | proves view-level: the multiplier runs in `semantic.py` BEFORE this call |
| S9 | velocity peak source (soft-dep PDR-2) | `frame.trend_velocity` `perception/frame.py:38` → `ctx.extras["trend_velocity"]` `adapter.py:51-52` | PDR-2 fills `{score, baseline_z, peak_asof, asof}`; PDR-4 reads `peak_asof` when present |
| S10 | packet metadata (fallback confirm-date / age) | `synthesize.py:127-136` `metadata` dict; `SemanticPacket.asof` `semantic.py:36` | when PDR-2 absent, derive age from `packet.asof`; optional `metadata["confirm_date"]` if present |

> **CRITICAL DATA-SHAPE FACT (verified against HEAD 2026-05-31 — do NOT skip):**
> `frame.semantic_packets` holds packet **DICTS, not `SemanticPacket` objects**. `load_packets_for`
> returns `list[dict]` (`synthesize.py:166`, `:198`) and the builder does
> `semantic_packets = tuple(packets)` (`builder.py:182`). So the PRODUCE code (§3.2) MUST read
> packet fields with dict `.get(...)`, **never** `getattr(pkt, ...)` — `getattr` on a dict returns
> the default every time, which would silently make the basis always `"no_basis"` and saturation a
> permanent no-op even with the flag ON. (PDR-3's builder stamp already uses `.get()` correctly;
> this is a PDR-4-specific footgun.)

**THE APPLY POINT, verbatim (`semantic.py:124-132`):**
```python
        # ADR-0063: regime-aware confidence multiplier (gated by env flag)
        try:
            from hermes_quant.regime.regime_aware_confidence import apply_regime_multiplier
            _regime = ctx.extras.get("regime") if hasattr(ctx, "extras") else None
            confidence = apply_regime_multiplier(float(confidence), _regime, "semantic")
        except Exception:  # noqa: BLE001
            pass
                              # <-- PDR-4 [SATURATE] multiplier inserts HERE (after regime, before view)
        view = AnalystView(
            analyst=self.name,
            ...
            confidence=float(confidence),
```

---

## 3. New + modified files

### 3.1 NEW — `hermes_quant/perception/saturation.py` (the pure primitive)

A pure, offline-testable function over `(packet, trend_velocity, asof)`. No I/O, no env read (the
flag-gate lives at the *call sites*, S3 + S6; the math stays flag-free so the producer is
unit-testable without env juggling). Stamps `asof`.

```python
"""hermes_quant.perception.saturation — the SaturationScore primitive (ADR-0079 PDR-4 / GAP-C).

The Camillo EXIT-on-information-parity, expressed as a confidence DECAY multiplier
m in (0, 1] applied to the HermesSemanticAnalyst's OWN view (semantic.py:130-132),
BEFORE BMA. Silence-only (post <= pre) and view-local by construction (D79.4).

This module is PURE: no I/O, no env reads. The HERMES_QUANT_SATURATION flag-gate
lives at the two call sites (builder.py produce, semantic.py apply). asof-honest:
reads only past observations (packet.asof, the velocity peak week <= asof, the
confirm-date <= asof). Empty/unknown inputs -> m=1.0 (no decay: do NOT silence a
position you cannot prove is stale).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

# Decay shape constants (tunable; the backtest in section 6.5 calibrates these).
_HALF_LIFE_DAYS_DEFAULT = 14.0   # post-peak edge half-life (Camillo: weeks, not months)
_FLOOR = 0.05                    # m never reaches exactly 0 from age alone; a hard confirm-date can


def compute_saturation(
    *,
    packet_asof: Any,                       # SemanticPacket.asof (ISO str or Timestamp)
    asof: pd.Timestamp,                     # decision/bar asof, UTC -- the lookahead anchor
    trend_velocity: Mapping[str, Any] | None = None,   # frame.trend_velocity (PDR-2), may be None
    confirm_date: Any | None = None,        # earnings/credit-card confirm date (metadata), may be None
    half_life_days: float = _HALF_LIFE_DAYS_DEFAULT,
) -> dict[str, Any]:
    """Return {"score": s in [0,1], "decay_multiplier": m in (0,1], "asof": iso, "basis": str}.

    score = saturation in [0,1]; m = _FLOOR + (1-_FLOOR)*decay, clamped to (0, 1].
    BASIS precedence (asof-honest, most-confident first):
      1. confirm_date passed (<= asof)            -> fully saturated, m -> _FLOOR  (hard exit)
      2. velocity peak week passed (PDR-2)        -> age-from-peak exponential decay
      3. packet age fallback (no PDR-2)           -> age-from-publication exponential decay
      4. nothing usable                           -> m = 1.0 (NO decay)
    Never raises; on any parse failure returns m=1.0 (silence-only safety: an
    un-estimable saturation must not silence a live signal).
    """
    asof_ts = _as_utc(asof)
    out_asof = asof_ts.isoformat()

    # ---- basis 1: hard confirm-date ----
    cd = _as_utc_or_none(confirm_date)
    if cd is not None and cd <= asof_ts:
        m = _FLOOR
        return {"score": round(1.0 - m, 6), "decay_multiplier": round(m, 6),
                "asof": out_asof, "basis": "confirm_date_passed"}

    # ---- basis 2: velocity peak (PDR-2) ----
    peak = None
    if trend_velocity is not None:
        peak = _as_utc_or_none(trend_velocity.get("peak_asof"))
    anchor, basis = (peak, "velocity_peak") if (peak is not None and peak <= asof_ts) else (None, None)

    # ---- basis 3: packet-age fallback ----
    if anchor is None:
        pub = _as_utc_or_none(packet_asof)
        if pub is not None and pub <= asof_ts:
            anchor, basis = pub, "packet_age"

    if anchor is None:
        return {"score": 0.0, "decay_multiplier": 1.0, "asof": out_asof, "basis": "no_basis"}

    age_days = max(0.0, (asof_ts - anchor).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / max(1e-9, half_life_days))   # 1.0 at age 0, ->0 with age
    m = _FLOOR + (1.0 - _FLOOR) * decay
    m = max(_FLOOR, min(1.0, m))
    return {"score": round(1.0 - m, 6), "decay_multiplier": round(m, 6),
            "asof": out_asof, "basis": basis}


def apply_saturation(confidence: float, saturation: Mapping[str, Any] | None) -> float:
    """Apply the decay multiplier to a confidence. SILENCE-ONLY: returns
    confidence * m with m in (0,1] (or confidence unchanged when saturation is
    None / malformed). Never raises; never raises confidence."""
    if saturation is None:
        return confidence
    try:
        m = float(saturation.get("decay_multiplier", 1.0))
    except Exception:  # noqa: BLE001
        return confidence
    if not (0.0 < m <= 1.0):   # defensive: any out-of-contract m is treated as a no-op
        return confidence
    return float(confidence) * m


def _as_utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

def _as_utc_or_none(ts: Any) -> pd.Timestamp | None:
    if ts is None:
        return None
    try:
        return _as_utc(ts)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["compute_saturation", "apply_saturation"]
```

**Why `apply_saturation` clamps `m ∈ (0,1]` rather than trusting it:** defense-in-depth for the
silence-only invariant. Even a buggy producer cannot make the multiplier amplify — Property Test A
holds even on adversarial `saturation` dicts.

### 3.2 MODIFIED — `hermes_quant/perception/builder.py` (PRODUCE, S3/S4)

Replace the hardcoded `saturation=None` (line 200) with a flag-gated produce, computed from the
loaded packets (S4, lines 172-185) + `frame.trend_velocity` (which PDR-2 fills; `None` today). The
frame currently builds `trend_velocity=None` (line 198), so the velocity basis is simply inactive
until PDR-2 ships — the packet-age fallback covers the interim.

Insert after the semantic slice (after line 185), before the `return PerceptionFrame(...)`:

```python
    # ---- Step 6b: PDR-4 SaturationScore (ADR-0079 GAP-C) -- default-OFF ----
    # Flag read at CALL time (mirrors wiring.py:40). OFF -> saturation stays None
    # -> adapter writes NOTHING -> semantic view byte-identical (flag-OFF safety).
    frame_saturation: Mapping[str, Any] | None = None
    if os.environ.get("HERMES_QUANT_SATURATION", "0") == "1" and semantic_packets:
        try:
            from hermes_quant.perception.saturation import compute_saturation
            # Score the freshest packet (the one the analyst selects: semantic.py:194).
            # CRITICAL: frame.semantic_packets holds packet DICTS, not objects —
            # load_packets_for returns list[dict] (synthesize.py:166,198) and the
            # builder does `tuple(packets)` (builder.py:182). Use dict .get(), NOT
            # getattr(): getattr on a dict returns the default every time, which would
            # make saturation a SILENT no-op even with the flag ON (the basis would
            # always be "no_basis"). Verified against HEAD 2026-05-31.
            _pkt = max(semantic_packets, key=lambda p: p.get("asof", ""))
            _md = _pkt.get("metadata") or {}
            _cd = _md.get("confirm_date") if isinstance(_md, Mapping) else None
            frame_saturation = compute_saturation(
                packet_asof=_pkt.get("asof"),
                asof=last_bar_ts_utc,            # the bar-asof replay anchor (== frame.asof)
                trend_velocity=None,             # PDR-2 fills frame.trend_velocity; None today
                confirm_date=_cd,
            )
        except Exception as exc:  # noqa: BLE001 -- never block frame build on saturation
            logger.debug("build_perception_frame(%s): saturation failed: %s", symbol, exc)
            frame_saturation = None
```

Then pass `saturation=frame_saturation` in the `PerceptionFrame(...)` call (replacing
`saturation=None` at line 200). `os` is already imported (line 173). When PDR-2 lands, swap
`trend_velocity=None` for the PDR-2 velocity score (and pass that same value to `trend_velocity=`
in the `PerceptionFrame(...)` constructor at line 198).

> **Decision-asof note:** the score is stamped with `last_bar_ts_utc` (the bar-asof replay anchor,
> `builder.py:128-133`), NOT wall-clock. This keeps backtests lookahead-honest and is consistent
> with how the no-lookahead gate validates the producing path. The decay anchors (peak / pub /
> confirm) are all required `<= asof` inside `compute_saturation`.

### 3.3 MODIFIED — `hermes_quant/analysts/semantic.py` (APPLY, S6)

Insert the `[SATURATE]` step between the regime multiplier (line 130) and the `AnalystView`
construction (line 132). Flag read at call time:

```python
        # ADR-0079 PDR-4 [SATURATE]: silence-only edge-decay multiplier on THIS
        # view's confidence, BEFORE BMA (D79.4: view-local, never the aggregate).
        # Flag read at call time; OFF or no saturation extra -> m=1.0 (no-op,
        # byte-identical). post <= pre is guaranteed by apply_saturation's clamp.
        import os
        if os.environ.get("HERMES_QUANT_SATURATION", "0") == "1" and ctx.extras:
            try:
                from hermes_quant.perception.saturation import apply_saturation
                _sat = ctx.extras.get("saturation")
                confidence = apply_saturation(float(confidence), _sat)
                if _sat is not None:
                    metadata["saturation"] = dict(_sat)   # provenance into the view metadata
            except Exception:  # noqa: BLE001 -- saturation must not break the view
                pass
```

This sits immediately before `view = AnalystView(...)` (line 132). `metadata` already exists (built
at lines 115-122). The `dict(_sat)` write satisfies the spec's "attach saturation to
`SemanticPacket.metadata`" via the view metadata that carries the packet's provenance.

> **Why the flag is read in BOTH places (builder produce + analyst apply):** they are independent
> guards. Builder-OFF → `frame.saturation` is `None` → adapter writes nothing → analyst has no
> extra to apply. Analyst-OFF → even if some other producer put a `saturation` extra in, the
> analyst ignores it. Either guard alone yields flag-OFF byte-identity; both held together is the
> belt-and-suspenders the rails want (mirrors how `HERMES_QUANT_SEMANTIC_ENABLED` is checked at
> both `advisor.py:377/960` and `wiring.py:40`).

### 3.4 NEW (eval-only, NOT a hermes_quant module) — `ops/scripts/quant-pdr4-saturation-backtest.py`

A driver, mirroring `ops/scripts/quant-catalyst-socialarb-eval.py` (passes graph/aliases
explicitly, mutates nothing live), that loads the versioned exit-set fixture (§5) and reports the
sat-on-vs-off social-arb Sharpe on the labeled exit set. Produces the §6.5 number for the operator
audit. Read-only; the flag flip is a separate human decision.

---

## 4. The default-OFF flag-gating idiom (copy this)

Copied verbatim-in-shape from `catalyst/wiring.py:40` and `regime_aware_confidence.py:25-26`:

```python
import os
if os.environ.get("HERMES_QUANT_SATURATION", "0") == "1":
    ...   # produce / apply
```

- Read **at call time**, never cached at import (so a test can flip it per-case).
- Default `"0"` → OFF → `m=1.0` everywhere → byte-identical.
- Two call sites (builder S3, analyst S6); either alone is sufficient for byte-identity, both held.

---

## 5. Versioned labeled exit-set fixture (N13 — NOT /tmp)

Per AGENTS.md N13 (fixtures live under `tests/fixtures/`, never `/tmp`), promote the labeled exit
set to:

```
tests/fixtures/pdr4_saturation/exit_set.v1.json
```

Schema (one record per labeled exit case — a social-arb trend that DID saturate, with the realized
forward return AFTER the saturation point so the decay is measurable as edge-preservation):

```json
{
  "version": 1,
  "description": "Labeled social-arb EXIT cases: trends past their velocity peak / confirm date.",
  "cases": [
    {
      "symbol": "CROX",
      "packet_asof": "2024-02-01T13:30:00Z",
      "decision_asof": "2024-03-15T13:30:00Z",
      "peak_asof": "2024-02-05T13:30:00Z",
      "confirm_date": "2024-02-20T21:00:00Z",
      "stance": "bullish",
      "pre_sat_confidence": 0.62,
      "realized_forward_return_pct": -3.1,
      "note": "Earnings (confirm_date) passed; Wall Street caught up; edge gone -> the silenced view AVOIDS the post-parity drawdown."
    }
  ]
}
```

The backtest (§6.5) replays each case twice (flag OFF → `m=1.0`; flag ON → decayed) and computes
the social-arb-slice Sharpe each way. **The exit set is small now (the mechanism + unit eval gate
on it immediately); a larger labeled set is the B09 follow-up that clears the live-influence bar.**

---

## 6. Tests = the eval gate as pytest-verifiable acceptance criteria

All new tests in `tests/unit/test_pdr4_saturation.py` unless noted. Run with:
`~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/unit/test_pdr4_saturation.py -q`

### 6.1 Property Test A — silence-only (post ≤ pre, for EVERY input)
Dense parametrize (or Hypothesis sweep) over `confidence ∈ [0,1]` × arbitrary `saturation` dicts
(including adversarial `decay_multiplier` values: `>1`, `<=0`, `NaN`, missing key):
```python
def test_saturation_never_raises_confidence():
    for conf in [0.0, 0.1, 0.37, 0.5, 0.62, 0.9, 1.0]:
        for sat in [None, {}, {"decay_multiplier": 0.5}, {"decay_multiplier": 1.0},
                    {"decay_multiplier": 1.5}, {"decay_multiplier": 0.0},
                    {"decay_multiplier": -0.2}, {"decay_multiplier": float("nan")}]:
            post = apply_saturation(conf, sat)
            assert post <= conf + 1e-12              # NEVER raises
            assert post >= 0.0
```
**ACCEPTANCE:** passes for every combination → silence-only proven (rail #2).

### 6.2 Property Test B — view-local (non-semantic views bit-identical sat-on vs sat-off)
Build a fixed `views` list (ClassicalTA + Kronos + Semantic), run the analyst loop
(`advisor.py:969-992`) or `recommend()` with `HERMES_QUANT_SATURATION` OFF then ON, assert every
NON-semantic `AnalystView` (`direction/magnitude/confidence/confidence_raw/rationale/metadata`) is
**bit-identical** across the two runs, and only the semantic view's `confidence` differs (and only
downward).
```python
def test_saturation_only_touches_semantic_view(monkeypatch):
    off = _run_analyst_views(monkeypatch, sat="0")   # collects views from advisor.py:969-992
    on  = _run_analyst_views(monkeypatch, sat="1")
    for name in ("classical_ta", "kronos", "microstructure"):
        assert _view(off, name) == _view(on, name)   # bit-identical
    assert _view(on, "hermes_semantic").confidence <= _view(off, "hermes_semantic").confidence
```
**ACCEPTANCE:** non-semantic views identical → the D79.4 boundary (no cross-view veto) proven
(rail #3). This is the test that distinguishes PDR-4 from the rejected aggregate-level placement.

### 6.3 Flag-OFF byte-identical
With `HERMES_QUANT_SATURATION` unset/`"0"`: `compute_saturation` is never called (builder gate),
`apply_saturation` is never called (analyst gate), `frame.saturation is None`, adapter writes no
`saturation` extra, and the full `recommend()` output is byte-identical to a baseline captured with
the PDR-4 code physically absent (compare serialized `AdvisorResult` / `AnalystView` list).
```python
def test_flag_off_byte_identical(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_SATURATION", raising=False)
    r = recommend(symbol="CROX", timeframe="1d", asof=T, perception_frame=frame)
    assert _serialize(r) == _BASELINE_SERIALIZED   # captured from HEAD before PDR-4
```
**ACCEPTANCE:** identical → default-OFF safety proven (rail #5).

### 6.4 asof / no-lookahead
`compute_saturation` must ignore any anchor in the future and must stamp `asof`:
```python
def test_saturation_is_lookahead_honest():
    asof = pd.Timestamp("2024-03-01T00:00:00Z")
    # future peak / future confirm / future packet -> all ignored -> no decay (m == 1.0)
    fut = compute_saturation(packet_asof="2024-04-01T00:00:00Z", asof=asof,
                             trend_velocity={"peak_asof": "2024-04-01T00:00:00Z"},
                             confirm_date="2024-04-01T00:00:00Z")
    assert fut["decay_multiplier"] == 1.0 and fut["basis"] == "no_basis"
    assert pd.Timestamp(fut["asof"]) == asof          # stamps the decision asof
    # past peak -> decays
    assert compute_saturation(packet_asof="2024-02-01T00:00:00Z", asof=asof,
                              trend_velocity={"peak_asof": "2024-02-01T00:00:00Z"}
                              )["decay_multiplier"] < 1.0
```
**ACCEPTANCE:** future anchors ignored, `asof` stamped → rail #6. Also confirm the existing
no-lookahead gate stays green on the producing path (it already covers the builder per Wave S5).

### 6.5 Backtest — decay improves social-arb Sharpe on the labeled exit set (live-influence gate)
Driven by `ops/scripts/quant-pdr4-saturation-backtest.py` over
`tests/fixtures/pdr4_saturation/exit_set.v1.json`. Replays each case sat-OFF (`m=1.0`) vs sat-ON
and computes the social-arb-slice Sharpe both ways.
```python
def test_decay_improves_exit_set_sharpe():
    cases = _load_exit_set("tests/fixtures/pdr4_saturation/exit_set.v1.json")
    sharpe_off = _sharpe(cases, saturation=False)
    sharpe_on  = _sharpe(cases, saturation=True)
    assert sharpe_on >= sharpe_off               # decay never HURTS on the exit set
```
**ACCEPTANCE (mechanism gate, builds now):** `sharpe_on >= sharpe_off` on the v1 fixture.
**ACCEPTANCE (live-influence gate, B09):** a strictly higher bar on the *larger* labeled exit set
before the operator flips the flag on a live cron. Stated explicitly: the v1 fixture proves the
mechanism; the live flip waits on B08/B09 data volume + a side-by-side audit.

### 6.6 Producer unit tests (round out coverage)
- `compute_saturation` basis precedence (confirm > peak > packet-age > none).
- `confirm_date_passed` → `m == _FLOOR`.
- empty / unparseable inputs → `m == 1.0` (silence-only safety).
- monotonic: older age → smaller `m` (within a basis).

---

## 7. Build order (one sitting)

1. `hermes_quant/perception/saturation.py` (§3.1) + `tests/unit/test_pdr4_saturation.py` §6.1/6.4/6.6
   (TDD: pure math + property A + no-lookahead first).
2. Wire the APPLY point in `analysts/semantic.py` (§3.3) + Property Test B (§6.2) + flag-OFF (§6.3).
3. Wire the PRODUCE point in `perception/builder.py` (§3.2) — confirm `frame.saturation` populates
   and projects via the adapter (`adapter.py:55-56`, already present). Use dict `.get()` (S10 note).
4. Promote `tests/fixtures/pdr4_saturation/exit_set.v1.json` (§5) + the backtest driver (§3.4) +
   §6.5. Hand the operator the sat-on-vs-off Sharpe number; the flag flip is their call.
5. Run the full unit suite + the no-lookahead gate; confirm all green and flag-OFF byte-identical.

---

## 8. What PDR-4 explicitly does NOT do (scope fence)

- Does NOT touch `BMAAggregator`, the gate, the analyst Protocol, or the discrete ladder.
- Does NOT apply to the post-BMA `AggregatedSignal` (the rejected aggregate-level placement, D79.4).
- Does NOT replace or relax `require_ensemble` (cross-ANALYST) or PDR-3 convergence (cross-SOURCE) —
  it is orthogonal and complementary to both.
- Does NOT add a new flag-flip to any live cron — the build ships default-OFF; arming is a separate
  human decision after the §6.5 live-influence gate clears on B09 data.
- Does NOT require PDR-2 to build: the velocity-peak basis is *preferred* but the packet-age and
  confirm-date bases let the mechanism + unit eval ship now.
