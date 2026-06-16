"""Gate invariants. Per AGENTS.md: test the SILENCE path more than the action path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
UTC = timezone.utc

from hypothesis import given, settings
from hypothesis import strategies as st

from quantcore.config import RiskConfig
from quantcore.gate import RiskGate, in_event_blackout
from quantcore.schemas import SIZING_LADDER, Position

from .conftest import ASOF, make_costs, make_portfolio, make_signal

GATE = RiskGate(RiskConfig.moderate())


# --- silence paths ----------------------------------------------------------


def test_halt_silences_everything():
    d = GATE.gate(make_signal(confidence=0.99), make_costs(), make_portfolio(halted=True))
    assert d.verdict == "silence" and d.rule == "rule0_halt"


def test_single_analyst_committee_is_degenerate():
    d = GATE.gate(make_signal(n_analysts=1, confidence=0.99), make_costs(), make_portfolio())
    assert d.verdict == "silence" and d.rule == "rule0_5_degenerate_committee"


def test_flat_signal_silences():
    d = GATE.gate(make_signal(direction=0), make_costs(), make_portfolio())
    assert d.verdict == "silence" and d.rule == "rule3_flat"


def test_zero_confidence_silences():
    d = GATE.gate(make_signal(confidence=0.0), make_costs(), make_portfolio())
    assert d.verdict == "silence" and d.rule == "rule3_flat"


def test_negative_edge_sign_guard():
    # p < 0.5 on a long: signed edge negative -> silence, never a short flip
    d = GATE.gate(make_signal(direction=1, confidence=0.35), make_costs(), make_portfolio())
    assert d.verdict == "silence" and d.reason == "cost_gate_edge_sign"


def test_cost_gate_below_threshold():
    # Tiny magnitude can't clear 2x round-trip costs
    d = GATE.gate(
        make_signal(confidence=0.55, magnitude=0.0005),
        make_costs(commission=0.001, spread=0.002, slippage=0.001),
        make_portfolio(),
    )
    assert d.verdict == "silence" and d.reason == "cost_gate_below_threshold"


def test_cooldown_silences():
    pf = make_portfolio(last_loss_at=ASOF - timedelta(minutes=10))
    d = GATE.gate(make_signal(confidence=0.8), make_costs(), pf)
    assert d.verdict == "silence" and d.rule == "rule4_cooldown"


def test_cooldown_expires():
    pf = make_portfolio(last_loss_at=ASOF - timedelta(minutes=120))
    d = GATE.gate(make_signal(confidence=0.8), make_costs(), pf)
    assert d.rule != "rule4_cooldown"


# --- circuit breakers ---------------------------------------------------------


def test_drawdown_breaker_flattens_and_halts():
    pf = make_portfolio(nav=80_000, peak=100_000)  # 20% > 15%
    d = GATE.gate(make_signal(confidence=0.9), make_costs(), pf)
    assert d.verdict == "flatten_halt" and d.halt and d.target_position_pct == 0.0
    assert d.halt_until is None  # durable: explicit resume only


def test_daily_loss_breaker_halts_until_session():
    pf = make_portfolio(nav=94_000, peak=100_000, day_start=100_000)  # 6% > 5%
    d = GATE.gate(make_signal(confidence=0.9), make_costs(), pf)
    assert d.verdict == "flatten_halt" and d.halt_until is not None


def test_nonfinite_nav_fails_closed():
    pf = make_portfolio()
    object.__setattr__(pf, "nav", float("nan"))
    d = GATE.gate(make_signal(confidence=0.9), make_costs(), pf)
    assert d.verdict == "flatten_halt"


# --- event blackout (ADR-0084) ------------------------------------------------


def _fomc(days_ahead: float, impact="high"):
    return [{"kind": "fomc", "impact": impact, "scheduled_for": (ASOF + timedelta(days=days_ahead)).isoformat()}]


def test_event_blackout_blocks_fresh_open():
    gate = RiskGate(RiskConfig(event_risk_enabled=True))
    d = gate.gate(
        make_signal(confidence=0.9, event_risk=_fomc(0.5)), make_costs(), make_portfolio()
    )
    assert d.verdict == "silence" and d.rule == "rule3_5_event_blackout"


def test_event_blackout_off_by_default():
    d = GATE.gate(make_signal(confidence=0.9, event_risk=_fomc(0.5)), make_costs(), make_portfolio())
    assert d.rule != "rule3_5_event_blackout"


def test_event_blackout_ignores_low_impact_and_past_and_malformed():
    asof = ASOF
    assert in_event_blackout(_fomc(0.5, impact="low"), asof=asof, window_days=1.0) == (False, None)
    assert in_event_blackout(_fomc(-0.5), asof=asof, window_days=1.0) == (False, None)
    assert in_event_blackout([{"kind": "fomc", "impact": "high", "scheduled_for": "junk"}], asof=asof, window_days=1.0) == (False, None)
    assert in_event_blackout(None, asof=asof, window_days=1.0) == (False, None)


# --- action path ----------------------------------------------------------------


def test_strong_signal_produces_ladder_action():
    d = GATE.gate(
        make_signal(confidence=0.75, magnitude=0.03),
        make_costs(volatility=0.02),
        make_portfolio(),
    )
    assert d.verdict == "action"
    assert any(abs(abs(d.target_position_pct) - rung) < 1e-9 for rung in SIZING_LADDER)
    assert 0 < d.target_position_pct <= 0.20


# --- property tests: invariants hold for ALL inputs ------------------------------


@settings(max_examples=300, deadline=None)
@given(
    direction=st.sampled_from([-1, 0, 1]),
    confidence=st.floats(0.0, 1.0),
    magnitude=st.floats(0.0, 1.0),
    volatility=st.floats(1e-4, 0.5),
    nav=st.floats(1.0, 1e7),
    peak=st.floats(1.0, 1e7),
)
def test_gate_never_exceeds_cap_or_leaves_ladder(direction, confidence, magnitude, volatility, nav, peak):
    cfg = RiskConfig.moderate()
    pf = make_portfolio(nav=nav, peak=max(nav, peak), day_start=nav)
    d = RiskGate(cfg).gate(
        make_signal(direction=direction, confidence=confidence, magnitude=magnitude),
        make_costs(volatility=volatility),
        pf,
    )
    assert abs(d.target_position_pct) <= cfg.max_position_pct + 1e-12
    if d.verdict == "action":
        assert any(abs(abs(d.target_position_pct) - r) < 1e-9 for r in SIZING_LADDER)
        # sign of action matches signal direction
        assert d.target_position_pct * direction > 0


@settings(max_examples=200, deadline=None)
@given(
    nav=st.floats(1.0, 1e7),
    peak=st.floats(1.0, 1e7),
    confidence=st.floats(0.5, 1.0),
)
def test_gate_never_acts_past_drawdown_breaker(nav, peak, confidence):
    cfg = RiskConfig.moderate()
    peak = max(nav, peak)
    pf = make_portfolio(nav=nav, peak=peak, day_start=nav)
    d = RiskGate(cfg).gate(make_signal(confidence=confidence), make_costs(), pf)
    if pf.drawdown_pct > cfg.max_drawdown_pct:
        assert d.verdict == "flatten_halt"
        assert d.target_position_pct == 0.0


# --- Rule 6.5: portfolio caps (ADR-0087) -----------------------------------------
# Reject-only at the single seam: never resizes, never blocks de-risking.


def _position(asset: str, pct: float) -> Position:
    """Inline extension of the conftest helpers (conftest stays untouched)."""
    return Position(
        asset=asset,
        asset_class="equity",
        position_pct=pct,
        avg_price=100.0,
        opened_at=ASOF,
    )


def _portfolio_with(positions: list[Position], **kw):
    return make_portfolio(**kw).model_copy(update={"positions": positions})


def _signal_010(direction=1):
    """Signal whose quarter-Kelly target is exactly 0.10 under moderate
    (see test_cap_arithmetic_baseline_target_is_010, which anchors this)."""
    return make_signal(direction=direction, confidence=0.6, magnitude=0.02)


_COSTS_010 = dict(volatility=0.0975)


def test_cap_arithmetic_baseline_target_is_010():
    # Anchor for every cap test below: empty book -> action at exactly 0.10.
    d = GATE.gate(_signal_010(), make_costs(**_COSTS_010), make_portfolio())
    assert d.verdict == "action"
    assert abs(d.target_position_pct - 0.10) < 1e-9


def test_gross_exposure_cap_rejects_new_position():
    # Existing 0.20 + 0.15 = 0.35; new AAPL target 0.10 -> gross 0.45 > 0.40.
    pf = _portfolio_with([_position("MSFT", 0.20), _position("GOOG", 0.15)])
    d = GATE.gate(_signal_010(), make_costs(**_COSTS_010), pf)
    assert d.verdict == "silence" and d.rule == "rule6_5_portfolio_caps"
    assert d.reason.startswith("gross_exposure_cap")
    # reject-only: the would-be target is reported, never resized to fit
    assert abs(d.target_position_pct - 0.10) < 1e-9


def test_concurrent_position_cap_rejects_fifth_asset():
    # 4 open assets at 0.05 each (gross 0.20; +0.10 = 0.30 <= 0.40, so only
    # the count cap can fire). Opening asset #5 under moderate (max 4) rejects.
    pf = _portfolio_with([_position(f"OTH{i}", 0.05) for i in range(4)])
    d = GATE.gate(_signal_010(), make_costs(**_COSTS_010), pf)
    assert d.verdict == "silence" and d.rule == "rule6_5_portfolio_caps"
    assert d.reason == "max_concurrent_positions"


def test_derisking_passes_caps_even_when_gross_over_cap():
    # Book gross 0.55 > 0.40 cap; AAPL 0.20 -> 0.10 is de-risking: never blocked.
    pf = _portfolio_with(
        [_position("MSFT", 0.20), _position("GOOG", 0.15), _position("AAPL", 0.20)]
    )
    d = GATE.gate(_signal_010(), make_costs(**_COSTS_010), pf)
    assert d.verdict == "action" and d.rule == "rule6_kelly"
    assert abs(d.target_position_pct - 0.10) < 1e-9


def test_reducing_position_never_blocked_by_count_cap():
    # 5 open assets (over the 4 cap) AND gross 0.60 (over the 0.40 cap):
    # reducing AAPL 0.20 -> 0.10 still goes through.
    pf = _portfolio_with(
        [_position(f"OTH{i}", 0.10) for i in range(4)] + [_position("AAPL", 0.20)]
    )
    d = GATE.gate(_signal_010(), make_costs(**_COSTS_010), pf)
    assert d.verdict == "action" and d.rule == "rule6_kelly"
    assert abs(d.target_position_pct - 0.10) < 1e-9


@settings(max_examples=300, deadline=None)
@given(
    others=st.lists(st.floats(-0.2, 0.2), min_size=0, max_size=6),
    current=st.floats(-0.2, 0.2),
    direction=st.sampled_from([-1, 1]),
    confidence=st.floats(0.0, 1.0),
    magnitude=st.floats(0.0, 0.1),
    volatility=st.floats(1e-3, 0.5),
)
def test_gate_action_never_pushes_gross_past_cap(
    others, current, direction, confidence, magnitude, volatility
):
    """Invariant: the gate never emits an action that brings prospective gross
    above max_gross_exposure_pct. De-risking actions are allowed while the book
    is over the cap, but they strictly reduce gross — so prospective gross
    never exceeds max(cap, gross-before-action)."""
    cfg = RiskConfig.moderate()
    positions = [_position(f"OTH{i}", pct) for i, pct in enumerate(others)]
    if abs(current) > 1e-12:
        positions.append(_position("AAPL", current))
    pf = _portfolio_with(positions)
    existing_gross = sum(abs(p) for p in others) + abs(current)
    d = RiskGate(cfg).gate(
        make_signal(direction=direction, confidence=confidence, magnitude=magnitude),
        make_costs(volatility=volatility),
        pf,
    )
    if d.verdict == "action":
        prospective_gross = sum(abs(p) for p in others) + abs(d.target_position_pct)
        assert prospective_gross <= max(cfg.max_gross_exposure_pct, existing_gross) + 1e-9
        if abs(d.target_position_pct) >= abs(current):  # not de-risking
            assert prospective_gross <= cfg.max_gross_exposure_pct + 1e-9
