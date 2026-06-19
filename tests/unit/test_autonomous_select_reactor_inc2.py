"""inc2 RED->GREEN — autonomous fires through the ONE dispatch chokepoint.

ra02 mechanism class (behind the 2026-06-02 41.6x-gross incident): the
autonomous fire-path hardcoded ``reactor = PaperReactor()`` in
``hermes_quant.autonomous._react`` and NEVER called the landed
``react.dispatch.select_reactor()`` seam — a SECOND, duplicate reactor-choice
site that did not inherit the dispatch chokepoint the HITL/CLI approve path uses.
Any future cap centralization wired through ``select_reactor`` would not apply
to autonomous fires.

THIS LANE routes the autonomous fire through ``select_reactor(proposal)``. The
three invariants this module pins:

  1. BYTE-IDENTICAL flags-OFF — with both routing flags unset (production
     default), ``select_reactor`` returns a ``PaperReactor`` for the synthesized
     equity proposal, exactly the type the seam constructed before. The fire
     lands one bus line stamped play_tag=autonomous, reactor_name=paper.
  2. paper_zero_costs GUARD still raises — when a routing flag would route a
     NON-paper reactor AND paper_zero_costs is set, ``_react`` raises ValueError
     BEFORE any execution side-effect (no bus write).
  3. flags-ON PARITY — with HERMES_QUANT_ALPACA_PAPER=1 the autonomous path
     routes the SAME reactor ``select_reactor`` returns for that proposal (the
     autonomous fire now inherits the HITL routing decision, not a hardcoded one).

All deterministic, no network: the alpaca branch is exercised by asserting the
routed reactor TYPE/.name parity with select_reactor (we monkeypatch the alpaca
reactor's execute to a no-op so no client is built).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_quant.react.dispatch as dispatch_mod
from hermes_quant.autonomous import _react
from hermes_quant.react.dispatch import ALPACA_PAPER_FLAG, DETERMINISTIC_EQUITY_FLAG
from hermes_quant.react.paper import PaperReactor
from hermes_quant.watchlist import WatchlistEntry


def _entry() -> WatchlistEntry:
    return WatchlistEntry(symbol="AAPL", asset_class="equity", timeframe="1d")


def _advisor_result() -> dict:
    return {
        "as_of": "2026-06-14T00:00:00Z",
        "decision_price": 200.0,
        "signal_id": "sig-inc2",
    }


@pytest.fixture(autouse=True)
def _flags_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production default: both routing flags OFF, admissibility OFF.

    TICK-LOCK ISOLATION (test-only): the PaperReactor per-symbol tick lock is
    DEFAULT-ON (paper.py:261) and ``tick_lock._quant_home()`` (tick_lock.py:112)
    resolves the lock dir from the ``HERMES_QUANT_HOME`` env var, else the LIVE
    ``~/.hermes/quant``. The conftest ``_isolate_quant_home_and_execution_bus``
    fixture monkeypatches only the *module globals* (tools.QUANT_HOME /
    signal_bus.QUANT_HOME) — it never sets ``HERMES_QUANT_HOME`` nor
    ``HERMES_QUANT_TICK_LOCK`` — so the lock would otherwise resolve to the shared
    live ``~/.hermes/quant/locks/paper-default__equity__AAPL.lock`` that every
    AAPL/equity fire contends on. Under concurrent (review-team) execution a
    sibling process holding that lock makes this test's fire SILENCE (0 fills),
    flaking the byte-identical / guard-silent assertions (proven: a held live
    lock turns ``len(lines) == 1`` into ``0 == 1``).

    Bypass the lock entirely (byte-identical to the pre-ADR-0078 path) so these
    fill assertions hold regardless of concurrent processes. ``HERMES_QUANT_HOME``
    is ALSO pointed at a per-test tmp dir as belt-and-suspenders: even if the lock
    were re-enabled, no two processes would share a lock file.
    """
    monkeypatch.delenv(ALPACA_PAPER_FLAG, raising=False)
    monkeypatch.delenv(DETERMINISTIC_EQUITY_FLAG, raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "0")
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "qh"))


