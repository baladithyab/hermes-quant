"""ar102 family-completeness — graph_mining.mine_graph must finite-guard the realized
forward return, identical to its profitability.py sibling.

`mine_graph` joined the propagation log against realized forward returns per EDGE with
the guard `if fwd is None or fwd == 0:` — a non-finite bar (NaN/inf) passes it (nan==0 /
inf==0 are False), is counted (`n_scored += 1`) and summed into the edge's
`sum_signed_return`, poisoning the edge's mean to NaN. The FLIP_SIGN/PRUNE verdict keys
on `mean_signed_return > 0` / `< 0` — both False for NaN — so a genuinely-consistent edge
silently mis-mines. mine_graph is LIVE (afa4: GRAPH_MINING=1 + the weekly cron enabled),
so the propose-only candidate file is mis-generated.

Fix: `if fwd is None or not math.isfinite(fwd) or fwd == 0:` — treat non-finite as
unscored (missing data), identical to the profitability.py fix.
"""

from __future__ import annotations

import json
import math

import pytest

from hermes_quant.catalyst import graph_mining


@pytest.fixture(autouse=True)
def _enable_graph_mining(monkeypatch):
    # mine_graph is flag-gated default-OFF; the live path has GRAPH_MINING=1 (afa4).
    monkeypatch.setenv("HERMES_QUANT_GRAPH_MINING", "1")


def _propagation_log(tmp_path, rows):
    p = tmp_path / "propagation-log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _one_edge_rows(n: int) -> list[dict]:
    # One edge celsius -> CELH (brand_self), all symbol_sign=+1 (propagated direction).
    return [{
        "symbol": "CELH", "source": "celsius", "relation": "brand_self",
        "symbol_sign": 1, "effect_sign": 1, "weight": 0.5,
        "asof": f"2026-05-{i + 1:02d}T00:00:00Z",
    } for i in range(n)]


def test_ar102_graphmine_nonfinite_fwd_is_unscored(tmp_path):
    """20 honest +3% bars + one NaN bar: the edge must score ONLY the 20 finite bars
    (n_scored==20, finite sum), NOT count the NaN (n_scored==21, NaN sum = poison)."""
    log = _propagation_log(tmp_path, _one_edge_rows(21))
    calls = {"n": 0}

    def _fetcher(sym, asof_date):
        calls["n"] += 1
        return float("nan") if calls["n"] == 21 else 0.03

    out = graph_mining.mine_graph(_fetcher, path=log, min_sample=20)
    assert len(out) == 1, f"expected the one edge to be scored, got {out}"
    ev = next(iter(out.values()))
    # The NaN bar must be DROPPED (unscored), not counted + summed:
    assert ev.n_scored == 20, (
        f"NaN bar wrongly counted: n_scored={ev.n_scored} (must be 20, the finite bars only)"
    )
    assert math.isfinite(ev.sum_signed_return), (
        f"NaN leaked into the edge sum: {ev.sum_signed_return} — poisons the FLIP_SIGN/PRUNE verdict"
    )
    assert ev.sum_signed_return == pytest.approx(0.6), "20 bars × +3% (sign-aligned) = +0.60"


def test_ar102_graphmine_inf_fwd_is_unscored(tmp_path):
    """An inf bar must also be dropped (inf would make the sum inf, mean inf)."""
    log = _propagation_log(tmp_path, _one_edge_rows(21))
    calls = {"n": 0}

    def _fetcher(sym, asof_date):
        calls["n"] += 1
        return float("inf") if calls["n"] == 21 else 0.03

    out = graph_mining.mine_graph(_fetcher, path=log, min_sample=20)
    ev = next(iter(out.values()))
    assert ev.n_scored == 20
    assert math.isfinite(ev.sum_signed_return)


def test_ar102_graphmine_finite_bars_byte_identical(tmp_path):
    """Non-vacuity / byte-identity: 20 finite sign-consistent bars (no NaN) score
    exactly as before — the guard only drops non-finite, never finite data."""
    log = _propagation_log(tmp_path, _one_edge_rows(20))
    out = graph_mining.mine_graph(lambda s, d: 0.03, path=log, min_sample=20)
    ev = next(iter(out.values()))
    assert ev.n_scored == 20
    assert ev.sum_signed_return == pytest.approx(0.6)
    assert ev.hits == 20  # all sign-aligned
