# Wave E — PDR-1: `PerceptionFrame` carrier + `frame_to_context` adapter

> **Status:** plan / implementation-ready · **Date:** 2026-05-30 · **Wave:** E (PDR-1, the first of the four ADR-0079 future abstractions)
> **This doc PLANS; the implementing session BUILDS.** It is grounded in, and must not contradict:
> [`docs/research/2026-05-30-r-perceptionframe-recon.md`](../research/2026-05-30-r-perceptionframe-recon.md) (the HEAD-pinned recon),
> [ADR-0079](../adr/ADR-0079-perception-decision-reaction-architecture.md) (§D79.2, §D79.6a, Rollout PDR-1),
> [`docs/design/pdr-unified-architecture.md`](../design/pdr-unified-architecture.md) §4.
>
> **Scope: additive, default-safe.** `perception_frame=None` ⇒ behavior is **byte-identical to today** on every
> existing caller, every backtest fixture, and the no-lookahead gate. There is **no behavior flag** for PDR-1 (per
> ADR-0079 Rollout PDR-1: "None = today"); additivity comes from the default-`None` kwarg, not an env gate.

---

## 0. What PDR-1 is, and what it deliberately is NOT

**Is:** a typed PERCEPTION boundary — one frozen `PerceptionFrame` that *is* "everything perceived about one
symbol at one asof", built **once** per symbol by **one** builder, projected into the existing `MarketContext`
by **one** pure adapter, and handed into `recommend()` via **one** new optional kwarg. It collapses the three
non-uniform catalyst-injection seams (GAP-D) into a single populated input so the flag/wiring decoupling becomes
**structurally impossible** to reintroduce, and gives the perception layer the typed name it lacks (GAP-E).

**Is NOT:** any new statistical surface. PDR-1 populates **zero** of the three future score fields
(`trend_velocity`/`convergence`/`saturation` stay empty until PDR-2/3/4 flip their own flags). It does not touch
the analyst Protocol, BMA, the gate, the discrete ladder, or any money path. The whole bar is "byte-identical."

**Three rail-critical correctness constraints this plan pins (from the build-wave reviews):**

- **M06 — ADMIT-before-GATE ordering is upstream of this wave and must be respected, not re-touched.** ADR-0077
  admissibility is a hard precondition that runs **before** `DefaultRiskGate.gate()` (ADR-0077 D77.4 / §83:
  "sits upstream of the ADR-0004 risk gate as a hard precondition"). PDR-1 changes **only how `ctx` is built**
  (Step 4 of `recommend`); Steps 5–8 (analysts → BMA → **admit → gate** → lessons) are **identical on both
  branches**. The frame projects INTO `MarketContext` and then the existing ADMIT-before-GATE sequence runs
  unchanged. The plan's replay gate (`§5`) is what proves PDR-1 did not perturb that ordering.
- **M17 — the tool-path semantic decoupling is closed by this wave.** Today `autonomous.tick`
  (`autonomous.py:368-369`) defaults `advisor_recommend` to the bare `advisor.recommend` and calls it at
  `autonomous.py:414-419` **with no `market_extras`** — only the *cron wrapper* (`quant-autonomous-tick.py:341-346`)
  monkey-patches injection. So the `quant_autonomous_tick` **TOOL** path (`__init__.py:153-156` → `quant_tools`)
  yields `no_semantic_packets` even with the flag ON. When the single builder lives **inside** the producer that
  both the tool and cron paths share (see §4 Step 3c), the tool path perceives the same frame the cron does —
  M17 closes structurally, not by a second monkey-patch.
- **S5 / M21 — the no-lookahead gate's producing-path extension is a PDR-1 precondition that this wave's replay
  test plugs into.** The frame builder is now the **producer** of `decision_asof` + `semantic_packets`, so the
  ADR-0074 publication-time honesty must hold *through the frame path*. The producing-path tests already exist
  (`tests/test_no_lookahead.py:597,638` — `test_semantic_market_extras_excludes_future_asof_packet`,
  `..._only_future_packet_injects_nothing`, labeled M21); PDR-1 adds the frame-path analogue (§5.2) so the gate
  stays green when the producer moves from the wiring helper into `build_perception_frame`.

