"""Tests for the ADR-0084 event_risk seam: PerceptionFrame.event_risk +
catalyst.wiring.calendar_market_extras + the adapter projection.

Fully offline / deterministic (injected events_loader, in-memory CalendarEvents,
monkeypatched flag — NO network, NO seed-file dependency). Covers the three
acceptance criteria the ADR-0084 plan requires:

  1. flag-OFF byte-identical — flag absent/'0' => seam returns None, adapter writes
     NO event_risk key, the default extras key-set is preserved.
  2. flag-ON no-lookahead — a FUTURE-announced event (announced_at > decision_asof)
     is EXCLUDED (the consumer cannot know it EXISTS yet).
  3. flag-ON asof-visible — an event whose schedule was already public populates
     the field with scheduled_for/kind/impact but NEVER an outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from hermes_quant.catalyst.calendar import CalendarEvent
from hermes_quant.catalyst.wiring import calendar_market_extras
from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.frame import PerceptionFrame

# A decision instant. Events are announced relative to this to exercise the asof gate.
DECISION_ASOF = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

# An event ALREADY public at DECISION_ASOF (announced in Jan, scheduled in June).
_VISIBLE_FOMC = CalendarEvent(
    kind="fomc",
    scheduled_for=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
    announced_at=datetime(2026, 1, 2, 14, 0, tzinfo=UTC),  # <= DECISION_ASOF
    market="US",
    impact="high",
    title="FOMC rate decision",
    source="seed",
)

# An event whose SCHEDULE was not yet public at DECISION_ASOF (announced AFTER it).
# A consumer cannot even know it EXISTS -> must be EXCLUDED (no lookahead).
_FUTURE_ANNOUNCED_CPI = CalendarEvent(
    kind="cpi",
    scheduled_for=datetime(2026, 7, 10, 12, 30, tzinfo=UTC),
    announced_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),  # > DECISION_ASOF
    market="US",
    impact="high",
    title="CPI release",
    source="seed",
)

# A single-name earnings event (scoped to its own symbol only).
_VISIBLE_AAPL_EARNINGS = CalendarEvent(
    kind="earnings",
    scheduled_for=datetime(2026, 4, 30, 20, 0, tzinfo=UTC),
    announced_at=datetime(2026, 2, 1, 13, 30, tzinfo=UTC),  # <= DECISION_ASOF
    symbol="AAPL",
    impact="high",
    title="AAPL Q2 earnings",
    source="seed",
)


def _loader_for(events):
    """An injectable events_loader(timeout) -> list[CalendarEvent] for offline tests."""
    def _loader(timeout=None):
        return list(events)
    return _loader


# ---------------------------------------------------------------------------
# 1. flag-OFF byte-identical: the seam is silent and the adapter writes nothing.
# ---------------------------------------------------------------------------


def test_seam_off_returns_none(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_CALENDAR_ENABLED", raising=False)
    out = calendar_market_extras(
        "SPY",
        decision_asof=DECISION_ASOF,
        events_loader=_loader_for([_VISIBLE_FOMC]),
    )
    assert out is None


def test_seam_explicit_zero_is_off(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "0")
    out = calendar_market_extras(
        "SPY",
        decision_asof=DECISION_ASOF,
        events_loader=_loader_for([_VISIBLE_FOMC]),
    )
    assert out is None


def _make_bars(n: int = 40, *, seed: int = 7):
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


def test_adapter_omits_event_risk_when_frame_field_none():
    """ADR-0084 rail: event_risk None (the OFF default) => adapter stamps NOTHING
    => default extras key-set preserved (byte-identical flag-OFF projection)."""
    bars = _make_bars()
    frame = PerceptionFrame(
        symbol="SPY",
        asof=pd.Timestamp(bars["timestamp"].iloc[-1]),
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        event_risk=None,
    )
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert "event_risk" not in ctx.extras
    # The default key-set is unchanged (only the regime triad).
    assert set(ctx.extras.keys()) == {"regime", "regime_failure", "regime_classifier_kind"}


def test_frame_default_event_risk_is_none():
    """Add-only field: default None so existing constructors are unaffected."""
    bars = _make_bars(3)
    frame = PerceptionFrame(
        symbol="SPY",
        asof=pd.Timestamp(bars["timestamp"].iloc[-1]),
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
    )
    assert frame.event_risk is None


# ---------------------------------------------------------------------------
# 2. flag-ON no-lookahead: a future-announced event is EXCLUDED.
# ---------------------------------------------------------------------------


def test_seam_on_excludes_future_announced_event(monkeypatch):
    """ADR-0084 D-2: an event whose schedule became public AFTER decision_asof is
    not yet known to exist -> it must NOT appear in event_risk (no lookahead)."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    out = calendar_market_extras(
        "SPY",
        decision_asof=DECISION_ASOF,
        events_loader=_loader_for([_VISIBLE_FOMC, _FUTURE_ANNOUNCED_CPI]),
    )
    assert out is not None
    kinds = [e["kind"] for e in out["event_risk"]["events"]]
    assert "fomc" in kinds  # already public -> visible
    assert "cpi" not in kinds  # announced after asof -> EXCLUDED (no lookahead)


