# ADR-0004: Risk gate — deterministic rules, silence-by-default

**Status**: Accepted (2026-05-12), implemented
**Date**: 2026-05-12

## Context

The risk gate sits between the aggregator and the signal bus. Its job: convert `AggregatedSignal → Action | None` under hard, deterministic rules that the aggregator (RL or otherwise) cannot circumvent.

Per `docs/research/01-rl-for-trading.md` §2, two of the three classic RL-trading failure modes are reward hacking via leverage and churning for fees. Neither is solvable by clever reward shaping; both are best handled by hard rules at the action-emission boundary.

The "silence by default" prior comes from Eidolon's PDR architecture — gates init with negative bias, false-positive penalty heavily outweighs false-negative. For trading, this means: when uncertain, hold cash. This is the most underrated property of profitable systems.

## Decision

The risk gate enforces six rules in sequence. If any rule rejects the action, the gate emits no action (silence).

```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float = 0.20         # never more than 20% of NAV in one position
    action_step: float = 0.05              # discrete sizes 0, 0.05, 0.10, 0.15, 0.20
    cost_multiple: float = 2.0             # edge must be at least 2x transaction cost
    max_drawdown_pct: float = 0.15         # 15% from peak → flatten + halt
    max_daily_loss_pct: float = 0.05       # 5% in a session → halt until next session
    min_trade_size: float = 0.02           # don't churn — minimum delta to act
    quarter_kelly: float = 0.25            # Kelly multiplier (1.0 = full Kelly)
    cooldown_after_loss_minutes: int = 60  # cooldown after a losing trade

def gate(signal: AggregatedSignal, market: MarketState, portfolio: Portfolio,
         cfg: RiskConfig) -> Optional[Action]:
    # Rule 1: silence on flat or zero-confidence signal
    if signal.direction == 0 or signal.confidence < 1e-6:
        return None

    # Rule 2: drawdown circuit breaker (HIGHEST priority)
    if portfolio.drawdown_pct > cfg.max_drawdown_pct:
        return Action(target_position=0, reason="drawdown_circuit_breaker", halt=True)

    # Rule 3: daily loss circuit breaker
    if portfolio.daily_loss_pct > cfg.max_daily_loss_pct:
        return Action(target_position=0, reason="daily_loss_circuit_breaker",
                      halt_until=next_session_open(market.tz))

    # Rule 4: post-loss cooldown
    if portfolio.last_loss_minutes_ago < cfg.cooldown_after_loss_minutes:
        return None

    # Rule 5: transaction-cost-aware threshold
    expected_edge = abs(signal.magnitude) * signal.confidence
    transaction_cost = market.commission + 0.5 * market.spread + market.slippage_estimate
    if expected_edge < cfg.cost_multiple * transaction_cost:
        return None  # not worth the friction

    # Rule 6: position size from Kelly-fractional
    kelly_size = (signal.magnitude * signal.confidence) / max(market.volatility, 1e-4)
    target_size = signal.direction * min(cfg.max_position_pct,
                                          cfg.quarter_kelly * abs(kelly_size))
    target_size = round_to_step(target_size, cfg.action_step)
    target_size = clip(target_size, -cfg.max_position_pct, +cfg.max_position_pct)

    # Rule 7: minimum trade-size guard
    delta = target_size - portfolio.current_position
    if abs(delta) < cfg.min_trade_size:
        return None  # don't churn

    return Action(target_position=target_size,
                  reason=f"signal_dir={signal.direction}_conf={signal.confidence:.3f}")
```

### Discrete action space — anti-leverage-gambling

