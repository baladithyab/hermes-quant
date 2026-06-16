"""Wave-4 review-fix regression tests (2026-06-12 review-team findings)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quantcore.config import RiskConfig
from quantcore.exec_guard import is_execution_tool
from quantcore.gate import RiskGate
from quantcore.ledger import Ledger
from quantcore.mask import AliasCodec
from quantcore.replay import DecisionRecord, assert_replayable
from quantcore.schemas import (
    AnalystView,
    CommitteeSignal,
    MarketCosts,
    PortfolioState,
)
from quantcore.verify_ledger import verify_ledger

UTC = timezone.utc
ASOF = datetime(2026, 6, 12, 14, 0, tzinfo=UTC)


def test_mask_many_days_no_substring_collision():
    # >10 days exercises the day_1 vs day_10 collision the review caught
    dates = [f"2026-06-{d:02d}" for d in range(1, 16)]  # 15 days
    c = AliasCodec.build(["AAPL"], dates, seed=4, level="blinded")
    obj = {"a": "as of 2026-06-14 vs 2026-06-01 and 2026-06-10"}
    assert c.unmask(c.mask(obj)) == obj  # must round-trip exactly


def test_mask_only_known_tickers():
    # prose words that are NOT in the universe must be left untouched
    c = AliasCodec.build(["AAPL"], ["2026-06-12"], seed=5, level="blinded")
    masked = c.mask({"n": "THE AAPL CEO SAID BUY"})
    assert "THE" in masked["n"] and "CEO" in masked["n"] and "BUY" in masked["n"]
    assert "AAPL" not in masked["n"]
    assert c.unmask(masked) == {"n": "THE AAPL CEO SAID BUY"}


@pytest.mark.parametrize("name", ["exercise_option", "assign", "mcp__b__execute_trade"])
def test_option_exercise_assign_denied(name):
    assert is_execution_tool(name)[0]


def test_review_order_allowed_by_design():
    # review/preview tools are dry-run (no execution) -> intentionally allowed
    assert not is_execution_tool("review_equity_order")[0]
    assert not is_execution_tool("review_option_order")[0]


def test_spurious_resume_detected(tmp_path):
    led = Ledger(tmp_path)
    led.append("resume", {"reason": "lifted breaker with no active halt"})
    rep = verify_ledger(tmp_path)
    assert not rep.ok
    assert rep.spurious_resumes  # flagged


def test_legit_halt_resume_ok(tmp_path):
    led = Ledger(tmp_path)
    led.append("halt", {"reason": "drawdown"})
    led.append("resume", {"reason": "human cleared"})
    rep = verify_ledger(tmp_path)
    assert rep.ok, rep.summary()
    assert not rep.spurious_resumes


def _record():
    cfg = RiskConfig(paper_zero_costs=True)
    sig = CommitteeSignal(
        asset="AAPL", asset_class="equity", direction=1, magnitude=0.05,
        confidence=0.7, horizon="5d", asof_decision=ASOF,
        views=[
            AnalystView(analyst="ta", asset="AAPL", asset_class="equity", direction=1,
                        magnitude=0.05, confidence=0.7, horizon="5d", asof_decision=ASOF),
            AnalystView(analyst="fund", asset="AAPL", asset_class="equity", direction=1,
                        magnitude=0.04, confidence=0.65, horizon="5d", asof_decision=ASOF),
        ],
    )
    costs = MarketCosts(commission=0.0, spread=0.0, slippage_estimate=0.0, volatility=0.02)
    pf = PortfolioState(nav=100_000.0, peak_nav=100_000.0, day_start_nav=100_000.0,
                        positions=[], asof=ASOF)
    decision = RiskGate(cfg).gate(sig, costs, pf)
    return DecisionRecord(config=cfg, signal=sig, costs=costs, portfolio=pf, decision=decision)


def test_tampered_config_rejected_by_digest():
    d = _record().to_dict()
    # tamper the config WITHOUT updating the stamped manifest digest
    d["config"]["max_drawdown_pct"] = 0.99
    rebuilt = DecisionRecord.from_dict(d)
    with pytest.raises(AssertionError):
        assert_replayable(rebuilt)
