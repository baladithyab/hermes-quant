# Wave C2 — Catalyst follow-ups (implementation-ready plan)

**Date:** 2026-05-30
**Status:** ready-to-execute
**Grounded in:** `docs/research/2026-05-30-r-catalyst-onboarding.md`, backlog B05–B10, ADR-0074/0075/0076/0077.
**Posture:** money-software. Every new capability ships DEFAULT-OFF behind a `HERMES_QUANT_*` flag, eval-gated before any live influence. Silence-by-default everywhere. `asof = publication/decision time` is the load-bearing honesty rule. Discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}` is never widened. The deterministic risk gate (ADR-0004) is FINAL authority; catalyst onboarding is admissibility/perception — it can only ADMIT a name to the candidate set and can never force, amplify, or override a gate decision.

This plan covers five items. Items are independently shippable; they have a partial dependency chain noted per-item. **Recommended PR order: C2-2 (wiring audit, P0-ish correctness) → C2-3 (test hardening) → C2-1 (profitability cron) → C2-4 (ADR-0075 onboarding) → C2-5 (B10 design doc).**

---

## State-of-the-world (verified against HEAD, file:line)

| Fact | Evidence |
|---|---|
| Packets reach **exactly one** of three live decision paths. | `load_packets_for` + `market_extras={"semantic_packets":…}` wired ONLY in `ops/scripts/quant-daily-interim.py:127-141`. `quant-autonomous-tick.py` (`auto.tick` via `_direction_screened_recommend`, :306-341) and `quant-playbook-tick.py` (`_recommend`, :465) call `recommend()` with NO `market_extras`. Confirmed by grep: `semantic_packets` populated in `market_extras` appears only in `quant-daily-interim.py` + `catalyst/synthesize.py` docstring. |
| `HERMES_QUANT_SEMANTIC_ENABLED` only adds the analyst to the loadout (`advisor.py:377-383`); it does NOT populate `ctx.extras["semantic_packets"]`. Flag and wiring are **decoupled** (gap G3). | When the flag is on but the path doesn't inject packets, `HermesSemanticAnalyst.analyze` abstains `no_semantic_packets` (`analysts/semantic.py:156`). |
| The SemanticAnalyst no-lookahead test **already exists**. | `tests/test_no_lookahead.py:428` `test_semantic_analyst_future_packet_has_zero_influence`, `:466` `test_semantic_analyst_future_packet_under_decision_asof_extra`, plus `:517` Kronos. Item C2-3 is therefore a *hardening/closing* task, not greenfield. |
| `profitability.py` + `quant-catalyst-profitability.py` exist and are correct; they are NOT wired into a firing cron (only the existing catalyst crons are scheduled). | `hermes_quant/catalyst/profitability.py` (`MIN_SAMPLE=20`, `MIN_HIT_RATE=0.6`); `ops/scripts/quant-catalyst-profitability.py` (silence-by-default `main()`). |
| The single-symbol Alpaca tradeability check the research note said must be "added" is now being built by **ADR-0077** (`hermes_quant/admissibility/shortability.py`, `AlpacaShortabilityOracle.verdict` → `TradingClient.get_asset(symbol)`, behind `HERMES_QUANT_ADMISSIBILITY=1`). | `docs/adr/ADR-0077-pretrade-admissibility-shortability.md:88-116`. The catalyst onboarding gate REUSES this oracle, not a duplicate `get_asset`. |
| Onboarding seam is the watchlist boundary, NOT the advisor. | `coverage_against_universe` (`catalyst/propagation.py:277`); `_read_universe`/`evolve_watchlist` (`playbook/watchlist_evolution.py:164,458,506`); `load_active_watchlist` (`quant-autonomous-tick.py:110`). The advisor has no universe filter. |
| Two distinct `WatchlistEntry` dataclasses exist. | `hermes_quant/watchlist.py:48` (frozen, `symbol/asset_class/timeframe`) used by autonomous-tick; `hermes_quant/playbook/watchlist_evolution.py:88` (frozen, `symbol/play/…/extras`) used by the evolver. The `extras` dict on the latter carries `admitted_via` to disk verbatim. |
| Learned-graph corpus accumulates but no consumer exists (B10). | `log_propagations` (`catalyst/propagation.py:191`) writes `propagation-log.jsonl` with `{symbol, source, relation, effect_sign, weight, symbol_sign, catalyst_sign, asof}`; nothing reads it for sign-correction. |

---

## C2-2 — Catalyst-wiring audit: wire packets into all relevant decision paths (gap G3)

**Backlog:** part of B05 enablement; the decoupling bug the codebase agent flagged.
**Priority:** P1 (correctness — a flipped flag silently does nothing on two of three paths).
**Depends-on:** none. **Do this first** — it is the prerequisite for catalyst influence anywhere besides the interim brief, and C2-4's onboarding is pointless if the path that trades onboarded names doesn't inject packets.

### The three live decision paths (grep-confirmed)

1. **`quant-daily-interim.py`** — the catalyst-aware brief. **Already wired** (`:127-141`). This is the canonical pattern to copy.
2. **`quant-autonomous-tick.py`** — `auto.tick(symbols, advisor_recommend=_direction_screened_recommend)` (`:337-341`). The wrapper `_direction_screened_recommend(**kwargs)` (`:308`) calls `_base_recommend(**kwargs)` with **no `market_extras`**. → packets never reach the analyst → abstains `no_semantic_packets`.
3. **`quant-playbook-tick.py`** — `_recommend(symbol, asset_class="equity", timeframe=…)` → `_recommend()` calls `advisor.recommend(symbol, asset_class="equity", timeframe=primary_timeframe)` (`:465`) with **no `market_extras`**. Same gap.

### The fix: a shared packet-loader helper, called by all three paths

Do NOT copy-paste the `try/except` block from `quant-daily-interim.py` into two more scripts. Extract the canonical loader into the library so there is **one** lookahead-honest packet-injection seam (mirrors the "ONLY coupling point to the advisor" comment at `synthesize.py:176`).

**New function** — `hermes_quant/catalyst/wiring.py` (new module, ~40 LoC):

```python
# hermes_quant/catalyst/wiring.py
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any

