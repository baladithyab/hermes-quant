"""Tests for ADR-0041 signal_provenance plumbing through risk/gate.py.

Validates the contract:
- gate_approval and gate_rejection audit events MUST carry a
  signal_provenance block with non-null required fields (n_views,
  n_distinct_analysts, contributing_analysts, aggregator_class,
  analyst_view_ids).
- The is_bma_degenerate(event) canonical predicate correctly distinguishes
  the n=1 BMA collapse from legitimate n_distinct=2-with-agreement at conf=1.0.
"""

from __future__ import annotations

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
    """ADR-0041 contract: every gate_approval payload includes signal_provenance."""
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
    """ADR-0041 contract: every gate_rejection payload also includes signal_provenance."""
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
# Fix A6: BMA discriminator must be observable in the audit trail.
#
# These exercise the END-TO-END path through a real BMAAggregator (not a
# hand-built signal) so the n_distinct_analysts / contributing_analysts
# discriminator is provably written through to the approval audit record,
# and a degenerate single-source case is distinguishable from the audit
# trail alone.
# ---------------------------------------------------------------------------


def _ensemble_av(
    analyst: str, *, direction: int = 1, confidence: float = 0.9, horizon: str = "1d"
) -> AnalystView:
    """Like _av but with a magnitude (0.05) large enough that the gate's
    cost-gate is cleared once an identity calibrator preserves confidence —
    so a real BMA aggregate yields an actual gate_approval to audit."""
    return AnalystView(
        analyst=analyst,
        direction=direction,
        magnitude=0.05,
        confidence=confidence,
        confidence_raw=max(confidence, 0.85),
        horizon=horizon,
    )


def _make_context(asset: str = "BTC/USDT"):
    """Minimal MarketContext for driving a real BMAAggregator.aggregate()."""
    from hermes_quant.protocol import MarketContext

    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-05-26", periods=3, freq="1h", tz="UTC"),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000.0, 1100.0, 1200.0],
        }
    )
    return MarketContext(
        asset=asset,
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=103.0,
        last_volume=1200.0,
        asof=pd.Timestamp("2026-05-26T02:00:00Z"),
    )


def test_approval_audit_carries_non_none_discriminator_for_real_bma_ensemble(
    audit_path: Path, halt_state
) -> None:
    """Fix A6: an approval driven by a REAL BMA aggregate with >=2 distinct
    analysts must carry non-None n_distinct_analysts / contributing_analysts
    on the gate_approval audit record. The require_ensemble gate fired
    (n_distinct >= 2) and that MUST be queryable from the audit trail.
    """
    from hermes_quant.aggregators.bma import BMAAggregator
    from hermes_quant.calibrators import IdentityCalibrator

    views = [
        _ensemble_av("ClassicalTA", direction=1, confidence=0.9),
        _ensemble_av("Microstructure", direction=1, confidence=0.85),
    ]
    agg = BMAAggregator()  # require_ensemble=True (default)
    # Identity calibrator: keep calibrated confidence high enough that the
    # gate's cost-gate doesn't silence (cold-start shrinkage would otherwise
    # cap confidence at 0.375 and never produce an approval to audit).
    agg.calibrator = IdentityCalibrator()
    signal = agg.aggregate(views, _make_context())
    # Two distinct analysts agreeing → BMA emits a live (non-flat) signal.
    assert signal.direction != 0, "two agreeing analysts should clear require_ensemble"
    assert signal.aggregator == "bma"

    g = DefaultRiskGate()
    action = g.gate(signal, _make_market(), _make_portfolio(), halt_state)
    assert action is not None and action.target_position_pct > 0

    payloads = _read_payloads(audit_path, "gate_approval")
    assert len(payloads) == 1, f"expected 1 gate_approval, got {len(payloads)}"
    sp = payloads[0].get("signal_provenance")
    assert sp is not None, "signal_provenance MUST be on the gate_approval payload"
    # The discriminator fields must NEVER be None when the aggregator
    # produced them — this is the entire point of Fix A6.
    assert sp["n_distinct_analysts"] is not None
    assert sp["n_distinct_analysts"] == 2
    assert sp["contributing_analysts"] is not None
    assert sorted(sp["contributing_analysts"]) == ["ClassicalTA", "Microstructure"]
    assert sp["n_views"] == 2


