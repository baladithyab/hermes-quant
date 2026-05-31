"""Unit tests for hermes_quant.reconcile.pmcc_shadow (ADR-0029 §2.7).

Deterministic, no network. Records a PMCC shadow with note==multi_leg_id, then
reconcile_pmcc_shadow marks it and joins on the id; a forced net-negative model theta
flags severity='build_bug_suspected'.
"""

from __future__ import annotations

from datetime import date

from hermes_quant.reconcile.pmcc_shadow import reconcile_pmcc_shadow
from hermes_quant.shadow.pmcc import OptionLeg, PMCCPosition, record_pmcc


def _pmcc(note: str, *, short_expiry="2026-07-02") -> PMCCPosition:
    return PMCCPosition(
        symbol="NVDA",
        opened_at="2026-05-30T18:00:00+00:00",
        long_leg=OptionLeg("long", "2027-12-17", 120.0, 48.0, 0.45, 1),
        short_leg=OptionLeg("short", short_expiry, 180.0, 3.5, 0.40, 1),
        spot_at_open=165.0,
        note=note,
    )


def test_join_on_multi_leg_id_returns_divergence(tmp_path) -> None:
    store = tmp_path / "pmcc.jsonl"
    record_pmcc(_pmcc("prop_mleg_001"), path=store)
    rows = reconcile_pmcc_shadow(
        asof=date(2026, 6, 1),
        spot_by_symbol={"NVDA": 168.0},
        real_marks_by_mleg_id={"prop_mleg_001": 4500.0},
        path=store,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.multi_leg_id == "prop_mleg_001"
    assert r.symbol == "NVDA"
    assert r.real_net_value == 4500.0
    assert r.net_value_divergence == r.model_net_value - 4500.0
    # A well-formed PMCC collects net theta (>0) => ok.
    assert r.model_net_theta_day > 0
    assert r.severity == "ok"


def test_real_mark_missing_severity(tmp_path) -> None:
    store = tmp_path / "pmcc.jsonl"
    record_pmcc(_pmcc("prop_mleg_002"), path=store)
    rows = reconcile_pmcc_shadow(
        asof=date(2026, 6, 1),
        spot_by_symbol={"NVDA": 168.0},
        real_marks_by_mleg_id={},  # no real mark for this id
        path=store,
    )
    assert rows[0].severity == "real_mark_missing"
    assert rows[0].net_value_divergence is None


def test_net_negative_theta_flags_build_bug(tmp_path) -> None:
    """A 'pmcc' whose MODEL theta comes back net-negative is a build bug (inverted
    structure): force it by swapping the legs so the short bleed is dwarfed."""
    store = tmp_path / "pmcc.jsonl"
    # Construct an inverted structure: a near-dated LONG + far-dated SHORT pays net
    # theta (theta-burning), which a real PMCC never does.
    bad = PMCCPosition(
        symbol="NVDA",
        opened_at="2026-05-30T18:00:00+00:00",
        long_leg=OptionLeg("long", "2026-06-19", 165.0, 6.0, 0.40, 1),  # near-dated long
        short_leg=OptionLeg("short", "2027-12-17", 120.0, 50.0, 0.45, 1),  # far-dated short
        spot_at_open=165.0,
        note="prop_mleg_bad",
    )
    record_pmcc(bad, path=store)
    rows = reconcile_pmcc_shadow(
        asof=date(2026, 6, 1),
        spot_by_symbol={"NVDA": 165.0},
        real_marks_by_mleg_id={"prop_mleg_bad": 0.0},
        path=store,
    )
    assert rows[0].model_net_theta_day < 0
    assert rows[0].severity == "build_bug_suspected"


def test_non_reactor_stamped_note_skipped(tmp_path) -> None:
    store = tmp_path / "pmcc.jsonl"
    record_pmcc(_pmcc(""), path=store)  # Phase-1 prose note (empty here)
    rows = reconcile_pmcc_shadow(
        asof=date(2026, 6, 1),
        spot_by_symbol={"NVDA": 168.0},
        real_marks_by_mleg_id={},
        path=store,
    )
    assert rows == []  # not joined (no multi_leg_id)


def test_missing_spot_skips_position(tmp_path) -> None:
    store = tmp_path / "pmcc.jsonl"
    record_pmcc(_pmcc("prop_mleg_003"), path=store)
    rows = reconcile_pmcc_shadow(
        asof=date(2026, 6, 1),
        spot_by_symbol={},  # no spot for NVDA
        real_marks_by_mleg_id={"prop_mleg_003": 4500.0},
        path=store,
    )
    assert rows == []
