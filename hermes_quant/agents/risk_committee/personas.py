"""hermes_quant.agents.risk_committee.personas — Aggressive/Conservative/Neutral.

ADR-0043: Wave 3 — three risk-management personas that debate a TraderProposal.

Each persona has:
  * SYSTEM_PROMPT_TEMPLATE — a TauricResearch-style system prompt with the
    verbatim "Output conversationally..." preamble (gap #2 anti-pattern fix:
    forces real debate text rather than bullet-point hiding).
  * decide(proposal, plan, prior_turns) — deterministic v0.1 decision rule
    returning ("amplify"|"silence"|"neutral", confidence, critique_text).

v0.2 LLM wiring is deferred behind RiskCommittee's ``llm_caller`` parameter;
the personas themselves stay deterministic and are reused by the LLM path
to render the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from hermes_quant.agents.trader import TraderProposal

# ---------------------------------------------------------------------------
# Verbatim TauricResearch preamble — see /tmp/research-tradingagents.md gap #2
# ---------------------------------------------------------------------------

CONVERSATIONAL_PREAMBLE: str = (
    "Output conversationally as if you are speaking without any special "
    "formatting"
)

# ---------------------------------------------------------------------------
# Decision rule constants (v0.1 deterministic)
# ---------------------------------------------------------------------------

# Aggressive amplifies when proposal is small enough that risk-adjusted upside
# could justify a tilt. Above this fraction, the deterministic gate is already
# allocating meaningful capital — Aggressive stays neutral rather than piling on.
_AGGRESSIVE_AMPLIFY_THRESHOLD: float = 0.15

# Conservative silences when stop placement is wider than this fraction of
# entry — i.e. >5% loss tolerance is too lax for a "Conservative" persona.
_CONSERVATIVE_STOP_DISTANCE_THRESHOLD: float = 0.05


RiskAssessment = Literal["amplify", "silence", "neutral"]


# ---------------------------------------------------------------------------
# Persona base + concrete classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PersonaDecision:
    """Internal result of a persona's deterministic decision rule."""

    risk_assessment: RiskAssessment
    confidence: float
    critique_text: str
    evidence_ids: list[str]


class RiskPersona:
    """Base persona — subclasses MUST set name and SYSTEM_PROMPT_TEMPLATE."""

    name: str = "base"
    SYSTEM_PROMPT_TEMPLATE: str = ""

    def render_system_prompt(self, **kwargs: Any) -> str:
        """Render the system prompt template with ticker/asof context.

        Returns the template verbatim if no placeholders apply. Used by the
        v0.2 LLM path; v0.1 just calls this in tests to verify the verbatim
        preamble is present.
        """
        try:
            return self.SYSTEM_PROMPT_TEMPLATE.format(**kwargs)
        except (KeyError, IndexError):
            # Missing placeholders are acceptable — the v0.1 deterministic
            # path doesn't use the rendered prompt for any decision.
            return self.SYSTEM_PROMPT_TEMPLATE

    def decide(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: Sequence[Any] = (),
    ) -> _PersonaDecision:
        raise NotImplementedError


