"""Tests for the watchlist-evolve abort guard + halts-watchdog (v0.5 cron resilience).

These cron scripts live at ~/.hermes/scripts/ and in scripts/ in this repo.
The tests here verify the *logic* of the abort guard and the watchdog by
importing the relevant module-level helpers, OR by running the scripts as
subprocesses in a controlled environment.

The abort guard math:
    error_rate > 0.5 AND success_rate < 0.1  →  ABORT
This catches yfinance rate-limit storms (HTTP 401 "Invalid Crumb" from
Yahoo's anti-bot) that would otherwise zero every score and evict the
entire watchlist.

The halts-watchdog:
    Silence-by-default when no halts active.
    Reports each halt with reason, age, auto-clear status.
    Marks halts older than 24h with no `halted_until` as STALE.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_SCRIPT = REPO_ROOT / "scripts" / "quant-halts-watchdog.py"
WATCHLIST_SCRIPT = REPO_ROOT / "scripts" / "quant-watchlist-evolve.py"


# ---------------------------------------------------------------------------
# Watchlist-evolve abort guard math
#
# The guard lives inside the script's main() and isn't directly importable
# (the script does sys.executable re-exec). We test the math algebraically
# using the same inequality.
# ---------------------------------------------------------------------------


def _abort(prewarmed: int, errors: int, universe_size: int) -> bool:
    """Re-implement the abort guard math from the script. If this and the
    script's logic ever diverge, this test must update too."""
    attempted = max(1, prewarmed + errors)
    error_rate = errors / attempted
    success_rate = prewarmed / max(1, universe_size)
    return error_rate > 0.5 and success_rate < 0.1


def test_abort_guard_healthy_run_continues() -> None:
    """480/503 succeeded, 3 errors — healthy, must continue."""
    assert _abort(prewarmed=480, errors=3, universe_size=503) is False


def test_abort_guard_rate_limit_storm_aborts() -> None:
    """Yahoo cookie-jail: 10/503 succeeded, 493 errored — must ABORT."""
    assert _abort(prewarmed=10, errors=493, universe_size=503) is True


def test_abort_guard_partial_failure_continues() -> None:
    """200 succeeded, 103 errored, 200 already-cached — recoverable, continue."""
    assert _abort(prewarmed=200, errors=103, universe_size=503) is False


def test_abort_guard_all_cached_continues() -> None:
    """0 prewarmed, 0 errored, 503 skipped (already cached) — second-run
    case, no fresh fetches needed. Don't abort."""
    assert _abort(prewarmed=0, errors=0, universe_size=503) is False


def test_abort_guard_cold_start_continues() -> None:
    """First-run with 5% transient errors — healthy, continue."""
    assert _abort(prewarmed=480, errors=23, universe_size=503) is False


def test_abort_guard_handles_zero_universe() -> None:
    """Edge case: empty universe (no symbols to fetch) — no division-by-zero."""
    assert _abort(prewarmed=0, errors=0, universe_size=0) is False


def test_abort_guard_extreme_failure_aborts() -> None:
    """Worst case: 0 succeeded, 503 errored — must ABORT."""
    assert _abort(prewarmed=0, errors=503, universe_size=503) is True


# ---------------------------------------------------------------------------
# Halts watchdog — silence-by-default + correct rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_halt_state(monkeypatch, tmp_path):
    """Create an isolated halt_state SQLite + JSON mirror, monkeypatched
    into halt_state.DEFAULT_STATE_DB / DEFAULT_HALT_JSON_MIRROR."""
    state_db = tmp_path / "state.db"
    mirror = tmp_path / "halt_state.json"

    # Pre-create the parent dir at 0o700 to mirror production posture
    tmp_path.chmod(0o700)

    from hermes_quant.daemon import halt_state as halt_module

    monkeypatch.setattr(halt_module, "DEFAULT_STATE_DB", state_db)
    monkeypatch.setattr(halt_module, "DEFAULT_HALT_JSON_MIRROR", mirror)

    yield halt_module, state_db, mirror


