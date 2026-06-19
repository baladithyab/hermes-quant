"""aegis-ageq2: composite (asset_class, symbol) open-book keying companion.

The per-position stop sweep must key the open book by (asset_class, symbol), not
by symbol alone, so an options entry (asset_class="us_option") routes to the
options sweep instead of being hardcoded "equity". This tests the NEW companion
``reconstruct_open_book_composite`` which returns ``dict[tuple[str,str], float]``
from the SAME canonical executions.jsonl source — WITHOUT changing the existing
symbol-keyed ``reconstruct_portfolio_state`` signature (other callers depend on it).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.portfolio.state import (
    reconstruct_open_book_composite,
    reconstruct_portfolio_state,
)


def _write_bus(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _eq(asset: str, target: float, ts: str, asset_class: str = "equity") -> dict:
    return {
        "proposal_id": f"prop_{asset}",
        "asset": asset,
        "asset_class": asset_class,
        "reactor_name": "paper",
        "account_id": "paper-default",
        "fill_size_pct": target,
        "target_position_pct": target,
        "fill_price": 100.0,
        "decision_price": 100.0,
        "asof_execution": ts,
        "asof_decision": ts,
        "signal_id": f"sig_{asset}",
    }


def test_composite_key_carries_asset_class(tmp_path):
    """An equity AND an option entry must each key by (asset_class, symbol)."""
    bus = tmp_path / "executions.jsonl"
    _write_bus(
        bus,
        [
            _eq("ASTS", 0.20, "2026-06-04T15:00:00Z", "equity"),
            _eq("AAPL260116C00200000", 0.05, "2026-06-04T15:01:00Z", "us_option"),
        ],
    )
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {
        ("equity", "ASTS"): 0.20,
        ("us_option", "AAPL260116C00200000"): 0.05,
    }


def test_composite_values_match_symbol_keyed_for_equity_only(tmp_path):
    """For an equity-only book (today's reality) the VALUES must match the existing
    symbol-keyed reconstruct exactly — the composite key only adds the asset_class
    dimension, it must NOT change which fraction each symbol holds."""
    bus = tmp_path / "executions.jsonl"
    _write_bus(
        bus,
        [
            _eq("ASTS", 0.20, "2026-06-04T15:00:00Z"),
            _eq("NVDA", 0.10, "2026-06-04T15:00:01Z"),
        ],
    )
    symbol_keyed = reconstruct_portfolio_state(bus, reactor_filter=None).positions
    composite = reconstruct_open_book_composite(bus, reactor_filter=None)
    # Project the composite back to a symbol->float view and compare.
    projected = {sym: frac for (_ac, sym), frac in composite.items()}
    assert projected == symbol_keyed


def test_latest_target_supersedes_per_composite_key(tmp_path):
    """Latest-asof wins per (asset_class, symbol) key, mirroring the symbol-keyed
    reconstruct's supersede semantics."""
    bus = tmp_path / "executions.jsonl"
    _write_bus(
        bus,
        [
            _eq("ASTS", 0.20, "2026-06-04T15:00:00Z"),
            _eq("ASTS", 0.12, "2026-06-04T16:00:00Z"),  # later supersedes
        ],
    )
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {("equity", "ASTS"): 0.12}


def test_zero_target_dropped_per_composite_key(tmp_path):
    """A flatten-to-zero (latest target 0.0) drops the position from the book."""
    bus = tmp_path / "executions.jsonl"
    _write_bus(
        bus,
        [
            _eq("ASTS", 0.20, "2026-06-04T15:00:00Z"),
            _eq("ASTS", 0.0, "2026-06-04T16:00:00Z"),  # closed
        ],
    )
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {}


def test_missing_asset_class_defaults_to_equity(tmp_path):
    """A legacy record lacking asset_class keys as ("equity", symbol) — the live
    equity producers all stamp asset_class, but a defensive default keeps the
    equity path byte-identical for any historic record that omitted it."""
    bus = tmp_path / "executions.jsonl"
    rec = _eq("ASTS", 0.20, "2026-06-04T15:00:00Z")
    rec.pop("asset_class")
    _write_bus(bus, [rec])
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {("equity", "ASTS"): 0.20}


def test_nofill_record_excluded_from_composite_book(tmp_path):
    """A no-fill record (ar92 family) must not conjure a phantom composite position."""
    bus = tmp_path / "executions.jsonl"
    rec = _eq("ASTS", 0.20, "2026-06-04T15:00:00Z")
    rec["fill_price"] = 0.0
    rec["fill_size_pct"] = 0.0
    rec["reactor_metadata"] = {"no_fill": True}
    _write_bus(bus, [rec])
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {}


def test_nonfinite_target_dropped_composite(tmp_path):
    """A non-finite target (ar03 family) is dropped, never poisons the book."""
    bus = tmp_path / "executions.jsonl"
    rec = _eq("ASTS", 0.20, "2026-06-04T15:00:00Z")
    rec["target_position_pct"] = float("nan")
    _write_bus(bus, [rec])
    book = reconstruct_open_book_composite(bus, reactor_filter=None)
    assert book == {}


def test_missing_or_empty_file_returns_empty(tmp_path):
    assert reconstruct_open_book_composite(tmp_path / "nope.jsonl") == {}
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert reconstruct_open_book_composite(empty) == {}
