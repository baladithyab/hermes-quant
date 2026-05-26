"""Tests for ADR-0029 patched D7 live-promotion type-level gate.

The gate is correctness-by-construction: LiveBroker has NO `submit_mleg_order`
method at the class level, and instances cannot be built without a fully
validated LiveTradingApproval. There is no runtime boolean an attacker (or
an unattended LLM) can flip to authorize live multi-leg trading.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hermes_quant.react.live import LiveBroker, LiveTradingApproval


def _valid_approval_kwargs(**overrides):
    base = dict(
        approval_id="test_001",
        issued_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        paper_outcomes_count=100,
        rolling_30d_realized_sharpe=1.5,
        sharpe_95ci_lower=1.0,
        rolling_30d_max_drawdown_pct=0.005,
        no_killswitch_in_trailing_14d=True,
        immutable_breaches_in_window=0,
        weekly_retro_evidence_ids=["rev_id_1"],
        promoter_human_id="codeseys",
    )
    base.update(overrides)
    return base


def test_live_broker_has_no_mleg_method_without_approval():
    """The class itself exposes no `submit_mleg_order` — it's bound per-instance
    only after a valid LiveTradingApproval is supplied. Constructing without
    one raises TypeError, so there's no path to a usable live broker without
    the approval gate.
    """
    member_names = {name for name, _ in inspect.getmembers(LiveBroker)}
    assert "submit_mleg_order" not in member_names
    assert getattr(LiveBroker, "submit_mleg_order", None) is None

    # Constructing without an approval raises TypeError.
    with pytest.raises(TypeError):
        LiveBroker(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LiveBroker("not-an-approval")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LiveBroker({"paper_outcomes_count": 100})  # type: ignore[arg-type]


def test_live_trading_approval_rejects_subhundred_n():
    with pytest.raises(ValidationError, match=r"paper_outcomes_count must be >= 100"):
        LiveTradingApproval(**_valid_approval_kwargs(paper_outcomes_count=99))


def test_live_trading_approval_uses_lower_ci_not_point():
    """The gate checks the 95% CI lower bound, not the point estimate. Even a
    great rolling Sharpe with a wide CI must be rejected.
    """
    with pytest.raises(ValidationError, match=r"sharpe_95ci_lower must be >= 1\.0"):
        LiveTradingApproval(
            **_valid_approval_kwargs(
                rolling_30d_realized_sharpe=2.5,
                sharpe_95ci_lower=0.7,
            )
        )

    # Boundary: exactly 1.0 is OK.
    approval = LiveTradingApproval(**_valid_approval_kwargs(sharpe_95ci_lower=1.0))
    assert approval.sharpe_95ci_lower == 1.0


def test_live_trading_approval_drawdown_threshold():
    # Just over 1% — rejected.
    with pytest.raises(ValidationError, match=r"rolling_30d_max_drawdown_pct must be <= 0\.01"):
        LiveTradingApproval(**_valid_approval_kwargs(rolling_30d_max_drawdown_pct=0.01001))

    # Exactly 1.0% — OK (boundary).
    a1 = LiveTradingApproval(**_valid_approval_kwargs(rolling_30d_max_drawdown_pct=0.01))
    assert a1.rolling_30d_max_drawdown_pct == 0.01

    # 0.99% — OK.
    a2 = LiveTradingApproval(**_valid_approval_kwargs(rolling_30d_max_drawdown_pct=0.0099))
    assert a2.rolling_30d_max_drawdown_pct == 0.0099


def test_live_trading_approval_killswitch_window():
    with pytest.raises(ValidationError, match=r"kill-switch"):
        LiveTradingApproval(**_valid_approval_kwargs(no_killswitch_in_trailing_14d=False))


def test_live_trading_approval_immutable_breach_zero():
    with pytest.raises(ValidationError, match=r"immutable-rule breach"):
        LiveTradingApproval(**_valid_approval_kwargs(immutable_breaches_in_window=1))

    a = LiveTradingApproval(**_valid_approval_kwargs(immutable_breaches_in_window=0))
    assert a.immutable_breaches_in_window == 0


def test_live_broker_with_approval_can_be_constructed():
    """A valid approval permits LiveBroker construction; submit_mleg_order is
    then exposed on the INSTANCE, but never on the class.
    """
    approval = LiveTradingApproval(**_valid_approval_kwargs())
    broker = LiveBroker(approval)

    # Instance gets submit_mleg_order …
    assert hasattr(broker, "submit_mleg_order")
    assert callable(broker.submit_mleg_order)
    # … but the class itself still doesn't.
    assert getattr(LiveBroker, "submit_mleg_order", None) is None


def test_live_broker_submit_mleg_order_raises_notimplemented_in_v01x():
    """Even with a fully-valid approval, the v0.1.x stub refuses to submit.
    Paper is the only execution path until a future ADR defines the contract.
    """
    approval = LiveTradingApproval(**_valid_approval_kwargs())
    broker = LiveBroker(approval)

    with pytest.raises(NotImplementedError, match=r"v0\.1\.x"):
        broker.submit_mleg_order()
