# R — PerceptionFrame design-recon (PDR-1), grounded in HEAD code

> **Status:** research / design-recon · **Date:** 2026-05-30 · For: PDR-1 (the `PerceptionFrame`
> carrier from ADR-0079 §D79.2, design doc `pdr-unified-architecture.md` §4).
> **This doc RECONS; it does not BUILD.** It pins the exact current shapes/seams so the PDR-1
> plan is implementable with zero churn to the analyst Protocol, BMA, or the gate.
>
> Anchors: ADR-0079 (D79.1/D79.2/D79.5, Rollout PDR-1), `docs/design/pdr-unified-architecture.md` §4,
> ADR-0063 (regime extras), ADR-0074 (semantic asof), ADR-0068/0069 (decision-asof vs bar-asof).

---

## 1. The EXACT current shape of `MarketContext` and what lands in `ctx.extras`

`MarketContext` is a `@dataclass(frozen=True)` at **`hermes_quant/protocol.py:57-82`**. Fields, in order:

| field | type | source in advisor | line |
|---|---|---|---|
| `asset` | `str` | the symbol | `advisor.py:872` |
| `timeframe` | `str` (a `Timeframe` literal) | resolved per asset_class | `advisor.py:873` |
| `asset_class` | `str` (an `AssetClass` literal) | arg / recipe default | `advisor.py:874` |
| `exchange` | `str | None` | always `None` for yfinance equity | `advisor.py:875` |
| `bars` | `pd.DataFrame` (canonical OHLCV, timestamp a **column**) | `_fetch_with_as_of()` → as_of filter → `drop_still_forming_bar` | `advisor.py:780,817,830` |
| `last_close` | `float` | `float(bars["close"].iloc[-1])` | `advisor.py:877` |
| `last_volume` | `float` | `float(bars["volume"].iloc[-1])` | `advisor.py:878` |
| `asof` | `pd.Timestamp` (UTC) | the **last-bar timestamp** (`last_bar_ts_utc`), NOT wall-clock | `advisor.py:847,879` |
| `extras` | `Mapping[str, Any]` (read-only; default `{}`) | `ctx_extras_base` | `advisor.py:850-880` |

**Required bar columns:** `['timestamp','open','high','low','close','volume']`; optional `['amount']`
(`protocol.py:64-65`). Bars are UTC, ascending, deduped — enforced at the data layer.

**The exact `ctx.extras` key-set the advisor builds today** (`recommend`, `advisor.py:850-880`):
- `caller market_extras` first — `ctx_extras_base = dict(market_extras or {})` (`:850`). This is where
  the catalyst seam injects `semantic_packets` + `decision_asof` (see §2).
- **still-forming bar values** (ADR-0069), only when a still-forming daily bar was dropped (`:854-858`):
  `still_forming_close`, `still_forming_high`, `still_forming_low`, `still_forming_volume`.
- **regime** (ADR-0063), merged **OVER** caller values so callers can't shadow it (`:861-863` →
  `build_regime_extras`): always sets three keys — `regime` (a `RegimePacket | None`,
  `extras_builder.py:43-64`), `regime_failure` (`str | None`), `regime_classifier_kind`
  (`"rule_based" | "hmm" | "unavailable"`). Never raises (ADR-0036 outer guard, `extras_builder.py:172-181`).
- **semantic** (only when the catalyst seam ran with the flag ON, via `market_extras`):
  `semantic_packets` (list of packet dicts) + `decision_asof` (ISO string) — set in
  `catalyst/wiring.py:50-51`, consumed by `HermesSemanticAnalyst` at `analysts/semantic.py:148,166`.

So **`extras` is the de-facto perception side-channel today**: `{regime, regime_failure,
regime_classifier_kind, [semantic_packets, decision_asof], [still_forming_*]}`. `recommend_multi_horizon`
builds the same regime keys per-horizon (`advisor.py:567-593`) but does **not** inject semantic.

> Two distinct timestamps matter for `PerceptionFrame.asof`: **bar-asof** (`ctx.asof` = last settled bar,
> the replay anchor; `advisor.py:847`) vs **decision-asof** (wall-clock now, the live lookahead cutoff
> for packets; `wiring.py:45`, consumed at `semantic.py:161-172`). The frame must carry the bar-asof as
> `asof` (to preserve replay) and keep `decision_asof` inside `extras`/a field so the semantic honesty
> branch (ADR-0068/0074) is unchanged.

