"""RED→GREEN test: durable drawdown baseline NAV must use MTM, not cost-basis.

DEFECT (P2): autonomous.py:1745 computes _durable_nav via _account_nav_usd(),
which reads cash.equity_total from state.db.  cash.equity_total is written by
apply_execution() using avg_entry_price — i.e. cost-basis, not mark-to-market.

After a NAV-fraction BUY fill and a mark drop, equity_total stays at initial_cash
(the cost-basis NAV-fraction math is self-cancelling), so drawdown_pct == 0% even
when a real unrealized loss is open.  The ADR-0004 15% breaker is silently disabled
for the entire horizon the position is open.

Scope qualifier: HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE is NOT currently exported
by the live wrapper, so this is a latent defect.  The fix is purely additive behind
the existing flag.

RED proof (pre-fix):
  equity_total (cost-basis) stays at initial_cash after a fill + mark drop.
  get_marked_equity() correctly shows a lower marked_equity.
  → the OLD _account_nav_usd()-based path returns cost-basis, not MTM.

GREEN proof (post-fix):
  _account_nav_mtm() returns marked_equity when mark prices are available.
  Fails-closed to cost_basis when marks are absent.
"""

from __future__ import annotations

import math
import pathlib

import pytest

from hermes_quant.state.portfolio_state import PortfolioState, _default_initial_cash

_INITIAL_CASH = 100_000.0


