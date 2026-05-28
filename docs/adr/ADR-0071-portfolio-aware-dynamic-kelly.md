# ADR-0071: Portfolio-aware dynamic Kelly sizing and exposure caps

**Status:** Proposed
**Date:** 2026-05-28
**Wave:** D (paper-trading fidelity → risk control)
**Supersedes:** nothing
**Amends:** [ADR-0004](ADR-0004-deterministic-risk-gate.md) — adds portfolio-layer sizing on top of per-symbol cost-gate + Kelly
**Cites:** [ADR-0004](ADR-0004-deterministic-risk-gate.md) (deterministic risk gate), [ADR-0014](ADR-0014-portfolio-context-deferred.md) (portfolio context deferral), [ADR-0029](ADR-0029-multi-leg-paper-reactor.md) (multi-leg reactor), [ADR-0035](ADR-0035-portfolio-rebalancing.md) (portfolio rebalancing), `hermes_quant.risk.kelly` (current quarter-Kelly sizer), `hermes_quant.risk.gate` (current per-symbol gate)

---

## Context

Forensic on 2026-05-28 paper book:

```
43 open positions
  long:    5 × 20% NAV =   100% gross
  short:  38 × 20% NAV =   760% gross
  total gross exposure:    860% of NAV
  net exposure:           -660% of NAV (heavily net-short)
```

Each fill was sized to `±20%` of NAV by the per-symbol Kelly sizer, with **no awareness** that 42 other positions were also being sized at 20%. The deterministic gate's `max_position_pct = 0.20` is a **per-symbol** cap, not a portfolio cap.

This is structurally bad for two reasons the operator already named:

### P1. Loss-stacking risk

A 20% NAV short can lose ~5% (one σ adverse day) → portfolio loses 1% NAV per name. With 38 of them, a single bad day where most short-leg names move together (e.g. an unexpected Fed dovish pivot, short squeeze contagion, broad reversal) compounds into 30–40% NAV drawdown in one session. The cost-gate's per-symbol edge filter is *individually* prudent and *jointly* reckless because correlations across the short book are high (most are equity-beta proxies during a bearish-regime regime classification).

### P2. Capital starvation for opportunities

The book is at 860% gross. There is **no cash sleeve** for new opportunities. If a high-conviction long single-name catalyst appears tomorrow at 14:00 ET, the gate would size it to +20%, but the portfolio cannot honor that without first closing something. Today's PaperReactor doesn't enforce this — it happily writes the new fill at +20% on top of the existing 860% — but live execution would fail at margin. Paper says yes; live says no. We want paper to say no first.

Both failure modes share a common cause: the gate sizes each pick as if it were the *only* pick. The Kelly criterion's optimality is conditional on **no correlated positions**; a portfolio of correlated bets at full Kelly is a known blow-up profile (see Thorp's "Kelly is too aggressive" caveat for correlated assets).

[ADR-0014](ADR-0014-portfolio-context-deferred.md) deliberately deferred portfolio context with a v0.1 single-symbol view. That deferral made sense when the system fired 1–3 picks per day. With 43 picks per day post-autonomy, it does not.

---

## Decision

### D71.1 Two-stage sizing — per-symbol Kelly first, portfolio normalization second

The current gate produces a per-symbol target. Wrap that target in a portfolio-aware second stage:

```
stage 1 (existing):  per_symbol_target = quarter_kelly_size(edge, σ², ...)
                     (clipped to per_symbol_max, snapped to action_step)

stage 2 (new):       portfolio_target = portfolio_normalize(per_symbol_target, portfolio_state)
```

Stage 1 is unchanged. Stage 2 reads current book state and scales each pending pick so that the portfolio respects three caps simultaneously.

### D71.2 The three portfolio caps

| Cap | Default | Meaning |
|---|---|---|
| `max_gross_exposure_pct` | **2.0** (200% NAV) | Sum of `abs(target_pct)` across all open + pending positions ≤ this. Standard "2× leveraged" bound. |
| `max_net_exposure_pct` | **1.0** (100% NAV) | `sum(target_pct)` ∈ `[-max_net, +max_net]`. Prevents a 760% net-short book. |
| `min_cash_reserve_pct` | **0.20** (20% NAV) | At least this much NAV is kept free. Equivalent to `max_gross ≤ 1 - min_cash`, but tracked separately so a single high-gross trade can be denied independently of a high-net trade. |

The 200% / 100% / 20% defaults are operator-tunable via `RiskConfig.PortfolioCaps`. Profile-specific overrides for "conservative" (100/50/40) and "aggressive" (300/150/10) follow the same shape as the existing `RiskConfig.profile_*` constants.

### D71.3 Dynamic Kelly: scale-to-fit, drop-with-priority

When pending picks would breach a cap, normalize via two policies in sequence:

