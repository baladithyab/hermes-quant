"""hermes_quant.observability.fallback_probe — synthetic LLM-failure probe.

ADR-0060: silence-by-default verification under realistic LLMCaller failure.

This module exposes a single entry point — ``run_fallback_probe`` — that
intentionally fails the LLMCaller with a battery of failure modes and asserts
that every LLM-wired surface (TraderNodeLLM, RiskCommittee v0.2, Reflector v0.2,
HMM regime classifier) gracefully falls back to its deterministic v0.1 path.

Failure modes injected (str literals)
─────────────────────────────────────
  * 'happy_path'      → stub LLM returns a valid synthetic JSON for that surface
  * 'timeout'         → stub raises TimeoutError / asyncio.TimeoutError
  * 'rate_limit'      → stub raises RateLimitError (HTTP 429-shaped)
  * 'server_error'    → stub raises ServerError (HTTP 500-shaped)
  * 'malformed_json'  → stub returns (None, raw_text) where raw_text is non-JSON
  * 'schema_invalid'  → stub returns (None, raw_text) where JSON parses but
                        does not match the surface's pydantic schema
  * 'empty'           → stub returns (None, raw_text="") empty string

Every probe NEVER raises. All exceptions are caught and recorded in the
``error`` field of FallbackProbeResult.  The exit code of the CLI is 1 when
any output is invalid (i.e. silence-by-default did not hold) and 0 otherwise.

NO REAL NETWORK CALLS are ever made.  All LLMCaller stubs are pure in-process
Python that raise / return synthetic values.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Type
from unittest.mock import patch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

FAILURE_MODES: tuple[str, ...] = (
    "happy_path",
    "timeout",
    "rate_limit",
    "server_error",
    "malformed_json",
    "schema_invalid",
    "empty",
)
"""Synthetic failure modes injected into the stub LLMCaller."""

SURFACES: tuple[str, ...] = ("trader", "risk_committee", "reflector", "regime_hmm")
"""LLM-wired surfaces exercised by the probe."""

# Failure modes that make sense for the HMM (which doesn't speak to an LLM
# per-call but loads a model file; we map LLM failure modes to model-load
# failure modes to keep the matrix consistent).
_HMM_MEANINGFUL_MODES: tuple[str, ...] = (
    "happy_path",
    "timeout",
    "server_error",
    "schema_invalid",
)


# ---------------------------------------------------------------------------
# Custom synthetic exception types (HTTP-shaped, no real httpx required)
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Synthetic 429 Rate-Limited error.  Equivalent to httpx 429 for the probe."""

    status_code = 429


class ServerError(Exception):
    """Synthetic 500 Server-Error.  Equivalent to httpx 5xx for the probe."""

    status_code = 500


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FallbackProbeResult:
    """One row of the probe matrix.

    Attributes
    ----------
    surface_name:
        Canonical surface identifier — one of SURFACES.
    llm_enabled:
        True iff the v0.2 feature flag was ON for this run.
    llm_failure_mode:
        Which synthetic failure was injected (one of FAILURE_MODES).
    output_valid:
        True iff the surface produced a non-None, schema-valid output.
        This is the silence-by-default invariant we are verifying.
    output_matches_v0_1:
        True iff the output equals (within tolerance) what the deterministic
        v0.1 path would have produced.  Always True under failure modes
        because all failure modes must trigger fallback to v0.1.  Under
        'happy_path' this is False because the LLM stub returns a different
        synthetic answer.
    latency_ms:
        Wall-clock latency for the surface invocation, in milliseconds.
    error:
        Captured exception message (str(exc)) when the probe itself failed.
        None when the probe ran cleanly (regardless of whether the surface
        used the LLM or fell back).
    """

    surface_name: str
    llm_enabled: bool
    llm_failure_mode: str
    output_valid: bool
    output_matches_v0_1: bool
    latency_ms: float
    error: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# StubLLMCaller — used by all surfaces that take an LLMCaller-shaped object
# ---------------------------------------------------------------------------


