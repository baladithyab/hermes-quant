# ADR-0070: Paper-execution fidelity — slippage, queue delay, and fill realism

**Status:** Accepted (2026-05-28), implemented
**Date:** 2026-05-28
**Wave:** D (paper-trading fidelity)
**Supersedes:** nothing
**Cites:** [ADR-0011](ADR-0011-broker-adapter-seam.md) (broker-adapter seam), [ADR-0049](ADR-0049-shadow-account-counterfactual.md) (shadow-account counterfactual), [ADR-0068](ADR-0068-decision-time-vs-bar-time-honesty.md) (decision-time honesty), [ADR-0069](ADR-0069-still-forming-bar-discipline.md) (bar discipline), `hermes_quant.daemon.slippage` (existing slippage measurement helpers)

---

## Context

`PaperReactor.execute` (`hermes_quant/react/paper.py`) writes `fill_price = decision_price` for every paper fill. The module docstring is explicit about this — "v0.1.2 deliberately does NOT simulate slippage on paper fills." The reasoning at the time: slippage modeling lived upstream in `MarketState` and the cost-gate threshold check, and the paper layer was meant to be a simple recorder.

Forensic on 2026-05-28 (112 fills): **`fill_price == decision_price` for 112/112 fills (100%). Mean realized slippage: 0 bps.**

This is the "paper-system tells you everything it does is perfect" failure mode. Three concrete consequences:

### P1. Live execution will be strictly worse, with no warning

A live broker fill on a 20%-of-NAV equity short during regular session sees:
- **Spread cross**: ~1–5 bps for top-100 liquid names, 10–30 bps for thin names.
- **Implementation shortfall**: ~5–15 bps for size-of-NAV orders that walk the book.
- **Queue-position penalty**: a market order at 14:00:00 ET fills at 14:00:01 ET's price, not 14:00:00 ET's. On a moving market that's typically 1–10 bps drift per second of latency.
- **Open/close auction premium**: post-15:30 ET activity that times into the close auction can pay 10–50 bps depending on imbalance.

A paper system that reports 0 bps on every fill provides no signal that the live version will give back 10–40 bps round-trip. Wave-7 reflection P&L numbers, calibration confidence intervals, and Sharpe estimates from the paper system are **all biased upward** by 2× the round-trip cost relative to live truth.

### P2. The cost-gate threshold is the only thing modeling cost — and it's pre-decision

`hermes_quant.risk.gate.cost_gate_threshold` enforces `abs(edge) > cost_multiple × round_trip_cost` before the gate emits an action. That's the *forecast* of cost used to decide IF to trade. It's not the *realized* cost of the trade. Without realized slippage on paper fills, we can never validate the cost forecast against truth on this dataset.

The companion module `hermes_quant.daemon.slippage` exists to *measure* slippage (it has `compute_adverse(fill_price, decision_price, side)` and `record_slippage_event(...)`), but it always sees zero on paper fills because PaperReactor passes through `decision_price` as `fill_price`.

### P3. Decision-time vs bar-time honesty (ADR-0068) widens the gap

After ADR-0068 lands, `asof_decision` becomes wall-clock (e.g. 17:09 UTC). `asof_execution` is also wall-clock (within 1s of asof_decision). At zero modeled slippage, `fill_price - decision_price = 0` is precisely defensible — the BMA computed at 17:09:03 and PaperReactor "filled" at 17:09:03 with the same tick. **But that's not what live looks like.** Live execution between t and t+1s sees price drift that paper currently ignores. ADR-0068 makes the decision-execution latency visible and honest; ADR-0070 makes the *price consequence* of that latency visible and honest.

---

## Decision

### D70.1 PaperReactor models a configurable, per-asset-class slippage envelope

Replace the unconditional `fill_price = decision_price` with a slippage model:

```python
fill_price = apply_slippage(
    decision_price=decision_price,
    side=side,                              # "long" | "short" or +1/-1
    target_pct=target_position_pct,         # for size-aware impact
    market=ctx.market_state,                # spread, ATR, ADV
    asset_class=task.asset_class,
    config=PaperSlippageConfig(...),        # per-asset-class envelope
    rng=self._rng,                          # deterministic per-fill seed
)
```

`PaperSlippageConfig` exposes:

| Knob | Default (US equities) | Default (crypto) | Meaning |
|---|---|---|---|
| `spread_cross_bps` | 3 | 8 | Half-spread paid on entry + half on exit; we model the entry side (multiply ×2 for round-trip on exit). |
| `impact_bps_per_pct_adv` | 5 | 12 | Linear permanent-impact term; size proportional to `target_pct × NAV / ADV_dollars`. |
| `queue_latency_seconds` | 1.0 | 0.5 | Wall-clock delay before fill; price moves one σ_per_second of ATR-implied volatility. |
| `auction_premium_bps` | 10 (post-15:30 ET) | 0 | Extra paid for late-session orders that time into the close auction. |

The mean of the modeled slippage is roughly **5–15 bps per fill for liquid US equities at 20% NAV**, with realistic variance around it (driven by ATR-implied σ_per_second and a fixed RNG seed).

### D70.2 Determinism via fill-keyed RNG seed

A hash of `(proposal_id, asof_execution)` seeds a per-fill `Generator`. Two replays of the same fill produce the same modeled slippage. This preserves the ADR-0009 §replay-equality property even with stochastic slippage.

### D70.3 Existing `hermes_quant.daemon.slippage` integration

`compute_adverse(fill_price, decision_price, side)` and `record_slippage_event` exist and currently always observe 0 on paper. After this ADR, they observe the modeled slippage, which feeds into the realized-cost-vs-forecast comparator that the cost-gate calibration loop will eventually consume.

### D70.4 Feature flag for v0.1 → v0.2 transition

Default the new behavior **off** for one full trading day after merge:

```bash
HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2  # opt-in
```

When unset or `v0.1`, PaperReactor falls back to the legacy `fill_price = decision_price` behavior. This protects in-flight retrospection / reflection / calibration jobs from a sudden regime change in the paper data they consume. After 1 trading day of side-by-side validation (run the comparator below), flip the default to `v0.2` and deprecate `v0.1`.

### D70.5 Realized-cost calibration loop (deferred to a future ADR)

The cost-gate uses *forecast* round-trip cost (`market.commission + 0.5*market.spread + market.slippage_estimate`). Once D70.1 is live, we have *realized* cost per fill (`compute_adverse` against the new `fill_price`). A future ADR establishes a calibration loop that compares forecast to realized and tightens `cost_gate_threshold` parameters when forecast underestimates. This ADR does **not** change cost-gate forecast parameters — only the realized observation.

### D70.6 Counterfactual / shadow-account symmetry

`ShadowAccount` ([ADR-0049](ADR-0049-shadow-account-counterfactual.md)) replays through `PaperReactor` to produce counterfactual fills. After this ADR, shadow fills also carry modeled slippage. The "paper vs no-trade counterfactual" comparison stays apples-to-apples.

---

## Consequences

**Positive:**
- Live-vs-paper P&L gap shrinks from 10–40 bps round-trip-uncalibrated to 1–5 bps round-trip-residual.
- Realized-vs-forecast cost calibration becomes possible (deferred to future ADR).
- Reflection P&L numbers stop being 2× round-trip-cost biased.
- Wave-7+ Sharpe / IR estimates produced on paper data approach live truth.

**Negative / risks:**
- Existing reflectors / calibrators consuming `executions.jsonl` see a regime shift on the day the v0.2 flag flips. Mitigated by D70.4's feature flag and 1-day side-by-side burn-in.
- Slippage parameter defaults are *opinionated estimates*. Wrong defaults are loudly wrong (P&L shifts visibly), not silently wrong, but operators should expect to tune them after the calibration loop (D70.5) lands.
- Calibration of slippage parameters requires real broker fills to ground-truth against. Until ADR-0011's broker reactors land with real fills, defaults are theoretical.

