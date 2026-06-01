"""hermes_quant.perception.adapter — pure ``frame_to_context`` projection (PDR-1).

The ONLY place that knows how a ``PerceptionFrame`` projects into today's
``MarketContext.extras`` shape. It reproduces ``advisor.py:849-881`` byte-for-byte:
the regime keys (ADR-0063), the semantic keys (ADR-0074), the still-forming keys
(ADR-0069), and ``decision_asof`` — all of which already live in ``frame.extras``
or as the ``frame.regime`` / ``frame.semantic_packets`` fields.

**Critical fidelity rule (recon §3.2):** this function is *pure*. It does NOT call
``build_regime_extras`` or ``semantic_market_extras`` — those run during frame
*construction* (``builder.py``). The adapter only re-shapes, so the
"regime merged OVER caller values" ordering and the "no double-classify" property
hold exactly as in the ``recommend`` ``None`` branch.
"""

from __future__ import annotations

from typing import Any

from hermes_quant.perception.frame import PerceptionFrame
from hermes_quant.protocol import MarketContext


def frame_to_context(
    frame: PerceptionFrame,
    *,
    timeframe: str,
    asset_class: str,
    exchange: str | None = None,
) -> MarketContext:
    """Project a ``PerceptionFrame`` into the existing ``MarketContext``.

    Pure: re-shapes the frame's already-built fields into ``ctx.extras``,
    reproducing the advisor's ``None``-branch extras key-set exactly. No
    classification, no packet loading — those happened during frame construction.
    """
    extras: dict[str, Any] = dict(frame.extras)  # decision_asof, still_forming_*, regime_failure/_kind
    # Regime keys EXACTLY as advisor writes them (ADR-0063); frame.regime IS the
    # RegimePacket object (or None). The adapter re-expands it to the 3 keys.
    extras["regime"] = frame.regime
    extras.setdefault("regime_failure", None)
    extras.setdefault(
        "regime_classifier_kind",
        "unavailable" if frame.regime is None else getattr(frame.regime, "classifier_kind", "rule_based"),
    )
    if frame.semantic_packets:  # only when non-empty (matches today's "None means absent")
        extras["semantic_packets"] = list(frame.semantic_packets)
    # The three FUTURE scores ride in extras under their own keys; analysts ignore
    # unknown keys (protocol.py:16). In PDR-1 these are always None so NOTHING is
    # written — preserving the default-path extras key-set exactly.
    if frame.trend_velocity is not None:
        extras["trend_velocity"] = frame.trend_velocity
    if frame.convergence is not None:
        extras["convergence"] = frame.convergence
    if frame.saturation is not None:
        extras["saturation"] = frame.saturation
    # ADR-0084: outcome-free, asof-honest scheduled-event risk. None when OFF, so
    # the default extras key-set is preserved (byte-identical flag-OFF). Analysts
    # ignore unknown keys (protocol.py:16); this is the bull/bear/judge READ surface.
    if frame.event_risk is not None:
        extras["event_risk"] = frame.event_risk
    return MarketContext(
        asset=frame.symbol,
        timeframe=timeframe,
        asset_class=asset_class,
        exchange=exchange,
        bars=frame.bars,
        last_close=frame.last_close,
        last_volume=float(frame.bars["volume"].iloc[-1]),
        asof=frame.asof,
        extras=extras,
    )


__all__ = ["frame_to_context"]
