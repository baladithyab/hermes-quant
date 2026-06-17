"""tests/governance/test_promotion_ar125_settlement_derived.py

ar125 — RED->GREEN tests for the two structurally-vacuous sub-gates.

Before the fix:
  (1) paper_outcomes_count counted kind='fill' events with broker='paper'.
      No producer ever emits kind='fill' in production → always 0 → always blocks.
  (2) sharpe_95ci_lower read from promotion_event.payload['sharpe_95ci_lower'].
      No producer ever emits that field in production → always 0.0 → always blocks.

After the fix:
  (1) paper_outcomes_count ALSO derives from the canonical settlement ledger
      (settlement_loop.join_exit_fills on executions.jsonl, filtered to
      account_id='paper-default').
  (2) sharpe_95ci_lower ALSO derives a 95% CI bootstrap lower bound from the
      settled round-trip return series when no promotion_event snapshot exists.

Tests verify:
  A. A synthetic executions.jsonl with >=min_paper_outcomes profitable settled
     paper-default round trips + sufficient Sharpe CAN pass those two sub-gates
     (was impossible before fix — RED).
  B. A thin book (<min_paper_outcomes) still BLOCKS.
  C. A losing book (returns with negative mean → negative Sharpe) still BLOCKS.
  D. Backward compat: existing promotion_event snapshot wins over settlement-derived
     CI when an in-window snapshot exists (test that uses fill-kind events + a
     promotion_event with sharpe_95ci_lower still passes unchanged).
  E. An empty executions.jsonl (no records) → settlement count = 0 → correctly
     blocks if fill-kind events also absent.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.governance import audit_log, promotion
from hermes_quant.governance.audit_log import GovernanceEvent

# All asof values in tests are relative to this anchor.
NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)

# Threshold from react.live (100 paper outcomes, sharpe_95ci_lower >= 1.0).
MIN_OUTCOMES = 100
SHARPE_FLOOR = 1.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", p)
    return p


def _make_exec_record(
    asset: str,
    fill_size_pct: float,
    fill_price: float,
    asof_execution: str,
    *,
    asset_class: str = "equity",
    account_id: str = "paper-default",
) -> dict:
    """Build a minimal real-bus ExecutionRecord dict (PaperReactor._record_to_dict shape)."""
    return {
        "proposal_id": f"p-{asset}-{asof_execution[:10]}",
        "signal_id": f"s-{asset}-{asof_execution[:10]}",
        "asset": asset,
        "asset_class": asset_class,
        "timeframe": "1d",
        "asof_decision": asof_execution,
        "asof_execution": asof_execution,
        "target_position_pct": fill_size_pct,
        "decision_price": fill_price,
        "fill_price": fill_price,
        "fill_size_pct": fill_size_pct,
        "reactor_name": "paper",
        "human_in_the_loop": False,
        "approver_user_id": None,
        "reactor_metadata": {"account_id": account_id},
        "bar_ts": asof_execution,
        "play_tag": None,
        "schema_version": None,
    }


def _write_executions(bus_path: Path, records: list[dict]) -> None:
    """Write records to executions.jsonl atomically (creates parent dirs)."""
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    with bus_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _seed_all_passing_non_sharpe_gates(asof: datetime) -> None:
    """Write the events needed to pass ALL gates EXCEPT paper_outcomes and sharpe.

    This seeds:
    - No kill_switch_fired in 14d window.
    - No immutable breaches.
    - calibrator_drift <= 0.05.
    - weekly_retro_promotion_readiness=True.
    - rolling_30d_max_drawdown_pct=0.005 (well below 0.01 max).
    But does NOT seed fill-kind events or sharpe_95ci_lower snapshots — those
    are the two structurally-vacuous gates being tested.
    """
    # One promotion_event with everything EXCEPT sharpe_95ci_lower.
    # This seeds the other gates (drawdown, drift, retro-readiness) so
    # only paper_outcomes and sharpe remain as potential blockers.
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=asof - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.02,
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
                # Intentionally NO 'sharpe_95ci_lower' key — mirrors production.
            },
        )
    )


def _make_profitable_round_trips(
    n: int,
    asof: datetime,
    bus_path: Path,
    *,
    entry_price: float = 100.0,
    exit_price: float = 110.0,  # 10% gain
    asset_base: str = "TEST",
    asset_class: str = "equity",
) -> None:
    """Write n profitable paper-default round trips to executions.jsonl.

    Each round trip is: one BUY fill (opens) + one SELL fill (closes).
    Settled realized_return = (exit_price - entry_price) / entry_price = 0.10.
    All exits are within the 30d window (day-15 to day-1 relative to asof).
    """
    records = []
    for i in range(n):
        # Spread exits across the 30d window to avoid same-asof collisions.
        # Use a simple deterministic offset: each trade is 1 hour apart.
        # Entry is always 2 days before exit to ensure asof_entry < asof_exit.
        exit_time = asof - timedelta(days=15) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)

        asset = f"{asset_base}{i}"
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=+0.01,  # buy 1% of NAV
                fill_price=entry_price,
                asof_execution=entry_time.isoformat(),
                asset_class=asset_class,
            )
        )
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=-0.01,  # sell 1% of NAV (closes the long)
                fill_price=exit_price,
                asof_execution=exit_time.isoformat(),
                asset_class=asset_class,
            )
        )
    _write_executions(bus_path, records)


# ---------------------------------------------------------------------------
# A. PRIMARY GREEN path: settlement-derived count + CI clears the two gates
# ---------------------------------------------------------------------------


def test_ar125_settlement_derived_outcomes_count_passes_when_enough_trades(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 — GREEN: >=100 profitable settled paper-default round trips in the
    30d window clears the paper_outcomes_count gate.

    Before the fix this ALWAYS blocked (count was structurally 0 from the fill-kind
    path which no producer writes). Now the settlement-derived count is the primary
    source.
    """
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    # Write 120 round trips so we're well above the 100 threshold.
    _make_profitable_round_trips(120, NOW, bus_path)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["paper_outcomes_count"] >= 100, (
        f"ar125: settlement-derived paper_outcomes_count={metrics['paper_outcomes_count']} "
        f"< 100; fix did not wire the settlement path"
    )


