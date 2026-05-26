"""Tests for hermes_quant.evidence.lookahead_gate.

Uses an in-memory _MockStore so these tests are independent of the real
EvidenceStore implementation (sibling Wave-B task). The gate only requires
a `.get(evidence_id) -> dict | None` shape, which is the contract these
tests pin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from hermes_quant.evidence.lookahead_gate import (
    EvidenceLookaheadError,
    LookaheadCheckResult,
    assert_no_lookahead,
    check_view_lookahead,
    check_views_lookahead,
)
from hermes_quant.protocol import AnalystView

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _MockStore:
    """In-memory mock to decouple tests from the real EvidenceStore. Only
    the `.get(evidence_id) -> dict | None` shape is required by the gate."""

    rows: dict[str, dict] = field(default_factory=dict)

    def get(self, evidence_id) -> dict | None:
        return self.rows.get(str(evidence_id))


def _view(evidence_ids: tuple = (), analyst: str = "ta_classical") -> AnalystView:
    return AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.7,
        horizon="1d",
        evidence_ids=tuple(str(x) for x in evidence_ids),
    )


_T = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_view_with_no_evidence_ids_passes():
    """An AnalystView with empty evidence_ids tuple is trivially OK."""
    store = _MockStore()
    result = check_view_lookahead(_view(), _T, store)
    assert isinstance(result, LookaheadCheckResult)
    assert result.ok is True
    assert result.n_evidence_checked == 0
    assert result.violations == ()
    assert result.n_evidence_with_unknown_id == 0


def test_check_view_passes_when_all_available_at_le_asof():
    """All cited evidence available at-or-before asof → no violation."""
    store = _MockStore(
        rows={
            "ev1": {"available_at": _T - timedelta(hours=1)},
            "ev2": {"available_at": _T},  # exactly at asof is OK (not > asof)
        }
    )
    result = check_view_lookahead(_view(("ev1", "ev2")), _T, store)
    assert result.ok is True
    assert result.n_evidence_checked == 2
    assert result.violations == ()
    assert result.n_evidence_with_unknown_id == 0


def test_check_view_fails_when_evidence_available_at_gt_asof():
    """Cited evidence with available_at strictly after asof → violation."""
    future = _T + timedelta(minutes=5)
    store = _MockStore(rows={"ev_future": {"available_at": future}})
    result = check_view_lookahead(_view(("ev_future",)), _T, store)
    assert result.ok is False
    assert result.n_evidence_checked == 1
    assert len(result.violations) == 1
    err = result.violations[0]
    assert isinstance(err, EvidenceLookaheadError)
    assert err.evidence_id == "ev_future"
    assert err.asof == _T
    assert err.available_at == future


def test_check_view_treats_unknown_evidence_id_as_warning_not_error():
    """Unknown evidence_id (store.get returns None) → counted but not a
    lookahead violation."""
    store = _MockStore(rows={"known": {"available_at": _T - timedelta(hours=2)}})
    result = check_view_lookahead(_view(("known", "missing1", "missing2")), _T, store)
    assert result.ok is True  # no actual lookahead violations
    assert result.n_evidence_checked == 3
    assert result.n_evidence_with_unknown_id == 2
    assert result.violations == ()


def test_check_views_aggregates_across_multiple_views():
    """check_views_lookahead aggregates counts and violations across views."""
    future = _T + timedelta(seconds=1)
    store = _MockStore(
        rows={
            "ok1": {"available_at": _T - timedelta(hours=1)},
            "bad1": {"available_at": future},
            "ok2": {"available_at": _T},
            "bad2": {"available_at": future},
        }
    )
    views = [
        _view(("ok1", "bad1"), analyst="a1"),
        _view(("ok2", "bad2", "missing"), analyst="a2"),
    ]
    result = check_views_lookahead(views, _T, store)
    assert result.ok is False
    assert result.n_evidence_checked == 5
    assert result.n_evidence_with_unknown_id == 1
    assert len(result.violations) == 2
    analysts = {v.view_analyst for v in result.violations}
    assert analysts == {"a1", "a2"}


def test_assert_no_lookahead_raises_on_first_violation():
    """Strict-mode helper raises EvidenceLookaheadError on first violation."""
    future = _T + timedelta(minutes=10)
    store = _MockStore(rows={"bad": {"available_at": future}})
    with pytest.raises(EvidenceLookaheadError) as exc_info:
        assert_no_lookahead(_view(("bad",)), _T, store)
    assert exc_info.value.evidence_id == "bad"
    assert exc_info.value.asof == _T
    assert exc_info.value.available_at == future


def test_evidence_lookahead_error_carries_diagnostic_fields():
    """Exception object exposes evidence_id, asof, available_at, view_analyst."""
    future = _T + timedelta(hours=1)
    err = EvidenceLookaheadError(
        evidence_id="ev-xyz",
        asof=_T,
        available_at=future,
        view_analyst="microstructure",
    )
    assert err.evidence_id == "ev-xyz"
    assert err.asof == _T
    assert err.available_at == future
    assert err.view_analyst == "microstructure"
    msg = str(err)
    assert "ev-xyz" in msg
    assert "microstructure" in msg
    assert future.isoformat() in msg
    assert _T.isoformat() in msg


def test_iso_string_available_at_parsed_correctly():
    """Mock returning ISO-8601 string is parsed to datetime by the gate."""
    future_iso = (_T + timedelta(minutes=30)).isoformat()
    past_iso = (_T - timedelta(minutes=30)).isoformat()
    store = _MockStore(
        rows={
            "past": {"available_at": past_iso},
            "future": {"available_at": future_iso},
        }
    )
    # past-only view → ok
    ok_result = check_view_lookahead(_view(("past",)), _T, store)
    assert ok_result.ok is True
    # future view → violation, with parsed datetime in the error
    bad_result = check_view_lookahead(_view(("future",)), _T, store)
    assert bad_result.ok is False
    assert len(bad_result.violations) == 1
    parsed = bad_result.violations[0].available_at
    assert isinstance(parsed, datetime)
    assert parsed == datetime.fromisoformat(future_iso)
