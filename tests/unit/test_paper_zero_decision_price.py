"""cr05 RED proof: a proposal with a 0/negative decision_price currently DEGRADES to
an APPENDED fill_price=0.0 execution record (a corrupt P&L basis) instead of being
rejected.

``PaperReactor._extract_decision_price`` returns the sentinel 0.0 when no usable
decision price is present (gated proposal approved-anyway / missing advisor field).
That 0.0 then flows unconditionally into the ExecutionRecord and onto the bus:

  * v0.1 (HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1): legacy passthrough sets
    ``fill_price = decision_price`` = 0.0 directly.
  * v0.2 (default): ``apply_slippage`` raises ValueError("decision_price must be
    finite and > 0"); the reactor DEGRADES to passthrough (fill_price = 0.0) and
    stamps ``reactor_metadata['slippage_breakdown']['error']`` — but still APPENDS the
    fill_price=0.0 record.

Both branches fall through to the unconditional ExecutionRecord construction +
``append_locked`` write — a $0 fill price corrupts realized-P&L / cost-basis math
downstream.

TEST-ONLY disposition: changing the degrade into a REJECT/silence is a LIVE reactor
behavior change (a new refusal path), so it is DEFERRED / flag-gated to its own
increment. This test documents the corruption today; it does NOT change the reactor.
"""

from __future__ import annotations

import json
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


def _isolate_state(tmp_path: Path) -> None:
    ps_mod.DEFAULT_STATE_DB = tmp_path / "state.db"
    with ps_mod._singleton_lock:
        ps_mod._singleton = None


def test_v01_zero_decision_price_appends_corrupt_fill(tmp_path, monkeypatch):
    """v0.1 passthrough: decision_price sentinel 0.0 => fill_price 0.0 record APPENDED."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_no_price(), fill_size_pct=0.05, play_tag="autonomous")

    # The corrupt record: fill_price degraded to 0.0 instead of rejected.
    assert record.decision_price == pytest.approx(0.0)
    assert record.fill_price == pytest.approx(0.0)
    assert not (record.reactor_metadata or {}).get("silenced")

    # And it LANDED on the bus (one position-moving fill at price 0.0).
    lines = [ln for ln in executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected the corrupt fill to be appended, got {lines}"
    rec = json.loads(lines[0])
    assert rec["fill_price"] == pytest.approx(0.0)
    assert rec["fill_size_pct"] == pytest.approx(0.05)


def test_v02_zero_decision_price_degrades_and_appends_corrupt_fill(tmp_path, monkeypatch):
    """v0.2 slippage ON: apply_slippage raises on price<=0; the reactor DEGRADES to a
    fill_price=0.0 record (with the error surfaced in metadata) and STILL appends it."""
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    _isolate_state(tmp_path)

    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)
    record = reactor.execute(_proposal_no_price(), fill_size_pct=0.05, play_tag="autonomous")

    # Degraded-to-passthrough: fill_price 0.0, not rejected.
    assert record.fill_price == pytest.approx(0.0)
    assert not (record.reactor_metadata or {}).get("silenced")
    # The ValueError is surfaced in the slippage breakdown rather than rejecting.
    breakdown = (record.reactor_metadata or {}).get("slippage_breakdown") or {}
    assert "error" in breakdown, f"expected a slippage error in metadata, got {breakdown}"

    # And the corrupt record landed on the bus.
    lines = [ln for ln in executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected the degraded fill to be appended, got {lines}"
    rec = json.loads(lines[0])
    assert rec["fill_price"] == pytest.approx(0.0)