def test_ar125_settlement_derived_sharpe_ci_passes_when_book_earns_it(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 — GREEN: >=100 profitable settled round trips produce a sharpe_95ci_lower
    that clears the >=1.0 floor.

    Before the fix sharpe_95ci_lower was always 0.0 (no promotion_event wrote the
    field). Now the settlement-derived bootstrap CI is used when no in-window
    promotion_event snapshot exists.
    """
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    # 200 profitable round trips: 10% return each → mean=0.10, std very small
    # (all identical returns) → effectively infinite point Sharpe → CI lower bound
    # well above 1.0.
    # We need std > 0 to avoid the zero-variance branch returning 0.0, so use a
    # mix of return magnitudes.
    records = []
    for i in range(200):
        # Alternate between 8% and 12% return so std > 0.
        exit_price = 108.0 if i % 2 == 0 else 112.0
        exit_time = NOW - timedelta(days=15) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)
        asset = f"ARB{i}"
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=+0.01,
                fill_price=100.0,
                asof_execution=entry_time.isoformat(),
            )
        )
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=-0.01,
                fill_price=exit_price,
                asof_execution=exit_time.isoformat(),
            )
        )
    _write_executions(bus_path, records)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    ci_lower = metrics["sharpe_95ci_lower"]
    assert math.isfinite(ci_lower), f"ar125: sharpe_95ci_lower={ci_lower!r} is non-finite"
    assert ci_lower >= SHARPE_FLOOR, (
        f"ar125: sharpe_95ci_lower={ci_lower:.4f} < {SHARPE_FLOOR}; "
        f"a book with consistently positive returns should clear the CI floor"
    )


def test_ar125_evaluate_promotes_with_healthy_settlement_book(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 — GREEN (end-to-end): evaluate() PROMOTES a healthy paper book.

    Before the fix: always blocked on paper_outcomes_count=0 AND sharpe_95ci_lower=0.0.
    After the fix: a book with >=100 profitable settled paper-default round trips
    AND a strong realized-return series passes BOTH structurally-vacuous gates.
    The other gates are seeded passing via _seed_all_passing_non_sharpe_gates.
    """
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"

    # 200 round trips alternating 8%/12% returns (std > 0, all positive).
    records = []
    for i in range(200):
        exit_price = 108.0 if i % 2 == 0 else 112.0
        exit_time = NOW - timedelta(days=15) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)
        asset = f"GRN{i}"
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=+0.01,
                fill_price=100.0,
                asof_execution=entry_time.isoformat(),
            )
        )
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=-0.01,
                fill_price=exit_price,
                asof_execution=exit_time.isoformat(),
            )
        )
    _write_executions(bus_path, records)

    # Must inject executions_path; evaluate() calls _collect_metrics internally.
    # We monkeypatch _collect_metrics so it passes executions_path through.
    import unittest.mock as _mock

    original_collect = promotion._collect_metrics

    def _collect_with_path(asof_arg: datetime) -> dict:
        return original_collect(asof_arg, executions_path=bus_path)

    with _mock.patch.object(promotion, "_collect_metrics", side_effect=_collect_with_path):
        decision = promotion.evaluate(NOW)

    assert decision.promoted is True, (
        f"ar125: evaluate() still blocked on a healthy settlement book. "
        f"blocked_by={decision.blocked_by}"
    )
    assert decision.paper_outcomes_count >= 100
    assert decision.sharpe_95ci_lower >= SHARPE_FLOOR


