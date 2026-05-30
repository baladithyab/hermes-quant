# R — Catalyst-driven universe onboarding (ADR-0075) + SemanticAnalyst lookahead-test coverage

**Date:** 2026-05-30
**Scope:** Two linked gaps. (a) Catalyst Sense perceives events on out-of-universe names (LUNR/RKLB/CELH/CROX/LCID/SPR) but the advisor only recommends within its liquidity-screened universe → those packets are dead-on-arrival (ADR-0075, not built; backlog **B05**, P1). (b) The release-blocking no-lookahead CI gate (`tests/test_no_lookahead.py`) exercises only ClassicalTA + MicrostructureLite — NOT the now-LIVE `HermesSemanticAnalyst`, despite "asof = publication time" being the operator's #1 honesty rule.

This is a **research note**, not an implementation. Everything below is design + seam-mapping for a later default-OFF, eval-gated build.

---

## TL;DR

- **Onboarding seam is the watchlist evolver, NOT the advisor.** Universe flows `scan_universe → alpaca-daily.json → evolve_watchlist._read_universe (`hermes_quant/playbook/watchlist_evolution.py:164`, consumed at `:506`) → play-fit.json → autonomous-tick`. The advisor itself never reads the universe file — it recommends whatever symbol it's handed. So a catalyst-admitted symbol must be **injected into the candidate list that feeds `evolve_watchlist` / the autonomous-tick watchlist**, behind `HERMES_QUANT_CATALYST_ONBOARDING=1` gated on `HERMES_QUANT_SEMANTIC_ENABLED=1`.
- **Tradeability gate uses Alpaca `TradingClient.get_asset(symbol)`** → require `.tradable and .fractionable` (and a lower-than-universe but non-zero dollar-volume floor). The scanner already reads exactly these three fields (`hermes_quant/universe/alpaca_scanner.py:98-100, 246-248`); the onboarding gate is a per-symbol version of the same screen. There is **no single-symbol tradable check in the repo yet** (`grep get_asset` = 0 hits) — it must be added.
- **SemanticAnalyst lookahead test ≠ shuffle_timestamps_test.** `shuffle_timestamps_test` operates on a bars DataFrame's `timestamp` column; the SemanticAnalyst consumes **packets**, not bars, and already enforces lookahead via `validate_semantic_packet`'s `future_packet` check (`hermes_quant/semantic.py:135`). The correct invariant test: a packet whose `asof > decision_time` MUST be dropped (analyst abstains `future_packet`), and a packet with `asof <= decision_time` MUST be admitted — asserted via the `analyst.analyze()` abstain-reason, plus a Kronos analogue.
- **Best-practice convergence (external):** two-stage coarse/fine liquidity filter (QuantConnect LEAN), news-events as *primary decision units* with a **hard direction gate** (Janus-Q, arXiv 2602.19919), point-in-time membership to avoid survivorship/look-ahead (Nautilus #3359), and sticky removal protection (`CanRemoveMember`) so an admitted name isn't evicted before its horizon closes. ADR-0075's ≤3 cap + scoped-horizon + `admitted_via` tag matches all four.

---

## 1. The onboarding seam — exactly where a catalyst-admitted symbol enters the watchlist

### 1.1 The data flow (traced, file:line)

```
ops/scripts/quant-universe-scan.py
   └─> hermes_quant/universe/alpaca_scanner.py :: scan_universe()
         filters: tradable=True (:98) AND fractionable=True (:100)
                  AND min_price<=close<=max_price AND advd>=min_avg_dollar_volume_30d (:253-258)
         writes ~/.hermes/quant/universe/alpaca-daily.json  (atomic, :277)

ops/scripts/quant-watchlist-evolve.py
   └─> hermes_quant/playbook/watchlist_evolution.py :: evolve_watchlist()
         _read_universe(universe_path)            (def :164, called :506)  ← UNIVERSE ENTERS HERE
         _evolve_one_play(... universe=universe ) (:248, loop over symbols :279)
         onboard rule: score>=onboard_floor for sticky_onboard_days runs, active_count<max_per_play (:366-389)
         writes ~/.hermes/quant/watchlist/play-fit.json

ops/scripts/quant-autonomous-tick.py
   └─> load_active_watchlist()  (:110)  reads play-fit.json, state=="active" rows
   └─> hermes_quant.autonomous.tick(symbols=entries)  (:287)
         per symbol -> advisor.recommend(..., market_extras={"semantic_packets": load_packets_for(sym, asof)})
```

**Key finding:** `advisor.recommend()` (`hermes_quant/advisor.py:631`) has **no universe filter inside it** — it recommends on whatever symbol it's called with, and injects packets via `market_extras["semantic_packets"]` (`:850`; analyst reads it at `hermes_quant/analysts/semantic.py:148`). The universe screen lives **entirely upstream** in the watchlist evolver's `_read_universe`. Therefore the dead-on-arrival gap is *not* in the advisor; it's that the out-of-universe symbol never makes it into `play-fit.json`, so the autonomous-tick never calls `recommend()` for it.

### 1.2 Recommended injection point

Inject at the **watchlist boundary**, two viable seams (prefer the first):

- **Seam A (preferred) — augment the universe list fed to `evolve_watchlist`.** Add a thin `catalyst_admissions()` helper that:
  1. reads the catalyst packet store and runs `coverage_against_universe()` (`hermes_quant/catalyst/propagation.py:277`) to get `dead_on_arrival`;
  2. for each dead symbol, loads its freshest packet (`load_packets_for`, `synthesize.py:158`) and keeps it iff `confidence >= τ_conf AND magnitude >= τ_mag` (ADR-0075 starts 0.6 / 0.04);
  3. runs the **tradeability gate** (§1.3) on survivors;
  4. caps to ≤3 (ADR-0075), tags `admitted_via=catalyst`.
  The survivors are unioned into the `universe` list passed to `evolve_watchlist`, and given a `sticky_onboard_days=0`/fast-track so a strong catalyst is actionable *that day* (the normal 3-day sticky onboard defeats the purpose for a 1-day catalyst horizon). This is the minimal-blast-radius seam: it reuses the entire existing onboard/evict/journal machinery and the `extras` dict on `WatchlistEntry` (`watchlist_evolution.py:111`) carries `admitted_via` / horizon to disk verbatim.

- **Seam B (alternative) — a parallel admitted-list in autonomous-tick.** `load_active_watchlist()` (`quant-autonomous-tick.py:110`) unions a `catalyst_admissions()` list onto the play-fit rows. Simpler to gate but bypasses the journal audit trail and the eviction/horizon-expiry machinery, so it's the worse choice for money-software (less replayable).

**Default-OFF:** the helper returns `[]` unless `HERMES_QUANT_CATALYST_ONBOARDING=1` AND `HERMES_QUANT_SEMANTIC_ENABLED=1` (onboarding without semantic is meaningless — ADR-0075 §5). Gate read at call time so a cron flip / test takes effect immediately (matches the existing pattern at `advisor.py:377`).

### 1.3 The tradeability gate (the Alpaca `tradable` check)

The scanner reads three fields per asset and the onboarding gate needs the **per-symbol** version:

```python
# hermes_quant/universe/alpaca_scanner.py already does (batch, :98-100, :246-248):
a.tradable        # Alpaca will accept orders
a.fractionable    # Kelly-sized {0.05..0.20}*NAV on a high-price name is sub-share
a.shortable       # needed for the 22-of-25-SHORT signal reality (B04/B01)
```

There is currently **no single-symbol lookup** (`grep -rn "get_asset" hermes_quant/` = 0 hits). The gate must add one:

```python
asset = trading_client.get_asset(symbol)   # alpaca.trading TradingClient, paper=True
ok = (asset.tradable and asset.fractionable
      and _last_dollar_volume(symbol) >= ONBOARD_ADV_FLOOR)   # floor < universe screen, > 0
```

Rationale (ADR-0075 §2): admission MUST NOT create an unfillable order. A name with no borrow / no liquidity is rejected even if the catalyst is strong. Reuse the scanner's `_build_trading_client` (`alpaca_scanner.py:80`, paper-only, READ-ONLY) and IEX-feed dollar-volume estimate (`_avg_dollar_volume`, `:152`). Cache the per-symbol asset lookup (≤3 names/day) to avoid hammering the API.

### 1.4 Attribution + caps (reuse existing machinery)

- `admitted_via=catalyst` rides on `WatchlistEntry.extras` (`watchlist_evolution.py:111`) → play-fit.json → autonomous-tick audit row → executions.jsonl. This dovetails with **B13** (`play_tag` plumbing) and lets the weekly retro/calibrator bucket catalyst-onboarded trades distinctly (ADR-0075 verification §3).
- Hard cap ≤3 simultaneous catalyst-admitted names + tighter per-name size cap than universe names — reuse **ADR-0071 portfolio-caps** machinery (already shipped, `HERMES_QUANT_PORTFOLIO_CAPS=1`, B12).
- Scoped horizon: admit for the packet horizon only (1 trading day default). Eviction at horizon-close is the existing slow/fast-evict path; an admitted name that loses its catalyst falls back below `evict_floor` next run. **Sticky-removal caution** (Nautilus #3359 / LEAN `CanRemoveMember`): do NOT evict an admitted name with an open position mid-horizon — let the position close first.

---

## 2. SemanticAnalyst lookahead test — design

### 2.1 Why `shuffle_timestamps_test` does not apply as-is

`shuffle_timestamps_test(score_fn, bars, ...)` (`hermes_quant/evaluation/lookahead.py:75`) **requires a `timestamp` column on a bars DataFrame** (`:115` raises `ValueError` otherwise) and shuffles that column to check the analyst relies on temporal *ordering of bars*. The `HermesSemanticAnalyst` consumes **packets** from `ctx.extras["semantic_packets"]`, not bars — it does not read the OHLCV frame at all for its signal. Shuffling bar timestamps would be a no-op on its output. So the bar-shuffle test is **structurally inapplicable**; the semantic analyst needs a *packet-time* lookahead test.

### 2.2 The real invariant for a packet-driven analyst

The honesty rule is `asof = publication time`, enforced today inside `validate_semantic_packet` (`hermes_quant/semantic.py:135`): `if packet_asof > ctx_asof: return False, "future_packet"`. The analyst's `_select_packet` (`analysts/semantic.py:147-195`) validates against `decision_asof` (wall-clock decision time) when supplied, else `ctx.asof` (backtest bar boundary). **The lookahead invariant to test:**

> A packet with `asof > decision_time` MUST be dropped (analyst abstains `future_packet`); a packet with `asof <= decision_time` MUST be admitted. The analyst's view at decision time T must be *identical* whether or not future-asof packets are present in `ctx.extras`.

This is the packet-domain analogue of Invariant 1 in `test_no_lookahead.py:81` ("analyst view at T independent of future bars") — here "future bars" becomes "future-asof packets."

### 2.3 Concrete test design (drop-in for `tests/test_no_lookahead.py`)

Add a third parametrization branch + a packet-specific test. Sketch:

```python
from hermes_quant.analysts.semantic import HermesSemanticAnalyst
from hermes_quant.semantic import semantic_packet_from_dict

def _pkt(asof, stance="bullish", conf=0.7):
    return semantic_packet_from_dict({
        "schema_version": 1, "asset": "TEST", "asof": asof, "horizon": "1d",
        "stance": stance, "confidence": conf, "magnitude": 0.04,
        "summary": "catalyst thesis", "model": "test",
    }).to_dict()

def _sem_ctx(decision_asof, packets):
    return MarketContext(
        asset="TEST", timeframe="1d", asset_class="equity", exchange=None,
        bars=_make_bars(30), last_close=100.0, last_volume=1e6,
        asof=pd.Timestamp(decision_asof),
        extras={"semantic_packets": packets,
                "decision_asof": decision_asof},
    )

def test_semantic_drops_future_asof_packet():
    """asof > decision_time MUST be dropped (future_packet); the view at T is
    identical whether or not a future-asof packet is also present — the
    packet-domain no-lookahead invariant."""
    a = HermesSemanticAnalyst()
    T = "2026-03-15T00:00:00Z"
    past   = _pkt("2026-03-14T18:00:00Z")          # publishable at T
    future = _pkt("2026-03-16T09:00:00Z", "bearish")  # asof AFTER T -> illegal

    v_clean   = a.analyze(_sem_ctx(T, [past]))
    v_polluted = a.analyze(_sem_ctx(T, [past, future]))
    # future packet leaked in must NOT change the decision
    assert v_clean.direction == v_polluted.direction == 1
    assert v_clean.confidence_raw == pytest.approx(v_polluted.confidence_raw)

    # a context with ONLY the future packet must abstain future_packet
    v_only_future = a.analyze(_sem_ctx(T, [future]))
    assert v_only_future.direction == 0
    assert v_only_future.metadata["abstain_reason"] == "future_packet"

def test_semantic_admits_at_boundary():
    """asof == decision_time is admissible (publication AT the boundary is honest)."""
    a = HermesSemanticAnalyst()
    T = "2026-03-15T00:00:00Z"
    v = a.analyze(_sem_ctx(T, [_pkt(T)]))
    assert v.direction == 1 and v.metadata.get("abstain_reason") is None
```

**Why this is the right shape:** it reuses the *existing* validation path (no new lookahead logic to drift), it tests the silence-by-default branch (`future_packet` → abstain), and the "polluted-but-present" assertion mirrors the codebase's established `polluted_but_sliced` pattern (`test_no_lookahead.py:105`). It is fully deterministic (explicit timestamps, no network), satisfying the unit-test discipline in AGENTS.md.

### 2.4 Also close the Kronos gap (same file, same release-blocker)

The brief and AGENTS.md item #9 ("Skip the shuffle-timestamp test on a new analyst") imply *every shipped analyst* belongs in the gate. Kronos/Kairos (`hermes_quant/analysts/kronos.py`) is bar-driven, so it CAN use `shuffle_timestamps_test` directly — add it to the `analyst_factory` parametrize lists at `test_no_lookahead.py:75` and `:282`. Two distinct test types result:

| Analyst | Input | Lookahead test |
|---|---|---|
| ClassicalTA, MicrostructureLite, **Kronos/Kairos** | bars | `shuffle_timestamps_test` (existing) + Invariant-1 future-bar slice |
| **HermesSemanticAnalyst** | packets | packet `asof > decision_time` drop test (§2.3) |

### 2.5 A note on the `decision_asof` subtlety (do not regress)

`_select_packet` (`analysts/semantic.py:164-172`) deliberately validates against `ctx.extras["decision_asof"]` (wall-clock now) when present, falling back to `ctx.asof` (last daily-bar close) otherwise. This is the ADR-0068/0074 fix: in a *live* run the last bar is yesterday's close, so a packet published *today* is legitimately available at decision time and must NOT be rejected as `future_packet`. The test in §2.3 supplies `decision_asof` explicitly so it exercises the live-path semantics. A **backtest** (no `decision_asof`) falls back to `ctx.asof`, where bar-time IS the decision boundary and the strict no-lookahead clamp holds — the test should also cover the no-`decision_asof` case to pin that backtest honesty.

---

## 3. Best practices — event-driven universe expansion (external, brief)

| System | Pattern | Relevance to ADR-0075 |
|---|---|---|
| **QuantConnect LEAN** (docs/v2 universes) | Two-stage **coarse (price/volume) → fine (fundamental)** filter; `OnSecuritiesChanged` event on add/remove; `ActiveSecurities`; `CanRemoveMember` blocks eviction before the algo has had time to act; news/sentiment universes (Brain, Quiver) as first-class data-driven universes. | Validates the liquidity-coarse-screen + catalyst-fine-admit split. `CanRemoveMember` → the sticky-removal rule in §1.4 (don't evict an admitted name with an open position). |
| **Janus-Q** (arXiv 2602.19919, event-driven trading) | News **events as PRIMARY decision units** (not auxiliary features); a **hard direction gate** `g_dir∈{0,1}` zeroes reward under wrong polarity; event-type soft gate discounts wrong category; entry at next open, exit within 2 days (scoped horizon). | Directly mirrors ADR-0074's sign-consistency eval axis + ADR-0075's scoped-horizon. The hard direction gate is the academic analog of "wrong edge-sign now has a direct trade consequence on a volatile small-cap" — raising the bar on the sign gate before flipping the flag. |
| **NautilusTrader #3359** (dynamic universe in backtest) | Pre-loading the super-set is memory-prohibitive & invites survivorship/look-ahead bias; preferred = **point-in-time** `Date -> [InstrumentId]` map, register instruments on rebalance event. | The `admitted_via=catalyst` + per-day admission record IS a point-in-time membership log → replayable backtests without survivorship bias (AGENTS.md reproducibility principle). |

**Convergent lesson:** every mature system gates event-driven additions behind (a) a hard tradeability/liquidity check, (b) a hard direction gate, and (c) point-in-time membership for replayability. ADR-0075's tradeability gate + ≤3 cap + `admitted_via` tag + default-OFF flag already encode all three; the missing pieces are the single-symbol Alpaca lookup (§1.3) and the sign-eval axis on the admitted path (ADR-0075 verification §1).

---

## 4. Open questions / risks for the build (not this note)

1. **Cold-start miscalibration** (ADR-0075 §Consequences): the calibrator has never seen the catalyst-onboarded path. The `admitted_via` bucket must be retro-attributed separately before the haircut is relaxed.
2. **Wider slippage band** for low-liquidity admitted names — ADR-0070 slippage model (B12) must apply, likely with a wider band keyed on `admitted_via=catalyst`.
3. **Fast-track vs sticky onboard tension** — a 1-day catalyst horizon is incompatible with the default `sticky_onboard_days=3`; the admission path needs `sticky_onboard_days=0` while normal universe names keep the sticky default. This is the one place the onboarding path must diverge from `evolve_watchlist`'s defaults.
4. **Eval gate before flag-flip** — per ADR-0075 verification: a labeled out-of-universe case (the real LUNR Blue-Origin move) must produce admission + correct direction + a fillable simulated order. The flag stays OFF until that axis passes.
