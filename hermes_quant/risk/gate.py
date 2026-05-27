"""hermes_quant.risk.gate — Concrete risk gate implementation.

Per ADR-0004 + ADR-0009 §P0-1 + §P0-5 + synthesis-v2 §P0-A:

Sequence (HIGHEST priority FIRST per ADR-0009 §P0-5):
  Rule 0: halt check (any active halt covering scope → silence)
  Rule 1: drawdown circuit breaker (>max_drawdown_pct → flatten + halt)
  Rule 2: daily-loss circuit breaker (>max_daily_loss_pct → flatten + halt-until-session)
  Rule 3: silence on flat or zero-confidence signal
  Rule 4: post-loss cooldown (last loss < cooldown_minutes → silence)
  Rule 5: cost gate (|expected_signed_edge| < cost_multiple × round_trip_cost → silence)
  Rule 6: position size from quarter-Kelly (uses expected_signed_edge / σ²)
  Rule 7: minimum-trade-size guard (|delta| < min_trade_size → silence)

Per synthesis-v2 §P0-A: BOTH the cost-gate AND the Kelly sizer use the
SAME expected_signed_edge formula (single source of truth from
hermes_quant.risk.kelly).

Per ADR-0004 §Configuration profiles: ships three named profiles
(conservative, moderate, aggressive) loaded from
~/.hermes/config.yaml::quant.risk.profile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    HaltState,
    MarketState,
    Portfolio,
)
from hermes_quant.risk.kelly import (
    cost_gate_threshold,
    expected_signed_edge,
    quarter_kelly_size,
)

logger = logging.getLogger(__name__)


def _emit_audit(
    *,
    kind: str,
    asof: datetime,
    payload: dict[str, Any],
) -> None:
    """Emit a governance audit event. Failures are swallowed (silence-by-default
    for observation — audit must NEVER block a gate decision).
    """
    try:
        from hermes_quant.governance import audit_log

        audit_log.append(
            audit_log.GovernanceEvent(
                kind=kind,  # type: ignore[arg-type]
                asof=asof,
                source="risk.gate",
                payload=payload,
            )
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("audit_log.append failed for %s: %s", kind, e)


def _build_signal_provenance(signal: AggregatedSignal) -> dict[str, Any]:
    """Build the signal_provenance block for audit-log payloads (ADR-0041).

    The block carries the discriminative metadata required to detect
    BMA-degeneracy retroactively from the audit trail alone, replacing the
    out-of-band recommend()-reprobe pattern that surfaced during the
    2026-05-26 BMA n=1 collapse incident.

    All fields default to None when the underlying signal does not produce
    them (e.g., a signal from a pre-Wave-1 aggregator that doesn't expose
    bma_weights). The fields that ARE always derivable from the
    AggregatedSignal protocol (n_views, n_distinct_analysts,
    contributing_analysts, aggregator_class) MUST be populated and are not
    nullable. Tests guard this contract.

    Per ADR-0041: this is the canonical predicate input. The
    `is_bma_degenerate(event)` helper in
    `hermes_quant.governance.audit_log_query` consumes this block.
    """
    components = signal.components or ()
    md = dict(signal.metadata or {})

    contributing_analysts = sorted({v.analyst for v in components})
    analyst_view_ids: list[str] = []
    for v in components:
        v_md = dict(v.metadata or {})
        # Per-view stable ID — present on Wave-1+ analyst views; falls back
        # to the analyst-class-name when absent so the field is always
        # populated for cross-referencing.
        vid = v_md.get("view_id")
        if vid:
            analyst_view_ids.append(str(vid))
        else:
            analyst_view_ids.append(f"{v.analyst}:{v.horizon}")

    # data_quality may live on the signal itself or on the aggregator
    # metadata. Prefer the signal-level field if present; otherwise fall
    # back to metadata; otherwise None.
    # Cross-model review M5: explicit None-check rather than `or`, so that
    # a legitimate falsy data_quality value (e.g. {"score": 0.0}) is not
    # silently replaced by the metadata fallback.
    sig_dq = getattr(signal, "data_quality", None)
    dq = sig_dq if sig_dq is not None else md.get("data_quality")

    return {
        "n_views": len(components),
        "n_distinct_analysts": len(set(contributing_analysts)),
        "contributing_analysts": contributing_analysts,
        "vote_share": md.get("vote_share"),
        "n_contributing": md.get("n_contributing"),
        "bma_weights": md.get("bma_weights"),
        "aggregator_class": signal.aggregator,
        "analyst_view_ids": analyst_view_ids,
        "data_quality": dq,
    }


def _ts_to_datetime(ts: pd.Timestamp | datetime) -> datetime:
    """Coerce pd.Timestamp or datetime to a tz-aware UTC datetime."""
    if isinstance(ts, pd.Timestamp):
        py = ts.to_pydatetime()
    else:
        py = ts
    if py.tzinfo is None:
        py = py.replace(tzinfo=UTC)
    return py


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    """Per ADR-0004 + ADR-0009 §P0-5."""

    max_position_pct: float = 0.20
    """Hard cap on absolute target position fraction. Default 20% NAV."""

    action_step: float = 0.05
    """Discrete action step (anti-leverage-gambling). Positions in
    {0, ±0.05, ±0.10, ±0.15, ±0.20} of NAV."""

    cost_multiple: float = 2.0
    """Edge must be ≥ N × round-trip transaction cost."""

    max_drawdown_pct: float = 0.15
    """Drawdown circuit breaker — flatten + durable halt above this."""

    max_daily_loss_pct: float = 0.05
    """Daily-loss circuit breaker — flatten + halt-until-session."""

    min_trade_size: float = 0.02
    """Minimum |target - current| to act on (anti-churn)."""

    quarter_kelly: float = 0.25
    """Kelly multiplier (0.25 = quarter-Kelly per literature consensus)."""

    cooldown_after_loss_minutes: int = 60
    """Cooldown window after a realized loss (heuristic; v0.2 may
    config-default-off)."""

    paper_zero_costs: bool = False
    """PAPER-MODE-ONLY override: when True, the cost-gate threshold is
    forced to 0.0 (skipping the `cost_multiple × round_trip_cost` check)
    while preserving the edge-sign alignment guard.

    Rationale (per docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md):
    Alpaca paper trading has zero real fees and only simulated slippage.
    The default `2× round_trip_cost` buffer is artificially conservative
    on paper while the calibrator is cold-starting and can't yet emit
    edges large enough to clear the live-mode threshold. This unblocks
    paper-mode learning without touching live behavior.

    Discipline:
      - Default False (conservative; live-mode behavior unchanged).
      - The edge-sign alignment guard (`edge * direction <= 0`) is NEVER
        bypassed — silence-by-default still wins on negative-edge signals.
      - Live-mode invocation with this flag must fail closed; the
        autonomous loop enforces that invariant before reaching the gate.
    """

    @classmethod
    def conservative(cls) -> RiskConfig:
        return cls(
            max_position_pct=0.10,
            action_step=0.05,
            cost_multiple=3.0,
            max_drawdown_pct=0.10,
            max_daily_loss_pct=0.03,
        )

    @classmethod
    def moderate(cls) -> RiskConfig:
        return cls()  # all defaults

    @classmethod
    def aggressive(cls) -> RiskConfig:
        return cls(
            max_position_pct=0.40,
            action_step=0.10,
            cost_multiple=1.5,
            max_drawdown_pct=0.20,
            max_daily_loss_pct=0.10,
        )


PROFILES = {
    "conservative": RiskConfig.conservative,
    "moderate": RiskConfig.moderate,
    "aggressive": RiskConfig.aggressive,
}


# ---------------------------------------------------------------------------
# Per-asset state (cooldown timers, last-loss tracking)
# ---------------------------------------------------------------------------


@dataclass
class _AssetCooldownState:
    """Cooldown timers per (account, asset_class, asset)."""

    last_loss_at: pd.Timestamp | None = None


# ---------------------------------------------------------------------------
# DefaultRiskGate
# ---------------------------------------------------------------------------


class DefaultRiskGate:
    """Concrete risk gate implementation.

    Implements the RiskGate Protocol from hermes_quant.protocol.

    Per synthesis-v2 §P0-A: cost gate AND Kelly sizer use expected_signed_edge.
    Per synthesis-v2 §P0-D ordering: halt FIRST, then any other check.
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        evidence_store: Any = None,
    ):
        """
        Args:
            config: Risk profile (defaults to moderate).
            evidence_store: Optional EvidenceStore-like object (must expose
                `.get(evidence_id)` returning a row dict with `available_at`).
                When provided, the gate runs the universal lookahead check
                (ADR-0033 D5) against component AnalystViews' evidence_ids
                BEFORE other rules, and silences signals whose evidence is
                tainted by data the gate could not have seen at `signal.asof`.
                When None (default), the lookahead check is skipped — preserves
                backward compatibility with existing tests.
        """
        self.config = config or RiskConfig()
        self.evidence_store = evidence_store
        self._cooldowns: dict[tuple[str, str, str], _AssetCooldownState] = {}
        # Action stats for observability
        self._n_actions = 0
        self._n_silenced_halt = 0
        self._n_silenced_drawdown = 0
        self._n_silenced_daily_loss = 0
        self._n_silenced_flat = 0
        self._n_silenced_cooldown = 0
        self._n_silenced_cost_gate = 0
        self._n_silenced_min_trade = 0
        self._n_silenced_lookahead = 0

    def _audit_rejection(self, signal: AggregatedSignal, reason: str) -> None:
        """Emit a 'gate_rejection' audit event. Failures are swallowed."""
        _emit_audit(
            kind="gate_rejection",
            asof=_ts_to_datetime(signal.asof),
            payload={
                "asset": signal.asset,
                "direction": int(signal.direction),
                "magnitude": float(signal.magnitude),
                "confidence": float(signal.confidence),
                "reason": reason,
                "asof": signal.asof.isoformat(),
                "signal_provenance": _build_signal_provenance(signal),
            },
        )

    def _audit_approval(self, signal: AggregatedSignal, action: Action) -> None:
        """Emit a 'gate_approval' audit event. Failures are swallowed."""
        _emit_audit(
            kind="gate_approval",
            asof=_ts_to_datetime(signal.asof),
            payload={
                "asset": signal.asset,
                "direction": int(signal.direction),
                "magnitude": float(signal.magnitude),
                "confidence": float(signal.confidence),
                "target_position_pct": float(action.target_position_pct),
                "reason": action.reason,
                "asof": signal.asof.isoformat(),
                "signal_provenance": _build_signal_provenance(signal),
            },
        )

    def _silence(self, signal: AggregatedSignal, *, reason: str) -> None:
        """Internal helper: emit gate_rejection audit and return None."""
        self._audit_rejection(signal, reason)
        return None

    def gate(
        self,
        signal: AggregatedSignal,
        market: MarketState,
        portfolio: Portfolio,
        halt_state: HaltState,
    ) -> Action | None:
        """Enforce the 8-rule sequence. Returns None for silence."""

        # Rule 0: Halt check (HIGHEST PRIORITY per synthesis-v2 §P0-D ordering)
        if halt_state.is_halted(portfolio.account_id, portfolio.asset_class, signal.asset):
            self._n_silenced_halt += 1
            return self._silence(signal, reason="halt_active")

        # Rule 0.5: Lookahead-evidence check (ADR-0033 D5).
        # Drop signals whose component AnalystViews cite evidence that wasn't
        # available at signal.asof. Only runs when an evidence_store was
        # injected at construction; otherwise this is a no-op (backward
        # compat with tests that don't set up an evidence store).
        if self.evidence_store is not None and signal.components:
            from hermes_quant.evidence.lookahead_gate import check_view_lookahead

            asof_dt = _ts_to_datetime(signal.asof)
            for view in signal.components:
                if not view.evidence_ids:
                    continue
                result = check_view_lookahead(view, asof_dt, self.evidence_store)
                if not result.ok:
                    self._n_silenced_lookahead += 1
                    return self._silence(
                        signal,
                        reason=f"lookahead_tainted_{result.violations[0].evidence_id}",
                    )

        # Rule 1: Drawdown circuit breaker
        if portfolio.drawdown_pct > self.config.max_drawdown_pct:
            self._n_silenced_drawdown += 1
            action = Action(
                target_position_pct=0.0,
                reason=f"drawdown_circuit_breaker_{portfolio.drawdown_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=None,  # explicit resume only
            )
            self._audit_rejection(signal, action.reason)
            return action

        # Rule 2: Daily-loss circuit breaker
        if portfolio.daily_loss_pct > self.config.max_daily_loss_pct:
            self._n_silenced_daily_loss += 1
            action = Action(
                target_position_pct=0.0,
                reason=f"daily_loss_circuit_breaker_{portfolio.daily_loss_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=_next_session_open(market.tz, portfolio.asof),
            )
            self._audit_rejection(signal, action.reason)
            return action

        # Rule 3: Silence on flat or zero-confidence signal
        if signal.direction == 0 or signal.confidence < 1e-6:
            self._n_silenced_flat += 1
            return self._silence(signal, reason="flat_or_zero_confidence")

        # Rule 4: Post-loss cooldown
        cooldown_key = (portfolio.account_id, portfolio.asset_class, signal.asset)
        cooldown = self._cooldowns.get(cooldown_key)
        if cooldown is not None and cooldown.last_loss_at is not None:
            elapsed_minutes = (portfolio.asof - cooldown.last_loss_at).total_seconds() / 60.0
            if elapsed_minutes < self.config.cooldown_after_loss_minutes:
                self._n_silenced_cooldown += 1
                return self._silence(signal, reason="post_loss_cooldown")

        # Rule 5: Cost gate (synthesis-v2 §P0-A: uses expected_signed_edge)
        edge = expected_signed_edge(
            direction=signal.direction,
            probability=signal.confidence,
            magnitude=abs(signal.magnitude),
        )
        # PAPER-MODE-ONLY override (per docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md):
        # when `paper_zero_costs=True`, the threshold is forced to 0.0
        # INSTEAD of computing `cost_multiple × round_trip_cost` from
        # market.commission/spread/slippage. Paper accounts (Alpaca) have
        # zero real fees and only simulated slippage, so the live buffer
        # is artificially conservative on paper. Live behavior is
        # unchanged: this branch is only ever reached when an explicit
        # config flag is set, and the autonomous loop fails closed if
        # the active reactor is not 'paper'. The edge-sign alignment
        # guard below is NEVER bypassed.
        if self.config.paper_zero_costs:
            threshold = 0.0
        else:
            threshold = cost_gate_threshold(
                market_commission=market.commission,
                market_spread=market.spread,
                market_slippage=market.slippage_estimate,
                cost_multiple=self.config.cost_multiple,
            )
        # Phase-8 P0-B (synthesis 2026-05-13): edge-sign alignment guard.
        # `expected_signed_edge` returns positive when the signal's
        # direction-weighted expected return is favorable, negative when
        # adverse. With cold-start calibration shrinkage of 0.20, raw
        # confidence 0.55 emits effective confidence 0.35 → for a
        # signal.direction=+1, expected_signed_edge becomes NEGATIVE. Without
        # this guard, the threshold check `abs(edge) < threshold` allows
        # negatively-edged signals to pass when |edge| is large enough, and
        # the Kelly sizer then multiplies the negative edge through to
        # produce a target_size with the WRONG sign — emitting an action
        # opposite to the requested direction.
        #
        # Silence whenever the signed edge does not agree with the requested
        # direction. This is the silence-by-default discipline: if the
        # calibrated probability says we don't actually have a positive
        # expected return in the requested direction, we hold cash.
        if edge * signal.direction <= 0:
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="cost_gate_edge_sign")
        if abs(edge) < threshold:
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="cost_gate_below_threshold")

        # Rule 6: Position size from quarter-Kelly
        # variance = volatility² (volatility per ADR-0009 §P0-1 fix is stdev)
        variance = market.volatility**2
        target_size = quarter_kelly_size(
            edge=edge,
            variance=variance,
            quarter_kelly=self.config.quarter_kelly,
            max_position_pct=self.config.max_position_pct,
            action_step=self.config.action_step,
            direction=signal.direction,
        )

        # Rule 7: Minimum trade size guard (anti-churn)
        current = portfolio.current_position_pct(signal.asset)
        delta = target_size - current
        if abs(delta) < self.config.min_trade_size:
            self._n_silenced_min_trade += 1
            return self._silence(signal, reason="min_trade_size")

        self._n_actions += 1
        action = Action(
            target_position_pct=target_size,
            reason=(
                f"signal_dir={signal.direction}_conf={signal.confidence:.3f}_"
                f"edge={edge:.5f}_kelly_size={target_size:.3f}"
            ),
            signal_id=signal.metadata.get("id") if signal.metadata else None,
            halt=False,
        )
        self._audit_approval(signal, action)
        return action

    def record_loss(
        self,
        account_id: str,
        asset_class: str,
        asset: str,
        loss_at: pd.Timestamp,
    ) -> None:
        """Settlement loop calls this on a realized loss to start cooldown."""
        key = (account_id, asset_class, asset)
        if key not in self._cooldowns:
            self._cooldowns[key] = _AssetCooldownState()
        self._cooldowns[key].last_loss_at = loss_at

    def stats(self) -> dict:
        return {
            "n_actions": self._n_actions,
            "n_silenced_halt": self._n_silenced_halt,
            "n_silenced_drawdown": self._n_silenced_drawdown,
            "n_silenced_daily_loss": self._n_silenced_daily_loss,
            "n_silenced_flat": self._n_silenced_flat,
            "n_silenced_cooldown": self._n_silenced_cooldown,
            "n_silenced_cost_gate": self._n_silenced_cost_gate,
            "n_silenced_min_trade": self._n_silenced_min_trade,
            "n_silenced_lookahead": self._n_silenced_lookahead,
        }


