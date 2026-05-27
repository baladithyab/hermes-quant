"""Tests for cross-model review fixes (C1-C4 + M5).

Adds regression-resistant tests for:
- C1: is_n1_collapse structural-only predicate (the spoof-resistant sibling)
- C2: idempotency guard on apply_execution; future-bound asof rejection
- C3: state_reconstruction_failed audit event emitted on apply failure
- C4: state.db file mode 0o600
- Flat-position fix: closed positions not written to positions table
- M5: data_quality=0.0 is preserved (not replaced by `or` fallback)
"""

from __future__ import annotations

import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log_query import is_bma_degenerate, is_n1_collapse
from hermes_quant.protocol import AggregatedSignal, AnalystView
from hermes_quant.risk.gate import _build_signal_provenance
from hermes_quant.state.portfolio_state import PortfolioState


# ---------------------------------------------------------------------------
# C1: is_n1_collapse — structural-only n=1 predicate
# ---------------------------------------------------------------------------


def _approval_event(
    *,
    aggregator: str = "BMAAggregator",
    n_distinct: int = 1,
    confidence: float = 1.0,
) -> dict:
    return {
        "kind": "gate_approval",
        "asof": "2026-05-26T04:00:00+00:00",
        "source": "risk.gate",
        "payload": {
            "asset": "MRNA",
            "direction": -1,
            "confidence": confidence,
            "asof": "2026-05-26T04:00:00+00:00",
            "signal_provenance": {
                "n_views": 1,
                "n_distinct_analysts": n_distinct,
                "contributing_analysts": ["Kronos"] if n_distinct == 1 else ["Kronos", "ClassicalTA"],
                "aggregator_class": aggregator,
                "analyst_view_ids": ["Kronos:1d"],
                "data_quality": None,
            },
        },
    }


def test_is_n1_collapse_flags_structural_signature_regardless_of_aggregator_name() -> None:
    """C1 fix: is_n1_collapse fires on structural condition only.

    The 2026-05-26 incident wasn't about the *string* "bma"; it was about
    n=1 distinct analyst + saturated confidence. is_n1_collapse must
    catch the same signature even when the aggregator is renamed (e.g.
    a future BMAAggregator2 / WeightedBMA / "bma_v2") that
    is_bma_degenerate would silently miss.
    """
    # is_bma_degenerate misses these (aggregator string mismatch):
    for cosmetic_name in ("BMAAggregator2", "WeightedBMA", "bma_v2", "FutureBMA"):
        ev = _approval_event(aggregator=cosmetic_name, n_distinct=1, confidence=1.0)
        assert is_bma_degenerate(ev) is False, f"old predicate should miss {cosmetic_name}"
        assert is_n1_collapse(ev) is True, f"structural predicate must catch {cosmetic_name}"


def test_is_n1_collapse_does_not_flag_two_distinct_analysts_at_unanimous_confidence() -> None:
    """Legitimate 2-analyst-agreement at conf=1.0 is NOT a collapse."""
    ev = _approval_event(n_distinct=2, confidence=1.0)
    assert is_n1_collapse(ev) is False
    assert is_bma_degenerate(ev) is False  # both predicates agree on this case


def test_is_n1_collapse_returns_false_on_pre_provenance_event() -> None:
    ev = _approval_event()
    ev["payload"].pop("signal_provenance")
    assert is_n1_collapse(ev) is False


# ---------------------------------------------------------------------------
# C2: idempotency guard + future-asof bound
# ---------------------------------------------------------------------------


def _make_record(
    *,
    proposal_id: str = "prop_test_001",
    asof: str = "2026-05-27T16:50:00Z",
    asset: str = "MRNA",
    fill_size_pct: float = 0.05,
    fill_price: float = 100.0,
    asset_class: str = "equity",
) -> dict:
    return {
        "proposal_id": proposal_id,
        "asof_execution": asof,
        "asset": asset,
        "asset_class": asset_class,
        "fill_size_pct": fill_size_pct,
        "fill_price": fill_price,
        "account_id": "paper-default",
    }


def test_apply_execution_is_idempotent_on_proposal_id_asof_pair(tmp_path: Path) -> None:
    """C2: applying the same record twice must NOT double-count cash or position."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    rec = _make_record(asset="AAPL", fill_size_pct=0.05, fill_price=200.0)
    ps.apply_execution(rec)
    ps.apply_execution(rec)  # second apply should hit idempotency guard
    positions = ps.get_positions("paper-default")
    assert len(positions) == 1
    pos = positions[("equity", "AAPL")]
    # Quantity should be 0.05, NOT 0.10
    assert pos.quantity == pytest.approx(0.05, abs=1e-9)


def test_apply_execution_rejects_future_dated_asof(tmp_path: Path) -> None:
    """C2: an asof more than 24h in the future is rejected (would wedge the watermark)."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    far_future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    rec = _make_record(asof=far_future)
    # The exception is swallowed by apply_execution and an audit event fires.
    # Run via _unsafe to confirm the guard raises.
    with pytest.raises(ValueError, match="future"):
        ps._apply_execution_unsafe(rec)


