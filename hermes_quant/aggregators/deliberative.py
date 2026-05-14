"""Deliberative committee aggregator.

This is a deterministic scaffold for TradingAgents-style collaboration:
research debate -> trader synthesis -> risk debate -> portfolio manager. Future
Hermes/model-generated committee turns can be injected through MarketContext
extras, but aggregate() itself stays local and replayable.
"""
from __future__ import annotations

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


class DeliberativeCommitteeAggregator:
    """Aggregator that records structured disagreement before final signal.

    The first version is intentionally deterministic. It delegates statistical
    combination to BMA, then adjusts confidence downward when the committee sees
    high disagreement or insufficient voices. This makes deliberation a safety
    layer, not a leverage amplifier.
    """

    name = "deliberative_committee"

    def __init__(
        self,
        *,
        baseline: BMAAggregator | None = None,
        min_effective_views: int = 2,
        disagreement_penalty: float = 0.25,
        semantic_bonus_cap: float = 0.05,
    ) -> None:
        self.baseline = baseline or BMAAggregator()
        self.min_effective_views = min_effective_views
        self.disagreement_penalty = disagreement_penalty
        self.semantic_bonus_cap = semantic_bonus_cap

    def aggregate(self, views: list[AnalystView], context: MarketContext) -> AggregatedSignal:
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
        model_turns = self._model_turns_from_context(context)
        turns = [
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
        return [*turns, *model_turns]

    def _model_turns_from_context(self, context: MarketContext) -> list[CommitteeTurn]:
        raw_turns = context.extras.get("committee_turns") if context.extras else None
        turns: list[CommitteeTurn] = []
        for raw in raw_turns or []:
            try:
                if isinstance(raw, CommitteeTurn):
                    turns.append(raw)
                elif isinstance(raw, dict):
                    turns.append(CommitteeTurn(**raw))
            except Exception:
                continue
        return turns[:16]

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
                },
            }
        }
