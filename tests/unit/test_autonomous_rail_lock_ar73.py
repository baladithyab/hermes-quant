"""ar73 — the ADR-0016 §D9 per-account rail lock around autonomous.tick().

Companion to tests/unit/test_autonomous_concurrent_cap_race.py (the cross-process
RED->GREEN overshoot proof: two overlapping ticks each admitting a DISTINCT new
symbol against the same stale pre-fire book breached max_concurrent_positions =>
book ended at cap+1; the per-account lock serializes the read-decide-fire window
so it ends at exactly cap).

This file covers the two NON-race properties the lock must hold:

  * test_single_tick_byte_identical_under_lock — a single tick with NO contention
    acquires the lock on the first non-blocking try and behaves EXACTLY as the
    pre-ar73 path: the advisor is consulted, the watchlist symbol FIRES, the
    result carries fires=1 / a FIRE decision / a fired_this_tick budget. The lock
    is invisible when uncontended (silence-by-default is NOT triggered).

  * test_contention_skips_tick — when the per-account rail lock is already HELD
    (simulating an in-flight overlapping tick that hasn't committed) past the
    short acquire timeout, tick() SKIPS: it returns an honest EMPTY result (zero
    fires, zero silences, zero decisions) WITHOUT ever consulting the advisor, and
    it does NOT proceed unguarded against a stale pre-fire book. Recoverable next
    tick.

  * test_fail_open_safe_on_unopenable_lock_dir — a genuine flock-unsupported infra
    error (the lock file cannot be opened at all) DEGRADES to today's unguarded
    behavior (the tick still runs and fires) rather than wedging the always-on
    money tick — matching the documented daemon/tick_lock posture.

The lock is ALWAYS ON (a hard ADR-0016 §D9 rail, not a default-OFF refinement), so
none of these tests set an opt-in flag.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


def _write_autonomous_config(home: Path, *, max_concurrent: int = 5) -> None:
    """~/.hermes/config.yaml putting the tick into autonomous mode with a healthy
    kill-switch (0.0 disables the live trip) and a high per-tick cap."""
    cfg_dir = home / ".hermes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "quant:\n"
        "  pdr:\n"
        "    mode: autonomous\n"
        "  autonomous:\n"
        f"    max_concurrent_positions: {max_concurrent}\n"
        "    max_per_tick_opens: 5\n"
        "    kill_switch_pct: 0.0\n",
        encoding="utf-8",
    )


def _fire_advisor(consulted: list[str] | None = None):
    """High-conviction LONG advisor stub that clears the silence-bias gate.

    If ``consulted`` is provided, each call appends the symbol — so a test can
    assert the advisor was (or was NOT) reached.
    """

    def _recommend(**kwargs: Any) -> dict[str, Any]:
        if consulted is not None:
            consulted.append(kwargs.get("symbol", "?"))
        return {
            "as_of": "2026-06-13T00:00:00Z",
            "decision_price": 200.0,
            "aggregated_signal": {"direction": 1, "confidence": 0.9, "magnitude": 0.02},
            "risk_gate": {
                "pass": True,
                "gated_reason": None,
                "kelly_fraction": 0.05,
                "reason": "test_long",
            },
            "analyst_views": [
                {"metadata": {"atr_relative": 0.01}},
                {"metadata": {"atr_relative": 0.01}},
            ],
            "lessons": [],
        }

    return _recommend


def _isolate_env(monkeypatch, home: Path) -> Path:
    """Point HOME + autonomous module paths at an isolated tmp home; force the
    default-OFF refinement flags OFF so the path is the byte-identical baseline.
    Returns the quant_home."""
    monkeypatch.setenv("HOME", str(home))
    quant_home = home / ".hermes" / "quant"
    quant_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auto, "QUANT_HOME", quant_home)
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", quant_home / "autonomous_kill_switch.json")
    for flag in ("HERMES_QUANT_PORTFOLIO_CAPS", "HERMES_QUANT_ADMISSIBILITY"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")  # no perception fetch
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    if hasattr(auto, "_read_config"):
        try:
            auto._read_config.cache_clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return quant_home


def test_single_tick_byte_identical_under_lock(tmp_path: Path, monkeypatch) -> None:
    """A single uncontended tick acquires the rail lock immediately and FIRES
    exactly as the pre-ar73 path — the lock is invisible when there is no
    contention (no silence-by-default, no skip)."""
    home = tmp_path / "home"
    _write_autonomous_config(home)
    _isolate_env(monkeypatch, home)

    consulted: list[str] = []
    result = auto.tick(
        dry_run=True,  # dry-run still exercises the full rail region + cap math
        symbols=[WatchlistEntry(symbol="NVDA", asset_class="equity", timeframe="1d")],
        advisor_recommend=_fire_advisor(consulted),
    )

    # The advisor WAS consulted (the lock did not pre-empt the rail region).
    assert consulted == ["NVDA"], f"advisor must be consulted under uncontended lock; got {consulted}"
    # And the symbol fired (high-conviction LONG clears the gate, book empty < cap).
    assert result.fires == 1, f"expected exactly 1 fire, got fires={result.fires}"
    assert result.silences == 0
    assert result.errors == 0
    assert len(result.decisions) == 1
    assert result.decisions[0].symbol == "NVDA"
    assert result.decisions[0].gate == "FIRE"


def _hold_rail_lock(quant_home: Path):
    """Acquire the ar73 per-account rail lock file EXCLUSIVELY and return an open
    locked fd. The caller must close it to release. Mirrors the path tick()'s
    _account_rail_lock builds: QUANT_HOME / f"autonomous-rail-{account}.lock"."""
    quant_home.mkdir(parents=True, exist_ok=True)
    lock_path = quant_home / f"autonomous-rail-{auto._AUTONOMOUS_ACCOUNT_ID}.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_contention_skips_tick(tmp_path: Path, monkeypatch) -> None:
    """When the per-account rail lock is already HELD past the (short) acquire
    timeout, tick() SKIPS: it returns an honest EMPTY result and NEVER consults
    the advisor (so it cannot fire against a stale pre-fire book). Recoverable
    next tick."""
    home = tmp_path / "home"
    _write_autonomous_config(home)
    quant_home = _isolate_env(monkeypatch, home)

    # Simulate an in-flight overlapping tick: hold the rail lock for this account.
    held_fd = _hold_rail_lock(quant_home)
    # Make the second tick's acquire timeout tiny so it SKIPS quickly (contention,
    # NOT a fail-open — a would-block must never proceed unguarded).
    monkeypatch.setenv(auto._RAIL_LOCK_TIMEOUT_ENV, "0.1")

    consulted: list[str] = []
    try:
        result = auto.tick(
            dry_run=False,
            symbols=[WatchlistEntry(symbol="NVDA", asset_class="equity", timeframe="1d")],
            advisor_recommend=_fire_advisor(consulted),
        )
    finally:
        os.close(held_fd)  # release the held lock

    # SKIPPED: the advisor was never reached, so nothing fired against a stale book.
    assert consulted == [], f"contended tick must NOT consult the advisor; got {consulted}"
    assert result.fires == 0, f"contended tick must NOT fire; got fires={result.fires}"
    assert result.silences == 0
    assert result.errors == 0
    assert result.decisions == [], f"contended tick must return an EMPTY result; got {result.decisions}"

    # And once the holder releases, a fresh tick proceeds normally (recoverable).
    monkeypatch.delenv(auto._RAIL_LOCK_TIMEOUT_ENV, raising=False)
    consulted2: list[str] = []
    result2 = auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry(symbol="NVDA", asset_class="equity", timeframe="1d")],
        advisor_recommend=_fire_advisor(consulted2),
    )
    assert consulted2 == ["NVDA"], "after release the rail lock acquires immediately"
    assert result2.fires == 1


def test_fail_open_safe_on_unopenable_lock(tmp_path: Path, monkeypatch) -> None:
    """A genuine infra error opening the lock file DEGRADES to today's unguarded
    behavior (the tick still runs + fires) rather than wedging the always-on money
    tick — the documented fail-open-safe posture for an infrastructure fault (vs.
    contention, which SKIPS)."""
    home = tmp_path / "home"
    _write_autonomous_config(home)
    _isolate_env(monkeypatch, home)

    # Force os.open to raise an OSError ONLY for the rail lock file so the
    # context manager hits its fail-open-safe branch. Other os.open calls (state.db,
    # bus, etc. — here dry_run so minimal) pass through.
    real_open = os.open
    sentinel = f"autonomous-rail-{auto._AUTONOMOUS_ACCOUNT_ID}.lock"

    def _flaky_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(path, (str, bytes, os.PathLike)) and sentinel in os.fspath(path):
            raise OSError("simulated: read-only fs / no flock support")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _flaky_open)

    consulted: list[str] = []
    result = auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry(symbol="NVDA", asset_class="equity", timeframe="1d")],
        advisor_recommend=_fire_advisor(consulted),
    )

    # Fail-OPEN-SAFE: the rail region STILL ran (advisor consulted, symbol fired) —
    # the always-on tick is not wedged by an infrastructure fault.
    assert consulted == ["NVDA"], "fail-open-safe must run the rail region unguarded"
    assert result.fires == 1
    assert result.decisions[0].gate == "FIRE"