---

## 2. The three current injection seams (the non-uniformity PDR-1 collapses)

All three live paths call `advisor.recommend(..., market_extras=...)`, but each constructs `market_extras`
**differently** — this is GAP-D (design §4; ADR-0079 GAP-D). Wave C2-2 already routed all three through the
single helper `catalyst/wiring.py:semantic_market_extras(symbol, *, decision_asof=None, horizon, base_extras)`
(`wiring.py:22-54`), which returns `None` (the advisor no-op) when `HERMES_QUANT_SEMANTIC_ENABLED != "1"` or
there are no packets. But the **call shape still differs per path**:

| Path | File:line | How it builds `market_extras` |
|---|---|---|
| **daily-interim** | `ops/scripts/quant-daily-interim.py:125-128` | `market_extras = semantic_market_extras(symbol, horizon=timeframe)`; then `recommend(symbol=, asset_class=, timeframe=, market_extras=market_extras)`. Direct, eager. |
| **autonomous-tick** | `ops/scripts/quant-autonomous-tick.py:329-347` | injection is **inside a `_direction_screened_recommend(**kwargs)` wrapper** handed to `auto.tick(advisor_recommend=...)`. It injects only **when the caller didn't already pass `market_extras`** (`:342`) and pulls the symbol from `kwargs.get("symbol")`, horizon from `kwargs.get("timeframe","1d")`. Lazy, conditional, kwargs-threaded. |
| **playbook-tick** | `ops/scripts/quant-playbook-tick.py:464-476` | computes `primary_timeframe = horizons[-1]` from `HERMES_QUANT_HORIZONS`, wraps the helper in its own `try/except` (`:469-473`), then `recommend(symbol, asset_class="equity", timeframe=primary_timeframe, market_extras=_me)`. Has a `_mock_recommend` bypass (`:442-443`) that never reaches the seam. |

Three different call sites, three different ways to derive `(symbol, horizon, decision_asof)`, three error
postures — and `recommend_multi_horizon` (`advisor.py:446-628`) is a **fourth** entry that never injects
semantic at all. **PDR-1 collapses these to ONE frame built by one loader**, handed in via the new
`recommend(perception_frame=...)` kwarg; the helper stays the producer of the `semantic_packets`/`decision_asof`
slice, now poured into the frame instead of three ad-hoc `market_extras` dicts.

---

## 3. The minimal `PerceptionFrame` + `frame_to_context` + `recommend(perception_frame=None)`

### 3.1 The dataclass (matches ADR-0079 §D79.2 / design §4.1 exactly)

```python
# hermes_quant/perception/frame.py   (NEW module — FUTURE / PDR-1)
from __future__ import annotations
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any
import pandas as pd

@dataclass(frozen=True)
class PerceptionFrame:
    """Everything perceived about ONE symbol at ONE asof — the typed PERCEPTION
    boundary. A CONTAINER, never an authority. Built once per symbol so the
    catalyst flag/wiring decoupling (GAP-D) is structurally impossible.
    Add-only versioning (mirrors protocol.py:14)."""
    symbol: str
    asof: pd.Timestamp                       # BAR-asof, UTC = the replay anchor (== ctx.asof today)
    bars: pd.DataFrame                       # canonical OHLCV (already as_of-filtered + still-forming-dropped)
    last_close: float
    regime: Mapping[str, Any] | None = None  # the ADR-0063 RegimePacket (frame carries the packet object)
    semantic_packets: tuple[Any, ...] = ()   # finished catalyst/social packet dicts (already validated)
    trend_velocity: Mapping[str, Any] | None = None  # GAP-A  (HERMES_QUANT_TREND_VELOCITY) — empty until PDR-2
    convergence: Mapping[str, Any] | None = None      # GAP-B  (HERMES_QUANT_CONVERGENCE)     — empty until PDR-3
    saturation: Mapping[str, Any] | None = None       # GAP-C  (HERMES_QUANT_SATURATION)      — empty until PDR-4
    provenance: tuple[str, ...] = ()         # evidence_ids / source URLs / fetch run-ids (ADR-0033/0041)
    extras: Mapping[str, Any] = field(default_factory=dict)  # forward-compat escape hatch; carries decision_asof, still_forming_*, regime_failure, regime_classifier_kind
```