---

## 1. Exact current shapes this wave depends on (HEAD-pinned)

| Anchor | File:line | Why it matters to PDR-1 |
|---|---|---|
| `MarketContext` frozen dataclass, 9 fields | `protocol.py:57-82` | the adapter's projection target; unchanged. `extras` is `Mapping[str,Any]`, "consumers ignore unknown fields" (`protocol.py:16`). |
| `recommend(...)` signature, has `market_extras=None` but **no** `perception_frame` | `advisor.py:631-648` | the kwarg lands here; `market_extras=None` is the proven additive no-op pattern PDR-1 mirrors. |
| ctx-build block (the verbatim `None` branch) | `advisor.py:849-881` | fetch→as_of-filter (`:810-817`)→`drop_still_forming_bar` (`:830`)→still-forming extras (`:854-858`)→`build_regime_extras` merged OVER caller (`:861-863`)→`MarketContext` (`:871-881`). `result.as_of = last_bar_ts_utc` (`:847`) is the **bar-asof** = replay anchor. |
| Steps 5–8 (analysts→BMA→**admit→gate**→lessons) | `advisor.py:883-1026` | **identical on both branches** — PDR-1 does not touch these, preserving M06 ordering. |
| `semantic_market_extras(symbol, *, decision_asof=None, horizon, base_extras)` | `catalyst/wiring.py:22-54` | the single producer of the `{semantic_packets, decision_asof}` slice; becomes an internal of the builder. Returns `None` when flag OFF / no packets / any error. |
| daily-interim seam (eager, direct) | `quant-daily-interim.py:125-128` | call-site edit #1. |
| autonomous-tick seam (lazy, conditional, kwargs-threaded) + `autonomous.tick` default | `quant-autonomous-tick.py:341-347`, `autonomous.py:368-369,414-419` | call-site edit #2 — **and the M17 fix locus**. |
| playbook-tick seam (own try/except, `_mock_recommend` bypass) | `quant-playbook-tick.py:464-476` | call-site edit #3; `_mock_recommend` (`:442-443`) must stay un-touched (never reaches the seam). |
| replay test (the byte-identical proof's existing twin) | `tests/test_no_lookahead.py:236-272` | load-bearing keys: `["as_of","aggregated_signal","risk_gate","decision_price"]`. |
| producing-path honesty tests (M21) | `tests/test_no_lookahead.py:597,638,658,697` | the gate the frame path must keep green. |
| `RegimePacket` carried as object, re-expanded to 3 keys by advisor | `regime/extras_builder.py:43-64,172-181` | `frame.regime` carries the **packet object**; adapter re-expands. |

---

## 2. Deliverables (exact files)

| # | File | New / edit | Deliverable |
|---|---|---|---|
| D1 | `hermes_quant/perception/__init__.py` | **NEW** | package marker (dir does not exist — recon §confirmed). |
| D2 | `hermes_quant/perception/frame.py` | **NEW** | the `PerceptionFrame` frozen dataclass (§3.1). |
| D3 | `hermes_quant/perception/adapter.py` | **NEW** | the pure `frame_to_context(frame, *, timeframe, asset_class, exchange=None)` (§3.2). |
| D4 | `hermes_quant/perception/builder.py` | **NEW** | the single `build_perception_frame(...)` loader (§3.3); absorbs `semantic_market_extras`. |
| D5 | `hermes_quant/advisor.py` | **edit** | add `perception_frame: PerceptionFrame \| None = None` kwarg + the `None`/frame branch at Step 4 (§3.4). |
| D6 | `ops/scripts/quant-daily-interim.py` | **edit (FINAL step)** | replace bespoke `semantic_market_extras → recommend` with `build_perception_frame → recommend(perception_frame=)`. |
| D7 | `ops/scripts/quant-autonomous-tick.py` + `hermes_quant/autonomous.py` | **edit (FINAL step)** | move injection into the shared producer so tool + cron both perceive the frame (M17). |
| D8 | `ops/scripts/quant-playbook-tick.py` | **edit (FINAL step)** | same swap; keep the `_mock_recommend` bypass intact. |
| T1 | `tests/perception/test_frame_dataclass.py` | **NEW** | frame is frozen / add-only / defaults safe. |
| T2 | `tests/perception/test_frame_to_context.py` | **NEW** | adapter purity + exact extras-shape fidelity. |
| T3 | `tests/perception/test_frame_replay.py` | **NEW** | **the byte-identical-replay proof** (the eval gate, §5.1). |
| T4 | `tests/test_no_lookahead.py` | **edit** | add the frame-path producing honesty test (§5.2, the S5/M21 extension). |
| T5 | `tests/perception/test_advisor_frame_kwarg.py` | **NEW** | `recommend(perception_frame=...)` branch + None-default no-op + market_extras precedence. |

> **`advisor.py` import note:** import `PerceptionFrame` for the type hint under `TYPE_CHECKING` (or accept
> `Any` and duck-type) to avoid a new import-time dependency on the perception package from the hot advisor
> module — `frame_to_context` is imported lazily inside the frame branch (mirrors the existing lazy imports at
> `advisor.py:829,861,894`).

---

## 3. Signatures + the projection contract

### 3.1 `hermes_quant/perception/frame.py` — `PerceptionFrame` (matches ADR-0079 §D79.2 / design §4.1 exactly)

```python
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
    Add-only versioning (mirrors protocol.py:14-16: fields added only, never
    renamed/removed before a major bump; consumers ignore unknown fields)."""
    symbol: str
    asof: pd.Timestamp                       # BAR-asof, UTC = the replay anchor (== ctx.asof / advisor.py:847)
    bars: pd.DataFrame                       # canonical OHLCV, already as_of-filtered + still-forming-dropped
    last_close: float
    regime: Any | None = None                # the ADR-0063 RegimePacket OBJECT (adapter re-expands to 3 keys)
    semantic_packets: tuple[Any, ...] = ()   # finished catalyst/social packet dicts (already validated)
    trend_velocity: Mapping[str, Any] | None = None  # GAP-A (HERMES_QUANT_TREND_VELOCITY) — empty until PDR-2
    convergence:    Mapping[str, Any] | None = None   # GAP-B (HERMES_QUANT_CONVERGENCE)     — empty until PDR-3
    saturation:     Mapping[str, Any] | None = None   # GAP-C (HERMES_QUANT_SATURATION)      — empty until PDR-4
    provenance: tuple[str, ...] = ()         # evidence_ids / source URLs / fetch run-ids (ADR-0033/0041)
    extras: Mapping[str, Any] = field(default_factory=dict)
    # extras carries: decision_asof (ISO str), still_forming_* (4 keys), regime_failure, regime_classifier_kind
```

Rationale pins: `regime` is typed `Any | None` (carries the `RegimePacket` object, not the 3-key dict — recon
§Implementability); the three score fields default to `None` so the adapter omits them (no new `extras` keys in
the default path); `extras` is the forward-compat escape hatch that holds *exactly* the non-regime/non-semantic
keys the advisor builds today (`decision_asof`, the four `still_forming_*`, `regime_failure`,
`regime_classifier_kind`).

### 3.2 `hermes_quant/perception/adapter.py` — `frame_to_context` (PURE; reproduces `advisor.py:849-881` byte-for-byte)

```python
def frame_to_context(
    frame: PerceptionFrame, *, timeframe: str, asset_class: str, exchange: str | None = None,
) -> MarketContext:
    extras: dict[str, Any] = dict(frame.extras)             # decision_asof, still_forming_*, regime_failure/_kind
    # regime keys EXACTLY as advisor writes them (ADR-0063); frame.regime IS the RegimePacket object.
    extras["regime"] = frame.regime
    extras.setdefault("regime_failure", None)
    extras.setdefault(
        "regime_classifier_kind",
        "unavailable" if frame.regime is None else getattr(frame.regime, "classifier_kind", "rule_based"),
    )
    if frame.semantic_packets:                              # only when non-empty (matches "None means absent")
        extras["semantic_packets"] = list(frame.semantic_packets)
    # the three FUTURE scores ride in extras under their own keys; analysts ignore unknown keys (protocol.py:16).
    # In PDR-1 these are always None so NOTHING is written — preserving the default-path extras key-set exactly.
    if frame.trend_velocity is not None: extras["trend_velocity"] = frame.trend_velocity
    if frame.convergence    is not None: extras["convergence"]    = frame.convergence
    if frame.saturation     is not None: extras["saturation"]     = frame.saturation
    return MarketContext(
        asset=frame.symbol, timeframe=timeframe, asset_class=asset_class, exchange=exchange,
        bars=frame.bars, last_close=frame.last_close,
        last_volume=float(frame.bars["volume"].iloc[-1]),
        asof=frame.asof, extras=extras,
    )
```

**Critical fidelity rule (recon §3.2):** the adapter is *pure* — it does **not** call `build_regime_extras` or
`semantic_market_extras`. Those run during frame **construction** (§3.3). The adapter only re-shapes, so the
"regime merged OVER caller values" ordering and the "no double-classify" property hold.

**`last_close` provenance pin:** the adapter must reproduce `advisor.py:877` exactly. The builder stores
`frame.last_close = float(bars["close"].iloc[-1])`; the adapter passes it through (does **not** recompute from a
possibly-different column). `last_volume` is recomputed in the adapter from `frame.bars["volume"].iloc[-1]`
identically to `advisor.py:878`.

### 3.3 `hermes_quant/perception/builder.py` — `build_perception_frame` (the ONE loader)

Does exactly what `recommend`'s `None` branch does **up to ctx-build** (`advisor.py:741-869`), then returns a
frame. It is the single producer of the `{semantic_packets, decision_asof}` slice (absorbs
`semantic_market_extras`), so GAP-D / M17 cannot recur.

```python
def build_perception_frame(
    symbol: str, *,
    timeframe: str,
    asset_class: str,
    provider: Any,                         # already-resolved provider (advisor resolves it; builder does not re-resolve)
    asof_ts: pd.Timestamp,                 # the normalized as_of (wall-clock-now or replay cutoff) — advisor passes it in
    lookback_bars: int,
    decision_asof: datetime | None = None, # wall-clock now for live; explicit for backtests (ADR-0068/0074)
    base_extras: Mapping[str, Any] | None = None,
) -> PerceptionFrame | None:               # None when no usable bars (caller falls back to a gated no-data result)
    ...
```

Construction order (each step cites the verbatim advisor line it mirrors):

1. fetch (`advisor.py:766-801`) → as_of filter (`:810-817`) → `drop_still_forming_bar` (`:830`). On any
   empty/no-data outcome, return `None` so the advisor frame-branch produces the *same* `_gated_no_data` result
   the `None` branch would (preserving byte-identity on the degenerate paths).
2. compute `last_bar_ts_utc` (`:839-847`) → this is `frame.asof` (the **bar-asof** replay anchor).
3. seed `extras = dict(base_extras or {})`; add the four `still_forming_*` keys when dropped (`:854-858`).
4. `build_regime_extras(symbol, bars)` (`:861-863`) — but **pull `regime` out** of the returned dict into
   `frame.regime` (the object), and keep `regime_failure` / `regime_classifier_kind` in `frame.extras`. (The
   adapter re-expands `regime` to the `extras["regime"]` key.) Honor the `advisor.py:864-869` except-fallback
   verbatim (regime=None, failure string, kind="unavailable").
5. **semantic slice (absorbs the wiring helper):** call the existing `semantic_market_extras` logic with
   `decision_asof` (default wall-clock now). When it yields packets, set `frame.semantic_packets = tuple(packets)`
   and `frame.extras["decision_asof"] = asof.isoformat()`. When OFF / no packets / error → leave empty
   (silence-by-default; identical to today's `market_extras=None` no-op).
6. `frame.last_close = float(bars["close"].iloc[-1])` (`:877`).
7. PDR-1 leaves `trend_velocity=convergence=saturation=None` and `provenance=()` (filled by PDR-2/3/4 / ADR-0033
   linkage later).

> **Why the advisor passes `provider`, `asof_ts`, `lookback_bars` in** (rather than the builder re-resolving):
> the advisor already does recipe-resolution, provider-resolution (`advisor.py:742-748`), as_of-normalization
> (`:717-728`), and lookback defaults (`:713-715`) **before** Step 4. Re-doing them in the builder risks a
> subtle divergence. The builder receives the *already-resolved* inputs so the frame path and the `None` path
> share the identical pre-fetch state — this is the single most important byte-identity safeguard.

### 3.4 `hermes_quant/advisor.py` — `recommend(perception_frame=None)`

Add the kwarg (last positional-or-keyword in the `*`-only block, after `market_extras`):

```python
def recommend(symbol, *, ..., market_extras=None, perception_frame=None):
    ...  # recipe-resolve, defaults, as_of-normalize, provider-resolve, result scaffold (advisor.py:687-748) — UNCHANGED
    # ---- Step 1-4: build ctx ----
    if perception_frame is None:
        # EXISTING PATH, byte-identical: advisor.py:749-881 verbatim (fetch → as_of → still-forming →
        # build_regime_extras → MarketContext). market_extras stays the proven no-op side-channel.
        ...unchanged...
    else:
        # PDR-1 PATH: the frame was built ONCE upstream; just project it.
        ctx = frame_to_context(perception_frame, timeframe=timeframe, asset_class=asset_class)  # lazy import
        result.as_of = perception_frame.asof          # bar-asof = replay anchor (== :847)
        result.bars_received = len(perception_frame.bars)
        last_bar_ts_utc = perception_frame.asof        # Steps 7-8 read this local
        # NOTE: do NOT recompute last_bar_age_minutes here in PDR-1 (it's wall-clock-derived and already
        # excluded from the replay-compared keys); if set, derive it from (asof_ts - frame.asof) identically.
    # ---- Step 5-8: analysts → BMA → ADMIT → GATE → lessons ----  IDENTICAL on both branches (M06 preserved).
    ...unchanged...
```

**Mutual-exclusion rule (recon §3.3):** when **both** `perception_frame` and `market_extras` are passed, the
**frame wins** (it already absorbed the semantic slice). Posture: *ignore-`market_extras`-with-caveat* (append a
`caveats` entry `"market_extras ignored: perception_frame present"`), to keep the silence-by-default posture — do
NOT raise. Pin this in T5.

**The frame branch must reach the *same* gated-no-data results.** The builder returns `None` on the degenerate
paths (no bars, all-dropped); the **caller** (the cron) then either (a) passes `perception_frame=None` so the
advisor's own `None` branch produces the gated result, or (b) the advisor frame-branch treats a `None` frame as
"build internally." Pin: **a `None` frame is identical to not passing one** — the simplest contract, and it lets
the crons always call `build_perception_frame(...)` and forward the (possibly-`None`) result without branching.

---

## 4. Migration — additive, default-OFF, all callers + backtests bit-identical

PDR-1 has **no behavior flag**; additivity is the default-`None` kwarg. Order is strict — **the 3 cron call-site
edits are the FINAL step**, gated on the replay proof (§5) being green first.

### Step 1 — land the module + adapter + kwarg (default `None`). No call-site changes.
`recommend(perception_frame=None)` with the `None` branch = today's `advisor.py:749-881` verbatim. Every backtest
(`tests/integration/test_backtest_replay.py`, `test_walk_forward_backtest.py`, `test_backtest_calibrator_loop.py`)
and the no-lookahead gate (`tests/test_no_lookahead.py`) call `recommend(...)` with no frame → unchanged. **Run the
full suite here; it must be green before Step 2.**

### Step 2 — land `build_perception_frame` + the replay proof (T3). Still no call-site changes.
Prove `frame_to_context(build_perception_frame(...)) == ctx_built_by_recommend's None branch` on all fixtures via
the **end-to-end** equality `recommend(no frame) == recommend(perception_frame=build_perception_frame(...))` on
the load-bearing keys (§5.1). **This is the eval gate. Do not proceed to Step 3 until it is green.**

### Step 3 — switch the 3 crons to hand in ONE frame (the FINAL step).
Each replaces its bespoke `semantic_market_extras → recommend(market_extras=)` with
`frame = build_perception_frame(...); recommend(..., perception_frame=frame)`. `semantic_market_extras` becomes
an internal of `build_perception_frame` (its `decision_asof` default — wall-clock now — moves into the builder,
preserving ADR-0068/0074 live semantics).

- **3a · daily-interim** (`quant-daily-interim.py:125-128`): the most direct swap. `market_extras =
  semantic_market_extras(symbol, horizon=timeframe)` + `recommend(..., market_extras=market_extras)` →
  `frame = build_perception_frame(symbol, timeframe=timeframe, asset_class=asset_class, ...)` +
  `recommend(symbol=symbol, asset_class=asset_class, timeframe=timeframe, perception_frame=frame)`.
- **3b · playbook-tick** (`quant-playbook-tick.py:464-476`): same swap; **keep** the `_mock_recommend` bypass
  (`:442-443`) un-touched (it never reaches the seam), and keep the surrounding try/except posture (builder
  returns `None`/raises-safe → fall back to `perception_frame=None`).
- **3c · autonomous-tick + `autonomous.tick` (the M17 fix locus)** — this is the load-bearing edit. Move the
  injection **out of** the cron's `_direction_screened_recommend` monkey-patch (`quant-autonomous-tick.py:341-346`)
  and **into** `autonomous.tick` itself, so the **tool** path (`__init__.py:153-156 quant_autonomous_tick` →
  `quant_tools` → `autonomous.tick`, which today calls bare `advisor_recommend(...)` at `autonomous.py:414-419`)
  perceives the same frame the cron does. Concretely: in `autonomous.tick`, before each
  `advisor_recommend(symbol=...)`, build `frame = build_perception_frame(entry.symbol, ...)` and call
  `advisor_recommend(symbol=entry.symbol, ..., perception_frame=frame)`. The cron's direction-bias screen stays a
  pure post-processing wrapper (it no longer needs to inject). **M17 closes: there is one producer, reached by
  both the tool and the cron.** (`advisor_recommend` test-override callers that don't accept `perception_frame`
  are tolerated — pass it only when the override is the real `recommend`; or always pass it and let the kwarg
  default-None contract absorb it. Pin in T5.)

### Step 4 — `recommend_multi_horizon` is OUT of PDR-1 scope (recon §4.4).
It keeps its current per-horizon ctx build (`advisor.py:446-628`) and never injected semantic anyway. A later,
optional opt-in. PDR-1 only needs the single-horizon `recommend` path the 3 crons use.

**Rails preserved (restate):** analyst Protocol / BMA / `DefaultRiskGate` untouched (frame projects INTO
`MarketContext`); discrete ladder untouched; the frame is a container, never consulted by the gate; **M06
ADMIT-before-GATE ordering unchanged** (Steps 5–8 identical on both branches); money still CLI/HITL only; `asof`
honesty kept (`frame.asof` = bar-asof for replay, `decision_asof` in `frame.extras` for the live packet cutoff).
With no frame passed → byte-identical.

---

## 5. The eval gate — byte-identical replay + no-lookahead green (producing-path extension)

PDR-1's promotion gate (ADR-0079 Rollout PDR-1; design §6.2) is **two assertions**. Both are deterministic,
fixture-only, no-network (AGENTS.md testing discipline). PDR-1 adds **zero** new statistical surface, so
"byte-identical" is the whole bar.

### 5.1 Byte-identical replay on all fixtures — `tests/perception/test_frame_replay.py` (T3)

The ADR-0079 worked example (`ADR-0079:209-215`): `assert r_today == r_frame`. For each fixture, assert

```python
r_today = recommend(symbol, asset_class=ac, as_of=t, provider=p, include_lessons=False)
frame   = build_perception_frame(symbol, timeframe=tf, asset_class=ac, provider=p,
                                  asof_ts=pd.Timestamp(t, tz="UTC"), lookback_bars=lb)
r_frame = recommend(symbol, asset_class=ac, as_of=t, provider=p, include_lessons=False,
                    perception_frame=frame)
for key in ["as_of", "aggregated_signal", "risk_gate", "decision_price", "analyst_views"]:
    assert r_today[key] == r_frame[key]
```

- Reuse the same load-bearing key-set the existing replay test pins
  (`test_no_lookahead.py:268`: `["as_of","aggregated_signal","risk_gate","decision_price"]`), **plus**
  `analyst_views` (proves the analysts saw an identical `ctx`).
- Fixtures: the synthetic `_make_bars` generators (`test_no_lookahead.py:36`) via `_RecordingProvider`, **plus**
  the integration replay/walk-forward fixtures (`tests/fixtures/bars/*.parquet`, AGENTS.md §Fixture data:
  `AAPL-1d`, `SPY-1h`, `BTC-USDT-1h`).
- **Exclude** the wall-clock-derived fields by construction (don't compare `decision_wall_clock` at
  `advisor.py:1022`, `last_bar_age_minutes`, `signal_id`) — same exclusion the existing replay test makes.
- Cover **both** flag states: `HERMES_QUANT_SEMANTIC_ENABLED` OFF (the default; no packets either way → trivially
  identical) **and** ON with a fixture packet store (proves the absorbed `semantic_market_extras` produces the
  identical `ctx.extras["semantic_packets"]` + `decision_asof` the cron path produced).
- Cover the **degenerate** paths: no-bars and all-still-forming-dropped both yield the identical
  `_gated_no_data` result on both branches (the builder returns `None` ⇒ advisor's `None` branch).

### 5.2 No-lookahead gate green on the producing path — `tests/test_no_lookahead.py` (T4, the S5/M21 extension)

The frame builder is now the producer of `decision_asof` / `semantic_packets`, so the ADR-0074 publication-time
honesty (Invariant 5, `test_no_lookahead.py:350-545`) must pass *through the frame path*. Add a frame-path
analogue of the existing producing-path tests (`:597 test_semantic_market_extras_excludes_future_asof_packet`,
`:638 ..._only_future_packet_injects_nothing`, `:658/:697 catalyst_admissions_*`):

```python
def test_build_perception_frame_excludes_future_asof_packet(monkeypatch, tmp_path):
    # Seed the packet store with one past-asof and one future-asof packet (reuse the M21 fixture seeding).
    frame = build_perception_frame("AAPL", ..., decision_asof=decision)
    asofs = [pd.Timestamp(p["asof"]) for p in frame.semantic_packets]
    assert all(a <= decision for a in asofs), "future-asof packet leaked into the FRAME — producing-path lookahead"

def test_frame_to_context_preserves_decision_asof_cutoff():
    # frame carries decision_asof in extras verbatim; after projection the SemanticAnalyst's `<=` cutoff
    # (semantic.py:161-172) is unchanged → a packet with asof > decision_asof has ZERO influence post-projection.
```

Both reuse the existing producing-path fixture seeding and the `_semantic_ctx` / Invariant-5 helpers. The
SemanticAnalyst's `<=` cutoff is unchanged because the adapter passes `decision_asof` through verbatim
(`frame.extras["decision_asof"]` → `ctx.extras["decision_asof"]`). This is the S5 extension's frame-path plug-in:
`frame.asof` = bar-asof (replay), `frame.extras["decision_asof"]` = the live packet cutoff.

### 5.3 The CI command (matches the ADR-0079 verification shape)

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest \
  tests/perception/ tests/test_no_lookahead.py \
  tests/integration/test_backtest_replay.py tests/integration/test_walk_forward_backtest.py -q
# Expect: green. The replay test (§5.1) is the PDR-1 promotion gate; the no-lookahead gate (§5.2) is release-blocking.
```

---

## 6. Acceptance criteria (Definition of Done)

1. `hermes_quant/perception/{__init__,frame,adapter,builder}.py` exist; `PerceptionFrame` is `@dataclass(frozen=True)`
   with the 11 fields of ADR-0079 §D79.2 in order, all future-score fields defaulting to `None`/empty.
2. `frame_to_context` is pure (no `build_regime_extras` / `semantic_market_extras` calls) and reproduces
   `advisor.py:849-881`'s `ctx.extras` key-set exactly when the future-score fields are `None` (T2 green).
3. `recommend(perception_frame=None)` lands with the `None` branch **byte-identical** to today; both-passed
   precedence is frame-wins-with-caveat (T5 green).
4. The byte-identical-replay proof (T3) is green on the synthetic + parquet fixtures, both flag states, and the
   degenerate (no-bars / all-dropped) paths — on the keys `["as_of","aggregated_signal","risk_gate",
   "decision_price","analyst_views"]`.
5. The no-lookahead gate (existing + the new frame-path producing tests, §5.2) is green — the S5/M21 extension
   covers the producing path.
6. The full existing suite is green with **zero** edits to analyst Protocol / BMA / `DefaultRiskGate` /
   `MarketContext` and **zero** change to the ADMIT-before-GATE ordering (M06) — Steps 5–8 of `recommend` are
   untouched on both branches.
7. The 3 cron call sites (D6/D7/D8) each build one frame via `build_perception_frame` and pass it through
   `perception_frame=`; `semantic_market_extras` is now reached only via the builder. **M17 closes:** the
   `quant_autonomous_tick` **tool** path injects packets with `HERMES_QUANT_SEMANTIC_ENABLED=1` (regression test
   asserts the tool path no longer returns `no_semantic_packets`).
8. `recommend_multi_horizon` is untouched (out of scope).

---

## 7. Risks & mitigations (from ADR-0079 Consequences)

- **Refactor risk — a frame-builder bug silently diverges a live path from the backtest.** Mitigated by the §5.1
  replay proof (the builder's output is asserted byte-identical to the in-`recommend` ctx build) **landed before
  the cron edits** (Step 2 gates Step 3), and by passing already-resolved `provider`/`asof_ts`/`lookback_bars`
  into the builder (§3.3) so the two paths share identical pre-fetch state.
- **Central-contract ossification.** Mitigated by the add-only versioning rule (`protocol.py:14-16` mirror) and
  the `extras` escape hatch; PDR-2/3/4 fill fields, never rename them.
- **M17 over-reach.** Moving injection into `autonomous.tick` must not change the cron's direction-bias screen
  semantics — the screen stays a pure post-processor; T5 pins that a tick with the flag OFF is byte-identical.
- **Provider-resolution divergence.** The builder must NOT re-resolve provider/recipe/as_of — it receives them.
  Pinned in §3.3 and asserted by §5.1.

---

## 8. Out of scope (explicit)

- The three perception producers (`TrendVelocity` PDR-2, `ConvergenceValidator` PDR-3, `SaturationScore` PDR-4) —
  separate eval-gated waves; PDR-1 only lands the empty carrier fields.
- `recommend_multi_horizon` opt-in (later, optional).
- Any change to the discrete ladder, the analyst Protocol, BMA, the gate's authority, the ADMIT-before-GATE
  ordering, or any money/live path.
- `provenance` population (ADR-0033/0041 evidence-store linkage) beyond the empty tuple default.
```
