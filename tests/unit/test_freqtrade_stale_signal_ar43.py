"""ar43 — freqtrade stale-signal guard must FAIL CLOSED on an unorderable asof.

Found by the parallel find->fix workflow (wf_77fb9392). _latest_signal_for's stale guard
did `pd.Timestamp(sig["asof"])` then `age_minutes > max`. pd.Timestamp returns NaT (WITHOUT
raising) for None / "" / nan / JSON-null, so age_minutes is NaN and `nan > max` is False —
the stale guard did NOT trip and an unknowable-age signal off the externally-writable
signals.jsonl bus was RETURNED, driving an entry + sizing (fail-OPEN, contradicting the
module's own "stale signals are ignored" contract). Fix funnels the asof through the
_parse_asof_utc helper (NaT/garbage -> None) -> fail-closed silence.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch):
    s = HermesQuantConsumer({})
    monkeypatch.setattr(s, "EXECUTION_BUS_PATH", tmp_path / "executions.jsonl")
    monkeypatch.setattr(s, "SIGNAL_BUS_PATH", tmp_path / "signals.jsonl")
    monkeypatch.setattr(s, "HALT_STATE_MIRROR", tmp_path / "halt_state.json")
    return s


def _signal_record(asof_present: bool, asof_value):
    rec = {
        "schema_version": 1,
        "type": "signal",
        "asset": "ETH/USDT",
        "direction": 1,
        "target_position_pct": 0.25,
        "id": "sig-corrupt-1",
    }
    if asof_present:
        rec["asof"] = asof_value
    return rec


@pytest.mark.parametrize(
    "asof_present,asof_value,label",
    [
        (True, None, "json-null"),
        (True, "", "empty-string"),
        (True, float("nan"), "nan"),
        (False, None, "missing-key"),
    ],
)
def test_ar43_stale_guard_fails_closed_on_unorderable_asof(
    strategy, monkeypatch, asof_present, asof_value, label
):
    s = strategy
    rec = _signal_record(asof_present, asof_value)
    s._signal_cache["ETH/USDT"] = rec
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    s._last_heartbeat = now
    monkeypatch.setattr(s, "_refresh_state", lambda _ct: None)

    result = s._latest_signal_for("ETH/USDT", now)
    assert result is None, (
        f"ar43: stale guard failed OPEN for asof={label}: an unorderable asof must NOT "
        f"be returned (it would drive an entry/sizing off a stale/corrupt bus record)"
    )


def test_ar43_stale_guard_still_returns_fresh_signal(strategy, monkeypatch):
    s = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    s._signal_cache["ETH/USDT"] = _signal_record(True, now.isoformat())
    s._last_heartbeat = now
    monkeypatch.setattr(s, "_refresh_state", lambda _ct: None)
    result = s._latest_signal_for("ETH/USDT", now)
    assert result is not None and result["id"] == "sig-corrupt-1"


def test_ar43_stale_guard_rejects_old_signal(strategy, monkeypatch):
    s = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    old = now - pd.Timedelta(minutes=120)  # > max_signal_age_minutes (30)
    s._signal_cache["ETH/USDT"] = _signal_record(True, old.isoformat())
    s._last_heartbeat = now
    monkeypatch.setattr(s, "_refresh_state", lambda _ct: None)
    assert s._latest_signal_for("ETH/USDT", now) is None