def semantic_market_extras(
    symbol: str,
    *,
    decision_asof: datetime | None = None,
    horizon: str = "1d",
    base_extras: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return market_extras carrying lookahead-honest semantic packets for `symbol`,
    or None when semantic is OFF / no packets / any error (silence-by-default).

    This is the SINGLE catalyst→advisor wiring seam. All live decision paths
    (daily-interim, autonomous-tick, playbook-tick) call this so flipping
    HERMES_QUANT_SEMANTIC_ENABLED=1 takes effect on EVERY path, not just one.

    decision_asof defaults to wall-clock now (live path): packets validate against
    decision time, not the stale last-daily-bar close (ADR-0068/0074). Pass an
    explicit asof for backtests so the strict bar-time clamp holds.
    """
    if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") != "1":
        return None
    try:
        from hermes_quant.catalyst.synthesize import load_packets_for
        asof = decision_asof or datetime.now(timezone.utc)
        packets = load_packets_for(symbol, asof, horizon=horizon)
        if not packets:
            return None
        out = dict(base_extras or {})
        out["semantic_packets"] = packets
        out["decision_asof"] = asof.isoformat()
        return out
    except Exception:  # noqa: BLE001 — never block a recommend on packet loading
        return None
```

**Call-site edits (all three paths):**

- `ops/scripts/quant-daily-interim.py:122-141` — **replace** the inline block with `market_extras = semantic_market_extras(symbol, horizon=timeframe)` then `recommend(..., market_extras=market_extras)` (passing `market_extras=None` is already a no-op in `recommend`, `advisor.py:567/850`, so the `if market_extras is not None` branch collapses to one call). Behavior is byte-identical; this proves the helper reproduces the existing canonical path.
- `ops/scripts/quant-autonomous-tick.py` — inside `_direction_screened_recommend` (`:308`), before `_base_recommend(**kwargs)`:
  ```python
  sym = kwargs.get("symbol")
  if sym and "market_extras" not in kwargs:
      from hermes_quant.catalyst.wiring import semantic_market_extras
      me = semantic_market_extras(sym, horizon=kwargs.get("timeframe", "1d"))
      if me is not None:
          kwargs = {**kwargs, "market_extras": me}
  res = _base_recommend(**kwargs)
  ```
  (Lazy import keeps the module import surface unchanged; the existing direction-bias screen logic is untouched and runs on `res` exactly as before.)
- `ops/scripts/quant-playbook-tick.py:465` — before the `_recommend(...)` call, build extras and pass them: `me = semantic_market_extras(symbol, horizon=primary_timeframe); result = _recommend(symbol, asset_class="equity", timeframe=primary_timeframe, market_extras=me)`. Confirm `advisor.recommend` accepts `market_extras=None` (it does, `:454`). The `_mock_recommend` path (`:443`) is unaffected (it never loads packets, by design for the unit-test stub).

### Tests (`tests/unit/test_catalyst_wiring.py`, new)

- `test_semantic_market_extras_off_returns_none` — flag unset → `None` (monkeypatch `os.environ`).
- `test_semantic_market_extras_no_packets_returns_none` — flag on, empty store (`tmp_path`) → `None`.
- `test_semantic_market_extras_loads_and_stamps_decision_asof` — flag on, a written packet in `tmp_path` store (monkeypatch `synthesize._DEFAULT_STORE`) → dict has `semantic_packets` non-empty AND `decision_asof` ISO string; `base_extras` keys preserved.
- `test_semantic_market_extras_never_raises` — monkeypatch `load_packets_for` to raise → returns `None`, no exception.
- `test_all_three_paths_inject_packets` (integration-lite, `tests/unit/`): monkeypatch `advisor.recommend` to a spy that records `market_extras`, run each path's recommend-shim with the flag on and a stubbed store, assert `semantic_packets` present in the captured kwargs for autonomous-tick and playbook-tick (the regression test that pins G3 closed). Use the existing `HERMES_QUANT_PLAYBOOK_TICK_MOCK` knobs where they help isolate.

### Acceptance

- Grep proves all three paths route packets through one helper; no duplicated `try/except load_packets_for` blocks remain.
- With `HERMES_QUANT_SEMANTIC_ENABLED=1` + a packet in the store, the autonomous tick and playbook tick produce a `semantic` analyst view (not `no_semantic_packets`), verified by the spy test.
- With the flag OFF, all three paths produce identical output to today (the helper returns `None`, `recommend(market_extras=None)` is the existing no-op).

---

## C2-3 — SemanticAnalyst no-lookahead test (close the gap fully)

**Backlog:** the ADR-0075-research §2 ask; gap G2.
**Priority:** P1 (release-blocking gate completeness).
**Depends-on:** none.

### Finding: most of this is already done

`tests/test_no_lookahead.py` already contains:
- `test_semantic_analyst_future_packet_has_zero_influence` (`:428`) — future-asof packet dropped; view byte-identical to future-absent case; surviving view reflects the past packet.
- `test_semantic_analyst_future_packet_under_decision_asof_extra` (`:466`) — the live `decision_asof` branch (the ADR-0068/0074 subtlety the research note §2.5 flagged "do not regress").
- `test_kronos_view_at_t_independent_of_future_bars` (`:517`) — Kronos bar-domain invariant (importorskip-guarded).

This already satisfies the research note's core §2.3 design and §2.4 Kronos ask. **Two residual gaps remain to close:**

1. **No explicit `future_packet` abstain-reason assertion.** The existing tests prove *zero influence by differencing*, but never assert the silence-by-default branch directly — i.e. that a context with ONLY a future packet abstains with `metadata["abstain_reason"] == "future_packet"` (the research note §2.3 `test_semantic_drops_future_asof_packet` final assertion). Add it. This pins the abstain *reason string* that downstream observability keys on (`analysts/semantic.py:198,207`).
2. **No boundary-equality case.** A packet with `asof == decision_time` MUST be admitted (publication AT the boundary is honest — research §2.3 `test_semantic_admits_at_boundary`). Add it.

### Edits (`tests/test_no_lookahead.py`, append two tests in the Invariant-5 block ~line 509)

```python
def test_semantic_analyst_only_future_packet_abstains_future_packet():
    """A context whose ONLY packet is published after the decision boundary
    must abstain with the explicit future_packet reason (silence-by-default,
    not a silent zero-influence) — pins the abstain-reason observability keys on."""
    decision = "2026-01-01T12:00:00Z"
    future = _semantic_packet(
        asof="2026-01-01T13:00:00Z", stance="bearish", confidence=0.95, magnitude=0.05
    )
    view = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [future]})
    )
    assert view is not None
    assert view.direction == 0
    assert view.metadata.get("abstain_reason") == "future_packet"


def test_semantic_analyst_admits_packet_at_decision_boundary():
    """asof == decision_time is admissible: publication exactly at the boundary
    is lookahead-honest (<=, not <). Backtest path (no decision_asof => ctx.asof)."""
    decision = "2026-01-01T12:00:00Z"
    at_boundary = _semantic_packet(
        asof=decision, stance="bullish", confidence=0.70, magnitude=0.01
    )
    view = HermesSemanticAnalyst().analyze(
        _semantic_ctx(asof=decision, extras={"semantic_packets": [at_boundary]})
    )
    assert view is not None and view.direction == 1
    assert view.metadata.get("abstain_reason") is None
```

(Reuses the existing `_semantic_packet`/`_semantic_ctx`/`HermesSemanticAnalyst` helpers already in the file — no new fixtures.)

### Acceptance

- `pytest tests/test_no_lookahead.py -q` green, including the two new cases.
- The `future_packet` abstain-reason and the `<=`-boundary admission are now both asserted directly (not only by differencing).
- Verify the existing two semantic tests still pass unchanged (no regression to the `decision_asof` live branch).

---

## C2-1 — B06 profitability cron (wrapper exists; design the schedule + no_agent contract)

**Backlog:** B06.
**Priority:** P1.
**Depends-on:** none (`quant-catalyst-profitability.py` + `profitability.py` already exist and are correct).

### What's already done vs what this item delivers

- DONE: `hermes_quant/catalyst/profitability.py` (`measure_profitability`, `format_report`, `RelationStats.verdict` with `INSUFFICIENT_SAMPLE`/`PROFITABLE`/`UNPROFITABLE_CONSIDER_PRUNE`/`MARGINAL_HOLD`, `MIN_SAMPLE=20`, `MIN_HIT_RATE=0.6`).
- DONE: `ops/scripts/quant-catalyst-profitability.py` (venv re-exec, `_yf_forward_return`, `main()` silence-by-default — returns 0 with empty stdout when `not stats`).
- **THIS ITEM:** (a) make the script a proper change-detecting **no_agent watchdog** that is silent until a relation class *clears `MIN_SAMPLE`* (mirrors the coverage probe pattern from commit `e4ecad5`), and (b) write the schedule design + the agent-cron registration line.

### (a) Make `main()` a clearance-detecting watchdog

Today's `main()` prints the full report whenever `stats` is non-empty — that fires every run once any data exists, training the operator to skim (the exact anti-pattern fixed for the coverage probe). Change it to **emit only on a state transition**: a relation class crossing `n_scored >= MIN_SAMPLE` for the first time (it just became trustworthy), or a class flipping verdict (`PROFITABLE`↔`UNPROFITABLE_CONSIDER_PRUNE`↔`MARGINAL_HOLD`). Persist a baseline like the coverage probe.

**Edits to `ops/scripts/quant-catalyst-profitability.py`:**

```python
_BASELINE = Path.home() / ".hermes" / "quant" / "catalyst" / "profitability-baseline.json"

def _load_baseline() -> dict[str, dict]:
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def _save_baseline(state: dict[str, dict]) -> None:
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(state, sort_keys=True))
    except OSError:
        pass

def main() -> int:
    verbose = "--verbose" in sys.argv
    stats = measure_profitability(_yf_forward_return, max_rows=120)
    if not stats:
        return 0  # silence-by-default: no scored data yet

    cur = {r: {"cleared": s.n_scored >= MIN_SAMPLE, "verdict": s.verdict}
           for r, s in stats.items()}
    baseline = _load_baseline()
    transitions = []
    for r, c in cur.items():
        b = baseline.get(r)
        if b is None and c["cleared"]:
            transitions.append(f"{r} CLEARED MIN_SAMPLE ({c['verdict']})")
        elif b is not None:
            if c["cleared"] and not b.get("cleared"):
                transitions.append(f"{r} CLEARED MIN_SAMPLE ({c['verdict']})")
            elif c["cleared"] and c["verdict"] != b.get("verdict"):
                transitions.append(f"{r} verdict {b.get('verdict')} -> {c['verdict']}")
    _save_baseline(cur)

    if verbose:
        print("📊 " + format_report(stats))
        return 0
    if not transitions:
        return 0  # standing state, unchanged -> silent (no_agent contract)
    print("📊 catalyst-profitability: " + "; ".join(transitions))
    print(format_report(stats))  # full table only when something changed
    return 0
```

**Why this is the right shape:** it directly serves B06's purpose ("silent until a relation class clears `MIN_SAMPLE`") and B07's gate (raising `CONSUMER_TREND_CONFIDENCE_HAIRCUT` only after `brand_self` clears its bar). `--verbose` always shows the full picture for on-demand pulls. Import `MIN_SAMPLE` from `hermes_quant.catalyst.profitability` (already exported).

### (b) Schedule design

The Hermes-agent cron host runs in PT and registers jobs as `deliver=origin` no_agent watchdogs (per commit `e4ecad5`: "Scheduled as cron quant-catalyst-coverage-daily (03:45 PT weekdays…), deliver=origin"). The profitability loop needs a *forward-return* window to have elapsed (`_FWD_WINDOW_DAYS=21`), so daily is wasteful — **weekly** is correct and matches B06's stated "weekly no_agent."

| Field | Value | Rationale |
|---|---|---|
| Job name | `quant-catalyst-profitability-weekly` | mirrors `quant-catalyst-coverage-daily` naming |
| Schedule (cron, PT) | `0 5 * * 6` (Sat 05:00 PT) | weekend, off-market; after Friday's forward returns settle; no contention with the daily universe-scan/coverage chain |
| Command | `~/.hermes/hermes-agent/venv/bin/python3 <repo>/ops/scripts/quant-catalyst-profitability.py` | script self-re-execs the venv, so a bare `python3` also works |
| deliver | `origin` (no_agent) | silence-by-default; empty stdout → no message |
| Output contract | emits ONLY on a `MIN_SAMPLE` clearance or verdict flip | watchdog, not a digest |

**Registration** is done in the Hermes-agent cron config (NOT in this repo — same as the existing catalyst crons; this repo only ships the script). Document the exact registration line in the PR body so the operator can add it: the agent-cron `add` invocation with `name=quant-catalyst-profitability-weekly schedule="0 5 * * 6" deliver=origin`.

### Tests (`tests/unit/test_catalyst_profitability_cron.py`, new — script-level)

The ops script re-execs the venv at import, so test the *logic* by importing the module functions guardedly, OR (preferred, matches repo convention for ops scripts) test the watchdog transition logic by factoring `_load_baseline`/`_save_baseline`/transition-diff into pure helpers and unit-testing those with `tmp_path`:
- `test_profitability_silent_when_no_stats` — `measure_profitability` returns `{}` (empty/missing log) → `main()` exits 0, no print (capsys empty).
- `test_profitability_silent_when_unchanged` — baseline == current → no transitions → silent.
- `test_profitability_emits_on_min_sample_clearance` — baseline has `brand_self` uncleared, current has it cleared → one transition line printed.
- `test_profitability_emits_on_verdict_flip` — cleared class flips `MARGINAL_HOLD`→`PROFITABLE` → transition printed.
- Reuse `RelationStats` fixtures from the existing `tests/unit/test_catalyst_integration.py` style; inject a fake `measure_profitability` via monkeypatch so no network.

### Acceptance

- First run with ≥`MIN_SAMPLE` scored `brand_self` rows: prints one clearance line + table. Re-run unchanged: silent. Verdict flip: one line. `--verbose`: always full table.
- No network in unit tests (fetcher injected/monkeypatched).
- Schedule + registration line documented in the PR body; nothing scheduled inside the repo.

---

## C2-4 — ADR-0075 catalyst-driven universe onboarding (default-OFF build)

**Backlog:** B05 (P1, the perceive-but-can't-act gap).
**Priority:** P1.
**Depends-on:** **C2-2** (the path that trades onboarded names must inject packets) and **ADR-0077** (`hermes_quant/admissibility/shortability.py` provides the single-symbol `get_asset` tradeability check — reuse it, do NOT duplicate `get_asset`). If ADR-0077 has not landed when this starts, the tradeability gate ships behind its own sub-flag returning REJECT (fail-closed) so onboarding stays inert until the oracle exists.

### Design summary (from research §1, ADR-0075 Decision)

Inject at the **watchlist boundary** (Seam A, preferred), behind `HERMES_QUANT_CATALYST_ONBOARDING=1` gated on `HERMES_QUANT_SEMANTIC_ENABLED=1`. A new helper computes ≤3 catalyst admissions and unions them into the universe fed to `evolve_watchlist`, with `admitted_via=catalyst` on the `WatchlistEntry.extras` and a fast-track `sticky_onboard_days=0` so a 1-day catalyst is actionable that day. The advisor and risk gate are untouched.

### New module — `hermes_quant/catalyst/onboarding.py` (~120 LoC)

```python
# hermes_quant/catalyst/onboarding.py
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

# ADR-0075 thresholds (conservative start; tune via eval-gate before flag-flip)
TAU_CONF = 0.60            # packet confidence floor for admission
TAU_MAG = 0.04            # packet magnitude floor for admission
MAX_ADMISSIONS = 3        # hard cap on simultaneous catalyst-admitted names
ONBOARD_ADV_FLOOR = 1_000_000.0   # dollar-volume floor < universe screen (5M), > 0

@dataclass(frozen=True)
class CatalystAdmission:
    symbol: str
    stance: str            # bullish | bearish
    direction: int         # +1 | -1   (derived from stance)
    confidence: float
    magnitude: float
    horizon: str
    packet_asof: str
    admitted_via: str = "catalyst"

# tradeable(symbol) -> bool : injected so this module is offline/testable.
# Production wires hermes_quant.admissibility.shortability.AlpacaShortabilityOracle
# (ADR-0077) for the get_asset(symbol).tradable/fractionable check + a dollar-volume floor.
TradeabilityCheck = Callable[[str], bool]

def catalyst_admissions(
    universe_symbols: set[str],
    *,
    tradeable: TradeabilityCheck,
    asof: datetime | None = None,
    tau_conf: float = TAU_CONF,
    tau_mag: float = TAU_MAG,
    max_admissions: int = MAX_ADMISSIONS,
) -> list[CatalystAdmission]:
    """Return ≤max_admissions out-of-universe symbols with a fresh, strong catalyst
    packet that pass the tradeability gate. [] unless BOTH flags are on
    (silence-by-default; never raises).

    Flow (research §1.2 Seam A):
      1. coverage_against_universe(universe) -> dead_on_arrival symbols
      2. load_packets_for(sym, asof) -> freshest packet; keep iff conf>=tau_conf AND mag>=tau_mag
      3. tradeable(sym) gate (ADR-0077 oracle in prod; fail-closed on error)
      4. rank by confidence*magnitude, cap to max_admissions, tag admitted_via=catalyst
    """
    if (os.environ.get("HERMES_QUANT_CATALYST_ONBOARDING", "0") != "1"
            or os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") != "1"):
        return []
    try:
        from hermes_quant.catalyst.propagation import coverage_against_universe, load_graph
        from hermes_quant.catalyst.synthesize import load_packets_for
        asof = asof or datetime.now(timezone.utc)
        graph, _ = load_graph()
        cov = coverage_against_universe(universe_symbols, graph)
        candidates: list[CatalystAdmission] = []
        for sym in cov["dead_on_arrival"]:           # type: ignore[index]
            packets = load_packets_for(sym, asof)
            if not packets:
                continue
            best = max(packets, key=lambda p: (p.get("confidence", 0.0), p.get("asof", "")))
            conf = float(best.get("confidence", 0.0))
            mag = float(best.get("magnitude", 0.0))
            if conf < tau_conf or mag < tau_mag:
                continue
            stance = best.get("stance", "")
            direction = 1 if stance == "bullish" else -1 if stance == "bearish" else 0
            if direction == 0:
                continue
            try:
                if not tradeable(sym):           # fail-closed: any falsy -> reject
                    continue
            except Exception:  # noqa: BLE001
                continue
            candidates.append(CatalystAdmission(
                symbol=sym, stance=stance, direction=direction, confidence=conf,
                magnitude=mag, horizon=best.get("horizon", "1d"),
                packet_asof=best.get("asof", ""),
            ))
        candidates.sort(key=lambda a: a.confidence * a.magnitude, reverse=True)
        return candidates[:max_admissions]
    except Exception:  # noqa: BLE001 — silence-by-default
        return []
```

### Tradeability gate adapter — `hermes_quant/catalyst/onboarding.py::default_tradeable`

A thin adapter that prefers the ADR-0077 oracle and falls back to fail-closed:

```python
def default_tradeable(symbol: str, *, adv_floor: float = ONBOARD_ADV_FLOOR) -> bool:
    """Production tradeability check: ADR-0077 admissibility oracle (get_asset:
    tradable AND fractionable) + a dollar-volume floor lower than the universe
    screen but > 0. Fail-closed: any error or missing oracle -> False (reject)."""
    try:
        from hermes_quant.admissibility.shortability import AlpacaShortabilityOracle
        # long-side admissibility: tradable+fractionable (no short borrow needed for a long admit)
        oracle = AlpacaShortabilityOracle()
        # ADR-0077 verdict() is short-focused; add/READ a long-tradeable predicate there
        # (asset.tradable and asset.fractionable). If absent, this branch raises -> reject.
        return oracle.is_tradeable_long(symbol)   # add this read-only helper to ADR-0077 oracle
    except Exception:  # noqa: BLE001
        return False
```

> **Cross-ADR note:** ADR-0077's oracle is short-focused (`verdict(symbol, side, qty, asof)`). Onboarding needs a *long-tradeable* read (`tradable and fractionable`). Add a small read-only `is_tradeable_long(symbol) -> bool` to the ADR-0077 oracle (one `get_asset` call, cached) so onboarding reuses the same client/`get_asset` plumbing instead of duplicating it. This is a one-method addition to the ADR-0077 work, recorded as an ADR-0075 implementation dependency.

### Seam A wiring — `ops/scripts/quant-watchlist-evolve.py`

Augment the universe list passed to `evolve_watchlist`. Two surgical changes:

1. Before calling `evolve_watchlist`, compute `admissions = catalyst_admissions(set(universe_symbols), tradeable=default_tradeable)`. Union `[a.symbol for a in admissions]` into the universe list.
2. Pass a per-symbol `sticky_onboard_days` override for admitted names (0, so a strong catalyst is actionable that day) while universe names keep the default 3. This requires a minimal `evolve_watchlist` signature addition: `fast_track_symbols: set[str] | None = None` (symbols in this set use `sticky_onboard_days=0` in `_evolve_one_play`'s onboard rule at `watchlist_evolution.py:366-389`), and stamping `extras={"admitted_via": "catalyst", "catalyst_horizon": a.horizon, "catalyst_asof": a.packet_asof}` on the onboarded `WatchlistEntry`.

**`hermes_quant/playbook/watchlist_evolution.py` edits:**
- `evolve_watchlist(... , fast_track_symbols: set[str] | None = None, admission_extras: dict[str, dict] | None = None)` — both default `None` (preserves all existing callers bit-for-bit).
- Thread `fast_track_symbols` into `_evolve_one_play` (`:248`); in the onboard rule (`:366`) use `0 if symbol in fast_track_symbols else sticky_onboard_days`.
- When a row onboards and `symbol in admission_extras`, set `extras=admission_extras[symbol]` on the new `WatchlistEntry` (the dataclass already carries `extras` to disk, `:111,131`).
- **Sticky-removal protection** (research §1.4, Nautilus #3359 / LEAN `CanRemoveMember`): in the eviction path, do NOT evict an `admitted_via=catalyst` row that has an open position before its `catalyst_horizon` closes — let the position close first. Implement as: skip slow-evict for rows whose `extras.get("admitted_via")=="catalyst"` and whose horizon window has not elapsed AND that have a live position (position lookup is best-effort; if unknown, do NOT evict — fail-safe toward holding the catalyst's horizon).

### Attribution + caps (reuse existing machinery)

- `admitted_via=catalyst` rides `WatchlistEntry.extras` → `play-fit.json` → autonomous-tick audit row (`quant-autonomous-tick.py:400` already copies `plays`; add `extras`/`admitted_via` to the decision record). Dovetails with B13 `play_tag`.
- Hard cap ≤3 enforced in `catalyst_admissions` (`MAX_ADMISSIONS`). Tighter per-name size: reuse ADR-0071 portfolio-caps (`HERMES_QUANT_PORTFOLIO_CAPS=1`) keyed on the `admitted_via` tag for a lower per-name pct ceiling. Do NOT widen the discrete ladder.

### Tests (`tests/unit/test_catalyst_onboarding.py`, new)

- `test_admissions_empty_when_flag_off` — both flags unset → `[]` (default).
- `test_admissions_empty_when_only_semantic_on` — onboarding flag unset → `[]`.
- `test_admissions_skips_in_universe_symbols` — a covered symbol is never admitted (only `dead_on_arrival`).
- `test_admissions_threshold_gate` — packet below `TAU_CONF` or `TAU_MAG` → not admitted; above both → admitted (monkeypatch store via `tmp_path`).
- `test_admissions_tradeability_fail_closed` — `tradeable=lambda s: False` → `[]`; `tradeable` raising → `[]` (not an exception).
- `test_admissions_cap_to_three` — 5 eligible dead symbols → exactly 3 returned, ranked by `confidence*magnitude`.
- `test_admissions_neutral_stance_dropped` — a packet with non-directional stance → not admitted.
- `test_admission_carries_admitted_via_tag` — `CatalystAdmission.admitted_via == "catalyst"`; direction derived from stance (+1 bullish / -1 bearish).
- `test_default_tradeable_fail_closed_without_oracle` — ADR-0077 oracle absent/raising → `default_tradeable` returns `False`.
- Watchlist seam (`tests/unit/test_watchlist_evolution_catalyst.py`): `test_fast_track_onboards_same_day` (admitted symbol with `fast_track_symbols={sym}` onboards in one run at `score>=onboard_floor`, vs a normal symbol that needs 3); `test_admission_extras_persisted` (`admitted_via` survives to `to_dict()`); `test_admitted_open_position_not_evicted_mid_horizon` (sticky-removal protection).

### Eval gate before flag-flip (ADR-0075 Verification — do NOT flip without this)

Add an axis to the catalyst eval (`hermes_quant/catalyst/eval.py`, runner `quant-catalyst-eval-gate.py`): a labeled out-of-universe case (the real LUNR Blue-Origin move) must produce (1) an admission, (2) correct direction, (3) a fillable simulated order. The flag stays OFF until this passes. Document this in the ADR-0075 status update (flip ADR-0075 header to Accepted only after the axis is green).

### Acceptance

- With both flags OFF: `catalyst_admissions` returns `[]`; `evolve_watchlist` output is byte-identical to today (new kwargs default `None`).
- With both flags ON + a strong out-of-universe packet + tradeable=True: the symbol appears in `play-fit.json` with `extras.admitted_via=="catalyst"` and onboards same-day; ≤3 names ever admitted.
- Tradeability fail-closed proven by test (no oracle → no admission).
- Eval-gate axis (LUNR case) documented as the gating artifact before the flag flips.

---

## C2-5 — Learned-graph mining job (B10) — DESIGN ONLY

**Backlog:** B10 (P1, L, "the moat"). **This item is a design doc, not a build.**
**Priority:** P1 (design); build deferred.
**Depends-on:** none for design; build depends on corpus volume.

### The corpus (exists today)

`log_propagations` (`catalyst/propagation.py:191`) appends one row per propagation to `~/.hermes/quant/catalyst/propagation-log.jsonl`:
`{symbol, source, relation, effect_sign, weight, symbol_sign, catalyst_sign, asof}`. `profitability.py::measure_profitability` already joins this against forward returns by relation class — the *infrastructure* for the join is proven. B10 is the next layer: learn **per-edge corrected signs and weights** from accumulated outcomes.

### Design: `hermes_quant/catalyst/graph_mining.py` (DESIGN — not built this wave)

```
mine_graph(fetcher, *, path=propagation-log.jsonl, min_sample=30, horizon_days=21)
  -> {edge_key: EdgeEvidence}

edge_key = (source, target_symbol, relation)
EdgeEvidence:
    n_scored: int
    sign_hit_rate: float          # P(sign(fwd_return) == propagated symbol_sign)
    mean_signed_return: float
    suggested_effect_sign: int    # flip iff sign_hit_rate < 0.5 AND n_scored>=min_sample
    confidence_multiplier: float  # downweight toward 0 for low-hit-rate edges (never amplify >1.0)
    verdict: KEEP | FLIP_SIGN | DOWNWEIGHT | PRUNE
```

**Honesty rails (non-negotiable, from AGENTS.md + ADR-0074):**
- Forward return measured from the **next bar after `asof`** (lookahead-honest); the miner never sees returns when the graph propagates. Same fetcher contract as `profitability.py` (`ForwardReturnFetcher`, injected → offline-testable, no network in unit tests).
- The miner **proposes** edge edits; it NEVER auto-mutates `propagation_graph.seed.yaml`. Output is a report + a *candidate* graph diff the operator reviews. This preserves "hard rules over learned policy" — the curated graph stays operator-authored; the miner is evidence.
- `confidence_multiplier` is **silence-only**: it can pull an edge's weight toward 0 (a wrong edge gets quieter) but never above its curated weight (no amplification). Mirrors the catalyst-as-evidence-never-authority boundary.
- The OPEC-removal lesson (`propagation.py:96-103`) is the canonical positive case: the sign-consistency eval already caught one mis-signed edge by hand; the miner is the systematic version. A `FLIP_SIGN` verdict must additionally pass the existing market-data-free sign-consistency check before the operator applies it (don't flip on noise).

### Cron design (when built)

- Job `quant-catalyst-graph-mine-weekly`, `0 6 * * 6` (Sat 06:00 PT, after the profitability cron), `deliver=origin` no_agent. Silent unless an edge crosses `min_sample` with a `FLIP_SIGN`/`PRUNE` verdict (same change-detecting watchdog pattern as coverage + profitability).
- Emits a candidate graph diff to `~/.hermes/quant/catalyst/graph-mine-candidates.json` for operator review; never writes the live YAML.

### Open questions for the build (not this wave)

1. Minimum corpus volume before any edge is trustworthy — `min_sample=30` is a starting guess; calibrate against `MIN_SAMPLE=20` in profitability.
2. Survivorship/point-in-time: the log is already point-in-time (each row carries `asof`), so the corpus is replayable — but the universe membership at `asof` must be reconstructed for fillability (ties to ADR-0075's `admitted_via` log + B34/B36).
3. Multi-edge interaction: a symbol hit by two opposing edges — does the miner learn per-edge or per-(symbol,event) sign? Start per-edge (matches `propagate`'s noisy-OR × agreement structure).

### Acceptance (design item)

- This section in the plan IS the deliverable. No code ships for C2-5 this wave. The build is gated on corpus volume and is tracked as B10 remaining-open.

---

## Cross-cutting: flags, rails, and verification

| Flag | Default | Item | Effect |
|---|---|---|---|
| `HERMES_QUANT_SEMANTIC_ENABLED` | 0 | C2-2/C2-4 | adds the analyst (existing) AND now gates the shared packet-wiring helper |
| `HERMES_QUANT_CATALYST_ONBOARDING` | 0 | C2-4 | enables `catalyst_admissions`; AND-gated on SEMANTIC_ENABLED |
| `HERMES_QUANT_ADMISSIBILITY` | 0 | C2-4 (dep) | ADR-0077 oracle that backs `default_tradeable` |

**Rails honored:** every new capability is DEFAULT-OFF behind a flag; gate read at call time (cron/test flips take effect immediately); onboarding only ADMITS to the candidate set (the ADR-0004 gate remains final authority and can still silence); the discrete sizing ladder is untouched; `asof` honesty is preserved (the wiring helper stamps `decision_asof`; the onboarding helper validates packets via `load_packets_for`); silence-by-default on every error path (helpers return `None`/`[]`, never raise).

**Run before any PR:**
```
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_no_lookahead.py tests/unit/test_catalyst*.py -q
~/.hermes/hermes-agent/venv/bin/python3 -m ruff check hermes_quant/ tests/ ops/scripts/
~/.hermes/hermes-agent/venv/bin/python3 -m mypy hermes_quant/
```

## File manifest

**New:**
- `hermes_quant/catalyst/wiring.py` (C2-2)
- `hermes_quant/catalyst/onboarding.py` (C2-4)
- `tests/unit/test_catalyst_wiring.py` (C2-2)
- `tests/unit/test_catalyst_onboarding.py` (C2-4)
- `tests/unit/test_watchlist_evolution_catalyst.py` (C2-4)
- `tests/unit/test_catalyst_profitability_cron.py` (C2-1)

**Edited:**
- `ops/scripts/quant-daily-interim.py` (C2-2: replace inline block with helper)
- `ops/scripts/quant-autonomous-tick.py` (C2-2: inject in `_direction_screened_recommend`; C2-4: copy `admitted_via` into audit record)
- `ops/scripts/quant-playbook-tick.py` (C2-2: pass `market_extras` into `_recommend`)
- `ops/scripts/quant-catalyst-profitability.py` (C2-1: change-detecting watchdog)
- `ops/scripts/quant-watchlist-evolve.py` (C2-4: union admissions + fast-track)
- `hermes_quant/playbook/watchlist_evolution.py` (C2-4: `fast_track_symbols`/`admission_extras` kwargs + sticky-removal protection)
- `hermes_quant/admissibility/shortability.py` (C2-4 dep: add read-only `is_tradeable_long`)
- `tests/test_no_lookahead.py` (C2-3: two new assertions)
- `docs/adr/ADR-0075-catalyst-driven-universe-onboarding.md` (C2-4: status note + eval-gate axis)

**Design-only (no code):**
- `hermes_quant/catalyst/graph_mining.py` (C2-5: specified, not built)
