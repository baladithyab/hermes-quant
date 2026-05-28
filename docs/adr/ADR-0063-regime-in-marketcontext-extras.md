# ADR-0063 — Regime in MarketContext.extras

**Status:** Accepted (2026-05-27)
**Wave:** v0.6.0
**Full design:** [docs/design/v0.6.0-regime-in-state.md](../design/v0.6.0-regime-in-state.md) (532 lines)

## Context

The `MarketContext.extras` docstring at `protocol.py:80-82` claims it carries `(orderbook, news, regime, ...)`. Audit confirms regime is **never populated** there in production code. Regime classification only happens inside `aggregators/bma.py:389-411` *after* analyst fan-out — meaning analysts run BLIND to the current regime.

This is wasted information. ClassicalTA's trend-following weight, Microstructure's orderbook-flow significance, Semantic's news-volatility correlation, and Kronos's regime-transition uncertainty all benefit from regime context at analysis time.

## Decision

Introduce `hermes_quant/regime/extras_builder.py` with `build_regime_extras(symbol, bars, asof, min_bars=60)` returning a dict suitable for merge into `MarketContext.extras`.

**Public API:**
```python
ctx.extras["regime"] = RegimePacket(           # frozen dataclass | None
    label: str,                                # human-readable, NOT for branching
    volatility_tier: int,                      # -1 (low) / 0 (normal) / +1 (high) — STABLE across HMM retrains
    posterior: float,                          # confidence
    state_vars: StateVariables,                # full feature snapshot
    asof: datetime,
    classifier_kind: str,                      # 'hmm' | 'rule_based' | ...
    reason: Optional[str],                     # populated only on UNKNOWN
)
ctx.extras["regime_failure"] = "<reason>"      # set ONLY on classifier failure
ctx.extras["regime_classifier_kind"] = "..."   # always populated
```

**Hard rule (per ADR-0058 unsupervised label-mapping caveat):**
> Analyst conditioning MUST branch on `volatility_tier` or numeric `posterior`. Anchoring on `.label` strings is forbidden (one carve-out: `RegimeState.UNKNOWN` is fixed).

## Consequences

**Positive:**
- Analysts can adjust confidence/weight by regime without anchoring on unstable label strings
- Single point of regime classification (was duplicated per-tick implicitly via BMA)
- Clean abstain semantics on classifier failure (`extras["regime"] = None` + reason)
- Future-proof: `volatility_tier` survives HMM retraining and even classifier swap

**Negative:**
- Adds one classification call per tick (mitigation: cache by (symbol, asof) — a 5-min cache covers tick-loop calls)
- Analysts must handle `extras.get("regime") is None` defensively
- Tests must cover both regime-aware and regime-unavailable paths

## Implementation Plan

1. Create `hermes_quant/regime/extras_builder.py` with `build_regime_extras()` and `RegimePacket` frozen dataclass
2. Modify `advisor.recommend()` (and `recommend_multi_horizon`) to call `build_regime_extras()` and merge into `market_extras` BEFORE constructing `MarketContext`
3. Update each analyst's `analyze(ctx)` to read `ctx.extras.get("regime")` and apply per-analyst confidence multiplier (Behind `HERMES_QUANT_ANALYSTS_USE_REGIME=1` flag, default OFF for v0.6.0 ship; flip to ON in v0.6.1 after observation)
4. Per-analyst multipliers (deliberate, conservative — each is a single multiply, no branching tree):
   - **ClassicalTA** in `volatility_tier == +1` (high vol): `confidence *= 0.7` (trend signals less reliable in chop)
   - **Microstructure** in `volatility_tier == -1` (low vol): `confidence *= 1.15` (orderbook flow more meaningful)
   - **Semantic** in `volatility_tier == +1` (high vol): `confidence *= 1.20` (news drives more)
   - **Kronos** when `label == 'UNKNOWN'`: `confidence *= 0.85` (transition uncertainty)
5. Update `governance/audit_log.py` to optionally include regime in `gate_event` provenance

## Test Plan

7 unit tests + 1 integration:
- `test_happy_path_returns_regime_packet`
- `test_classifier_missing_returns_failure_reason`
- `test_insufficient_bars_returns_failure_reason`
- `test_classify_exception_returns_failure_reason`
- `test_label_stability_invariant_across_seeds` (regression for ADR-0058 caveat)
- `test_unknown_is_not_failure` (UNKNOWN is a valid regime, not an error)
- `test_caller_cannot_shadow_regime_key` (extras_builder is authoritative for `regime`)
- `test_e2e_advisor_with_regime_in_extras` (integration)

## Migration

- v0.6.0: Ship infrastructure + ADR. `HERMES_QUANT_ANALYSTS_USE_REGIME=0` default — analysts read regime but DO NOT condition on it. Just observability.
- v0.6.1: Flip flag to default ON after a week of observation (verify no spurious confidence inflation/deflation patterns).

## Alternatives Considered

- **Inline classify in `advisor.recommend()`**: rejected for testability + complexity reasons. Helper is cleaner.
- **Top-level `MarketContext.regime` field** (not in extras): deferred to v1.0 protocol bump. Backward-compat blocker.
- **Bare label string in extras**: rejected. Violates ADR-0058 stability invariant.

## Related

- ADR-0047 (regime-aware BMA weights) — the existing consumer
- ADR-0058 (HMM regime classifier) — the underlying classifier with label-mapping caveat
- ADR-0036 (silence-by-default) — the `try/except` wrapper pattern
