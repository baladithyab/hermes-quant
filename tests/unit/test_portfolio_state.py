"""Tests for hermes_quant.portfolio.state — PortfolioState reconstruction (ADR-0071).

Locks in the contract that `executions.jsonl` is the canonical source of truth
for current paper positions until ADR-0035 wave-4 lands a queryable state.db.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hermes_quant.portfolio.state import reconstruct_portfolio_state


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def test_empty_file_yields_empty_state(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    p.write_text("", encoding="utf-8")
    state = reconstruct_portfolio_state(p)
    assert state.positions == {}
    assert state.cash_pct == 1.0


def test_missing_file_yields_empty_state(tmp_path: Path) -> None:
    p = tmp_path / "nope.jsonl"
    state = reconstruct_portfolio_state(p)
    assert state.positions == {}


def test_single_fill_per_symbol(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:09:03Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "MSFT",
                "asof_execution": "2026-05-28T17:09:04Z",
                "target_position_pct": -0.10,
                "reactor_name": "paper",
            },
        ],
    )
    state = reconstruct_portfolio_state(p)
    assert math.isclose(state.positions["AAPL"], 0.20)
    assert math.isclose(state.positions["MSFT"], -0.10)
    assert math.isclose(state.gross_exposure_pct, 0.30)


def test_latest_fill_supersedes_earlier(tmp_path: Path) -> None:
    """Two fills on AAPL: second supersedes first (PaperReactor target semantics)."""
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T19:00:00Z",
                "target_position_pct": -0.15,
                "reactor_name": "paper",
            },
        ],
    )
    state = reconstruct_portfolio_state(p)
    assert math.isclose(state.positions["AAPL"], -0.15)


def test_zero_target_drops_position_by_default(tmp_path: Path) -> None:
    """target_position_pct=0 means 'closed' — drop from snapshot."""
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T19:00:00Z",
                "target_position_pct": 0.0,
                "reactor_name": "paper",
            },
        ],
    )
    state = reconstruct_portfolio_state(p)
    assert "AAPL" not in state.positions


def test_zero_target_retained_when_drop_zeros_false(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T19:00:00Z",
                "target_position_pct": 0.0,
                "reactor_name": "paper",
            },
        ],
    )
    state = reconstruct_portfolio_state(p, drop_zeros=False)
    assert state.positions["AAPL"] == 0.0


def test_asof_filter_excludes_later_fills(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T19:00:00Z",
                "target_position_pct": -0.15,
                "reactor_name": "paper",
            },
        ],
    )
    state = reconstruct_portfolio_state(p, asof="2026-05-28T18:00:00Z")
    # Only the 17:00 fill counts at this asof
    assert math.isclose(state.positions["AAPL"], 0.20)


def test_reactor_filter_excludes_non_paper(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "MSFT",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": -0.10,
                "reactor_name": "alpaca",  # live broker fill
            },
        ],
    )
    state = reconstruct_portfolio_state(p)
    assert "AAPL" in state.positions
    assert "MSFT" not in state.positions


def test_reactor_filter_none_includes_all(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            {
                "asset": "AAPL",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "MSFT",
                "asof_execution": "2026-05-28T17:00:00Z",
                "target_position_pct": -0.10,
                "reactor_name": "alpaca",
            },
        ],
    )
    state = reconstruct_portfolio_state(p, reactor_filter=None)
    assert "AAPL" in state.positions
    assert "MSFT" in state.positions


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    p = tmp_path / "executions.jsonl"
    p.write_text(
        '{"asset":"AAPL","asof_execution":"2026-05-28T17:00:00Z","target_position_pct":0.20,"reactor_name":"paper"}\n'
        "this is not json\n"
        '{"missing":"required_keys"}\n'
        '{"asset":"MSFT","asof_execution":"2026-05-28T17:00:00Z","target_position_pct":-0.10,"reactor_name":"paper"}\n',
        encoding="utf-8",
    )
    state = reconstruct_portfolio_state(p)
    assert "AAPL" in state.positions
    assert "MSFT" in state.positions


def test_reconstructs_43_position_book_correctly(tmp_path: Path) -> None:
    """Replay the 5/28 forensic case: 43 fills produces 43-position state."""
    p = tmp_path / "executions.jsonl"
    records = []
    for i in range(38):
        records.append(
            {
                "asset": f"SHORT{i}",
                "asof_execution": f"2026-05-28T17:09:{i:02d}Z",
                "target_position_pct": -0.20,
                "reactor_name": "paper",
            }
        )
    for i in range(5):
        records.append(
            {
                "asset": f"LONG{i}",
                "asof_execution": f"2026-05-28T19:39:{40+i:02d}Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            }
        )
    _write_jsonl(p, records)

    state = reconstruct_portfolio_state(p)
    assert len(state.positions) == 43
    assert math.isclose(state.gross_exposure_pct, 8.6)
    assert math.isclose(state.net_exposure_pct, -38 * 0.20 + 5 * 0.20)
    assert state.cash_pct < 0  # over-leveraged
