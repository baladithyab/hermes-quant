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
    REASON_MISSING_ACCOUNT_CONTEXT,
    REASON_MISSING_QUOTE,
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
# FAIL-CLOSED: shortability resolved, but account/quote context missing => REJECT.
# Money-software: a missing hard-precondition input is never an implicit pass.
# --------------------------------------------------------------------------- #
def test_missing_account_equity_rejects_etb_short():
    # The headline: a perfectly-ETB whole-share short with UNKNOWN account_equity must
    # REJECT (not ACCEPT). Before the fix this fell through to ACCEPTED (fail-open bug).
    ctx = _etb_ctx(account_equity=None, current_ask=100.0, available_bp=1_000_000.0)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_MISSING_ACCOUNT_CONTEXT


def test_missing_quote_rejects_etb_short():
    # Equity present + sufficient, but no ask to value the short => cannot prove BP fits.
    ctx = _etb_ctx(account_equity=100_000.0, current_ask=None, available_bp=1_000_000.0)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_MISSING_QUOTE


def test_missing_buying_power_rejects_etb_short():
    # Quote present, but available_bp unknown => cannot prove the order fits => REJECT.
    ctx = _etb_ctx(account_equity=100_000.0, current_ask=100.0, available_bp=None)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_MISSING_ACCOUNT_CONTEXT


def test_alpaca_oracle_fail_closed_on_missing_account_context():
    # Live oracle resolves shortability from get_asset, but the caller supplied no
    # account/quote context => REJECT (MISSING_ACCOUNT_CONTEXT), never ACCEPT.
    oracle = AlpacaShortabilityOracle(get_asset=lambda sym: _FakeAsset())
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_MISSING_ACCOUNT_CONTEXT


def test_etb_short_accepts_only_with_full_context_and_sufficient_bp():
    # Counterpart to the missing-context REJECT: full context + sufficient BP => ACCEPT.
    ctx = _etb_ctx(account_equity=100_000.0, current_ask=100.0, available_bp=1_000_000.0)
    v = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert v.state is AdmissibilityState.ACCEPTED
    assert v.reason is None
    assert 0.0 < v.annual_cbr < 0.02


def test_audit_mode_scopes_out_missing_account_context_but_live_default_rejects():
    # The require_account_context flag is the ONLY difference between the live precondition
    # (fail-closed default) and the offline shortability audit. Same ETB ctx with no account
    # context: live default REJECTS, audit mode ACCEPTS (shortability resolved).
    ctx = _etb_ctx(account_equity=None)  # also drops current_ask / available_bp
    live = evaluate_admissibility("AAPL", "short", 100, T, ctx)
    assert live.state is AdmissibilityState.REJECTED
    assert live.reason == REASON_MISSING_ACCOUNT_CONTEXT

    audit = evaluate_admissibility(
        "AAPL", "short", 100, T, ctx, require_account_context=False
    )
    assert audit.state is AdmissibilityState.ACCEPTED
    assert audit.reason is None


# --------------------------------------------------------------------------- #
# PARTIAL + ACCEPT cases
# --------------------------------------------------------------------------- #
def test_ssr_marketable_short_partial():
    # Full account context: SSR is reached only once the BP+equity preconditions clear.
    ctx = _etb_ctx(
        ssr_active=True,
        is_marketable=True,
        current_ask=100.0,
        available_bp=1_000_000.0,
    )
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
    # Live ACCEPT requires the account/quote context the BP+equity preconditions need.
    ctx = AdmissibilityContext(
        account_equity=100_000.0, current_ask=100.0, available_bp=1_000_000.0
    )
    v = oracle.verdict("AAPL", "short", 100, T, ctx)
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


