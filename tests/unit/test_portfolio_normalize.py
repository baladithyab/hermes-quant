"""Tests for hermes_quant.risk.portfolio_normalize (ADR-0071).

Locks in:
  * Stage-2 portfolio caps math (gross / net / cash headroom)
  * Policy A scale_to_fit (uniform λ preserves Kelly relative ranking)
  * Policy B priority_rank (greedy accept by |target| desc until any cap binds)
  * Greedy online clip (clip_one_to_remaining_headroom for streaming decisions)
  * Headroom-already-breached fail-closed (no new fires until rebalanced)
"""

from __future__ import annotations

import math

import pytest

from hermes_quant.risk.portfolio_normalize import (
    PortfolioCaps,
    PortfolioState,
    NormalizedTarget,
    clip_one_to_remaining_headroom,
    headroom_summary,
    normalize_targets,
)


# ---------------------------------------------------------------------------
# PortfolioCaps validation
# ---------------------------------------------------------------------------


def test_caps_default_values() -> None:
    caps = PortfolioCaps()
    assert caps.max_gross_exposure_pct == 2.0
    assert caps.max_net_exposure_pct == 1.0
    assert caps.min_cash_reserve_pct == 0.20
    assert caps.normalization == "scale_to_fit"


def test_caps_validation_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="max_gross_exposure_pct"):
        PortfolioCaps(max_gross_exposure_pct=0.0)
    with pytest.raises(ValueError, match="max_net_exposure_pct"):
        PortfolioCaps(max_net_exposure_pct=-0.1)
    with pytest.raises(ValueError, match="min_cash_reserve_pct"):
        PortfolioCaps(min_cash_reserve_pct=1.0)
    with pytest.raises(ValueError, match="normalization"):
        PortfolioCaps(normalization="invalid_mode")


def test_profile_constructors() -> None:
    cons = PortfolioCaps.conservative()
    std = PortfolioCaps.standard()
    aggr = PortfolioCaps.aggressive()
    assert cons.max_gross_exposure_pct < std.max_gross_exposure_pct < aggr.max_gross_exposure_pct
    assert cons.min_cash_reserve_pct > std.min_cash_reserve_pct > aggr.min_cash_reserve_pct


# ---------------------------------------------------------------------------
# PortfolioState basic math
# ---------------------------------------------------------------------------


def test_state_empty() -> None:
    state = PortfolioState()
    assert state.gross_exposure_pct == 0.0
    assert state.net_exposure_pct == 0.0
    assert state.cash_pct == 1.0


def test_state_mixed_long_short() -> None:
    state = PortfolioState(positions={"AAPL": 0.20, "MSFT": -0.10, "GOOG": 0.05})
    assert math.isclose(state.gross_exposure_pct, 0.35)
    assert math.isclose(state.net_exposure_pct, 0.15)
    assert math.isclose(state.cash_pct, 0.65)


def test_state_overleveraged_negative_cash() -> None:
    """The 5/28 forensic case: 860% gross from 43 picks at 20% each."""
    positions = {f"SYM{i}": -0.20 for i in range(38)}
    positions.update({f"LONG{i}": 0.20 for i in range(5)})
    state = PortfolioState(positions=positions)
    assert math.isclose(state.gross_exposure_pct, 8.6)
    assert math.isclose(state.net_exposure_pct, -6.6)
    assert state.cash_pct < 0


# ---------------------------------------------------------------------------
# Headroom summary
# ---------------------------------------------------------------------------


def test_headroom_summary_empty_book() -> None:
    h = headroom_summary(PortfolioState(), PortfolioCaps())
    assert h["gross_exposure_pct"] == 0.0
    assert h["gross_headroom"] == 2.0
    assert h["cash_headroom"] == 0.80  # 1 - 0.20 - 0
    assert h["net_headroom_min_side"] == 1.0


def test_headroom_summary_overleveraged() -> None:
    state = PortfolioState(positions={f"S{i}": -0.20 for i in range(43)})
    h = headroom_summary(state, PortfolioCaps())
    assert h["gross_headroom"] < 0  # already breached
    assert h["cash_headroom"] < 0


# ---------------------------------------------------------------------------
# Policy A — scale_to_fit
# ---------------------------------------------------------------------------


