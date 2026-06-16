"""Semantic packet primitives for Hermes-native perception.

Semantic packets are precomputed Hermes/model/human research artifacts. The
trading tick consumes them as immutable inputs; it never calls an LLM or the web
from inside the analyst hot path. This preserves replayability.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

SemanticStance = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True)
class SemanticSource:
    """One provenance pointer used by a semantic packet."""

    type: str
    ref: str
    title: str | None = None


@dataclass(frozen=True)
class SemanticPacket:
    """Hermes semantic-analysis artifact consumed by HermesSemanticAnalyst."""

    schema_version: int
    asset: str
    asof: str
    horizon: str
    stance: SemanticStance
    confidence: float
    magnitude: float
    summary: str
    sources: tuple[SemanticSource, ...] = ()
    model: str = "hermes:unknown"
    packet_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        data = asdict(self)
        normalized_sources = []
        for src in self.sources:
            if isinstance(src, SemanticSource):
                normalized_sources.append(asdict(src))
            elif isinstance(src, dict):
                normalized_sources.append(dict(src))
            else:
                normalized_sources.append({"type": "unknown", "ref": str(src)})
        data["sources"] = normalized_sources
        if not include_hash:
            data.pop("packet_hash", None)
        return data

    @property
    def computed_hash(self) -> str:
        return semantic_packet_hash(self.to_dict(include_hash=False))

    def with_hash(self) -> SemanticPacket:
        return SemanticPacket(
            **{**self.to_dict(include_hash=True), "packet_hash": self.computed_hash}
        )


def semantic_packet_hash(payload: dict[str, Any]) -> str:
    """Hash canonical JSON, excluding any existing packet_hash key."""
    clean = dict(payload)
    clean.pop("packet_hash", None)
    raw = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_semantic_packet(payload: SemanticPacket | dict[str, Any]) -> SemanticPacket:
    """Parse a packet object or dict into a SemanticPacket dataclass."""
    if isinstance(payload, SemanticPacket):
        return payload
    data = dict(payload)
    sources_raw = data.get("sources") or []
    sources = []
    for src in sources_raw:
        if isinstance(src, SemanticSource):
            sources.append(src)
        elif isinstance(src, dict):
            sources.append(SemanticSource(**src))
        else:
            raise ValueError(f"invalid semantic source: {src!r}")
    data["sources"] = tuple(sources)
    data.setdefault("schema_version", 1)
    data.setdefault("model", "hermes:unknown")
    data.setdefault("metadata", {})
    return SemanticPacket(**data)


def validate_semantic_packet(
    packet: SemanticPacket,
    *,
    asset: str,
    asof: pd.Timestamp,
    horizon: str | None = None,
    max_age_minutes: float = 24 * 60,
    verify_hash: bool = True,
) -> tuple[bool, str]:
    """Return (ok, reason) for a packet in a market context."""
    if packet.schema_version != 1:
        return False, "unsupported_schema_version"
    if packet.asset != asset:
        return False, "asset_mismatch"
    if packet.stance not in {"bullish", "bearish", "neutral"}:
        return False, "invalid_stance"
    if not (0.0 <= float(packet.confidence) <= 1.0):
        return False, "invalid_confidence"
    if not math.isfinite(float(packet.magnitude)):
        return False, "invalid_magnitude"
    if not packet.summary:
        return False, "missing_summary"
    if horizon is not None and packet.horizon != horizon:
        return False, "horizon_mismatch"

    try:
        packet_asof = pd.Timestamp(packet.asof)
    except Exception:
        return False, "invalid_asof"
    # ar33: pd.Timestamp("") / pd.Timestamp(None) returns NaT WITHOUT raising, so the
    # except above does not catch an empty/None asof. A NaT then defeats BOTH freshness
    # gates below — `NaT > ctx_asof` is False (skips future_packet) and
    # `(ctx_asof - NaT).total_seconds()` is NaN so `NaN > max_age` is False (skips
    # stale_packet) — admitting an UNKNOWABLE-age semantic signal (fail-open). An
    # un-timestamped packet must be rejected, not waved through.
    if pd.isna(packet_asof):
        return False, "invalid_asof"
    if packet_asof.tzinfo is None:
        packet_asof = packet_asof.tz_localize("UTC")
    else:
        packet_asof = packet_asof.tz_convert("UTC")
    ctx_asof = asof.tz_localize("UTC") if asof.tzinfo is None else asof.tz_convert("UTC")
    if packet_asof > ctx_asof:
        return False, "future_packet"
    age_minutes = (ctx_asof - packet_asof).total_seconds() / 60.0
    if age_minutes > max_age_minutes:
        return False, "stale_packet"

    if (
        verify_hash
        and packet.packet_hash is not None
        and packet.packet_hash != packet.computed_hash
    ):
        return False, "packet_hash_mismatch"
    return True, "ok"


def semantic_packet_from_dict(
    payload: dict[str, Any], *, attach_hash: bool = True
) -> SemanticPacket:
    """Convenience helper for tests/packet writers."""
    packet = parse_semantic_packet(payload)
    return packet.with_hash() if attach_hash else packet
