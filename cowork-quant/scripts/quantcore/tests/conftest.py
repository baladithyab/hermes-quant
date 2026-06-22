from __future__ import annotations

from datetime import datetime, timezone
UTC = timezone.utc

import pytest

from quantcore.schemas import AnalystView, CommitteeSignal, MarketCosts, PortfolioState

ASOF = datetime(2026, 6, 9, 14, 0, tzinfo=UTC)


def make_view(analyst="classical-ta", direction=1, confidence=0.7, magnitude=0.02) -> AnalystView:
    return AnalystView(
        analyst=analyst,
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        horizon="5d",
        asof_decision=ASOF,
    )


def make_signal(
    direction=1,
    confidence=0.7,
    magnitude=0.02,
    n_analysts=2,
    event_risk=None,
) -> CommitteeSignal:
    return CommitteeSignal(
        asset="AAPL",
        asset_class="equity",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        horizon="5d",
        asof_decision=ASOF,
        views=[make_view(analyst=f"analyst-{i}", direction=direction) for i in range(n_analysts)],
        event_risk=event_risk or [],
    )


def make_costs(volatility=0.02, commission=0.0, spread=0.0005, slippage=0.0005) -> MarketCosts:
    return MarketCosts(
        commission=commission,
        spread=spread,
        slippage_estimate=slippage,
        volatility=volatility,
    )


def make_portfolio(
    nav=100_000.0,
    peak=100_000.0,
    day_start=100_000.0,
    halted=False,
    last_loss_at=None,
) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=peak,
        day_start_nav=day_start,
        positions=[],
        asof=ASOF,
        halted=halted,
        last_loss_at=last_loss_at,
    )


@pytest.fixture
def asof():
    return ASOF
