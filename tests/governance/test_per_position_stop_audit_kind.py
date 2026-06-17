"""RED->GREEN test: per_position_stop_fired must be a registered EventKind.

Defect (ar28 pattern): `per_position_stop_fired` was missing from both the
`EventKind` Literal and the `VALID_KINDS` tuple in audit_log.py. Pydantic
raised a `ValidationError` on every `GovernanceEvent(kind='per_position_stop_fired', ...)`
call, and the `except Exception` in `_emit_per_position_stop_audit` silently
swallowed it — the stop rail fired with zero governance audit trace (invisible
to the downstream promotion kill-switch gate that reads kind=='kill_switch_fired'
family events).

Fix: add `"per_position_stop_fired"` to both `EventKind` Literal and `VALID_KINDS`.

RED proof: without the fix, `GovernanceEvent(kind='per_position_stop_fired', ...)`
raises `ValidationError: 1 validation error for GovernanceEvent kind Input should be
'proposal_emitted'...`. With the fix, the instantiation succeeds and the event
round-trips through audit_log.append / audit_log.read correctly.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log import GovernanceEvent, VALID_KINDS, EventKind


NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


# ---------------------------------------------------------------------------
# Core RED->GREEN: the kind must be in both the Literal and the tuple.
# ---------------------------------------------------------------------------

def test_per_position_stop_fired_in_valid_kinds():
    """VALID_KINDS must include 'per_position_stop_fired'.

    RED if missing: VALID_KINDS does not contain the kind, so audit_log.append()
    raises ValueError (kind gate), and the _emit_per_position_stop_audit
    best-effort wrapper silently swallows the error — zero audit trace on stop fire.
    """
    assert "per_position_stop_fired" in VALID_KINDS, (
        "'per_position_stop_fired' is missing from VALID_KINDS; "
        "audit_log.append() will raise ValueError and the stop-fire audit event "
        "will be silently discarded (ar28 pattern: money rail with zero audit trace)"
    )


def test_per_position_stop_fired_governance_event_instantiates():
    """GovernanceEvent(kind='per_position_stop_fired', ...) must NOT raise.

    RED (before fix): Pydantic raises ValidationError because the kind is absent
    from the EventKind Literal — the except-Exception handler in
    _emit_per_position_stop_audit silently swallows it.

    GREEN (after fix): instantiation succeeds.
    """
    evt = GovernanceEvent(
        kind="per_position_stop_fired",  # type: ignore[arg-type]
        asof=NOW,
        source="autonomous_per_position_stop",
        payload={
            "rail": "per_position_unrealized_loss_pct",
            "symbol": "ASTS",
            "unrealized_loss_pct": 0.2092,
            "threshold_pct": 0.08,
            "held_fraction": 0.20,
        },
    )
    assert evt.kind == "per_position_stop_fired"
    assert evt.schema_version == audit_log.CURRENT_SCHEMA_VERSION


def test_per_position_stop_fired_round_trips_through_append_read(audit_path: Path):
    """The event must survive a full append -> read round-trip without error.

    RED (before fix): audit_log.append raises ValueError (kind not in VALID_KINDS)
    before the event is written, so read() returns nothing.

    GREEN (after fix): the event is written and read back with all payload fields
    intact — the governance audit trace is durable.
    """
    evt = GovernanceEvent(
        kind="per_position_stop_fired",  # type: ignore[arg-type]
        asof=NOW,
        source="autonomous_per_position_stop",
        payload={
            "rail": "per_position_unrealized_loss_pct",
            "symbol": "ASTS",
            "unrealized_loss_pct": 0.2092,
            "threshold_pct": 0.08,
            "held_fraction": 0.20,
        },
    )
    audit_log.append(evt)

    rows = list(audit_log.read(kinds=["per_position_stop_fired"]))
    assert len(rows) == 1, (
        "Expected 1 per_position_stop_fired event in the audit log after append; "
        "got 0 — the event was silently dropped (ar28 pattern)"
    )
    assert rows[0].kind == "per_position_stop_fired"
    assert rows[0].payload["symbol"] == "ASTS"
    assert rows[0].payload["unrealized_loss_pct"] == pytest.approx(0.2092, abs=1e-6)
    assert rows[0].payload["threshold_pct"] == pytest.approx(0.08, abs=1e-9)


def test_per_position_stop_fired_not_skipped_by_read_unfiltered(audit_path: Path):
    """An unfiltered read() must yield per_position_stop_fired rows (not skip them as
    'extension kinds').

    This guards against a regression where the kind is added to VALID_KINDS but NOT to
    EventKind Literal — the read-side extension-kind guard (ar56) would skip it on
    reconstruction, making the event invisible to consumers that do kinds=None.
    """
    audit_log.append(
        GovernanceEvent(
            kind="fill",  # type: ignore[arg-type]
            asof=NOW,
            source="paper_reactor",
            payload={"broker": "paper"},
        )
    )
    audit_log.append(
        GovernanceEvent(
            kind="per_position_stop_fired",  # type: ignore[arg-type]
            asof=NOW,
            source="autonomous_per_position_stop",
            payload={"symbol": "ASTS"},
        )
    )

    all_rows = list(audit_log.read())  # kinds=None — must NOT skip the stop event
    kinds_seen = {r.kind for r in all_rows}
    assert "per_position_stop_fired" in kinds_seen, (
        "per_position_stop_fired event was skipped by an unfiltered read() — "
        "the kind is likely in VALID_KINDS but not in the EventKind Literal, "
        "causing the ar56 extension-kind guard to discard it"
    )
    assert "fill" in kinds_seen