**Out of scope:**
- Borrow / locate fees on shorts. Real, but separate. Future amendment.
- Hard-to-borrow availability gate. Real, but separate.
- Options multi-leg fills. Covered by [ADR-0029](ADR-0029-multi-leg-paper-reactor.md).
- Crypto exchange fee tiers. Future amendment.

---

## Implementation hooks

- New module: `hermes_quant/react/slippage_model.py` with `apply_slippage(decision_price, side, target_pct, market, asset_class, config, rng) -> float`.
- New config dataclass: `PaperSlippageConfig` with the four-knob envelope above; per-asset-class defaults.
- `hermes_quant/react/paper.py:PaperReactor.execute`: replace `fill_price = decision_price` with a call to `apply_slippage` gated on `HERMES_QUANT_PAPER_SLIPPAGE_MODEL == "v0.2"`.
- `hermes_quant/react/paper.py`: thread `PaperSlippageConfig` through `__init__` for testability; default config from env / package defaults.
- Test: deterministic-rng round-trip on a fixture order; assert `fill_price` in `[decision_price * (1 - cap_bps), decision_price * (1 + cap_bps)]` for a sane cap (~50 bps); same seed → same fill_price.
- Test: side correctness — a long fill pays positive slippage (fill_price > decision_price), a short fill pays positive slippage (fill_price < decision_price, fewer dollars credited).
- Comparator script (one-shot): replay last 7 days of `executions.jsonl` through the v0.2 model, diff to v0.1, surface aggregate P&L delta. Sanity: should be a few percent of NAV at most for the current 20%-per-pick book.

This ADR's **scope is the model + its plumbing into PaperReactor**. The realized-cost calibration loop (D70.5) is a follow-up. The minimum viable inline fix is plumbing the model with sensible defaults behind the feature flag — operators can tune knobs post-hoc.

---

## Verification

```python
import numpy as np
from hermes_quant.react.slippage_model import apply_slippage, PaperSlippageConfig

cfg = PaperSlippageConfig.equity_default()
rng = np.random.default_rng(seed=12345)

# Long fill: pays positive slippage (filled higher than decision)
fp_long = apply_slippage(
    decision_price=100.0, side=+1, target_pct=0.20,
    market=fake_market(spread=0.0003, atr_pct=0.015, adv_dollars=5e9),
    asset_class="equity", config=cfg, rng=rng,
)
assert fp_long > 100.0
assert fp_long < 100.5   # not insane

# Short fill: pays positive slippage (filled lower → fewer dollars credited)
rng = np.random.default_rng(seed=12345)  # same seed → reproducible
fp_short = apply_slippage(
    decision_price=100.0, side=-1, target_pct=-0.20,
    market=fake_market(spread=0.0003, atr_pct=0.015, adv_dollars=5e9),
    asset_class="equity", config=cfg, rng=rng,
)
assert fp_short < 100.0
assert fp_short > 99.5

# Determinism: same proposal_id + asof_execution → same fill_price
# (PaperReactor seeds rng from hash(proposal_id, asof_execution))
```

Post-merge with flag on, sanity probe:

```bash
~/.hermes/hermes-agent/venv/bin/python3 -c "
import json
from pathlib import Path
fills = [json.loads(l) for l in Path('~/.hermes/quant/executions.jsonl').expanduser().read_text().splitlines() if l.strip()]
recent = [f for f in fills if f.get('asof_execution', '').startswith('2026-05-29') and f.get('reactor_metadata', {}).get('slippage_model') == 'v0.2']
deltas = [(f['fill_price'] - f['decision_price']) / f['decision_price'] * 1e4 for f in recent]
print(f'count={len(deltas)} mean_bps={sum(deltas)/len(deltas):.1f} max_bps={max(deltas, default=0):.1f}')
"
# Expect: mean_bps in [3, 15] for liquid equities, max_bps < 50.
```