### 3.2 The pure adapter `frame_to_context(frame) -> MarketContext`

The adapter is the **only** place that knows how the frame projects into today's `extras` shape. It must
reproduce `advisor.py:850-880` byte-for-byte: regime keys + semantic keys + still-forming keys + decision_asof.

```python
def frame_to_context(
    frame: PerceptionFrame,
    *,
    timeframe: str,
    asset_class: str,
    exchange: str | None = None,
) -> MarketContext:
    extras: dict[str, Any] = dict(frame.extras)            # decision_asof, still_forming_*, regime_failure/_kind
    # regime keys exactly as advisor builds them (ADR-0063); frame.regime IS the RegimePacket
    extras["regime"] = frame.regime
    extras.setdefault("regime_failure", None)
    extras.setdefault("regime_classifier_kind",
                      "unavailable" if frame.regime is None else getattr(frame.regime, "classifier_kind", "rule_based"))
    if frame.semantic_packets:                              # only set when non-empty (matches today's "None means absent")
        extras["semantic_packets"] = list(frame.semantic_packets)
    # the three future scores ride in extras under their own keys (analysts ignore unknown keys, protocol.py:16)
    if frame.trend_velocity is not None: extras["trend_velocity"] = frame.trend_velocity
    if frame.convergence   is not None: extras["convergence"]    = frame.convergence
    if frame.saturation    is not None: extras["saturation"]     = frame.saturation
    return MarketContext(
        asset=frame.symbol, timeframe=timeframe, asset_class=asset_class, exchange=exchange,
        bars=frame.bars, last_close=frame.last_close,
        last_volume=float(frame.bars["volume"].iloc[-1]),
        asof=frame.asof, extras=extras,
    )
```

**Critical fidelity rule:** `frame_to_context` is *pure* and does **not** call `build_regime_extras` or the
semantic helper — those run during frame *construction*. The adapter only re-shapes. This keeps the
"regime merged OVER caller values" ordering intact and avoids double-classifying.

### 3.3 `recommend(perception_frame=None)` — None builds internally (byte-identical to today)

`recommend` today (`advisor.py:631-648`) has `market_extras: Mapping | None = None` but **no
`perception_frame`** — ADR-0079 §D79.2 promised it; PDR-1 adds it. The minimal, non-churning shape:

```python
def recommend(symbol, *, ..., market_extras=None, perception_frame=None):
    ...
    if perception_frame is None:
        # EXISTING PATH, byte-identical: fetch → as_of filter → drop_still_forming → build_regime_extras
        # → ctx (advisor.py:741-881). market_extras still works as the no-op side-channel it is today.
        ...build ctx exactly as now...
    else:
        # PDR-1 PATH: the frame was built once upstream; just project it.
        ctx = frame_to_context(perception_frame, timeframe=timeframe, asset_class=asset_class)
        result.as_of = perception_frame.asof
        result.bars_received = len(perception_frame.bars)
    # Steps 5-8 (analysts → BMA → gate → lessons) are IDENTICAL on both branches.
```

Mutual-exclusion rule: if both `perception_frame` and `market_extras` are passed, the frame's `extras`
wins (it already absorbed the semantic slice); document `market_extras` as ignored-when-frame-present, or
raise `ValueError` (recommend: ignore-with-caveat, to keep the silence-by-default posture). The
default-None path leaves every existing caller and `recommend_multi_horizon` untouched.

---

## 4. The migration — default-OFF / additive, all callers + backtests bit-identical

PDR-1 has **no behavior flag** (ADR-0079 Rollout PDR-1: "None = today"). Additivity comes from the
default-`None` kwarg, not an env gate. Steps:

1. **Add the module + adapter + kwarg, default `None`.** `recommend(perception_frame=None)` with the
   `None` branch = today's `advisor.py:741-881` verbatim. No existing call site changes; every backtest
   (`tests/integration/test_backtest_replay.py`, `test_walk_forward_backtest.py`, `test_backtest_calibrator_loop.py`)
   and the no-lookahead gate (`tests/test_no_lookahead.py`) call `recommend(...)` with no frame → unchanged.