# ---------------------------------------------------------------------------
# B. Thin-book: fewer than min_paper_outcomes still BLOCKS
# ---------------------------------------------------------------------------


def test_ar125_thin_book_still_blocks_on_outcomes_count(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 fail-CLOSED: 50 round trips (< 100 threshold) must still block."""
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    _make_profitable_round_trips(50, NOW, bus_path)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["paper_outcomes_count"] < MIN_OUTCOMES, (
        f"ar125: 50 round trips gave paper_outcomes_count={metrics['paper_outcomes_count']} "
        f">= {MIN_OUTCOMES}; something is overcounting"
    )

    import unittest.mock as _mock
    original_collect = promotion._collect_metrics
    def _collect_with_path(asof_arg: datetime) -> dict:
        return original_collect(asof_arg, executions_path=bus_path)
    with _mock.patch.object(promotion, "_collect_metrics", side_effect=_collect_with_path):
        decision = promotion.evaluate(NOW)

    assert decision.promoted is False
    assert any("paper_outcomes_count" in r for r in decision.blocked_by), (
        f"ar125: thin book did not block on outcomes_count; blocked_by={decision.blocked_by}"
    )


def test_ar125_thin_book_sharpe_ci_returns_zero(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 fail-CLOSED: fewer than _MIN_ROUNDS_FOR_CI (10) round trips → CI=0.0."""
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    # Only 5 round trips → below _MIN_ROUNDS_FOR_CI → CI = 0.0 → blocks.
    _make_profitable_round_trips(5, NOW, bus_path)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["sharpe_95ci_lower"] == 0.0, (
        f"ar125: <10 round trips should give sharpe_95ci_lower=0.0; "
        f"got {metrics['sharpe_95ci_lower']}"
    )


# ---------------------------------------------------------------------------
# C. Losing book: negative mean return → CI below floor → still BLOCKS
# ---------------------------------------------------------------------------


def test_ar125_losing_book_sharpe_ci_blocks(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 fail-CLOSED: a net-losing book (negative mean return) must still block.

    We write 200 losing round trips (all exits below entry → negative returns).
    The derived sharpe_95ci_lower should be negative → well below 1.0 → blocks.
    """
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    records = []
    for i in range(200):
        # Loss: entry=100, exit alternates 92/88 → returns -0.08 / -0.12 (all negative).
        exit_price = 92.0 if i % 2 == 0 else 88.0
        exit_time = NOW - timedelta(days=15) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)
        asset = f"LOSE{i}"
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=+0.01,
                fill_price=100.0,
                asof_execution=entry_time.isoformat(),
            )
        )
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=-0.01,
                fill_price=exit_price,
                asof_execution=exit_time.isoformat(),
            )
        )
    _write_executions(bus_path, records)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["sharpe_95ci_lower"] < SHARPE_FLOOR, (
        f"ar125: a losing book should have sharpe_95ci_lower < {SHARPE_FLOOR}; "
        f"got {metrics['sharpe_95ci_lower']}"
    )

    import unittest.mock as _mock
    original_collect = promotion._collect_metrics
    def _collect_with_path(asof_arg: datetime) -> dict:
        return original_collect(asof_arg, executions_path=bus_path)
    # Losing book has 200 outcomes → passes the count gate, but must fail the sharpe gate.
    with _mock.patch.object(promotion, "_collect_metrics", side_effect=_collect_with_path):
        decision = promotion.evaluate(NOW)

    assert decision.promoted is False
    assert any("sharpe_95ci_lower" in r for r in decision.blocked_by), (
        f"ar125: losing book did not block on sharpe_95ci_lower; blocked_by={decision.blocked_by}"
    )


# ---------------------------------------------------------------------------
# D. Backward compat: promotion_event snapshot wins over settlement CI
# ---------------------------------------------------------------------------


def test_ar125_promotion_event_snapshot_takes_precedence_over_settlement(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 backward compat: when an in-window promotion_event with sharpe_95ci_lower
    EXISTS (e.g. a future explicit producer, or the existing test fixtures), that value
    wins over the settlement-derived CI (latest-wins semantics unchanged).

    Here: promotion_event has sharpe_95ci_lower=1.5 AND 100 fill-kind events.
    executions.jsonl has a losing settlement that would produce a negative CI.
    The gate should PASS (promotion_event's 1.5 wins, fill-kind events provide count).
    """
    # Seed the full passing run via fill-kind events + promotion_event (existing test pattern).
    for i in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0 + (i % 3) * 0.1},
            )
        )
    audit_log.append(
        GovernanceEvent(
            kind="promotion_event",
            asof=NOW - timedelta(days=1),
            source="weekly_retro",
            payload={
                "calibrator_drift": 0.02,
                "sharpe_95ci_lower": 1.5,  # explicitly good snapshot
                "rolling_30d_max_drawdown_pct": 0.005,
                "weekly_retro_promotion_readiness": True,
            },
        )
    )

    # Write a LOSING settlement to executions.jsonl — should NOT override the snapshot.
    bus_path = tmp_path / "quant" / "executions.jsonl"
    records = []
    for i in range(50):
        exit_time = NOW - timedelta(days=5) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=1)
        asset = f"BAD{i}"
        records.append(
            _make_exec_record(asset=asset, fill_size_pct=+0.01, fill_price=100.0,
                              asof_execution=entry_time.isoformat())
        )
        records.append(
            _make_exec_record(asset=asset, fill_size_pct=-0.01, fill_price=85.0,  # -15% loss
                              asof_execution=exit_time.isoformat())
        )
    _write_executions(bus_path, records)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    # The promotion_event snapshot (1.5) must win over the settlement CI (negative).
    assert metrics["sharpe_95ci_lower"] == 1.5, (
        f"ar125: promotion_event snapshot should win over settlement CI; "
        f"got sharpe_95ci_lower={metrics['sharpe_95ci_lower']}"
    )
    # paper_outcomes_count = 100 (fill-kind) + 50 (settlement) = 150.
    assert metrics["paper_outcomes_count"] >= 100


