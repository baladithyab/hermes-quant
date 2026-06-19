"""Unit tests for the ADR-0078 tick-lock helper (hermes_quant.daemon.tick_lock).

These exercise the lock's CONTRACT directly (in-process), complementing the
multiprocessing race test in test_tick_lock_race.py:
  * acquire succeeds and releases cleanly (re-acquirable afterward);
  * a second holder of the SAME triple is contended -> acquired=False, contended=True,
    within the timeout (does NOT block forever);
  * different symbols never contend;
  * a lock-open failure FAILS OPEN (acquired=False, fail_open=True), never raises;
  * an flock-unsupported error FAILS OPEN, never raises;
  * the lock file lives under QUANT_HOME/locks (honors HERMES_QUANT_HOME) and the
    symbol is path-sanitized.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_quant.daemon import tick_lock as tl


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path))
    return tmp_path


def test_lock_path_under_quant_home_locks_and_sanitized(tmp_path: Path) -> None:
    p = tl.lock_path_for("paper-default", "equity", "BRK/B")
    # Lives under QUANT_HOME/locks and the unsafe '/' is escaped (no traversal).
    assert p.parent == tmp_path / "locks"
    assert "/" not in p.name.replace(".lock", "").replace("__", "")
    assert p.name == "paper-default__equity__BRK_B.lock"


def test_acquire_then_reacquire(tmp_path: Path) -> None:
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as r:
        assert r.acquired is True
        assert r.contended is False
        assert r.fail_open is False
        assert r.path is not None and r.path.exists()
    # Released on exit -> immediately re-acquirable.
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as r2:
        assert r2.acquired is True


def test_second_holder_same_symbol_is_contended_and_does_not_block(tmp_path: Path) -> None:
    """A second acquire of the SAME triple while the first is held must return
    contended=False-acquired within the timeout, NOT hang."""
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as first:
        assert first.acquired
        start = time.monotonic()
        with tl.symbol_tick_lock(
            "paper-default", "equity", "AAPL", timeout_s=0.2
        ) as second:
            elapsed = time.monotonic() - start
            assert second.acquired is False
            assert second.contended is True
            assert second.fail_open is False
            # Bounded by the timeout (small slack), proving non-blocking.
            assert elapsed < 2.0, f"contended acquire should be bounded, took {elapsed:.2f}s"


def test_different_symbols_never_contend(tmp_path: Path) -> None:
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as a:
        with tl.symbol_tick_lock(
            "paper-default", "equity", "MSFT", timeout_s=0.2
        ) as b:
            assert a.acquired and b.acquired  # different files, no contention


def test_fail_open_when_lock_file_unopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.open raising on the lock path => fail-open (no raise, acquired=False)."""
    real_open = os.open

    def _boom(path, *a, **k):  # type: ignore[no-untyped-def]
        if str(path).endswith(".lock"):
            raise OSError(13, "Permission denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr(tl.os, "open", _boom)
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as r:
        assert r.acquired is False
        assert r.fail_open is True
        assert r.contended is False
        assert "lock_open_failed" in r.reason


def test_fail_open_when_flock_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock raising a non-EAGAIN error (e.g. ENOTSUP) => fail-open, never raises,
    never busy-loops to the deadline."""
    import errno

    def _boom_flock(fd, op):  # type: ignore[no-untyped-def]
        raise OSError(errno.ENOTSUP, "flock not supported")

    monkeypatch.setattr(tl.fcntl, "flock", _boom_flock)
    start = time.monotonic()
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL", timeout_s=5.0) as r:
        assert r.acquired is False
        assert r.fail_open is True
        # An unsupported-flock error must break out immediately, NOT poll to 5s.
        assert (time.monotonic() - start) < 1.0


def test_timeout_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK_TIMEOUT_S", "0.05")
    with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as first:
        assert first.acquired
        start = time.monotonic()
        with tl.symbol_tick_lock("paper-default", "equity", "AAPL") as second:
            assert second.acquired is False and second.contended is True
            assert (time.monotonic() - start) < 1.0
