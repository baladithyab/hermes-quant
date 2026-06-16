"""hermes_quant.gates.silence_bias — 4-dim silence-bias gate (ADR-0016).

Per the founding charter §"REACT" three-bullet silence-by-default spec:
  - ensemble disagreement is LOW (high confidence)
  - expected edge > transaction cost + slippage + risk premium
  - position would not violate risk limits (VaR, exposure caps)

ADR-0016 codifies these as 4 dims (charter clauses become Confidence +
Urgency + Compute Budget + Salience). All four must pass; default is
silence. The pattern source is Eidolon's pdr_lwm/decision.py::OutputGateSystem
7-dim gate (need/timing/confidence/modality/urgency/compute/adaptation/salience),
collapsed for the trading domain.

This module is PURE FUNCTION (no IO, no logger side effects beyond
the standard logger). The autonomous orchestrator handles tick output,
audit trail, and React. The gate decides; the orchestrator acts.
"""

from __future__ import annotations

import enum
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------


class GateDecision(enum.StrEnum):
    """Per ADR-0016 §D2. Structured silence reasons make tuning a data
    exercise rather than guesswork."""

    FIRE = "FIRE"
    """All 4 dims passed; React allowed."""

    SILENCE_LOW_CONFIDENCE = "SILENCE_LOW_CONFIDENCE"
    """Aggregated confidence below `min_confidence`."""

    SILENCE_LOW_URGENCY = "SILENCE_LOW_URGENCY"
    """Edge/volatility ratio below `min_urgency`."""

    SILENCE_INSUFFICIENT_VOICES = "SILENCE_INSUFFICIENT_VOICES"
    """Fewer analysts emitted than `min_analysts_emitted`."""

    SILENCE_SALIENCE_VETO = "SILENCE_SALIENCE_VETO"
    """Symbol has too many recent rejections in the journal."""

    SILENCE_GATED_BY_ADVISOR = "SILENCE_GATED_BY_ADVISOR"
    """The advisor itself returned risk_gate.pass=false; nothing for us
    to evaluate. Distinct from the silence-bias dims because the source
    is a different gate."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Tunable thresholds for the 4-dim silence-bias gate (ADR-0016 §D2).

    Defaults are intentionally CONSERVATIVE — autonomous mode should
    fire SELDOM. Tuning happens via config edits informed by tick-output
    silence reasons over time.

    Cite: founding charter "Otherwise: hold cash, do nothing. This is
    the most underrated property — most trading systems lose because
    they over-trade."
    """

    min_confidence: float = 0.65
    """Post-calibration ensemble probability threshold. Stricter than
    HITL (HITL operator can override with judgment; autonomous can't)."""

    min_urgency: float = 0.5
    """Edge / volatility ratio. The charter says edge must exceed
    transaction cost + slippage + risk premium. We codify a Sharpe-like
    threshold — edge of half a stdev combined with confidence > 0.65 is
    a meaningful quality bar."""

    min_volatility: float = 0.001
    """Positive FLOOR on the urgency divisor (10 bps). Without it, a
    tiny-but-positive atr_relative (a flatlined / illiquid name where
    atr/last_close ~ 1e-6) makes urgency = edge / vol explode, so the
    autonomous FIRE gate clears trivially on pure noise — a finite-but-huge
    urgency passes the `math.isfinite` guard. The floor bounds
    urgency <= abs(edge) / 0.001 = 1000 * abs(edge): a 0.1% noise edge
    yields urgency ~0.4 (correctly SILENCED at min_urgency=0.5) while a
    genuine 1%+ edge still clears. The 10 bps value mirrors the module
    author's own 'tiny ATR is noise' insight — MicrostructureLite treats
    atr_rel < 0.005 as quiet/noise for the toxicity sub-signal."""

    min_analysts_emitted: int = 2
    """Minimum number of analysts that emitted a view. With 2 analysts
    in v0.1.2 (ClassicalTA + MicrostructureLite), require both. Default
    raises to 2-of-3 when KronosAnalyst lands. Single-voice signals are
    NEVER enough in autonomous mode."""

    max_recent_rejections: int = 3
    """Skip symbols with N+ recent rejections in the journal. The
    operator's repeated 'no' is signal the system shouldn't override."""

    salience_window_hours: int = 168
    """7-day window for the salience veto. Tunable per asset class
    if needed (rejections from a year ago aren't relevant; rejections
    from this week are)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Decision + structured details. The `details` dict is intended for
    tick-output JSON consumption — operators read it to understand why
    the gate decided what it did."""

    decision: GateDecision
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.decision == GateDecision.FIRE


# ---------------------------------------------------------------------------
# The pure-function gate
# ---------------------------------------------------------------------------