def _next_session_open(tz: str, now: pd.Timestamp) -> pd.Timestamp:
    """Next session open per asset's tz. UTC (24/7 crypto) → 0000 next day.

    For non-UTC tz (e.g. equities at 'America/New_York'), v0.1.1 returns
    `now + 24h` rather than `next-UTC-day midnight`. Per Phase-8 P1-δ
    (synthesis 2026-05-13): the previous next-UTC-day-normalize approach
    had a bug where a circuit breaker tripped at 14:00 ET would resolve
    to next-UTC-day 00:00 = 19:00 ET SAME day, and `auto_clear_expired`
    would lift the halt during after-hours. Returning `now + 24h` bounds
    the halt by ~one full session regardless of trip time, eliminating
    that re-trip risk window. v0.1.2 will use `trading_calendars` for
    proper session boundaries (next 09:30 ET / 09:00 LSE / etc.).
    """
    # Crypto: next UTC day 0000 (sessionless 24/7 → midnight is fine)
    if tz.upper() == "UTC":
        next_day = (now + pd.Timedelta(days=1)).normalize()
        return next_day
    # Non-UTC tz (equities, futures with sessions): + 24 hours, NOT
    # normalize-to-midnight. This guarantees at least one full elapsed
    # session before the auto-clear-expired path can fire.
    return now + pd.Timedelta(days=1)
