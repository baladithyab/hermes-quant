"""ADR-0084 builder-level proof: HERMES_QUANT_CALENDAR_ENABLED gates frame.event_risk.

Flag-OFF => frame.event_risk is None AND the projected ctx.extras carries no
event_risk key (byte-identical default path). Flag-ON => the builder stamps an
asof-honest, outcome-free event_risk via the calendar_market_extras seam.

Offline/deterministic: a RecordingProvider serves in-memory bars; the calendar
seam is exercised via its injectable loader (monkeypatched onto the wiring module
default), so NO network and NO seed-file dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from hermes_quant.catalyst.calendar import CalendarEvent
from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.builder import build_perception_frame

DECISION_ASOF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

_VISIBLE_FOMC = CalendarEvent(
    kind="fomc",
    scheduled_for=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
    announced_at=datetime(2026, 1, 2, 14, 0, tzinfo=UTC),
    market="US",
    impact="high",
    title="FOMC rate decision",
    source="seed",
)


def _make_bars(n: int = 60, *, seed: int = 11):
    rng = np.random.default_rng(seed=seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * 0.3 + rng.normal(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.3,
            "low": closes - 0.3,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _RecordingProvider:
    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _frame(bars):
    return build_perception_frame(
        "SPY",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(bars["timestamp"].iloc[-1]),
        lookback_bars=200,
        decision_asof=DECISION_ASOF,
    )


def test_builder_flag_off_event_risk_none_and_adapter_silent(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_CALENDAR_ENABLED", raising=False)
    bars = _make_bars()
    frame = _frame(bars)
    assert frame is not None
    assert frame.event_risk is None
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert "event_risk" not in ctx.extras


def test_builder_flag_on_stamps_event_risk(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    # Inject the seam's default loader so the builder picks up an in-memory event.
    import hermes_quant.catalyst.wiring as wiring_mod

    monkeypatch.setattr(wiring_mod, "_load_seed_events", lambda timeout=None: [_VISIBLE_FOMC])

    bars = _make_bars()
    frame = _frame(bars)
    assert frame is not None
    assert frame.event_risk is not None
    events = frame.event_risk["events"]
    assert [e["kind"] for e in events] == ["fomc"]
    assert events[0]["scheduled_for"] == _VISIBLE_FOMC.scheduled_for.isoformat()
    assert "outcome" not in events[0]  # outcome-free

    # The adapter projects it onto ctx.extras (analyst READ surface).
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert ctx.extras["event_risk"] == frame.event_risk