class AggressivePersona(RiskPersona):
    """Aggressive — champions high-reward, high-risk opportunities.

    v0.1 rule:
      * size_fraction < 0.15  -> amplify (vote: small position, push harder)
      * else                  -> neutral (already a meaningful tilt)

    Note: "amplify" votes are RECORDED in the debate trail but never raise
    silence_multiplier above 1.0 (CV5 anti-pattern guard, ADR-0043).
    """

    name = "aggressive"

    SYSTEM_PROMPT_TEMPLATE: str = (
        "You are the Aggressive Risk Manager on a 3-person risk committee. "
        "Your role is to actively champion high-reward, high-risk "
        "opportunities and emphasize bold strategies. When you evaluate a "
        "trader proposal, you focus on the upside potential, the "
        "competitive edge, and the strategic actions the trader is "
        "proposing. Push back hard on overly cautious sizing or excessively "
        "tight stops that you believe will get whipped out.\n\n"
        "{conversational_preamble}, presenting your arguments as if you "
        "were directly speaking to your colleagues at the round-robin "
        "debate. Engage with their points, defend your high-conviction "
        "stance, and make a compelling case for why the proposal — if "
        "anything — should be sized MORE aggressively or held LONGER.\n\n"
        "Context:\n"
        "  Ticker: {ticker}\n"
        "  Trader proposal: {proposal_json}\n"
        "  Research plan: {plan_json}\n"
        "  Prior turns: {prior_turns_json}\n"
    )

    def decide(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: Sequence[Any] = (),
    ) -> _PersonaDecision:
        size = float(proposal.size_fraction or 0.0)
        evidence_ids = [f"size_fraction={size:.4f}"]

        if size < _AGGRESSIVE_AMPLIFY_THRESHOLD and proposal.action.value != "HOLD":
            return _PersonaDecision(
                risk_assessment="amplify",
                confidence=0.7,
                critique_text=(
                    f"This sizing of {size:.2%} is timid given the research "
                    f"plan's {plan.get('recommendation', 'rating')} rating "
                    f"with {plan.get('confidence', 0.0):.0%} confidence. "
                    "I would normally argue for a stronger tilt — though I "
                    "respect that the deterministic gate has already chosen "
                    "this number, so my amplify vote is recorded for the "
                    "audit trail only."
                ),
                evidence_ids=evidence_ids,
            )

        return _PersonaDecision(
            risk_assessment="neutral",
            confidence=0.55,
            critique_text=(
                f"Sizing of {size:.2%} is in the meaningful-tilt zone. "
                "I have no objection — the upside case is reasonably "
                "captured here without my needing to push for more."
            ),
            evidence_ids=evidence_ids,
        )


class ConservativePersona(RiskPersona):
    """Conservative — prioritizes asset protection, minimize volatility.

    v0.1 rule:
      * stop_loss is None                                -> silence
      * |entry - stop_loss| / entry > 5%                 -> silence (too wide)
      * else                                             -> neutral
    """

    name = "conservative"

    SYSTEM_PROMPT_TEMPLATE: str = (
        "You are the Conservative Risk Manager on a 3-person risk "
        "committee. Your role is to prioritize asset protection, minimize "
        "volatility exposure, and challenge the trader whenever the stop "
        "placement is missing, too wide, or the position size is "
        "incompatible with the worst-case loss the strategy can absorb. "
        "You are not the enemy of returns — you are the guardian of "
        "long-run survival.\n\n"
        "{conversational_preamble}, presenting your arguments as if you "
        "were directly speaking to your colleagues. Challenge the "
        "Aggressive risk manager's arguments specifically when their "
        "reasoning would expand drawdown. If the stop is missing or wider "
        "than 5% of entry, recommend SILENCING the trade — the trader can "
        "wait for a better setup.\n\n"
        "Context:\n"
        "  Ticker: {ticker}\n"
        "  Trader proposal: {proposal_json}\n"
        "  Research plan: {plan_json}\n"
        "  Prior turns: {prior_turns_json}\n"
    )

    def decide(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: Sequence[Any] = (),
    ) -> _PersonaDecision:
        entry = proposal.entry_price
        stop = proposal.stop_loss

        if stop is None:
            return _PersonaDecision(
                risk_assessment="silence",
                confidence=0.85,
                critique_text=(
                    "There is no stop_loss on this proposal. We are not in "
                    "the business of taking unbounded losses. I recommend "
                    "we SILENCE this trade until the trader can specify a "
                    "concrete invalidation level."
                ),
                evidence_ids=["stop_loss=None"],
            )

        if entry is None:
            # No entry to compare against — fall through to neutral; the
            # missing-stop case is already handled above.
            return _PersonaDecision(
                risk_assessment="neutral",
                confidence=0.5,
                critique_text=(
                    "No entry price is specified, so I cannot evaluate the "
                    "stop distance. I am abstaining from this turn."
                ),
                evidence_ids=["entry_price=None"],
            )

        stop_distance_pct = abs(entry - stop) / entry
        evidence_ids = [
            f"entry_price={entry:.4f}",
            f"stop_loss={stop:.4f}",
            f"stop_distance_pct={stop_distance_pct:.4f}",
        ]

        if stop_distance_pct > _CONSERVATIVE_STOP_DISTANCE_THRESHOLD:
            return _PersonaDecision(
                risk_assessment="silence",
                confidence=0.8,
                critique_text=(
                    f"This stop is {stop_distance_pct:.1%} away from "
                    "entry — too wide for a controlled-risk position. "
                    "I recommend SILENCING and re-pricing the entry, or "
                    "tightening the stop, before we put capital at risk."
                ),
                evidence_ids=evidence_ids,
            )

        return _PersonaDecision(
            risk_assessment="neutral",
            confidence=0.6,
            critique_text=(
                f"Stop is {stop_distance_pct:.1%} from entry — within my "
                "tolerance for controlled risk. I have no objection on "
                "stop placement grounds."
            ),
            evidence_ids=evidence_ids,
        )


