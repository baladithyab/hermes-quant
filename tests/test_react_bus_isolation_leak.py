"""Regression guard for the react/* execution-bus test-isolation leak.

Root cause (found 2026-06-22 via the 6bb9 clean-day audit): 22 phantom AAPL@200
fixture fills (signal_id=sig-partial-fill / sig-toctou, asof_decision=2026-06-04)
leaked from tests/test_quant_approve_{partial_fill_reporting,toctou_double_fire}.py
into the REAL ~/.hermes/quant/executions.jsonl. Chain:

  1. ~/.hermes/.env has HERMES_QUANT_DETERMINISTIC_EQUITY=1, which leaks into the
     pytest process env (the leaking tests never delenv it).
  2. select_reactor() reads that flag from os.environ -> returns
     DeterministicEquityReactor instead of the PaperReactor the tests patched.
  3. react/deterministic_equity.py binds EXECUTION_BUS_PATH via
     `from daemon.signal_bus import EXECUTION_BUS_PATH` -> a PRIVATE module copy.
     The conftest autouse fixture patched the attribute on tools + signal_bus,
     which does NOT rebind the already-imported copies in react/*. So the
     det-equity reactor wrote the real bus.

The durable fix (closes the whole from-import-copy family, not just the 2 tests):
the conftest now ALSO patches EXECUTION_BUS_PATH on react.{paper,deterministic_equity,
alpaca_paper,multileg} AND delenvs the reactor-routing flags so selection is
deterministic. These tests assert both halves hold for EVERY test by construction.
"""

from __future__ import annotations

import os

import pytest

# react.multileg imports a torch-stub chain in some envs; guard the import so a
# missing optional dep degrades to a skip, not a collection error.
REACT_MODULES = [
    "hermes_quant.react.paper",
    "hermes_quant.react.deterministic_equity",
    "hermes_quant.react.alpaca_paper",
    "hermes_quant.react.multileg",
]


@pytest.mark.parametrize("mod_name", REACT_MODULES)
def test_react_module_execution_bus_is_isolated_to_tmp(mod_name, tmp_path):
    """LOAD-BEARING RED PROOF: each react module's EXECUTION_BUS_PATH must point
    inside the per-test tmp_path, never the real ~/.hermes.

    Before the conftest fix this is RED for deterministic_equity/alpaca_paper/
    multileg/paper: their from-import copies still pointed at the live bus, so
    relative_to(tmp_path) raises ValueError.
    """
    mod = pytest.importorskip(mod_name)
    bus = mod.EXECUTION_BUS_PATH
    # relative_to raises ValueError if `bus` is not under the test's tmp_path —
    # i.e. if it still points at the real ~/.hermes/quant/executions.jsonl.
    bus.relative_to(tmp_path)


def test_routing_flags_are_unset_by_default():
    """The reactor-routing flags must be scrubbed by the autouse fixture so a
    leaked operator HERMES_QUANT_DETERMINISTIC_EQUITY=1 cannot silently re-route
    a test's equity fill to a different reactor.
    """
    assert os.environ.get("HERMES_QUANT_DETERMINISTIC_EQUITY") is None
    assert os.environ.get("HERMES_QUANT_ALPACA_PAPER") is None


def test_det_equity_reactor_under_leaked_flag_still_writes_tmp(tmp_path, monkeypatch):
    """Behavioral proof: even when a test OPTS IN to the det-equity flag (as the
    leaking tests effectively did via env inheritance), select_reactor returns a
    reactor whose executions_path is the isolated tmp bus, not the real ledger.
    """
    from hermes_quant.react import dispatch

    monkeypatch.setenv("HERMES_QUANT_DETERMINISTIC_EQUITY", "1")

    class _EquityProposal:
        proposal_kind = "equity"
        symbol = "AAPL"
        asset_class = "equity"
        timeframe = "1d"
        proposal_id = "prop-test"

    reactor = dispatch.select_reactor(_EquityProposal())
    # If the deterministic backend is the resolved default, we get the det-equity
    # reactor; otherwise PaperReactor. EITHER way its bus must be under tmp_path.
    reactor.executions_path.relative_to(tmp_path)