The action step (`0.05`) means positions are 0, ±5%, ±10%, ±15%, ±20% of NAV. This is the single biggest defense against the RL-trading reward-hacking pattern (per DeepSeek's research note `01-rl-for-trading.md` §5 gotcha #2). A continuous action space invites the agent to learn "always max-leverage in the direction of the slightest edge."

### Quarter-Kelly with floor

`kelly_size = magnitude * confidence / volatility`. The full-Kelly formula assumes accurate probability + return estimates; in practice these are noisy, so the literature consensus is to size at quarter-Kelly. The position cap (`max_position_pct=0.20`) further floors against pathological signal conditions.

### Hard rules, not learned

The RL aggregator (v0.2) cannot loosen these. The aggregator emits `AggregatedSignal`; the gate decides whether to act. This separation is critical — without it, a learned aggregator will eventually figure out that emitting unrealistic confidence values lets it bypass the cost threshold.

### Configuration profiles

`~/.hermes/config.yaml::quant.risk` ships three named profiles:

```yaml
quant:
  risk:
    profile: conservative                  # active profile
    profiles:
      conservative:                         # for v0.1 paper-trade defaults
        max_position_pct: 0.10
        action_step: 0.05
        cost_multiple: 3.0
        max_drawdown_pct: 0.10
        max_daily_loss_pct: 0.03
      moderate:                             # default after 30 days of paper-trade
        max_position_pct: 0.20
        action_step: 0.05
        cost_multiple: 2.0
        max_drawdown_pct: 0.15
        max_daily_loss_pct: 0.05
      aggressive:                           # only for users with track record
        max_position_pct: 0.40
        action_step: 0.10
        cost_multiple: 1.5
        max_drawdown_pct: 0.20
        max_daily_loss_pct: 0.10
```

`hermes quant setup` defaults to `conservative`. Switching to `moderate`/`aggressive` is an explicit user opt-in and surfaces a confirmation prompt.

### Halt semantics

`Action(halt=True)` sets a flag in `~/.hermes/quant/state.json` that the daemon reads at the start of every tick. Halt persists across daemon restarts. `hermes quant resume` clears it (with a confirmation prompt and a warning). Halts are logged at WARN level and shown in `quant_doctor`.

## Consequences

### Positive

- Reward hacking via leverage is structurally impossible.
- Drawdown + daily-loss circuit breakers preserve capital during regime breaks.
- Cost-aware threshold prevents the most common death-by-friction pattern.
- Profile system gives a clear graduation path from paper-trade-conservative to live-aggressive.
- Halts persist across restarts — important when the daemon is auto-restarted by systemd after a panic.

### Negative

- Discrete action space loses information that a continuous policy could exploit. In practice, the literature suggests the loss is small (≤0.1 Sharpe) and the safety gain is large.
- Kelly-fractional with hardcoded multiplier doesn't adapt to regime. Quarter-Kelly is conservative across regimes; we accept the loss.
- Cooldown after loss is a heuristic. Some research argues cooldowns leave alpha on the table after losing trades. We ship it; v0.2 may make it config-default-off after track record.
- Daily-loss circuit breaker uses wall-clock days; for 24/7 crypto this is reset at UTC 0000. For equities it resets at session open. The asset's `tz` is the source of truth.

## Implementation notes

- `gate()` is pure given inputs. Tested via fixture-based unit tests (50+ cases covering all rule branches).
- `Portfolio.drawdown_pct` and `Portfolio.daily_loss_pct` are computed from the realized P&L log, not from broker-reported balance (broker balance lags). The settlement loop updates these.
- `MarketState.slippage_estimate` is a per-asset rolling average of (fill_price - decision_price) / decision_price. Bootstraps from a constant (5 bps for liquid crypto, 2 bps for liquid equities) until 30 days of fills exist.
- The risk gate is the LAST thing the daemon does before emitting a signal. Action records (including silence reasons) are persisted to `actions` SQLite table for `quant_show_signals` and post-hoc analysis.

## References

- `docs/research/01-rl-for-trading.md` §2, §5 — failure modes and counter-rules
- `docs/research/03-plugin-architecture.md` §5 — risk-gate sketch (this ADR formalizes it)
- Eidolon `AGENTS.md` — silence-by-default principle and 7-dim gate architecture (reference, not direct port)

---

## Amendment 2026-05-13: Edge-sign alignment + session-aware halt_until

**Status**: Part A accepted (shipped v0.1.1); Part B proposed (planned v0.1.2)
**Date**: 2026-05-13

### Context

Phase-8 review of v0.1.1 surfaced two related defects in the rule sequence above. Both touch the silence-by-default invariant — one was a live correctness bug (Part A, fixed in v0.1.1), the other is a semantic-precision gap that becomes visible once asset-tz coverage broadens beyond UTC crypto (Part B, slated for v0.1.2). Bundled here because they evolve the same canonical rule list and the v0.1.2 diff is small.

Both reviewers (Claude P2, DeepSeek P0) flagged Part A as the same root cause from different traces. Part B was raised under §P1-δ as a follow-on once the durable halt ordering (ADR-0009 §P0-D, extended in Phase-8 P0-C) made the `halt_until` value observable to the tick loop rather than ephemeral.

### Part A — Rule 5a edge-sign alignment guard (v0.1.1, shipped)

**The bug.** Under cold-start calibration shrinkage (λ=0.20 default per ADR-0002), an analyst's raw confidence is shrunk toward 0.50. A signal with raw `confidence=0.55` and `direction=+1` emits effective `confidence=0.35` — `expected_signed_edge` becomes **negative** in the requested direction. Rule 5's original `abs(edge) < threshold` check tests magnitude, not sign, so the signal passed. Rule 6's Kelly sizer then computed `target_size = direction * |kelly|` and produced a long target driven by a negative edge — or, equivalently, a SHORT action emitted while the analyst had aggregated to LONG.

This is a silent silence-by-default violation: the gate emitted the wrong-direction action instead of holding cash, and the persisted `actions` row was indistinguishable from a genuine reversal signal.

**The fix.** A new intermediate rule between Rule 5 (cost-gate threshold) and Rule 6 (Kelly sizer):

```python
# Rule 5a: edge-sign alignment guard
if expected_signed_edge * signal.direction <= 0:
    return None  # calibrated edge does not support the requested direction
```

The condition is `<= 0` (not `< 0`): a zero-edge signal in either direction also silences, consistent with the Rule 3 zero-confidence guard.

**Implementation**: `hermes_quant/risk/gate.py:229-231`.

**Tests**: `tests/unit/test_risk_gate.py::TestRule5CostGate::{test_negatively_edged_long_signal_silenced, test_negatively_edged_short_signal_silenced, test_positively_edged_signal_still_passes}`.

### Part B — `trading_calendars` for `halt_until` (v0.1.2, planned)

**The current naive code.** Rule 2 (daily-loss circuit breaker) sets `halt_until` via `_next_session_open(market.tz, portfolio.asof)`. v0.1.1 ships a coarse two-branch approximation (`hermes_quant/risk/gate.py:300-310`):
- `tz == UTC` → next UTC midnight (correct for 24/7 crypto)
- otherwise → `now + 24h` (conservative wall-clock fallback)

This is correct enough to ship — the fallback is never *less* than 24h, so the breaker cannot under-halt — but it is not session-aware. An equity breaker tripped at 14:00 ET auto-clears at 14:00 ET next day (mid-session, acceptable); the same trip at 16:00 ET (EOD) auto-clears at 16:00 ET next day, which is 6.5h post-open of the following session. The halt-until semantics are not predictable across asset classes.

**The v0.1.2 plan.** Adopt `trading-calendars` (or its successor `exchange-calendars`) for proper session boundaries:

```python
# Rule 2 halt_until — v0.1.2 implementation sketch
def _next_session_open(tz: str, asof: datetime) -> datetime:
    try:
        cal = get_calendar(_TZ_TO_CALENDAR[tz])  # America/New_York → XNYS, Europe/London → XLON, ...
        return cal.next_open(asof)               # strictly after asof
    except (KeyError, ImportError, CalendarError):
        return asof + timedelta(hours=24)        # v0.1.1 conservative fallback preserved
```

Mapping table lives in `hermes_quant/risk/_calendars.py`. Crypto (`tz=UTC` with `asset_class=crypto`) bypasses the calendar lookup and keeps the next-UTC-midnight behavior — markets are 24/7, but the daily-loss reset still respects the configured session boundary.

**Fallback discipline.** Any failure path (calendar missing, tz unmapped, library import failure, `next_open` raises) falls through to `now + 24h`. This preserves the v0.1.1 invariant: `halt_until` is **never less than 24h from the trip**. We do not narrow halts under uncertainty.

**Dependency.** Adds `trading-calendars >= 4.0` (or `exchange-calendars >= 4.5`, TBD during v0.1.2 spike) to `pyproject.toml [project.optional-dependencies] risk` extras group. The `risk` extra is already required for live trading; backtest-only installs still resolve.

**Backward compat.** Rule 2's emit signature is unchanged — `Action(target_position=0, reason="daily_loss_circuit_breaker", halt=True, halt_until=<datetime>)`. Only the `halt_until` value differs. Existing consumers (notably the tick loop's durable-halt installer per Phase-8 P0-C) work unchanged.