class NeutralPersona(RiskPersona):
    """Neutral — balanced perspective, weighing both potential benefits and risks.

    v0.1 rule:
      * if both Aggressive AND Conservative voted "silence" earlier in the
        same round — second the silence (shared concern raises the bar)
      * if both voted "amplify" — second neutral (Neutral never amplifies;
        CV5 anti-pattern guard means amplify votes are audit-only anyway)
      * else                                             -> neutral
    """

    name = "neutral"

    SYSTEM_PROMPT_TEMPLATE: str = (
        "You are the Neutral Risk Manager on a 3-person risk committee. "
        "Your role is to provide a balanced perspective, weighing both the "
        "potential benefits and risks of the trader's proposal. You "
        "consider the arguments of both the Aggressive and Conservative "
        "risk managers and synthesize a measured judgment. You do not "
        "rubber-stamp either side; you also do not amplify risk on your "
        "own initiative.\n\n"
        "{conversational_preamble}, presenting your arguments as if you "
        "were directly speaking to your colleagues. If both Aggressive "
        "and Conservative are signaling silence, second that silence — "
        "two-out-of-three concern is a real signal. Otherwise, stay "
        "neutral and let the deterministic risk gate make the final "
        "call.\n\n"
        "Context:\n"
        "  Ticker: {ticker}\n"
        "  Trader proposal: {proposal_json}\n"
        "  Research plan: {plan_json}\n"
        "  Prior turns: {prior_turns_json}\n"
    )

    def decide(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: Sequence[Any] = (),
    ) -> _PersonaDecision:
        # Look at the most recent Aggressive + Conservative turns in the
        # current round. We use 'amplify'/'silence'/'neutral' from the prior
        # turns list, which the committee passes in order.
        agg_assessment: str | None = None
        cons_assessment: str | None = None
        for t in reversed(list(prior_turns)):
            persona = getattr(t, "persona", None)
            if persona == "aggressive" and agg_assessment is None:
                agg_assessment = getattr(t, "risk_assessment", None)
            elif persona == "conservative" and cons_assessment is None:
                cons_assessment = getattr(t, "risk_assessment", None)
            if agg_assessment is not None and cons_assessment is not None:
                break

        evidence_ids = [
            f"prior_aggressive_assessment={agg_assessment}",
            f"prior_conservative_assessment={cons_assessment}",
        ]

        if agg_assessment == "silence" and cons_assessment == "silence":
            return _PersonaDecision(
                risk_assessment="silence",
                confidence=0.9,
                critique_text=(
                    "Both my Aggressive and Conservative colleagues are "
                    "calling for silence on this trade. When the bull and "
                    "the bear both want out, the right answer is to step "
                    "aside and wait for a cleaner setup. I second the "
                    "silence vote."
                ),
                evidence_ids=evidence_ids,
            )

        # NOTE: Even when both A and C "amplify", Neutral does NOT amplify —
        # see ADR-0043 §"silence-multiplier-only invariant". Amplify votes
        # are recorded for audit but cannot raise silence_multiplier above 1.0.
        return _PersonaDecision(
            risk_assessment="neutral",
            confidence=0.55,
            critique_text=(
                "After weighing both Aggressive's and Conservative's "
                "arguments, I see no reason to override the deterministic "
                "gate's sizing. I'll stay neutral and let the gate run."
            ),
            evidence_ids=evidence_ids,
        )