def test_scale_to_fit_passthrough_when_caps_dont_bind() -> None:
    """1 pick at 20% on empty book: passes through with scale=1.0."""
    results = normalize_targets([("AAPL", 0.20)], PortfolioState(), PortfolioCaps())
    assert len(results) == 1
    r = results[0]
    assert r.asset == "AAPL"
    assert r.fired is True
    assert math.isclose(r.portfolio_target_pct, 0.20)
    assert math.isclose(r.scale_factor, 1.0)


def test_scale_to_fit_uniform_scale_preserves_relative_ranking() -> None:
    """Mixed-magnitude picks scale by the same λ (Kelly ranking preserved)."""
    state = PortfolioState()
    caps = PortfolioCaps()
    targets = [("A", 0.30), ("B", -0.20), ("C", 0.10)]
    results = normalize_targets(targets, state, caps)
    scales = {round(r.scale_factor, 6) for r in results}
    assert len(scales) == 1, f"expected uniform scale, got {scales}"
    # Relative ratios preserved
    portfolio = {r.asset: r.portfolio_target_pct for r in results}
    assert math.isclose(portfolio["A"] / portfolio["B"], 0.30 / -0.20, rel_tol=1e-9)


def test_scale_to_fit_43_picks_at_20pct_replays_5_28() -> None:
    """The exact 5/28 demand pattern: 43 picks at ±20% on empty book.

    With default caps (200% gross, 100% net, 20% cash):
      gross_demand = 8.6 NAV → λ_gross = 2.0/8.6 = 0.232
      cash room    = 0.80 NAV → λ_cash  = 0.80/8.6 = 0.093 (cash binds)
      net_demand   = -7.4 NAV (38 short × 0.20 + 5 long × 0.20)
                   → λ_net = 1.0/7.4 = 0.135
      → λ = min(0.232, 0.093, 0.135) = 0.093 (cash cap binds)
    """
    targets = [(f"SYM{i}", -0.20 if i < 38 else 0.20) for i in range(43)]
    results = normalize_targets(targets, PortfolioState(), PortfolioCaps())
    gross_post = sum(abs(r.portfolio_target_pct) for r in results)
    cash_floor = PortfolioCaps().min_cash_reserve_pct
    # Gross post must respect (1 - cash_floor) as the binding cap here
    assert gross_post <= (1.0 - cash_floor) + 1e-9
    # All scale factors equal under Policy A
    scales = {round(r.scale_factor, 6) for r in results}
    assert len(scales) == 1
    # All 43 picks fire (just smaller)
    assert all(r.fired for r in results)


def test_scale_to_fit_pre_existing_consumes_headroom() -> None:
    """Existing positions consume headroom; new picks must fit in remainder."""
    state = PortfolioState(positions={"EXIST": 0.50})  # 50% gross already
    caps = PortfolioCaps()  # 200% gross cap
    new = [("NEW1", 0.50), ("NEW2", -0.50)]  # demand 1.0 gross
    results = normalize_targets(new, state, caps)
    new_gross_post = sum(abs(r.portfolio_target_pct) for r in results)
    # Existing 0.5 + new gross_post must respect cap
    assert state.gross_exposure_pct + new_gross_post <= caps.max_gross_exposure_pct + 1e-9


# ---------------------------------------------------------------------------
# Policy B — priority_rank
# ---------------------------------------------------------------------------


def test_priority_rank_accepts_largest_first() -> None:
    """Sort by |target| desc; accept until any cap binds."""
    caps = PortfolioCaps(normalization="priority_rank")
    targets = [("SMALL", 0.05), ("BIG", 0.50), ("MED", 0.20)]
    results = normalize_targets(targets, PortfolioState(), caps)
    # Order in result preserves input order
    by_asset = {r.asset: r for r in results}
    assert by_asset["BIG"].fired is True
    assert math.isclose(by_asset["BIG"].portfolio_target_pct, 0.50)


