"""Corrupt halt-mirror must FAIL CLOSED in the freqtrade consumer (kill-switch rail).

The hermes daemon writes ``~/.hermes/quant/halt_state.json`` as the cross-process
mirror of the durable SQLite halt registry (ADR-0009 / ADR-0016 §D9). The live
freqtrade crypto strategy reads this mirror — in a SEPARATE process — to decide
whether to enter safe-stop and refuse new entries, instead of opening the SQLite
DB (lock-contention avoidance). The mirror's own writer docstring
(``daemon/halt_state._write_atomic_json``) warns:

    "A torn or lost write therefore lets a live consumer read STALE halt state
     and trade an asset that is actually halted (fail-OPEN on a halt rail)."

DEFECT (the case this test pins): the consumer's halt-mirror check did

    try:
        if self.HALT_STATE_MIRROR.exists():
            halts = json.loads(self.HALT_STATE_MIRROR.read_text())
            if halts:
                self._enter_safe_stop(...)
    except (json.JSONDecodeError, OSError):
        pass                      # <-- FAIL OPEN

so a halt mirror that EXISTS but is CORRUPT / TORN (a partial write — exactly the
ar87 torn-mirror scenario, or any mid-rename crash / external corruption) makes
``json.loads`` raise ``JSONDecodeError``, the ``except`` swallows it, and the
strategy does NOT enter safe-stop — it keeps trading an asset that may be halted.
This is a FAIL-OPEN on the halt rail across a degraded read.

The established money-software posture for a corrupt halt mirror is FAIL-CLOSED:
the sibling ops driver ``ops/scripts/quant-autonomous-tick.py:read_active_halts``
returns a synthetic ``fail-closed`` halt on ``JSONDecodeError`` so the autonomous
tick aborts. The consumer must do the same: a corrupt halt mirror -> safe-stop,
never silently continue trading.

These tests are hermetic: every bus path is redirected to a tmp dir, and a FRESH
heartbeat is written so the dead-man-switch stays quiet — isolating the halt-mirror
read as the ONLY thing that can (and must) drive safe-stop. They do NOT edit any
existing test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch):
    s = HermesQuantConsumer({})
    sig_path = tmp_path / "signals.jsonl"
    exec_path = tmp_path / "executions.jsonl"
    halt_path = tmp_path / "halt_state.json"
    monkeypatch.setattr(s, "SIGNAL_BUS_PATH", sig_path)
    monkeypatch.setattr(s, "EXECUTION_BUS_PATH", exec_path)
    monkeypatch.setattr(s, "HALT_STATE_MIRROR", halt_path)
    return s, sig_path, halt_path


_NOW = pd.Timestamp("2026-06-16T10:10:00+00:00")


def _write_fresh_heartbeat(sig_path: Path) -> None:
    """A heartbeat 30s before _NOW so the dead-man-switch (60s) stays quiet —
    the halt-mirror read is then the ONLY thing that can trip safe-stop."""
    sig_path.write_text(
        json.dumps(
            {"type": "heartbeat", "schema_version": 1, "asof": "2026-06-16T10:09:30+00:00"}
        )
        + "\n"
    )


def test_corrupt_halt_mirror_fails_closed(strategy):
    """A halt mirror that EXISTS but is CORRUPT must enter safe-stop, not fail open.

    With the bug, json.loads raises JSONDecodeError, the except swallows it, and the
    strategy keeps trading an asset that may be halted. With the fix, a corrupt
    mirror is treated as a hard halt (fail-closed) -> safe-stop."""
    s, sig_path, halt_path = strategy
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")
    _write_fresh_heartbeat(sig_path)
    # Torn / partial write — a half-flushed '{"reason": "kil' fragment.
    halt_path.write_text('[{"reason": "kil')

    s._refresh_state(_NOW)

    assert s._safe_stop_active is True, (
        "halt rail FAILED OPEN: a corrupt/torn halt_state.json was swallowed and "
        "the strategy kept trading — a halted asset can be traded by the live "
        "freqtrade process (the exact torn-mirror fail-open the writer docstring warns about)"
    )
    assert "halt" in s._safe_stop_reason.lower()


def test_non_list_halt_mirror_fails_closed(strategy):
    """A halt mirror that is VALID JSON but not the expected list shape (e.g. a dict
    from a different writer / partial structural corruption) must ALSO fail closed:
    the consumer cannot establish 'no active halts', so it must not keep trading."""
    s, sig_path, halt_path = strategy
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")
    _write_fresh_heartbeat(sig_path)
    halt_path.write_text('{"unexpected": "shape"}')  # valid JSON, wrong type

    s._refresh_state(_NOW)

    assert s._safe_stop_active is True, (
        "halt rail FAILED OPEN on a non-list (structurally corrupt) halt mirror — "
        "an unreadable halt state must fail closed, not be treated as 'no halts'"
    )
    assert "halt" in s._safe_stop_reason.lower()


def test_valid_active_halt_still_safe_stops(strategy):
    """Non-vacuity / happy path: a VALID, non-empty halt mirror still enters
    safe-stop with the recorded reason — the fix must not break the real-halt path."""
    s, sig_path, halt_path = strategy
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")
    _write_fresh_heartbeat(sig_path)
    halt_path.write_text(json.dumps([{"reason": "operator_emergency_stop"}]))

    s._refresh_state(_NOW)

    assert s._safe_stop_active is True
    assert "operator_emergency_stop" in s._safe_stop_reason


def test_empty_halt_mirror_keeps_trading(strategy):
    """Control: an EMPTY-list halt mirror ([]) means 'no active halts' and must NOT
    safe-stop (with a fresh heartbeat) — guards against an over-eager fix that trips
    on the legitimate no-halts state."""
    s, sig_path, halt_path = strategy
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")
    _write_fresh_heartbeat(sig_path)
    halt_path.write_text("[]")

    s._refresh_state(_NOW)

    assert s._safe_stop_active is False, (
        "an empty halt mirror is the legitimate no-halts state and must keep trading; "
        "the fail-closed-on-corrupt fix must not over-trip on []"
    )
    assert s._safe_stop_reason == ""


def test_absent_halt_mirror_keeps_trading(strategy):
    """Control: an ABSENT halt mirror (cold start, no halts ever written) means
    'no active halts' and must NOT safe-stop — matching the ops-script
    not-exists branch (absent -> []). Distinguishes absent (benign) from corrupt
    (fail-closed)."""
    s, sig_path, halt_path = strategy
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")
    _write_fresh_heartbeat(sig_path)
    # halt_path intentionally not created.
    assert not halt_path.exists()

    s._refresh_state(_NOW)

    assert s._safe_stop_active is False
    assert s._safe_stop_reason == ""
