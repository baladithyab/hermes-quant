"""Artifact-driven committee runner.

This module writes replayable CommitteeTurn artifacts. The default runner is a
local deterministic synthesis so the CLI works for every installer. External
Hermes/model mixtures can generate richer turns upstream and still use the same
artifact schema.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hermes_quant.aggregators.deliberative import CommitteeTurn
from hermes_quant.artifacts import write_committee_turns
from hermes_quant.protocol import Direction
from hermes_quant.semantic import parse_semantic_packet


def build_committee_turns_from_packets(
    packets: list[dict[str, Any]],
    *,
    asset: str,
    model: str = "deterministic:semantic_packets",
) -> list[CommitteeTurn]:
    """Create a model-vote-like debate trace from semantic packet artifacts."""
    parsed = [parse_semantic_packet(p) for p in packets]
    bull = [p for p in parsed if p.stance == "bullish"]
    bear = [p for p in parsed if p.stance == "bearish"]
    neutral = [p for p in parsed if p.stance == "neutral"]

    def strength(items) -> float:
        return float(sum(float(p.confidence) * abs(float(p.magnitude)) for p in items))

    bull_strength = strength(bull)
    bear_strength = strength(bear)
    neutral_strength = strength(neutral)
    if bull_strength > bear_strength and bull_strength > neutral_strength:
        final_direction: Direction = 1
        final_stance = "bullish"
        final_conf = min(1.0, sum(p.confidence for p in bull) / max(len(bull), 1))
    elif bear_strength > bull_strength and bear_strength > neutral_strength:
        final_direction = -1
        final_stance = "bearish"
        final_conf = min(1.0, sum(p.confidence for p in bear) / max(len(bear), 1))
    else:
        final_direction = 0
        final_stance = "neutral"
        final_conf = min(1.0, 0.5 + neutral_strength)

    packet_hashes = [p.packet_hash or p.computed_hash for p in parsed]
    return [
        CommitteeTurn(
            role="bull_researcher",
            stance="bull_case",
            direction=1,
            confidence=min(1.0, bull_strength),
            rationale=f"{len(bull)} bullish semantic packets; strength={bull_strength:.4f}",
            model=model,
            metadata={"packet_hashes": packet_hashes},
        ),
        CommitteeTurn(
            role="bear_researcher",
            stance="bear_case",
            direction=-1,
            confidence=min(1.0, bear_strength),
            rationale=f"{len(bear)} bearish semantic packets; strength={bear_strength:.4f}",
            model=model,
            metadata={"packet_hashes": packet_hashes},
        ),
        CommitteeTurn(
            role="risk_conservative",
            stance="semantic_risk_check",
            direction=0 if abs(bull_strength - bear_strength) < 1e-9 else final_direction,
            confidence=min(1.0, 0.25 + abs(bull_strength - bear_strength)),
            rationale="Conservative semantic risk prefers flat when semantic evidence is balanced",
            model=model,
            metadata={"packet_hashes": packet_hashes},
        ),
        CommitteeTurn(
            role="portfolio_manager",
            stance=final_stance,
            direction=final_direction,
            confidence=float(final_conf),
            rationale=(
                f"Portfolio-manager synthesis from semantic packet archive for {asset}: "
                f"bull={bull_strength:.4f}, bear={bear_strength:.4f}, neutral={neutral_strength:.4f}"
            ),
            model=model,
            metadata={"packet_hashes": packet_hashes},
        ),
    ]


def build_model_mixture_prompt(
    packets: list[dict[str, Any]],
    *,
    asset: str,
    models: list[str],
) -> str:
    """Self-contained prompt for a Hermes multi-model committee job.

    The prompt asks the agent to produce replayable CommitteeTurn JSON only;
    it does not grant any trading authority.
    """
    packet_hashes = [parse_semantic_packet(p).packet_hash or parse_semantic_packet(p).computed_hash for p in packets]
    models_text = ", ".join(models) if models else "current Hermes model"
    return (
        "You are running a hermes-quant model-mixture committee job. Do NOT trade. "
        "Read the semantic packet evidence and produce committee_turns artifacts only.\n\n"
        f"Asset: {asset}\n"
        f"Models/roles to use or simulate: {models_text}\n"
        f"Semantic packet hashes: {packet_hashes}\n\n"
        "Required roles: bull_researcher, bear_researcher, risk_conservative, portfolio_manager. "
        "Each turn must include role, stance, direction (-1/0/1), confidence [0,1], rationale, "
        "model, input_hash, and metadata.packet_hashes. If using multiple real models, record the "
        "exact provider/model in the model field and hash each model input into input_hash.\n\n"
        "After deliberation, write the artifact with:\n"
        f"hermes quant committee run --asset {asset!r} "
        + " ".join(f"--semantic-packet-file <packet-{h}.json>" for h in packet_hashes)
        + " --model '<provider/model-or-mixture-id>'\n\n"
        "Return only the artifact path/hash and a short safety summary."
    )


def run_committee_from_packets(
    packets: list[dict[str, Any]],
    *,
    asset: str,
    asof: str | pd.Timestamp | None = None,
    model: str = "deterministic:semantic_packets",
    root=None,
):
    turns = build_committee_turns_from_packets(packets, asset=asset, model=model)
    return write_committee_turns(turns, asset=asset, asof=asof, root=root)
