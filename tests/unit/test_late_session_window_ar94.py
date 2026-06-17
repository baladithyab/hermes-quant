"""ar94 — is_late_session_equity must not crash for a non-default minutes_before_close.

`is_late_session_equity` had a DEAD-but-EXECUTED line:

    et_late_window_start = et_close.replace(hour=15, minute=60 - minutes_before_close)
    ...
    et_late_window_start = et_close - timedelta(minutes=minutes_before_close)  # the real one

The second assignment overwrites the first, so line 1 is dead — but it still RUNS, and
`datetime.replace(minute=60 - minutes_before_close)` raises `ValueError: minute must be
in 0..59` whenever `minutes_before_close > 60` (negative minute) or `< 0` (>59). It also
hardcodes `hour=15`, so it could never express a window wider than 60 min anyway. The
default (30 -> minute=30) is safe, so current callers (which all pass the default) don't
trip it — a LATENT crash on the LIVE deterministic-equity + paper slippage paths the
moment anyone configures a wider auction window.

FIX (ar94): delete the dead/buggy line; the timedelta computation already below it is
correct for all minutes_before_close. Verifies the window math stays correct for the
default AND a wide (>60) window.
"""

from __future__ import annotations

from hermes_quant.react.slippage_model import is_late_session_equity

# 2026-05-13 is a Wednesday (a normal trading day). ET is EDT (UTC-4) in May.
# 16:00 ET == 20:00 UTC. 15:45 ET == 19:45 UTC. 14:30 ET == 18:30 UTC.


def test_ar94_default_window_unchanged():
    """Byte-identity: the default 30-min window classifies 15:45 ET as late-session
    and 15:15 ET (before 15:30) as NOT — unchanged behavior."""
    assert is_late_session_equity("2026-05-13T19:45:00Z") is True  # 15:45 ET, in [15:30,16:00]
    assert is_late_session_equity("2026-05-13T19:15:00Z") is False  # 15:15 ET, before 15:30


def test_ar94_wide_window_does_not_crash():
    """THE BUG: a >60-min window must NOT raise ValueError (dead line 302 did
    `replace(minute=60-90=-30)`). A 90-min window starts at 14:30 ET."""
    # 14:45 ET == 18:45 UTC: inside a 90-min window [14:30, 16:00], outside the 30-min one.
    assert is_late_session_equity("2026-05-13T18:45:00Z", minutes_before_close=90) is True
    # 14:15 ET == 18:15 UTC: before the 90-min window start (14:30) -> False.
    assert is_late_session_equity("2026-05-13T18:15:00Z", minutes_before_close=90) is False


def test_ar94_exactly_60_min_boundary():
    """A 60-min window (the edge the buggy `60 - mbc` line straddled: minute=0) must
    work: window start 15:00 ET."""
    assert is_late_session_equity("2026-05-13T19:30:00Z", minutes_before_close=60) is True  # 15:30 ET
    assert is_late_session_equity("2026-05-13T18:45:00Z", minutes_before_close=60) is False  # 14:45 ET, before 15:00


def test_ar94_outside_window_still_false():
    """Non-vacuity: a pre-open and a post-close time are still False for any window."""
    assert is_late_session_equity("2026-05-13T13:00:00Z", minutes_before_close=90) is False  # 09:00 ET pre-open
    assert is_late_session_equity("2026-05-13T21:00:00Z", minutes_before_close=90) is False  # 17:00 ET post-close