def test_audit_trail_distinguishes_single_source_from_ensemble(
    audit_path: Path, halt_state
) -> None:
    """Fix A6: a degenerate single-source case is distinguishable in the
    audit trail from a genuine multi-analyst ensemble.

    A lone analyst run through BMA with require_ensemble=False passes
    through; its approval audit record carries n_distinct_analysts == 1,
    which is exactly the signature is_bma_degenerate / is_n1_collapse can
    query — and which is NOT confusable with the n_distinct >= 2 ensemble.
    """
    from hermes_quant.aggregators.bma import BMAAggregator
    from hermes_quant.calibrators import IdentityCalibrator
    from hermes_quant.governance.audit_log_query import is_n1_collapse

    # Single source: one distinct analyst. require_ensemble=False so BMA
    # passes it through rather than silencing (research/degenerate config).
    lone_view = [_ensemble_av("Kronos", direction=1, confidence=0.95)]
    agg = BMAAggregator(require_ensemble=False)
    agg.calibrator = IdentityCalibrator()
    signal = agg.aggregate(lone_view, _make_context())
    assert signal.direction != 0

    g = DefaultRiskGate()
    g.gate(signal, _make_market(), _make_portfolio(), halt_state)

    payloads = _read_payloads(audit_path, "gate_approval")
    assert len(payloads) == 1
    sp = payloads[0]["signal_provenance"]
    # Single-source signature: explicitly n_distinct == 1, NOT None and NOT 2.
    assert sp["n_distinct_analysts"] == 1
    assert sp["contributing_analysts"] == ["Kronos"]
    assert sp["n_distinct_analysts"] != 2

    # And it is forensically distinguishable: reconstruct the event shape
    # that is_n1_collapse consumes. (We don't assert it fires unless conf
    # saturated — we assert the DISCRIMINATOR is present and usable.)
    ev = {"kind": "gate_approval", "payload": payloads[0]}
    # The predicate is well-defined (returns a bool, not erroring on None).
    assert isinstance(is_n1_collapse(ev), bool)


def test_provenance_falls_back_to_metadata_when_components_stripped() -> None:
    """Fix A6 root-cause: a signal carrying authoritative aggregator metadata
    counts but EMPTY components must still produce non-None discriminator
    fields. The old implementation recomputed solely from components and
    silently wrote n_distinct_analysts=0 / contributing_analysts=[] — the
    blind spot that surfaced as None in the audit trail.
    """
    signal = _make_signal(
        direction=1,
        components=(),  # stripped (e.g. serialized-then-reconstructed signal)
        metadata={
            "weights": {"ClassicalTA": 0.6, "Kronos": 0.4},
            "n_views": 2,
            "n_contributing": 2,
            "vote_share": 1.0,
        },
    )
    sp = _build_signal_provenance(signal)
    assert sp["n_distinct_analysts"] == 2
    assert sp["contributing_analysts"] == ["ClassicalTA", "Kronos"]
    assert sp["n_views"] == 2
    # Aggregator metadata still propagates as before.
    assert sp["vote_share"] == 1.0
    assert sp["n_contributing"] == 2


def test_provenance_none_when_neither_components_nor_metadata_counts() -> None:
    """Honest None: when an aggregator produced NEITHER components NOR
    countable metadata, the discriminator is None (not a misleading 0).
    is_pre_provenance_schema / operators can treat that as 'unknown'.
    """
    signal = _make_signal(direction=1, components=(), metadata={"reason": "flat"})
    sp = _build_signal_provenance(signal)
    assert sp["n_distinct_analysts"] is None
    assert sp["contributing_analysts"] == []
    assert sp["n_views"] is None


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
    """Pre-ADR-0041 events have no signal_provenance block. Predicate
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
