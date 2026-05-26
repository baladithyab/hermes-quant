"""Tests for hermes_quant.governance.promotion (ADR-0031 D5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log, promotion
from hermes_quant.governance.audit_log import GovernanceEvent


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


# Anchor for "now". asof in tests is this constant; we synthesize events at
# offsets relative to it.
NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _seed_passing_run(asof: datetime, n_outcomes: int = 100) -> None:
    """Write enough synthetic events that every threshold passes."""
    for i in range(n_outcomes):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=asof - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0 + (i % 3) * 0.1},
            )
        )

    # Calibrator + sharpe + drawdown + retro-readiness snapshot
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=asof - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.02,
                "sharpe_95ci_lower": 1.25,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
            },
        )
    )


def test_promotion_gate_blocks_when_outcomes_below_adr29_threshold(
    audit_path: Path,
) -> None:
    """99 settled outcomes → blocked."""
    _seed_passing_run(NOW, n_outcomes=99)
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("paper_outcomes_count=99" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_calibrator_drift_gt_5pct(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(hours=1),
            source="weekly_retro",
            payload={"calibrator_drift": 0.06},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("calibrator_drift_max" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_immutable_breach_count_nonzero(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="gate_rejection",
            asof=NOW - timedelta(days=10),
            source="risk_gate",
            payload={"reason": "net_delta_cap", "immutable_breach": True},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("immutable_breaches_in_window" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_killswitch_in_14d_window(
    audit_path: Path,
) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    audit_log.append(
        GovernanceEvent(
            kind="kill_switch_fired",
            asof=NOW - timedelta(days=5),
            source="risk_gate",
            payload={"reason": "drawdown"},
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("kill switch fired" in r for r in decision.blocked_by)


def test_promotion_gate_blocks_when_retro_readiness_false(
    audit_path: Path,
) -> None:
    # Seed everything except the retro-readiness snapshot
    for i in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0},
            )
        )
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.01,
                "sharpe_95ci_lower": 1.5,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": False,
            },
        )
    )
    decision = promotion.evaluate(NOW)
    assert decision.promoted is False
    assert any("weekly_retro_promotion_readiness" in r for r in decision.blocked_by)


def test_promotion_gate_passes_when_all_thresholds_pass(audit_path: Path) -> None:
    _seed_passing_run(NOW, n_outcomes=100)
    decision = promotion.evaluate(NOW)
    assert decision.promoted is True, f"blocked_by={decision.blocked_by}"
    assert decision.blocked_by == []
    assert decision.paper_outcomes_count >= 100
    assert decision.no_killswitch_in_trailing_14d is True
    assert decision.weekly_retro_promotion_readiness is True


def test_promotion_gate_emits_audit_event(audit_path: Path) -> None:
    _seed_passing_run(NOW, n_outcomes=100)

    before_rows = list(audit_log.read(kinds=["promotion_event"]))
    n_before = sum(1 for r in before_rows if r.payload.get("row_type") == "evaluate_result")

    promotion.evaluate(NOW)

    after_rows = list(audit_log.read(kinds=["promotion_event"]))
    n_after = sum(1 for r in after_rows if r.payload.get("row_type") == "evaluate_result")
    assert n_after == n_before + 1


def test_promotion_uses_late_bind_thresholds_when_react_live_missing() -> None:
    """The fallback path is exercised whenever hermes_quant.react.live
    is not importable. After react.live lands the live path is used; this
    test still asserts the THRESHOLD VALUES match because react.live is
    the single source of truth and the fallback mirrors it."""
    thresholds = promotion._load_thresholds()
    assert thresholds["min_paper_outcomes"] == 100
    assert thresholds["min_sharpe_95ci_lower"] == 1.0
    assert thresholds["max_rolling_30d_drawdown_pct"] == 0.01
    assert thresholds["max_calibrator_drift"] == 0.05


def test_promotion_threshold_path_actually_uses_react_live() -> None:
    """Integration test: with both modules present, _load_thresholds()
    must return the dict from react.live, NOT the local fallback. We
    verify by mutating react.live's dict and observing the change."""
    from hermes_quant.react import live as react_live

    # Sanity: both modules are importable now
    assert hasattr(react_live, "LIVE_APPROVAL_THRESHOLDS")

    # The wire works: bumping a number in react.live shows up here
    original = react_live.LIVE_APPROVAL_THRESHOLDS["min_paper_outcomes"]
    try:
        react_live.LIVE_APPROVAL_THRESHOLDS["min_paper_outcomes"] = 999
        thresholds = promotion._load_thresholds()
        assert thresholds["min_paper_outcomes"] == 999, (
            "Late-bind isn't actually wired — _load_thresholds is "
            "returning the local _LATE_BIND_THRESHOLDS sentinel instead "
            "of pulling from react.live."
        )
    finally:
        react_live.LIVE_APPROVAL_THRESHOLDS["min_paper_outcomes"] = original


def test_promotion_threshold_keys_match_late_bind_keys() -> None:
    """Both naming styles in react.live must include every key
    governance.promotion expects — missing keys silently fall back."""
    from hermes_quant.react import live as react_live

    required_keys = {
        "min_paper_outcomes",
        "min_sharpe_95ci_lower",
        "max_rolling_30d_drawdown_pct",
        "max_calibrator_drift",
        "killswitch_window_days",
        "immutable_breach_window_days",
    }
    actual = set(react_live.LIVE_APPROVAL_THRESHOLDS.keys())
    missing = required_keys - actual
    assert not missing, (
        f"react.live.LIVE_APPROVAL_THRESHOLDS is missing keys that "
        f"governance.promotion expects: {missing}. Either add them to "
        f"react.live or rename governance.promotion's lookups."
    )
