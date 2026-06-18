"""hermes_quant.pdr_core.gate — the host-agnostic DefaultRiskGate (ADR-0092 Inc-1-cont, STAGE 3).

A VERBATIM port of ``hermes_quant.risk.gate.DefaultRiskGate`` (ADR-0004 — the
FINAL money-safety authority) onto the host-blind read-interfaces in
:mod:`hermes_quant.pdr_core.gate_types`. SAME rule sequence (Rule 0..Rule 7),
SAME arithmetic, SAME numbers, SAME reason strings as the live gate. The only
changes are behavior-preserving severances of host imports:

  (a) AUDIT is an INJECTED sink (``audit_sink``) defaulting to a no-op. The live
      gate's ``_emit_audit`` is already best-effort and swallows every failure,
      so a no-op default is byte-identical on the decision path. The shell wires
      a real sink that forwards to ``governance.audit_log`` if it wants audit.
  (b) EVENT-RISK is a flag on :class:`RiskConfig` (``event_risk_enabled``,
      default ``False``) instead of ``os.environ.get("HERMES_QUANT_EVENT_RISK")``.
      Same default-off posture; the shell flips the flag from the env if it wants.
  (c) The ADR-0033 LOOKAHEAD/EVIDENCE check is DROPPED TO THE SHELL. The core
      gate imports NO evidence module and consumes a PRE-FILTERED
      :class:`~hermes_quant.pdr_core.gate_types.CoreSignal` (no ``components``
      surface). A host shell runs its own lookahead filter upstream.

The gate RETURNS a :class:`~hermes_quant.pdr_core.gate_types.GateDecision`
carrying the FULL durable-HALT triple (``halt`` / ``halt_scope`` / ``halt_until``),
NOT a bare ``Proposal`` — collapsing onto ``Proposal`` would silently DROP the
Rule-1/Rule-2 circuit-breaker halt = a money-safety regression. ``None`` means
silence.

Sequence (HIGHEST priority FIRST per ADR-0009 §P0-5):
  Rule 0: halt check (any active halt covering scope → silence)
  Rule 1: drawdown circuit breaker (>max_drawdown_pct → flatten + halt)
  Rule 2: daily-loss circuit breaker (>max_daily_loss_pct → flatten + halt-until-session)
  Rule 3: silence on flat or zero-confidence signal
  Rule 3.5: ADR-0084 pre-event blackout (default-OFF, additive)
  Rule 4: post-loss cooldown (last loss < cooldown_minutes → silence)
  Rule 5: cost gate (|expected_signed_edge| < cost_multiple × round_trip_cost → silence)
  Rule 6: position size from quarter-Kelly (uses expected_signed_edge / σ²)
  Rule 7: minimum-trade-size guard (|delta| < min_trade_size → silence)

PURITY: stdlib + pandas only. Imports the PURE kelly leaves from
``hermes_quant.pdr_core.kelly`` and the read-interfaces from
``hermes_quant.pdr_core.gate_types``. No host/infra import — the purity gate
(``tests/pdr_core/test_contract_purity.py``) stays green.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from hermes_quant.pdr_core.gate_types import (
    CoreMarketState,
    CorePortfolio,
    CoreSignal,
    GateDecision,
)
from hermes_quant.pdr_core.kelly import (
    cost_gate_threshold,
    expected_signed_edge,
    quarter_kelly_size,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit sink — coupling edit (a). Injected, defaults to a no-op.
# ---------------------------------------------------------------------------


class AuditSink(Protocol):
    """A best-effort governance audit sink. The gate calls it on every
    approval/rejection; the host shell wires one that forwards to
    ``governance.audit_log``. The core's default is a no-op.

    Mirrors the live ``_emit_audit`` signature (kind / asof / payload). Failures
    are swallowed by the gate (audit must NEVER block a decision)."""

    def __call__(self, *, kind: str, asof: datetime, payload: dict[str, Any]) -> None: ...


def _noop_audit_sink(*, kind: str, asof: datetime, payload: dict[str, Any]) -> None:
    """Default audit sink — does nothing. Byte-identical to the live gate's
    decision path (where ``_emit_audit`` is best-effort and swallowed)."""
    return None


# ---------------------------------------------------------------------------
# Pure leaves — VERBATIM from hermes_quant.risk.gate.
# ---------------------------------------------------------------------------


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _slippage_haircut_edge(edge: float, penalty_frac: Any) -> float:
    """Shrink ``|edge|`` toward silence by the conservative live-execution penalty.

    cut/01f0 (ADR-0097), the host-blind pure leaf. ``edge`` is the signed expected
    log return (a NAV-fraction); ``penalty_frac`` is the PRE-COMPUTED one-way
    live-vs-paper penalty (same units, passed IN from the shell — the estimator is
    NOT imported into the pure core). The penalty is ALWAYS a positive COST: its
    MAGNITUDE is subtracted from ``|edge|``, preserving sign, clamped at 0.

    HAIRCUT-TOWARD-SILENCE invariant: the result's magnitude is ``<= |edge|`` and
    its sign is unchanged — it can ONLY shrink edge, NEVER amplify it. FAIL-CLOSED
    (the ar08 finite-guard family): a non-finite ``edge`` OR a non-finite
    ``penalty_frac`` drives the edge to ``0.0`` (full silence), the conservative
    floor — a NaN/inf penalty must NEVER become a free pass that leaves edge intact.

    Pure; never raises.
    """
    if not _is_finite_number(edge):
        return 0.0
    pen = penalty_frac
    if not _is_finite_number(pen):
        # Unknown penalty => most conservative outcome: zero the edge (silence).
        return 0.0
    pen = abs(float(pen))
    e = float(edge)
    sign = 1.0 if e >= 0.0 else -1.0
    shrunk = abs(e) - pen
    if shrunk <= 0.0:
        return 0.0
    return sign * shrunk


def _ts_to_datetime(ts: Any) -> datetime:
    """Coerce pd.Timestamp or datetime to a tz-aware UTC datetime."""
    if isinstance(ts, pd.Timestamp):
        py = ts.to_pydatetime()
    else:
        py = ts
    if py.tzinfo is None:
        py = py.replace(tzinfo=UTC)
    return py


# ---------------------------------------------------------------------------
# ADR-0084: pre-event REJECT/abstain guard (default-OFF, additive)
# ---------------------------------------------------------------------------


def _parse_event_ts(s: Any) -> datetime | None:
    """Coerce an event timestamp (ISO string or datetime) to tz-aware UTC.

    Returns None on any failure — a malformed/missing scheduled_for can NEVER
    fabricate a blackout (ADR-0084 Negative-risk note: missing data => NO
    blackout, never invent one). Pure; never raises.
    """
    try:
        if isinstance(s, datetime):
            dt = s
        elif isinstance(s, str):
            v = s.strip()
            if not v:
                return None
            dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
        else:
            return None
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def in_event_blackout(
    event_risk: Mapping[str, Any] | None,
    *,
    asof: datetime,
    window_days: float,
    high_impact_only: bool = True,
) -> tuple[bool, str | None]:
    """Pure predicate: is `asof` inside the pre-event blackout window?

    Reads the asof-honest, outcome-free ``event_risk`` payload produced by the
    catalyst calendar wiring (already filtered to ``announced_at <=
    decision_asof`` upstream, so EXISTENCE was knowable at signal.asof; this
    predicate only inspects the FORWARD ``scheduled_for``).

    A blackout fires iff some event satisfies ALL of:
      * impact == "high" (when ``high_impact_only``; macro Tier-1 / earnings),
      * ``scheduled_for`` is FORWARD of (or equal to) ``asof``, and
      * ``scheduled_for - asof <= window_days`` (imminent).

    Returns ``(True, reason)`` on the FIRST qualifying event, else
    ``(False, None)``. A None/empty/malformed payload => ``(False, None)`` — the
    guard NEVER fabricates a blackout from missing data (ADR-0084 Negative).
    Pure; never raises; reads no env and no clock.
    """
    if not event_risk:
        return False, None
    events = event_risk.get("events") if isinstance(event_risk, Mapping) else None
    if not events:
        return False, None
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    horizon = asof + timedelta(days=window_days)
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        impact = str(ev.get("impact") or "").strip().lower()
        if high_impact_only and impact != "high":
            continue
        scheduled = _parse_event_ts(ev.get("scheduled_for"))
        if scheduled is None:
            continue  # missing/malformed schedule => never a blackout
        if scheduled < asof:
            continue
        if scheduled <= horizon:
            kind = str(ev.get("kind") or "event").strip().lower() or "event"
            return True, f"event_blackout_{kind}_high_impact"
    return False, None


def _next_session_open(tz: str, now: pd.Timestamp) -> pd.Timestamp:
    """Next session open per asset's tz. UTC (24/7 crypto) → 0000 next day.

    For non-UTC tz (e.g. equities at 'America/New_York'), returns ``now + 24h``
    rather than next-UTC-day midnight (Phase-8 P1-δ): a circuit breaker tripped
    at 14:00 ET must not resolve to 19:00 ET same day and get auto-cleared
    during after-hours. ``now + 24h`` bounds the halt by ~one full session
    regardless of trip time. VERBATIM from the live gate.
    """
    # Crypto: next UTC day 0000 (sessionless 24/7 → midnight is fine)
    if tz.upper() == "UTC":
        next_day = (now + pd.Timedelta(days=1)).normalize()
        return next_day
    # Non-UTC tz (equities, futures with sessions): + 24 hours, NOT
    # normalize-to-midnight.
    return now + pd.Timedelta(days=1)


# ---------------------------------------------------------------------------
# Configuration — VERBATIM RiskConfig + PROFILES (ADR-0004 numbers).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    """Per ADR-0004 + ADR-0009 §P0-5. Verbatim numbers from the live gate."""

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
    """Cooldown window after a realized loss."""

    event_risk_window_days: float = 1.0
    """ADR-0084 pre-event guard window (days FORWARD of signal.asof). Read ONLY
    when ``event_risk_enabled`` is True; default-off keeps behavior identical."""

    event_risk_enabled: bool = False
    """Coupling edit (b): replaces ``os.environ.get("HERMES_QUANT_EVENT_RISK")``.
    The ADR-0084 pre-event blackout guard runs ONLY when this is True. Default
    False = byte-identical to the env-absent live posture; the guard is ADDITIVE
    and can ONLY reject/abstain (never sizes, never blocks de-risking)."""

    paper_zero_costs: bool = False
    """PAPER-MODE-ONLY override: when True, the cost-gate threshold is forced to
    0.0 (skipping the ``cost_multiple × round_trip_cost`` check) while preserving
    the edge-sign alignment guard. Default False (live behavior unchanged)."""

    portfolio_variance_sizing_enabled: bool = False
    """aegis-ag01 (ADR-0096 Gate 1): when True,
    :meth:`DefaultRiskGate.apply_portfolio_variance_sizing` runs the
    correlation-aware POSITION-level haircut (``hermes_quant.pdr_core.portfolio_sizing``)
    so a basket's PORTFOLIO VARIANCE ``w^T Σ w`` stays within
    ``portfolio_variance_cap`` — not merely each |w_i| within the per-name cap.
    Default False = BYTE-IDENTICAL to 83bf280 (the basket method is a pass-through;
    the per-signal Rule-0..7 path is UNTOUCHED). The shell flips this from the env
    flag ``HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING`` (default-OFF + eval-gated). The
    step can ONLY shrink (haircut-toward-silence) — never sizes up."""

    portfolio_variance_cap: float = 0.02
    """aegis-ag01: the aggregate PORTFOLIO VARIANCE budget ``w^T Σ w`` (a
    NAV-fraction² number) the basket step haircuts toward. Read ONLY when
    ``portfolio_variance_sizing_enabled`` is True; default-off keeps behavior
    identical."""

    slippage_gate_enabled: bool = False
    """cut/01f0 (ADR-0097): when True, the Rule-5 cost gate + Rule-6 sizer
    consume a SLIPPAGE-HAIRCUT expected edge instead of the raw edge — the
    conservative live-vs-paper execution penalty (``slippage_penalty_frac``,
    pre-computed by the shell) is subtracted from ``|edge|`` toward silence
    BEFORE the cost gate, so a thin edge that only clears the cost gate on
    optimistic paper fills is SILENCED. Default False = BYTE-IDENTICAL to the
    raw-edge path (the gate sees the unmodified edge). The shell flips this from
    ``HERMES_QUANT_SLIPPAGE_GATE`` (default-OFF, eval-gated) and computes the
    penalty via ``hermes_quant.risk.slippage_haircut.estimate_live_penalty`` —
    that estimator is NOT imported into the pure core (PURITY: stdlib+pandas).
    HAIRCUT-TOWARD-SILENCE: the step can ONLY SHRINK ``|edge|`` (never grows it);
    a non-finite penalty fails toward the conservative floor (drives edge to 0 =
    silence), never improves edge."""

    slippage_penalty_frac: float = 0.0
    """cut/01f0 (ADR-0097): the PRE-COMPUTED one-way live-execution penalty as a
    NAV-fraction return (same units as ``edge``), passed IN by the shell so the
    pure core need not import the estimator. Read ONLY when
    ``slippage_gate_enabled`` is True; default ``0.0`` keeps behavior identical
    even if the flag were flipped without a penalty. ALWAYS treated as a positive
    COST (its magnitude is subtracted from ``|edge|``); a non-finite value fails
    toward silence (edge -> 0), never a free pass (the ar08 finite-guard family)."""

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
# Per-asset state (cooldown timers, last-loss tracking) — VERBATIM.
# ---------------------------------------------------------------------------


@dataclass
class _AssetCooldownState:
    """Cooldown timers per (account, asset_class, asset)."""

    last_loss_at: pd.Timestamp | None = None


# ---------------------------------------------------------------------------
# DefaultRiskGate — the host-agnostic port.
# ---------------------------------------------------------------------------


class DefaultRiskGate:
    """Host-agnostic concrete risk gate (ADR-0004 — the FINAL authority).

    Operates on the Stage-2 read-interfaces (CoreSignal / CoreMarketState /
    CorePortfolio / CoreHaltState) and emits a GateDecision (carrying the full
    halt triple) or None for silence.

    Per synthesis-v2 §P0-A: cost gate AND Kelly sizer use expected_signed_edge.
    Per synthesis-v2 §P0-D ordering: halt FIRST, then any other check.
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        audit_sink: AuditSink | None = None,
    ):
        """
        Args:
            config: Risk profile (defaults to moderate).
            audit_sink: Optional best-effort audit callable (coupling edit a).
                Defaults to a no-op — byte-identical to the live decision path
                (where audit is best-effort and swallowed). The shell wires one
                that forwards to ``governance.audit_log`` if it wants audit.
        """
        self.config = config or RiskConfig()
        self._audit_sink: AuditSink = audit_sink or _noop_audit_sink
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
        self._n_silenced_event_risk = 0
        self._n_silenced_nonfinite_portfolio = 0

    def _emit_audit(self, *, kind: str, asof: datetime, payload: dict[str, Any]) -> None:
        """Forward to the injected sink. Failures are swallowed (silence-by-
        default for observation — audit must NEVER block a gate decision)."""
        try:
            self._audit_sink(kind=kind, asof=asof, payload=payload)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("audit_sink failed for %s: %s", kind, e)

    def _audit_rejection(self, signal: CoreSignal, reason: str) -> None:
        """Emit a 'gate_rejection' audit event. Failures are swallowed."""
        self._emit_audit(
            kind="gate_rejection",
            asof=_ts_to_datetime(signal.asof),
            payload={
                "asset": signal.asset,
                "direction": int(signal.direction),
                "magnitude": float(signal.magnitude),
                "confidence": float(signal.confidence),
                "reason": reason,
            },
        )

    def _audit_approval(self, signal: CoreSignal, decision: GateDecision) -> None:
        """Emit a 'gate_approval' audit event. Failures are swallowed."""
        self._emit_audit(
            kind="gate_approval",
            asof=_ts_to_datetime(signal.asof),
            payload={
                "asset": signal.asset,
                "direction": int(signal.direction),
                "magnitude": float(signal.magnitude),
                "confidence": float(signal.confidence),
                "target_position_pct": float(decision.target_position_pct),
                "reason": decision.reason,
            },
        )

    def _silence(self, signal: CoreSignal, *, reason: str) -> None:
        """Internal helper: emit gate_rejection audit and return None."""
        self._audit_rejection(signal, reason)
        return None

    def _flatten_nonfinite_portfolio(
        self,
        signal: CoreSignal,
        portfolio: CorePortfolio,
    ) -> GateDecision:
        self._n_silenced_nonfinite_portfolio += 1
        decision = GateDecision(
            target_position_pct=0.0,
            reason="non_finite_portfolio_state",
            halt=True,
            halt_scope=(portfolio.account_id, portfolio.asset_class, None),
            halt_until=None,
        )
        self._audit_rejection(signal, decision.reason)
        return decision

    def gate(
        self,
        signal: CoreSignal,
        market: CoreMarketState,
        portfolio: CorePortfolio,
        halt_state: Any,
    ) -> GateDecision | None:
        """Enforce the 8-rule sequence. Returns None for silence."""

        # Rule 0: Halt check (HIGHEST PRIORITY per synthesis-v2 §P0-D ordering)
        if halt_state.is_halted(portfolio.account_id, portfolio.asset_class, signal.asset):
            self._n_silenced_halt += 1
            return self._silence(signal, reason="halt_active")

        # Rule 0.5 (lookahead-evidence) is DROPPED TO THE SHELL (coupling edit c):
        # the core gate imports no evidence module and consumes a PRE-FILTERED
        # CoreSignal. A host shell runs its own ADR-0033 lookahead filter upstream.

        try:
            drawdown_pct = portfolio.drawdown_pct
            daily_loss_pct = portfolio.daily_loss_pct
        except Exception:  # noqa: BLE001 - unknowable account state fails closed
            return self._flatten_nonfinite_portfolio(signal, portfolio)
        if not _is_finite_number(drawdown_pct) or not _is_finite_number(daily_loss_pct):
            return self._flatten_nonfinite_portfolio(signal, portfolio)

        # Rule 1: Drawdown circuit breaker
        if drawdown_pct > self.config.max_drawdown_pct:
            self._n_silenced_drawdown += 1
            decision = GateDecision(
                target_position_pct=0.0,
                reason=f"drawdown_circuit_breaker_{drawdown_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=None,  # explicit resume only
            )
            self._audit_rejection(signal, decision.reason)
            return decision

        # Rule 2: Daily-loss circuit breaker
        if daily_loss_pct > self.config.max_daily_loss_pct:
            self._n_silenced_daily_loss += 1
            decision = GateDecision(
                target_position_pct=0.0,
                reason=f"daily_loss_circuit_breaker_{daily_loss_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=_next_session_open(market.tz, portfolio.asof),
            )
            self._audit_rejection(signal, decision.reason)
            return decision

        # Rule 3: Silence on flat or zero-confidence signal
        if signal.direction == 0 or signal.confidence < 1e-6:
            self._n_silenced_flat += 1
            return self._silence(signal, reason="flat_or_zero_confidence")

        # Rule 3.5: ADR-0084 pre-event blackout guard (DEFAULT-OFF, ADDITIVE).
        # Coupling edit (b): gated on config.event_risk_enabled instead of an
        # env var. A HIGH-impact scheduled event within event_risk_window_days
        # FORWARD of signal.asof silences a fresh OPENING/INCREASING position.
        # RAILS (ADR-0084 D-1): this rule can ONLY reject/abstain — never sizes,
        # never blocks DE-RISKING.
        if self.config.event_risk_enabled:
            try:
                current = portfolio.current_position_pct(signal.asset)
            except Exception:  # noqa: BLE001 - unknowable account state fails closed
                return self._flatten_nonfinite_portfolio(signal, portfolio)
            if not _is_finite_number(current):
                return self._flatten_nonfinite_portfolio(signal, portfolio)
            is_opening_or_increasing = signal.direction * current >= 0
            if is_opening_or_increasing:
                event_risk = (signal.metadata or {}).get("event_risk")
                blackout, reason = in_event_blackout(
                    event_risk,
                    asof=_ts_to_datetime(signal.asof),
                    window_days=self.config.event_risk_window_days,
                )
                if blackout:
                    self._n_silenced_event_risk += 1
                    return self._silence(signal, reason=reason or "event_blackout")

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
        if not all(
            _is_finite_number(value)
            for value in (
                edge,
                market.commission,
                market.spread,
                market.slippage_estimate,
                market.volatility,
            )
        ):
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="non_finite_risk_input")
        # cut/01f0 (ADR-0097) SLIPPAGE HAIRCUT (DEFAULT-OFF). When enabled, shrink
        # |edge| toward silence by the PRE-COMPUTED conservative live-vs-paper
        # execution penalty (passed IN by the shell — the estimator is NOT imported
        # into the pure core) BEFORE the cost gate + sizer, so a thin edge that only
        # clears the cost gate on optimistic paper fills is SILENCED. The step can
        # ONLY shrink |edge| (never amplify); a non-finite penalty fails toward the
        # conservative floor (edge -> 0 = silence). Default-off => edge unchanged
        # (BYTE-IDENTICAL to the raw-edge path). Both the cost gate and the Kelly
        # sizer below consume this haircut edge (synthesis-v2 §P0-A: same edge for
        # gate AND sizer).
        if self.config.slippage_gate_enabled:
            edge = _slippage_haircut_edge(edge, self.config.slippage_penalty_frac)
        # PAPER-MODE-ONLY override: when paper_zero_costs=True, threshold is 0.0
        # INSTEAD of cost_multiple × round_trip_cost. The edge-sign alignment
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
        # Phase-8 P0-B: edge-sign alignment guard. Silence whenever the signed
        # edge does not agree with the requested direction (silence-by-default:
        # if the calibrated probability says we don't have a positive expected
        # return in the requested direction, we hold cash).
        if edge * signal.direction <= 0:
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="cost_gate_edge_sign")
        if abs(edge) < threshold:
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="cost_gate_below_threshold")

        # Rule 6: Position size from quarter-Kelly
        # variance = volatility² (volatility per ADR-0009 §P0-1 fix is stdev)
        variance = float(market.volatility) ** 2
        if not _is_finite_number(variance):
            self._n_silenced_cost_gate += 1
            return self._silence(signal, reason="non_finite_risk_input")
        target_size = quarter_kelly_size(
            edge=edge,
            variance=variance,
            quarter_kelly=self.config.quarter_kelly,
            max_position_pct=self.config.max_position_pct,
            action_step=self.config.action_step,
            direction=signal.direction,
        )

        # Rule 7: Minimum trade size guard (anti-churn)
        try:
            current = portfolio.current_position_pct(signal.asset)
        except Exception:  # noqa: BLE001 - unknowable account state fails closed
            return self._flatten_nonfinite_portfolio(signal, portfolio)
        if not _is_finite_number(current):
            return self._flatten_nonfinite_portfolio(signal, portfolio)
        delta = target_size - current
        if abs(delta) < self.config.min_trade_size:
            self._n_silenced_min_trade += 1
            return self._silence(signal, reason="min_trade_size")

        self._n_actions += 1
        decision = GateDecision(
            target_position_pct=target_size,
            reason=(
                f"signal_dir={signal.direction}_conf={signal.confidence:.3f}_"
                f"edge={edge:.5f}_kelly_size={target_size:.3f}"
            ),
            signal_id=signal.metadata.get("id") if signal.metadata else None,
            halt=False,
        )
        self._audit_approval(signal, decision)
        return decision

    def apply_portfolio_variance_sizing(
        self,
        targets: list[tuple[str, float]],
        cov: Any,
    ) -> list[tuple[str, float]]:
        """aegis-ag01 (ADR-0096 Gate 1): correlation-aware POSITION-level haircut.

        The per-signal :meth:`gate` sizes ONE name at a time and Rule 6.5 clips
        each |w_i| to the per-name cap — it has NO covariance view, so five
        correlated names at the per-name cap form a ~100% beta bet wearing a
        "diversified" mask. This BASKET method closes that gap: it haircuts the
        whole basket so the PORTFOLIO VARIANCE ``w^T Σ w`` stays within
        ``config.portfolio_variance_cap`` — not merely each |w_i| within a cap.

        DEFAULT-OFF (byte-identical to 83bf280): with
        ``config.portfolio_variance_sizing_enabled`` False (the default) this is a
        PASS-THROUGH — it returns ``targets`` UNCHANGED. The shell flips the flag
        from ``HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING`` (default-OFF, eval-gated).
        The per-signal Rule-0..7 path is UNTOUCHED.

        HAIRCUT-TOWARD-SILENCE: when enabled the step applies a single global
        scale ``λ ∈ [0, 1]`` (de-levering correlated names TOGETHER) and can ONLY
        shrink — it NEVER increases any |target|. FAIL-CLOSED: a non-finite Σ or
        target falls back to the conservative per-name behavior (never sizes up).

        Args:
            targets: ordered ``(asset, signed_target_pct)`` pairs — the per-name
                quarter-Kelly outputs (each already clipped to the per-name cap).
            cov: ``(N, N)`` covariance over the SAME N assets, in the SAME order
                (use ``portfolio_sizing.shrink_covariance`` to produce a shrunk
                estimate). Typed ``Any`` so the gate's eager imports stay
                pandas-only; numpy is imported lazily inside the step.

        Returns:
            Ordered ``(asset, haircut_target_pct)`` pairs (input order/identity
            preserved). When the flag is OFF, the exact input list is returned.
        """
        if not self.config.portfolio_variance_sizing_enabled:
            return targets
        # Lazy import: keeps gate.py's eager import surface pandas-only and the
        # numpy dependency confined to the (default-OFF) enabled path.
        from hermes_quant.pdr_core.portfolio_sizing import (
            PortfolioVarianceConfig,
            portfolio_variance_haircut,
        )

        names = [a for a, _ in targets]
        sizes = [w for _, w in targets]
        cfg = PortfolioVarianceConfig(variance_cap=self.config.portfolio_variance_cap)
        haircut = portfolio_variance_haircut(sizes, cov, config=cfg)
        return list(zip(names, haircut, strict=True))

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
            "n_silenced_event_risk": self._n_silenced_event_risk,
            "n_silenced_nonfinite_portfolio": self._n_silenced_nonfinite_portfolio,
        }
