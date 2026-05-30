# ADR-0075 — Catalyst-driven universe onboarding

**Status:** Proposed
**Date:** 2026-05-29
**Supersedes:** none
**Related:** ADR-0073 (event catalyst awareness), ADR-0074 (Catalyst Sense)

## Context

Catalyst Sense (ADR-0074) propagates a news catalyst from a source entity to
target symbols via the curated butterfly graph, emits `SemanticPacket`s, and
feeds them to the advisor as a peer analyst view. The advisor only ever
*recommends within its tradeable universe* — a liquidity-screened ~500-name set
refreshed daily (`~/.hermes/quant/universe/alpaca-daily.json`).

A coverage probe on 2026-05-29 (`coverage_against_universe`,
`ops/scripts/quant-catalyst-coverage.py`) surfaced a structural gap: **4 of 17
graph target symbols were NOT in the universe** — `AMD`, `LCID`, `LUNR`, `SPR`.
For those names a catalyst fires, a packet is produced and persisted, but the
advisor never recommends the symbol, so the semantic signal is **dead on
arrival**.

This is the original motivating case sharpened: the Blue Origin explosion moved
`LUNR` (Intuitive Machines) sharply, and `LUNR` is exactly one of the names that
falls below the liquidity screen on a given day. The feature can *perceive* the
catalyst but cannot *act* on it — the same scope boundary the whole subsystem
was built to close, displaced one layer.

Categories of dead-on-arrival target:
- **Transient screen artifact** (e.g. `AMD`) — a top-liquidity name that the
  daily screen happened to omit; it re-enters on the next refresh. The edge is
  correct; pruning it would be wrong.
- **Genuinely sub-threshold** (`LUNR`, `LCID`, `SPR`) — smaller-cap names that
  legitimately fall below the liquidity floor most days but are precisely the
  high-beta catalyst reactors we want to trade *when* a catalyst hits.

## Decision

Keep all edges (pruning discards real catalyst knowledge). Add a **catalyst-driven
temporary universe admission** mechanism: when a fresh, high-confidence,
high-severity packet targets an out-of-universe symbol, temporarily admit that
symbol to the tradeable set for the catalyst's horizon, subject to a hard
tradeability gate.

Shape (to implement):

1. **Admission trigger** — at advisor-recommend time, after `load_packets_for`,
   collect packets whose `asset` is out-of-universe AND
   `confidence >= τ_conf` AND `magnitude >= τ_mag` (both tunable; start
   conservative, e.g. 0.6 / 0.04). These are *admission candidates*.
2. **Tradeability gate** — before admitting, verify the symbol is actually
   tradeable on the broker (shortable/fractionable/min dollar-volume floor lower
   than the universe screen but non-zero). A name with no borrow / no liquidity
   is rejected — admission must not create an unfillable order.
3. **Scoped admission** — admit for the packet horizon only (e.g. 1 trading
   day), tagged `admitted_via=catalyst` in the execution record so the retro /
   calibrator can attribute performance to catalyst-onboarded trades distinctly.
4. **Caps** — a hard cap on simultaneous catalyst-admitted names (e.g. ≤ 3) and
   a tighter per-name size cap than universe names (these are higher-risk,
   lower-liquidity). Reuses the ADR-0071 portfolio-caps machinery.
5. **Default OFF** behind `HERMES_QUANT_CATALYST_ONBOARDING=1`, gated on
   `HERMES_QUANT_SEMANTIC_ENABLED=1` (onboarding without semantic is meaningless).
   Like ADR-0074, stays off until a precision/sign gate covers the admitted-name
   path.

## Consequences

**Positive:** closes the perceive-but-can't-act gap for the exact high-beta
names the feature targets; performance is attributable (the `admitted_via` tag);
the tradeability gate prevents unfillable orders; default-OFF + caps keep blast
radius small.

**Negative / risks:** admitted names are lower-liquidity → worse fills (the
ADR-0070 slippage model must apply, likely with a wider band for admitted
names); catalyst-onboarding is a new path the calibrator hasn't seen → cold-start
miscalibration; a wrong edge-sign now has a *direct* trade consequence on a
volatile small-cap, raising the bar on the ADR-0074 sign-consistency gate (now
shipped as the third eval axis).

**Until built:** the coverage probe (`quant-catalyst-coverage.py`) makes the gap
*visible* — the operator sees which targets are dead-on-arrival and can decide
keep/prune/onboard per symbol. Graph edits that add a dead edge surface
immediately instead of silently producing unconsumed packets.

## Verification (when implemented)

- Eval-gate axis for the admitted path: a labeled out-of-universe catalyst case
  (e.g. the real `LUNR` Blue Origin move) must produce an admission + correct
  direction + a fillable simulated order.
- `coverage_against_universe` dead-list shrinks for onboarding-eligible names
  when the flag is on.
- Retro attribution: `admitted_via=catalyst` trades reported as a distinct
  bucket in the weekly strategy retro.

## Implementation status (Wave C2 — 2026-05-30)

The onboarding mechanism is **built and merged behind the default-OFF flag**,
but the flag **remains OFF** (header stays *Proposed*) until the eval-gate axis
below is green. Shipped this wave:

- `hermes_quant/catalyst/onboarding.py` — `catalyst_admissions(universe, tradeable=…)`
  (≤3 cap via `MAX_ADMISSIONS`, `TAU_CONF=0.60`/`TAU_MAG=0.04` thresholds, ranks by
  `confidence*magnitude`, derives direction from stance, fail-closed tradeability
  gate). Returns `[]` unless **both** `HERMES_QUANT_CATALYST_ONBOARDING=1` AND
  `HERMES_QUANT_SEMANTIC_ENABLED=1` are set; never raises (silence-by-default).
- `default_tradeable(symbol)` reuses the ADR-0077 oracle via a new read-only
  `AlpacaShortabilityOracle.is_tradeable_long(symbol)` (`tradable AND fractionable`),
  fail-closed to `False` when the oracle is absent/raises (no duplicate `get_asset`).
- Seam A wiring (`ops/scripts/quant-watchlist-evolve.py` → `evolve_watchlist`): new
  default-`None` kwargs `fast_track_symbols` (same-day onboard, `sticky_onboard_days=0`),
  `admission_extras` (stamps `admitted_via=catalyst`/horizon/asof on the onboarded
  `WatchlistEntry.extras`), `extra_universe_symbols` (unions admitted out-of-universe
  names into the scored set), and `position_lookup` (sticky-removal protection —
  Nautilus #3359 / LEAN `CanRemoveMember`: a catalyst row with an open position is not
  slow-evicted before its horizon closes; fail-safe to hold when unknown). With the
  kwargs `None`, `evolve_watchlist` output is bit-for-bit identical to today.
- `admitted_via=catalyst` rides `play-fit.json` → the autonomous-tick decision audit
  row (`quant-autonomous-tick.py`) for distinct retro attribution.

**Eval-gate axis (gating artifact — do NOT flip the flag without this):** add the
labeled out-of-universe case (the real `LUNR` Blue-Origin move) to the catalyst eval
so it must produce (1) an admission, (2) correct direction, (3) a fillable simulated
order. The flag stays OFF and this ADR stays *Proposed* until that axis is green; flip
to *Accepted* only after.
