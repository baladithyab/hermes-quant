"""B13 — play_tag/source plumbing on the ExecutionRecord.

Before B13 the retro/settlement loop could not distinguish advisor vs playbook vs
autonomous-tick fires: every fill read as "advisor". B13 carries a ``play_tag`` field
through the ExecutionRecord so each of the three writers stamps its own source.

This suite pins:
  * the dataclass default is "advisor" (backward-compatible — pre-B13 records that
    lack the field read back as advisor);
  * PaperReactor.execute() stamps the passed play_tag on the record + bus line, and
    defaults to "advisor" when the kwarg is omitted (bit-for-bit prior behavior);
  * the autonomous-tick seam stamps "autonomous";
  * the admissibility-reject no-fill record carries the play_tag through;
  * the multileg serializer round-trips play_tag and a pre-B13 dict (no key) decodes
    as "advisor".

Deterministic: tmp executions bus, no network, no ~/.hermes writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_quant.admissibility.gate_order as gate_order
import hermes_quant.react.paper as paper_mod
from hermes_quant.admissibility import AdmissibilityState, ShortabilityVerdict
from hermes_quant.proposals import Proposal
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import PaperReactor, _record_to_dict


def _proposal(*, symbol: str = "AAPL", decision_price: float = 200.0) -> Proposal:
    return Proposal(
        proposal_id=f"prop_2026-05-30T00:00:00_{symbol}_abc123",
        state="pending",
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        created_at="2026-05-30T00:00:00Z",
        expires_at="2026-05-30T01:00:00Z",
        advisor_result={
            "as_of": "2026-05-30T00:00:00Z",
            "decision_price": decision_price,
            "signal_id": "sig-1",
        },
    )


@pytest.fixture()
def reactor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PaperReactor:
    # Tick-lock isolation: this fixture fires a REAL PaperReactor against the
    # default AAPL/equity proposal. The per-symbol tick lock (paper.py:261, DEFAULT-ON)
    # resolves its lock dir from HERMES_QUANT_HOME else the LIVE ~/.hermes/quant
    # (tick_lock.py:108-112), and the conftest QUANT_HOME isolation patches only
    # module globals, not the env — so under CONCURRENT execution all AAPL/equity
    # fires contend on the shared live lock and silence to 0 bus lines (the fill
    # assertions then fail IndexError/0==1). Bypass the lock (byte-identical to
    # pre-ADR-0078) + point HERMES_QUANT_HOME at a per-test dir (belt-and-suspenders).
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "0")
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "qh"))
    return PaperReactor(executions_path=tmp_path / "executions.jsonl")


def _bus_lines(r: PaperReactor) -> list[str]:
    return [ln for ln in r.executions_path.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Dataclass default + serialization
# --------------------------------------------------------------------------- #


def test_dataclass_default_is_advisor():
    """A record built without play_tag defaults to "advisor" (backward-compatible)."""
    rec = ExecutionRecord(
        proposal_id="p",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-05-30T00:00:00Z",
        asof_execution="2026-05-30T00:00:01Z",
        target_position_pct=0.05,
        decision_price=200.0,
        fill_price=200.0,
        fill_size_pct=0.05,
        reactor_name="paper",
        human_in_the_loop=True,
    )
    assert rec.play_tag == "advisor"
    assert _record_to_dict(rec)["play_tag"] == "advisor"


# --------------------------------------------------------------------------- #
# PaperReactor stamps the source
# --------------------------------------------------------------------------- #


def test_paper_default_play_tag_is_advisor(reactor, monkeypatch):
    """Omitting play_tag => "advisor" both on the record and the persisted bus line."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    rec = reactor.execute(_proposal(), fill_size_pct=0.05)
    assert rec.play_tag == "advisor"
    assert '"play_tag":"advisor"' in _bus_lines(reactor)[0]


