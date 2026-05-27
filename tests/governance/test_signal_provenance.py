"""Tests for ADR-0039 signal_provenance plumbing through risk/gate.py.

Validates the contract:
- gate_approval and gate_rejection audit events MUST carry a
  signal_provenance block with non-null required fields (n_views,
  n_distinct_analysts, contributing_analysts, aggregator_class,
  analyst_view_ids).
- The is_bma_degenerate(event) canonical predicate correctly distinguishes
  the n=1 BMA collapse from legitimate n_distinct=2-with-agreement at conf=1.0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.governance import audit_log
from hermes_quant.governance.audit_log_query import (
    coverage_summary,
    find_degenerate,
    is_bma_degenerate,
    is_pre_provenance_schema,
)
from hermes_quant.protocol import (
    AggregatedSignal,
    AnalystView,
    HaltState,
    MarketState,
    Portfolio,
)
from hermes_quant.risk.gate import DefaultRiskGate, _build_signal_provenance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a temp file for the duration of one test."""
    p = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


@pytest.fixture
def halt_state() -> HaltState:
    """In-memory halt-state stub: nothing halted."""

    class _NoHalt:
        def is_halted(self, account_id: str, asset_class: str, asset: str) -> bool:
            return False

    return _NoHalt()  # type: ignore[return-value]


def _av(
    analyst: str,
    *,
    direction: int = 1,
    horizon: str = "1d",
    confidence: float = 0.85,
    metadata: dict | None = None,
) -> AnalystView:
    return AnalystView(
        analyst=analyst,
        direction=direction,
        magnitude=0.02,
        confidence=confidence,
        confidence_raw=max(confidence, 0.85),
        horizon=horizon,
        metadata=metadata or None,
    )


def _make_signal(
    *,
    direction: int = 1,
    magnitude: float = 0.05,
    confidence: float = 0.85,
    asset: str = "BTC/USDT",
    components: tuple[AnalystView, ...] = (),
    aggregator: str = "bma",
    metadata: dict | None = None,
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-05-27T00:00:00Z"),
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=max(confidence, 0.85),
        horizon="4h",
        components=components,
        aggregator=aggregator,
        metadata=metadata,
    )


def _make_market() -> MarketState:
    return MarketState(
        asset="BTC/USDT",
        asof=pd.Timestamp("2026-05-27T00:00:00Z"),
        volatility=0.02,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0012,
        tz="UTC",
    )


def _make_portfolio(*, drawdown: float = 0.0) -> Portfolio:
    equity = 100_000.0
    peak = equity / max(1e-9, 1 - drawdown)
    return Portfolio(
        account_id="alpaca-paper",
        asset_class="crypto",
        asof=pd.Timestamp("2026-05-27T00:00:00Z"),
        positions={},
        cash=equity,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=peak,
        daily_open_equity=equity,
    )


# ---------------------------------------------------------------------------
# _build_signal_provenance unit tests
# ---------------------------------------------------------------------------


def test_provenance_required_fields_with_two_distinct_analysts() -> None:
    """Two analysts in components → n_distinct=2, contributing_analysts sorted."""
    components = (
        _av("ClassicalTA", direction=1),
        _av("Kronos", direction=1),
    )
    signal = _make_signal(components=components)
    sp = _build_signal_provenance(signal)
    assert sp["n_views"] == 2
    assert sp["n_distinct_analysts"] == 2
    assert sp["contributing_analysts"] == ["ClassicalTA", "Kronos"]
    assert sp["aggregator_class"] == "bma"
    assert len(sp["analyst_view_ids"]) == 2


def test_provenance_distinguishes_n_distinct_from_n_views() -> None:
    """Same analyst on two horizons → n_views=2 BUT n_distinct_analysts=1.

    This is the discriminator for the BMA degeneracy: same-analyst-multi-horizon
    is still single-source, regardless of how many views entered aggregation.
    """
    components = (
        _av("Kronos", horizon="1d"),
        _av("Kronos", horizon="1w"),
    )
    signal = _make_signal(components=components)
    sp = _build_signal_provenance(signal)
    assert sp["n_views"] == 2
    assert sp["n_distinct_analysts"] == 1
    assert sp["contributing_analysts"] == ["Kronos"]


def test_provenance_pulls_bma_metadata_when_present() -> None:
    """Aggregator's metadata fields (vote_share, n_contributing, bma_weights) propagate."""
    components = (_av("ClassicalTA"), _av("Kronos"))
    signal = _make_signal(
        components=components,
        metadata={
            "vote_share": 1.0,
            "n_contributing": 2,
            "bma_weights": {"ClassicalTA": 0.6, "Kronos": 0.4},
        },
    )
    sp = _build_signal_provenance(signal)
    assert sp["vote_share"] == 1.0
    assert sp["n_contributing"] == 2
    assert sp["bma_weights"] == {"ClassicalTA": 0.6, "Kronos": 0.4}


