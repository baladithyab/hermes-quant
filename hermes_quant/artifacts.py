"""Artifact stores for semantic packets and committee turns.

The stores are deliberately filesystem-first: JSON artifacts under
~/.hermes/quant/ are easy to inspect, copy into backtests, hash, and replay.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from hermes_quant.aggregators.deliberative import CommitteeTurn
from hermes_quant.semantic import (
    SemanticPacket,
    parse_semantic_packet,
    semantic_packet_hash,
    validate_semantic_packet,
)

QUANT_HOME = Path.home() / ".hermes" / "quant"
SEMANTIC_PACKET_DIR = QUANT_HOME / "semantic_packets"
COMMITTEE_TURN_DIR = QUANT_HOME / "committee_turns"

ArtifactKind = Literal["semantic_packet", "committee_turns"]


def safe_asset_path(asset: str) -> str:
    """Filesystem-safe but readable symbol key."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", asset).strip("_") or "asset"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def write_semantic_packet(
    payload: dict[str, Any] | SemanticPacket,
    *,
    root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Hash, validate shape, and write one semantic packet artifact."""
    packet = parse_semantic_packet(payload)
    if packet.packet_hash is None or packet.packet_hash != packet.computed_hash:
        packet = packet.with_hash()
    data = packet.to_dict()
    base = root or SEMANTIC_PACKET_DIR
    path = base / safe_asset_path(packet.asset) / f"{packet.packet_hash}.json"
    atomic_write_json(path, data)
    return path, data


def load_semantic_packet(path: str | Path) -> SemanticPacket:
    return parse_semantic_packet(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))


def validate_semantic_packet_file(
    path: str | Path,
    *,
    asset: str | None = None,
    asof: str | pd.Timestamp | None = None,
    horizon: str | None = None,
    max_age_minutes: float = 24 * 60,
) -> dict[str, Any]:
    packet = load_semantic_packet(path)
    asof_ts = pd.Timestamp(asof) if asof is not None else pd.Timestamp.now(tz="UTC")
    ok, reason = validate_semantic_packet(
        packet,
        asset=asset or packet.asset,
        asof=asof_ts,
        horizon=horizon,
        max_age_minutes=max_age_minutes,
    )
    return {
        "success": ok,
        "reason": reason,
        "path": str(Path(path).expanduser()),
        "packet_hash": packet.packet_hash,
        "computed_hash": packet.computed_hash,
        "asset": packet.asset,
        "asof": packet.asof,
    }


def list_semantic_packets(
    *,
    asset: str | None = None,
    root: Path | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    base = root or SEMANTIC_PACKET_DIR
    if not base.exists():
        return []
    search_root = base / safe_asset_path(asset) if asset else base
    paths = sorted(search_root.glob("**/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in paths[:limit]:
        try:
            packet = load_semantic_packet(path)
        except Exception:
            continue
        out.append({
            "path": str(path),
            "asset": packet.asset,
            "asof": packet.asof,
            "horizon": packet.horizon,
            "stance": packet.stance,
            "confidence": packet.confidence,
            "magnitude": packet.magnitude,
            "packet_hash": packet.packet_hash,
            "summary": packet.summary,
        })
    return out


def latest_semantic_packets_for_asset(
    asset: str,
    *,
    root: Path | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    return [load_semantic_packet(item["path"]).to_dict() for item in list_semantic_packets(asset=asset, root=root, limit=limit)]


def committee_turns_hash(turns: list[dict[str, Any]]) -> str:
    return semantic_packet_hash({"committee_turns": turns})


def write_committee_turns(
    turns: list[CommitteeTurn | dict[str, Any]],
    *,
    asset: str,
    asof: str | pd.Timestamp | None = None,
    root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    normalized = []
    for turn in turns:
        if isinstance(turn, CommitteeTurn):
            normalized.append(asdict(turn))
        else:
            normalized.append(dict(turn))
    artifact_hash = committee_turns_hash(normalized)
    asof_str = (pd.Timestamp(asof) if asof is not None else pd.Timestamp.now(tz="UTC")).isoformat()
    payload = {
        "schema_version": 1,
        "artifact_kind": "committee_turns",
        "asset": asset,
        "asof": asof_str,
        "turns_hash": artifact_hash,
        "turns": normalized,
    }
    base = root or COMMITTEE_TURN_DIR
    path = base / safe_asset_path(asset) / f"{artifact_hash}.json"
    atomic_write_json(path, payload)
    return path, payload


def load_committee_turns(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    turns = payload.get("turns") or []
    expected = payload.get("turns_hash")
    computed = committee_turns_hash(turns)
    if expected and expected != computed:
        raise ValueError(f"committee_turns hash mismatch: expected {expected}, computed {computed}")
    return payload


def list_committee_turn_artifacts(
    *,
    asset: str | None = None,
    root: Path | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    base = root or COMMITTEE_TURN_DIR
    if not base.exists():
        return []
    search_root = base / safe_asset_path(asset) if asset else base
    paths = sorted(search_root.glob("**/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in paths[:limit]:
        try:
            payload = load_committee_turns(path)
        except Exception:
            continue
        out.append({
            "path": str(path),
            "asset": payload.get("asset"),
            "asof": payload.get("asof"),
            "turns_hash": payload.get("turns_hash"),
            "n_turns": len(payload.get("turns") or []),
            "roles": [turn.get("role") for turn in payload.get("turns", [])],
        })
    return out
