from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from hermes_quant.proposals import Proposal, ProposalStore
from hermes_quant.protocol import AggregatedSignal, MarketState, Portfolio
from hermes_quant.react.paper import (
    FillSizeInvariantError,
    PaperReactor,
    _record_to_dict,
)
from hermes_quant.risk.gate import DefaultRiskGate
from hermes_quant.risk.kelly import quarter_kelly_size


@pytest.fixture(autouse=True)
def _paper_reactor_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)


def _proposal(
    *,
    kelly: float = 0.05,
    max_position_pct: float | None = None,
    proposal_id: str = "prop_2026-06-04T000000_AAPL_abc123",
) -> Proposal:
    risk_gate: dict[str, Any] = {
        "pass": True,
        "kelly_fraction": kelly,
        "recommended_action": "long_with_stop",
    }
    if max_position_pct is not None:
        risk_gate["max_position_pct"] = max_position_pct
    return Proposal(
        proposal_id=proposal_id,
        state="pending",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-06-04T00:00:00Z",
        expires_at="2026-06-04T00:15:00Z",
        advisor_result={
            "as_of": "2026-06-04T00:00:00Z",
            "decision_price": 200.0,
            "signal_id": "sig-fill-size",
            "risk_gate": risk_gate,
            "caveats": [],
        },
    )


def _bus_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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


def test_execute_nan_fill_size_raises_and_bus_unchanged(tmp_path: Path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)
    before = _bus_records(bus)

    with pytest.raises(FillSizeInvariantError):
        reactor.execute(_proposal(), fill_size_pct=float("nan"))

    assert _bus_records(bus) == before


def test_execute_inf_fill_size_raises_and_bus_unchanged(tmp_path: Path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)
    before = _bus_records(bus)

    with pytest.raises(FillSizeInvariantError):
        reactor.execute(_proposal(), fill_size_pct=float("inf"))

    assert _bus_records(bus) == before


def test_execute_over_default_cap_raises_and_bus_unchanged(tmp_path: Path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)
    before = _bus_records(bus)

    with pytest.raises(FillSizeInvariantError):
        reactor.execute(_proposal(), fill_size_pct=2.0)

    assert _bus_records(bus) == before


def test_execute_honors_aggressive_play_cap(tmp_path: Path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)

    record = reactor.execute(_proposal(max_position_pct=0.40), fill_size_pct=-0.40)

    assert record.fill_size_pct == pytest.approx(-0.40)
    assert _bus_records(bus)[0]["fill_size_pct"] == pytest.approx(-0.40)


def test_execute_default_cap_happy_path_writes_unchanged_record(tmp_path: Path) -> None:
    bus = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=bus)

    record = reactor.execute(_proposal(kelly=0.20), fill_size_pct=0.20)
    persisted = _bus_records(bus)

    assert record.fill_size_pct == pytest.approx(0.20)
    assert record.target_position_pct == pytest.approx(0.20)
    assert persisted == [_record_to_dict(record)]
    assert persisted[0]["fill_size_pct"] == pytest.approx(0.20)
    assert persisted[0]["target_position_pct"] == pytest.approx(0.20)


def test_quant_approve_over_cap_override_returns_invariant_error_and_stays_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_proposal().advisor_result,
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id, "size_override_pct": 2.0})
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert parsed["error"] == "fill_size_invariant"
    assert parsed["state"] == "pending"
    assert store.get(proposal.proposal_id).state == "pending"
    assert _bus_records(bus) == []


def test_quant_approve_nan_kelly_is_rejected_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pdr_mode(tmp_path, monkeypatch, "hitl")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_proposal(kelly=float("nan")).advisor_result,
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert parsed["error"] == "fill_size_invariant"
    assert parsed["state"] == "pending"
    assert store.get(proposal.proposal_id).state == "pending"
    assert _bus_records(bus) == []


def test_quant_approve_non_hitl_mode_returns_mode_mismatch_without_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pdr_mode(tmp_path, monkeypatch, "advise")
    store = _isolated_store(tmp_path, monkeypatch)
    bus = tmp_path / "executions.jsonl"
    _patch_executions_path(monkeypatch, bus)
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_proposal().advisor_result,
    )

    from hermes_quant.tools import quant_approve

    out = quant_approve({"proposal_id": proposal.proposal_id})
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert parsed["error"] == "mode_mismatch"
    assert parsed["current_mode"] == "advise"
    assert store.get(proposal.proposal_id).state == "pending"
    assert _bus_records(bus) == []


def test_quant_reject_non_hitl_mode_returns_mode_mismatch_without_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pdr_mode(tmp_path, monkeypatch, "autonomous")
    store = _isolated_store(tmp_path, monkeypatch)
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=_proposal().advisor_result,
    )

    from hermes_quant.tools import quant_reject

    out = quant_reject({"proposal_id": proposal.proposal_id, "reason": "no trade"})
    parsed = json.loads(out)

    assert parsed["success"] is False
    assert parsed["error"] == "mode_mismatch"
    assert parsed["current_mode"] == "autonomous"
    assert store.get(proposal.proposal_id).state == "pending"


def test_quarter_kelly_non_finite_inputs_return_zero() -> None:
    assert quarter_kelly_size(float("nan"), 0.0001, direction=1) == 0.0
    assert quarter_kelly_size(float("inf"), 0.0001, direction=1) == 0.0
    assert quarter_kelly_size(0.001, float("nan"), direction=1) == 0.0
    assert quarter_kelly_size(0.001, float("inf"), direction=1) == 0.0


class _NoHalt:
    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool:
        return False

    def active_halts(self) -> list[Any]:
        return []


def _signal(*, magnitude: float = 0.02, confidence: float = 0.70) -> AggregatedSignal:
    return AggregatedSignal(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-06-04T00:00:00Z"),
        direction=1,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence,
        horizon="4h",
        components=(),
        aggregator="bma",
    )


def _market(*, volatility: float = 0.02) -> MarketState:
    return MarketState(
        asset="BTC/USDT",
        asof=pd.Timestamp("2026-06-04T00:00:00Z"),
        volatility=volatility,
        commission=0.0,
        spread=0.0,
        slippage_estimate=0.0,
        tz="UTC",
    )


def _portfolio() -> Portfolio:
    return Portfolio(
        account_id="paper",
        asset_class="crypto",
        asof=pd.Timestamp("2026-06-04T00:00:00Z"),
        positions={},
        cash=100_000.0,
        equity_total=100_000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
    )


@pytest.mark.parametrize(
    ("signal", "market"),
    [
        (_signal(magnitude=float("nan")), _market()),
        (_signal(confidence=float("nan")), _market()),
        (_signal(), _market(volatility=float("nan"))),
    ],
)
def test_risk_gate_non_finite_inputs_abstain(
    signal: AggregatedSignal,
    market: MarketState,
) -> None:
    gate = DefaultRiskGate()

    action = gate.gate(signal, market, _portfolio(), _NoHalt())

    assert action is None
    assert gate.stats()["n_silenced_cost_gate"] == 1
