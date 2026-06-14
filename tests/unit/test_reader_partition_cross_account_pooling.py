"""cs18 — reconstruct_portfolio_state pools the alpaca-paper SHADOW book into the
paper-default rail/caps view.

THE BUG (orchestrator-scouted, RED-proven here):
    hermes_quant/portfolio/state.py reconstruct_portfolio_state keys
    latest_per_symbol by `asset` ALONE — no account_id, no asset_class. It filters
    only on `reactor_name` (portfolio/state.py:95-97). The alpaca-paper book is a
    DELIBERATELY SEPARATE partition (react/alpaca_paper.py:67 ALPACA_ACCOUNT_ID =
    "alpaca-paper") emitted under reactor_name "alpaca_paper", whereas the synthetic
    paper-default book is emitted under "paper" / "deterministic-equity"
    (account_id "paper-default", carried in reactor_metadata.account_id).

    cs16 (2f1a280) made BOTH the ADR-0016 §D9 max_concurrent_positions hard rail
    (autonomous.py:523) and the portfolio-caps gate (autonomous.py:551) call
    reconstruct_portfolio_state(reactor_filter=None). With the reactor filter OFF,
    the alpaca-paper SHADOW book's fills now contribute to the SAME asset-keyed dict
    the paper-default PortfolioCaps gross/net is computed from.

    Two failure modes:
      (A) DIFFERENT symbols  -> the shadow book's |fraction| is SUMMED into the
          paper-default gross/net  => caps see exposure that is NOT in the book the
          fires actually hit  => false caps silence / distorted headroom.
      (B) SAME symbol in both books -> the asset-only key COLLAPSES the two distinct
          logical positions (paper-default + alpaca-paper) to ONE row at the latest
          asof_execution => the D9 rail UNDER-counts the true concurrent count.

    Mirrors the live bus 2026-06-14: paper-default holds {AAPL, BA}; the alpaca-paper
    shadow book holds {T +0.0010}. reactor_filter=None returns {AAPL, BA, T} — the
    rail counts 3 where paper-default truly holds 2, and gross is polluted by T.

THE CONTRACT cs18 ASKS FOR (Option A, additive):
    The rail/caps must reconstruct ONLY the account the fires hit (paper-default,
    which INCLUDES deterministic-equity per reactor_metadata.account_id ==
    "paper-default"), EXCLUDING the alpaca-paper SHADOW partition — WITHOUT changing
    reconstruct_portfolio_state's public {symbol: float} return type. This mirrors
    the cs14 loader's already-shipped `_record_account` resolution
    (daemon/portfolio_loader.py:103-118: top-level account_id OR
    reactor_metadata.account_id OR the "paper-default" sentinel).

This test FAILS RED on current source (no account scoping exists) and PASSES GREEN
only after an additive `account` parameter is added that defaults to None
(= today's byte-identical whole-book behavior).
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.portfolio.state import reconstruct_portfolio_state
from hermes_quant.risk.portfolio_normalize import PortfolioCaps, PortfolioState


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def _paper_rec(asset: str, ts: str, target: float, reactor: str, acct: str) -> dict:
    """An equity ExecutionRecord as it lands in executions.jsonl: account_id lives
    INSIDE reactor_metadata (the bus has NO top-level account_id — verified on the
    live bus: 0/46 records carry a top-level account_id)."""
    return {
        "asset": asset,
        "asof_execution": ts,
        "target_position_pct": target,
        "reactor_name": reactor,
        "reactor_metadata": {"account_id": acct},
    }


def _mixed_book(path: Path) -> None:
    """paper-default holds {AAPL +0.05 (det-equity), BA -0.20 (paper)};
    the alpaca-paper SHADOW book holds {T +0.0010}. (Live-bus shape, 2026-06-14.)"""
    _write_jsonl(
        path,
        [
            _paper_rec("BA", "2026-06-14T17:00:00Z", -0.20, "paper", "paper-default"),
            _paper_rec(
                "AAPL", "2026-06-14T17:00:01Z", 0.05, "deterministic-equity", "paper-default"
            ),
            _paper_rec("T", "2026-06-14T17:00:02Z", 0.0010, "alpaca_paper", "alpaca-paper"),
        ],
    )


# ---------------------------------------------------------------------------
# RED PROOF 1 — failure mode (A): the shadow book pollutes paper-default caps.
# ---------------------------------------------------------------------------
def test_shadow_book_pollutes_paper_default_caps(tmp_path: Path) -> None:
    """cs16's reactor_filter=None sums the alpaca-paper shadow fraction into the
    SAME PortfolioCaps gross/net the paper-default fires are clipped against."""
    p = tmp_path / "executions.jsonl"
    _mixed_book(p)

    # What the cs16 rail/caps gate ACTUALLY computes today (asset-only key, no scope).
    whole = reconstruct_portfolio_state(p, reactor_filter=None)
    assert set(whole.positions) == {"AAPL", "BA", "T"}, (
        "cs16 reactor_filter=None pools the alpaca-paper shadow symbol T into the "
        "paper-default view — this is the bug"
    )
    # The shadow book's T (+0.0010) is summed into gross/net the paper-default
    # PortfolioCaps headroom is computed from.
    polluted_gross = whole.gross_exposure_pct  # 0.20 + 0.05 + 0.0010 = 0.2510
    assert abs(polluted_gross - 0.2510) < 1e-9

    # The TRUE paper-default book (what the fires actually hit) is {AAPL, BA} only.
    true_pd = PortfolioState(positions={"AAPL": 0.05, "BA": -0.20})
    assert abs(true_pd.gross_exposure_pct - 0.2500) < 1e-9

    # PROOF the shadow book distorts the gate: the gross the gate sees != the true
    # paper-default gross. (Small here, but unbounded as the shadow book grows.)
    assert polluted_gross != true_pd.gross_exposure_pct, (
        "the alpaca-paper shadow book MUST NOT contribute to the paper-default "
        "PortfolioCaps gross — the two are deliberately separate books"
    )

    # Option-A contract (additive account scope) — FAILS RED until implemented.
    scoped = reconstruct_portfolio_state(p, reactor_filter=None, account="paper-default")
    assert set(scoped.positions) == {"AAPL", "BA"}, (
        "account='paper-default' must EXCLUDE the alpaca-paper shadow book while "
        "KEEPING deterministic-equity (which is itself account_id 'paper-default')"
    )
    assert abs(scoped.gross_exposure_pct - 0.2500) < 1e-9
    _ = PortfolioCaps()  # the gate this feeds


# ---------------------------------------------------------------------------
# RED PROOF 2 — failure mode (B): same symbol in two books collapses to one row,
# so the D9 concurrent-positions rail UNDER-counts.
# ---------------------------------------------------------------------------
def test_same_symbol_two_books_collapses_rail_undercount(tmp_path: Path) -> None:
    """When a symbol is OPEN in BOTH the paper-default book AND the alpaca-paper
    shadow book, the asset-only key collapses them to ONE row at the latest
    asof_execution. Two distinct logical positions in two distinct books are
    counted as ONE — the D9 hard rail under-counts the true concurrent count."""
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            # paper-default holds a real T position ...
            _paper_rec("T", "2026-06-14T17:00:00Z", 0.18, "paper", "paper-default"),
            # ... and the alpaca-paper SHADOW book ALSO holds T (separate book), with
            # a LATER asof so it wins the asset-only collapse.
            _paper_rec("T", "2026-06-14T17:00:05Z", 0.0010, "alpaca_paper", "alpaca-paper"),
        ],
    )

    whole = reconstruct_portfolio_state(p, reactor_filter=None)
    # The bug: ONE T row at the LATEST target (the tiny shadow 0.0010), masking the
    # real paper-default 0.18 entirely. The rail sees 1 position; there are truly 2
    # distinct logical positions (one per book), and the paper-default size is GONE.
    assert set(whole.positions) == {"T"}
    assert len(whole.positions) == 1, (
        "asset-only key collapses the two-book T to ONE row — the D9 rail "
        "under-counts (and the surviving target is the WRONG book's)"
    )
    assert abs(whole.positions["T"] - 0.0010) < 1e-9, (
        "the SHADOW book's tiny 0.0010 won the collapse, masking paper-default's "
        "0.18 — the rail/caps now see the wrong size AND the wrong count"
    )

    # Option-A contract — FAILS RED until implemented.
    scoped = reconstruct_portfolio_state(p, reactor_filter=None, account="paper-default")
    assert set(scoped.positions) == {"T"}
    assert abs(scoped.positions["T"] - 0.18) < 1e-9, (
        "account='paper-default' must recover the REAL paper-default T (0.18), "
        "not the alpaca-paper shadow's 0.0010"
    )


# ---------------------------------------------------------------------------
# BYTE-IDENTICAL GUARD — Option A default (account=None) must not change today.
# ---------------------------------------------------------------------------
def test_account_none_is_byte_identical_to_today(tmp_path: Path) -> None:
    """The additive `account` param defaults to None == today's whole-book behavior.
    On a single-account (paper-default-only) bus — i.e. the LIVE invariant before any
    alpaca_paper fill — account=None and account='paper-default' are identical."""
    p = tmp_path / "executions.jsonl"
    _write_jsonl(
        p,
        [
            _paper_rec("BA", "2026-06-14T17:00:00Z", -0.20, "paper", "paper-default"),
            _paper_rec(
                "AAPL", "2026-06-14T17:00:01Z", 0.05, "deterministic-equity", "paper-default"
            ),
        ],
    )
    today = reconstruct_portfolio_state(p, reactor_filter=None)
    assert set(today.positions) == {"AAPL", "BA"}

    # account=None MUST equal today (no shadow book present -> nothing to exclude).
    none_scoped = reconstruct_portfolio_state(p, reactor_filter=None, account=None)
    assert none_scoped.positions == today.positions, (
        "account=None must be byte-identical to today's whole-book reconstruction"
    )
    # And on a single-account book, scoping to paper-default is also identical.
    pd_scoped = reconstruct_portfolio_state(p, reactor_filter=None, account="paper-default")
    assert pd_scoped.positions == today.positions