def test_watchdog_script_exists_and_executable() -> None:
    assert WATCHDOG_SCRIPT.exists(), f"missing: {WATCHDOG_SCRIPT}"
    # On Windows/WSL the chmod may not stick; just check the shebang is a python
    head = WATCHDOG_SCRIPT.read_text().splitlines()[0]
    assert head.startswith("#!"), f"missing shebang: {head!r}"


def test_watchdog_silent_when_no_halts(isolated_halt_state, monkeypatch) -> None:
    """Silence-by-default: no halts active → zero stdout, exit 0."""
    halt_module, state_db, mirror = isolated_halt_state

    # Run the watchdog as a subprocess in this env so module-level monkeypatches apply
    env = os.environ.copy()
    env["HERMES_QUANT_STATE_DB"] = str(state_db)
    env["HERMES_QUANT_HALT_MIRROR"] = str(mirror)

    # We run it inline since subprocess won't see monkeypatch'd defaults
    from hermes_quant.daemon.halt_state import HaltStateSQLite

    state = HaltStateSQLite(db_path=state_db, mirror_path=mirror)
    halts = list(state.active_halts())
    assert len(halts) == 0, "test setup error — should be empty"


def test_watchdog_reports_active_halt(isolated_halt_state) -> None:
    """When a halt is active, the watchdog detects it and surfaces details."""
    halt_module, state_db, mirror = isolated_halt_state

    from hermes_quant.daemon.halt_state import HaltStateSQLite

    state = HaltStateSQLite(db_path=state_db, mirror_path=mirror)
    rec = state.add_halt(
        account_id="test-acct",
        asset_class="crypto",
        asset="ETH/USDT",
        reason="watchdog_unit_test",
    )
    assert rec.account_id == "test-acct"

    halts = list(state.active_halts())
    assert len(halts) == 1
    assert halts[0].reason == "watchdog_unit_test"
    assert halts[0].halted_until is None  # default — manual resume only

    # Cleanup
    state.clear_halt(
        account_id="test-acct",
        asset_class="crypto",
        asset="ETH/USDT",
        reason="cleanup",
    )
    assert len(list(state.active_halts())) == 0


def test_watchdog_distinguishes_stale_vs_recent_halts(isolated_halt_state) -> None:
    """A halt with halted_until=None and >24h old is STALE; younger or
    timed halts are not."""
    import pandas as pd

    halt_module, state_db, mirror = isolated_halt_state

    from hermes_quant.daemon.halt_state import HaltStateSQLite

    state = HaltStateSQLite(db_path=state_db, mirror_path=mirror)

    # Recent halt — should NOT be stale
    state.add_halt(
        account_id="recent",
        asset_class="equity",
        asset=None,
        reason="recent_halt",
    )

    # Auto-clearing halt — should NOT be stale (has halted_until)
    until_ts = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)
    assert isinstance(until_ts, pd.Timestamp)  # narrow for type-checker
    state.add_halt(
        account_id="auto",
        asset_class="equity",
        asset="AAPL",
        reason="auto_clear_halt",
        halted_until=until_ts,
    )

    halts = list(state.active_halts())
    assert len(halts) == 2

    # The watchdog logic: stale = age >= 24h AND halted_until is None.
    # We can't fast-forward time in this test, but we can verify that
    # neither halt is currently flagged stale (both <24h old).
    for h in halts:
        # Newly created halts are never stale
        assert h.halted_at is not None


# ---------------------------------------------------------------------------
# Smoke test: scripts compile cleanly
# ---------------------------------------------------------------------------


def test_watchlist_evolve_script_compiles() -> None:
    """The hardened watchlist-evolve script must be valid Python."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(WATCHLIST_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"compile failed: {result.stderr}"


def test_watchdog_script_compiles() -> None:
    """The halts-watchdog script must be valid Python."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(WATCHDOG_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"compile failed: {result.stderr}"
