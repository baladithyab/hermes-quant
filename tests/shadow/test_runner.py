"""tests/shadow/test_runner.py — ShadowAccountRunner integration tests.

Wave 8b / ADR-0049.

Tests:
- replay 3 synthetic days of audit events, 5 shadow accounts, deterministic P&L
- comparison report identifies biggest_alpha winner correctly
- counterfactual_winners / losers correctly classified
- report truncated to 2048 chars
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from hermes_quant.shadow.rules import (
    AlwaysFollowAdvisorRule,
    InverseConsensusRule,
    SemanticOnlyRule,
    SentimentOnlyRule,
    TrendFollowingRule,
    default_rules,
)
from hermes_quant.shadow.runner import ShadowAccountRunner, ShadowComparisonReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str,
    asof: datetime,
    direction: str = "buy",
    ticker: str = "AAPL",
    vote_share: float = 0.75,
    analysts: list[str] | None = None,
    ta_direction: str | None = None,
) -> dict:
    if analysts is None:
        analysts = ["semantic", "sentiment", "classical_ta"]
    payload: dict = {
        "ticker": ticker,
        "advisor_result": {"direction": direction},
        "signal_provenance": {
            "advisor_direction": direction,
            "vote_share": vote_share,
            "contributing_analysts": analysts,
        },
    }
    if ta_direction is not None:
        payload["signal_provenance"]["classical_ta_direction"] = ta_direction
    return {
        "event_id": event_id,
        "kind": "gate_approval",
        "asof": asof.isoformat(),
        "source": "test",
        "payload": payload,
    }


DAY1 = datetime(2025, 6, 10, 14, 0, tzinfo=timezone.utc)
DAY2 = datetime(2025, 6, 11, 14, 0, tzinfo=timezone.utc)
DAY3 = datetime(2025, 6, 12, 14, 0, tzinfo=timezone.utc)

PRICES: dict[str, dict[date, float]] = {
    "AAPL": {
        date(2025, 6, 10): 180.0,
        date(2025, 6, 11): 185.0,
        date(2025, 6, 12): 190.0,
    },
    "TSLA": {
        date(2025, 6, 10): 250.0,
        date(2025, 6, 11): 245.0,
        date(2025, 6, 12): 255.0,
    },
}

THREE_DAY_EVENTS = [
    # Day 1 — buy AAPL with full confluence
    _make_event("d1e1", DAY1, direction="buy", ticker="AAPL", vote_share=0.80,
                analysts=["semantic", "sentiment", "classical_ta"], ta_direction="buy"),
    # Day 2 — buy TSLA, but no semantic → SemanticOnly won't fire
    _make_event("d2e1", DAY2, direction="buy", ticker="TSLA", vote_share=0.70,
                analysts=["sentiment", "classical_ta"], ta_direction="buy"),
    # Day 3 — sell AAPL
    _make_event("d3e1", DAY3, direction="sell", ticker="AAPL", vote_share=0.65,
                analysts=["semantic", "classical_ta"], ta_direction="sell"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner(tmp_path: Path) -> ShadowAccountRunner:
    return ShadowAccountRunner(
        rules=default_rules(),
        db_dir=tmp_path,
        initial_cash=100_000.0,
        cost_model_bps=10.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunnerInit:
    def test_creates_five_accounts(self, runner: ShadowAccountRunner):
        assert len(runner.accounts) == 5

    def test_account_names_match_rules(self, runner: ShadowAccountRunner):
        expected = {r.name for r in default_rules()}
        assert set(runner.accounts.keys()) == expected

    def test_each_account_has_separate_db(self, runner: ShadowAccountRunner):
        paths = [acc.db_path for acc in runner.accounts.values()]
        assert len(paths) == len(set(paths))  # all unique


class TestReplaySession:
    def test_returns_dict_of_accounts(self, runner: ShadowAccountRunner):
        result = runner.replay_session(THREE_DAY_EVENTS, PRICES)
        assert isinstance(result, dict)
        assert len(result) == 5

    def test_accounts_have_state_after_replay(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        # AlwaysFollowAdvisor should have processed all 3 events
        acct = runner.accounts["always_follow_advisor"]
        # At least some fills happened
        # (Day 1: buy AAPL; Day 3: sell AAPL → net zero or residual)
        # Cash should differ from initial due to fills and cost model
        assert acct.cash != pytest.approx(100_000.0) or acct.positions != {}

    def test_semantic_only_skips_day2_event(self, runner: ShadowAccountRunner):
        """SemanticOnly should NOT fire on Day 2 (no semantic analyst)."""
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        acct = runner.accounts["semantic_only"]
        # SemanticOnly fired on Day1 (semantic present) and Day3 (semantic present)
        # but NOT Day2 (no semantic in analysts list)
        # After Day1 buy + Day3 sell → position should be approximately flat
        # (not completely flat due to rounding and cost model, but close)
        fills_path = acct.db_path
        import sqlite3
        conn = sqlite3.connect(str(fills_path))
        fills = conn.execute("SELECT ticker, action FROM shadow_fills ORDER BY asof").fetchall()
        conn.close()
        tickers_and_actions = [(r[0], r[1]) for r in fills]
        # Day2 TSLA event should NOT appear in semantic_only fills
        assert ("TSLA", "buy") not in tickers_and_actions

    def test_trend_following_fires_on_confluence(self, runner: ShadowAccountRunner):
        """TrendFollowing should fire on Day1 (TA matches advisor, vote_share=0.80>0.6)."""
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        acct = runner.accounts["trend_following"]
        import sqlite3
        conn = sqlite3.connect(str(acct.db_path))
        fills = conn.execute("SELECT ticker FROM shadow_fills").fetchall()
        conn.close()
        tickers = [r[0] for r in fills]
        assert "AAPL" in tickers

    def test_non_approval_events_ignored(self, runner: ShadowAccountRunner):
        """Non-approval events should not cause fills."""
        events = [
            {"event_id": "x1", "kind": "fill", "asof": DAY1.isoformat(),
             "source": "test", "payload": {"ticker": "AAPL", "direction": "buy"}},
        ]
        runner.replay_session(events, PRICES)
        for acct in runner.accounts.values():
            assert acct.positions == {}


class TestCompareToReal:
    def test_returns_comparison_report(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        for acct in runner.accounts.values():
            # Stamp the MTM row to the historical session date (mirrors the
            # harness) — otherwise the wall-clock-now asof falls outside the
            # session and compare_to_real silently reports every rule as 0.0.
            acct.mark_to_market({"AAPL": 190.0, "TSLA": 255.0}, asof=date(2025, 6, 12))

        report = runner.compare_to_real(real_pnl=50.0, asof=date(2025, 6, 12))
        assert isinstance(report, ShadowComparisonReport)
        # Non-vacuity: at least one rule that actually traded must report a
        # non-zero P&L for a historical replay. If the terminal MTM row is
        # dropped by the <= session_date filter, every rule reads 0.0 and this
        # assertion fails — the original bug.
        assert any(abs(p) > 1e-6 for p in report.shadow_pnls.values()), (
            "all shadow P&Ls are 0.0 — the terminal mark-to-market row was "
            "dropped from compare_to_real for the historical session"
        )


class TestHistoricalReplayMarkToMarket:
    """Regression: shadow rule P&L must not be reported as $0.00 for a
    historical replay where the terminal mark_to_market is stamped to the
    session date (the documented '--from .. --to ..' usage).
    """

    def test_terminal_mtm_counted_for_historical_session(self, runner: ShadowAccountRunner):
        # One Day-1 buy of AAPL @ 180; mark to market at a much higher 220 so
        # the session has a clearly non-zero (positive) P&L move.
        events = [
            _make_event("h1", DAY1, direction="buy", ticker="AAPL", vote_share=0.80,
                        analysts=["semantic", "sentiment", "classical_ta"], ta_direction="buy"),
        ]
        prices: dict[str, dict[date, float]] = {
            "AAPL": {date(2025, 6, 10): 180.0, date(2025, 6, 15): 220.0},
        }
        runner.replay_session(events, prices)

        # Mark to market at end-date prices, stamped to the historical session
        # end (exactly what scripts/shadow-replay-daily.py does).
        for acct in runner.accounts.values():
            acct.mark_to_market({"AAPL": 220.0}, asof=date(2025, 6, 15))

        report = runner.compare_to_real(real_pnl=-1000.0, asof=date(2025, 6, 15))

        # always_follow_advisor went long into a rising market → strictly
        # positive shadow P&L (and a genuine winner vs the -1000 real P&L).
        assert report.shadow_pnls["always_follow_advisor"] > 0.0
        # inverse_consensus shorts the same rising market → it LOSES money and
        # must NOT be a fictional counterfactual winner.
        assert report.shadow_pnls["inverse_consensus"] < 0.0
        assert "inverse_consensus" not in report.counterfactual_winners
        # biggest_alpha must reflect a real (non-zero-derived) advantage.
        best_rule, best_alpha = report.biggest_alpha
        assert best_alpha == pytest.approx(
            report.shadow_pnls[best_rule] - (-1000.0)
        )

    def test_real_pnl_stored(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        for acct in runner.accounts.values():
            acct.mark_to_market({"AAPL": 190.0, "TSLA": 255.0})
        report = runner.compare_to_real(real_pnl=42.0)
        assert report.real_pnl == pytest.approx(42.0)

    def test_shadow_pnls_has_all_rules(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        report = runner.compare_to_real(real_pnl=0.0)
        assert set(report.shadow_pnls.keys()) == set(runner.accounts.keys())

    def test_biggest_alpha_is_max_winner(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        for acct in runner.accounts.values():
            acct.mark_to_market({"AAPL": 200.0, "TSLA": 260.0})

        real_pnl = -500.0  # deliberately low so all shadows win
        report = runner.compare_to_real(real_pnl=real_pnl)

        best_rule, best_alpha = report.biggest_alpha
        # biggest_alpha should match the highest shadow_pnl - real_pnl
        expected_alpha = report.shadow_pnls[best_rule] - real_pnl
        assert best_alpha == pytest.approx(expected_alpha)
        # It should indeed be the maximum
        for r, p in report.shadow_pnls.items():
            assert p - real_pnl <= best_alpha + 1e-6

    def test_winners_and_losers_partition(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        report = runner.compare_to_real(real_pnl=0.0)
        all_rules = set(runner.accounts.keys())
        winners_set = set(report.counterfactual_winners)
        losers_set = set(report.counterfactual_losers)
        assert winners_set | losers_set == all_rules
        assert winners_set & losers_set == set()

    def test_evidence_summary_max_length(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        report = runner.compare_to_real(real_pnl=0.0)
        assert len(report.evidence_summary) <= 2048

    def test_report_to_dict(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        report = runner.compare_to_real(real_pnl=100.0)
        d = report.to_dict()
        assert "real_pnl" in d
        assert "shadow_pnls" in d
        assert "biggest_alpha" in d
        assert "counterfactual_winners" in d
        assert "counterfactual_losers" in d
        assert "evidence_summary" in d

    def test_asof_stored(self, runner: ShadowAccountRunner):
        runner.replay_session(THREE_DAY_EVENTS, PRICES)
        report = runner.compare_to_real(real_pnl=0.0, asof=date(2025, 6, 12))
        assert report.asof == date(2025, 6, 12)


class TestDeterministicPnL:
    """Replay same events twice → same P&L (idempotency)."""

    def test_second_replay_same_pnl(self, tmp_path: Path):
        runner1 = ShadowAccountRunner(rules=default_rules(), db_dir=tmp_path / "r1")
        runner2 = ShadowAccountRunner(rules=default_rules(), db_dir=tmp_path / "r2")

        runner1.replay_session(THREE_DAY_EVENTS, PRICES)
        runner2.replay_session(THREE_DAY_EVENTS, PRICES)

        for rule_name in runner1.accounts:
            acc1 = runner1.accounts[rule_name]
            acc2 = runner2.accounts[rule_name]
            assert acc1.cash == pytest.approx(acc2.cash, abs=0.01), \
                f"Cash mismatch for {rule_name}"