### Updated rule sequence

The canonical sequence after both amendments. This supersedes the six-rule list in the original Decision section:

- **Rule 0** — halt check (silence if scope is halted; reads `state.json`)
- **Rule 1** — drawdown circuit breaker (`drawdown_pct > max_drawdown_pct` → flatten + `Action(halt=True)`)
- **Rule 2** — daily-loss circuit breaker (`daily_loss_pct > max_daily_loss_pct` → flatten + `Action(halt=True, halt_until=…)`; `halt_until` from `trading_calendars` next session open in v0.1.2, `now + 24h` fallback)
- **Rule 3** — silence on flat or zero-confidence (`direction == 0 or confidence < 1e-6`)
- **Rule 4** — post-loss cooldown (`last_loss_minutes_ago < cooldown_after_loss_minutes` → silence)
- **Rule 5** — cost-gate threshold (`|expected_signed_edge| < cost_multiple × round_trip_cost` → silence)
- **Rule 5a** — *NEW* edge-sign alignment guard (`expected_signed_edge * signal.direction <= 0` → silence)
- **Rule 6** — position size from quarter-Kelly (with `max_position_pct` cap and `action_step` rounding)
- **Rule 7** — minimum trade-size guard (`|delta_pct| < min_trade_size` → silence)

Numbering preserved across the amendment to keep test names and log breadcrumbs stable. Rule 5a is intentionally a sub-numbered insertion rather than a renumber.