def test_seam_on_all_future_announced_yields_none(monkeypatch):
    """If EVERY event is future-announced, the seam is silent (returns None)."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    out = calendar_market_extras(
        "SPY",
        decision_asof=DECISION_ASOF,
        events_loader=_loader_for([_FUTURE_ANNOUNCED_CPI]),
    )
    assert out is None


# ---------------------------------------------------------------------------
# 3. flag-ON asof-visible: field populated with scheduled_for, NEVER an outcome.
# ---------------------------------------------------------------------------


def test_seam_on_populates_visible_event_outcome_free(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    out = calendar_market_extras(
        "SPY",
        decision_asof=DECISION_ASOF,
        events_loader=_loader_for([_VISIBLE_FOMC]),
    )
    assert out is not None
    er = out["event_risk"]
    assert er["decision_asof"] == DECISION_ASOF.isoformat()
    assert len(er["events"]) == 1
    ev = er["events"][0]
    assert ev["kind"] == "fomc"
    assert ev["impact"] == "high"
    # scheduled_for is exposed (the forward payload the committee can weigh)...
    assert ev["scheduled_for"] == _VISIBLE_FOMC.scheduled_for.isoformat()
    # ...but the OUTCOME is NEVER present (the calendar is outcome-free by contract).
    assert "outcome" not in ev
    # announced_at is NOT leaked into the read surface (only the asof-honest payload).
    assert "announced_at" not in ev


def test_seam_on_macro_applies_to_any_symbol(monkeypatch):
    """A macro event (symbol=None) is visible for every symbol."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    for sym in ("SPY", "TSLA", "QQQ"):
        out = calendar_market_extras(
            sym, decision_asof=DECISION_ASOF, events_loader=_loader_for([_VISIBLE_FOMC])
        )
        assert out is not None
        assert [e["kind"] for e in out["event_risk"]["events"]] == ["fomc"]


def test_seam_on_single_name_scoped_to_its_symbol(monkeypatch):
    """A single-name earnings event appears ONLY for its own symbol."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    events = [_VISIBLE_FOMC, _VISIBLE_AAPL_EARNINGS]
    # AAPL sees BOTH the macro FOMC and its own earnings.
    aapl = calendar_market_extras("AAPL", decision_asof=DECISION_ASOF, events_loader=_loader_for(events))
    assert {e["kind"] for e in aapl["event_risk"]["events"]} == {"fomc", "earnings"}
    # MSFT sees ONLY the macro FOMC (not AAPL's earnings).
    msft = calendar_market_extras("MSFT", decision_asof=DECISION_ASOF, events_loader=_loader_for(events))
    assert {e["kind"] for e in msft["event_risk"]["events"]} == {"fomc"}


def test_seam_on_emitted_events_sorted_deterministic(monkeypatch):
    """Events are sorted by (scheduled_for, kind) so the read surface is stable
    regardless of loader/seed ordering."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    events = [_VISIBLE_FOMC, _VISIBLE_AAPL_EARNINGS]  # FOMC later, earnings earlier
    out = calendar_market_extras("AAPL", decision_asof=DECISION_ASOF, events_loader=_loader_for(events))
    sched = [e["scheduled_for"] for e in out["event_risk"]["events"]]
    assert sched == sorted(sched)  # earnings (Apr) before FOMC (Jun)


# ---------------------------------------------------------------------------
# silence-by-default: a loader that raises must not propagate (returns None).
# ---------------------------------------------------------------------------


def test_seam_on_loader_failure_is_silent(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")

    def boom(timeout=None):
        raise RuntimeError("seed missing")

    out = calendar_market_extras("SPY", decision_asof=DECISION_ASOF, events_loader=boom)
    assert out is None  # never raises


def test_seam_default_loader_delegates_to_fomc_seed(monkeypatch):
    """With no events_loader injected, the default loader delegates to the canonical
    calendar.load_fomc_seed() (single source of truth — no schema drift). It must
    never raise and never leak an outcome. If the vendored seed is present, macro
    FOMC events surface for any symbol; if absent, the seam is silent (None)."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    out = calendar_market_extras("SPY", decision_asof=DECISION_ASOF)
    if out is not None:  # seed present -> outcome-free, asof-honest payload
        for ev in out["event_risk"]["events"]:
            assert "outcome" not in ev  # the calendar is outcome-free by contract
            assert "scheduled_for" in ev
            # asof-honest: nothing scheduled before it was announced leaks through;
            # the seam already filtered to announced_at <= decision_asof.


def test_seam_default_loader_uses_canonical_load_fomc_seed(monkeypatch):
    """The default loader is a thin delegate to calendar.load_fomc_seed (not a
    re-implemented parser), so the seam can never schema-drift from the seed file."""
    monkeypatch.setenv("HERMES_QUANT_CALENDAR_ENABLED", "1")
    import hermes_quant.catalyst.calendar as cal_mod

    called: dict[str, bool] = {"hit": False}
    real = cal_mod.load_fomc_seed

    def _spy(*a, **k):
        called["hit"] = True
        return real(*a, **k)

    monkeypatch.setattr(cal_mod, "load_fomc_seed", _spy)
    calendar_market_extras("SPY", decision_asof=DECISION_ASOF)
    assert called["hit"], "default loader did not delegate to calendar.load_fomc_seed"
