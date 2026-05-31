"""PDR-3 convergence eval gate — the HIGHER-bar precision eval (plan §5.2, B09).

ADR-0079 Rollout PDR-3: the labeled social-arb set clears a HIGHER bar (>=0.65,
strictly above the 0.60 D74.7 floor) with the >=2-source requirement ON. The
multi-source (validated) cases survive and are directionally correct; the
single-source cases are DROPPED (not scored). External truth = realized forward
returns committed in camillo_labels.json. Fully offline + deterministic off the
versioned fixture (N13: tests/fixtures/socialarb, NEVER /tmp).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from hermes_quant.catalyst.eval import EvalCase, run_precision_with_convergence
from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import load_graph

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "socialarb"

# The PROD-loaded consumer-trend graph already produces the 5 Camillo packets.
GRAPH, ALIASES = load_graph()


def _items_for(case: dict) -> list[CatalystItem]:
    published = dt.datetime.fromisoformat(case["date"]).replace(tzinfo=dt.UTC)
    return [
        CatalystItem(
            title=raw["title"],
            published_at=published,
            source=raw["source"],
            link=raw.get("link", "n/a"),
            query="convergence-eval",
        )
        for raw in case["items"]
    ]


def _load_case_item_sets() -> list[tuple[EvalCase, list[CatalystItem]]]:
    items_fixt = json.loads((FIXT / "convergence_items.json").read_text())["cases"]
    labels = {c["ticker"]: c for c in json.loads((FIXT / "camillo_labels.json").read_text())}
    out: list[tuple[EvalCase, list[CatalystItem]]] = []
    for case in items_fixt:
        sym = case["ticker"]
        label = labels[sym]
        if label["fwd_return_pct"] is None:
            continue
        items = _items_for(case)
        # the EvalCase.item is the primary (first) item; realized return from labels
        ec = EvalCase(
            item=items[0],
            symbol=sym,
            realized_forward_return=float(label["fwd_return_pct"]),
        )
        out.append((ec, items))
    return out


def test_fixture_is_versioned_not_tmp():
    """N13: the convergence eval set is committed under tests/fixtures, never /tmp."""
    assert (FIXT / "convergence_items.json").exists()
    assert (FIXT / "camillo_labels.json").exists()


def test_convergence_clears_higher_bar_with_requirement_on(monkeypatch):
    """ADR-0079 Rollout PDR-3: the labeled set clears a HIGHER bar (>=0.65) with
    the >=2-source requirement ON. Multi-source (validated) cases survive and are
    directionally correct; single-source cases are dropped (not scored)."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    case_item_sets = _load_case_item_sets()
    res = run_precision_with_convergence(
        case_item_sets, min_hit_rate=0.65, graph=GRAPH, aliases=ALIASES
    )
    assert res.passed, (
        f"PDR-3 eval gate FAIL: hit_rate={res.hit_rate} scored={res.n_scored} "
        f"misses={res.misses}"
    )
    assert res.hit_rate >= 0.65


def test_single_source_cases_dropped_not_scored(monkeypatch):
    """The single-source negative cases (TPR, NWL) are DROPPED at emission, so they
    are not scored — only the 3 validated (multi-source) cases reach the scorer."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    case_item_sets = _load_case_item_sets()
    res = run_precision_with_convergence(
        case_item_sets, min_hit_rate=0.65, graph=GRAPH, aliases=ALIASES
    )
    # 5 cases handed in, only the 3 validated multi-source ones scored.
    assert res.n_cases == 5
    assert res.n_scored == 3, f"expected 3 validated cases scored, got {res.n_scored}"


def test_higher_bar_is_strictly_above_d747_floor():
    """The PDR-3 bar (0.65) is strictly higher than the 0.60 D74.7 precision floor."""
    # default min_hit_rate for the convergence runner is the HIGHER bar.
    import inspect

    from hermes_quant.catalyst.eval import run_precision

    sig = inspect.signature(run_precision_with_convergence)
    assert sig.parameters["min_hit_rate"].default == 0.65
    assert inspect.signature(run_precision).parameters["min_hit_rate"].default == 0.6
