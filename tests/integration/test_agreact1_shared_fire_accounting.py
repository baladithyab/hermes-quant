"""agreact1: the shared post-execution accounting tail (_apply_fire_accounting +
_reactor_record_is_nofill) that the equity (_react) AND options (_originate_mleg_proposal)
paths both call — killing the divergent inline no-fill copy the options helper carried.

Pins: (1) the no-fill detection UNION across every reactor signal; (2) a real fill journals
once + returns (pid, realized post-clip size); (3) a None/0.0 fill is a no-fill (conservative).
"""
from __future__ import annotations

from unittest import mock

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.react.base import ExecutionRecord


def _record(**over) -> ExecutionRecord:
    base = dict(
        proposal_id="prop_x",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-18T00:00:00Z",
        asof_execution="2026-06-18T00:00:00Z",
        target_position_pct=0.05,
        decision_price=100.0,
        fill_price=100.0,
        fill_size_pct=0.05,
        reactor_name="paper",
        human_in_the_loop=False,
        reactor_metadata={},
    )
    base.update(over)
    return ExecutionRecord(**base)


class _Prop:
    proposal_id = "prop_x"
    symbol = "AAPL"


# --------------------------------------------------------------------------- #
# 1. The no-fill detection UNION (every reactor's silence signal).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("meta,fill,expect_nofill", [
    ({}, 0.05, False),                                  # a real fill
    ({"silenced": True}, 0.0, True),                    # PaperReactor cap-clip
    ({"no_fill": True}, 0.0, True),                     # DeterministicEquity no-fill
    ({"bp_rejected": True}, 0.0, True),                 # BP refusal
    ({"unfilled_timeout": True}, 0.0, True),            # broker timeout
    ({}, 0.0, True),                                    # zero fill (no capital moved)
    ({}, None, True),                                   # None fill (cannot report -> no-fill)
])
def test_reactor_record_nofill_union(meta, fill, expect_nofill):
    rec = _record(reactor_metadata=meta, fill_size_pct=fill)
    assert auto._reactor_record_is_nofill(rec) is expect_nofill


# --------------------------------------------------------------------------- #
# 2. A real fill journals ONCE + returns (pid, realized).
# --------------------------------------------------------------------------- #
def test_apply_fire_accounting_real_fill_journals_and_returns(monkeypatch):
    rec = _record(fill_size_pct=0.037)  # a post-clip realized smaller than requested
    calls = []
    monkeypatch.setattr(
        "hermes_quant.journal.writer.append_human_override",
        lambda prop, **kw: calls.append((getattr(prop, "proposal_id", None), kw.get("kind"), kw.get("reason"))),
    )
    out = auto._apply_fire_accounting(rec, _Prop(), symbol="AAPL", journal_reason="autonomous_options_fire")
    assert out is not None
    pid, realized = out
    assert pid == "prop_x"
    assert realized == pytest.approx(0.037)  # the REALIZED post-clip size (ar80)
    assert calls == [("prop_x", "approve", "autonomous_options_fire")], (
        "a real fill must journal exactly once as a non-human 'approve' override"
    )


def test_apply_fire_accounting_nofill_returns_none_and_does_not_journal(monkeypatch):
    rec = _record(reactor_metadata={"silenced": True}, fill_size_pct=0.0)
    calls = []
    monkeypatch.setattr(
        "hermes_quant.journal.writer.append_human_override",
        lambda prop, **kw: calls.append(1),
    )
    out = auto._apply_fire_accounting(rec, _Prop(), symbol="AAPL", journal_reason="x")
    assert out is None, "a no-fill must return None (caller counts no fire)"
    assert calls == [], "a no-fill must NOT write a journal entry (nothing happened)"


# --------------------------------------------------------------------------- #
# 3. _react still uses the shared tail (equity path unchanged behavior).
# --------------------------------------------------------------------------- #
def test_react_delegates_to_shared_tail(monkeypatch):
    """A silenced record routed through _react returns None via the shared guard."""
    class _Silencing:
        name = "paper"
        def execute(self, prop, **kw):
            return _record(asset=prop.symbol, reactor_metadata={"no_fill": True}, fill_size_pct=0.0)
    monkeypatch.setattr("hermes_quant.react.dispatch.select_reactor", lambda prop: _Silencing())
    from hermes_quant.watchlist import WatchlistEntry
    out = auto._react(
        {"as_of": "2026-06-18T00:00:00Z", "decision_price": 100.0},
        WatchlistEntry("AAPL", "equity", "1d"),
        0.05,
    )
    assert out is None