2. **Add a single frame builder** `build_perception_frame(symbol, *, asof, timeframe, asset_class, provider, ...)`
   that does exactly what `recommend`'s `None` branch does up to ctx-build (fetch → as_of → still-forming →
   `build_regime_extras` → `semantic_market_extras`), then returns a `PerceptionFrame`. Prove
   `frame_to_context(build_perception_frame(...)) == ctx_built_by_recommend(...)` on fixtures (the replay test).
3. **Switch the 3 crons to hand in ONE frame.** daily-interim (`:125-128`), autonomous-tick wrapper
   (`:329-347`), playbook-tick (`:464-476`) each replace their bespoke `semantic_market_extras → recommend`
   with `frame = build_perception_frame(...); recommend(..., perception_frame=frame)`. The
   `semantic_market_extras` helper becomes an internal of `build_perception_frame` (its `decision_asof`
   default — wall-clock now — moves into the builder, preserving ADR-0068/0074 live semantics). GAP-D
   cannot recur: there is one populated input, not three side-channels.
4. **`recommend_multi_horizon` is a later, optional opt-in** — out of PDR-1 scope; it keeps its current
   per-horizon ctx build. (PDR-1 only needs the single-horizon `recommend` path the 3 crons use.)

**Rails preserved:** analyst Protocol / BMA / `DefaultRiskGate` are untouched (frame projects INTO
`MarketContext`); discrete ladder untouched; the frame is a container, never consulted by the gate;
money still flows CLI/HITL only; `asof` honesty kept (frame.asof = bar-asof for replay, `decision_asof`
in extras for the live packet cutoff). With no frame passed → byte-identical.

---

## 5. The eval gate — byte-identical replay + no-lookahead green

PDR-1's promotion gate (ADR-0079 Rollout PDR-1; design §6.2) is **two assertions**:

1. **Byte-identical replay on all fixtures.** New test (extend `tests/test_no_lookahead.py` and/or a new
   `tests/perception/test_frame_replay.py`): for each fixture (the synthetic `_make_bars` generators in
   `test_no_lookahead.py`, plus the integration replay/walk-forward fixtures), assert
   `recommend(symbol, as_of=t, provider=p)  ==  recommend(symbol, as_of=t, provider=p, perception_frame=build_perception_frame(...))`
   on the load-bearing keys already pinned by `test_advisor_deterministic_under_as_of_replay`
   (`test_no_lookahead.py:264`): `["as_of", "aggregated_signal", "risk_gate", "decision_price"]`, plus
   `analyst_views`. This is the ADR-0079 worked example (`ADR-0079:209-215`): `assert r_today == r_frame`.
2. **No-lookahead gate green on the producing path.** The frame builder is now the producer of
   `decision_asof` / `semantic_packets`, so the ADR-0074 publication-time honesty tests
   (`test_no_lookahead.py:427-538`, Invariant 5) must pass through the frame path too: a packet with
   `asof > decision_asof` still has zero influence after projection. Wave **S5** is extending the
   no-lookahead gate to the *producing* path — PDR-1's replay test plugs into that extension (frame.asof =
   bar-asof, frame carries decision_asof; the semantic analyst's `<=` cutoff at `semantic.py:161-172` is
   unchanged because the adapter passes `decision_asof` through verbatim).

Both gates are deterministic, fixture-only, no network (AGENTS.md testing discipline). The frame adds
**zero** new statistical surface (no scores populated until PDR-2/3/4 flip their own flags) — PDR-1 is a
pure refactor of the perception boundary, so "byte-identical" is the whole bar.

---

### Implementability notes (zero-churn confirmations)

- `MarketContext.extras` is typed `Mapping[str, Any]` with `protocol.py:16` "consumers ignore unknown
  fields" — the three future score keys (`trend_velocity`/`convergence`/`saturation`) ride in `extras`
  with no Protocol change.
- The advisor already tolerates `market_extras=None` as a no-op (`advisor.py:850`), so the
  `perception_frame=None` default is the *same* proven additive pattern (ADR-0079 §D79.2).
- `frame.regime` should carry the `RegimePacket` **object** (not the 3-key dict) — the adapter re-expands
  it to the 3 keys advisor writes, so `ctx.extras["regime"]` is the same `RegimePacket | None` analysts
  read today (`semantic.py:127`, `regime_aware_confidence.py`).
- New package dir `hermes_quant/perception/` does not exist yet (verified) — PDR-1 creates it.
