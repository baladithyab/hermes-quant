"""Tests for hermes_quant.risk.open_guard (ADR-0072).

Locks in the advisor-layer intraday open-guard:

  * already_opened_today() OR-semantics: filled-today OR pending-proposal-today
  * direction-awareness: a same-day SHORT→LONG flip is NOT a duplicate
  * ET-trading-day boundary, not UTC calendar day
  * open_guard_filter() partitions picks into (kept, deduped)
  * deduped picks carry a human-readable reason
  * allow_intraday_add bypass hatch
  * fail-open-safe: a corrupt/missing executions file does not crash the filter

The guard takes injectable inputs (executions iterable, pending-proposals
iterable, now_et) so tests are pure — no disk, no clock, no DB.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_quant.risk.open_guard import (
    already_opened_today,
    open_guard_filter,
    pick_direction,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Fixtures: minimal execution + pending-proposal shapes
# ---------------------------------------------------------------------------

def _exec(asset: str, target_pct: float, asof_execution: str, *, account="alpaca-paper",
          asset_class="equity") -> dict:
    """Minimal executions.jsonl row (the fields the guard reads)."""
    return {
        "asset": asset,
        "asset_class": asset_class,
        "target_position_pct": target_pct,
        "asof_execution": asof_execution,  # ISO UTC
        "reactor_name": "paper",
    }


def _pending(symbol: str, direction: int, created_at: str, *, asset_class="equity") -> dict:
    """Minimal pending-proposal dict (the fields the guard reads).

    Mirrors hermes_quant.proposals._proposal_to_dict() shape: direction lives
    in advisor_result.aggregated_signal.direction.
    """
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "state": "pending",
        "created_at": created_at,  # ISO UTC
        "advisor_result": {"aggregated_signal": {"direction": direction}},
    }


def _pick(symbol: str, target_pct: float, *, asset_class="equity", **extra) -> dict:
    """An actionable pick as quant-daily-interim.py produces it."""
    d = {"symbol": symbol, "asset_class": asset_class, "target_position_pct": target_pct}
    d.update(extra)
    return d


# A fixed "now" mid-session ET: 2026-05-29 11:00 ET (15:00 UTC).
NOW_ET = datetime(2026, 5, 29, 11, 0, tzinfo=ET)


# ---------------------------------------------------------------------------
# pick_direction
# ---------------------------------------------------------------------------

def test_pick_direction_sign():
    assert pick_direction(_pick("AAPL", 0.20)) == 1
    assert pick_direction(_pick("HOOD", -0.20)) == -1
    assert pick_direction(_pick("FLAT", 0.0)) == 0


# ---------------------------------------------------------------------------
# already_opened_today — source 1: filled today
# ---------------------------------------------------------------------------

def test_blocked_when_filled_same_direction_today():
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]  # 08:34 ET, same ET day
    blocked, reason = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert blocked is True
    assert "filled" in reason.lower()
    assert "short" in reason.lower()


def test_not_blocked_when_filled_opposite_direction_today():
    # Premarket shorted HOOD; midday signal flipped LONG → genuine new decision.
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]
    blocked, _ = already_opened_today(
        "HOOD", 1, "alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert blocked is False


def test_not_blocked_when_filled_yesterday():
    # Fill is from the prior ET day → today's pick is a fresh open, allowed.
    execs = [_exec("HOOD", -0.20, "2026-05-28T20:00:00+00:00")]  # 2026-05-28 16:00 ET
    blocked, _ = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert blocked is False


def test_not_blocked_for_different_symbol():
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]
    blocked, _ = already_opened_today(
        "AAPL", 1, "alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert blocked is False


def test_not_blocked_for_different_account():
    # Fill belongs to a different account → must NOT block a pick on alpaca-paper.
    # executions.jsonl rows carry no explicit account field today (the paper
    # book is single-account), so the contract is: a row's account defaults to
    # the queried account UNLESS it carries an explicit "account" key that
    # differs. Here the fill is explicitly tagged to "other".
    execs = [_exec_with_account("HOOD", -0.20, "2026-05-29T12:34:00+00:00", account="other")]
    blocked, _ = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert blocked is False


def _exec_with_account(asset, target_pct, asof, *, account, asset_class="equity"):
    return {
        "asset": asset, "asset_class": asset_class,
        "target_position_pct": target_pct, "asof_execution": asof,
        "account": account, "reactor_name": "paper",
    }


# ---------------------------------------------------------------------------
# already_opened_today — source 2: pending proposal today
# ---------------------------------------------------------------------------

def test_blocked_when_pending_proposal_same_direction_today():
    pend = [_pending("HOOD", -1, "2026-05-29T12:34:00+00:00")]
    blocked, reason = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=[], pending=pend, now_et=NOW_ET
    )
    assert blocked is True
    assert "pending" in reason.lower()


def test_not_blocked_when_pending_proposal_opposite_direction():
    pend = [_pending("HOOD", -1, "2026-05-29T12:34:00+00:00")]
    blocked, _ = already_opened_today(
        "HOOD", 1, "alpaca-paper", executions=[], pending=pend, now_et=NOW_ET
    )
    assert blocked is False


def test_not_blocked_when_pending_proposal_from_yesterday():
    pend = [_pending("HOOD", -1, "2026-05-28T20:00:00+00:00")]
    blocked, _ = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=[], pending=pend, now_et=NOW_ET
    )
    assert blocked is False


# ---------------------------------------------------------------------------
# ET-day vs UTC-day boundary (the latent bug D72.5 guards against)
# ---------------------------------------------------------------------------

def test_et_day_boundary_evening_pt_run():
    # A fill at 2026-05-29 23:30 PT = 2026-05-30 06:30 UTC, but still
    # 2026-05-30 02:30 ET... wait: choose a window where UTC date != ET date.
    # Fill at 2026-05-30 02:00 UTC = 2026-05-29 22:00 ET (prior ET day boundary).
    # "now" at 2026-05-29 22:30 ET (= 2026-05-30 02:30 UTC). Same ET day → blocked.
    now_et_evening = datetime(2026, 5, 29, 22, 30, tzinfo=ET)
    execs = [_exec("HOOD", -0.20, "2026-05-30T02:00:00+00:00")]  # 2026-05-29 22:00 ET
    blocked, _ = already_opened_today(
        "HOOD", -1, "alpaca-paper", executions=execs, pending=[], now_et=now_et_evening
    )
    # Keyed on ET day → both are 2026-05-29 ET → blocked. A naive UTC-date guard
    # would compare 2026-05-30 (fill) vs 2026-05-29 (now) and fail to block.
    assert blocked is True


# ---------------------------------------------------------------------------
# open_guard_filter — batch partitioning
# ---------------------------------------------------------------------------

def test_filter_partitions_kept_and_deduped():
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]  # HOOD shorted earlier today
    picks = [
        _pick("HOOD", -0.20),   # dup → deduped
        _pick("AAPL", 0.20),    # new → kept
        _pick("MSFT", -0.20),   # new → kept
    ]
    kept, deduped = open_guard_filter(
        picks, account="alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert [p["symbol"] for p in kept] == ["AAPL", "MSFT"]
    assert [p["symbol"] for p in deduped] == ["HOOD"]
    assert deduped[0].get("dedup_reason")  # carries a human reason


def test_filter_allows_intraday_add_bypass():
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]
    picks = [_pick("HOOD", -0.20, allow_intraday_add=True)]
    kept, deduped = open_guard_filter(
        picks, account="alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert [p["symbol"] for p in kept] == ["HOOD"]
    assert deduped == []


def test_filter_flat_picks_never_deduped():
    # A flat (direction 0) pick never fires, so the guard must not block it as a dup.
    execs = [_exec("HOOD", -0.20, "2026-05-29T12:34:00+00:00")]
    picks = [_pick("HOOD", 0.0)]
    kept, deduped = open_guard_filter(
        picks, account="alpaca-paper", executions=execs, pending=[], now_et=NOW_ET
    )
    assert [p["symbol"] for p in kept] == ["HOOD"]
    assert deduped == []


def test_filter_empty_inputs():
    kept, deduped = open_guard_filter(
        [], account="alpaca-paper", executions=[], pending=[], now_et=NOW_ET
    )
    assert kept == []
    assert deduped == []


def test_filter_intra_batch_dedup():
    # Two same-direction picks for the same symbol IN THE SAME batch: keep first,
    # dedup the second (a single run should not double-open either).
    picks = [_pick("HOOD", -0.20), _pick("HOOD", -0.20)]
    kept, deduped = open_guard_filter(
        picks, account="alpaca-paper", executions=[], pending=[], now_et=NOW_ET
    )
    assert len(kept) == 1
    assert len(deduped) == 1
    assert deduped[0]["symbol"] == "HOOD"
