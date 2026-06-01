"""hermes_quant.memory.decisions — Layer 1: append-only decision log (ADR-0042).

Storage: ~/.hermes/quant/memory/decisions.jsonl

One row per committee decision event. Two event kinds:
  - kind="decision"   : the initial record at decision time (state=pending)
  - kind="resolution" : a separate row linking decision_id → reflection_id
                        (state=resolved). The original decision row is NEVER
                        mutated — this is the same append-only discipline as
                        audit_log.py (ADR-0031).

Append-only enforcement: truncate() and update() raise AppendOnlyViolation.
schema_version=1 on every row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
MEMORY_HOME = QUANT_HOME / "memory"
DECISIONS_PATH = MEMORY_HOME / "decisions.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppendOnlyViolation(Exception):
    """Raised on attempts to truncate/update an append-only log."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _to_utc_iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def _make_decision_id(asof: str | datetime, ticker: str) -> str:
    """Stable decision ID: dec_<UTC-compact>_<TICKER>_<6-hex>."""
    if isinstance(asof, datetime):
        ts = asof.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    else:
        ts = asof.replace(":", "").replace("-", "").replace("Z", "").replace("+00:00", "")[:15]
    h = hashlib.sha1(f"{ts}{ticker}".encode()).hexdigest()[:6]
    return f"dec_{ts}_{ticker.upper()}_{h}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DecisionLog:
    """Append-only wrapper around decisions.jsonl.

    Parameters
    ----------
    path:
        Override the default path (useful in tests via tmp_path fixtures).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DECISIONS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def record_decision(
        self,
        *,
        asof_decision: str | datetime,
        ticker: str,
        asset_class: str,
        rating: str,
        direction: int,
        confidence: float,
        target_position_pct: float,
        thesis_summary: str,
        thesis_evidence_ids: list[str] | None = None,
        signal_provenance: dict[str, Any] | None = None,
        research_plan_text: str | None = None,
        trader_proposal: dict[str, Any] | None = None,
        risk_debate_summary: str | None = None,
        state: str = "pending",
        decision_id: str | None = None,
    ) -> str:
        """Append a new decision record and return the decision_id.

        The row is written once and never mutated. To mark it resolved, call
        ``record_resolution()``.
        """
        asof_str = _to_utc_iso(asof_decision) or datetime.now(UTC).isoformat()
        dec_id = decision_id or _make_decision_id(asof_str, ticker)

        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "decision",
            "decision_id": dec_id,
            "asof_decision": asof_str,
            "tau_observable": None,
            "ticker": ticker.upper(),
            "asset_class": asset_class,
            "rating": rating,
            "direction": direction,
            "confidence": confidence,
            "target_position_pct": target_position_pct,
            "thesis_summary": thesis_summary,
            "thesis_evidence_ids": thesis_evidence_ids or [],
            "signal_provenance": signal_provenance or {},
            "research_plan_text": research_plan_text or "",
            "trader_proposal": trader_proposal,
            "risk_debate_summary": risk_debate_summary,
            "state": state,
            "resolution": None,
        }

        self._append(row)
        logger.info("decision-log: recorded %s ticker=%s state=%s", dec_id, ticker, state)
        return dec_id

    def record_resolution(self, decision_id: str, reflection_id: str) -> None:
        """Append a resolution event linking decision_id → reflection_id.

        Does NOT mutate the original decision row; the "current state" of a
        decision is materialized by replaying the event chain (ADR-0042).
        """
        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "resolution",
            "decision_id": decision_id,
            "reflection_id": reflection_id,
            "asof_resolution": datetime.now(UTC).isoformat(),
        }
        self._append(row)
        logger.info("decision-log: resolution %s → %s", decision_id, reflection_id)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def read_all(self) -> Iterator[dict[str, Any]]:
        """Stream every row in the log (decisions + resolutions)."""
        if not self._path.exists():
            return
        with open(self._path) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                yield obj

    def read_pending(self) -> Iterator[dict[str, Any]]:
        """Yield decision rows whose decision_id has no matching resolution."""
        decisions: dict[str, dict[str, Any]] = {}
        resolved_ids: set[str] = set()
        for row in self.read_all():
            if row.get("kind") == "decision":
                decisions[row["decision_id"]] = row
            elif row.get("kind") == "resolution":
                resolved_ids.add(row["decision_id"])
        for dec_id, row in decisions.items():
            if dec_id not in resolved_ids:
                yield row

    def read_resolved(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield (decision_row, resolution_row) pairs for resolved decisions."""
        decisions: dict[str, dict[str, Any]] = {}
        resolutions: dict[str, dict[str, Any]] = {}
        for row in self.read_all():
            if row.get("kind") == "decision":
                decisions[row["decision_id"]] = row
            elif row.get("kind") == "resolution":
                resolutions[row["decision_id"]] = row
        for dec_id, res_row in resolutions.items():
            if dec_id in decisions:
                yield decisions[dec_id], res_row

    # ------------------------------------------------------------------
    # Append-only enforcement
    # ------------------------------------------------------------------

    def truncate(self) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "decisions.jsonl is append-only; truncate() is not supported"
        )

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "decisions.jsonl is append-only; update() is not supported"
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _append(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        with _write_lock:
            with open(self._path, "a", buffering=1) as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