# ---------------------------------------------------------------------------
# E. Empty bus → settlement count = 0 (does not double-block)
# ---------------------------------------------------------------------------


def test_ar125_empty_executions_bus_falls_back_to_fill_kind_count(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125: when executions.jsonl is absent, the fill-kind count is the ONLY source.
    Seeding 100 fill-kind events → paper_outcomes_count = 100 (settlement = 0).
    """
    for i in range(100):
        audit_log.append(
            GovernanceEvent(
                kind="fill",
                asof=NOW - timedelta(days=15),
                source="paper_reactor",
                payload={"broker": "paper", "realized_pnl": 1.0},
            )
        )

    # No executions.jsonl in tmp_path.
    bus_path = tmp_path / "quant" / "executions.jsonl"
    # bus_path does not exist.

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["paper_outcomes_count"] == 100, (
        f"ar125: absent executions.jsonl should not reduce fill-kind count; "
        f"got {metrics['paper_outcomes_count']}"
    )


# ---------------------------------------------------------------------------
# F. Non-paper-default account fills do NOT count toward paper_outcomes_count
# ---------------------------------------------------------------------------


def test_ar125_non_paper_account_round_trips_are_excluded(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125 cross-account isolation: freqtrade (account_id='freqtrade') round trips
    must NOT be counted as paper-default outcomes. Mirrors ar34's account filtering."""
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    records = []
    for i in range(200):
        exit_time = NOW - timedelta(days=15) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)
        asset = f"ETH{i}"
        # freqtrade account — should be excluded.
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=+0.5,
                fill_price=100.0,
                asof_execution=entry_time.isoformat(),
                account_id="freqtrade",
            )
        )
        records.append(
            _make_exec_record(
                asset=asset,
                fill_size_pct=-0.5,
                fill_price=110.0,
                asof_execution=exit_time.isoformat(),
                account_id="freqtrade",
            )
        )
    _write_executions(bus_path, records)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["paper_outcomes_count"] == 0, (
        f"ar125: freqtrade round trips leaked into paper_outcomes_count; "
        f"got {metrics['paper_outcomes_count']}"
    )