def test_null_oracle_accepts_missing_account_context():
    # Flag-OFF bit-for-bit no-op: even an ETB short with NO account/quote context
    # (which the live path now REJECTS) is still ACCEPTED by the NullOracle. The
    # fail-closed account-context hardening must not leak into flag-OFF behavior.
    null = NullShortabilityOracle()
    v = null.verdict("AAPL", "short", 100, T, AdmissibilityContext())
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
    # Offline shortability audit: ACCEPTS short-eligible names WITHOUT live quote/BP context
    # (the audit scopes itself to shortability; the live path requires the account context).
    v = oracle.verdict("AAPL", "short", 100, T, AdmissibilityContext())
    assert v.state is AdmissibilityState.ACCEPTED
    assert v.annual_cbr == pytest.approx(0.0030)


def test_static_allowlist_still_rejects_present_failing_bp():
    # The audit-mode relaxation only SKIPS missing inputs; a PRESENT-and-failing BP still
    # REJECTS (the audit must not under-report a real collateral breach).
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
    ctx = AdmissibilityContext(current_ask=100.0, available_bp=1_000.0)  # 1.03*100*1000 >> 1k
    v = oracle.verdict("AAPL", "short", 1000, T, ctx)
    assert v.state is AdmissibilityState.REJECTED
    assert v.reason == REASON_INSUFFICIENT_BPR


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


def test_unit_bridge_non_finite_inputs_fail_closed_to_zero_shares():
    # The unit bridge does math.floor((abs(target_pct) * nav) / price). A non-finite
    # target_pct / nav / price must FAIL-CLOSED to 0 shares (the contract: 0 shares ->
    # oracle REJECT), NOT raise OverflowError ('cannot convert float infinity to integer')
    # or ValueError ('cannot convert float NaN to integer') out of the bridge — which
    # would abort the autonomous tick mid-watchlist instead of silencing this entry.
    inf = float("inf")
    nan = float("nan")
    # -inf target is the documented PRIMARY live trigger (kelly = -inf, -inf < 0 is True
    # so it enters the admissibility short branch).
    assert target_pct_to_shares(-inf, nav=100_000.0, price=50.0) == 0
    assert target_pct_to_shares(inf, nav=100_000.0, price=50.0) == 0
    assert target_pct_to_shares(nan, nav=100_000.0, price=50.0) == 0
    # non-finite nav / price too (e.g. a corrupt NAV source) -> fail-closed, never raise.
    assert target_pct_to_shares(-0.10, nav=inf, price=50.0) == 0
    assert target_pct_to_shares(-0.10, nav=nan, price=50.0) == 0
    assert target_pct_to_shares(-0.10, nav=100_000.0, price=inf) == 0
    assert target_pct_to_shares(-0.10, nav=100_000.0, price=nan) == 0
    # finite inputs are unchanged (byte-identical to before the guard).
    assert target_pct_to_shares(-0.10, nav=10_000.0, price=33.0) == -30


def test_admit_or_reject_non_finite_target_no_raise(monkeypatch):
    # End-to-end: a -inf target_pct (the live `kelly = float(rg.get("kelly_fraction"))`
    # path) reaches the unit bridge at gate_order.py BEFORE any oracle verdict. The
    # PRIMARY contract is that admit_or_reject must NOT raise OverflowError/ValueError
    # out of the seam (which would abort the whole autonomous tick mid-watchlist).
    from hermes_quant.admissibility import admit_or_reject

    # Flag OFF (default): NullShortabilityOracle ACCEPTs everything bit-for-bit, but the
    # bridge must still not RAISE — qty resolves to 0 (fail-closed share count).
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    for bad in (float("-inf"), float("inf"), float("nan")):
        verdict = admit_or_reject(
            "GME", "short", bad, 100_000.0, 50.0, T,
            account_equity=100_000.0, available_bp=None,
        )
        assert verdict.qty_shares == 0  # no raise; bridge fail-closed to 0 shares

    # Flag ON: the live oracle sees a 0-share short -> REJECT (fail-closed), so the
    # verdict is admitted=False -> SILENCE_ADMISSIBILITY, never a raise/abort.
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    for bad in (float("-inf"), float("inf"), float("nan")):
        verdict = admit_or_reject(
            "GME", "short", bad, 100_000.0, 50.0, T,
            account_equity=100_000.0, available_bp=None,
        )
        assert verdict.admitted is False
        assert verdict.qty_shares == 0
