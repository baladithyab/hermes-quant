"""hermes_quant.journal.reader — Markdown journal parser + retrieval helper.

Per ADR-0010 §Decision §7: `get_recent_lessons(symbol, n_same, n_cross)`
returns the recency tail for consumers (advisor, future LLMAnalyst).

The parser reads the META_BEGIN/META_END block of each entry — that's
the source of truth for round-tripping. Narrative prose is preserved
in the rendered file but not re-extracted (lossy is fine: round-trip
is anchored to the meta block).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AnalystComponent, Reflection, SettlementEntry

logger = logging.getLogger(__name__)


_ENTRY_DELIM = "<!-- ENTRY_END -->"
_META_BEGIN = "<!-- META_BEGIN -->"
_META_END = "<!-- META_END -->"


def parse_journal(content: str) -> list[SettlementEntry]:
    """Parse a journal file content into SettlementEntry objects.

    Tolerant of:
    - Empty file (returns [])
    - Header-only file (returns [])
    - Trailing whitespace
    - Missing optional fields

    Per ADR-0010: any entry whose META_BEGIN/META_END block is unparseable
    is silently skipped. Hand-edits to the meta block produce silently
    dropped entries; per §Decision §8 this is a feature.
    """
    if not content or not content.strip():
        return []

    entries: list[SettlementEntry] = []
    blocks = content.split(_ENTRY_DELIM)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        meta = _extract_meta(block)
        if not meta:
            continue
        try:
            entries.append(_meta_to_entry(meta))
        except Exception as exc:  # noqa: BLE001
            logger.debug("journal.reader: skipping unparseable entry: %s", exc)
            continue
    return entries


def get_recent_lessons(
    symbol: str,
    *,
    n_same: int = 5,
    n_cross: int = 3,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the recency tail of journal entries (ADR-0010 §7).

    Adapted from TradingAgents pattern #10 — flat tail, no embeddings.

    Returns list of dicts (not SettlementEntry) so consumers don't need to
    import the Pydantic models. Each dict has stable keys:
      - when: ISO timestamp (asof_settlement if resolved else asof_decision)
      - symbol
      - is_same: True if same symbol as query, False if cross-asset
      - direction
      - confidence
      - resolved: bool
      - raw_return / alpha_return: only populated when resolved
      - hitl_kind: "approve"|"reject"|"expire" if HITL-driven, else None
      - reflection: dict | None
      - reason: human-readable

    Sorted newest-first.
    """
    from .writer import DEFAULT_JOURNAL_PATH  # local import to avoid cycle
    target = path or DEFAULT_JOURNAL_PATH
    target = Path(target)
    if not target.exists():
        return []

    try:
        all_entries = parse_journal(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_recent_lessons: parse failed: %s", exc)
        return []

    # Sort newest-first by best-available timestamp
    def _sort_key(e: SettlementEntry) -> str:
        ts = e.asof_settlement or e.asof_decision
        return _iso(ts)

    same_symbol = sorted(
        [e for e in all_entries if e.symbol == symbol],
        key=_sort_key, reverse=True,
    )[:n_same]
    cross_symbol = sorted(
        [e for e in all_entries if e.symbol != symbol],
        key=_sort_key, reverse=True,
    )[:n_cross]

    out: list[dict[str, Any]] = []
    for e in same_symbol:
        out.append(_entry_to_lesson(e, is_same=True))
    for e in cross_symbol:
        out.append(_entry_to_lesson(e, is_same=False))
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_meta(block: str) -> dict[str, str] | None:
    """Extract the META_BEGIN..META_END block as a dict."""
    if _META_BEGIN not in block or _META_END not in block:
        return None
    start = block.index(_META_BEGIN) + len(_META_BEGIN)
    end = block.index(_META_END)
    meta_text = block[start:end].strip()
    fields: dict[str, str] = {}
    for line in meta_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields if fields else None


def _meta_to_entry(meta: dict[str, str]) -> SettlementEntry:
    """Convert a parsed meta dict to a SettlementEntry. Raises on bad data."""
    asof_decision = _parse_iso(meta["asof_decision"])
    direction = int(meta["direction"])

    asof_settlement = (
        _parse_iso(meta["asof_settlement"]) if "asof_settlement" in meta else None
    )

    reflection: Reflection | None = None
    if "reflection_thesis_held" in meta:
        reflection = Reflection(
            thesis_held=meta["reflection_thesis_held"].lower() == "true",
            magnitude_error=float(meta.get("reflection_magnitude_error", 0.0)),
            rule_version=meta.get("reflection_rule_version", "deterministic-v1"),
        )

    return SettlementEntry(
        entry_id=meta["entry_id"],
        asof_decision=asof_decision,
        symbol=_extract_symbol_from_meta(meta),
        asset_class=meta.get("asset_class", "equity"),
        direction=direction,
        confidence=float(meta.get("confidence", 0.0)),
        target_position_pct=float(meta.get("target_position_pct", 0.0)),
        decision_price=float(meta.get("decision_price", 0.0)),
        benchmark_symbol=meta.get("benchmark_symbol", "SPY"),
        per_analyst_components=[],   # not round-tripped from prose body
        reason=meta.get("reason", ""),
        asof_settlement=asof_settlement,
        exit_price=_safe_float(meta.get("exit_price")),
        raw_return=_safe_float(meta.get("raw_return")),
        alpha_return=_safe_float(meta.get("alpha_return")),
        hold_minutes=_safe_int(meta.get("hold_minutes")),
        reflection=reflection,
        hitl_kind=meta.get("hitl_kind"),
        hitl_reason=meta.get("hitl_reason"),
        hitl_approver=meta.get("hitl_approver"),
    )


def _extract_symbol_from_meta(meta: dict[str, str]) -> str:
    """Symbol isn't in the meta block (it's on the heading line). For now we
    keep it absent and reconstruct from entry_id which embeds it
    (`prop_<iso>_<symbol>_<rand>`). Fallback to 'UNKNOWN'."""
    eid = meta.get("entry_id", "")
    parts = eid.split("_")
    # Format: prop_<iso>_<symbol>_<rand6>
    if len(parts) >= 4 and parts[0] == "prop":
        return parts[-2]
    return "UNKNOWN"


def _entry_to_lesson(e: SettlementEntry, *, is_same: bool) -> dict[str, Any]:
    when = e.asof_settlement or e.asof_decision
    return {
        "when": _iso(when),
        "symbol": e.symbol,
        "is_same": is_same,
        "direction": int(e.direction),
        "confidence": float(e.confidence),
        "resolved": e.is_resolved(),
        "raw_return": e.raw_return,
        "alpha_return": e.alpha_return,
        "hitl_kind": e.hitl_kind,
        "reflection": (
            {
                "thesis_held": e.reflection.thesis_held,
                "magnitude_error": e.reflection.magnitude_error,
            }
            if e.reflection else None
        ),
        "reason": e.reason,
    }


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt.tzinfo is None else dt.isoformat()
    return str(dt)


def _parse_iso(s: str) -> datetime:
    s = s.strip().rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(s: object) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_int(s: object) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