def silence_bias_gate(
    advisor_result: dict[str, Any],
    *,
    config: GateConfig | None = None,
    journal_lessons: list[dict[str, Any]] | None = None,
    market_volatility: float | None = None,
) -> GateResult:
    """Evaluate the 4-dim silence-bias gate against an advisor result.

    Args:
        advisor_result: The dict returned from advisor.recommend(). Must
            include 'aggregated_signal', 'risk_gate', 'analyst_views' keys
            per ADR-0014 §D1.
        config: GateConfig with tuned thresholds. Defaults to safe
            conservative bias.
        journal_lessons: Recent journal entries for the symbol. From
            journal.reader.get_recent_lessons(). Used for the salience
            veto (D2 dim 4). None or empty = no salience veto possible.
        market_volatility: Optional override for urgency calc. If None,
            we derive from the analyst_views' magnitude metadata; if
            still unavailable we conservatively use 0.01 (1% vol assumed).

    Returns:
        GateResult with decision + structured details suitable for
        operator-readable tick output.

    The function has NO side effects beyond debug logging. The caller
    handles tick output, journal append, React.
    """
    cfg = config or GateConfig()

    # Step 0: respect upstream gating from the risk gate. If the advisor's
    # own risk_gate.pass is False, we never have a signal to evaluate.
    rg = (advisor_result or {}).get("risk_gate") or {}
    if not rg.get("pass", False):
        return GateResult(
            decision=GateDecision.SILENCE_GATED_BY_ADVISOR,
            details={
                "gated_reason": rg.get("gated_reason", "unknown"),
            },
        )

    sig = (advisor_result or {}).get("aggregated_signal") or {}
    views = (advisor_result or {}).get("analyst_views") or []

    # ---- Dim 3: Compute Budget (number of voices) ----
    # Check first because cheapest and zero-voice -> all other dims meaningless.
    n_emitted = len(views)
    if n_emitted < cfg.min_analysts_emitted:
        return GateResult(
            decision=GateDecision.SILENCE_INSUFFICIENT_VOICES,
            details={
                "emitted": n_emitted,
                "min_required": cfg.min_analysts_emitted,
                "rationale": (
                    "Single-voice signals are insufficient in autonomous "
                    "mode (ADR-0016 §D2). Wait for ensemble agreement."
                ),
            },
        )

    # ---- Dim 1: Confidence ----
    # NaN-fail-CLOSED (deep-review 2026-06-07): a non-finite confidence must
    # SILENCE, not slip through. `NaN < min_confidence` is False, so without the
    # explicit finite check a NaN confidence would pass this gate dim toward FIRE.
    confidence = float(sig.get("confidence", 0.0))
    if not math.isfinite(confidence) or confidence < cfg.min_confidence:
        return GateResult(
            decision=GateDecision.SILENCE_LOW_CONFIDENCE,
            details={
                "confidence": confidence,
                "min_required": cfg.min_confidence,
                "rationale": (
                    "Calibrated ensemble confidence below threshold. "
                    "Cold-start calibrator (n<200 samples) shrinks raw "
                    "scores by 0.20; if the system is new, this is "
                    "expected — let it accumulate fills first."
                ),
            },
        )

    # ---- Dim 2: Urgency = expected_signed_edge / volatility ----
    # The charter: "expected edge > transaction cost + slippage + risk
    # premium". We codify as a Sharpe-like ratio. The risk gate (already
    # passed in step 0) has confirmed edge > transaction cost; the
    # silence-bias urgency check is the additional risk-premium-aware filter.
    magnitude = abs(float(sig.get("magnitude", 0.0)))
    direction = int(sig.get("direction", 0))
    expected_signed_edge = magnitude * (2 * confidence - 1.0)  # signed edge proxy
    if direction == 0:
        # Should have been caught by Dim 0 risk_gate, but defensive
        return GateResult(
            decision=GateDecision.SILENCE_LOW_URGENCY,
            details={
                "edge": 0.0,
                "min_required": cfg.min_urgency,
                "rationale": "Direction is flat; nothing to act on.",
            },
        )

    vol = market_volatility
    if vol is None:
        # Try to extract from analyst metadata; fall back to defensive default
        for v in views:
            md = v.get("metadata") or {}
            atr_rel = md.get("atr_relative")
            if atr_rel is not None and atr_rel > 0:
                vol = float(atr_rel)
                break
        if vol is None:
            vol = 0.01  # 1% default — conservative, will gate small magnitudes

    if vol <= 0:
        vol = 0.01
    # Positive FLOOR on the divisor (deep-review 2026-06-16): a tiny-but-positive
    # vol (e.g. atr_relative ~1e-6 from a flatlined/illiquid name supplied via
    # analyst metadata above) would otherwise make urgency = edge/vol explode and
    # the FIRE gate clear trivially on pure noise. The `math.isfinite(urgency)`
    # check below catches NaN/inf ONLY — a large FINITE urgency slips through. This
    # is distinct from the NaN-fail-CLOSED family; it is the tiny-positive-finite
    # gap. Floor justified by MicrostructureLite treating atr_rel<0.005 as noise.
    if cfg.min_volatility > 0:
        vol = max(vol, cfg.min_volatility)
    urgency = abs(expected_signed_edge) / vol

    # NaN-fail-CLOSED (deep-review 2026-06-07): a non-finite urgency (from a NaN
    # magnitude/confidence/vol) must SILENCE. `NaN < min_urgency` is False, so
    # without this check a NaN urgency would slip through toward FIRE.
    if not math.isfinite(urgency) or urgency < cfg.min_urgency:
        return GateResult(
            decision=GateDecision.SILENCE_LOW_URGENCY,
            details={
                "urgency": urgency,
                "expected_signed_edge": expected_signed_edge,
                "volatility": vol,
                "min_required": cfg.min_urgency,
                "rationale": (
                    "Edge / volatility below the Sharpe-like threshold. "
                    "The signal is in the right direction but not strong "
                    "enough vs noise to justify autonomous action."
                ),
            },
        )

    # ---- Dim 4: Salience (recent rejections in the journal) ----
    rejections = _count_recent_rejections(
        journal_lessons or [],
        window_hours=cfg.salience_window_hours,
    )
    if rejections >= cfg.max_recent_rejections:
        return GateResult(
            decision=GateDecision.SILENCE_SALIENCE_VETO,
            details={
                "recent_rejections": rejections,
                "max_allowed": cfg.max_recent_rejections,
                "window_hours": cfg.salience_window_hours,
                "rationale": (
                    "Operator has rejected this symbol "
                    f"{rejections} times in the last "
                    f"{cfg.salience_window_hours}h. Autonomous mode "
                    "respects repeated human vetoes."
                ),
            },
        )

    # All four dims passed -> FIRE
    return GateResult(
        decision=GateDecision.FIRE,
        details={
            "confidence": confidence,
            "urgency": urgency,
            "n_voices": n_emitted,
            "recent_rejections": rejections,
            "passed_dims": [
                "confidence",
                "urgency",
                "compute_budget",
                "salience",
            ],
        },
    )


