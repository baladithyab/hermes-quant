"""Unit tests for HermesQuantConsumer dead-man-switch heartbeat parsing.

The strategy reads heartbeat records off SIGNAL_BUS_PATH and computes the
dead-man-switch itself (per synthesis-v2 §P0-C): if the most-recent heartbeat
is older than ``dead_man_switch_seconds`` — or none has been observed within
``bootstrap_grace_seconds`` — it enters safe-stop and refuses to keep trading.

DEFECT (the case these tests pin): the heartbeat-parse loop parsed asof via the
RAW ``pd.Timestamp(r["asof"])`` under ``except (ValueError, KeyError)``. But
``pd.Timestamp(None)`` / ``pd.Timestamp('')`` / ``pd.Timestamp(nan)`` all return
``NaT`` and raise NEITHER ValueError NOR KeyError — so the except never fired.
A heartbeat record whose asof is null/empty (a garbage-producing daemon) then
poisoned ``_last_heartbeat`` with ``NaT``:

  * ``_last_heartbeat`` starts as Python ``None`` -> the first heartbeat record
    seen (scanning newest-first) with asof=None sets ``_last_heartbeat = NaT``
    and ``break``s, SHADOWING any older VALID heartbeat in the list.
  * Then ``age = (now - NaT).total_seconds()`` is ``nan`` and
    ``nan > dead_man_switch_seconds`` is ``False`` — so ``_enter_safe_stop`` is
    NEVER called. The dead-man-switch is silently disabled and the strategy
    keeps trading on cached signals while the heartbeat stream is producing
    garbage. FAIL-OPEN on a money-software safety rail.

These tests feed a heartbeat record with a null/empty asof and assert the
strategy still enters safe-stop (a null heartbeat must behave identically to
"no valid heartbeat observed"). They are hermetic: SIGNAL_BUS_PATH is redirected
to a tmp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch):
    """A HermesQuantConsumer whose bus paths point at a hermetic tmp dir."""
    s = HermesQuantConsumer({})
    sig_path = tmp_path / "signals.jsonl"
    exec_path = tmp_path / "executions.jsonl"
    halt_path = tmp_path / "halt_state.json"
    monkeypatch.setattr(s, "SIGNAL_BUS_PATH", sig_path)
    monkeypatch.setattr(s, "EXECUTION_BUS_PATH", exec_path)
    monkeypatch.setattr(s, "HALT_STATE_MIRROR", halt_path)
    return s, sig_path


def _write_bus(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_null_asof_heartbeat_shadows_valid_stale_heartbeat_still_safe_stops(strategy):
    """Case A: newest heartbeat has asof=None, shadowing an OLDER valid (but stale)
    heartbeat. With the raw-parse bug, _last_heartbeat=NaT, age=nan, nan>60=False,
    and safe-stop NEVER fires. The valid older heartbeat is itself stale (age > 60s),
    so the dead-man-switch MUST fire reason='heartbeat_stale'."""
    s, sig_path = strategy
    # An older VALID heartbeat at T0, then a newest GARBAGE heartbeat (asof=None).
    # "now" is 600s past T0 — well beyond dead_man_switch_seconds (60s).
    _write_bus(
        sig_path,
        [
            {"type": "heartbeat", "schema_version": 1, "asof": "2026-06-16T10:00:00+00:00"},
            {"type": "heartbeat", "schema_version": 1, "asof": None},  # newest, garbage
        ],
    )
    now = pd.Timestamp("2026-06-16T10:10:00+00:00")  # +600s -> stale
    # Start time well in the past so bootstrap grace has long elapsed.
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")

    s._refresh_state(now)

    assert s._safe_stop_active is True, (
        "dead-man-switch failed OPEN: a null-asof heartbeat poisoned _last_heartbeat "
        "with NaT so the stale-heartbeat check never fired"
    )
    assert s._safe_stop_reason == "heartbeat_stale"


def test_empty_string_asof_heartbeat_still_safe_stops(strategy):
    """Same as Case A but asof='' (empty string). pd.Timestamp('') is also NaT."""
    s, sig_path = strategy
    _write_bus(
        sig_path,
        [
            {"type": "heartbeat", "schema_version": 1, "asof": "2026-06-16T10:00:00+00:00"},
            {"type": "heartbeat", "schema_version": 1, "asof": ""},  # newest, garbage
        ],
    )
    now = pd.Timestamp("2026-06-16T10:10:00+00:00")
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")

    s._refresh_state(now)

    assert s._safe_stop_active is True
    assert s._safe_stop_reason == "heartbeat_stale"


def test_only_null_asof_heartbeat_after_bootstrap_safe_stops(strategy):
    """No valid heartbeat at all — the only heartbeat record has asof=None. After the
    bootstrap grace window elapses this must be treated as 'no heartbeat observed' and
    safe-stop with reason='no_heartbeat_observed_after_bootstrap', NOT fail-open."""
    s, sig_path = strategy
    _write_bus(
        sig_path,
        [
            {"type": "heartbeat", "schema_version": 1, "asof": None},
        ],
    )
    now = pd.Timestamp("2026-06-16T10:10:00+00:00")
    # Start 200s before now: bootstrap grace (120s) has elapsed.
    s._strategy_start_time = now - pd.Timedelta(seconds=200)

    s._refresh_state(now)

    assert s._safe_stop_active is True
    assert s._safe_stop_reason == "no_heartbeat_observed_after_bootstrap"


def test_fresh_valid_heartbeat_does_not_safe_stop(strategy):
    """Control: a single fresh valid heartbeat (age < dead_man_switch) keeps trading.
    Guards against an over-eager fix that always safe-stops."""
    s, sig_path = strategy
    _write_bus(
        sig_path,
        [
            {"type": "heartbeat", "schema_version": 1, "asof": "2026-06-16T10:09:30+00:00"},
        ],
    )
    now = pd.Timestamp("2026-06-16T10:10:00+00:00")  # +30s -> fresh
    s._strategy_start_time = pd.Timestamp("2026-06-16T09:00:00+00:00")

    s._refresh_state(now)

    assert s._safe_stop_active is False
    assert s._safe_stop_reason == ""
