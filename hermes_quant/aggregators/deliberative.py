"""Deliberative committee aggregator.

This is a deterministic scaffold for TradingAgents-style collaboration:
research debate -> trader synthesis -> risk debate -> portfolio manager. Future
Hermes/model-generated committee turns can be injected through MarketContext
extras, but aggregate() itself stays local and replayable.

Three TradingAgents safety patterns are enforced at the intake boundary:

1. **Two-tier LLM split** (`tier='quick' | 'deep' | 'deterministic'`):
   Quick-tier models drive high-volume specialist/debate turns. Deep-tier
   models drive final-synthesis turns (`trader`, `portfolio_manager`).
   A `tier='quick'` turn bound to `role='portfolio_manager'` or
   `role='trader'` is REJECTED at intake and the deterministic fallback
   fills the slot. A quick model cannot be promoted into a final-synthesis
   role at runtime.

2. **Bull/bear deterministic turn cap**:
   A non-converging debate cannot run forever; the deterministic turn cap
   forces termination at `2 * max_debate_rounds` total bull+bear turns. The
   cap is set at construction time and is not negotiable per ADR-0031
   invariants — future amendments must change it via ADR amendment, not
   runtime parameter.

3. **msg-clear at intake boundary**:
   Per TradingAgents msg-clear discipline, cross-role context pollution is
   prevented at intake. Agent context that produces a turn does NOT travel
   to the next role's context. Keys `messages`, `tool_calls`,
   `context_messages`, `prior_messages` are stripped from inbound turn
   metadata.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np

from hermes_quant.aggregators.bma import ABSTAIN_THRESHOLD, BMAAggregator
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    Direction,
    EpisodeOutcome,
    MarketContext,
)

logger = logging.getLogger(__name__)

CommitteeRole = Literal[
    "bull_researcher",
    "bear_researcher",
    "neutral_researcher",
    "trader",
    "risk_aggressive",
    "risk_conservative",
    "risk_neutral",
    "portfolio_manager",
]

LLMTier = Literal["quick", "deep", "deterministic"]

# Roles that require deep-tier models. Quick-tier turns bound to these roles
# are rejected at intake (deterministic fallback fills the slot).
_DEEP_REQUIRED_ROLES: frozenset[str] = frozenset({"trader", "portfolio_manager"})

# Roles whose default tier is 'quick' when a model-backed turn omits the
# explicit tier field.
_QUICK_DEFAULT_ROLES: frozenset[str] = frozenset(
    {
        "bull_researcher",
        "bear_researcher",
        "neutral_researcher",
        "risk_aggressive",
        "risk_conservative",
        "risk_neutral",
    }
)

# Metadata keys that are upstream agent-context artifacts and MUST NOT leak
# between roles (msg-clear discipline).
_MSG_CLEAR_KEYS: tuple[str, ...] = (
    "messages",
    "tool_calls",
    "context_messages",
    "prior_messages",
)


@dataclass(frozen=True)
class CommitteeTurn:
    role: CommitteeRole
    stance: str
    direction: Direction
    confidence: float
    rationale: str
    model: str = "deterministic:deliberative_committee"
    input_hash: str | None = None
    metadata: dict[str, Any] | None = None
    tier: LLMTier = "deterministic"


def _infer_tier_from_role(role: str) -> LLMTier:
    """Infer LLM tier from role when an inbound turn omits explicit tier.

    - bull/bear/neutral researcher and risk_* -> 'quick' (specialist debate)
    - trader, portfolio_manager -> 'deep' (final synthesis)
    - everything else -> 'quick'
    """
    if role in _DEEP_REQUIRED_ROLES:
        return "deep"
    if role in _QUICK_DEFAULT_ROLES:
        return "quick"
    return "quick"


class DeliberativeCommitteeAggregator:
    """Aggregator that records structured disagreement before final signal.

    The first version is intentionally deterministic. It delegates statistical
    combination to BMA, then adjusts confidence downward when the committee sees
    high disagreement or insufficient voices. This makes deliberation a safety
    layer, not a leverage amplifier.

    Three TradingAgents safety patterns are enforced at intake (see module
    docstring): two-tier LLM split, bull/bear deterministic turn cap, and
    msg-clear at intake boundary.
    """

    name = "deliberative_committee"

    def __init__(
        self,
        *,
        baseline: BMAAggregator | None = None,
        min_effective_views: int = 2,
        disagreement_penalty: float = 0.25,
        semantic_bonus_cap: float = 0.05,
        max_debate_rounds: int = 1,
    ) -> None:
        self.baseline = baseline or BMAAggregator()
        self.min_effective_views = min_effective_views
        self.disagreement_penalty = disagreement_penalty
        self.semantic_bonus_cap = semantic_bonus_cap
        self.max_debate_rounds = max_debate_rounds
        # Counters populated during _model_turns_from_context for safety
        # metadata reporting. Reset per aggregate() call.
        self._dropped_turns_last: int = 0

    def aggregate(self, views: list[AnalystView], context: MarketContext) -> AggregatedSignal:
        # Reset per-call safety counters.
        self._dropped_turns_last = 0

        effective_views = [v for v in (views or []) if v.confidence >= ABSTAIN_THRESHOLD]
        baseline_signal = self.baseline.aggregate(effective_views, context)
        turns = self._build_turns(effective_views, baseline_signal, context)
        disagreement = self._disagreement_score(effective_views)

        if len(effective_views) < self.min_effective_views:
            final = self._flat_from_baseline(
                baseline_signal,
                effective_views,
                turns,
                disagreement,
                "insufficient_effective_views",
            )
            return final

        confidence = float(baseline_signal.confidence)
        confidence_raw = float(baseline_signal.confidence_raw)
        if disagreement > 0:
            confidence = max(0.0, confidence - disagreement * self.disagreement_penalty)
            confidence_raw = max(0.0, confidence_raw - disagreement * self.disagreement_penalty)

        semantic_views = [v for v in effective_views if "semantic" in v.analyst.lower()]
        if semantic_views and baseline_signal.direction != 0:
            aligned_semantic = [v for v in semantic_views if v.direction == baseline_signal.direction]
            if aligned_semantic:
                confidence = min(1.0, confidence + min(self.semantic_bonus_cap, 0.02 * len(aligned_semantic)))
                confidence_raw = min(1.0, confidence_raw + min(self.semantic_bonus_cap, 0.02 * len(aligned_semantic)))

        if disagreement >= 0.80:
            return self._flat_from_baseline(
                baseline_signal,
                effective_views,
                turns,
                disagreement,
                "high_committee_disagreement",
            )

        metadata = self._metadata(effective_views, turns, disagreement, "accepted")
        return AggregatedSignal(
            asset=baseline_signal.asset,
            timeframe=baseline_signal.timeframe,
            asset_class=baseline_signal.asset_class,
            asof=baseline_signal.asof,
            direction=baseline_signal.direction,
            magnitude=baseline_signal.magnitude,
            confidence=confidence,
            confidence_raw=confidence_raw,
            horizon=baseline_signal.horizon,
            components=tuple(effective_views),
            aggregator=self.name,
            metadata=metadata,
        )

    def update(self, outcome: EpisodeOutcome) -> None:
        self.baseline.update(outcome)

    def status(self) -> dict[str, Any]:
        base_status = self.baseline.status() if hasattr(self.baseline, "status") else {}
        return {
            "name": self.name,
            "baseline": base_status,
            "min_effective_views": self.min_effective_views,
            "disagreement_penalty": self.disagreement_penalty,
            "semantic_bonus_cap": self.semantic_bonus_cap,
            "max_debate_rounds": self.max_debate_rounds,
        }

    def _build_turns(
        self,
        views: list[AnalystView],
        baseline_signal: AggregatedSignal,
        context: MarketContext,
    ) -> list[CommitteeTurn]:
        long_views = [v for v in views if v.direction > 0]
        short_views = [v for v in views if v.direction < 0]
        neutral_views = [v for v in views if v.direction == 0]
        bull_strength = self._side_strength(long_views)
        bear_strength = self._side_strength(short_views)
        neutral_strength = self._side_strength(neutral_views)
        # Deterministic scaffold turns: one bull + one bear (count = 2,
        # exactly at cap when max_debate_rounds=1).
        deterministic_turns = [
            CommitteeTurn(
                role="bull_researcher",
                stance="bull_case",
                direction=1,
                confidence=min(1.0, bull_strength),
                rationale=f"{len(long_views)} bullish effective views; weighted strength={bull_strength:.3f}",
            ),
            CommitteeTurn(
                role="bear_researcher",
                stance="bear_case",
                direction=-1,
                confidence=min(1.0, bear_strength),
                rationale=f"{len(short_views)} bearish effective views; weighted strength={bear_strength:.3f}",
            ),
            CommitteeTurn(
                role="neutral_researcher",
                stance="neutral_case",
                direction=0,
                confidence=min(1.0, neutral_strength),
                rationale=f"{len(neutral_views)} neutral effective views; abstention/flat strength={neutral_strength:.3f}",
            ),
            CommitteeTurn(
                role="trader",
                stance="provisional_signal",
                direction=baseline_signal.direction,
                confidence=float(baseline_signal.confidence),
                rationale="BMA baseline translated specialist views into provisional signal",
            ),
            CommitteeTurn(
                role="risk_aggressive",
                stance="allow_size_if_gate_passes",
                direction=baseline_signal.direction,
                confidence=max(0.0, float(baseline_signal.confidence) - 0.10),
                rationale="Aggressive risk accepts baseline direction but cannot bypass deterministic gate",
            ),
            CommitteeTurn(
                role="risk_conservative",
                stance="prefer_silence_on_uncertainty",
                direction=0 if self._disagreement_score(views) > 0.25 else baseline_signal.direction,
                confidence=min(1.0, self._disagreement_score(views) + 0.25),
                rationale="Conservative risk penalizes disagreement and missing voices",
            ),
            CommitteeTurn(
                role="risk_neutral",
                stance="baseline_with_penalty",
                direction=baseline_signal.direction,
                confidence=max(0.0, float(baseline_signal.confidence) - self._disagreement_score(views) * 0.15),
                rationale="Neutral risk follows baseline after disagreement penalty",
            ),
            CommitteeTurn(
                role="portfolio_manager",
                stance="final_synthesis",
                direction=baseline_signal.direction,
                confidence=float(baseline_signal.confidence),
                rationale="Final synthesis remains subject to risk gate and silence bias",
            ),
        ]
        # Count bull+bear from deterministic turns toward the cap so inbound
        # bull/bear turns drop in once the cap is reached.
        deterministic_bull_bear = sum(
            1 for t in deterministic_turns if t.role in ("bull_researcher", "bear_researcher")
        )
        model_turns = self._model_turns_from_context(
            context, existing_bull_bear=deterministic_bull_bear
        )
        return [*deterministic_turns, *model_turns]

    def _model_turns_from_context(
        self, context: MarketContext, *, existing_bull_bear: int = 0
    ) -> list[CommitteeTurn]:
        """Parse inbound model-backed turns with three safety filters.

        1. Two-tier LLM split: a turn declaring `tier='quick'` for a role in
           `_DEEP_REQUIRED_ROLES` is rejected.
        2. Bull/bear turn cap: drop additional bull/bear turns once
           `bull_bear_count >= 2 * max_debate_rounds`.
        3. msg-clear: strip upstream-context keys (`messages`, `tool_calls`,
           `context_messages`, `prior_messages`) from turn metadata.
        """
        raw_turns = context.extras.get("committee_turns") if context.extras else None
        turns: list[CommitteeTurn] = []
        bull_bear_count = existing_bull_bear
        cap = 2 * self.max_debate_rounds
        dropped = 0
        for raw in raw_turns or []:
            try:
                # Normalize to dict form for filter logic; preserve the
                # CommitteeTurn-input path for callers passing instances.
                if isinstance(raw, CommitteeTurn):
                    role = raw.role
                    tier = raw.tier
                    # msg-clear on instance metadata (defensive copy).
                    cleaned_metadata = raw.metadata
                    if isinstance(cleaned_metadata, dict):
                        cleaned_metadata = {
                            k: v
                            for k, v in cleaned_metadata.items()
                            if k not in _MSG_CLEAR_KEYS
                        }
                    # Tier-split rejection.
                    if tier == "quick" and role in _DEEP_REQUIRED_ROLES:
                        logger.warning(
                            "Rejecting committee turn: quick-tier model bound to "
                            "deep-required role %r",
                            role,
                        )
                        continue
                    # Bull/bear cap.
                    if role in ("bull_researcher", "bear_researcher"):
                        if bull_bear_count >= cap:
                            dropped += 1
                            continue
                        bull_bear_count += 1
                    # Reconstruct with cleaned metadata if changed.
                    if cleaned_metadata is not raw.metadata:
                        raw = CommitteeTurn(
                            role=raw.role,
                            stance=raw.stance,
                            direction=raw.direction,
                            confidence=raw.confidence,
                            rationale=raw.rationale,
                            model=raw.model,
                            input_hash=raw.input_hash,
                            metadata=cleaned_metadata,
                            tier=raw.tier,
                        )
                    turns.append(raw)
                elif isinstance(raw, dict):
                    raw = dict(raw)  # don't mutate caller's dict
                    role = raw.get("role")
                    # msg-clear on inbound metadata BEFORE construction.
                    if "metadata" in raw and isinstance(raw["metadata"], dict):
                        raw_metadata = dict(raw["metadata"])
                        for k in _MSG_CLEAR_KEYS:
                            raw_metadata.pop(k, None)
                        raw["metadata"] = raw_metadata
                    # Tier inference: honor explicit tier; otherwise infer
                    # from role.
                    if "tier" not in raw or raw.get("tier") is None:
                        raw["tier"] = _infer_tier_from_role(role) if role else "quick"
                    tier = raw.get("tier")
                    # Tier-split rejection.
                    if tier == "quick" and role in _DEEP_REQUIRED_ROLES:
                        logger.warning(
                            "Rejecting committee turn: quick-tier model bound to "
                            "deep-required role %r",
                            role,
                        )
                        continue
                    # Bull/bear cap.
                    if role in ("bull_researcher", "bear_researcher"):
                        if bull_bear_count >= cap:
                            dropped += 1
                            continue
                        bull_bear_count += 1
                    turns.append(CommitteeTurn(**raw))
            except Exception:  # noqa: BLE001 — defensive intake boundary
                continue
        # Truncate first, then record drops; truncation drops are also
        # routing-level and should be counted toward dropped_turns metadata.
        truncated = turns[:16]
        if len(turns) > 16:
            dropped += len(turns) - 16
        self._dropped_turns_last = dropped
        return truncated

    @staticmethod
    def _side_strength(views: list[AnalystView]) -> float:
        return float(sum(abs(v.magnitude) * v.confidence for v in views))

    @staticmethod
    def _disagreement_score(views: list[AnalystView]) -> float:
        directional = [v for v in views if v.direction != 0]
        if not directional:
            return 0.0
        long_weight = sum(v.confidence for v in directional if v.direction > 0)
        short_weight = sum(v.confidence for v in directional if v.direction < 0)
        total = long_weight + short_weight
        if total <= 0:
            return 0.0
        return float(1.0 - abs(long_weight - short_weight) / total)

    def _flat_from_baseline(
        self,
        baseline_signal: AggregatedSignal,
        views: list[AnalystView],
        turns: list[CommitteeTurn],
        disagreement: float,
        reason: str,
    ) -> AggregatedSignal:
        return AggregatedSignal(
            asset=baseline_signal.asset,
            timeframe=baseline_signal.timeframe,
            asset_class=baseline_signal.asset_class,
            asof=baseline_signal.asof,
            direction=0,
            magnitude=0.0,
            confidence=0.0,
            confidence_raw=0.0,
            horizon=baseline_signal.horizon,
            components=tuple(views),
            aggregator=self.name,
            metadata=self._metadata(views, turns, disagreement, reason),
        )

    def _metadata(
        self,
        views: list[AnalystView],
        turns: list[CommitteeTurn],
        disagreement: float,
        decision: str,
    ) -> dict[str, Any]:
        return {
            "committee": {
                "decision": decision,
                "n_effective_views": len(views),
                "disagreement_score": float(np.clip(disagreement, 0.0, 1.0)),
                "roles": [turn.role for turn in turns],
                "turns": [asdict(turn) for turn in turns],
                "model_backed_turns": [
                    asdict(turn) for turn in turns if not turn.model.startswith("deterministic:")
                ],
                "safety": {
                    "risk_gate_still_required": True,
                    "missing_models_degrade_to_deterministic": True,
                    "disagreement_reduces_confidence": True,
                    "tier_split_enforced": True,
                    "turn_cap_active": True,
                    "max_debate_rounds": self.max_debate_rounds,
                    "dropped_turns": self._dropped_turns_last,
                    "msg_clear_enforced": True,
                },
            }
        }
