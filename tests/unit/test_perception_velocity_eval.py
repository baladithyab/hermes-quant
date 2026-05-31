"""PDR-2 D74.7 eval gate (plan §4, §6 T5/T6) — THE PROMOTION GATE.

Reuses catalyst/eval.py UNCHANGED in shape: with velocity-sourced magnitude (flag ON),
directional precision must still clear >= 0.6 hit-rate vs REAL forward returns, and the
negative control must still produce ZERO packets. External truth (committed yfinance
returns), never self-graded. Fully offline/deterministic off the versioned fixture (N13).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from hermes_quant.catalyst.eval import EvalCase, eval_gate, run_precision
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import load_graph
from hermes_quant.perception.velocity import compute_trend_velocity

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "socialarb"

# The PROD-loaded consumer-trend graph already produces the 5 Camillo packets (the
# brand_self edges were promoted to the seed after the n=5 eval cleared 0.60). We pass
# it explicitly so the harness mutates nothing live (mirrors the ops eval script).
GRAPH, ALIASES = load_graph()


def _item(headline: str, date: str) -> CatalystItem:
    return CatalystItem(
        title=headline,
        published_at=dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC),
        source="phase0-label",
        link="n/a",
        query="social-arb-eval",
    )


def _velocity_by_symbol() -> dict[str, dict]:
    """Reconstruct the per-symbol velocity scores from the committed interest series
    via the REAL producer (no I/O of live counts; fully reproducible)."""
    series = json.loads((FIXT / "interest_series.json").read_text())
    out: dict[str, dict] = {}
    for sym, s in series.items():
        idx = pd.PeriodIndex(
            [pd.Timestamp(c["week_start"]).to_period(s["freq"]) for c in s["counts"]]
        )
        counts = pd.Series([c["n"] for c in s["counts"]], index=idx)
        sc = compute_trend_velocity(counts, asof=s["asof"])
        assert sc is not None, f"{sym} series too short to score"
        out[sym] = sc.to_mapping()
    return out


def _load_cases() -> tuple[list[EvalCase], dict[str, dict]]:
    labels = json.loads((FIXT / "camillo_labels.json").read_text())  # versioned, NOT /tmp
    cases: list[EvalCase] = []
    for c in labels:
        if c["fwd_return_pct"] is None:
            continue
        cases.append(
            EvalCase(
                item=_item(c["headline"], c["date"]),
                symbol=c["ticker"],
                realized_forward_return=float(c["fwd_return_pct"]),
            )
        )
    return cases, _velocity_by_symbol()


_BENIGN = [
    _item("Celsius reports quarterly results in line with expectations", "2024-01-15"),
    _item("Crocs announces routine board meeting schedule for the year", "2024-02-01"),
    _item("Tapestry to present at investor conference next month", "2024-03-01"),
    _item("Newell updates corporate governance guidelines", "2024-01-20"),
]


def test_fixture_is_versioned_not_tmp():
    """N13: the eval set is committed under tests/fixtures, never /tmp."""
    assert (FIXT / "camillo_labels.json").exists()
    assert (FIXT / "interest_series.json").exists()
    assert (FIXT / "README.md").exists()


def test_d747_velocity_sourced_magnitude_clears_precision_bar(monkeypatch):
    """ADR-0079 Rollout PDR-2 eval gate: with velocity-sourced magnitude (flag ON),
    directional precision is >= 0.6 hit-rate vs REAL forward returns (D74.7). External
    truth. Re-sourcing magnitude must not BREAK the directional bar."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    cases, vel = _load_cases()
    res = run_precision(
        cases, min_hit_rate=0.6, graph=GRAPH, aliases=ALIASES, velocity_by_symbol=vel
    )
    assert res.passed, (
        f"D74.7 FAIL: hit_rate={res.hit_rate} scored={res.n_scored} misses={res.misses}"
    )
    assert res.n_scored == 5
    assert res.hit_rate >= 0.6


def test_velocity_sourced_magnitude_in_band(monkeypatch):
    """Every velocity-sourced packet magnitude lands inside the severity band
    [0, 0.06] (rail #2: a flag flip cannot hand BMA an out-of-band magnitude)."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    from hermes_quant.catalyst.synthesize import synthesize_packets
    from hermes_quant.perception.velocity import VELOCITY_MAGNITUDE_CEIL, VELOCITY_MAGNITUDE_FLOOR

    cases, vel = _load_cases()
    for case in cases:
        packets = synthesize_packets(
            [case.item], graph=GRAPH, aliases=ALIASES, velocity_by_symbol=vel
        )
        for p in packets:
            if p.metadata.get("magnitude_source") == "velocity":
                assert VELOCITY_MAGNITUDE_FLOOR <= p.magnitude <= VELOCITY_MAGNITUDE_CEIL, (
                    f"{p.asset} velocity magnitude {p.magnitude} out of band"
                )


def test_negative_control_zero_packets_with_velocity_on(monkeypatch):
    """Benign headlines still produce ZERO packets with velocity ON (cry-wolf guard).
    Velocity sources magnitude, never EMISSION — a benign headline that fires no
    classifier polarity stays silent regardless of the flag."""
    monkeypatch.setenv("HERMES_QUANT_TREND_VELOCITY", "1")
    cases, _vel = _load_cases()
    passed, neg, prec, sign = eval_gate(
        _BENIGN, cases, min_hit_rate=0.6, graph=GRAPH, aliases=ALIASES
    )
    assert neg.passed, f"negative control fired packets: {neg.spurious}"
    assert neg.n_spurious_packets == 0
