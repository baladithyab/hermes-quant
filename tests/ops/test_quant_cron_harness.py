"""Tests for the quant-cron-harness cron-expression matcher + job loading.

The matcher must agree with standard cron semantics for the expr forms the live
hermes-quant jobs use (``*``, ``a,b``, ``a-b``, ``*/n``, dow ``1-5``), because the
daemon mode fires jobs based on it — a wrong matcher would fire a job at the wrong
minute (or never).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-cron-harness.py"


def _load():
    spec = importlib.util.spec_from_file_location("quant_cron_harness", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["quant_cron_harness"] = mod  # so dataclass __module__ resolves
    spec.loader.exec_module(mod)
    return mod


H = _load()


# 2026-06-17 is a Wednesday; 06-20 Sat; 06-21 Sun; 06-22 Mon.
@pytest.mark.parametrize(
    "expr,dt,expected",
    [
        # 30-min in-market tick, weekdays 06:30-13:30
        ("30 6-13 * * 1-5", datetime(2026, 6, 17, 6, 30), True),  # Wed :30
        ("30 6-13 * * 1-5", datetime(2026, 6, 17, 6, 31), False),  # wrong minute
        ("30 6-13 * * 1-5", datetime(2026, 6, 17, 14, 30), False),  # past 13
        ("30 6-13 * * 1-5", datetime(2026, 6, 20, 6, 30), False),  # Saturday
        # top + half hour
        ("0,30 6-13 * * 1-5", datetime(2026, 6, 17, 7, 0), True),
        ("0,30 6-13 * * 1-5", datetime(2026, 6, 17, 7, 30), True),
        ("0,30 6-13 * * 1-5", datetime(2026, 6, 17, 7, 15), False),
        # every 15 min, any time
        ("*/15 * * * *", datetime(2026, 6, 17, 9, 45), True),
        ("*/15 * * * *", datetime(2026, 6, 17, 9, 46), False),
        # weekly: Sunday 13:00
        ("0 13 * * 0", datetime(2026, 6, 21, 13, 0), True),
        ("0 13 * * 0", datetime(2026, 6, 17, 13, 0), False),  # Wed
        # weekly: Monday 08:00
        ("0 8 * * 1", datetime(2026, 6, 22, 8, 0), True),
        ("0 8 * * 1", datetime(2026, 6, 21, 8, 0), False),  # Sun
        # monthly: 1st at 14:00
        ("0 14 1 * *", datetime(2026, 7, 1, 14, 0), True),
        ("0 14 1 * *", datetime(2026, 7, 2, 14, 0), False),
        # daily 04:00 every day
        ("0 4 * * *", datetime(2026, 6, 20, 4, 0), True),  # even Saturday
    ],
)
def test_cron_matches(expr, dt, expected):
    assert H.cron_matches(expr, dt) is expected


def test_next_fire_finds_upcoming_weekday_tick():
    # From Wed 06-17 14:00 (past the day's last 13:30 tick), the next 30-6-13 tick
    # is Thu 06-18 06:30.
    after = datetime(2026, 6, 17, 14, 0)
    nf = H.next_fire("30 6-13 * * 1-5", after)
    assert nf == datetime(2026, 6, 18, 6, 30)


def test_next_fire_returns_none_beyond_horizon():
    # A monthly job more than the horizon away returns None (not a crash).
    after = datetime(2026, 6, 2, 0, 0)  # just after the 1st
    nf = H.next_fire("0 14 1 * *", after, horizon_minutes=60)  # 1h horizon
    assert nf is None


def test_parse_field_step_range():
    assert H._parse_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert H._parse_field("6-13", 0, 23) == {6, 7, 8, 9, 10, 11, 12, 13}
    assert H._parse_field("1-5", 0, 7) == {1, 2, 3, 4, 5}
    assert H._parse_field("0,30", 0, 59) == {0, 30}


def test_five_field_required():
    with pytest.raises(ValueError):
        H.cron_matches("0 6 * *", datetime(2026, 6, 17, 6, 0))  # 4 fields


def test_armed_wrapper_skipped_in_observe_mode():
    """An -armed.sh wrapper must NOT be invoked when armed=False (no paper fires)."""
    job = H.Job(
        id="x", name="quant-autonomous-tick-30min", expr="30 6-13 * * 1-5",
        enabled=True, script="quant-autonomous-tick-armed.sh", skill=None, prompt_head="",
    )
    argv, note = H.build_command(job, armed=False)
    assert argv is None
    assert "observe mode skips" in note


def test_prompt_driven_job_skipped():
    job = H.Job(
        id="y", name="quant-daily-eod-interim", expr="30 12 * * 1-5",
        enabled=True, script=None, skill=None, prompt_head="brief",
    )
    argv, note = H.build_command(job, armed=False)
    assert argv is None
    assert "prompt" in note.lower()
