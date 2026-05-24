"""Tests for hermes_quant.governance.audit_log (ADR-0031 D2)."""
from __future__ import annotations

import builtins
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log import (
    AppendOnlyViolation,
    AuditLogSchemaMismatch,
    GovernanceEvent,
)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


def _evt(kind: str, **payload) -> GovernanceEvent:
    return GovernanceEvent(
        kind=kind,  # type: ignore[arg-type]
        asof=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
        source="test",
        payload=payload,
    )


def test_audit_log_append_creates_file(audit_path: Path) -> None:
    audit_log.append(_evt("fill", broker="paper", realized_pnl=1.5))
    assert audit_path.exists()
    line = audit_path.read_text().strip()
    row = json.loads(line)
    assert row["kind"] == "fill"
    assert row["schema_version"] == 1


def test_audit_log_is_append_only_no_truncate(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrap builtin open(); reject any mode containing 'w', 'x', or '+'.
    'r' must remain allowed for reads.
    """
    real_open = builtins.open

    observed_modes: list[str] = []

    def guarded_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        observed_modes.append(mode)
        # For audit log path specifically, enforce append-only writes
        if str(file) == str(audit_path):
            forbidden = {"w", "x", "+"}
            if any(ch in mode for ch in forbidden):
                raise AppendOnlyViolation(
                    f"audit_log opened with disallowed mode {mode!r}"
                )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    audit_log.append(_evt("fill"))
    audit_log.append(_evt("gate_approval"))

    write_modes = [m for m in observed_modes if m != "r"]
    # Every write should be 'a' or contain 'a' but never 'w' or '+'.
    for m in write_modes:
        assert "w" not in m and "+" not in m and "x" not in m, m


def test_audit_log_event_schema_is_versioned(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write a row with the current version, then bump the reader's
    expected version; read must raise AuditLogSchemaMismatch.
    """
    audit_log.append(_evt("fill"))
    monkeypatch.setattr(audit_log, "CURRENT_SCHEMA_VERSION", 999)
    with pytest.raises(AuditLogSchemaMismatch):
        list(audit_log.read())


def test_audit_log_read_filters_by_kind(audit_path: Path) -> None:
    audit_log.append(_evt("fill", n=1))
    audit_log.append(_evt("gate_approval", n=2))
    audit_log.append(_evt("kill_switch_fired", n=3))

    fills = list(audit_log.read(kinds=["fill"]))
    assert len(fills) == 1
    assert fills[0].payload["n"] == 1


def test_audit_log_read_filters_by_since(audit_path: Path) -> None:
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(3):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=base + timedelta(days=i),
                source="t",
                payload={"i": i},
            )
        )
    cutoff = base + timedelta(days=1)
    rows = list(audit_log.read(since=cutoff))
    assert [r.payload["i"] for r in rows] == [1, 2]


def test_audit_log_fsync_after_write(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch os.fsync; assert it was called after append."""
    import os

    calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    audit_log.append(_evt("fill"))
    assert len(calls) >= 1


def test_audit_log_truncate_forbidden(audit_path: Path) -> None:
    with pytest.raises(AppendOnlyViolation):
        audit_log.truncate()


def test_audit_log_update_forbidden(audit_path: Path) -> None:
    with pytest.raises(AppendOnlyViolation):
        audit_log.update()


def test_audit_log_invalid_kind_rejected(audit_path: Path) -> None:
    evt = GovernanceEvent.model_construct(
        event_id="x",
        kind="not_a_real_kind",  # type: ignore[arg-type]
        schema_version=1,
        asof=datetime.now(timezone.utc),
        source="t",
        payload={},
    )
    with pytest.raises(ValueError):
        audit_log.append(evt)


def test_audit_log_round_trip(audit_path: Path) -> None:
    e = _evt("promotion_event", promoted=False, blocked_by=["x"])
    audit_log.append(e)
    rows = list(audit_log.read())
    assert len(rows) == 1
    assert rows[0].kind == "promotion_event"
    assert rows[0].payload == {"promoted": False, "blocked_by": ["x"]}
