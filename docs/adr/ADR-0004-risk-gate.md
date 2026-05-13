# ADR-0004: Risk gate — deterministic rules, silence-by-default

**Status**: proposed
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