# --------------------------------------------------------------------------- #
# (1) BYTE-IDENTICAL: flags OFF -> PaperReactor, single bus line, autonomous tag
# --------------------------------------------------------------------------- #
def test_flags_off_routes_paper_reactor_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = tmp_path / "executions.jsonl"
    captured: dict[str, object] = {}

    # Redirect the bus AND capture the routed reactor type. dispatch resolves
    # PaperReactor from its own module namespace (`from .paper import ...`), so
    # patch it THERE (the package export is not what runs).
    def _paper_factory(*_a, **_k):  # noqa: ANN002, ANN003
        r = PaperReactor(executions_path=bus)
        captured["reactor"] = r
        return r

    monkeypatch.setattr(dispatch_mod, "PaperReactor", _paper_factory)

    out = _react(_advisor_result(), _entry(), 0.05)

    # ar38/ar80: _react returns (pid, realized_fill_size_pct) on a fire (None on no-fill).
    assert out is not None, "reactor should have fired"
    pid, _realized = out
    assert isinstance(pid, str) and pid
    # The routed reactor with both flags OFF is a PaperReactor (byte-identical
    # to the hardcoded type the seam constructed before the cutover).
    assert isinstance(captured["reactor"], PaperReactor)
    assert getattr(captured["reactor"], "name", None) == "paper"

    lines = [ln for ln in bus.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, "exactly one fill must land (identical fire behavior)"
    assert '"play_tag":"autonomous"' in lines[0]
    assert '"reactor_name":"paper"' in lines[0]


# --------------------------------------------------------------------------- #
# (2) GUARD: paper_zero_costs + routing-flag-ON non-paper reactor -> ValueError
#     BEFORE any execution side-effect.
# --------------------------------------------------------------------------- #
class _NonPaperStub:
    name = "alpaca_paper"
    requires_credentials = True

    def __init__(self) -> None:
        self.executed = False

    def execute(self, proposal, **kwargs):  # noqa: ANN001, ANN003
        self.executed = True
        return None


def test_guard_raises_when_routing_flag_routes_non_paper_and_zero_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a routing flag ON select_reactor CAN return a non-paper reactor; the
    paper_zero_costs guard must STILL raise before execute() (now more load-bearing
    because the reactor can vary)."""
    monkeypatch.setenv(ALPACA_PAPER_FLAG, "1")
    holder: dict[str, _NonPaperStub] = {}

    def _select(_proposal):  # noqa: ANN001, ANN202
        r = _NonPaperStub()
        holder["reactor"] = r
        return r

    monkeypatch.setattr(dispatch_mod, "select_reactor", _select)

    with pytest.raises(
        ValueError, match="paper_zero_costs is set but reactor is not paper"
    ):
        _react(_advisor_result(), _entry(), 0.05, paper_zero_costs=True)

    assert holder["reactor"].executed is False, (
        "guard must raise BEFORE reactor.execute() — a non-paper reactor must "
        "never execute with the cost gate zeroed"
    )


def test_guard_silent_when_flag_routes_paper_and_zero_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flags OFF -> select_reactor returns paper -> paper_zero_costs is allowed
    (no raise), and the fire proceeds."""
    bus = tmp_path / "executions.jsonl"
    monkeypatch.setattr(
        dispatch_mod, "PaperReactor", lambda *a, **k: PaperReactor(executions_path=bus)
    )
    out = _react(_advisor_result(), _entry(), 0.05, paper_zero_costs=True)
    assert out is not None, "reactor should have fired"
    pid, _realized = out
    assert isinstance(pid, str) and pid
    lines = [ln for ln in bus.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
# (3) PARITY: flags ON -> autonomous routes the SAME reactor select_reactor does.
# --------------------------------------------------------------------------- #
def test_flags_on_routes_same_reactor_as_select_reactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HERMES_QUANT_ALPACA_PAPER=1: the autonomous fire inherits the HITL routing
    decision — the reactor _react uses is the SAME one select_reactor returns for
    this proposal (an AlpacaPaperReactor, .name='alpaca_paper'), NOT a hardcoded
    PaperReactor. We assert via the routed-reactor identity; execute() is stubbed
    to a no-op so no Alpaca client is built (deterministic, no network)."""
    monkeypatch.setenv(ALPACA_PAPER_FLAG, "1")

    captured: dict[str, object] = {}
    real_select = dispatch_mod.select_reactor

    def _spy_select(proposal):  # noqa: ANN001, ANN202
        reactor = real_select(proposal)
        captured["reactor"] = reactor
        # Stub execute so the alpaca path does not touch the network. We assert
        # on routing (which reactor was chosen), not on the Alpaca fill mechanics.
        monkeypatch.setattr(reactor, "execute", lambda *a, **k: None)
        return reactor

    monkeypatch.setattr(dispatch_mod, "select_reactor", _spy_select)

    out = _react(_advisor_result(), _entry(), 0.05)
    # ar38/ar80: _react returns (pid, realized_fill_size_pct) on a fire (None on no-fill).
    assert out is not None, "reactor should have fired"
    pid, _realized = out
    assert isinstance(pid, str) and pid

    routed = captured["reactor"]
    # select_reactor(equity proposal) with ALPACA_PAPER on returns AlpacaPaperReactor.
    assert getattr(routed, "name", None) == "alpaca_paper", (
        "with the flag ON the autonomous fire must inherit the same routed reactor "
        "the HITL path uses, not a hardcoded PaperReactor"
    )
    # And it is NOT a PaperReactor (the pre-cutover hardcoded type).
    assert not isinstance(routed, PaperReactor)