**Policy A — equal-share scale-to-fit.** Scale every pending pick uniformly by a factor `λ ∈ (0, 1]` such that the post-fill portfolio respects all caps. This preserves *relative* sizing across picks (the per-symbol Kelly ratio is preserved) while shrinking the overall footprint.

```
λ_gross = (max_gross - existing_gross) / new_gross_demand
λ_net   = (max_net   - existing_net)   / new_net_demand   # if signed, more nuanced
λ_cash  = (1 - min_cash - existing_gross) / new_gross_demand
λ       = min(λ_gross, λ_net, λ_cash, 1.0)
```

For the 5/28 case: 43 picks × 20% = 860% demand. With caps (200% gross, 20% cash → 160% available headroom), `λ ≈ 160/860 = 0.186`. Each pick scales from 20% to ~3.7%. The book still fires all 43 names, just much smaller.

**Policy B — priority-rank drop.** If the operator prefers fewer larger positions over many smaller ones, sort pending picks by Kelly-edge magnitude, accept top-N until caps bind, drop the rest. This preserves *absolute* per-pick sizing (each fired pick sees its full per-symbol Kelly) at the cost of dropping marginal-edge names.

Default policy: **A (scale-to-fit)** — preserves the gate's intent of "this is the per-symbol relative attractiveness ranking" without the operator having to choose a top-N cutoff. Policy B is selectable via `RiskConfig.portfolio_normalization = "priority_rank"`.

### D71.4 Treat existing positions as already-committed

Stage-2 normalization reads the **current book** (positions already filled today and prior days), computes available headroom after them, and applies λ to **only the pending new picks**. Existing positions are not re-sized retroactively — that's a separate rebalancer's job ([ADR-0035](ADR-0035-portfolio-rebalancing.md), wave 4 wired).

If existing positions ALREADY breach a cap (e.g. you wake up at 860% gross and the cap is 200%), Stage-2 silences ALL new picks until the rebalancer brings the book back in bounds. This fails closed: we don't add to a too-leveraged book.

### D71.5 Correlation-aware optional refinement (deferred)

A more sophisticated normalization weights pending picks by their portfolio-correlation contribution: a pick correlated with the existing book sees a stricter shrink than an uncorrelated pick. This requires an estimated cross-asset correlation matrix at decision time. Defer to a future ADR; the ADR-0071 baseline uses uniform scaling, which is **strictly safer than the status quo** even without correlation awareness.

### D71.6 Plumbing into the existing gate flow

`RiskGate.evaluate` produces `Action(target_position_pct=...)` per signal. The new flow:

1. Per-symbol gate runs unchanged, produces `per_symbol_target`.
2. The deciding caller (autonomous tick loop, advisor `recommend()`, or playbook tick) collects all per-symbol targets for this batch, builds a `PortfolioState` (current positions from `executions.jsonl` settlement), and calls `portfolio_normalize(targets, portfolio_state, caps) -> normalized_targets`.
3. Each normalized target is written back into the `Action` / `Proposal`. The audit log records BOTH the per-symbol target AND the post-normalization target so we can inspect "what would Kelly have wanted vs what the portfolio cap allowed."

### D71.7 Live-vs-paper symmetry

Live broker reactors (when they land per [ADR-0011](ADR-0011-broker-adapter-seam.md)) typically reject orders that breach margin. The portfolio-cap layer is the **paper-side mirror** of margin enforcement — paper system says no for the same reason live would say no. This makes paper→live promotion pass-through; nothing surprises us at the live boundary.

---

## Consequences

**Positive:**
- Loss-stacking bounded: 200% gross caps the worst-case correlated-day drawdown to roughly 200% × σ_basket ≈ 4–8% NAV per σ-day, vs. 30–40% today.
- Capital reserve preserved: 20% min-cash means a fresh high-conviction pick can always size in (modulo headroom from existing positions).
- Paper/live margin parity: paper enforces what live margin enforces. No surprise rejection at the live boundary.
- Dynamic Kelly: relative ranking from the per-symbol gate is preserved through Policy A scaling — we still trade the gate's preferred names, just at appropriate aggregate size.
- Operator-tunable: caps live in `RiskConfig.PortfolioCaps`, not hardcoded.

**Negative / risks:**
- Per-pick size shrinks dramatically when the gate is firing many picks. With 43 actionables and 200%/20% caps, each pick fires at ~3.7% NAV instead of 20%. The expected return per pick shrinks proportionally; the *aggregate* expected return is roughly preserved (more picks at smaller size ≈ same Kelly-budget allocation), but per-pick attribution gets noisier.
- Operators may want different policies per regime (aggressive in low-vol, conservative in high-vol). Out of scope here; can be wired by binding `RiskConfig.portfolio_caps` to the regime classifier output (see ADR-0058 + ADR-0063).
- The implicit assumption "per-symbol Kelly ranking captures relative attractiveness" weakens when picks are highly correlated. D71.5 (correlation-aware normalization) is the right answer; until that lands, Policy A's uniform scaling is conservative-but-fair.

