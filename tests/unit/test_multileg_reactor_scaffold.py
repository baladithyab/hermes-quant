"""Unit tests for hermes_quant.react.multileg — reactor SCAFFOLD (Wave B2).

Deterministic, no network. Per plan §2.4: flag-gate + Protocol conformance +
no-write-while-disabled.
"""

from __future__ import annotations

import pytest

from hermes_quant.react import __all__ as react_all
from hermes_quant.react.base import Reactor
from hermes_quant.react.multileg import (
    MultiLegPaperReactor,
    MultiLegReactorDisabled,
)


def test_execute_raises_disabled_when_flag_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(object(), fill_size_pct=0.05)
    # Nothing written: the bus must not even be created while disabled.
    assert not bus.exists()


def test_execute_writes_nothing_to_existing_bus_when_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    bus = tmp_path / "executions.jsonl"
    bus.write_text("")  # pre-existing empty bus
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(MultiLegReactorDisabled):
        reactor.execute(object(), fill_size_pct=0.05)
    assert bus.read_text() == ""  # unchanged


def test_execute_raises_not_implemented_when_enabled(monkeypatch, tmp_path) -> None:
    """With the flag set, execute() raises NotImplementedError (body deferred),
    proving the flag gate is the FIRST check and the live body is absent."""
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    with pytest.raises(NotImplementedError):
        reactor.execute(object(), fill_size_pct=0.05)
    assert not bus.exists()  # still writes nothing


def test_protocol_conformance() -> None:
    assert isinstance(MultiLegPaperReactor(), Reactor) is True


def test_name_and_credentials() -> None:
    reactor = MultiLegPaperReactor()
    assert reactor.name == "multileg-paper"
    assert reactor.requires_credentials is False


def test_not_in_react_all() -> None:
    """Regression guard: the scaffold stays un-dispatched this wave."""
    assert "MultiLegPaperReactor" not in react_all
