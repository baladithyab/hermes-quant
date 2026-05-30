"""Unit tests for hermes_quant.admissibility.oracle + order_state (ADR-0077).

Deterministic, no network. The AlpacaShortabilityOracle is driven with an injected fake
`get_asset`; the live network path is exercised only under --run-integration.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_quant.admissibility import (
    REASON_EQUITY_BELOW_2K,
    REASON_FRACTIONAL_SHORT,
    REASON_INSUFFICIENT_BPR,
    REASON_NOT_ETB,
    REASON_NOT_MARGINABLE,
    REASON_NOT_SHORTABLE,
    REASON_PTP_BLOCKED,
    REASON_SSR_MARKETABLE_SHORT,
    REASON_UNKNOWN_SHORTABILITY,
    AdmissibilityContext,
    AdmissibilityState,
    AlpacaShortabilityOracle,
    ETBSnapshotEntry,
    NullShortabilityOracle,
    ShortabilityVerdict,
    StaticETBAllowlistOracle,
    apply_verdict_to_target,
    evaluate_admissibility,
    select_oracle,
    target_pct_to_shares,
)

T = datetime(2026, 5, 30, 14, 0, 0, tzinfo=UTC)


def _etb_ctx(**overrides) -> AdmissibilityContext:
    base = dict(
        tradable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=False,
        account_equity=100_000.0,
        annual_cbr=0.0030,
    )
    base.update(overrides)
    return AdmissibilityContext(**base)


# --------------------------------------------------------------------------- #
# REJECT cases
# --------------------------------------------------------------------------- #
def test_unknown_shortability_rejects_short():
    # The silence-by-default headline: missing easy_to_borrow => REJECT, never assume.
    ctx = _etb_ctx(easy_to_borrow=None)
    v = evaluate_admissibility("ANY", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_UNKNOWN_SHORTABILITY

    ctx2 = _etb_ctx(shortable=None)
    v2 = evaluate_admissibility("ANY", "short", 100, T, ctx2)
    assert v2.state is AdmissibilityState.REJECTED
    assert v2.reason == REASON_UNKNOWN_SHORTABILITY


def test_not_etb_rejects_short():
    ctx = _etb_ctx(easy_to_borrow=False)
    v = evaluate_admissibility("MIDCAP", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_ETB


def test_not_shortable_rejects_short():
    ctx = _etb_ctx(shortable=False)
    v = evaluate_admissibility("NOSHORT", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_SHORTABLE


def test_not_marginable_rejects_short():
    ctx = _etb_ctx(marginable=False)
    v = evaluate_admissibility("CASHONLY", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_MARGINABLE


def test_equity_below_2k_rejects_short():
    ctx = _etb_ctx(account_equity=1_999.0)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_EQUITY_BELOW_2K


def test_fractional_short_rejected():
    ctx = _etb_ctx()
    v = evaluate_admissibility("AAPL", "short", 10.5, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_FRACTIONAL_SHORT


def test_insufficient_bp_rejected():
    # 1.03 * 100 * 1000 = 103_000 > 1_000 available.
    ctx = _etb_ctx(current_ask=100.0, available_bp=1_000.0)
    v = evaluate_admissibility("AAPL", "short", 1000, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_INSUFFICIENT_BPR


def test_ptp_no_exception_rejected():
    ctx = _etb_ctx(attributes=("ptp_no_exception",))
    v = evaluate_admissibility("PTP", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_PTP_BLOCKED


# --------------------------------------------------------------------------- #
# PARTIAL + ACCEPT cases
# --------------------------------------------------------------------------- #
def test_ssr_marketable_short_partial():
    ctx = _etb_ctx(ssr_active=True, is_marketable=True)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.PARTIAL
    assert v.reason == REASON_SSR_MARKETABLE_SHORT


def test_etb_whole_share_accepted_with_low_cbr():
    ctx = _etb_ctx(current_ask=100.0, available_bp=1_000_000.0)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.ACCEPTED
    assert v.reason is None
    assert 0.0 < v.annual_cbr < 0.02


def test_long_side_always_accepted_zero_cbr():
    # Even with easy_to_borrow=False, a long/buy is never constrained.
    ctx = _etb_ctx(easy_to_borrow=False)
    for side in ("long", "buy"):
        v = evaluate_admissibility("AAPL", side, 100, T, ctx)
        assert v.state is AdmissibilityState.ACCEPTED
        assert v.annual_cbr == 0.0


# --------------------------------------------------------------------------- #
# AlpacaShortabilityOracle (injected fake get_asset)
# --------------------------------------------------------------------------- #
class _FakeAsset:
    def __init__(self, **kw):
        self.tradable = kw.get("tradable", True)
        self.marginable = kw.get("marginable", True)
        self.shortable = kw.get("shortable", True)
        self.easy_to_borrow = kw.get("easy_to_borrow", True)
        self.fractionable = kw.get("fractionable", False)
        self.attributes = kw.get("attributes", [])
        self.margin_requirement_short = kw.get("margin_requirement_short", "1.50")


def test_alpaca_oracle_accepts_etb_short():
    oracle = AlpacaShortabilityOracle(get_asset=lambda sym: _FakeAsset())
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.ACCEPTED
    assert 0.0 < v.annual_cbr < 0.02


def test_alpaca_oracle_rejects_non_etb_short():
    oracle = AlpacaShortabilityOracle(get_asset=lambda sym: _FakeAsset(easy_to_borrow=False))
    v = oracle.verdict("SMALLCAP", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_ETB


def test_alpaca_oracle_fail_closed_on_get_asset_error():
    def _boom(sym):
        raise RuntimeError("network down")

    oracle = AlpacaShortabilityOracle(get_asset=_boom)
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_UNKNOWN_SHORTABILITY


# --------------------------------------------------------------------------- #
# NullShortabilityOracle + factory (flag-OFF == today, bit-for-bit)
# --------------------------------------------------------------------------- #
def test_null_oracle_accepts_everything():
    null = NullShortabilityOracle()
    # Even a non-ETB short is ACCEPTED — this IS the bug we preserve when the flag is OFF.
    ctx = _etb_ctx(easy_to_borrow=False)
    v = null.verdict("ANY", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.ACCEPTED
    assert v.reason is None
    assert v.annual_cbr == 0.0


def test_select_oracle_flag_off_is_null(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    assert isinstance(select_oracle(), NullShortabilityOracle)
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "0")
    assert isinstance(select_oracle(), NullShortabilityOracle)


def test_select_oracle_flag_on_is_alpaca(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    assert isinstance(select_oracle(), AlpacaShortabilityOracle)


# --------------------------------------------------------------------------- #
# StaticETBAllowlistOracle — asof honesty
# --------------------------------------------------------------------------- #
def test_static_allowlist_accepts_etb_for_matching_asof():
    snap = {
        "AAPL": ETBSnapshotEntry(
            symbol="AAPL",
            asof="2026-05-30",
            easy_to_borrow=True,
            shortable=True,
            marginable=True,
            annual_cbr=0.0030,
        )
    }
    oracle = StaticETBAllowlistOracle(snap)
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.ACCEPTED
    assert v.annual_cbr == pytest.approx(0.0030)


def test_static_allowlist_missing_snapshot_rejects():
    oracle = StaticETBAllowlistOracle({})
    v = oracle.verdict("UNKNOWN", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_ETB


def test_static_allowlist_wrong_date_rejects():
    # A snapshot for a DIFFERENT date does not apply (no look-ahead).
    snap = {
        "AAPL": ETBSnapshotEntry(
            symbol="AAPL",
            asof="2026-05-29",  # decision time is 2026-05-30
            easy_to_borrow=True,
            shortable=True,
            marginable=True,
            annual_cbr=0.0030,
        )
    }
    oracle = StaticETBAllowlistOracle(snap)
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_NOT_ETB


# --------------------------------------------------------------------------- #
# Authority boundary (ADR-0004): the oracle can ONLY subtract.
# --------------------------------------------------------------------------- #
LADDER = [0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20]
STATES = [
    ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0030),
    ShortabilityVerdict(AdmissibilityState.PARTIAL, REASON_SSR_MARKETABLE_SHORT, 0.0030),
    ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_ETB, 0.0),
]


@pytest.mark.parametrize("target_pct", LADDER)
@pytest.mark.parametrize("verdict", STATES)
def test_authority_boundary_never_amplifies(target_pct, verdict):
    adj = apply_verdict_to_target(target_pct, verdict)
    # Never amplifies the magnitude.
    assert abs(adj.adjusted_target_pct) <= abs(target_pct) + 1e-12
    # Sign never flips/inflates: either zeroed, or kept on the same side.
    if adj.adjusted_target_pct != 0.0:
        assert (adj.adjusted_target_pct > 0) == (target_pct > 0)
    # ACCEPTED passes through unchanged; everything else zeroes out.
    if verdict.state is AdmissibilityState.ACCEPTED:
        assert adj.adjusted_target_pct == target_pct
    else:
        assert adj.adjusted_target_pct == 0.0


def test_authority_boundary_fine_grid():
    # A small deterministic loop over a finer grid (NOT hypothesis — not a repo dep).
    grid = [round(x, 4) for x in [-0.2 + 0.013 * i for i in range(31)]]
    for target_pct in grid:
        for verdict in STATES:
            adj = apply_verdict_to_target(target_pct, verdict)
            assert abs(adj.adjusted_target_pct) <= abs(target_pct) + 1e-12
            if adj.adjusted_target_pct != 0.0:
                assert (adj.adjusted_target_pct > 0) == (target_pct > 0)


def test_flatten_inadmissible_held_short():
    # An inadmissible HELD short flattens to 0.0 and flags it.
    reject = ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_ETB, 0.0)
    adj = apply_verdict_to_target(-0.10, reject, existing_position_qty=-30.0)
    assert adj.adjusted_target_pct == 0.0
    assert adj.flattened_existing_short is True


# --------------------------------------------------------------------------- #
# Whole-share floor
# --------------------------------------------------------------------------- #
def test_whole_share_short_floor():
    # floor(10_000 * 0.10 / 33.0) = floor(30.30) = 30 -> -30 (never -30.3).
    assert target_pct_to_shares(-0.10, nav=10_000.0, price=33.0) == -30
    assert target_pct_to_shares(0.10, nav=10_000.0, price=33.0) == 30
    # degenerate inputs -> 0 shares.
    assert target_pct_to_shares(-0.10, nav=0.0, price=33.0) == 0
    assert target_pct_to_shares(-0.10, nav=10_000.0, price=0.0) == 0
