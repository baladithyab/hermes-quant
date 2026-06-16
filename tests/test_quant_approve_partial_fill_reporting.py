"""cs02 reporting-honesty: quant_approve success path must report the REALIZED fill.

The HITL ``quant_approve`` success return historically echoed the operator's
*requested* size (the local ``fill_size_pct`` variable) regardless of what the
reactor actually booked. Two live paths can reach the success return with a
PARTIAL fill — a nonzero realized fill that is strictly smaller than requested,
and is NOT a silence and NOT a no-fill:

  1. PaperReactor with HERMES_QUANT_PORTFOLIO_CAPS=1 on a "partial scale":
     ``_portfolio_cap_clip`` returns ``(None, clipped_pct, cap_metadata)`` where
     cap_metadata carries cap_scaled_from/to/factor but NO ``silenced`` key. The
     reactor books a REAL fill at the smaller clipped size
     (record.fill_size_pct = clipped). This escapes the ``silenced`` guard and the
     ``no_fill`` guard, so it falls through to the success return.

  2. AlpacaPaperReactor on a done_for_day/canceled order carrying a realized
     partial: fill_size_pct = realized_fill_pct (< requested) with
     reactor_metadata carrying alpaca_status/filled_qty but no silenced/no_fill/
     unfilled_timeout/bp_rejected flag — also reaches the success return.

In both cases the pre-fix success JSON reported ``fill_size_pct`` = the REQUESTED
value, overstating the realized size to the operator (operator-facing
dishonesty, cs02 family). The fix reports the realized fill from the execution
record and surfaces the requested size alongside it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.proposals import ProposalStore
from hermes_quant.react.paper import ExecutionRecord


def _advisor_result(*, kelly: float = 0.20) -> dict[str, Any]:
    return {
        "as_of": "2026-06-04T00:00:00Z",
        "decision_price": 200.0,
        "signal_id": "sig-partial-fill",
        "risk_gate": {
            "pass": True,
            "kelly_fraction": kelly,
            "recommended_action": "long_with_stop",
        },
        "caveats": [],
    }


def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProposalStore:
    import hermes_quant.proposals as proposals_module

    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    monkeypatch.setattr(proposals_module, "_default_store", store)
    return store


def _patch_executions_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import hermes_quant.daemon.signal_bus as bus_module
    import hermes_quant.react.paper as paper_module

    monkeypatch.setattr(paper_module, "EXECUTION_BUS_PATH", path)
    monkeypatch.setattr(bus_module, "EXECUTION_BUS_PATH", path)


def _set_pdr_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg_dir = tmp_path / ".hermes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(f"quant:\n  pdr:\n    mode: {mode}\n")


def test_quant_approve_paper_cap_partial_scale_reports_realized_not_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PaperReactor cap-scale: success JSON reports the CLIPPED realized fill."""
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    # Pin slippage off so the booked fill_size_pct equals the clipped value exactly.
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(kelly=0.20),
    )

    requested = 0.20
    clipped = 0.05  # cap scaled the fill down to a quarter of requested

    # Force the cap seam to take the "partial scale" branch (paper.py:806-811):
    # return (None, clipped, cap_metadata-without-silenced). The reactor then books
    # a REAL fill at the clipped size. This faithfully reproduces live path #1
    # without standing up a full PortfolioState.
    import hermes_quant.react.paper as paper_module

    def _fake_clip(self, prop, fill_size_pct, now, play_tag=None):  # noqa: ANN001
        cap_metadata = {
            "cap_scaled_from": fill_size_pct,
            "cap_scaled_to": clipped,
            "cap_scale_factor": clipped / fill_size_pct,
        }
        return None, clipped, cap_metadata

    monkeypatch.setattr(paper_module.PaperReactor, "_portfolio_cap_clip", _fake_clip)

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    # Sanity: this IS a successful approval (a partial is a success, state stays
    # approved) and the booked record carries the clipped realized fill.
    assert parsed["success"] is True
    assert parsed["state"] == "approved"
    assert parsed["execution"]["fill_size_pct"] == pytest.approx(clipped)

    # cs02 honesty: the PROMINENT reported fill must be the REALIZED (clipped)
    # value, NOT the requested 0.20. Pre-fix this was the requested size.
    assert parsed["fill_size_pct"] == pytest.approx(clipped)
    assert parsed["realized_fill_size_pct"] == pytest.approx(clipped)
    assert parsed["requested_fill_size_pct"] == pytest.approx(requested)


class _FakeAlpacaPartialReactor:
    """Mimics AlpacaPaperReactor returning a done_for_day partial fill.

    The booked ExecutionRecord carries the REALIZED fill_size_pct (< requested)
    with reactor_metadata that has alpaca_status/filled_qty but NO silencing flag
    (no silenced / no_fill / unfilled_timeout / bp_rejected), exactly like the
    real reactor's partial path (alpaca_paper.py:285-301).
    """

    name = "alpaca_paper"

    def __init__(self, requested: float, realized: float) -> None:
        self._requested = requested
        self._realized = realized

    def execute(self, proposal, *, fill_size_pct, approver_user_id=None, play_tag=None):  # noqa: ANN001
        return ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id="sig-partial-fill",
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision="2026-06-04T00:00:00Z",
            asof_execution="2026-06-04T00:01:00Z",
            target_position_pct=fill_size_pct,  # the REQUESTED NAV fraction
            decision_price=200.0,
            fill_price=200.0,
            fill_size_pct=self._realized,  # ACTUAL partial fraction (< requested)
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "alpaca_paper": True,
                "alpaca_status": "done_for_day",
                "filled_qty": 30,
                "requested_target_pct": self._requested,
                # NOTE: deliberately NO silenced / no_fill / unfilled_timeout / bp_rejected.
            },
            bar_ts="2026-06-04T00:00:00Z",
            play_tag=play_tag,
        )


def test_quant_approve_alpaca_partial_fill_reports_realized_not_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AlpacaPaperReactor done_for_day partial: success JSON reports realized fill."""
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(kelly=0.20),
    )

    requested = 0.20
    realized = 0.06  # broker filled only 30% of the requested notional

    import hermes_quant.react.dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module,
        "select_reactor",
        lambda _prop: _FakeAlpacaPartialReactor(requested, realized),
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["state"] == "approved"
    assert parsed["execution"]["fill_size_pct"] == pytest.approx(realized)

    # cs02 honesty: prominent fill_size_pct must be the realized partial, not requested.
    assert parsed["fill_size_pct"] == pytest.approx(realized)
    assert parsed["realized_fill_size_pct"] == pytest.approx(realized)
    assert parsed["requested_fill_size_pct"] == pytest.approx(requested)


def test_quant_approve_full_fill_realized_equals_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: a FULL fill reports realized == requested (no false partial).

    The flag-OFF default PaperReactor books the full requested size; the success
    JSON must report that size in all three fields. This pins byte-identical
    behavior when no cap clip / no partial occurs.
    """
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)

    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_advisor_result(kelly=0.20),
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    assert parsed["success"] is True
    assert parsed["state"] == "approved"
    assert parsed["fill_size_pct"] == pytest.approx(0.20)
    assert parsed["realized_fill_size_pct"] == pytest.approx(0.20)
    assert parsed["requested_fill_size_pct"] == pytest.approx(0.20)
