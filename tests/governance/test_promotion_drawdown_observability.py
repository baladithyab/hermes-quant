"""Observability-of-silence regression: the paper→live promotion gate must SEE a
realized drawdown that the risk-gate drawdown circuit breaker silenced/halted.

Bug class (sibling of ar77, which fixed the never-written `immutable_breach`
flag): the gate's `rolling_30d_max_drawdown_pct > max` block read a
`promotion_event` payload field that NO live producer emits
(`weekly_retro.emit_promotion_readiness` emits only readiness/belief counts), so
the value stayed at its 0.0 default and the block was VACUOUS — a strategy that
tripped a real drawdown circuit breaker was NOT blocked from promotion
(latent fail-OPEN).

Fix: derive the drawdown magnitude from the `drawdown_circuit_breaker_{pct}`
`gate_rejection` reason that risk/gate.py:544 actually emits, so the silenced
drawdown becomes observable to the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log, promotion
from hermes_quant.governance.audit_log import GovernanceEvent
from hermes_quant.memory import weekly_retro

NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


def _seed_otherwise_passing(asof: datetime) -> None:
    """Seed a run that passes every OTHER gate check using ONLY the shapes the
    LIVE producers actually emit (paper fills + weekly_retro readiness)."""
    for _ in range(100):
        audit_log.append(GovernanceEvent(
            kind="fill", asof=asof - timedelta(days=15), source="paper_reactor",
            payload={"broker": "paper", "realized_pnl": 1.0},
        ))
    # Sharpe-CI cleared (the gate reads this from a promotion_event; seed the
    # shape a meta-retro would write so the test isolates the drawdown axis).
    audit_log.append(GovernanceEvent(
        kind="promotion_event", asof=asof - timedelta(days=1), source="meta_retro",
        payload={"sharpe_95ci_lower": 1.25},
    ))
    # The LIVE weekly_retro producer emit (readiness only — no drawdown/drift/sharpe).
    weekly_retro.emit_promotion_readiness(
        weekly_retro.WeeklyRetroResult(
            asof=asof.isoformat(), n_reflections_read=0, beliefs_distilled=0,
            beliefs_expired=0, active_belief_count=0, under_budget=True,
            promotion_readiness_emitted=False,
        ),
        asof - timedelta(days=1),
    )


def test_drawdown_breaker_in_window_blocks_promotion(audit_path: Path) -> None:
    """A drawdown circuit breaker (the way risk/gate.py emits it) WITHIN the 30d
    window must surface as the gate's rolling_30d_max_drawdown_pct and BLOCK
    promotion. NON-VACUITY: assert the drawdown magnitude is observed AND that it
    is among the blockers — this is the RED→GREEN line."""
    _seed_otherwise_passing(NOW)
    # risk/gate.py:544 — reason encodes the realized drawdown magnitude.
    audit_log.append(GovernanceEvent(
        kind="gate_rejection", asof=NOW - timedelta(days=5), source="risk.gate",
        payload={"reason": "drawdown_circuit_breaker_0.3000"},
    ))

    decision = promotion.evaluate(NOW)

    # The silenced drawdown is now OBSERVABLE to the gate (the fix).
    assert decision.rolling_30d_max_drawdown_pct == pytest.approx(0.30)
    # And it BLOCKS — closing the fail-open (RED before the fix: not blocked).
    assert decision.promoted is False
    assert any("rolling_30d_max_drawdown_pct" in r for r in decision.blocked_by)


def test_no_breaker_is_byte_identical_zero(audit_path: Path) -> None:
    """Byte-identical happy path: with NO drawdown breaker on the log, the derived
    drawdown stays 0.0 and the drawdown check does not contribute a blocker."""
    _seed_otherwise_passing(NOW)
    decision = promotion.evaluate(NOW)
    assert decision.rolling_30d_max_drawdown_pct == 0.0
    assert not any("rolling_30d_max_drawdown_pct" in r for r in decision.blocked_by)


def test_breaker_outside_30d_window_is_ignored(audit_path: Path) -> None:
    """A drawdown breaker OLDER than 30 days must NOT be counted (window bound)."""
    _seed_otherwise_passing(NOW)
    audit_log.append(GovernanceEvent(
        kind="gate_rejection", asof=NOW - timedelta(days=45), source="risk.gate",
        payload={"reason": "drawdown_circuit_breaker_0.5000"},
    ))
    decision = promotion.evaluate(NOW)
    assert decision.rolling_30d_max_drawdown_pct == 0.0


def test_explicit_promotion_event_drawdown_still_honored(audit_path: Path) -> None:
    """An explicit rolling_30d_max_drawdown_pct on a promotion_event (the original
    contract) is still honored, and the gate takes the MAX of it and any breaker."""
    _seed_otherwise_passing(NOW)
    audit_log.append(GovernanceEvent(
        kind="promotion_event", asof=NOW - timedelta(days=2), source="meta_retro",
        payload={"rolling_30d_max_drawdown_pct": 0.02},
    ))
    audit_log.append(GovernanceEvent(
        kind="gate_rejection", asof=NOW - timedelta(days=3), source="risk.gate",
        payload={"reason": "drawdown_circuit_breaker_0.0800"},
    ))
    decision = promotion.evaluate(NOW)
    assert decision.rolling_30d_max_drawdown_pct == pytest.approx(0.08)  # max(0.02, 0.08)
    assert decision.promoted is False