@pytest.mark.parametrize("tag", ["advisor", "playbook", "autonomous"])
def test_paper_stamps_passed_play_tag(reactor, monkeypatch, tag):
    """The reactor records the play_tag the caller passed, on the record and the bus."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    rec = reactor.execute(_proposal(), fill_size_pct=0.05, play_tag=tag)
    assert rec.play_tag == tag
    assert f'"play_tag":"{tag}"' in _bus_lines(reactor)[0]


def test_admissibility_reject_carries_play_tag(reactor, monkeypatch):
    """Flag ON + inadmissible short: the no-fill audit record carries the play_tag too,
    so a rejected playbook/autonomous order is attributable in the audit trail."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)

    class _RejectingOracle:
        def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
            return ShortabilityVerdict(AdmissibilityState.REJECTED, "NOT_ETB", 0.0)

    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RejectingOracle())

    rec = reactor.execute(_proposal(), fill_size_pct=-0.20, play_tag="autonomous")
    assert rec.fill_size_pct == 0.0  # no-fill
    assert rec.reactor_metadata["admissibility_rejected"] is True
    assert rec.play_tag == "autonomous"
    assert _bus_lines(reactor) == []  # nothing written


# --------------------------------------------------------------------------- #
# Autonomous-tick writer stamps "autonomous"
# --------------------------------------------------------------------------- #


def test_autonomous_react_stamps_autonomous(tmp_path, monkeypatch):
    """The autonomous._react seam fires the PaperReactor with play_tag="autonomous",
    so its fills are distinguishable from advisor/playbook fills in executions.jsonl."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    bus = tmp_path / "executions.jsonl"

    # inc2: _react now routes through `react.dispatch.select_reactor(proposal)`
    # instead of hardcoding PaperReactor(). With both routing flags OFF (the test
    # default here) select_reactor returns a PaperReactor — patch the name where
    # dispatch resolves it (`from .paper import PaperReactor` binds it into the
    # dispatch module namespace, so the package-level export is NOT what runs).
    import hermes_quant.autonomous as autonomous_mod
    import hermes_quant.react.dispatch as dispatch_mod
    from hermes_quant.watchlist import WatchlistEntry

    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)
    monkeypatch.delenv("HERMES_QUANT_DETERMINISTIC_EQUITY", raising=False)
    # TICK-LOCK ISOLATION (test-only): this test fires a REAL PaperReactor for an
    # AAPL/equity proposal, which acquires the DEFAULT-ON per-symbol tick lock
    # (paper.py:261). tick_lock._quant_home() (tick_lock.py:112) resolves the lock
    # dir from HERMES_QUANT_HOME else the LIVE ~/.hermes/quant, and the conftest
    # isolation fixture patches only module globals (not the env), so the lock
    # otherwise resolves to the shared live locks/paper-default__equity__AAPL.lock.
    # Under concurrent execution a sibling holding that lock SILENCES this fire
    # (0 fills) and flakes the len(lines)==1 assertion. Bypass the lock (byte-
    # identical to the pre-ADR-0078 path) + point HOME at a per-test tmp dir.
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "0")
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "qh"))
    monkeypatch.setattr(
        dispatch_mod, "PaperReactor", lambda *a, **k: PaperReactor(executions_path=bus)
    )

    entry = WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")
    advisor_result = {
        "as_of": "2026-05-30T00:00:00Z",
        "decision_price": 200.0,
        "signal_id": "sig-1",
    }
    autonomous_mod._react(advisor_result, entry, 0.05)

    lines = [ln for ln in bus.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert '"play_tag":"autonomous"' in lines[0]


# --------------------------------------------------------------------------- #
# Multileg serializer round-trip + backward-compat
# --------------------------------------------------------------------------- #


def test_multileg_dict_round_trip_and_backward_compat():
    """_record_to_dict emits play_tag; _dict_to_record reads it back; and a pre-B13 dict
    that lacks the key decodes as the safe default "advisor"."""
    from hermes_quant.react.multileg import _dict_to_record
    from hermes_quant.react.multileg import _record_to_dict as mleg_to_dict

    rec = ExecutionRecord(
        proposal_id="p",
        signal_id=None,
        asset="AAPL",
        asset_class="multi_leg",
        timeframe="",
        asof_decision="2026-05-30T00:00:00Z",
        asof_execution="2026-05-30T00:00:01Z",
        target_position_pct=0.05,
        decision_price=1.0,
        fill_price=1.0,
        fill_size_pct=0.05,
        reactor_name="multileg-paper",
        human_in_the_loop=True,
        play_tag="playbook",
    )
    d = mleg_to_dict(rec)
    assert d["play_tag"] == "playbook"
    assert _dict_to_record(d).play_tag == "playbook"

    # pre-B13 bus row: no play_tag key => decodes as "advisor".
    legacy = dict(d)
    legacy.pop("play_tag")
    assert _dict_to_record(legacy).play_tag == "advisor"