def test_priority_rank_drops_picks_after_cash_or_net_cap_binds() -> None:
    """4 shorts of 20% saturate the 80% cash sleeve (cap binds before net).

    With default caps: 20% cash floor → 80% NAV available for gross; 4 × 20% = 80%
    saturates cash. The 5th short would push gross to 100% and cash to 0%, breaching
    the 20% cash floor — silenced. (Net cap is 100% so 5 shorts would also have
    been the binding cap, but cash binds first.)
    """
    caps = PortfolioCaps(normalization="priority_rank")
    targets = [(f"SHORT{i}", -0.20) for i in range(8)]
    results = normalize_targets(targets, PortfolioState(), caps)
    fired = [r for r in results if r.fired]
    assert len(fired) == 4
    silenced = [r for r in results if not r.fired]
    # Either cash/gross or net cap reason is acceptable
    assert all(
        ("gross_or_cash_bound" in (r.silence_reason or ""))
        or ("net_cap_bound" in (r.silence_reason or ""))
        for r in silenced
    )


def test_priority_rank_drops_picks_after_net_cap_with_aggressive_caps() -> None:
    """With higher cash tolerance, net cap binds first.

    Aggressive caps (10% cash floor → 90% gross room) let 4-4.5 shorts fit gross/cash;
    but net cap is 1.5 NAV, so 7 shorts × 0.20 = 1.4 < 1.5, 8th pushes to 1.6 > 1.5.

    Actually with min_cash=0.10, gross room is 0.90; 4 picks × 0.20 = 0.80 fit, 5th
    would push to 1.00 > 0.90 → cash still binds first. Use a custom caps for the
    net-binds-first scenario.
    """
    caps = PortfolioCaps(
        max_gross_exposure_pct=2.0,
        max_net_exposure_pct=0.50,    # tighter net cap
        min_cash_reserve_pct=0.0,     # no cash floor
        normalization="priority_rank",
    )
    targets = [(f"SHORT{i}", -0.20) for i in range(8)]
    results = normalize_targets(targets, PortfolioState(), caps)
    fired = [r for r in results if r.fired]
    # Net binds at -0.50: 2 picks × -0.20 = -0.40 fit, 3rd → -0.60 > 0.50 cap → silenced
    assert len(fired) == 2
    silenced = [r for r in results if not r.fired]
    assert all("net_cap_bound" in (r.silence_reason or "") for r in silenced)


# ---------------------------------------------------------------------------
# Headroom-already-breached fail-closed
# ---------------------------------------------------------------------------


def test_breached_book_silences_all_new_picks() -> None:
    """5/28 forensic case: book already at 860% gross; new picks all silenced."""
    positions = {f"S{i}": -0.20 for i in range(43)}
    state = PortfolioState(positions=positions)
    caps = PortfolioCaps()  # 200% gross cap
    new = [("NEW1", -0.20), ("NEW2", 0.20)]
    results = normalize_targets(new, state, caps)
    assert all(not r.fired for r in results)
    assert all(r.portfolio_target_pct == 0.0 for r in results)
    assert all("headroom_breached" in (r.silence_reason or "") for r in results)


# ---------------------------------------------------------------------------
# Greedy online clip
# ---------------------------------------------------------------------------


def test_clip_one_passthrough_on_empty_book() -> None:
    r = clip_one_to_remaining_headroom("AAPL", 0.20, PortfolioState(), PortfolioCaps())
    assert r.fired is True
    assert math.isclose(r.portfolio_target_pct, 0.20)
    assert math.isclose(r.scale_factor, 1.0)


def test_clip_one_shrinks_to_remaining_cash_headroom() -> None:
    """Existing 75% gross + 20% cash floor = 5% remaining for new pick."""
    state = PortfolioState(positions={"EXIST": 0.75})
    caps = PortfolioCaps()  # 20% cash floor → 80% available - 75% used = 5% room
    r = clip_one_to_remaining_headroom("NEW", 0.20, state, caps)
    assert r.fired is True
    assert math.isclose(r.portfolio_target_pct, 0.05)
    assert math.isclose(r.scale_factor, 0.25)


def test_clip_one_silences_when_breached() -> None:
    state = PortfolioState(positions={f"S{i}": -0.20 for i in range(15)})  # 300% gross
    r = clip_one_to_remaining_headroom("NEW", -0.20, state, PortfolioCaps())
    assert r.fired is False
    assert r.portfolio_target_pct == 0.0
    assert "headroom_breached" in (r.silence_reason or "")


