"""tests/cron/test_proposals_ttl_watchdog.py — TTL watchdog regression tests.

Per architecture critique 2026-05-27 Risk 3: proposals TTL silent expiry.
Verify watchdog correctly identifies aging proposals and stays silent otherwise.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# Load the script as a module without sys.path mangling
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quant-proposals-ttl-watchdog.py"
if not SCRIPT.exists():
    SCRIPT = Path.home() / ".hermes" / "scripts" / "quant-proposals-ttl-watchdog.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(),
    reason=f"quant-proposals-ttl-watchdog.py not found at {SCRIPT}",
)

spec = importlib.util.spec_from_file_location("ttl_watchdog", SCRIPT)
ttl_watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ttl_watchdog)


def _create_test_db(tmp_path: Path, proposals: list[dict]) -> Path:
    """Create a minimal proposals.db at tmp_path with given proposals."""
    db = tmp_path / "proposals.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            approved_at TEXT,
            rejected_at TEXT,
            expired_at TEXT,
            record_json TEXT NOT NULL
        )
    """)
    for p in proposals:
        conn.execute(
            "INSERT INTO proposals (proposal_id, state, symbol, asset_class, "
            "timeframe, created_at, expires_at, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["proposal_id"],
                p.get("state", "pending"),
                p["symbol"],
                p.get("asset_class", "equity"),
                p.get("timeframe", "1d"),
                p["created_at"],
                p["expires_at"],
                p.get("record_json", "{}"),
            ),
        )
    conn.commit()
    conn.close()
    return db


# --- Test cases ---


def test_silent_when_no_aging_proposals(tmp_path):
    """Fresh proposals (< 18h) → empty list → silent."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_fresh_1",
            "symbol": "AAPL",
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "expires_at": (now + timedelta(hours=22)).isoformat(),
        },
        {
            "proposal_id": "prop_fresh_2",
            "symbol": "MSFT",
            "created_at": (now - timedelta(hours=10)).isoformat(),
            "expires_at": (now + timedelta(hours=14)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    assert aging == []
    assert ttl_watchdog.render_alert(aging) == ""


def test_alerts_when_proposal_aging(tmp_path):
    """Proposal > 18h elapsed (still unexpired) → alert."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_aging_1",
            "symbol": "NVDA",
            "created_at": (now - timedelta(hours=20)).isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    assert len(aging) == 1
    assert aging[0]["proposal_id"] == "prop_aging_1"
    assert aging[0]["symbol"] == "NVDA"
    assert aging[0]["age_hours"] >= 18.0
    assert aging[0]["expires_in_hours"] > 0

    alert = ttl_watchdog.render_alert(aging)
    assert "Proposals TTL Watchdog" in alert
    assert "prop_aging_1" in alert
    assert "NVDA" in alert
    assert "approve <PROPOSAL_ID>" in alert


def test_skips_already_expired(tmp_path):
    """Proposal past expires_at → not aging-warned (separate cleanup concern)."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_already_expired",
            "symbol": "TSLA",
            "created_at": (now - timedelta(hours=30)).isoformat(),
            "expires_at": (now - timedelta(hours=6)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    # expires_in_hours <= 0 means already expired; not in scope of warner
    assert aging == []


def test_skips_non_pending_states(tmp_path):
    """Approved/rejected proposals are ignored even if aging."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_approved",
            "state": "approved",
            "symbol": "GOOG",
            "created_at": (now - timedelta(hours=20)).isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    assert aging == []


def test_silent_when_db_missing(tmp_path):
    """Missing DB → silent (system not yet provisioned)."""
    nonexistent = tmp_path / "nope.db"
    aging = ttl_watchdog.find_aging_proposals(nonexistent)
    assert aging == []


def test_sorted_by_most_urgent_first(tmp_path):
    """Multiple aging proposals → smallest expires_in_hours first."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_urgent",
            "symbol": "URGENT",
            "created_at": (now - timedelta(hours=23)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        },
        {
            "proposal_id": "prop_less_urgent",
            "symbol": "LATER",
            "created_at": (now - timedelta(hours=19)).isoformat(),
            "expires_at": (now + timedelta(hours=5)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    assert len(aging) == 2
    assert aging[0]["proposal_id"] == "prop_urgent"  # smallest expires_in first
    assert aging[1]["proposal_id"] == "prop_less_urgent"


def test_unparseable_timestamps_skipped(tmp_path):
    """Garbage timestamps → row skipped, not crash."""
    now = datetime.now(timezone.utc)
    db = _create_test_db(tmp_path, [
        {
            "proposal_id": "prop_garbage",
            "symbol": "BAD",
            "created_at": "not-a-timestamp",
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        },
        {
            "proposal_id": "prop_good",
            "symbol": "OK",
            "created_at": (now - timedelta(hours=20)).isoformat(),
            "expires_at": (now + timedelta(hours=4)).isoformat(),
        },
    ])

    aging = ttl_watchdog.find_aging_proposals(db)
    assert len(aging) == 1
    assert aging[0]["proposal_id"] == "prop_good"


def test_alert_includes_approve_instructions(tmp_path):
    """Alert message must remind operator to use proposal_id, not ticker."""
    now = datetime.now(timezone.utc)
    aging = [{
        "proposal_id": "prop_test",
        "symbol": "XYZ",
        "asset_class": "equity",
        "age_hours": 19.5,
        "expires_in_hours": 4.5,
    }]

    alert = ttl_watchdog.render_alert(aging)
    assert "approve <PROPOSAL_ID>" in alert
    assert "id only" in alert.lower() or "NOT ticker" in alert
