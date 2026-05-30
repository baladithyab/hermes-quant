"""Tests for hermes_quant.shadow.pmcc — PMCC shadow structure tracker."""
from __future__ import annotations

from datetime import date

from hermes_quant.shadow.pmcc import (
    OptionLeg,
    PMCCPosition,
    bs_call_delta,
    load_pmcc_positions,
    mark_pmcc,
    record_pmcc,
)


def _amzn_pmcc() -> PMCCPosition:
    # The live structure from the 2026-05-30 analysis: deep-ITM LEAPS + near OTM short.
    return PMCCPosition(
        symbol="AMZN",
        opened_at="2026-05-30T18:00:00+00:00",
        long_leg=OptionLeg("long", "2027-12-17", 205.0, 86.90, 0.48, 1),
        short_leg=OptionLeg("short", "2026-07-02", 285.0, 4.88, 0.32, 1),
        spot_at_open=270.64,
        note="Phase-1 PMCC shadow; ADR-0029 multi-leg reactor not yet live.",
    )


def test_net_debit_matches_analysis():
    pos = _amzn_pmcc()
    # (86.90 - 4.88) * 100 = 8202
    assert abs(pos.net_debit() - 8202.0) < 1.0


def test_bs_deep_itm_leaps_high_delta():
    # deep-ITM (K=205, S=270.64) LEAPS should be ~0.8 delta
    d = bs_call_delta(270.64, 205.0, 566 / 365, 0.48)
    assert 0.75 <= d <= 0.90


def test_mark_at_open_is_near_zero_pnl():
    pos = _amzn_pmcc()
    mark = mark_pmcc(pos, spot=270.64, asof=date(2026, 5, 30))
    # at open, marked at entry IVs, net_value ~= net_debit (small BS-vs-quoted gap ok)
    assert abs(mark.unrealized_pnl) < 1500  # within model-vs-quote slack
    assert mark.long_dte == _days("2027-12-17", "2026-05-30")
    assert mark.short_dte == _days("2026-07-02", "2026-05-30")


def test_net_theta_is_positive_collect():
    """The defining PMCC property: near-ATM short bleed collected > slow LEAPS bleed."""
    pos = _amzn_pmcc()
    mark = mark_pmcc(pos, spot=270.64, asof=date(2026, 5, 30))
    assert mark.net_theta_day > 0, f"expected net-positive theta, got {mark.net_theta_day}"


def test_net_delta_between_zero_and_long_delta():
    pos = _amzn_pmcc()
    mark = mark_pmcc(pos, spot=270.64, asof=date(2026, 5, 30))
    # net delta = long(~81) - short(~33) ~= 48, in (0, 100)
    assert 0 < mark.net_delta < 100


def test_upside_move_increases_value_but_short_caps():
    pos = _amzn_pmcc()
    base = mark_pmcc(pos, spot=270.64, asof=date(2026, 6, 15))
    up = mark_pmcc(pos, spot=300.0, asof=date(2026, 6, 15))
    assert up.net_value > base.net_value  # still net-positive on an up move
    # but short leg gained value too (works against us) — net delta < pure-long delta
    assert up.net_delta < bs_call_delta(300.0, 205.0, _years("2027-12-17", "2026-06-15"), 0.48) * 100


def test_record_and_load_roundtrip(tmp_path):
    p = tmp_path / "pmcc.jsonl"
    pos = _amzn_pmcc()
    assert record_pmcc(pos, path=p) == 1
    loaded = load_pmcc_positions(path=p)
    assert len(loaded) == 1
    assert loaded[0].symbol == "AMZN"
    assert loaded[0].long_leg.strike == 205.0
    assert loaded[0].short_leg.side == "short"
    assert abs(loaded[0].net_debit() - 8202.0) < 1.0


def test_load_missing_returns_empty(tmp_path):
    assert load_pmcc_positions(path=tmp_path / "nope.jsonl") == []


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _years(a: str, b: str) -> float:
    return _days(a, b) / 365.0