class StubLLMCaller:
    """In-process stand-in for LLMCaller that injects a chosen failure mode.

    Implements the duck-typed interface used by the v0.2 surfaces:
        - .available() -> bool
        - .call(system_prompt, user_prompt, *, schema=None) -> (obj|None, raw)
        - .model_id : str

    Never makes any network call.  Never raises from .available().
    .call() may raise depending on the failure mode (those exceptions are
    expected to be caught by the surface's fallback machinery).
    """

    def __init__(
        self,
        failure_mode: str = "happy_path",
        *,
        model_id: str = "stub/probe-model",
        happy_payload_factory=None,
    ) -> None:
        if failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"failure_mode must be one of {FAILURE_MODES}; got {failure_mode!r}"
            )
        self.failure_mode = failure_mode
        self.model_id = model_id
        self._happy_payload_factory = happy_payload_factory
        self.calls: list[dict[str, Any]] = []  # for test introspection

    def available(self) -> bool:
        return True

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        schema: Optional[Type[Any]] = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Stub call — drives the failure mode chosen at construction time."""
        self.calls.append(
            {
                "system_prompt": system_prompt[:80],
                "user_prompt": user_prompt[:80],
                "schema": getattr(schema, "__name__", None),
            }
        )
        mode = self.failure_mode

        if mode == "timeout":
            # Use the stdlib TimeoutError — equivalent to asyncio.TimeoutError
            # and to a socket timeout for the purposes of fallback verification.
            raise TimeoutError("stub: synthetic LLM timeout")
        if mode == "rate_limit":
            raise RateLimitError("stub: synthetic 429 rate-limit")
        if mode == "server_error":
            raise ServerError("stub: synthetic 500 server error")
        if mode == "malformed_json":
            # Surface receives (None, raw) — its fallback machinery should
            # detect that obj is not a schema instance and fall back to v0.1.
            return None, {"_raw_text": "not-json {{ <<< malformed >>>"}
        if mode == "schema_invalid":
            # JSON parses but does not match the schema's required fields.
            return None, {"_raw_text": json.dumps({"unrelated_key": 42})}
        if mode == "empty":
            return None, {"_raw_text": ""}
        if mode == "happy_path":
            if self._happy_payload_factory is None:
                # No factory provided — surface treats this as parse failure.
                return None, {"_raw_text": ""}
            obj = self._happy_payload_factory(schema)
            return obj, {
                "choices": [{"message": {"content": "stub-happy-path"}}],
                "model": self.model_id,
            }
        # Unreachable
        raise AssertionError(f"unhandled failure_mode={mode!r}")


# ---------------------------------------------------------------------------
# Happy-path payload factories — used by StubLLMCaller in 'happy_path' mode
# ---------------------------------------------------------------------------


def _trader_happy_payload(schema):
    """Synthesize a valid TraderProposal for the happy-path probe."""
    from hermes_quant.agents.trader import TraderAction, TraderProposal

    if schema is TraderProposal or schema is None:
        return TraderProposal(
            action=TraderAction.BUY,
            size_fraction=0.10,
            entry_price=100.0,
            stop_loss=96.0,
            target_price=104.0,
            time_horizon_days=21,
            confidence=0.78,
            rationale="stub LLM happy-path proposal (probe).",
            warning_message=None,
        )
    return None


_RC_TURN_COUNTER = {"n": 0}


def _risk_committee_happy_payload(schema):
    """Synthesize a valid RiskCommitteeTurn for each persona, round-robin."""
    from hermes_quant.agents.risk_committee.committee import RiskCommitteeTurn

    if schema is not RiskCommitteeTurn:
        return None
    persona_order = ["aggressive", "conservative", "neutral"]
    idx = _RC_TURN_COUNTER["n"]
    _RC_TURN_COUNTER["n"] += 1
    persona = persona_order[idx % 3]
    return RiskCommitteeTurn(
        persona=persona,
        turn_index=idx,
        critique_text=f"stub LLM critique from {persona} (probe).",
        evidence_ids=[f"probe_evidence_{persona}"],
        risk_assessment="neutral",
        confidence=0.7,
    )


def _reflector_happy_payload(schema):
    """Synthesize a valid ReflectionLLMOutput for the happy-path probe."""
    try:
        from hermes_quant.memory.reflector import ReflectionLLMOutput
    except ImportError:  # pragma: no cover
        return None
    if schema is not ReflectionLLMOutput:
        return None
    return ReflectionLLMOutput(
        reflection_text=(
            "Stub LLM reflection text. Direction call held up reasonably "
            "well given the alpha figure. Lesson: probe-level synthetic."
        ),
        lesson_category="thesis_correct",
    )


# ---------------------------------------------------------------------------
# Per-surface probe functions
# ---------------------------------------------------------------------------


def probe_trader_node(failure_mode: str) -> FallbackProbeResult:
    """Probe the TraderNodeLLM surface under one synthetic failure mode.

    Verifies that the surface produces a valid TraderProposal regardless of
    whether the LLM call succeeds or fails.  Under any failure mode the
    output must equal the v0.1 deterministic path.
    """
    from hermes_quant.agents.trader import TraderNode, TraderNodeLLM, TraderProposal

    surface = "trader"
    research_plan = {
        "recommendation": "Buy",
        "confidence": 0.8,
        "rationale": "Strong upward momentum and bullish EMA crossover.",
        "strategic_actions": "Enter long at current market price.",
        "horizon_emphasis": "medium-term (20–40 days)",
    }
    advisor_signal = {
        "direction": 1,
        "confidence": 0.75,
        "magnitude": 0.5,
        "metadata": {"atr_relative": 0.02, "last_close": 100.0},
        "data_quality": {"bars_received": 200, "last_close": 100.0},
    }

    # Always compute the v0.1 reference output for matches_v0_1 comparison.
    v01_ref = TraderNode()(research_plan, advisor_signal)

    stub = StubLLMCaller(
        failure_mode=failure_mode, happy_payload_factory=_trader_happy_payload
    )

    start = time.monotonic()
    error: Optional[str] = None
    proposal: Optional[TraderProposal] = None
    try:
        with patch.dict(os.environ, {"HERMES_QUANT_TRADER_LLM": "1"}):
            node = TraderNodeLLM(llm_caller=stub)
            proposal = node(research_plan, advisor_signal)
    except Exception as exc:  # noqa: BLE001 — never raises out of the probe
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("probe_trader_node: surface raised unexpectedly")

    latency_ms = (time.monotonic() - start) * 1000.0

    output_valid = isinstance(proposal, TraderProposal)
    matches_v0_1 = bool(
        output_valid
        and proposal is not None
        and proposal.action == v01_ref.action
        and abs(proposal.size_fraction - v01_ref.size_fraction) < 1e-9
        and (
            (proposal.entry_price is None and v01_ref.entry_price is None)
            or (
                proposal.entry_price is not None
                and v01_ref.entry_price is not None
                and abs(proposal.entry_price - v01_ref.entry_price) < 1e-6
            )
        )
    )

    return FallbackProbeResult(
        surface_name=surface,
        llm_enabled=True,
        llm_failure_mode=failure_mode,
        output_valid=output_valid,
        output_matches_v0_1=matches_v0_1,
        latency_ms=round(latency_ms, 3),
        error=error,
        notes=f"calls={len(stub.calls)}",
    )


def probe_risk_committee(failure_mode: str) -> FallbackProbeResult:
    """Probe the RiskCommittee v0.2 surface under one synthetic failure mode."""
    from hermes_quant.agents.risk_committee.committee import RiskCommittee
    from hermes_quant.agents.risk_committee.committee import (
        RiskDebateSummary as _RDS,
    )
    from hermes_quant.agents.trader import TraderAction, TraderProposal

    surface = "risk_committee"
    plan = {
        "ticker": "AAPL",
        "recommendation": "Buy",
        "confidence": 0.75,
        "rationale": "Strong momentum and earnings beat.",
        "strategic_actions": "Enter long position.",
    }
    proposal = TraderProposal(
        action=TraderAction.BUY,
        size_fraction=0.10,
        entry_price=100.0,
        stop_loss=97.0,
        target_price=106.0,
        time_horizon_days=21,
        confidence=0.75,
        rationale="probe input proposal",
    )

    # v0.1 reference summary (no LLM)
    v01_ref = RiskCommittee().debate(proposal, plan, max_rounds=1)

    # Reset round-robin counter for happy-path turns.
    _RC_TURN_COUNTER["n"] = 0
    stub = StubLLMCaller(
        failure_mode=failure_mode,
        happy_payload_factory=_risk_committee_happy_payload,
    )

    start = time.monotonic()
    error: Optional[str] = None
    summary: Optional[_RDS] = None
    try:
        with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}):
            committee = RiskCommittee(llm_caller=stub)
            summary = committee.debate(proposal, plan, max_rounds=1)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("probe_risk_committee: surface raised unexpectedly")

    latency_ms = (time.monotonic() - start) * 1000.0

    output_valid = (
        summary is not None
        and hasattr(summary, "silence_multiplier")
        and 0.0 <= float(summary.silence_multiplier) <= 1.0
        and len(getattr(summary, "turns", []) or []) >= 1
    )
    # Under fallback the silence_multiplier should equal the v0.1 reference.
    matches_v0_1 = bool(
        output_valid
        and summary is not None
        and abs(float(summary.silence_multiplier) - float(v01_ref.silence_multiplier))
        < 1e-9
    )

    return FallbackProbeResult(
        surface_name=surface,
        llm_enabled=True,
        llm_failure_mode=failure_mode,
        output_valid=bool(output_valid),
        output_matches_v0_1=matches_v0_1,
        latency_ms=round(latency_ms, 3),
        error=error,
        notes=f"calls={len(stub.calls)}",
    )


def probe_reflector(failure_mode: str) -> FallbackProbeResult:
    """Probe the Reflector v0.2 surface under one synthetic failure mode."""
    from pathlib import Path
    import tempfile

    from hermes_quant.memory.reflector import Reflection, Reflector

    surface = "reflector"

    decision = {
        "schema_version": 1,
        "kind": "decision",
        "decision_id": "dec_probe_NVDA_aabbcc",
        "asof_decision": "2026-04-01T10:00:00+00:00",
        "ticker": "NVDA",
        "asset_class": "equity",
        "rating": "Buy",
        "direction": 1,
        "confidence": 0.80,
        "target_position_pct": 0.10,
        "thesis_summary": "Probe synthetic decision.",
        "llm_committee_model_id": "anthropic/claude-sonnet-4-5",
    }
    exit_record = {
        "asof_resolution": "2026-04-21T14:00:00+00:00",
        "entry_price": 800.0,
        "exit_price": 860.0,
        "benchmark_return": 0.02,
    }

    stub = StubLLMCaller(
        failure_mode=failure_mode,
        model_id="anthropic/claude-haiku-4-5",  # different from PM model — no self-grade
        happy_payload_factory=_reflector_happy_payload,
    )

    start = time.monotonic()
    error: Optional[str] = None
    reflection: Optional[Reflection] = None
    v01_ref: Optional[Reflection] = None

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ref_path = Path(tmpdir) / "reflections.jsonl"

            # v0.1 reference (no LLM caller, flag off)
            with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "0"}):
                v01_ref = Reflector(reflections_path=ref_path).reflect_on_close(
                    decision, exit_record
                )

            ref_path_v02 = Path(tmpdir) / "reflections_v02.jsonl"
            with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
                reflector = Reflector(
                    reflections_path=ref_path_v02,
                    llm_caller=stub,
                    model_name="anthropic/claude-haiku-4-5",
                )
                reflection = reflector.reflect_on_close(decision, exit_record)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("probe_reflector: surface raised unexpectedly")

    latency_ms = (time.monotonic() - start) * 1000.0

    output_valid = (
        reflection is not None
        and isinstance(reflection, Reflection)
        and bool(reflection.reflection_text)
        and reflection.lesson_category is not None
    )
    # Under failure modes, the v0.2 path must fall back to the v0.1 stub —
    # so the lesson_category must equal the deterministic one.
    matches_v0_1 = bool(
        output_valid
        and v01_ref is not None
        and reflection is not None
        and reflection.lesson_category == v01_ref.lesson_category
        and reflection.tau_observable == v01_ref.tau_observable
    )

    return FallbackProbeResult(
        surface_name=surface,
        llm_enabled=True,
        llm_failure_mode=failure_mode,
        output_valid=bool(output_valid),
        output_matches_v0_1=matches_v0_1,
        latency_ms=round(latency_ms, 3),
        error=error,
        notes=f"calls={len(stub.calls)}",
    )


def probe_regime_hmm(failure_mode: str) -> FallbackProbeResult:
    """Probe the HMM regime classifier surface under one synthetic failure mode.

    The HMM doesn't talk to an LLM per call — instead, the v0.2 path is gated
    by HERMES_QUANT_REGIME_HMM=1 which auto-instantiates an HMMClassifier.
    We map the LLM failure modes to model-load failure modes by patching
    ``HMMClassifier.classify`` to raise the equivalent exception, so the
    RegimeDetector's fallback-to-rule-based path is exercised.
    """
    import pandas as pd
    from hermes_quant.regime.detector import RegimeDetector, RegimeState
    from hermes_quant.regime.state_variables import StateVariables

    surface = "regime_hmm"
    sv = StateVariables(
        realized_vol_60d=0.18,
        realized_vol_percentile=0.55,
        yield_curve_slope=0.5,
        trend_strength=0.6,
        as_of=pd.Timestamp("2026-05-27T00:00:00Z"),
        metadata={},
    )

    # v0.1 rule-based reference (no HMM)
    v01_ref_state, v01_ref_reason = RegimeDetector().classify(sv)

    start = time.monotonic()
    error: Optional[str] = None
    state: Optional[RegimeState] = None
    reason: str = ""

    def _hmm_failure_classifier(_state_vars: StateVariables):
        if failure_mode == "timeout":
            raise TimeoutError("stub: synthetic HMM model-load timeout")
        if failure_mode == "rate_limit":
            raise RateLimitError("stub: synthetic 429 (HMM rate-limited remote model)")
        if failure_mode == "server_error":
            raise ServerError("stub: synthetic 500 (HMM remote model down)")
        if failure_mode == "malformed_json":
            raise ValueError("stub: synthetic malformed model state file")
        if failure_mode == "schema_invalid":
            # Return a string instead of (RegimeState, reason) tuple — surface
            # must reject this and fall back to rule-based.
            return "not_a_regime_state"
        if failure_mode == "empty":
            return None  # surface must reject and fall back
        if failure_mode == "happy_path":
            # Return a synthetic valid (RegimeState, reason) tuple.
            return RegimeState.BULL, "stub HMM happy-path: synthetic BULL"
        raise AssertionError(f"unhandled failure_mode={failure_mode!r}")

    try:
        detector = RegimeDetector(hmm_classifier=_hmm_failure_classifier)
        state, reason = detector.classify(sv)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("probe_regime_hmm: surface raised unexpectedly")

    latency_ms = (time.monotonic() - start) * 1000.0

    output_valid = isinstance(state, RegimeState)
    matches_v0_1 = bool(output_valid and state == v01_ref_state)

    return FallbackProbeResult(
        surface_name=surface,
        llm_enabled=True,
        llm_failure_mode=failure_mode,
        output_valid=output_valid,
        output_matches_v0_1=matches_v0_1,
        latency_ms=round(latency_ms, 3),
        error=error,
        notes=f"reason={reason[:60]!r}",
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


_SURFACE_TO_PROBE = {
    "trader": probe_trader_node,
    "risk_committee": probe_risk_committee,
    "reflector": probe_reflector,
    "regime_hmm": probe_regime_hmm,
}

# For each surface, which failure modes are meaningful.  HMM has fewer
# meaningful modes since it doesn't talk to an LLM per call.
_SURFACE_MODES: dict[str, tuple[str, ...]] = {
    "trader": FAILURE_MODES,
    "risk_committee": FAILURE_MODES,
    "reflector": FAILURE_MODES,
    "regime_hmm": _HMM_MEANINGFUL_MODES,
}


def run_fallback_probe(
    surfaces: Optional[list[str]] = None,
    failure_modes: Optional[list[str]] = None,
    *,
    dry_run: bool = True,
) -> list[FallbackProbeResult]:
    """Run the full fallback-probe matrix and return results.

    Args:
        surfaces: list of surface names; defaults to all four SURFACES.
        failure_modes: list of failure-mode names; defaults to all FAILURE_MODES.
            For each surface, only the meaningful modes are exercised.
        dry_run: parameter retained for future API compatibility (the probe
            never performs real network calls regardless of this flag).

    Returns:
        list[FallbackProbeResult] — one per (surface, failure_mode) pair.

    Never raises — all exceptions are captured in the per-result ``error`` field.
    """
    selected_surfaces = list(surfaces) if surfaces else list(SURFACES)
    selected_modes = list(failure_modes) if failure_modes else list(FAILURE_MODES)

    results: list[FallbackProbeResult] = []
    for surface in selected_surfaces:
        if surface not in _SURFACE_TO_PROBE:
            results.append(
                FallbackProbeResult(
                    surface_name=surface,
                    llm_enabled=False,
                    llm_failure_mode="",
                    output_valid=False,
                    output_matches_v0_1=False,
                    latency_ms=0.0,
                    error=f"unknown_surface: {surface!r}",
                )
            )
            continue
        meaningful = _SURFACE_MODES[surface]
        for mode in selected_modes:
            if mode not in meaningful:
                # Mode is not applicable for this surface (e.g. HMM has no per-call
                # rate-limit semantics).  Record a row marked as 'skipped' so the
                # cross-product is observable, but DO NOT count it as a pass —
                # output_valid=False keeps the summary honest, and notes makes
                # the reason for non-evaluation visible to operators.
                # (Per v0.4 MoA F5 finding: previous version emitted output_valid=True
                # for skipped rows, which fabricated successes in the PASS summary.)
                results.append(
                    FallbackProbeResult(
                        surface_name=surface,
                        llm_enabled=True,
                        llm_failure_mode=mode,
                        output_valid=False,
                        output_matches_v0_1=False,
                        latency_ms=0.0,
                        error=None,
                        notes="skipped: mode not meaningful for surface",
                    )
                )
                continue
            probe = _SURFACE_TO_PROBE[surface]
            try:
                results.append(probe(mode))
            except Exception as exc:  # noqa: BLE001
                # Defensive — should never happen because each probe_* is
                # already exception-safe.  Recorded just in case.
                results.append(
                    FallbackProbeResult(
                        surface_name=surface,
                        llm_enabled=True,
                        llm_failure_mode=mode,
                        output_valid=False,
                        output_matches_v0_1=False,
                        latency_ms=0.0,
                        error=f"probe_runner_caught: {type(exc).__name__}: {exc}",
                    )
                )
    return results


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def format_results_human(results: list[FallbackProbeResult]) -> str:
    """Render results as a multi-line human-readable table."""
    if not results:
        return "(no results)"

    header = (
        f"{'surface':<16} {'failure_mode':<16} {'valid':<6} "
        f"{'matches_v01':<13} {'latency_ms':<10} {'error':<40}"
    )
    rule = "-" * len(header)
    lines = [
        "hermes-quant fallback-probe — silence-by-default verification (ADR-0060)",
        f"asof: {datetime.now(UTC).isoformat(timespec='seconds')}",
        rule,
        header,
        rule,
    ]
    for r in results:
        err = r.error or ""
        if len(err) > 40:
            err = err[:37] + "..."
        lines.append(
            f"{r.surface_name:<16} {r.llm_failure_mode:<16} "
            f"{('yes' if r.output_valid else 'NO'):<6} "
            f"{('yes' if r.output_matches_v0_1 else 'no'):<13} "
            f"{r.latency_ms:<10.2f} {err:<40}"
        )
    lines.append(rule)
    # Skipped rows (mode not applicable to surface) are excluded from
    # valid/match denominator AND numerator — counting them inflates PASS
    # rate by construction (per v0.4 MoA F5).
    skipped = [r for r in results if (r.notes or "").startswith("skipped:")]
    evaluated = [r for r in results if r not in skipped]
    n_total_evaluated = len(evaluated)
    n_valid = sum(1 for r in evaluated if r.output_valid)
    n_match = sum(1 for r in evaluated if r.output_matches_v0_1)
    lines.append(
        f"summary: {n_valid}/{n_total_evaluated} output_valid, "
        f"{n_match}/{n_total_evaluated} match v0.1 deterministic"
        + (f" ({len(skipped)} skipped: mode not meaningful)" if skipped else "")
    )
    # PASS is determined ONLY over evaluated probes — skipped rows do not vote.
    if n_total_evaluated > 0 and n_valid == n_total_evaluated:
        lines.append("RESULT: PASS — silence-by-default holds for all surfaces.")
    elif n_total_evaluated == 0:
        lines.append("RESULT: NO-OP — no probes were evaluated (all skipped).")
    else:
        lines.append("RESULT: FAIL — at least one surface failed to fall back.")
    return "\n".join(lines)


def format_results_json(results: list[FallbackProbeResult]) -> str:
    """Render results as a JSON array (one object per row)."""
    skipped = [r for r in results if (r.notes or "").startswith("skipped:")]
    evaluated = [r for r in results if r not in skipped]
    return json.dumps(
        {
            "asof": datetime.now(UTC).isoformat(timespec="seconds"),
            "n_total": len(results),
            "n_evaluated": len(evaluated),
            "n_skipped": len(skipped),
            "n_valid": sum(1 for r in evaluated if r.output_valid),
            "n_match_v01": sum(1 for r in evaluated if r.output_matches_v0_1),
            "results": [r.to_dict() for r in results],
        },
        indent=2,
        default=str,
    )
