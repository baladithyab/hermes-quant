"""Wave A + Wave B.5 wiring integration tests.

Verifies the safety modules ACTUALLY FIRE on Tuesday's market-open path:

- risk/gate.py emits gate_approval / gate_rejection audit events on every decision.
- daemon/halt_state.py emits kill_switch_fired audit events on halt creation.
- proposals.py emits proposal_emitted audit events on proposal creation.
- risk/gate.py drops signals with look-ahead-tainted evidence_ids when an
  evidence_store is injected (ADR-0033 D5 universal lookahead gate).
- Audit failures NEVER block gate decisions (silence-by-default observation).
- Backward compat: gate without evidence_store skips the lookahead check.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.evidence import (
    BarEvidence,
    EvidenceStore,
    derive_evidence_id,
    sha256_of_bytes,
)
from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log import GovernanceEvent
from hermes_quant.proposals import ProposalStore
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    MarketState,
    Portfolio,
)
from hermes_quant.risk.gate import DefaultRiskGate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the governance audit log to an isolated path per test."""
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


@pytest.fixture
def halt_state(tmp_path: Path) -> HaltStateSQLite:
    return HaltStateSQLite(
        db_path=tmp_path / "halts.db",
        mirror_path=tmp_path / "halts.json",
    )


@pytest.fixture
def evidence_store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(root=tmp_path / "evidence_store")


def _make_signal(
    *,
    direction: int = 1,
    magnitude: float = 0.02,
    confidence: float = 0.7,
    asset: str = "BTC/USDT",
    components: tuple[AnalystView, ...] = (),
    asof: pd.Timestamp | None = None,
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1h",
        asset_class="crypto",
        asof=asof or pd.Timestamp("2026-05-13T00:00:00Z"),
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=max(confidence, 0.85),
        horizon="4h",
        components=components,
        aggregator="bma",
    )


def _make_market() -> MarketState:
    return MarketState(
        asset="BTC/USDT",
        asof=pd.Timestamp("2026-05-13T00:00:00Z"),
        volatility=0.02,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0012,
        tz="UTC",
    )


def _make_portfolio(
    *,
    drawdown: float = 0.0,
    equity: float = 100_000.0,
    asset: str = "BTC/USDT",
    account_id: str = "alpaca-paper",
    asset_class: str = "crypto",
) -> Portfolio:
    peak = equity / max(1e-9, 1 - drawdown)
    return Portfolio(
        account_id=account_id,
        asset_class=asset_class,
        asof=pd.Timestamp("2026-05-13T00:00:00Z"),
        positions={},
        cash=equity,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=peak,
        daily_open_equity=equity,
    )


def _make_bar_evidence(
    *,
    payload: bytes,
    available_at: datetime,
    symbol: str = "BTC/USDT",
    source: str = "binance",
) -> BarEvidence:
    """Build a BarEvidence record with explicit available_at (so we can test
    both pre- and post-asof scenarios)."""
    h = sha256_of_bytes(payload)
    eid = derive_evidence_id("bar", source, h)
    # Choose a published_at that satisfies causality: published_at <= available_at
    published_at = available_at - timedelta(seconds=60)
    ingested_at = published_at + timedelta(seconds=5)
    return BarEvidence(
        id=eid,
        kind="bar",
        symbol=symbol,
        source=source,
        published_at=published_at,
        ingested_at=ingested_at,
        available_at=available_at,
        payload_ref=f"blobs/{h}.json",
        payload_hash=h,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000.0,
    )


def _read_audit_events(path: Path, kind: str | None = None) -> list[GovernanceEvent]:
    """Read all audit events. Returns [] if file doesn't exist."""
    if not path.exists():
        return []
    kinds = [kind] if kind else None
    return list(audit_log.read(kinds=kinds))


# ---------------------------------------------------------------------------
# Test 1: gate emits gate_approval audit event on a green signal
# ---------------------------------------------------------------------------


def test_risk_gate_emits_gate_approval_audit_event(
    audit_path: Path, halt_state: HaltStateSQLite
) -> None:
    """A signal that passes all rules should land on the audit log as
    'gate_approval'."""
    g = DefaultRiskGate()
    # Use a confident long signal with a fat magnitude so the cost gate
    # passes and Kelly emits a non-trivial size.
    signal = _make_signal(direction=1, confidence=0.85, magnitude=0.05)
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)

    assert action is not None, "gate should approve this signal"
    assert action.target_position_pct > 0
    events = _read_audit_events(audit_path, kind="gate_approval")
    assert len(events) == 1, f"expected exactly one gate_approval row, got {len(events)}"
    payload = events[0].payload
    assert payload["asset"] == signal.asset
    assert payload["direction"] == 1
    assert events[0].source == "risk.gate"


