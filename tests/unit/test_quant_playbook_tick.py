"""Tests for the daily playbook decision tick (ADR-0035 wave 2).

The script under test lives at ~/.hermes/scripts/quant-playbook-tick.py — it's
a Hermes cron script, not a hermes_quant package module. We load it via
importlib.util to keep the test self-contained.

These tests exercise the dry-run path with the mock advisor + mock snapshot
(via HERMES_QUANT_PLAYBOOK_TICK_MOCK=1), so they require neither a live
Alpaca account nor yfinance network calls. Coverage:

  * end-to-end run with synthetic watchlist + happy path FIRE on AAPL
  * silence rules: overnight gap and earnings lockout
  * idempotency: a second run on the same day produces zero new fires
  * halt fail-closed: aborts cleanly when halt_state.json has active halts
  * journal schema: every record has the expected fields
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "quant-playbook-tick.py"


@pytest.fixture
def tick_module(monkeypatch, tmp_path):
    """Load the script as a module with HOME redirected to tmp_path.

    Each test gets a clean fake-home so journal/halt/watchlist are isolated.
    """
    # Fake home — relocate every quant path the script reads/writes.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".hermes" / "quant" / "watchlist").mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "playbook").mkdir(parents=True)
    (fake_home / ".hermes" / "secrets").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Mock mode — bypass advisor + yfinance.
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", "1")

    # Force-reload by stripping any cached version, then load.
    sys.modules.pop("quant_playbook_tick", None)
    spec = importlib.util.spec_from_file_location("quant_playbook_tick", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Module-level path constants captured Path.home() at import time. Rebind
    # them to the fake-home equivalents so the test sees a clean filesystem.
    mod.HERMES_HOME = fake_home / ".hermes"
    mod.QUANT_HOME = mod.HERMES_HOME / "quant"
    mod.WATCHLIST_PATH = mod.QUANT_HOME / "watchlist" / "play-fit.json"
    mod.HALT_MIRROR_PATH = mod.QUANT_HOME / "halt_state.json"
    mod.PLAYBOOK_DIR = mod.QUANT_HOME / "playbook"
    mod.JOURNAL_PATH = mod.PLAYBOOK_DIR / "tick-journal.jsonl"
    mod.SECRETS_PATH = mod.HERMES_HOME / "secrets" / "alpaca.env"
    return mod


def _write_watchlist(mod, pairs):
    """pairs = [(symbol, play, score)]."""
    plays_dict: dict[str, list] = {}
    for sym, play, score in pairs:
        plays_dict.setdefault(play, []).append({
            "symbol": sym, "play": play, "state": "active", "last_score": score,
            "consecutive_days_above_floor": 1, "consecutive_days_below_onboard": 0,
            "extras": {}, "last_seen_at": "2026-05-26T19:48:18+00:00",
            "onboarded_at": "2026-05-25T19:48:18+00:00", "eviction_reason": None,
        })
    mod.WATCHLIST_PATH.write_text(json.dumps({
        "as_of": "2026-05-26T19:48:18+00:00",
        "plays": plays_dict,
    }))


# ---------------------------------------------------------------------------
# end-to-end happy path
# ---------------------------------------------------------------------------

def test_dry_run_aapl_fires_under_mock(tick_module):
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    summary = tick_module.run_tick(dry_run=True)
    assert summary["scanned"] == 1
    assert summary["fired"] == 1
    assert summary["silenced"] == 0
    assert summary["gate_rejected"] == 0
    assert summary["halt_aborted"] is False

    # Journal: one decision row + one summary row.
    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    assert len(rows) == 2
    decision = rows[0]
    assert decision["symbol"] == "AAPL"
    assert decision["play"] == "swing"
    assert decision["decision"] == "fire"
    assert decision["dry_run"] is True
    assert decision["order_id"] is None
    assert decision["notional_usd"] > 0
    assert "confidence" in decision


def test_dry_run_makes_no_alpaca_call(tick_module, monkeypatch):
    """Sanity-check: dry-run must never invoke place_paper_market_order."""
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])

    sentinel = mock.MagicMock(side_effect=AssertionError("dry-run should not place orders"))
    monkeypatch.setattr(tick_module, "place_paper_market_order", sentinel)

    summary = tick_module.run_tick(dry_run=True)
    assert summary["fired"] == 1
    sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# silence rules
# ---------------------------------------------------------------------------

def test_overnight_gap_silences(tick_module):
    """Mock snapshot for GAP1: 10% gap vs ~2% × 1.5 ATR threshold → SILENCE."""
    _write_watchlist(tick_module, [("GAP1", "swing", 0.9)])
    summary = tick_module.run_tick(dry_run=True)
    assert summary["fired"] == 0
    assert summary["silenced"] == 1
    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    decision = rows[0]
    assert decision["decision"] == "silenced"
    assert "overnight_gap" in decision["reason"]


def test_earnings_lockout_silences(tick_module):
    """Mock snapshot EARN: days_until_earnings=2 < 5 → SILENCE."""
    _write_watchlist(tick_module, [("EARN", "leaps", 0.9)])
    summary = tick_module.run_tick(dry_run=True)
    assert summary["fired"] == 0
    assert summary["silenced"] == 1
    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    decision = rows[0]
    assert decision["decision"] == "silenced"
    assert "days_until_earnings" in decision["reason"]


def test_unavailable_snapshot_silences(tick_module):
    _write_watchlist(tick_module, [("DARK", "swing", 0.9)])
    summary = tick_module.run_tick(dry_run=True)
    assert summary["silenced"] == 1


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

def test_idempotent_same_day_run(tick_module, monkeypatch):
    """Real (non-dry) fire then second run → second run is idempotent_skip."""
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    monkeypatch.setattr(
        tick_module, "place_paper_market_order",
        lambda sym, notional, side="buy": {"id": "fake-order-1", "client_order_id": "c1",
                                             "submitted_at": "2026-05-26T13:00:00Z"},
    )

    s1 = tick_module.run_tick(dry_run=False)
    assert s1["fired"] == 1

    # Second run same day — no new orders.
    s2 = tick_module.run_tick(dry_run=False)
    assert s2["fired"] == 0
    assert s2["idempotent_skipped"] == 1

    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    fire_rows = [r for r in rows if r.get("decision") == "fire"]
    assert len(fire_rows) == 1


def test_dry_run_does_not_block_subsequent_real_fire(tick_module, monkeypatch):
    """Spec: dry-run rows do NOT count toward idempotency. A real fire after a
    dry-run on the same day must still be allowed."""
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    s1 = tick_module.run_tick(dry_run=True)
    assert s1["fired"] == 1  # dry-run "fire"

    monkeypatch.setattr(
        tick_module, "place_paper_market_order",
        lambda sym, notional, side="buy": {"id": "fake-real-1", "client_order_id": "c1"},
    )
    s2 = tick_module.run_tick(dry_run=False)
    assert s2["fired"] == 1
    assert s2["idempotent_skipped"] == 0


# ---------------------------------------------------------------------------
# halt fail-closed
# ---------------------------------------------------------------------------

def test_halt_active_aborts_tick(tick_module):
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    tick_module.HALT_MIRROR_PATH.write_text(json.dumps([
        {"account_id": "*", "asset_class": "*", "asset": "*",
         "reason": "test halt — all trading paused", "halted_at": "2026-05-26T13:00:00Z",
         "halted_until": None, "cleared_at": None, "halt_epoch": 1}
    ]))

    summary = tick_module.run_tick(dry_run=True)
    assert summary["halt_aborted"] is True
    assert summary["fired"] == 0
    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    abort_rows = [r for r in rows if r.get("decision") == "halt_abort"]
    assert len(abort_rows) == 1
    assert "test halt" in abort_rows[0]["reason"]


def test_cleared_halt_does_not_abort(tick_module):
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    tick_module.HALT_MIRROR_PATH.write_text(json.dumps([
        {"account_id": "*", "asset_class": "*", "asset": "*",
         "reason": "old", "halted_at": "2026-05-25T13:00:00Z",
         "halted_until": None, "cleared_at": "2026-05-26T08:00:00Z",
         "cleared_reason": "manual resume", "halt_epoch": 1}
    ]))
    summary = tick_module.run_tick(dry_run=True)
    assert summary["halt_aborted"] is False
    assert summary["fired"] == 1


# ---------------------------------------------------------------------------
# multi-pair + non-equity filter
# ---------------------------------------------------------------------------

def test_non_equity_plays_skipped(tick_module):
    """covered_call/csp/wheel are filtered out before scanning (Half A: equity-only)."""
    _write_watchlist(tick_module, [
        ("AAPL", "swing", 0.9),
        ("MSFT", "covered_call", 0.9),  # filtered
        ("NVDA", "csp", 0.9),            # filtered
        ("AMZN", "wheel", 0.9),          # filtered
        ("AAPL", "leaps", 0.9),
    ])
    summary = tick_module.run_tick(dry_run=True)
    # Only AAPL/swing + AAPL/leaps survive the equity filter.
    assert summary["scanned"] == 2
    assert summary["fired"] == 2  # mock fires AAPL


def test_journal_schema_completeness(tick_module):
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])
    tick_module.run_tick(dry_run=True)
    rows = [json.loads(l) for l in tick_module.JOURNAL_PATH.read_text().splitlines() if l.strip()]
    decision = rows[0]
    # Required fields per task spec:
    # {ts, symbol, play, decision, confidence, reason, order_id?}
    for k in ("ts", "symbol", "play", "decision", "reason", "tick_id", "date_et", "dry_run"):
        assert k in decision, f"missing field {k!r}"
