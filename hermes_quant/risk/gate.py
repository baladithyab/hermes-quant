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
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


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

    Fix A6 (BMA discriminator observability): the discriminative counts
    (n_views, n_distinct_analysts, contributing_analysts) are derived from
    `signal.components` when those are present, and otherwise fall back to
    the aggregator's authoritative metadata counts (BMA stores `n_views`
    and per-analyst `weights` on `signal.metadata`). The previous
    implementation recomputed SOLELY from `components`, so a signal that
    carried metadata counts but stripped/empty components (e.g. a
    serialized-then-reconstructed signal, or any aggregator that doesn't
    round-trip components) wrote a degenerate `n_distinct_analysts=0` /
    `contributing_analysts=[]` into the audit trail — surfacing downstream
    as the "n_distinct_analysts=None" blind spot the BMA degeneracy
    predicate can't query. Preferring metadata-on-fallback guarantees these
    fields are never None/degenerate whenever the aggregator produced them.

    Per ADR-0041: this is the canonical predicate input. The
    `is_bma_degenerate(event)` helper in
    `hermes_quant.governance.audit_log_query` consumes this block.
    """
    components = signal.components or ()
    md = dict(signal.metadata or {})

    analyst_view_ids: list[str] = []
    # ar126: per-analyst direction map {analyst_name: "buy"|"sell"|"flat"}. Additive
    # field (no existing consumer reads it, so byte-identical to those). The ADR-0049
    # shadow TrendFollowingRule needs the classical-TA analyst's OWN direction to test
    # the "TA + advisor agree" confluence hypothesis; without this the rule was vacuous
    # (it had no per-analyst direction to read). Derived from signal.components, the
    # canonical per-view source (AnalystView.analyst + .direction).
    per_analyst_directions: dict[str, str] = {}
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
        try:
            d = int(v.direction)
        except (TypeError, ValueError):
            continue
        # Last-writer-wins per analyst name (a single analyst contributes one view
        # per tick in practice); map the signed int to the shadow-rule vocabulary.
        per_analyst_directions[str(v.analyst)] = (
            "buy" if d > 0 else "sell" if d < 0 else "flat"
        )

    # Discriminative counts. PREFER signal.components (richest source —
    # carries per-view IDs and exact analyst names). When components is
    # empty but the aggregator stashed authoritative counts on metadata,
    # fall back to those so the audit record reflects what the aggregator
    # actually produced rather than a degenerate zero.
    #
    # BMAAggregator metadata contract (hermes_quant.aggregators.bma):
    #   metadata["weights"]  -> {analyst_name: weight}  (distinct analysts)
    #   metadata["n_views"]  -> int  (count of views entering aggregation)
    if components:
        contributing_analysts = sorted({v.analyst for v in components})
        n_distinct_analysts: int | None = len(set(contributing_analysts))
        n_views: int | None = len(components)
    else:
        weights = md.get("weights")
        if isinstance(weights, dict) and weights:
            contributing_analysts = sorted({str(a) for a in weights})
            n_distinct_analysts = len(contributing_analysts)
        else:
            contributing_analysts = []
            n_distinct_analysts = None
        md_n_views = md.get("n_views")
        if isinstance(md_n_views, int):
            n_views = md_n_views
        elif n_distinct_analysts is not None:
            # Best-effort: at least as many views as distinct analysts.
            n_views = n_distinct_analysts
        else:
            n_views = None

    # data_quality may live on the signal itself or on the aggregator
    # metadata. Prefer the signal-level field if present; otherwise fall
    # back to metadata; otherwise None.
    # Cross-model review M5: explicit None-check rather than `or`, so that
    # a legitimate falsy data_quality value (e.g. {"score": 0.0}) is not
    # silently replaced by the metadata fallback.
    sig_dq = getattr(signal, "data_quality", None)
    dq = sig_dq if sig_dq is not None else md.get("data_quality")

    return {
        "n_views": n_views,
        "n_distinct_analysts": n_distinct_analysts,
        "contributing_analysts": contributing_analysts,
        "vote_share": md.get("vote_share"),
        "n_contributing": md.get("n_contributing"),
        "bma_weights": md.get("bma_weights"),
        "aggregator_class": signal.aggregator,
        "analyst_view_ids": analyst_view_ids,
        "per_analyst_directions": per_analyst_directions,  # ar126 (additive)
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
# ADR-0084: pre-event REJECT/abstain guard (default-OFF, additive)
# ---------------------------------------------------------------------------

# Flag name (mirrors the catalyst calendar feature flag). The guard is a no-op
# unless this env var is exactly "1"; absent/"0" => byte-identical to today.
EVENT_RISK_FLAG = "HERMES_QUANT_EVENT_RISK"


def _event_risk_enabled() -> bool:
    """True iff HERMES_QUANT_EVENT_RISK=1. Read at gate() time (mirrors the
    catalyst flag-reading posture: os.environ.get, never cached at import)."""
    return os.environ.get(EVENT_RISK_FLAG, "0") == "1"


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
    catalyst calendar wiring (ctx.extras['event_risk'] — already filtered to
    ``announced_at <= decision_asof`` upstream, so EXISTENCE was knowable at
    signal.asof; this predicate only inspects the FORWARD ``scheduled_for``).

    A blackout fires iff some event satisfies ALL of:
      * impact == "high" (when ``high_impact_only``; macro Tier-1 / earnings),
      * ``scheduled_for`` is FORWARD of (or equal to) ``asof`` (a past event is
        not a pre-event risk; the position already lived through it), and
      * ``scheduled_for - asof <= window_days`` (imminent).

    Returns ``(True, reason)`` on the FIRST qualifying event (deterministic:
    callers feed a list already sorted by (scheduled_for, kind)), else
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
        # Forward-window test: the event is still ahead (or exactly at asof) AND
        # within window_days. A schedule strictly in the past is not pre-event.
        if scheduled < asof:
            continue
        if scheduled <= horizon:
            kind = str(ev.get("kind") or "event").strip().lower() or "event"
            return True, f"event_blackout_{kind}_high_impact"
    return False, None


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

    event_risk_window_days: float = 1.0
    """ADR-0084 pre-event guard: how many days FORWARD of signal.asof a
    HIGH-impact scheduled event (from ctx.extras['event_risk']) silences a
    fresh opening/increasing position. Default 1.0 = the macro window
    (FOMC/CPI/NFP print day). Config-only knob; the guard is ADDITIVE and
    ENTIRELY GATED on HERMES_QUANT_EVENT_RISK=1 — when the flag is absent the
    window value is never read and behavior is byte-identical (ADR-0084 D-3).
    The guard can ONLY reject/abstain; it never touches the ladder, never
    sizes up, never blocks de-risking (ADR-0084 D-1)."""

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

    slippage_gate_enabled: bool = False
    """01f0 (ADR-0097): when True, the LIVE decision gate haircuts the expected
    signed edge toward silence by ``slippage_penalty_frac`` BEFORE the Rule-5
    cost gate + Rule-6 sizer, so a thin edge that only clears the cost gate on
    optimistic paper fills is SILENCED. Default False => byte-identical (edge is
    never haircut). The haircut may ONLY shrink |edge| (sign preserved); a
    non-finite penalty/edge fails to 0.0 (silence). Mirrors the pdr_core leaf
    ``_slippage_haircut_edge`` (single source of truth). Eval-gated:
    HERMES_QUANT_SLIPPAGE_GATE wires this from the shell."""

    slippage_penalty_frac: float = 0.0
    """01f0: the PRE-COMPUTED one-way live-vs-paper penalty (NAV-fraction return,
    same units as the Kelly edge), supplied by the shell via
    ``slippage_haircut.estimate_live_penalty``. 0.0 => no haircut. Only consulted
    when ``slippage_gate_enabled`` is True. Fail-closed: the shell floors it to
    the conservative prior on any estimator error, never 0.0-on-error."""

    def __post_init__(self) -> None:
        """ar124: fail-CLOSED validation of the money-critical thresholds.

        ``RiskConfig`` is built from operator-editable recipe YAML
        (``recipes.instantiate_recipe_risk_gate`` → ``RiskConfig(**recipe.risk_gate_config)``)
        with NO prior validation, and the frozen dataclass had no guard. A non-finite
        threshold from YAML (``max_drawdown_pct: 1e400`` overflows to ``inf`` with no
        error; ``.nan`` parses to NaN) silently DISABLES the rail it bounds: the Rule-1
        drawdown breaker (``drawdown_pct > max_drawdown_pct``) and Rule-2 daily-loss
        breaker compare ``> inf``/``> nan`` as always-False, so a catastrophic real
        drawdown never trips/halts (fail-OPEN); ``max_position_pct = inf/nan`` likewise
        defeats the quarter-Kelly position cap (``min(size, inf) == size``). This is the
        operator-config seam that bypassed the ar08-12 finite-guard family. We fail LOUD
        (recipe load already raises on bad config) rather than fail-open with a corrupt
        rail — a non-finite or non-positive money threshold is a configuration error, not
        a runtime degrade.

        The breaker/cap thresholds must be finite and in (0, 1] (a fraction of NAV);
        cost_multiple and min_trade_size must be finite and >= 0. Upper bounds are the
        100%-of-NAV sanity ceiling, not the specific preset values (aggressive uses
        max_position_pct=0.40, max_daily_loss_pct=0.10 — all within range).
        """
        def _frac_0_1(name: str, value: object, *, allow_zero: bool = False) -> None:
            try:
                v = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ValueError(f"RiskConfig.{name} must be a number, got {value!r}")
            if not math.isfinite(v):
                raise ValueError(
                    f"RiskConfig.{name} must be finite (a NaN/inf threshold disables the "
                    f"money rail it bounds — fail-OPEN); got {value!r}"
                )
            lo_ok = (v >= 0.0) if allow_zero else (v > 0.0)
            if not (lo_ok and v <= 1.0):
                raise ValueError(
                    f"RiskConfig.{name} must be in "
                    f"{'[0, 1]' if allow_zero else '(0, 1]'} (fraction of NAV); got {v}"
                )

        def _nonneg(name: str, value: object) -> None:
            try:
                v = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ValueError(f"RiskConfig.{name} must be a number, got {value!r}")
            if not math.isfinite(v) or v < 0.0:
                raise ValueError(
                    f"RiskConfig.{name} must be finite and >= 0; got {value!r}"
                )

        # Rail-bounding fractions: a non-finite/out-of-range value disables the rail.
        _frac_0_1("max_position_pct", self.max_position_pct)
        _frac_0_1("max_drawdown_pct", self.max_drawdown_pct)
        _frac_0_1("max_daily_loss_pct", self.max_daily_loss_pct)
        _frac_0_1("min_trade_size", self.min_trade_size, allow_zero=True)
        _nonneg("cost_multiple", self.cost_multiple)

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
        baseline_store: Any = None,
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
            baseline_store: Optional DrawdownBaselineStore-like object (must
                expose `.reconcile(account_id, asset_class, equity_total, asof,
                tz) -> Baseline(peak_equity, daily_open_equity)`). cs01 fix:
                when provided, the gate reconciles the DURABLE high-water-mark
                peak and the session-anchored daily-open BEFORE the Rule-1
                (drawdown) / Rule-2 (daily-loss) circuit breakers, and recomputes
                drawdown_pct / daily_loss_pct against those durable baselines
                instead of the loader's inception-collapsed peak_equity /
                daily_open_equity (which fail-OPEN on a profitable-from-inception
                account that suffers a large peak-to-trough fall). The durable
                baselines are conservative-by-construction (HWM never decreases;
                daily-open re-anchors only at the session boundary), so the
                breaker can only trip EARLIER / equally — always the safe
                direction. When None (default), behavior is BYTE-IDENTICAL to
                today (reads portfolio.drawdown_pct / portfolio.daily_loss_pct
                directly) — mirrors the evidence_store=None no-op seam so live
                wiring is a separate operator-gated step.
        """
        self.config = config or RiskConfig()
        self.evidence_store = evidence_store
        self.baseline_store = baseline_store
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
        self._n_silenced_event_risk = 0
        self._n_silenced_nonfinite_portfolio = 0

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

    @staticmethod
    def _pct_from_baseline(base: Any, equity: Any) -> float:
        """Recompute a drawdown/daily-loss fraction from a durable baseline.

        Replicates the EXACT formula in protocol.py drawdown_pct (:308-318) /
        daily_loss_pct (:320-328), including the `base<=0` and non-finite
        NaN-fail-CLOSED guards, so the recomputed value behaves identically to
        the property the gate would otherwise read — only the DENOMINATOR is the
        durable baseline (HWM peak / session-open) rather than the loader's
        inception-collapsed value. Returns a sentinel >= any plausible threshold
        (1.0) on a non-finite numerator/denominator so Rule-1/Rule-2 trip on
        unknowable state (fail-CLOSED), matching the property exactly.
        """
        try:
            b = float(base)
            eq = float(equity)
        except (TypeError, ValueError):
            return 1.0
        if not (math.isfinite(b) and math.isfinite(eq)) or b <= 0:
            return 0.0 if (math.isfinite(b) and b <= 0) else 1.0
        return max(0.0, (b - eq) / b)

    def _durable_breaker_pcts(
        self,
        portfolio: Portfolio,
        market: MarketState,
    ) -> tuple[float, float]:
        """cs01 fix: drawdown_pct / daily_loss_pct against DURABLE baselines.

        Reconciles the durable high-water-mark peak + session-anchored daily-open
        via the injected baseline_store, then recomputes the two circuit-breaker
        fractions against those baselines. The store call is wrapped fail-CLOSED:
        any failure falls back to a baseline AT-LEAST-AS-STRICT as today
        (max(portfolio.peak_equity, equity) for peak; portfolio.daily_open_equity
        for the session anchor) and a warning, and NEVER raises out of gate().

        Direction invariant: durable peak >= portfolio.peak_equity and durable
        daily_open >= portfolio.daily_open_equity whenever the loader collapsed
        them to inception, so the recomputed fractions are >= the portfolio's
        reported values — the breaker can only trip EARLIER / equally.
        """
        equity = portfolio.equity_total
        try:
            baseline = self.baseline_store.reconcile(
                account_id=portfolio.account_id,
                asset_class=portfolio.asset_class,
                equity_total=equity,
                asof=portfolio.asof,
                tz=market.tz,
            )
            peak = baseline.peak_equity
            daily_open = baseline.daily_open_equity
        except Exception as e:  # noqa: BLE001 - store failure must fail CLOSED, never raise
            logger.warning(
                "baseline_store.reconcile raised (%s) — failing CLOSED to "
                "at-least-as-strict portfolio baselines",
                e,
            )
            # Fail-CLOSED: never weaker than today. peak >= reported peak (so
            # recomputed drawdown >= portfolio.drawdown_pct); daily_open = the
            # portfolio's own session anchor (so recomputed daily_loss >=
            # portfolio.daily_loss_pct). A non-finite peak/equity still routes to
            # _flatten_nonfinite_portfolio via the _is_finite_number guard.
            try:
                rep_peak = float(portfolio.peak_equity)
                eq_f = float(equity)
                peak = max(rep_peak, eq_f) if (
                    math.isfinite(rep_peak) and math.isfinite(eq_f)
                ) else portfolio.peak_equity
            except (TypeError, ValueError):
                peak = portfolio.peak_equity
            daily_open = portfolio.daily_open_equity
        drawdown_pct = self._pct_from_baseline(peak, equity)
        daily_loss_pct = self._pct_from_baseline(daily_open, equity)
        return drawdown_pct, daily_loss_pct

    def _flatten_nonfinite_portfolio(
        self,
        signal: AggregatedSignal,
        portfolio: Portfolio,
    ) -> Action:
        self._n_silenced_nonfinite_portfolio += 1
        action = Action(
            target_position_pct=0.0,
            reason="non_finite_portfolio_state",
            halt=True,
            halt_scope=(portfolio.account_id, portfolio.asset_class, None),
            halt_until=None,
        )
        self._audit_rejection(signal, action.reason)
        return action

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

        # cs01 fix: the Rule-1 (drawdown) / Rule-2 (daily-loss) denominators.
        # When a durable baseline_store is injected, recompute drawdown_pct /
        # daily_loss_pct against the DURABLE high-water-mark peak + session-
        # anchored daily-open instead of portfolio.peak_equity /
        # portfolio.daily_open_equity (which the loader collapses to the
        # inception baseline → a profitable-from-inception account that suffers a
        # large peak-to-trough fall fails OPEN). The durable baselines are
        # conservative-by-construction so the breaker only trips EARLIER /
        # equally. When baseline_store is None (default) this is BYTE-IDENTICAL
        # to today — reads the portfolio properties directly. The store path is
        # fail-CLOSED (never raises out of gate()); the recomputed values still
        # run the same _is_finite_number → _flatten_nonfinite_portfolio guard.
        try:
            if self.baseline_store is not None:
                drawdown_pct, daily_loss_pct = self._durable_breaker_pcts(portfolio, market)
            else:
                drawdown_pct = portfolio.drawdown_pct
                daily_loss_pct = portfolio.daily_loss_pct
        except Exception:  # noqa: BLE001 - unknowable account state fails closed
            return self._flatten_nonfinite_portfolio(signal, portfolio)
        if not _is_finite_number(drawdown_pct) or not _is_finite_number(daily_loss_pct):
            return self._flatten_nonfinite_portfolio(signal, portfolio)

        # Rule 1: Drawdown circuit breaker
        if drawdown_pct > self.config.max_drawdown_pct:
            self._n_silenced_drawdown += 1
            action = Action(
                target_position_pct=0.0,
                reason=f"drawdown_circuit_breaker_{drawdown_pct:.4f}",
                halt=True,
                halt_scope=(portfolio.account_id, portfolio.asset_class, None),
                halt_until=None,  # explicit resume only
            )
            self._audit_rejection(signal, action.reason)
            return action

        # Rule 2: Daily-loss circuit breaker
        if daily_loss_pct > self.config.max_daily_loss_pct:
            self._n_silenced_daily_loss += 1
            action = Action(
                target_position_pct=0.0,
                reason=f"daily_loss_circuit_breaker_{daily_loss_pct:.4f}",
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

        # Rule 3.5: ADR-0084 pre-event blackout guard (DEFAULT-OFF, ADDITIVE).
        # A HIGH-impact scheduled event (FOMC/CPI/NFP/earnings) within
        # config.event_risk_window_days FORWARD of signal.asof silences a fresh
        # OPENING/INCREASING position. asof-honest: the event_risk payload was
        # already filtered upstream to announced_at<=decision_asof (the event's
        # EXISTENCE was knowable at signal.asof); this rule only tests the
        # forward scheduled_for window — a forward date that was knowable at
        # signal.asof, mirroring the halt/cooldown/drawdown SILENCE rules.
        #
        # RAILS (ADR-0084 D-1): this rule can ONLY reject/abstain. It NEVER
        # touches the ladder, never sizes, never blocks DE-RISKING. It is fully
        # gated on HERMES_QUANT_EVENT_RISK=1 — flag absent => this whole block is
        # skipped => byte-identical to today. The deterministic gate stays the
        # FINAL, IMMUTABLE authority; this is an ADDED reject condition only,
        # never a weakened one.
        if _event_risk_enabled():
            # Opening/increasing only: a signal that pushes exposure further from
            # flat in its own direction is opening/increasing; a signal opposite
            # the current position is de-risking and is NEVER blocked. Flat
            # (current==0) is treated as opening.
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
        # 01f0 (ADR-0097): haircut the signed edge toward silence by the conservative
        # live-vs-paper execution penalty BEFORE the cost gate + sizer, so a thin edge
        # that only clears the cost gate on optimistic paper fills is SILENCED on the
        # LIVE decision path (b61c wired the haircut into clean_window EVIDENCE only; the
        # live admission gate still used raw edge — the orphan this closes). The pure leaf
        # _slippage_haircut_edge (single source of truth, shared with pdr_core) can ONLY
        # shrink |edge| (sign preserved); a non-finite penalty/edge -> 0.0 (silence). The
        # penalty is pre-computed by the shell into config.slippage_penalty_frac. Default-OFF
        # (slippage_gate_enabled=False) => edge untouched => byte-identical.
        if self.config.slippage_gate_enabled:
            from hermes_quant.pdr_core.gate import _slippage_haircut_edge

            edge = _slippage_haircut_edge(edge, self.config.slippage_penalty_frac)
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
            "n_silenced_event_risk": self._n_silenced_event_risk,
            "n_silenced_nonfinite_portfolio": self._n_silenced_nonfinite_portfolio,
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
