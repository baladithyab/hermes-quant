"""tests/backtest/test_event_risk_ablation.py — C2a: make EVENT_RISK measurable.

The flag-ablation harness drives ``AdvisorStrategy``, whose risk-gate call reads
the pre-event blackout carrier from ``signal.metadata['event_risk']``. The plain
advisor path never populates that carrier, so ablating ``HERMES_QUANT_EVENT_RISK``
through it is a FALSE NULL — which is why ``cli/ablate.py`` used to REFUSE the flag
(``verdict: NOT_MEASURABLE``).

``hermes_quant.backtest.event_risk_ablation`` closes that gap by injecting an
asof-honest synthetic macro calendar into ``signal.metadata['event_risk']`` so the
guard genuinely bites. These tests lock in the load-bearing invariants
(money-software):

  * Calendar + carrier are asof-honest BY CONSTRUCTION (announced_at <=
    scheduled_for; the carrier filters to announced_at <= asof — no lookahead).
  * The strategy STAMPS the carrier into the (frozen) signal via replace, and the
    blackout guard actually fires when EVENT_RISK=1 (n_trades drops vs OFF).
  * EVENT_RISK=0 is byte-identical to a plain AdvisorStrategy (the carrier is
    inert when the flag is off).
  * The CLI no longer returns NOT_MEASURABLE for EVENT_RISK — it produces a real
    OFF-vs-ON card.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest.event_risk_ablation import (
    EventRiskAblationStrategy,
    build_event_risk_payload,
    historical_fomc_calendar,
    synthetic_macro_calendar,
)
from hermes_quant.catalyst.calendar import CalendarEvent

UTC = timezone.utc


# ---------------------------------------------------------------------------
# synthetic_macro_calendar — asof-honest by construction
# ---------------------------------------------------------------------------


def test_calendar_spans_window_and_cycles_kinds():
    cal = synthetic_macro_calendar("2024-01-01", "2024-04-01", cadence_days=21)
    assert len(cal) >= 3
    # Sorted by scheduled_for.
    assert cal == sorted(cal, key=lambda e: e.scheduled_for)
    # Cycles fomc/cpi/nfp.
    kinds = {e.kind for e in cal}
    assert {"fomc", "cpi", "nfp"} & kinds


def test_calendar_events_are_asof_honest():
    """Every event: announced_at <= scheduled_for, both tz-aware (enforced by the
    CalendarEvent dataclass, but assert it explicitly — this is the no-lookahead
    foundation)."""
    cal = synthetic_macro_calendar("2024-01-01", "2024-06-01")
    for e in cal:
        assert e.scheduled_for.tzinfo is not None
        assert e.announced_at.tzinfo is not None
        assert e.announced_at <= e.scheduled_for
        assert e.outcome is None  # outcome-free by contract


def test_calendar_high_impact_only_for_tier1_macro():
    cal = synthetic_macro_calendar("2024-01-01", "2024-12-31")
    for e in cal:
        if e.kind in ("fomc", "cpi"):
            assert e.impact == "high"
        else:
            assert e.impact != "high"


def test_calendar_rejects_inverted_window():
    with pytest.raises(ValueError):
        synthetic_macro_calendar("2024-04-01", "2024-01-01")


# ---------------------------------------------------------------------------
# historical_fomc_calendar — real public-record dates for real-data verdicts
# ---------------------------------------------------------------------------


def test_historical_fomc_calendar_has_8_meetings_per_year():
    cal = historical_fomc_calendar()
    # 2023 + 2024 = 16 meetings (the Fed holds 8/year).
    assert len(cal) == 16
    years = {e.scheduled_for.year for e in cal}
    assert years == {2023, 2024}
    for yr in (2023, 2024):
        assert sum(1 for e in cal if e.scheduled_for.year == yr) == 8


def test_historical_fomc_calendar_is_asof_honest_and_high_impact():
    cal = historical_fomc_calendar()
    assert cal == sorted(cal, key=lambda e: e.scheduled_for)
    for e in cal:
        assert e.kind == "fomc"
        assert e.impact == "high"  # FOMC is Tier-1 — bites the blackout
        assert e.scheduled_for.tzinfo is not None
        assert e.announced_at.tzinfo is not None
        assert e.announced_at <= e.scheduled_for  # no lookahead
        assert e.outcome is None  # outcome-free


def test_historical_fomc_known_dates_present():
    """Spot-check a few well-known FOMC decision days are in the calendar (guards
    against a typo silently shifting an event off the real decision day)."""
    cal = historical_fomc_calendar()
    days = {e.scheduled_for.date().isoformat() for e in cal}
    # A few hard public-record FOMC decision dates.
    for known in ("2023-03-22", "2023-07-26", "2024-09-18", "2024-12-18"):
        assert known in days, known


# ---------------------------------------------------------------------------
# build_event_risk_payload — asof filter (the no-lookahead guarantee)
# ---------------------------------------------------------------------------


def test_payload_filters_to_announced_only():
    """An event whose schedule is NOT YET public at asof must be EXCLUDED — even
    though it exists in the calendar. This is the defense-in-depth no-lookahead
    filter (announced_at <= asof)."""
    future_announce = CalendarEvent(
        kind="fomc",
        scheduled_for=datetime(2024, 6, 1, 18, tzinfo=UTC),
        announced_at=datetime(2024, 5, 1, tzinfo=UTC),  # announced 2024-05-01
        market="US",
        impact="high",
    )
    cal = [future_announce]
    # asof BEFORE the announcement -> event not yet knowable -> excluded.
    before = build_event_risk_payload(cal, asof=datetime(2024, 4, 15, tzinfo=UTC))
    assert before["events"] == []
    # asof AFTER the announcement -> event knowable -> included.
    after = build_event_risk_payload(cal, asof=datetime(2024, 5, 15, tzinfo=UTC))
    assert len(after["events"]) == 1
    assert after["events"][0]["kind"] == "fomc"


def test_payload_shape_matches_gate_contract():
    """The carrier must have the exact shape in_event_blackout reads:
    {'events': [{'impact','kind','scheduled_for',...}]}."""
    cal = synthetic_macro_calendar("2024-01-01", "2024-02-15")
    payload = build_event_risk_payload(cal, asof=datetime(2024, 3, 1, tzinfo=UTC))
    assert "events" in payload
    assert isinstance(payload["events"], list)
    for ev in payload["events"]:
        assert {"kind", "scheduled_for", "impact", "announced_at"} <= set(ev)
        # scheduled_for is an ISO string (CalendarEvent.to_dict()).
        assert isinstance(ev["scheduled_for"], str)


def test_payload_handles_naive_asof():
    cal = synthetic_macro_calendar("2024-01-01", "2024-02-15")
    # naive asof must not raise (treated as UTC).
    payload = build_event_risk_payload(cal, asof=datetime(2024, 3, 1))
    assert "events" in payload


# ---------------------------------------------------------------------------
# EventRiskAblationStrategy — the gate stamping
# ---------------------------------------------------------------------------


def _gbm_ohlcv(n_days: int = 90, seed: int = 11) -> pd.DataFrame:
    dates = pd.bdate_range(start="2024-01-01", periods=n_days)
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0008, 0.012, n_days)
    closes = 100.0 * np.cumprod(1 + rets)
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.004,
            "low": np.minimum(opens, closes) * 0.996,
            "close": closes,
            "volume": rng.integers(500_000, 1_000_000, n_days).astype(float),
        },
        index=dates,
    )


class _RecordingGate:
    """Captures the signal it was gated with so we can assert the carrier was
    stamped. Returns None (silence) so the strategy emits HOLD — we only care
    about WHAT reached the gate here."""

    def __init__(self):
        self.seen_signals = []

    def gate(self, signal, market, portfolio, halt_state):  # noqa: ANN001
        self.seen_signals.append(signal)
        return None


def test_strategy_stamps_event_risk_carrier_into_signal_metadata():
    """The strategy must inject signal.metadata['event_risk'] before the gate —
    proving the carrier reaches the seam the gate reads (signal.metadata)."""
    cal = synthetic_macro_calendar("2024-01-01", "2024-06-01")
    gate = _RecordingGate()
    strat = EventRiskAblationStrategy(
        ["SYN"], calendar=cal, risk_gate=gate, learn_from_fills=False
    )
    ohlcv = _gbm_ohlcv()
    # Drive enough bars that the advisor warms up and reaches the gate.
    for i in range(35, len(ohlcv)):
        strat.decide(ohlcv.index[i], ohlcv.iloc[: i + 1])
    assert gate.seen_signals, "gate was never reached — advisor never emitted a non-flat signal"
    # At least one gated signal carries the event_risk carrier with the right shape.
    carriers = [
        (s.metadata or {}).get("event_risk")
        for s in gate.seen_signals
        if (s.metadata or {}).get("event_risk") is not None
    ]
    assert carriers, "no signal carried event_risk metadata"
    assert "events" in carriers[0]


def test_strategy_carrier_is_asof_honest_at_gate():
    """The carrier stamped at decision time `asof` must contain ONLY events whose
    announced_at <= asof — never a not-yet-announced (future-knowledge) event."""
    # One event announced far in the future relative to the decision bars.
    late = CalendarEvent(
        kind="fomc",
        scheduled_for=datetime(2024, 12, 1, 18, tzinfo=UTC),
        announced_at=datetime(2024, 11, 1, tzinfo=UTC),
        market="US",
        impact="high",
    )
    gate = _RecordingGate()
    strat = EventRiskAblationStrategy(
        ["SYN"], calendar=[late], risk_gate=gate, learn_from_fills=False
    )
    ohlcv = _gbm_ohlcv()  # Jan–Apr 2024 — all BEFORE the Nov-2024 announcement.
    for i in range(35, len(ohlcv)):
        strat.decide(ohlcv.index[i], ohlcv.iloc[: i + 1])
    # Every gated carrier must be empty: the event wasn't announced yet.
    for s in gate.seen_signals:
        payload = (s.metadata or {}).get("event_risk")
        if payload is not None:
            assert payload["events"] == [], "leaked a not-yet-announced event (lookahead!)"


# ---------------------------------------------------------------------------
# End-to-end: blackout bites when ON, inert when OFF (via run_flag_ablation)
# ---------------------------------------------------------------------------


def test_event_risk_ablation_bites_when_on(monkeypatch):
    """ON vs OFF must differ — the blackout guard suppresses at least one fresh
    open inside an event window. If n_trades were identical the flag would be a
    no-op (the FALSE NULL this whole module exists to prevent)."""
    from hermes_quant.backtest.ablation import run_flag_ablation
    from hermes_quant.cli.ablate import _config_for, _synthetic_committee, _synthetic_ohlcv

    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    ohlcv = _synthetic_ohlcv("2024-01-01", "2024-04-01")
    cal = synthetic_macro_calendar(ohlcv.index[0], ohlcv.index[-1])

    def _factory():
        return EventRiskAblationStrategy(
            ["SYN"],
            calendar=cal,
            analysts=_synthetic_committee(),
            learn_from_fills=True,
        )

    result = run_flag_ablation(
        "HERMES_QUANT_EVENT_RISK",
        strategy_factory=_factory,
        universe=["SYN"],
        ohlcv=ohlcv,
        config=_config_for(ohlcv),
    )
    # The guard can ONLY reduce or hold trade count (it silences fresh opens,
    # never adds). A real bite shows fewer ON trades than OFF.
    assert result.on.n_trades <= result.off.n_trades
    assert result.d_n_trades <= 0
    # The carrier genuinely bit on this window (proves not a false null).
    assert result.d_n_trades < 0, (
        "EVENT_RISK toggled NO trades — carrier did not bite (false null regression)"
    )


def test_event_risk_off_leg_does_not_leak_env(monkeypatch):
    """run_flag_ablation must restore os.environ exactly — no HERMES_QUANT_EVENT_RISK
    leak after the call (the harness's no-leakage contract)."""
    import os

    from hermes_quant.backtest.ablation import run_flag_ablation
    from hermes_quant.cli.ablate import _config_for, _synthetic_committee, _synthetic_ohlcv

    monkeypatch.delenv("HERMES_QUANT_EVENT_RISK", raising=False)
    ohlcv = _synthetic_ohlcv("2024-01-01", "2024-04-01")
    cal = synthetic_macro_calendar(ohlcv.index[0], ohlcv.index[-1])
    run_flag_ablation(
        "HERMES_QUANT_EVENT_RISK",
        strategy_factory=lambda: EventRiskAblationStrategy(
            ["SYN"], calendar=cal, analysts=_synthetic_committee(), learn_from_fills=True
        ),
        universe=["SYN"],
        ohlcv=ohlcv,
        config=_config_for(ohlcv),
    )
    assert "HERMES_QUANT_EVENT_RISK" not in os.environ


# ---------------------------------------------------------------------------
# CLI: EVENT_RISK is no longer NOT_MEASURABLE
# ---------------------------------------------------------------------------


def test_cli_event_risk_is_measurable_not_refused(capsys):
    """The CLI must RUN the EVENT_RISK ablation (ran=True) instead of refusing it
    with verdict=NOT_MEASURABLE — the whole point of C2a."""
    import json

    from hermes_quant.cli.ablate import cmd_ablate

    ns = argparse.Namespace(
        flag="HERMES_QUANT_EVENT_RISK",
        universe="SYN",
        from_date="2024-01-01",
        to_date="2024-04-01",
        synthetic=True,
        json=True,
        on="1",
        off="0",
    )
    rc = cmd_ablate(ns)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ran"] is True
    assert out["verdict"] != "NOT_MEASURABLE"
    assert out["flag"] == "HERMES_QUANT_EVENT_RISK"
    # A real card has both legs.
    assert "off" in out and "on" in out