# ---------------------------------------------------------------------------
# G. Out-of-window exits do NOT count
# ---------------------------------------------------------------------------


def test_ar125_out_of_window_exits_are_excluded(
    audit_path: Path, tmp_path: Path
) -> None:
    """ar125: exits OUTSIDE the 30d window (> 30 days ago) must not count."""
    _seed_all_passing_non_sharpe_gates(NOW)
    bus_path = tmp_path / "quant" / "executions.jsonl"
    records = []
    for i in range(200):
        # All exits are 45 days ago — outside the 30d window.
        exit_time = NOW - timedelta(days=45) + timedelta(hours=i)
        entry_time = exit_time - timedelta(days=2)
        asset = f"OLD{i}"
        records.append(
            _make_exec_record(
                asset=asset, fill_size_pct=+0.01, fill_price=100.0,
                asof_execution=entry_time.isoformat()
            )
        )
        records.append(
            _make_exec_record(
                asset=asset, fill_size_pct=-0.01, fill_price=110.0,
                asof_execution=exit_time.isoformat()
            )
        )
    _write_executions(bus_path, records)

    metrics = promotion._collect_metrics(NOW, executions_path=bus_path)
    assert metrics["paper_outcomes_count"] == 0, (
        f"ar125: out-of-window exits leaked into paper_outcomes_count; "
        f"got {metrics['paper_outcomes_count']}"
    )


# ---------------------------------------------------------------------------
# H. _sharpe_95ci_lower_from_round_trips unit tests
# ---------------------------------------------------------------------------


