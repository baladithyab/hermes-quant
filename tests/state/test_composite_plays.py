"""Tests for hermes_quant.state.composite_plays (ADR-0098 Part B).

Coverage
--------
1. open_composite inserts a row with state='open'
2. open → partial on first leg close (is_decompose=True, H1)
3. partial NEVER auto-closes (legs_remaining=None keeps partial)
4. partial stays partial when legs_remaining > 0
5. all legs closed → 'closed' (open + is_decompose=False, direct path)
6. partial + legs_remaining=0 → 'closed' (last leg closed after partial)
7. detect_orphan True when state=='open' and active_leg_count < expected
8. detect_orphan False when state=='open' and active_leg_count == expected
9. detect_orphan False when state=='partial' (already handled, not orphan)
10. detect_orphan raises CompositeNotFoundError on unknown id
11. illegal transition: closed → open raises
12. illegal transition: closed → partial raises
13. illegal transition: decomposed → closed raises
14. illegal transition: partial → open raises
15. BEGIN IMMEDIATE atomicity: a mid-transition error rolls back (row unchanged)
16. duplicate multi_leg_id raises IntegrityError on open_composite
17. non-finite net_entry_price raises ValueError
18. non-finite fill_size_pct raises ValueError
19. non-finite max_loss raises ValueError
20. transition_state open → decomposed
21. transition_state decomposed → closed raises
22. get() returns CompositePlayRow; get() on unknown returns None
23. list_open / list_partial filters correctly
24. closed_at is set when composite transitions to 'closed', None otherwise
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.state.composite_plays import (
    STATE_CLOSED,
    STATE_DECOMPOSED,
    STATE_OPEN,
    STATE_PARTIAL,
    CompositePlaysStore,
    CompositeNotFoundError,
    IllegalTransitionError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> CompositePlaysStore:
    return CompositePlaysStore(db_path=tmp_path / "composite_plays.db")


def _open(
    store: CompositePlaysStore,
    multi_leg_id: str = "prop_test_001",
    *,
    expected_leg_count: int = 2,
    account_id: str = "paper-default",
    underlying: str = "AAPL",
    strategy_kind: str = "covered_call",
    outer_qty: int = 1,
    net_entry_price: float = 1.50,
    fill_size_pct: float = 0.05,
    max_loss: float | None = None,
) -> None:
    store.open_composite(
        multi_leg_id=multi_leg_id,
        account_id=account_id,
        underlying=underlying,
        strategy_kind=strategy_kind,
        opened_at="2026-06-17T10:00:00.000000Z",
        outer_qty=outer_qty,
        net_entry_price=net_entry_price,
        fill_size_pct=fill_size_pct,
        expected_leg_count=expected_leg_count,
        max_loss=max_loss,
    )


# ---------------------------------------------------------------------------
# 1. open_composite inserts a row with state='open'
# ---------------------------------------------------------------------------


def test_open_composite_inserts_open_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_001")
    row = store.get("prop_001")
    assert row is not None
    assert row.state == STATE_OPEN
    assert row.underlying == "AAPL"
    assert row.strategy_kind == "covered_call"
    assert row.outer_qty == 1
    assert row.net_entry_price == pytest.approx(1.50)
    assert row.fill_size_pct == pytest.approx(0.05)
    assert row.expected_leg_count == 2
    assert row.closed_at is None


# ---------------------------------------------------------------------------
# 2. open → partial on first leg close (is_decompose=True, H1)
# ---------------------------------------------------------------------------


def test_open_to_partial_on_first_decompose_leg_close(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_002")
    new_state = store.record_leg_close("prop_002", is_decompose=True)
    assert new_state == STATE_PARTIAL
    row = store.get("prop_002")
    assert row is not None
    assert row.state == STATE_PARTIAL
    assert row.closed_at is None  # not closed yet


# ---------------------------------------------------------------------------
# 3. partial NEVER auto-closes (legs_remaining=None keeps partial)
# ---------------------------------------------------------------------------


def test_partial_never_auto_closes_when_legs_remaining_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_003", expected_leg_count=3)
    # First decompose leg close → partial
    store.record_leg_close("prop_003", is_decompose=True)
    # Second call with legs_remaining=None → stays partial (H1 invariant)
    new_state = store.record_leg_close("prop_003", is_decompose=True, legs_remaining=None)
    assert new_state == STATE_PARTIAL
    row = store.get("prop_003")
    assert row is not None
    assert row.state == STATE_PARTIAL


# ---------------------------------------------------------------------------
# 4. partial stays partial when legs_remaining > 0
# ---------------------------------------------------------------------------


def test_partial_stays_partial_when_legs_remaining_gt_zero(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_004", expected_leg_count=3)
    store.record_leg_close("prop_004", is_decompose=True)
    # Still 1 leg remaining → stays partial
    new_state = store.record_leg_close("prop_004", is_decompose=True, legs_remaining=1)
    assert new_state == STATE_PARTIAL


# ---------------------------------------------------------------------------
# 5. all legs closed → 'closed' (open + is_decompose=False)
# ---------------------------------------------------------------------------


def test_open_to_closed_direct_path(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_005")
    new_state = store.record_leg_close("prop_005", is_decompose=False)
    assert new_state == STATE_CLOSED
    row = store.get("prop_005")
    assert row is not None
    assert row.state == STATE_CLOSED


# ---------------------------------------------------------------------------
# 6. partial + legs_remaining=0 → 'closed' (last leg closed)
# ---------------------------------------------------------------------------


def test_partial_to_closed_when_legs_remaining_zero(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_006", expected_leg_count=2)
    store.record_leg_close("prop_006", is_decompose=True)  # → partial
    new_state = store.record_leg_close("prop_006", is_decompose=True, legs_remaining=0)
    assert new_state == STATE_CLOSED
    row = store.get("prop_006")
    assert row is not None
    assert row.state == STATE_CLOSED
    assert row.closed_at is not None


# ---------------------------------------------------------------------------
# 7. detect_orphan True when state=='open' and active_leg_count < expected
# ---------------------------------------------------------------------------


def test_detect_orphan_true_when_open_and_short_legs(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_007", expected_leg_count=2)
    # Only 1 leg active, expected 2 → orphaned
    assert store.detect_orphan("prop_007", active_leg_count=1) is True


# ---------------------------------------------------------------------------
# 8. detect_orphan False when state=='open' and active_leg_count == expected
# ---------------------------------------------------------------------------


def test_detect_orphan_false_when_all_legs_present(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_008", expected_leg_count=2)
    assert store.detect_orphan("prop_008", active_leg_count=2) is False


# ---------------------------------------------------------------------------
# 9. detect_orphan False when state=='partial'
# ---------------------------------------------------------------------------


def test_detect_orphan_false_when_partial(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_009", expected_leg_count=2)
    store.record_leg_close("prop_009", is_decompose=True)
    # partial state → not orphan signal (already in H1-managed state)
    assert store.detect_orphan("prop_009", active_leg_count=1) is False


# ---------------------------------------------------------------------------
# 10. detect_orphan raises CompositeNotFoundError on unknown id
# ---------------------------------------------------------------------------


def test_detect_orphan_raises_on_unknown_id(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(CompositeNotFoundError):
        store.detect_orphan("nonexistent_id", active_leg_count=0)


# ---------------------------------------------------------------------------
# 11–14. illegal transitions
# ---------------------------------------------------------------------------


def test_illegal_transition_closed_to_open_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_011")
    store.record_leg_close("prop_011", is_decompose=False)  # → closed
    with pytest.raises(IllegalTransitionError):
        store.transition_state("prop_011", target_state=STATE_OPEN)


def test_illegal_transition_closed_to_partial_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_012")
    store.record_leg_close("prop_012", is_decompose=False)  # → closed
    with pytest.raises(IllegalTransitionError):
        store.transition_state("prop_012", target_state=STATE_PARTIAL)


def test_illegal_transition_decomposed_to_closed_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_013")
    store.transition_state("prop_013", target_state=STATE_DECOMPOSED)
    with pytest.raises(IllegalTransitionError):
        store.transition_state("prop_013", target_state=STATE_CLOSED)


def test_illegal_transition_partial_to_open_raises(tmp_path: Path) -> None:
    """Partial cannot revert to open — operator must handle it, not auto-reset."""
    store = _make_store(tmp_path)
    _open(store, "prop_014", expected_leg_count=2)
    store.record_leg_close("prop_014", is_decompose=True)  # → partial
    with pytest.raises(IllegalTransitionError):
        store.transition_state("prop_014", target_state=STATE_OPEN)


# ---------------------------------------------------------------------------
# 15. BEGIN IMMEDIATE atomicity: a failed transition does not corrupt state
# ---------------------------------------------------------------------------


def test_atomicity_illegal_transition_leaves_row_unchanged(tmp_path: Path) -> None:
    """A failed (illegal) transition attempt must leave the row in its prior state.

    The implementation wraps every mutation in BEGIN IMMEDIATE / COMMIT with a
    ROLLBACK on any exception. We exercise this via an IllegalTransitionError
    raised inside the transaction: the row must still read 'open' after the
    exception escapes record_leg_close.
    """
    store = _make_store(tmp_path)
    _open(store, "prop_015")

    # Transition to 'closed' first (terminal state)
    store.record_leg_close("prop_015", is_decompose=False)
    row_closed = store.get("prop_015")
    assert row_closed is not None
    assert row_closed.state == STATE_CLOSED

    # Now attempt another transition — must fail and leave row closed
    with pytest.raises(IllegalTransitionError):
        store.record_leg_close("prop_015", is_decompose=True)

    row_after = store.get("prop_015")
    assert row_after is not None
    assert row_after.state == STATE_CLOSED  # unchanged


def test_atomicity_open_composite_rollback_on_duplicate(tmp_path: Path) -> None:
    """A duplicate open_composite (PK conflict) raises and leaves no partial state.

    The INSERT is wrapped in BEGIN IMMEDIATE; the conflict raises IntegrityError
    which triggers ROLLBACK — the duplicate row must not exist.
    """
    store = _make_store(tmp_path)
    _open(store, "prop_015b")

    # Verify first insert is present
    assert store.get("prop_015b") is not None

    # Second insert must raise and leave the original intact
    with pytest.raises(sqlite3.IntegrityError):
        store.open_composite(
            multi_leg_id="prop_015b",
            underlying="AAPL",
            strategy_kind="covered_call",
            opened_at="2026-06-17T11:00:00.000000Z",
            outer_qty=2,
            net_entry_price=2.00,
            fill_size_pct=0.10,
            expected_leg_count=2,
        )

    # Original row must be unchanged
    row = store.get("prop_015b")
    assert row is not None
    assert row.outer_qty == 1  # the original value, not 2
    assert row.net_entry_price == pytest.approx(1.50)  # the original value


# ---------------------------------------------------------------------------
# 16. duplicate multi_leg_id raises on open_composite
# ---------------------------------------------------------------------------


def test_duplicate_multi_leg_id_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_016")
    with pytest.raises(sqlite3.IntegrityError):
        _open(store, "prop_016")


# ---------------------------------------------------------------------------
# 17–19. finite guards
# ---------------------------------------------------------------------------


def test_non_finite_net_entry_price_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="net_entry_price"):
        store.open_composite(
            multi_leg_id="prop_017",
            underlying="AAPL",
            strategy_kind="covered_call",
            opened_at="2026-06-17T10:00:00.000000Z",
            outer_qty=1,
            net_entry_price=float("nan"),
            fill_size_pct=0.05,
            expected_leg_count=2,
        )


def test_non_finite_fill_size_pct_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="fill_size_pct"):
        store.open_composite(
            multi_leg_id="prop_018",
            underlying="AAPL",
            strategy_kind="covered_call",
            opened_at="2026-06-17T10:00:00.000000Z",
            outer_qty=1,
            net_entry_price=1.50,
            fill_size_pct=float("inf"),
            expected_leg_count=2,
        )


def test_non_finite_max_loss_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(ValueError, match="max_loss"):
        store.open_composite(
            multi_leg_id="prop_019",
            underlying="AAPL",
            strategy_kind="iron_condor",
            opened_at="2026-06-17T10:00:00.000000Z",
            outer_qty=1,
            net_entry_price=2.00,
            fill_size_pct=0.10,
            expected_leg_count=4,
            max_loss=float("nan"),
        )


# ---------------------------------------------------------------------------
# 20–21. transition_state explicit paths
# ---------------------------------------------------------------------------


def test_transition_state_open_to_decomposed(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_020")
    store.transition_state("prop_020", target_state=STATE_DECOMPOSED)
    row = store.get("prop_020")
    assert row is not None
    assert row.state == STATE_DECOMPOSED


def test_transition_state_decomposed_to_open_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_021")
    store.transition_state("prop_021", target_state=STATE_DECOMPOSED)
    with pytest.raises(IllegalTransitionError):
        store.transition_state("prop_021", target_state=STATE_OPEN)


# ---------------------------------------------------------------------------
# 22. get() / not-found
# ---------------------------------------------------------------------------


def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.get("nonexistent") is None


def test_get_returns_correct_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(
        store,
        "prop_022",
        underlying="SPY",
        strategy_kind="iron_condor",
        outer_qty=3,
        net_entry_price=2.75,
        fill_size_pct=0.10,
        expected_leg_count=4,
        max_loss=300.0,
    )
    row = store.get("prop_022")
    assert row is not None
    assert row.underlying == "SPY"
    assert row.strategy_kind == "iron_condor"
    assert row.outer_qty == 3
    assert row.net_entry_price == pytest.approx(2.75)
    assert row.expected_leg_count == 4
    assert row.max_loss == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# 23. list_open / list_partial
# ---------------------------------------------------------------------------


def test_list_open_and_list_partial(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_023a", expected_leg_count=2)
    _open(store, "prop_023b", expected_leg_count=2)
    _open(store, "prop_023c", expected_leg_count=2)
    # Close one directly
    store.record_leg_close("prop_023c", is_decompose=False)
    # Decompose one → partial
    store.record_leg_close("prop_023b", is_decompose=True)

    open_rows = store.list_open()
    partial_rows = store.list_partial()

    open_ids = {r.multi_leg_id for r in open_rows}
    partial_ids = {r.multi_leg_id for r in partial_rows}

    assert "prop_023a" in open_ids
    assert "prop_023b" not in open_ids
    assert "prop_023c" not in open_ids

    assert "prop_023b" in partial_ids
    assert "prop_023a" not in partial_ids


# ---------------------------------------------------------------------------
# 24. closed_at is set when composite transitions to 'closed', None otherwise
# ---------------------------------------------------------------------------


def test_closed_at_set_on_closed_none_on_partial(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _open(store, "prop_024a", expected_leg_count=2)
    _open(store, "prop_024b", expected_leg_count=2)

    # Direct close
    store.record_leg_close("prop_024a", is_decompose=False)
    row_a = store.get("prop_024a")
    assert row_a is not None
    assert row_a.closed_at is not None

    # Partial — no closed_at yet
    store.record_leg_close("prop_024b", is_decompose=True)
    row_b = store.get("prop_024b")
    assert row_b is not None
    assert row_b.closed_at is None

    # Complete the partial close
    store.record_leg_close("prop_024b", is_decompose=True, legs_remaining=0)
    row_b2 = store.get("prop_024b")
    assert row_b2 is not None
    assert row_b2.state == STATE_CLOSED
    assert row_b2.closed_at is not None