def test_provenance_required_fields_default_safely_when_metadata_missing() -> None:
    """Missing metadata → vote_share/n_contributing/bma_weights are None,
    BUT n_views/n_distinct/contributing_analysts/aggregator_class STAY populated."""
    components = (_av("ClassicalTA"),)
    signal = _make_signal(components=components, metadata=None)
    sp = _build_signal_provenance(signal)
    assert sp["vote_share"] is None
    assert sp["n_contributing"] is None
    assert sp["bma_weights"] is None
    # Required-non-null fields:
    assert sp["n_views"] == 1
    assert sp["n_distinct_analysts"] == 1
    assert sp["contributing_analysts"] == ["ClassicalTA"]
    assert sp["aggregator_class"] == "bma"


def test_provenance_uses_view_id_metadata_when_present() -> None:
    """When AnalystView.metadata carries view_id, propagate it; else use analyst:horizon."""
    components = (
        _av("ClassicalTA", metadata={"view_id": "view_a3f9"}),
        _av("Kronos"),  # no view_id
    )
    signal = _make_signal(components=components)
    sp = _build_signal_provenance(signal)
    assert "view_a3f9" in sp["analyst_view_ids"]
    # The fallback form must appear for the second view:
    assert any(":1d" in vid for vid in sp["analyst_view_ids"])


# ---------------------------------------------------------------------------
# Gate-emit integration: provenance MUST appear in audit-log payloads
# ---------------------------------------------------------------------------


def _read_payloads(path: Path, kind: str) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ev in audit_log.read(kinds=[kind]):
        out.append(ev.payload)
    return out


def test_gate_approval_carries_signal_provenance(audit_path: Path, halt_state) -> None:
    """ADR-0039 contract: every gate_approval payload includes signal_provenance."""
    components = (_av("ClassicalTA", direction=1), _av("Kronos", direction=1))
    signal = _make_signal(direction=1, confidence=0.85, components=components)
    g = DefaultRiskGate()
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)
    assert action is not None and action.target_position_pct > 0

    payloads = _read_payloads(audit_path, "gate_approval")
    assert len(payloads) == 1, f"expected 1 gate_approval, got {len(payloads)}"
    sp = payloads[0].get("signal_provenance")
    assert sp is not None, "signal_provenance MUST be on every gate_approval payload"
    assert sp["n_views"] == 2
    assert sp["n_distinct_analysts"] == 2
    assert sp["contributing_analysts"] == ["ClassicalTA", "Kronos"]
    assert sp["aggregator_class"] == "bma"


def test_gate_rejection_carries_signal_provenance(audit_path: Path, halt_state) -> None:
    """ADR-0039 contract: every gate_rejection payload also includes signal_provenance."""
    components = (_av("Kronos", direction=1),)
    signal = _make_signal(direction=1, confidence=0.85, components=components)
    g = DefaultRiskGate()
    # Drawdown > 0.15 → drawdown breaker → gate_rejection
    portfolio = _make_portfolio(drawdown=0.20)
    g.gate(signal, _make_market(), portfolio, halt_state)
    payloads = _read_payloads(audit_path, "gate_rejection")
    assert len(payloads) >= 1
    sp = payloads[0].get("signal_provenance")
    assert sp is not None, "signal_provenance MUST be on every gate_rejection payload"
    assert sp["n_views"] == 1
    assert sp["n_distinct_analysts"] == 1


# ---------------------------------------------------------------------------
# is_bma_degenerate canonical predicate
# ---------------------------------------------------------------------------


def _approval_event(
    *,
    aggregator: str = "BMAAggregator",
    n_distinct: int = 1,
    confidence: float = 1.0,
    asset: str = "MRNA",
    asof: str = "2026-05-26T04:00:00+00:00",
) -> dict:
    return {
        "kind": "gate_approval",
        "asof": asof,
        "source": "risk.gate",
        "payload": {
            "asset": asset,
            "direction": -1,
            "magnitude": 0.045,
            "confidence": confidence,
            "target_position_pct": -0.2,
            "reason": "approve",
            "asof": asof,
            "signal_provenance": {
                "n_views": 1,
                "n_distinct_analysts": n_distinct,
                "contributing_analysts": ["Kronos"] if n_distinct == 1 else ["Kronos", "ClassicalTA"],
                "vote_share": 1.0,
                "n_contributing": n_distinct,
                "bma_weights": None,
                "aggregator_class": aggregator,
                "analyst_view_ids": ["Kronos:1d"],
                "data_quality": None,
            },
        },
    }


