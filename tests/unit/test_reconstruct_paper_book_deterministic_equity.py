"""Regression: reconstruct_portfolio_state's default "paper" view must count the
DeterministicEquityReactor's fills, because they land in the SAME synthetic
``paper-default`` book the legacy PaperReactor writes — they are NOT a separate
broker partition.

Dual-ledger / divergence bug. With ``HERMES_QUANT_DETERMINISTIC_EQUITY=1`` LIVE
(set in ~/.hermes/.env), an operator HITL-approving an equity proposal routes
through ``react.dispatch.select_reactor`` -> ``DeterministicEquityReactor``,
which writes ``reactor_name="deterministic-equity"`` into ``account_id=paper-default``
(see ``DeterministicEquityReactor`` docstring: "shares the SAME book the autonomous
tick + the legacy PaperReactor read/write").

The ADR-0016 §D9 ``max_concurrent_positions`` HARD safety rail in ``autonomous.py``
counts open positions via ``reconstruct_portfolio_state(...).positions`` with the
DEFAULT ``reactor_filter="paper"``. Before the fix the default exact-matched ONLY
``reactor_name == "paper"``, so every position opened by the now-LIVE
deterministic-equity reactor was INVISIBLE to the rail — the rail UNDERCOUNTS the
real ``paper-default`` book and admits MORE concurrent fires than ADR-0016 §D9
permits (fail-OPEN on a money safety rail).

The Alpaca reactor (``reactor_name="alpaca_paper"``, ``account_id="alpaca-paper"``)
is a SEPARATE shadow partition and MUST stay excluded from the paper-book view.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.portfolio.state import reconstruct_portfolio_state


def _write(p: Path, rows: list[dict]) -> None:
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_default_paper_view_counts_deterministic_equity_fills(tmp_path: Path) -> None:
    """The default ``reactor_filter="paper"`` view must include deterministic-equity
    fills (same paper-default book) so the ADR-0016 §D9 concurrent-position rail
    sees the TRUE open count — not just the legacy "paper" reactor's positions."""
    p = tmp_path / "executions.jsonl"
    _write(
        p,
        [
            # legacy PaperReactor fill
            {
                "asset": "NVDA",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:00:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "paper",
                "reactor_metadata": {"account_id": "paper-default"},
            },
            # now-LIVE DeterministicEquityReactor fills — SAME paper-default book
            {
                "asset": "AAPL",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:01:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "deterministic-equity",
                "reactor_metadata": {"account_id": "paper-default", "quantity": 50.0},
            },
            {
                "asset": "MSFT",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:02:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "deterministic-equity",
                "reactor_metadata": {"account_id": "paper-default", "quantity": 25.0},
            },
        ],
    )

    # Default filter == the ADR-0016 §D9 safety-rail call (autonomous.py reads
    # reconstruct_portfolio_state(...).positions with NO reactor_filter arg).
    state = reconstruct_portfolio_state(p)

    # RED before fix: only NVDA (reactor_name=="paper") was counted -> len 1.
    # The rail would believe 1 position is open when 3 are.
    assert set(state.positions) == {"NVDA", "AAPL", "MSFT"}, (
        f"safety rail undercounts paper-default book: saw {sorted(state.positions)}"
    )
    assert len(state.positions) == 3


def test_default_paper_view_still_excludes_alpaca_partition(tmp_path: Path) -> None:
    """The Alpaca reactor writes the SEPARATE ``alpaca-paper`` shadow partition; it
    must NOT bleed into the synthetic paper-default book view (preserves the
    intent of test_reactor_filter_excludes_non_paper)."""
    p = tmp_path / "executions.jsonl"
    _write(
        p,
        [
            {
                "asset": "NVDA",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:00:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "paper",
            },
            # Alpaca shadow fill — separate alpaca-paper partition, MUST be excluded.
            {
                "asset": "TSLA",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:01:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "alpaca_paper",
                "reactor_metadata": {"account_id": "alpaca-paper"},
            },
            # the legacy-test spelling "alpaca" must also stay excluded.
            {
                "asset": "AMD",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:02:00Z",
                "target_position_pct": 0.10,
                "reactor_name": "alpaca",
            },
        ],
    )
    state = reconstruct_portfolio_state(p)
    assert "NVDA" in state.positions
    assert "TSLA" not in state.positions
    assert "AMD" not in state.positions
    assert len(state.positions) == 1


def test_explicit_paper_filter_matches_default(tmp_path: Path) -> None:
    """Passing reactor_filter="paper" explicitly behaves like the default — both
    count the paper-default book family (paper + deterministic-equity)."""
    p = tmp_path / "executions.jsonl"
    _write(
        p,
        [
            {
                "asset": "AAPL",
                "asset_class": "equity",
                "asof_execution": "2026-06-01T10:01:00Z",
                "target_position_pct": 0.15,
                "reactor_name": "deterministic-equity",
            },
        ],
    )
    assert "AAPL" in reconstruct_portfolio_state(p).positions
    assert "AAPL" in reconstruct_portfolio_state(p, reactor_filter="paper").positions
