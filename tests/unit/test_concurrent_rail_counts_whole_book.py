"""cs16 — the ADR-0016 §D9 max_concurrent_positions hard rail must count the WHOLE
open equity book, not just the paper-only slice.

THE BUG (orchestrator-confirmed on the live bus ~/.hermes/quant/executions.jsonl):
    hermes_quant/autonomous.py:513 counted the open book for the D9 hard rail via
    a BARE `reconstruct_portfolio_state(QUANT_HOME / "executions.jsonl").positions`,
    and the portfolio-caps gate at :538 via a BARE `reconstruct_portfolio_state()`.
    reconstruct_portfolio_state DEFAULTS reactor_filter="paper" (portfolio/state.py:40).
    But select_reactor (react/dispatch.py) routes equity fills to reactor_name
    "deterministic-equity" (HERMES_QUANT_DETERMINISTIC_EQUITY=1) and "alpaca_paper"
    (HERMES_QUANT_ALPACA_PAPER=1), both armed live. So the rail counted 1 where the
    true open book was 3 — it could open MORE than max_concurrent_positions.

This is a SAFETY RAIL. The conservative direction is fail-CLOSED: counting MORE
open positions (blocking more fires) is safe; counting FEWER (admitting more) is the
bug. The fix moves the rail to count the TRUE open book (reactor_filter=None at both
call sites), never below it.

Same class as cr00/cr01: a det-equity migration shipped + armed, but the safety-rail
consumer was not updated.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from hermes_quant import autonomous as auto
from hermes_quant.portfolio.state import reconstruct_portfolio_state


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """JSONL builder — same shape as tests/unit/test_portfolio_state.py."""
    path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def _three_reactor_open_book(path: Path) -> None:
    """A mixed-reactor bus with THREE distinct OPEN (non-zero) equity positions,
    one under each reactor_name the live router can emit:
        BA   under "paper"
        T    under "alpaca_paper"
        AAPL under "deterministic-equity"
    """
    _write_jsonl(
        path,
        [
            {
                "asset": "BA",
                "asof_execution": "2026-06-13T17:00:00Z",
                "target_position_pct": 0.20,
                "reactor_name": "paper",
            },
            {
                "asset": "T",
                "asof_execution": "2026-06-13T17:00:01Z",
                "target_position_pct": 0.15,
                "reactor_name": "alpaca_paper",
            },
            {
                "asset": "AAPL",
                "asof_execution": "2026-06-13T17:00:02Z",
                "target_position_pct": -0.10,
                "reactor_name": "deterministic-equity",
            },
        ],
    )


def test_default_filter_undercounts_whole_book(tmp_path: Path) -> None:
    """Documents the bug AND proves the 'paper' default is untouched.

    With the paper-only default filter, the rail sees ONE position (BA) where the
    true open equity book is THREE (BA + T + AAPL). reactor_filter=None sees all 3.
    """
    p = tmp_path / "executions.jsonl"
    _three_reactor_open_book(p)

    # Default filter (reactor_filter="paper") — the buggy view the rail used.
    paper_only = reconstruct_portfolio_state(p)
    assert set(paper_only.positions) == {"BA"}, (
        "default reactor_filter='paper' must still see ONLY the paper slice "
        "(proves portfolio/state.py:40 default is unchanged)"
    )

    # Whole equity book — what the safety rail MUST count.
    whole_book = reconstruct_portfolio_state(p, reactor_filter=None)
    assert set(whole_book.positions) == {"BA", "T", "AAPL"}, (
        "reactor_filter=None must count the WHOLE open equity book across all "
        "reactor_names (paper + alpaca_paper + deterministic-equity)"
    )
    assert len(whole_book.positions) == 3


def test_whole_book_no_double_count(tmp_path: Path) -> None:
    """Guards the over-count concern: asset-keying in reconstruct_portfolio_state
    already collapses any single symbol to ONE row, so a symbol written under two
    reactor names (e.g. an older paper fill superseded by a newer det-equity fill)
    appears exactly once at the LATEST target — no over-count, no de-dup needed.
    """
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            # Older paper fill on AAPL ...
            {
                "asset": "AAPL",
                "asof_execution": "2026-06-13T17:00:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "paper",
            },
            # ... superseded by a newer deterministic-equity fill on the SAME symbol.
            {
                "asset": "AAPL",
                "asof_execution": "2026-06-13T18:00:00Z",
                "target_position_pct": 0.05,
                "reactor_name": "deterministic-equity",
            },
        ],
    )

    whole_book = reconstruct_portfolio_state(p, reactor_filter=None)
    # Exactly ONE AAPL row (not two), at the LATEST target (0.05) — the rail cannot
    # over-count a single logical position written under two reactor names.
    assert set(whole_book.positions) == {"AAPL"}
    assert len(whole_book.positions) == 1
    assert abs(whole_book.positions["AAPL"] - 0.05) < 1e-9


def test_d9_rail_counts_all_reactor_names(tmp_path, monkeypatch) -> None:
    """The source-bound RED->GREEN guard.

    Both the D9 count path and the portfolio-caps path inside autonomous.tick must
    pass reactor_filter=None. FAILS RED on current source (no kwarg present); PASSES
    GREEN only after the fix. Paired with a behavioral arm that exercises the exact
    rail expression against the 3-reactor bus.
    """
    src = inspect.getsource(auto.tick)

    # The D9 hard-rail count path (QUANT_HOME bus, explicit path).
    assert "reactor_filter=None).positions" in src, (
        "the D9 max_concurrent_positions count path in autonomous.tick must pass "
        "reactor_filter=None so it counts the WHOLE open equity book"
    )
    # The portfolio-caps path (bare reconstruct_portfolio_state()).
    assert "reconstruct_portfolio_state(reactor_filter=None)" in src, (
        "the portfolio-caps path in autonomous.tick must pass reactor_filter=None "
        "so headroom is computed against the WHOLE open equity book"
    )
    # And the buggy bare-default forms must be GONE from both rail call sites.
    assert 'QUANT_HOME / "executions.jsonl").positions' not in src, (
        "the bare default-filter D9 count must be removed (it undercounts the book)"
    )

    # Behavioral arm: monkeypatch QUANT_HOME to a tmp bus and assert the rail's exact
    # expression counts all 3 reactor-name positions.
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    _three_reactor_open_book(tmp_path / "executions.jsonl")
    open_book = reconstruct_portfolio_state(
        auto.QUANT_HOME / "executions.jsonl", reactor_filter=None
    ).positions
    assert len(open_book) == 3
    assert set(open_book) == {"BA", "T", "AAPL"}
