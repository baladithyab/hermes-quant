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
    ({}, 0.0, True),                                    # zero fill (no capital moved)
    ({}, None, True),                                   # None fill (cannot report -> no-fill)
    # wave4-review FIX (was vacuous): each METADATA leg must be exercised with a NON-zero
    # fill so the realized==0.0 leg cannot mask it. A reactor that signals a silence/reject
    # via metadata but reports a stale/partial NON-zero fill_size_pct (partial-then-reject)
    # MUST still be caught as a no-fill — the safety-critical phantom-fire guard. RED-proven:
    # deleting the four metadata legs makes EACH of these fail (the realized leg sees 0.05/-0.03).
    ({"silenced": True}, 0.05, True),                   # PaperReactor cap-clip, stale fill
    ({"no_fill": True}, 0.05, True),                    # DeterministicEquity no-fill, stale fill
    ({"bp_rejected": True}, 0.05, True),                # BP refusal, stale fill
    ({"unfilled_timeout": True}, 0.05, True),           # broker timeout, stale fill
    ({"silenced": True}, -0.03, True),                  # metadata wins even on a non-zero short
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


# --------------------------------------------------------------------------- #
# 0aa6: a multi-leg PARENT sizes by contracts (outer_qty), so fill_size_pct==0.0 on a
# REAL fill must NOT be mis-counted as a no-fill (it would skip journal + concurrency
# accounting while the legs moved the book). The parent's no-fill keys on parent_status /
# outer_qty / explicit reject metadata, NOT on fill_size_pct.
# --------------------------------------------------------------------------- #
def _mleg_parent(**meta_over):
    meta = {
        "role": "parent", "multi_leg_id": "ml1", "strategy_kind": "bull_put_spread",
        "outer_qty": 2, "parent_status": "filled", "leg_symbols": ["AAA260101P00100000"],
    }
    meta.update(meta_over)
    return _record(asset="AAA", asset_class="multi_leg", fill_size_pct=0.0, reactor_metadata=meta)


def test_mleg_parent_real_fill_with_zero_fraction_is_not_nofill():
    """RED-proof for 0aa6: a multi-leg parent with fill_size_pct==0.0 but parent_status
    'filled' + outer_qty>0 is a REAL fill, NOT a no-fill. Before the fix, the realized==0.0
    leg classified it as a no-fill -> the options fire was recorded as a silence."""
    rec = _mleg_parent(parent_status="filled", outer_qty=2)
    assert auto._reactor_record_is_nofill(rec) is False, (
        "a real multi-leg fill (outer_qty>0, parent_status='filled') must NOT be a no-fill "
        "just because fill_size_pct==0.0 (options size by contracts, not NAV-fraction)"
    )


@pytest.mark.parametrize("meta", [
    {"parent_status": "unfilled_timeout"},   # broker did not fill
    {"no_fill": True},                        # explicit reject
    {"silenced": True},                       # gross-cap silence
    {"outer_qty": 0},                         # zero contracts = nothing moved
    {"parent_status": "rejected"},            # non-'filled' status
])
def test_mleg_parent_genuine_nofill_signals(meta):
    """A multi-leg parent IS a no-fill on the contract-units signals (status/qty/metadata),
    independent of fill_size_pct (which is 0.0 here too)."""
    rec = _mleg_parent(**meta)
    assert auto._reactor_record_is_nofill(rec) is True


def test_apply_fire_accounting_mleg_real_fill_journals(monkeypatch):
    """End-to-end through the shared tail: a real mleg parent fill (0.0 fraction) journals
    + returns (pid, 0.0) — NOT None. Before 0aa6 it returned None (mis-counted no-fill)."""
    rec = _mleg_parent(parent_status="filled", outer_qty=2)
    calls = []
    monkeypatch.setattr(
        "hermes_quant.journal.writer.append_human_override",
        lambda prop, **kw: calls.append((kw.get("kind"), kw.get("reason"))),
    )
    out = auto._apply_fire_accounting(rec, _Prop(), symbol="AAA", journal_reason="autonomous_options_fire")
    assert out is not None, "0aa6: a real mleg fill must NOT be dropped as a no-fill"
    pid, realized = out
    assert pid == "prop_x"
    assert realized == pytest.approx(0.0)  # the parent's fraction IS 0.0 (sized by contracts)
    assert calls == [("approve", "autonomous_options_fire")], "a real mleg fill must journal once"