def test_clip_one_respects_net_cap_signed() -> None:
    """Existing net=-0.95 with no cash floor; new short of -0.20 → clip to -0.05.

    Use min_cash_reserve_pct=0 so cash doesn't bind first; we want to isolate
    the net-cap clipping behavior on the *signed* axis.
    """
    state = PortfolioState(positions={"EXIST_SHORT": -0.95})
    caps = PortfolioCaps(
        max_gross_exposure_pct=2.0,
        max_net_exposure_pct=1.0,
        min_cash_reserve_pct=0.0,
    )
    r = clip_one_to_remaining_headroom("MORE_SHORT", -0.20, state, caps)
    assert r.fired is True
    # Should be clipped to -0.05 to land exactly at -1.00 net
    assert math.isclose(r.portfolio_target_pct, -0.05, abs_tol=1e-9)


def test_clip_one_zero_target_passthrough() -> None:
    r = clip_one_to_remaining_headroom("X", 0.0, PortfolioState(), PortfolioCaps())
    assert r.fired is False
    assert r.silence_reason == "zero_target"


# ---------------------------------------------------------------------------
# Empty / edge inputs
# ---------------------------------------------------------------------------


def test_normalize_empty_targets() -> None:
    assert normalize_targets([], PortfolioState(), PortfolioCaps()) == []


def test_normalize_zero_target_in_batch() -> None:
    results = normalize_targets(
        [("A", 0.20), ("B", 0.0), ("C", -0.10)], PortfolioState(), PortfolioCaps()
    )
    by_asset = {r.asset: r for r in results}
    assert by_asset["B"].fired is False or by_asset["B"].portfolio_target_pct == 0.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_normalize_targets_deterministic() -> None:
    state = PortfolioState(positions={"X": 0.30, "Y": -0.20})
    caps = PortfolioCaps()
    targets = [("A", 0.40), ("B", -0.30), ("C", 0.10), ("D", -0.50)]
    r1 = normalize_targets(targets, state, caps)
    r2 = normalize_targets(targets, state, caps)
    assert r1 == r2


# ---------------------------------------------------------------------------
# ar03 — a NON-FINITE existing book must fail CLOSED (silence all new picks),
# not silently no-op the cap. (Archaeology wave-2: a NaN target_position_pct in
# the book made `g_room <= 0` evaluate False — every NaN comparison is False —
# so the breach guard was a silent no-op and every new pick fired at full size.)
# ---------------------------------------------------------------------------


def test_normalize_targets_nan_book_fails_closed() -> None:
    """A NaN in the existing book silences ALL new picks (was: fired at full size)."""
    state = PortfolioState(positions={"XYZ": float("nan"), "AAPL": 0.10})
    assert not math.isfinite(state.gross_exposure_pct)
    out = normalize_targets([("AAPL", 0.10), ("MSFT", 0.10)], state, PortfolioCaps())
    assert all(not nt.fired for nt in out), "NaN book must silence every new pick"
    assert all(nt.portfolio_target_pct == 0.0 for nt in out)
    assert all("nonfinite" in (nt.silence_reason or "") for nt in out), [
        nt.silence_reason for nt in out
    ]


def test_normalize_targets_priority_rank_nan_book_fails_closed() -> None:
    """The priority_rank policy must also fail closed on a NaN book."""
    state = PortfolioState(positions={"XYZ": float("nan")})
    caps = PortfolioCaps(normalization="priority_rank")
    out = normalize_targets([("AAPL", 0.20), ("MSFT", 0.20), ("TSLA", 0.20)], state, caps)
    assert all(not nt.fired for nt in out), "priority_rank NaN book must silence every pick"


def test_clip_one_nan_book_fails_closed() -> None:
    """The greedy single-fire path must fail closed on a NaN book."""
    state = PortfolioState(positions={"XYZ": float("nan")})
    nt = clip_one_to_remaining_headroom("AAPL", 0.10, state, PortfolioCaps())
    assert nt.fired is False
    assert nt.portfolio_target_pct == 0.0
    assert "nonfinite" in (nt.silence_reason or "")


def test_finite_book_is_byte_identical_after_guard() -> None:
    """A finite book is unaffected by the guard — a normal in-headroom pick still fires."""
    state = PortfolioState(positions={"AAPL": 0.10})
    nt = clip_one_to_remaining_headroom("MSFT", 0.10, state, PortfolioCaps())
    assert nt.fired is True
    out = normalize_targets([("MSFT", 0.10)], state, PortfolioCaps())
    assert out[0].fired is True