# ---------------------------------------------------------------------------
# Test 2: gate emits gate_rejection audit event when silenced by drawdown
# ---------------------------------------------------------------------------


def test_risk_gate_emits_gate_rejection_audit_event(
    audit_path: Path, halt_state: HaltStateSQLite
) -> None:
    """A signal silenced by the drawdown circuit breaker should emit
    'gate_rejection' on the audit log."""
    g = DefaultRiskGate()
    signal = _make_signal(direction=1, confidence=0.85)
    # 0.20 drawdown > default 0.15 max → drawdown breaker fires
    portfolio = _make_portfolio(drawdown=0.20)
    action = g.gate(signal, _make_market(), portfolio, halt_state)

    # Drawdown breaker emits a flatten action (target_pct=0) with halt=True;
    # this is a rejection of the signal-as-requested.
    assert action is not None
    assert action.target_position_pct == 0.0

    events = _read_audit_events(audit_path, kind="gate_rejection")
    assert len(events) == 1
    assert "drawdown" in events[0].payload["reason"]


# ---------------------------------------------------------------------------
# Test 3: audit_log failure must NOT block the gate decision
# ---------------------------------------------------------------------------


def test_risk_gate_audit_failure_does_not_block_gate_decision(
    audit_path: Path,
    halt_state: HaltStateSQLite,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If audit_log.append raises, the gate must still return its decision
    and log a WARNING (silence-by-default observation: audit is observation,
    NEVER a control plane gate)."""

    def raising_append(event: GovernanceEvent) -> None:
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(audit_log, "append", raising_append)

    g = DefaultRiskGate()
    signal = _make_signal(direction=1, confidence=0.85, magnitude=0.05)

    with caplog.at_level(logging.WARNING, logger="hermes_quant.risk.gate"):
        action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)

    # The gate STILL returns its decision.
    assert action is not None
    assert action.target_position_pct > 0
    # And surfaces a warning so the operator can detect audit-pipeline failure.
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("audit_log.append failed" in m for m in warning_messages), (
        f"expected an audit-failure warning, got: {warning_messages}"
    )


# ---------------------------------------------------------------------------
# Test 4: halt_state insert emits kill_switch_fired audit event
# ---------------------------------------------------------------------------


def test_halt_state_create_emits_kill_switch_fired_event(
    audit_path: Path, halt_state: HaltStateSQLite
) -> None:
    record = halt_state.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="manual_test_halt")
    assert record.halt_epoch >= 1

    events = _read_audit_events(audit_path, kind="kill_switch_fired")
    assert len(events) == 1, f"expected one kill_switch_fired row, got {len(events)}"
    payload = events[0].payload
    assert payload["account_id"] == "alpaca-paper"
    assert payload["asset_class"] == "crypto"
    assert payload["asset"] == "BTC/USDT"
    assert payload["reason"] == "manual_test_halt"
    assert payload["halt_epoch"] == record.halt_epoch
    assert events[0].source == "daemon.halt_state"


# ---------------------------------------------------------------------------
# Test 5: proposals.propose emits proposal_emitted audit event
# ---------------------------------------------------------------------------


def test_proposal_create_emits_proposal_emitted_event(audit_path: Path, tmp_path: Path) -> None:
    store = ProposalStore(
        bus_path=tmp_path / "proposals.jsonl",
        db_path=tmp_path / "proposals.db",
    )
    advisor_result = {
        "symbol": "AAPL",
        "asset_class": "equity",
        "timeframe": "1d",
        "as_of": "2026-05-13T16:00:00Z",
        "aggregated_signal": {
            "asset": "AAPL",
            "direction": 1,
            "magnitude": 0.012,
            "confidence": 0.7,
        },
        "target_size_pct_nav": 0.05,
    }
    proposal = store.propose(
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result=advisor_result,
    )
    assert proposal.state == "pending"

    events = _read_audit_events(audit_path, kind="proposal_emitted")
    assert len(events) == 1, f"expected one proposal_emitted row, got {len(events)}"
    payload = events[0].payload
    assert payload["proposal_id"] == proposal.proposal_id
    assert payload["asset"] == "AAPL"
    assert payload["direction"] == 1
    assert payload["target_size_pct_nav"] == pytest.approx(0.05)
    assert events[0].source == "proposals.create"


# ---------------------------------------------------------------------------
# Test 6: gate drops signal with look-ahead-tainted evidence_id
# ---------------------------------------------------------------------------


def test_risk_gate_drops_signal_with_lookahead_tainted_evidence_id(
    audit_path: Path,
    halt_state: HaltStateSQLite,
    evidence_store: EvidenceStore,
) -> None:
    """If a component AnalystView cites evidence whose available_at is in
    the FUTURE relative to signal.asof, the gate must silence with reason
    starting 'lookahead_tainted_'."""
    asof = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    # Available 1h AFTER asof — this is look-ahead bias.
    tainted = _make_bar_evidence(payload=b"future-bar", available_at=asof + timedelta(hours=1))
    evidence_store.append(tainted)

    view = AnalystView(
        analyst="lookahead_test_analyst",
        direction=1,
        magnitude=0.02,
        confidence=0.85,
        confidence_raw=0.9,
        horizon="4h",
        evidence_ids=(str(tainted.id),),
    )
    signal = _make_signal(
        direction=1,
        confidence=0.85,
        components=(view,),
        asof=pd.Timestamp(asof),
    )
    g = DefaultRiskGate(evidence_store=evidence_store)
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)
    assert action is None, "lookahead-tainted signal should be silenced"

    rejection_events = _read_audit_events(audit_path, kind="gate_rejection")
    assert len(rejection_events) == 1
    reason = rejection_events[0].payload["reason"]
    assert reason.startswith("lookahead_tainted_"), reason
    # The counter should reflect the lookahead silence.
    assert g.stats()["n_silenced_lookahead"] == 1


# ---------------------------------------------------------------------------
# Test 7: gate passes a clean signal through the lookahead check
# ---------------------------------------------------------------------------


def test_risk_gate_passes_clean_signal_through_lookahead_check(
    audit_path: Path,
    halt_state: HaltStateSQLite,
    evidence_store: EvidenceStore,
) -> None:
    """Same setup as test 6 but with available_at <= signal.asof: the gate
    must let the signal proceed (no lookahead silence)."""
    asof = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    # Available 1h BEFORE asof — clean.
    clean = _make_bar_evidence(payload=b"past-bar", available_at=asof - timedelta(hours=1))
    evidence_store.append(clean)

    view = AnalystView(
        analyst="clean_test_analyst",
        direction=1,
        magnitude=0.05,
        confidence=0.85,
        confidence_raw=0.9,
        horizon="4h",
        evidence_ids=(str(clean.id),),
    )
    signal = _make_signal(
        direction=1,
        magnitude=0.05,
        confidence=0.85,
        components=(view,),
        asof=pd.Timestamp(asof),
    )
    g = DefaultRiskGate(evidence_store=evidence_store)
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)

    assert action is not None, "clean signal should pass the lookahead gate"
    assert action.target_position_pct > 0
    # Must have emitted a gate_approval (NOT a lookahead rejection)
    approvals = _read_audit_events(audit_path, kind="gate_approval")
    assert len(approvals) == 1
    rejections = _read_audit_events(audit_path, kind="gate_rejection")
    # No lookahead rejection
    assert not any(r.payload.get("reason", "").startswith("lookahead_tainted_") for r in rejections)
    assert g.stats()["n_silenced_lookahead"] == 0


# ---------------------------------------------------------------------------
# Test 8: gate without evidence_store skips lookahead check (backward compat)
# ---------------------------------------------------------------------------


def test_risk_gate_without_evidence_store_skips_lookahead_check(
    audit_path: Path, halt_state: HaltStateSQLite
) -> None:
    """When the gate is constructed without evidence_store, signals carrying
    evidence_ids must pass through (or be evaluated by other rules) as if
    the lookahead-gate didn't exist. Backward-compat for existing tests
    that don't wire an evidence store."""
    asof = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    # Note: this evidence_id NEVER resolves (no store) — gate must not crash.
    fake_uuid = "a3f5a1c2-1234-5678-9abc-def012345678"
    view = AnalystView(
        analyst="phantom_evidence_analyst",
        direction=1,
        magnitude=0.05,
        confidence=0.85,
        confidence_raw=0.9,
        horizon="4h",
        evidence_ids=(fake_uuid,),
    )
    signal = _make_signal(
        direction=1,
        magnitude=0.05,
        confidence=0.85,
        components=(view,),
        asof=pd.Timestamp(asof),
    )
    # No evidence_store kwarg.
    g = DefaultRiskGate()
    assert g.evidence_store is None
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)

    assert action is not None, (
        "without evidence_store, signal with evidence_ids should pass through"
    )
    assert action.target_position_pct > 0
    assert g.stats()["n_silenced_lookahead"] == 0
    approvals = _read_audit_events(audit_path, kind="gate_approval")
    assert len(approvals) == 1