def test_ar125_sharpe_ci_helper_returns_zero_for_empty_list() -> None:
    assert promotion._sharpe_95ci_lower_from_round_trips([]) == 0.0


def test_ar125_sharpe_ci_helper_returns_zero_for_thin_data() -> None:
    """Fewer than _MIN_ROUNDS_FOR_CI round trips → 0.0."""
    from hermes_quant.daemon.settlement_loop import SettledRoundTrip
    import pandas as pd

    stub_rt = SettledRoundTrip(
        asset="A",
        account_id="paper-default",
        asset_class="equity",
        side="buy",
        qty=0.01,
        entry_price=100.0,
        exit_price=110.0,
        asof_entry=pd.Timestamp("2026-01-01"),
        asof_exit=pd.Timestamp("2026-01-05"),
        entry_exec_id=None,
        exit_exec_id=None,
        entry_signal_id=None,
        exit_signal_id=None,
        fees=0.0,
        realized_return=0.10,
    )
    # Only 5 round trips (< _MIN_ROUNDS_FOR_CI = 10).
    thin = [stub_rt] * 5
    result = promotion._sharpe_95ci_lower_from_round_trips(thin)
    assert result == 0.0, f"thin data should return 0.0; got {result}"


def test_ar125_sharpe_ci_helper_positive_for_consistent_gains() -> None:
    """Many identical profitable round trips → CI lower bound > 0."""
    from hermes_quant.daemon.settlement_loop import SettledRoundTrip
    import pandas as pd

    # Need std > 0 to avoid zero-variance branch (all identical → std=0 → return 0.0).
    # Alternate 8% and 12% returns so std > 0.
    rts = []
    for i in range(100):
        rr = 0.08 if i % 2 == 0 else 0.12
        rt = SettledRoundTrip(
            asset="A",
            account_id="paper-default",
            asset_class="equity",
            side="buy",
            qty=0.01,
            entry_price=100.0,
            exit_price=100.0 * (1 + rr),
            asof_entry=pd.Timestamp("2026-01-01") + pd.Timedelta(hours=i * 2),
            asof_exit=pd.Timestamp("2026-01-01") + pd.Timedelta(hours=i * 2 + 1),
            entry_exec_id=None,
            exit_exec_id=None,
            entry_signal_id=None,
            exit_signal_id=None,
            fees=0.0,
            realized_return=rr,
        )
        rts.append(rt)

    ci_lower = promotion._sharpe_95ci_lower_from_round_trips(rts)
    assert math.isfinite(ci_lower), f"CI should be finite for consistent gains; got {ci_lower}"
    assert ci_lower > 0.0, (
        f"CI lower bound should be positive for consistently positive returns; got {ci_lower}"
    )


def test_ar125_sharpe_ci_helper_negative_for_consistent_losses() -> None:
    """Many loss round trips → CI lower bound < 0."""
    from hermes_quant.daemon.settlement_loop import SettledRoundTrip
    import pandas as pd

    rts = []
    for i in range(100):
        rr = -0.08 if i % 2 == 0 else -0.12
        rt = SettledRoundTrip(
            asset="A",
            account_id="paper-default",
            asset_class="equity",
            side="buy",
            qty=0.01,
            entry_price=100.0,
            exit_price=100.0 * (1 + rr),
            asof_entry=pd.Timestamp("2026-01-01") + pd.Timedelta(hours=i * 2),
            asof_exit=pd.Timestamp("2026-01-01") + pd.Timedelta(hours=i * 2 + 1),
            entry_exec_id=None,
            exit_exec_id=None,
            entry_signal_id=None,
            exit_signal_id=None,
            fees=0.0,
            realized_return=rr,
        )
        rts.append(rt)

    ci_lower = promotion._sharpe_95ci_lower_from_round_trips(rts)
    assert ci_lower < 0.0, (
        f"CI lower bound should be negative for consistent losses; got {ci_lower}"
    )