def test_apply_execution_accepts_recent_asof_within_24h(tmp_path: Path) -> None:
    """Sanity: 1 hour in the future is fine (clock skew tolerance)."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    near_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rec = _make_record(asof=near_future)
    # Should not raise:
    ps._apply_execution_unsafe(rec)


# ---------------------------------------------------------------------------
# C3: state_reconstruction_failed audit event on apply failure
# ---------------------------------------------------------------------------


def test_state_reconstruction_failed_audit_event_emitted_on_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3: when apply_execution raises, a state_reconstruction_failed audit
    event MUST land on the canonical audit log so silent state.db drift
    becomes visible."""
    audit_path = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", audit_path)

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # Force apply_execution to fail by giving an asof too far in the future:
    far_future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    rec = _make_record(asof=far_future)
    ps.apply_execution(rec)  # error swallowed; audit event emitted

    assert audit_path.exists()
    events = list(audit_log.read())
    failed = [e for e in events if e.kind == "state_reconstruction_failed"]
    assert len(failed) == 1
    assert failed[0].payload["error_type"] == "ValueError"
    assert failed[0].payload["proposal_id"] == rec["proposal_id"]
    assert failed[0].payload["asset"] == "MRNA"


def test_state_reconstruction_failed_kind_is_in_valid_kinds() -> None:
    """C3: the new event kind is registered in the audit-log enum."""
    assert "state_reconstruction_failed" in audit_log.VALID_KINDS


# ---------------------------------------------------------------------------
# C4: file mode hardening
# ---------------------------------------------------------------------------


def test_state_db_file_is_chmod_0o600(tmp_path: Path) -> None:
    """C4: state.db file must NOT be world-readable."""
    db_path = tmp_path / "state.db"
    PortfolioState(state_db_path=db_path)
    mode = stat.S_IMODE(db_path.stat().st_mode)
    # Allow 0o600 or stricter; fail on anything more permissive.
    assert mode == 0o600 or mode == 0, f"state.db has mode {oct(mode)}; expected 0o600"


def test_state_db_parent_dir_is_chmod_0o700(tmp_path: Path) -> None:
    """C4: parent dir hardened to 0o700."""
    db_path = tmp_path / "myquant" / "state.db"
    PortfolioState(state_db_path=db_path)
    mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    assert mode == 0o700, f"parent dir has mode {oct(mode)}; expected 0o700"


# ---------------------------------------------------------------------------
# Flat-position fix: closed positions are removed from positions table
# ---------------------------------------------------------------------------


def test_closed_position_is_deleted_from_positions_table(tmp_path: Path) -> None:
    """When a long is fully closed by an opposing short, the row must be
    removed from positions (state.db is the OPEN-positions cache)."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # Open: +0.05 long
    ps.apply_execution(_make_record(proposal_id="p1", asset="AAPL", fill_size_pct=0.05, fill_price=200.0))
    # Close: -0.05 short
    ps.apply_execution(_make_record(proposal_id="p2", asset="AAPL", fill_size_pct=-0.05, fill_price=210.0))
    # The position should be GONE from the table, not stored as quantity=0.
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE symbol='AAPL'"
        ).fetchall()
        assert rows == [], f"closed position should be deleted, got rows={rows}"


def test_reconstruct_from_skips_flat_positions_in_table(tmp_path: Path) -> None:
    """Reconstruction should also drop flat positions, not write 0-quantity rows."""
    executions_path = tmp_path / "executions.jsonl"
    # Open and close in JSONL
    open_rec = _make_record(proposal_id="p1", asset="GOOG", fill_size_pct=0.10, fill_price=100.0)
    close_rec = _make_record(proposal_id="p2", asset="GOOG", fill_size_pct=-0.10, fill_price=105.0,
                             asof="2026-05-27T17:00:00Z")
    executions_path.write_text(
        json.dumps(open_rec) + "\n" + json.dumps(close_rec) + "\n",
        encoding="utf-8",
    )
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    result = ps.reconstruct_from(executions_path)
    # positions_written counts only OPEN positions written; flat rows are skipped
    assert result.positions_written == 0
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        assert rows == 0, f"reconstructed positions table should have no flat rows, got {rows}"


# ---------------------------------------------------------------------------
# M5: data_quality=0.0 preservation
# ---------------------------------------------------------------------------


def test_data_quality_zero_is_preserved_not_falsy_replaced() -> None:
    """M5: a legitimate data_quality value of `0` (falsy) must not be replaced
    by the metadata fallback via short-circuit `or`."""
    # Build a signal whose .data_quality == 0 (legitimate "score=0" case)
    av = AnalystView(
        analyst="ClassicalTA",
        direction=1,
        magnitude=0.02,
        confidence=0.85,
        confidence_raw=0.85,
        horizon="1d",
    )
    signal = AggregatedSignal(
        asset="AAPL",
        timeframe="1h",
        asset_class="equity",
        asof=pd.Timestamp("2026-05-27T00:00:00Z"),
        direction=1,
        magnitude=0.02,
        confidence=0.85,
        confidence_raw=0.85,
        horizon="1d",
        components=(av,),
        aggregator="bma",
        metadata={"data_quality": "FALLBACK_VALUE"},
    )
    # Inject a falsy data_quality directly on the signal via __dict__ trick
    # (AggregatedSignal is a frozen dataclass — set via object.__setattr__ to
    # simulate an upstream that emits data_quality=0).
    object.__setattr__(signal, "data_quality", 0)
    sp = _build_signal_provenance(signal)
    # If the M5 bug were present, we'd see "FALLBACK_VALUE" here.
    assert sp["data_quality"] == 0