# ---------------------------------------------------------------------------
# Class wrapper (state-bearing, for ergonomics; the function is canonical)
# ---------------------------------------------------------------------------


class SilenceBiasGate:
    """Convenience wrapper around silence_bias_gate() for callers that
    want to instantiate a configured gate object once and reuse.

    The pure function is the canonical surface; this class exists so
    autonomous orchestrators can pass `gate.evaluate(advisor_result)`
    instead of remembering to thread config through every call.
    """

    def __init__(self, config: GateConfig | None = None):
        self.config = config or GateConfig()
        self._n_evaluated = 0
        self._n_fires = 0

    def evaluate(
        self,
        advisor_result: dict[str, Any],
        *,
        journal_lessons: list[dict[str, Any]] | None = None,
        market_volatility: float | None = None,
    ) -> GateResult:
        result = silence_bias_gate(
            advisor_result,
            config=self.config,
            journal_lessons=journal_lessons,
            market_volatility=market_volatility,
        )
        self._n_evaluated += 1
        if result.fired:
            self._n_fires += 1
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "n_evaluated": self._n_evaluated,
            "n_fires": self._n_fires,
            "fire_rate": (self._n_fires / self._n_evaluated if self._n_evaluated else 0.0),
            "config": {
                "min_confidence": self.config.min_confidence,
                "min_urgency": self.config.min_urgency,
                "min_analysts_emitted": self.config.min_analysts_emitted,
                "max_recent_rejections": self.config.max_recent_rejections,
                "salience_window_hours": self.config.salience_window_hours,
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_recent_rejections(
    journal_lessons: list[dict[str, Any]],
    *,
    window_hours: int,
) -> int:
    """Count entries with hitl_kind=='reject' or rejected_at within the
    window. journal_lessons format from journal.reader.get_recent_lessons().

    The salience window is enforced at retrieval time too (the journal
    reader returns recency-tail), so this function is a defense-in-depth
    re-filter rather than the primary cutoff.
    """
    if not journal_lessons:
        return 0
    from datetime import datetime, timedelta

    cutoff = datetime.now(tz=UTC) - timedelta(hours=window_hours)
    count = 0
    for lesson in journal_lessons:
        if lesson.get("hitl_kind") != "reject":
            continue
        when = lesson.get("when")
        if not when:
            # Conservative: count it if we can't time-bound (better to
            # over-veto than under-veto in autonomous mode)
            count += 1
            continue
        try:
            ts = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= cutoff:
                count += 1
        except (ValueError, TypeError):
            count += 1  # conservative
    return count