### Cross-cuts

- **ADR-0002 (Analyst Protocol)** — cold-start calibration shrinkage is the *cause* of the negative-signed-edge case Rule 5a defends against. The ADR-0002 shrinkage default (λ=0.20) is unchanged; Rule 5a is its consequence at the gate boundary.
- **ADR-0003 amendment (calibration_quality lifecycle)** — once v0.1.2 lifts the calibrator readiness gate, calibrated edges become more accurate and Rule 5a should fire less often in steady state. It still fires defensively; do not remove it on accuracy improvements.
- **ADR-0009 §P0-D (durable halt ordering)** — unchanged in scope, but Phase-8 P0-C extends the ordering rule to the tick loop's circuit-breaker halt installer (the consumer of Rule 2's `halt_until`). Cross-link maintained.
- **ADR-0011 (portfolio reconstruction)** — Rule 1's drawdown computation depends on accurate equity reconstruction. This amendment does not modify equity inputs; ADR-0011 invariants stand.

### Provenance

Phase-8 synthesis: `docs/reviews/2026-05-13-v0.1.1-phase8/synthesis.md` §P0-B (Part A — Claude P2 and DeepSeek P0 converged on the same root cause from different traces) and §P1-δ (Part B). Implementations: `hermes_quant/risk/gate.py:229-231` (Part A, shipped) and `hermes_quant/risk/gate.py:300-310` (Part B's current naive `_next_session_open`, to be replaced in v0.1.2).

---

## Amendment 2026-05-26: Paper-mode-only cost-gate override

**Status**: accepted (paper-only)
**Date**: 2026-05-26

### Context

Per `docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md`, the autonomous tick on Alpaca paper has been emitting near-zero fires while the calibrator is cold-starting. Diagnosis: with default `cost_multiple=2.0` against the bootstrap `commission + 0.5×spread + slippage_estimate` round-trip, the live cost-gate threshold is several basis points — but on Alpaca **paper** trading the real fees are zero and slippage is simulated, so the buffer is artificially conservative. The aggregator can't yet emit edges large enough to clear it.

This is a paper-only inefficiency. Live trading must continue to enforce the full friction-aware threshold; defects there subtract from real capital. But the discipline asymmetry is clear: silence-by-default applies to **putting capital at risk**, not to refusing to learn on paper.

### Decision (paper-only)

Add a single config flag, default off, to bypass the cost-gate threshold on paper accounts only. The edge-sign alignment guard (Rule 5a) is **never** bypassed.

```yaml
quant:
  risk:
    paper_zero_costs: false   # default; conservative
    # Set to `true` ONLY when the active autonomous reactor is 'paper'.
    # The autonomous loop fails closed (raises ValueError) if a non-paper
    # reactor is ever invoked while this flag is set.
```

When `paper_zero_costs=true`:

- Rule 5's threshold is forced to `0.0` instead of `cost_multiple × round_trip_cost`. The `|expected_signed_edge| < threshold` check still runs against `0.0` — meaning any positive-magnitude signed edge clears the threshold, but exactly-zero edges still silence (consistent with the Rule 3 zero-confidence guard).
- Rule 5a (`expected_signed_edge × signal.direction <= 0` → silence) is **untouched**. Negative-signed-edge signals continue to be silenced. The override widens the cost-gate's threshold ONLY; it does not weaken the sign discipline.
- All other rules (halt, drawdown, daily-loss, post-loss cooldown, Kelly sizer caps, action_step rounding, min_trade_size churn guard) are untouched.

### Discipline guards

1. **Default off.** `RiskConfig().paper_zero_costs is False`. A repo without explicit YAML opt-in behaves identically to v0.1.1.
2. **Paper-only invariant in the autonomous loop.** `hermes_quant/autonomous.py::_react()` raises `ValueError("paper_zero_costs is set but reactor is not paper")` if the flag is true and the active reactor's `name != "paper"`. This is the fail-closed guard against accidental live-mode invocation.
3. **No widening of any other action-space limit.** `max_position_pct`, `action_step`, `min_trade_size`, `quarter_kelly`, the drawdown/daily-loss circuit breakers, and the post-loss cooldown all retain their existing values regardless of the flag.
4. **Edge-sign guard preserved.** The Phase-8 P0-B amendment (Rule 5a) is what protects against negative-edge sign-flip; that protection is upstream of the cost-gate threshold check and runs in **both** branches.

### Updated rule sequence (no renumber)

The canonical sequence from the 2026-05-13 amendment is unchanged. Rule 5's threshold computation is the only line that branches on `paper_zero_costs`; the sequence, naming, and silence-by-default ordering are stable.

### Negative consequences accepted

- A paper-mode tick can fire on tiny positive-edge signals that would never clear the live cost gate. This is intended — the paper run's purpose is to feed the calibrator, not to optimize paper P&L.
- Paper-mode fire counts and live-mode fire counts are no longer apples-to-apples. Any analytics that compare them must filter on the `paper_zero_costs` flag (recorded in the action audit payload via the existing `gate_approval` event).

### Implementation references

- `hermes_quant/risk/gate.py` — `RiskConfig.paper_zero_costs` (dataclass field + docstring) and the threshold branch inside `DefaultRiskGate.gate()`.
- `hermes_quant/autonomous.py::_read_safety_rails` — reads `quant.risk.paper_zero_costs` from config.
- `hermes_quant/autonomous.py::_react` — fail-closed guard against non-paper reactors.
- `tests/unit/test_paper_zero_costs.py` — four tests covering default-off, threshold-zeroing, edge-sign-guard preservation, and the cold-start clearing behavior.
