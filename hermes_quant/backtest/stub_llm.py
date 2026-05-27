"""hermes_quant.backtest.stub_llm — Deterministic StubLLMCommittee (Wave 6a / ADR-0045).

Purpose
-------
When dry_run_llm=True, the WalkForwardEngine replaces real LLM committee calls
with this stub so the full advisor → trader → risk_committee pipeline can run
in backtest without any API calls.

Design contract
---------------
- DETERMINISTIC given the same input direction.  Identical inputs → identical
  outputs across any number of calls, any process, any platform.
  This is the testability contract (Mai0313/TradingAgents --dry-run StubChatModel
  pattern; see also §3.2 of arxiv:2605.19337).
- Returns a valid ResearchPlan-shaped dict with the same schema consumed by
  TraderNode and RiskCommittee, so no production code needs special-casing.
- No random, no time-based state, no side effects.

Mapping rule (direction → recommendation):
    direction > 0  →  "Buy"       (strong bullish signal)
    direction == 0 →  "Hold"      (neutral)
    direction < 0  →  "Sell"      (bearish signal)

The magnitude of direction is used to set confidence proportionally.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Direction → recommendation mapping (deterministic)
# ---------------------------------------------------------------------------

_DIR_TO_RECOMMENDATION: dict[int, str] = {
    1: "Buy",
    0: "Hold",
    -1: "Sell",
}

_DIR_TO_STRATEGIC_ACTIONS: dict[int, str] = {
    1: "Enter long position per analyst consensus.",
    0: "Maintain current allocation; no new entry.",
    -1: "Reduce or exit long exposure; consider short.",
}

_DIR_TO_HORIZON: dict[int, str] = {
    1: "medium-term (21-30 days)",
    0: "short-term (14 days)",
    -1: "short-term (14 days)",
}


def _clamp_01(v: float) -> float:
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# StubLLMCommittee
# ---------------------------------------------------------------------------


class StubLLMCommittee:
    """Deterministic drop-in for LLM committee calls during backtesting.

    Implements the same call signature as the production LLM committee but
    returns DETERMINISTIC outputs so backtest runs are reproducible without
    any API keys or network access.

    Usage
    -----
    ::

        stub = StubLLMCommittee()
        plan = stub.research_plan(direction=1, confidence=0.75, symbol="AAPL")
        # Returns a ResearchPlan-compatible dict deterministically.

    The stub can also be used as a plain callable matching the
    ``(system_prompt: str, user_prompt: str) -> str`` signature expected by
    ``RiskCommittee(llm_caller=...)``.
    """

    # ------------------------------------------------------------------
    # Primary interface: produce a ResearchPlan-shaped dict
    # ------------------------------------------------------------------

    def research_plan(
        self,
        direction: int,
        confidence: float,
        symbol: str = "STUB",
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic ResearchPlan-shaped dict.

        Parameters
        ----------
        direction:
            +1 bullish, 0 neutral, -1 bearish.  Values outside [-1, 1] are
            clamped to the nearest key.
        confidence:
            Signal confidence in [0, 1].  Used as-is for the plan confidence.
        symbol:
            Ticker symbol (informational only; does not affect output).
        extra_context:
            Ignored — present for API compatibility.

        Returns
        -------
        dict
            ResearchPlan-compatible dict with keys:
            recommendation, confidence, rationale, strategic_actions,
            horizon_emphasis, signal_provenance.
        """
        canonical_dir = self._canonical_direction(direction)
        recommendation = _DIR_TO_RECOMMENDATION[canonical_dir]
        clipped_conf = _clamp_01(confidence)

        return {
            "recommendation": recommendation,
            "confidence": clipped_conf,
            "rationale": (
                f"StubLLMCommittee: deterministic {recommendation} signal for {symbol}. "
                f"Input direction={direction}, confidence={confidence:.3f}. "
                "Used in walk-forward dry-run mode (ADR-0045)."
            ),
            "strategic_actions": _DIR_TO_STRATEGIC_ACTIONS[canonical_dir],
            "horizon_emphasis": _DIR_TO_HORIZON[canonical_dir],
            "signal_provenance": {
                "source": "StubLLMCommittee",
                "direction": direction,
                "confidence": clipped_conf,
                "symbol": symbol,
            },
        }

    # ------------------------------------------------------------------
    # llm_caller compatibility: (system_prompt, user_prompt) -> str
    # ------------------------------------------------------------------

    def __call__(self, system_prompt: str, user_prompt: str) -> str:  # noqa: ARG002
        """RiskCommittee llm_caller interface.

        Always returns a deterministic neutral critique so the risk committee
        can run its full debate loop without an LLM.

        The response is intentionally short and consistent so persona parsing
        (regex / JSON extraction) doesn't encounter edge cases.
        """
        return (
            "RISK ASSESSMENT: neutral\n"
            "CONFIDENCE: 0.5\n"
            "CRITIQUE: StubLLMCommittee dry-run mode — deterministic neutral stance."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_direction(direction: int) -> int:
        """Clamp direction to canonical {-1, 0, 1}."""
        if direction > 0:
            return 1
        if direction < 0:
            return -1
        return 0