def _exec_rec(**kw):
    base = dict(
        proposal_id="test_durable_nav",
        asof_execution="2026-06-17T10:00:00Z",
        account_id="paper-default",
        asset_class="equity",
        fill_price=50.0,
        fill_size_pct=0.05,
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# RED PROOF: _account_nav_usd() is cost-basis — misses unrealized losses.
# ---------------------------------------------------------------------------


def test_equity_total_costbasis_misses_unrealized_loss(tmp_path):
    """RED: equity_total does NOT change when the mark falls below entry.

    This is the root cause: apply_execution() writes equity_total using
    avg_entry_price (cost-basis), so a mark drop is INVISIBLE to
    _account_nav_usd() → the durable drawdown baseline reads 0% drawdown.

    This test FAILS (is RED) if equity_total somehow tracked MTM — which it
    doesn't on HEAD 6cbab3f.  It proves the defect exists.
    """
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    initial_cash = _default_initial_cash()

    # BUY 5% of NAV worth of ASTS at $50
    ps.apply_execution(_exec_rec(asset="ASTS", fill_price=50.0, fill_size_pct=0.05))

    cash_after_fill = ps.get_cash("paper-default")
    assert cash_after_fill is not None

    # Cost-basis equity_total == initial_cash (the NAV-fraction algebra is
    # self-cancelling: delta_cash = -0.05*50 = -2.5; equity = (cash-2.5) +
    # 0.05*50 = initial_cash).
    assert abs(cash_after_fill.equity_total - initial_cash) < 1.0, (
        f"Expected cost-basis equity ≈ {initial_cash}, "
        f"got {cash_after_fill.equity_total}"
    )

    # MTM with mark = $40 (20% below entry): should show -1% NAV loss.
    marked = ps.get_marked_equity("paper-default", {"ASTS": 40.0})
    assert marked.marked_equity < cash_after_fill.equity_total, (
        "get_marked_equity must reflect the unrealized loss when mark < entry"
    )

    # Quantitative check: unrealized = 0.05 * 100000 * (40/50 - 1) = -1000
    expected_unrealized = 0.05 * initial_cash * (40.0 / 50.0 - 1.0)
    assert abs(marked.total_unrealized - expected_unrealized) < 10.0, (
        f"Expected total_unrealized ≈ {expected_unrealized}, "
        f"got {marked.total_unrealized}"
    )

    # The KEY assertion: cost-basis drawdown is 0% but MTM drawdown is real.
    costbasis_drawdown = (initial_cash - cash_after_fill.equity_total) / initial_cash
    mtm_drawdown = (initial_cash - marked.marked_equity) / initial_cash
    assert abs(costbasis_drawdown) < 1e-9, (
        f"Cost-basis drawdown should be ~0%, got {costbasis_drawdown:.6f}"
    )
    assert mtm_drawdown > 0.005, (
        f"MTM drawdown should be > 0.5% for a 20% mark-drop on 5% position, "
        f"got {mtm_drawdown:.6f}"
    )


# ---------------------------------------------------------------------------
# GREEN PROOF: _account_nav_mtm() uses MTM and falls back to cost-basis.
# ---------------------------------------------------------------------------


def test_account_nav_mtm_returns_mtm_when_marks_available(tmp_path, monkeypatch):
    """GREEN: _account_nav_mtm() returns marked_equity (not cost-basis) when
    a mark is available for the open position.

    Monkeypatches build_perception_frame_live so the test is network-free.
    """
    import hermes_quant.autonomous as auto

    # Wire the singleton to a test-isolated state.db
    from hermes_quant.state import portfolio_state as _ps_mod

    ps = PortfolioState(state_db_path=tmp_path / "state.db")

    # Monkeypatch the singleton so _account_nav_mtm() reads our test db.
    monkeypatch.setattr(_ps_mod, "_singleton", ps)

    initial_cash = _default_initial_cash()

    # BUY 5% of ASTS at $50
    ps.apply_execution(_exec_rec(asset="ASTS", fill_price=50.0, fill_size_pct=0.05))

    # Monkeypatch the perception builder to return mark=40 (network-free)
    class _FakeFrame:
        last_close = 40.0

    def _fake_build_perception(symbol, *, asset_class, timeframe):
        if symbol == "ASTS":
            return _FakeFrame()
        return None

    # Monkeypatch the perception import inside the function.
    import unittest.mock as mock

    with mock.patch(
        "hermes_quant.perception.build_perception_frame_live",
        side_effect=_fake_build_perception,
    ):
        nav_mtm = auto._account_nav_mtm()

    assert nav_mtm is not None, "_account_nav_mtm() must not return None for a valid position"
    assert math.isfinite(nav_mtm), "_account_nav_mtm() must return a finite value"

    # MTM nav should be BELOW initial_cash (unrealized loss from mark 40 < entry 50)
    assert nav_mtm < initial_cash, (
        f"MTM nav ({nav_mtm}) should be < initial_cash ({initial_cash}) "
        "because mark < entry"
    )

    # And close to the expected MTM value
    expected_mtm = ps.get_marked_equity("paper-default", {"ASTS": 40.0}).marked_equity
    assert abs(nav_mtm - expected_mtm) < 1.0, (
        f"_account_nav_mtm() result {nav_mtm} should ≈ get_marked_equity {expected_mtm}"
    )


def test_account_nav_mtm_fallsback_to_costbasis_when_no_marks(tmp_path, monkeypatch):
    """GREEN: _account_nav_mtm() falls back to cost-basis when perception fails."""
    import hermes_quant.autonomous as auto
    from hermes_quant.state import portfolio_state as _ps_mod

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    monkeypatch.setattr(_ps_mod, "_singleton", ps)

    initial_cash = _default_initial_cash()

    ps.apply_execution(_exec_rec(asset="ASTS", fill_price=50.0, fill_size_pct=0.05))

    # Perception builder always fails (network error, etc.)
    import unittest.mock as mock

    with mock.patch(
        "hermes_quant.perception.build_perception_frame_live",
        side_effect=RuntimeError("network error"),
    ):
        nav_mtm = auto._account_nav_mtm()

    # Should fall back to cost-basis (equity_total ≈ initial_cash)
    assert nav_mtm is not None
    assert abs(nav_mtm - initial_cash) < 1.0, (
        f"Without marks, _account_nav_mtm() should return cost-basis ≈ {initial_cash}, "
        f"got {nav_mtm}"
    )


def test_account_nav_mtm_fallsback_to_costbasis_when_no_positions(tmp_path, monkeypatch):
    """GREEN: _account_nav_mtm() returns cost-basis when no positions are open
    (cost-basis == MTM for a cash-only book)."""
    import hermes_quant.autonomous as auto
    from hermes_quant.state import portfolio_state as _ps_mod

    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    monkeypatch.setattr(_ps_mod, "_singleton", ps)

    initial_cash = _default_initial_cash()

    # No fills: cash-only book
    nav_mtm = auto._account_nav_mtm()

    # For a cash-only book, MTM == cost-basis (bootstrap cash)
    assert nav_mtm is not None
    assert abs(nav_mtm - initial_cash) < 1.0


def test_tick_uses_account_nav_mtm_when_flag_on(monkeypatch):
    """GREEN: tick() calls _account_nav_mtm() (not _account_nav_usd()) when
    HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE=1.

    The spy captures which function was called for the durable path.
    """
    monkeypatch.setenv("HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE", "1")
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    import hermes_quant.autonomous as auto

    # Pin autonomous mode — the tick's mode-gate returns early on a config-less /
    # cold home (CI), where _read_pdr_mode() correctly defaults to "advise". This is
    # the established idiom for autonomous tick tests; omitting it made this test
    # silently ride the live ~/.hermes/config.yaml leak that the ADR-0092
    # home-decouple (4aafaf3) closed.
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")

    called = {"nav_mtm": False, "nav_usd_direct": False}

    # Patch _account_nav_mtm to record the call and return a sentinel.
    orig_nav_usd = auto._account_nav_usd

    def _spy_nav_mtm():
        called["nav_mtm"] = True
        return 99_000.0

    def _spy_nav_usd():
        # Should NOT be called directly from tick() on the durable path.
        called["nav_usd_direct"] = True
        return 99_000.0

    monkeypatch.setattr(auto, "_account_nav_mtm", _spy_nav_mtm)
    monkeypatch.setattr(auto, "_account_nav_usd", _spy_nav_usd)

    from hermes_quant.watchlist import WatchlistEntry

    seen = {}

    def _spy_recommend(*, symbol, asset_class, timeframe, include_lessons,
                       perception_frame=None, durable_equity_account=None, **kw):
        seen["durable_equity_account"] = durable_equity_account
        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "aggregated_signal": None,
            "risk_gate": {
                "pass": False,
                "recommended_action": "gated",
                "gated_reason": "x",
                "kelly_fraction": 0.0,
            },
        }

    entry = WatchlistEntry(symbol="ASTS", asset_class="equity", timeframe="1d")
    auto.tick(dry_run=True, symbols=[entry], advisor_recommend=_spy_recommend)

    # tick() MUST have called _account_nav_mtm, NOT _account_nav_usd directly.
    assert called["nav_mtm"], "tick() should call _account_nav_mtm() on durable path"
    assert not called["nav_usd_direct"], (
        "tick() must NOT call _account_nav_usd() directly for the durable nav "
        "(it should go through _account_nav_mtm)"
    )
    # The MTM nav was threaded through.
    assert seen.get("durable_equity_account") == ("paper-default", "equity", 99_000.0)
