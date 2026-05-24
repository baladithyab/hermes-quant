"""hermes_quant.governance.audit_log — append-only governance event log
(ADR-0031 D2).

Storage: `~/.hermes/quant/governance/audit_log.jsonl`. Opened in append-only
mode; never opened with 'w', 'r+', 'x', or any '+' mode. Each row carries
`schema_version`. Reads with mismatched version raise `AuditLogSchemaMismatch`.

The seven event kinds are exactly those listed in ADR-0031 D1; new kinds
require a schema_version bump and human-edited ADR amendment.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
GOVERNANCE_HOME = QUANT_HOME / "governance"
AUDIT_LOG_PATH = GOVERNANCE_HOME / "audit_log.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

EventKind = Literal[
    "proposal_emitted",
    "gate_approval",
    "gate_rejection",
    "fill",
    "kill_switch_fired",
    "promotion_event",
    "retro_amendment_applied",
]

VALID_KINDS: tuple[str, ...] = (
    "proposal_emitted",
    "gate_approval",
    "gate_rejection",
    "fill",
    "kill_switch_fired",
    "promotion_event",
    "retro_amendment_applied",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuditLogSchemaMismatch(Exception):
    """Raised when reading a row with schema_version != CURRENT_SCHEMA_VERSION."""


class AppendOnlyViolation(Exception):
    """Raised on attempts to truncate/update the audit log."""


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class GovernanceEvent(BaseModel):
    """A single row on the governance audit log.

    Fields are intentionally minimal; richer detail belongs in `payload`.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: EventKind
    schema_version: int = CURRENT_SCHEMA_VERSION
    asof: datetime
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line (UTC ISO timestamps)."""
        d = self.model_dump()
        # Ensure datetimes are UTC ISO strings
        if isinstance(d["asof"], datetime):
            d["asof"] = _to_utc_iso(d["asof"])
        return json.dumps(d, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def _audit_path() -> Path:
    """Resolve the audit-log path each call so tests that monkey-patch
    HOME or AUDIT_LOG_PATH at runtime are respected."""
    return AUDIT_LOG_PATH


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append(event: GovernanceEvent) -> None:
    """Append one event to the audit log. fsync after write.

    Opens the file in append-only mode ('a'). Any other mode is a bug and
    is the subject of `test_audit_log_is_append_only_no_truncate`.
    """
    if event.kind not in VALID_KINDS:
        raise ValueError(f"invalid event kind: {event.kind!r}")

    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    line = event.to_jsonl()

    with _write_lock:
        with open(path, "a", buffering=1) as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def read(
    since: datetime | None = None,
    kinds: list[str] | None = None,
) -> Iterator[GovernanceEvent]:
    """Stream events from the audit log.

    Filters: `since` (asof >= since) and `kinds` (whitelist).
    Raises `AuditLogSchemaMismatch` if any row has a schema_version that
    does not equal `CURRENT_SCHEMA_VERSION`.
    """
    path = _audit_path()
    if not path.exists():
        return

    since_utc: datetime | None = None
    if since is not None:
        since_utc = since if since.tzinfo else since.replace(tzinfo=UTC)

    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            row_version = row.get("schema_version")
            if row_version != CURRENT_SCHEMA_VERSION:
                raise AuditLogSchemaMismatch(
                    f"row schema_version={row_version!r} "
                    f"but reader is at {CURRENT_SCHEMA_VERSION}"
                )

            row_kind = row.get("kind")
            if kinds is not None and row_kind not in kinds:
                continue

            asof_raw = row.get("asof")
            row_asof: datetime
            if isinstance(asof_raw, str):
                row_asof = datetime.fromisoformat(asof_raw)
                if row_asof.tzinfo is None:
                    row_asof = row_asof.replace(tzinfo=UTC)
            elif isinstance(asof_raw, datetime):
                row_asof = asof_raw
            else:
                row_asof = datetime.now(UTC)

            if since_utc is not None and row_asof < since_utc:
                continue

            yield GovernanceEvent(
                event_id=row["event_id"],
                kind=row_kind,
                schema_version=row_version,
                asof=row_asof,
                source=row.get("source", ""),
                payload=row.get("payload", {}),
            )


def truncate() -> None:
    """Forbidden — append-only log."""
    raise AppendOnlyViolation("audit_log is append-only; truncate() is not supported")


def update(*args: Any, **kwargs: Any) -> None:
    """Forbidden — append-only log."""
    raise AppendOnlyViolation("audit_log is append-only; update() is not supported")