def test_is_bma_degenerate_flags_n1_collapse() -> None:
    """The 2026-05-26 incident signature: BMA aggregator + n_distinct=1 + conf=1.0."""
    ev = _approval_event(aggregator="BMAAggregator", n_distinct=1, confidence=1.0)
    assert is_bma_degenerate(ev) is True


def test_is_bma_degenerate_does_not_flag_legitimate_two_analyst_agreement() -> None:
    """Two distinct analysts both agreeing at conf=1.0 is the legitimate
    saturation case, NOT degeneracy. Predicate must return False."""
    ev = _approval_event(aggregator="BMAAggregator", n_distinct=2, confidence=1.0)
    assert is_bma_degenerate(ev) is False


def test_is_bma_degenerate_handles_lowercase_aggregator_alias() -> None:
    """signal.aggregator field convention is 'bma' (lowercase). Predicate
    must recognize both 'BMAAggregator' (class name) and 'bma' (field)."""
    ev = _approval_event(aggregator="bma", n_distinct=1, confidence=1.0)
    assert is_bma_degenerate(ev) is True


def test_is_bma_degenerate_returns_false_on_rejection_kind() -> None:
    ev = _approval_event(aggregator="BMAAggregator", n_distinct=1, confidence=1.0)
    ev["kind"] = "gate_rejection"
    assert is_bma_degenerate(ev) is False


def test_is_bma_degenerate_returns_false_on_pre_provenance_event() -> None:
    """Pre-ADR-0039 events have no signal_provenance block. Predicate
    must NOT flag these (we don't know if they're degenerate from the
    audit alone)."""
    ev = _approval_event()
    ev["payload"].pop("signal_provenance")
    assert is_bma_degenerate(ev) is False


def test_is_bma_degenerate_tolerates_fp_wobble_around_1() -> None:
    """Confidence 0.9999 should still be flagged (FP wobble around 1.0)."""
    ev = _approval_event(aggregator="BMAAggregator", n_distinct=1, confidence=0.9999)
    assert is_bma_degenerate(ev) is True


def test_is_bma_degenerate_does_not_flag_high_but_not_unanimous_confidence() -> None:
    """Confidence 0.95 with n_distinct=1 is suspicious but NOT the
    canonical n=1-collapse signature this predicate names. Other
    diagnostics handle that case."""
    ev = _approval_event(aggregator="BMAAggregator", n_distinct=1, confidence=0.95)
    assert is_bma_degenerate(ev) is False


def test_is_pre_provenance_schema_flags_old_events() -> None:
    ev = _approval_event()
    assert is_pre_provenance_schema(ev) is False
    ev["payload"].pop("signal_provenance")
    assert is_pre_provenance_schema(ev) is True


def test_find_degenerate_filters_by_asof_prefix(tmp_path: Path) -> None:
    """The CLI filter mode lets operators scope to a single incident date."""
    p = tmp_path / "audit_log.jsonl"
    e1 = _approval_event(asof="2026-05-26T04:00:00+00:00")
    e2 = _approval_event(asof="2026-05-27T04:00:00+00:00")
    e2_legit = _approval_event(n_distinct=2, asof="2026-05-27T05:00:00+00:00")
    import json as _json

    p.write_text(
        "\n".join(_json.dumps(e) for e in (e1, e2, e2_legit)) + "\n",
        encoding="utf-8",
    )
    on_05_26 = find_degenerate(p, asof_prefix="2026-05-26")
    on_05_27 = find_degenerate(p, asof_prefix="2026-05-27")
    all_dates = find_degenerate(p)
    assert len(on_05_26) == 1
    assert len(on_05_27) == 1  # e2_legit excluded because n_distinct=2
    assert len(all_dates) == 2


def test_coverage_summary_counts_pre_and_post_provenance(tmp_path: Path) -> None:
    p = tmp_path / "audit_log.jsonl"
    new_ev = _approval_event()
    old_ev = _approval_event()
    old_ev["payload"].pop("signal_provenance")
    rejection_old = {
        "kind": "gate_rejection",
        "asof": "2026-05-13T00:00:00+00:00",
        "source": "risk.gate",
        "payload": {"asset": "BTC", "direction": 1, "reason": "halt_active"},
    }
    import json as _json

    p.write_text(
        "\n".join(_json.dumps(e) for e in (new_ev, old_ev, rejection_old)) + "\n",
        encoding="utf-8",
    )
    c = coverage_summary(p)
    assert c["total_events"] == 3
    assert c["gate_approvals"] == 2
    assert c["gate_rejections"] == 1
    assert c["with_provenance"] == 1
    assert c["without_provenance"] == 2
    assert c["degenerate"] == 1  # only new_ev is flagged
