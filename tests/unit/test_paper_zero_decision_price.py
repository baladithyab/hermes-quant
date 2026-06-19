"""cr05 (LANDED 2026-06-14): a proposal with a 0/negative/non-finite decision_price
is REJECTED (silence-by-default) instead of being booked as a corrupt fill_price=0.0
execution record.

``PaperReactor._extract_decision_price`` returns the sentinel 0.0 when no usable
decision price is present (gated proposal approved-anyway / missing advisor field).
BEFORE the fix that 0.0 flowed unconditionally onto the bus:

  * v0.1 (HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1): legacy passthrough set
    ``fill_price = decision_price`` = 0.0 directly.
  * v0.2 (default): ``apply_slippage`` raises ValueError("decision_price must be
    finite and > 0"); the reactor DEGRADED to passthrough (fill_price = 0.0) and
    stamped ``reactor_metadata['slippage_breakdown']['error']`` — but still APPENDED
    the fill_price=0.0 record.

A $0 (or NaN/negative) fill price corrupts realized-P&L / cost-basis math downstream
(zero-division in horizon-return). It is NOT a recoverable degradation. AFTER the fix
(paper.py): ``execute()`` rejects a non-finite / <= 0 ``decision_price`` UPSTREAM with a
SILENCE record (fill_size_pct=0.0, NOT appended, no state.db write, reactor_metadata
silenced=True silence_reason='zero_decision_price'), and the v0.2 slippage-rejection
branch FAILS CLOSED to the same silence shape (silence_reason='slippage_rejected')
instead of degrading. The reactor returns a record rather than raising so the live fire
loop (autonomous.py:948, no try/except) is never crashed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_quant.react.paper import PaperReactor
from hermes_quant.state import portfolio_state as ps_mod


def _proposal_no_price() -> SimpleNamespace:
    """A proposal whose advisor_result carries NO usable decision_price, so
    _extract_decision_price() falls through to the 0.0 sentinel."""
    return SimpleNamespace(
        proposal_id="prop_zero_price",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"as_of": "2026-06-12T10:00:00Z"},  # no decision_price, no analyst_views
        reactor_metadata=None,
    )


def _proposal_priced(decision_price: float = 100.0) -> SimpleNamespace:
    """A proposal with a finite, positive decision_price (a normal fill)."""
    return SimpleNamespace(
        proposal_id="prop_priced",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"decision_price": decision_price, "as_of": "2026-06-12T10:00:00Z"},
        reactor_metadata=None,
    )


def _isolate_state(tmp_path: Path) -> None:
    ps_mod.DEFAULT_STATE_DB = tmp_path / "state.db"
    with ps_mod._singleton_lock:
        ps_mod._singleton = None


def _nonblank_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def test_v01_zero_decision_price_rejected_not_booked(tmp_path, monkeypatch):
    """v0.1: the decision_price sentinel 0.0 is REJECTED (silenced), NOT booked."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_no_price(), fill_size_pct=0.05, play_tag="autonomous")

    # Rejected: silence record, no position movement.
    assert record.fill_size_pct == pytest.approx(0.0)
    assert (record.reactor_metadata or {}).get("silenced") is True
    assert (record.reactor_metadata or {}).get("silence_reason") == "zero_decision_price"

    # Nothing landed on the bus.
    assert _nonblank_lines(executions_path) == []

    # state.db has no AAPL position.
    book = ps_mod.get_portfolio_state().get_positions("paper-default")
    assert ("equity", "AAPL") not in book


def test_v02_zero_decision_price_rejected_not_booked(tmp_path, monkeypatch):
    """v0.2: the decision_price sentinel 0.0 is REJECTED UPSTREAM (before apply_slippage),
    NOT degraded to a fill_price=0.0 record."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_no_price(), fill_size_pct=0.05, play_tag="autonomous")

    # Rejected with the SAME shape as v0.1 — caught upstream by the A1 guard, so the
    # rejection reason is zero_decision_price and apply_slippage was never reached.
    assert record.fill_size_pct == pytest.approx(0.0)
    assert (record.reactor_metadata or {}).get("silenced") is True
    assert (record.reactor_metadata or {}).get("silence_reason") == "zero_decision_price"
    # apply_slippage NOT reached => no slippage_breakdown.error from the v0.2 branch.
    assert "slippage_breakdown" not in (record.reactor_metadata or {})

    assert _nonblank_lines(executions_path) == []
    book = ps_mod.get_portfolio_state().get_positions("paper-default")
    assert ("equity", "AAPL") not in book


def test_v02_finite_price_model_reject_fails_closed(tmp_path, monkeypatch):
    """v0.2: a FINITE, >0 decision_price (so the A1 guard does NOT fire) whose
    apply_slippage STILL raises ValueError must FAIL CLOSED — a silence record, NOT a
    degraded fill_price=0.0 booking."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    # Force apply_slippage to raise for a proposal that DOES have a finite price > 0,
    # so the only thing keeping it off the bus is the A2 fail-closed branch.
    import hermes_quant.react.slippage_model as slip_mod

    def _raise(*_a, **_k):
        raise ValueError("synthetic model rejection")

    monkeypatch.setattr(slip_mod, "apply_slippage", _raise)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_priced(100.0), fill_size_pct=0.05, play_tag="autonomous")

    # Finite price > 0 reached the slippage call (A1 did NOT reject it), then the model
    # raise was caught and turned into a fail-closed silence — NOT a fill_price=0.0 row.
    assert record.fill_size_pct == pytest.approx(0.0)
    assert (record.reactor_metadata or {}).get("silenced") is True
    assert (record.reactor_metadata or {}).get("silence_reason") == "slippage_rejected"
    # The model error is surfaced for audit, but the fill is NOT booked.
    breakdown = (record.reactor_metadata or {}).get("slippage_breakdown") or {}
    assert "error" in breakdown

    assert _nonblank_lines(executions_path) == []
    book = ps_mod.get_portfolio_state().get_positions("paper-default")
    assert ("equity", "AAPL") not in book


def test_v01_finite_price_normal_fill_not_silenced(tmp_path, monkeypatch):
    """Byte-identical guard: a normal finite price (100.0), v0.1, caps OFF, tick-lock ON
    appends EXACTLY ONE position-moving record at fill_price==100.0 — the A1/A2 guards
    NEVER fire for a healthy fill."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_priced(100.0), fill_size_pct=0.05, play_tag="autonomous")

    assert record.fill_size_pct == pytest.approx(0.05)
    assert record.fill_price == pytest.approx(100.0)
    assert record.decision_price == pytest.approx(100.0)
    assert not (record.reactor_metadata or {}).get("silenced")

    lines = _nonblank_lines(executions_path)
    assert len(lines) == 1, f"expected exactly one position-moving fill, got {lines}"
