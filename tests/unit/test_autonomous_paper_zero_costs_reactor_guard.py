"""Regression test for the autonomous fail-closed reactor-name guard.

`hermes_quant.autonomous._react` carries the ONLY rail (a ValueError at
~:885) preventing the paper-mode-only cost-gate override
(`paper_zero_costs=True`, which zeroes the cost gate so all positive-edge
signals fire) from ever being wired to a non-paper reactor:

    reactor = PaperReactor()
    if paper_zero_costs and getattr(reactor, "name", None) != "paper":
        raise ValueError("paper_zero_costs is set but reactor is not paper")

The sibling suite tests/unit/test_paper_zero_costs.py exercises the
cost-gate *threshold* behavior of the flag but does NOT pin this guard.
A refactor that renamed the reactor, changed the `getattr(..., "name")`
key, or dropped the `paper_zero_costs and ...` condition would silently
disable the rail — letting the cost gate be zeroed against a live
reactor with no exception. This test pins all three failure modes:

  1. paper_zero_costs=True + non-paper reactor  -> ValueError BEFORE any
     execution side-effect (the rail).
  2. paper_zero_costs=True + reactor named "paper" -> NO ValueError.
  3. paper_zero_costs=False + non-paper reactor  -> NO ValueError (the
     flag, not the reactor name alone, is the trigger; live behavior
     must be unaffected when the flag is off).

The guard fires before the Proposal stand-in is synthesized and before
reactor.execute(), so the guard-fires assertion exercises no fill I/O.
For the guard-does-NOT-fire cases we stub PaperReactor.execute to a
no-op so the test writes no real executions and asserts purely on the
guard's control flow.
"""

from __future__ import annotations

import pytest

import hermes_quant.react as react_pkg
from hermes_quant.autonomous import _react
from hermes_quant.watchlist import WatchlistEntry


class _StubReactor:
    """Minimal PaperReactor stand-in with a configurable .name.

    Records whether execute() was reached so the guard-fires case can
    assert no execution side-effect occurred before the ValueError.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.requires_credentials = False
        self.executed = False

    def execute(self, proposal, **kwargs):  # noqa: ANN001, ANN003
        self.executed = True
        return None


def _entry() -> WatchlistEntry:
    return WatchlistEntry(symbol="BTC/USDT", asset_class="crypto", timeframe="1h")


def _advisor_result() -> dict:
    # Minimal advisor_result; _react only reads `as_of` (with a fallback)
    # before the guard, and the guard fires (case 1) well before any field
    # of this dict is consumed.
    return {"as_of": "2026-06-13T00:00:00Z"}


def _patch_reactor(monkeypatch: pytest.MonkeyPatch, name: str) -> _StubReactor:
    """Force `_react`'s lazy `from hermes_quant.react import PaperReactor`
    to construct a stub reactor with the given .name."""
    holder: dict[str, _StubReactor] = {}

    def _factory(*args, **kwargs):  # noqa: ANN002, ANN003
        r = _StubReactor(name)
        holder["reactor"] = r
        return r

    monkeypatch.setattr(react_pkg, "PaperReactor", _factory)
    return holder  # type: ignore[return-value]


def test_react_guard_fires_for_non_paper_reactor_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE RAIL: paper_zero_costs=True against a reactor whose name is not
    'paper' MUST raise ValueError before any execution side-effect."""
    holder = _patch_reactor(monkeypatch, name="live")

    with pytest.raises(ValueError, match="paper_zero_costs is set but reactor is not paper"):
        _react(
            _advisor_result(),
            _entry(),
            fill_size_pct=0.05,
            paper_zero_costs=True,
        )

    # The guard must fire BEFORE execute() — no fill I/O on the rail path.
    assert holder["reactor"].executed is False, (
        "guard must raise before reactor.execute() — a non-paper reactor "
        "must never be allowed to execute with the cost gate zeroed"
    )


def test_react_guard_silent_for_paper_reactor_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reactor named 'paper' with paper_zero_costs=True must NOT raise —
    the override's intended (paper-only) configuration is allowed."""
    holder = _patch_reactor(monkeypatch, name="paper")

    # Must not raise; execute() is a no-op stub so no real fill is written.
    pid = _react(
        _advisor_result(),
        _entry(),
        fill_size_pct=0.05,
        paper_zero_costs=True,
    )
    assert isinstance(pid, str) and pid, "expected a synthesized proposal_id"
    assert holder["reactor"].executed is True, (
        "paper reactor must proceed past the guard to execute()"
    )


def test_react_guard_silent_for_non_paper_reactor_when_flag_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When paper_zero_costs is False, the guard must NOT fire even for a
    non-paper reactor — the flag (not the reactor name alone) is the
    trigger. This pins that live behavior is unaffected when the override
    is off (silence-by-default on the live path)."""
    holder = _patch_reactor(monkeypatch, name="live")

    # paper_zero_costs defaults to False; pass explicitly for clarity.
    pid = _react(
        _advisor_result(),
        _entry(),
        fill_size_pct=0.05,
        paper_zero_costs=False,
    )
    assert isinstance(pid, str) and pid
    assert holder["reactor"].executed is True, (
        "with the flag off the guard is bypassed and execution proceeds"
    )