**Out of scope (explicitly):**
- Correlation-aware sizing (D71.5).
- Sector / asset-class sub-caps (e.g. "no more than 30% gross in financials"). Future amendment.
- Dynamic-cap adjustment based on realized drawdown (e.g. shrink caps after a -10% week). Future amendment.
- VaR / CVaR-based sizing. Different framework; not in this ADR.
- Re-sizing existing positions when a new pick arrives. Rebalancer's job.

---

## Implementation hooks

- New module: `hermes_quant/risk/portfolio_normalize.py` with:
  - `@dataclass PortfolioCaps(max_gross_exposure_pct=2.0, max_net_exposure_pct=1.0, min_cash_reserve_pct=0.20, normalization="scale_to_fit")`.
  - `@dataclass PortfolioState(positions: dict[str, float], cash_pct: float)` — current book.
  - `normalize_targets(per_symbol_targets: list[tuple[asset, target_pct]], state: PortfolioState, caps: PortfolioCaps) -> list[tuple[asset, target_pct, scale_factor]]`.
  - Tests cover the 43-picks-at-20% case, the headroom-already-breached case (silence all), and the cap-not-binding case (no change).
- `hermes_quant/risk/gate.py:RiskConfig` — add `portfolio_caps: PortfolioCaps`.
- `hermes_quant/autonomous.py` and the tick-batch / playbook callers — collect per-symbol targets, build PortfolioState from settled `executions.jsonl`, call `normalize_targets`, write normalized `target_position_pct` into the Action/Proposal.
- `hermes_quant/governance/audit_log.jsonl` — gate_approval event payload gains `per_symbol_target_pct` (Stage 1) and `portfolio_target_pct` (Stage 2 = the actual fired size). Operators can grep for cases where they differ.
- `hermes_quant/cli/status.py` — show current book gross/net/cash alongside pending picks.
- `~/.hermes/scripts/quant-daily-interim.py` brief — print "would have fired X% / cap-scaled to Y%" for each pick.
- `PortfolioState` reconstruction: helper in `hermes_quant/portfolio/state.py` that walks `executions.jsonl` and computes current `positions` dict (last fill per symbol becomes the current target) plus implied `cash_pct = 1 - sum(abs(positions))`. This is also the foundation [ADR-0035](ADR-0035-portfolio-rebalancing.md) wave-4 needs and currently lacks.

---

## Verification

```python
from hermes_quant.risk.portfolio_normalize import (
    PortfolioCaps, PortfolioState, normalize_targets,
)

# Empty book, 43 picks at ±0.20 → scale-to-fit Policy A
caps = PortfolioCaps(max_gross_exposure_pct=2.0, max_net_exposure_pct=1.0, min_cash_reserve_pct=0.20)
empty_book = PortfolioState(positions={}, cash_pct=1.0)
demanded = [(f"SYM{i}", -0.20 if i < 38 else 0.20) for i in range(43)]
result = normalize_targets(demanded, empty_book, caps)

gross = sum(abs(t) for _, t, _ in result)
net   = sum(t       for _, t, _ in result)
assert gross <= 2.0 + 1e-9
assert -1.0 <= net <= 1.0 + 1e-9    # net-short bounded
# All scale_factors equal under Policy A
scales = {round(s, 6) for _, _, s in result}
assert len(scales) == 1
assert 0.18 < next(iter(scales)) < 0.19   # ~0.186 expected

# Already-breached book → silence everything
breached = PortfolioState(positions={"X": -8.6}, cash_pct=-7.6)   # 860% gross
result = normalize_targets(demanded, breached, caps)
assert all(abs(t) < 1e-9 for _, t, _ in result)

# Cap-not-binding case (1 pick at 20%): pass-through
result = normalize_targets([("AAPL", 0.20)], empty_book, caps)
assert abs(result[0][1] - 0.20) < 1e-9
assert abs(result[0][2] - 1.0) < 1e-9
```

Operational verification post-merge:

```bash
~/.hermes/hermes-agent/venv/bin/python3 -c "
import json
from pathlib import Path
fills = [json.loads(l) for l in Path('~/.hermes/quant/executions.jsonl').expanduser().read_text().splitlines() if l.strip()]
gross = sum(abs(f['target_position_pct']) for f in fills)
net   = sum(f['target_position_pct'] for f in fills)
print(f'gross={gross*100:.1f}%  net={net*100:+.1f}%')
"
# Pre-fix: gross=860.0%  net=-660.0%
# Post-fix on a fresh book at the new caps: gross<=200% net within [-100,+100]
```
