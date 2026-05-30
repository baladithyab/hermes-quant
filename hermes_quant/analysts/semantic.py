"""Hermes semantic analyst.

Consumes precomputed semantic packets from MarketContext.extras and emits a
normal AnalystView. It never calls a model or web API inside analyze(); Hermes or
another upstream job must create packets ahead of time.

Wave 5 (ADR-0038 §W5) grounding hook
--------------------------------------
When ``with_grounding=True`` (default) and a ``ground_truth_block`` is
present in ``ctx.extras``, the analyst:
  1. Renders the ground-truth block into the rationale as a preamble
     (including the HARD RULE citation requirement).
  2. Passes the rendered preamble through to the metadata so downstream
     ClaimVerifier can validate citation coverage.

Sentiment analyst ordered-tool-call constraint (TauricResearch v0.2.5 fix)
---------------------------------------------------------------------------
For any analyst that fetches news/social data:
  ORDERING RULE: tool calls MUST complete BEFORE synthesis begins.
  Do NOT synthesize from empty memory. Call the tool, then read the
  response, then produce the view. Use ``current_clear()`` after synthesis
  to purge stale tool-call messages before the next analyst stage.

  See: hermes_quant.grounding.current_clear for the purge node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_quant.protocol import AnalystView, MarketContext
from hermes_quant.semantic import (
    SemanticPacket,
    parse_semantic_packet,
    validate_semantic_packet,
)

_STANCE_TO_DIRECTION = {"bullish": 1, "bearish": -1, "neutral": 0}


@dataclass
class HermesSemanticAnalyst:
    """Packet-driven semantic analyst for Hermes-authored market theses.

    Wave 5: set ``with_grounding=True`` (default for new callers) to inject a
    GroundTruthBlock from ``ctx.extras['ground_truth_block']`` into the view
    metadata and rationale preamble. Existing tests that don't provide a
    ground_truth_block are unaffected — the flag gracefully no-ops when the
    block is absent.
    """

    name: str = "hermes_semantic"
    timeframes: list[str] | None = None
    asset_classes: list[str] | None = None
    enabled: bool = True
    max_age_minutes: float = 24 * 60
    require_horizon_match: bool = False
    verify_hash: bool = True
    confidence_shrink: float = 0.20
    with_grounding: bool = True
    """Wave 5: inject GroundTruthBlock from ctx.extras when available.
    Set False to suppress grounding (backwards-compatible with existing tests)."""

    def __post_init__(self) -> None:
        if self.timeframes is None:
            self.timeframes = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
        if self.asset_classes is None:
            self.asset_classes = ["crypto", "equity", "etf", "fx"]
        self._n_views_emitted = 0
        self._last_view_at = None
        self._last_abstain_reason = None
        self._error_count = 0

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        if not self.enabled:
            return None
        if ctx.timeframe not in (self.timeframes or []):
            return self._abstain(ctx, "timeframe_out_of_scope")
        if ctx.asset_class not in (self.asset_classes or []):
            return self._abstain(ctx, "asset_class_out_of_scope")

        packet, reason = self._select_packet(ctx)
        if packet is None:
            return self._abstain(ctx, reason)

        direction = _STANCE_TO_DIRECTION[packet.stance]
        confidence_raw = float(packet.confidence)
        confidence = max(0.0, confidence_raw - self.confidence_shrink)

        # --- Wave 5: grounding hook ---
        # Inject ground-truth preamble + block metadata when with_grounding=True
        # and a GroundTruthBlock is present in ctx.extras.
        grounding_metadata: dict[str, Any] = {}
        rationale_text = packet.summary[:256]
        if self.with_grounding and ctx.extras:
            block = ctx.extras.get("ground_truth_block")
            if block is not None:
                try:
                    from hermes_quant.grounding import render_for_prompt
                    gt_preamble = render_for_prompt(block)
                    # Prepend the ground-truth section so the rationale is grounded.
                    # Truncate to 512 chars to stay within AnalystView.rationale budget.
                    combined = f"[GT CONTEXT]\n{gt_preamble}\n[ANALYST RATIONALE]\n{rationale_text}"
                    rationale_text = combined[:512]
                    grounding_metadata = {
                        "ground_truth_symbol": block.symbol,
                        "ground_truth_asof": block.asof,
                        "citation_ids": list(block.citation_ids),
                        "with_grounding": True,
                    }
                except Exception:  # noqa: BLE001 — grounding must not break existing flow
                    grounding_metadata = {"with_grounding": False, "grounding_error": True}

        metadata: dict[str, Any] = {
            "packet_hash": packet.packet_hash or packet.computed_hash,
            "packet_asof": packet.asof,
            "packet_model": packet.model,
            "sources": [src.__dict__ for src in packet.sources][:8],
            "semantic_stance": packet.stance,
        }
        metadata.update(grounding_metadata)

        # ADR-0063: regime-aware confidence multiplier (gated by env flag)
        try:
            from hermes_quant.regime.regime_aware_confidence import apply_regime_multiplier
            _regime = ctx.extras.get("regime") if hasattr(ctx, "extras") else None
            confidence = apply_regime_multiplier(float(confidence), _regime, "semantic")
        except Exception:  # noqa: BLE001
            pass

        view = AnalystView(
            analyst=self.name,
            direction=direction,  # type: ignore[arg-type]
            magnitude=float(packet.magnitude),
            confidence=float(confidence),
            confidence_raw=confidence_raw,
            horizon=packet.horizon,
            rationale=rationale_text,
            metadata=metadata,
        )
        self._n_views_emitted += 1
        self._last_view_at = ctx.asof
        self._last_abstain_reason = None
        return view

    def _select_packet(self, ctx: MarketContext) -> tuple[SemanticPacket | None, str]:
        raw_packets = ctx.extras.get("semantic_packets") if ctx.extras else None
        if raw_packets is None:
            raw_packet = ctx.extras.get("semantic_packet") if ctx.extras else None
            raw_packets = [raw_packet] if raw_packet is not None else []
        if isinstance(raw_packets, dict):
            raw_packets = [raw_packets]

        valid: list[SemanticPacket] = []
        last_reason = "no_semantic_packets"
        # ADR-0074/ADR-0068: validate packets against the DECISION time, not the
        # bar time. In a live run ctx.asof is the last daily-bar close (e.g.
        # yesterday), so a packet published today would be rejected as
        # future_packet even though the news IS available at decision time. When
        # the caller supplies ctx.extras["decision_asof"] (wall-clock decision
        # time), use it; otherwise fall back to ctx.asof (backtests, where bar
        # time IS the decision boundary and lookahead safety must hold).
        validate_asof = ctx.asof
        if ctx.extras:
            _dec = ctx.extras.get("decision_asof")
            if _dec is not None:
                try:
                    import pandas as _pd
                    validate_asof = _pd.Timestamp(_dec)
                except Exception:  # noqa: BLE001
                    validate_asof = ctx.asof
        for raw in raw_packets or []:
            try:
                packet = parse_semantic_packet(raw)
            except Exception as exc:  # noqa: BLE001
                self._error_count += 1
                last_reason = f"packet_parse_error:{exc}"
                continue
            ok, reason = validate_semantic_packet(
                packet,
                asset=ctx.asset,
                asof=validate_asof,
                horizon=ctx.timeframe if self.require_horizon_match else None,
                max_age_minutes=self.max_age_minutes,
                verify_hash=self.verify_hash,
            )
            if ok:
                valid.append(packet)
            else:
                last_reason = reason
        if not valid:
            return None, last_reason
        valid.sort(key=lambda packet: packet.asof)
        return valid[-1], "ok"

    def _abstain(self, ctx: MarketContext, reason: str) -> AnalystView:
        self._last_abstain_reason = reason
        return AnalystView(
            analyst=self.name,
            direction=0,
            magnitude=0.0,
            confidence=0.0,
            confidence_raw=0.0,
            horizon=ctx.timeframe,
            rationale=f"semantic abstain: {reason}",
            metadata={"abstain_reason": reason},
        )

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": self._last_view_at.isoformat()
            if self._last_view_at is not None
            else None,
            "error_count": self._error_count,
            "last_abstain_reason": self._last_abstain_reason,
            "calibrator_status": {
                "type": "cold_start_shrinkage",
                "confidence_shrink": self.confidence_shrink,
                "is_calibrated": False,
            },
        }
