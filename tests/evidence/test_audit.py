"""Tests for hermes_quant.evidence.audit (Wave B.5 / ADR-0033 D6)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from hermes_quant.evidence.audit import (
    EvidenceAuditNode,
    supersedes_history,
    walkback_aggregated_signal,
    walkback_analyst_view,
    walkback_evidence,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView

# ---------------------------------------------------------------------------
# Mock store — decoupled from sibling task's real EvidenceStore.
# ---------------------------------------------------------------------------


@dataclass
class _MockStore:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, evidence_id: Any) -> dict[str, Any] | None:
        return self.rows.get(str(evidence_id))

    def supersedes_chain(self, evidence_id: Any) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        cur = str(evidence_id)
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            row = self.rows.get(cur)
            if not row:
                break
            chain.append(row)
            nxt = row.get("supersedes")
            cur = str(nxt) if nxt else ""
        return chain


def _row(
    eid: str,
    *,
    kind: str = "ohlcv",
    source: str = "yfinance",
    supersedes: str | None = None,
    payload_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "id": eid,
        "kind": kind,
        "source": source,
        "available_at": datetime.now(UTC).isoformat(),
        "supersedes": supersedes,
        "payload_ref": payload_ref or f"refs/{eid}.parquet",
    }


def _make_view(analyst: str = "ta", *, evidence_ids: tuple[str, ...] = ()) -> AnalystView:
    return AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.9,
        horizon="1h",
        rationale=None,
        metadata=None,
        evidence_ids=evidence_ids,
    )


def _make_signal(components: tuple[AnalystView, ...]) -> AggregatedSignal:
    return AggregatedSignal(
        asset="BTC-USD",
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-05-24T12:00:00Z"),
        direction=1,
        magnitude=0.01,
        confidence=0.65,
        confidence_raw=0.8,
        horizon="1h",
        components=components,
        aggregator="bma",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_walkback_evidence_resolves_known_id() -> None:
    eid = str(uuid4())
    store = _MockStore(rows={eid: _row(eid, kind="news", source="reuters")})

    tree = walkback_evidence(eid, store)

    assert tree.root_kind == "evidence"
    assert tree.root_id == eid
    assert tree.direct_evidence is not None
    assert tree.direct_evidence.found is True
    assert tree.direct_evidence.kind == "news"
    assert tree.direct_evidence.source == "reuters"
    assert tree.n_evidence_missing == 0


def test_walkback_evidence_marks_unknown_id_as_not_found() -> None:
    store = _MockStore(rows={})
    bogus = str(uuid4())

    tree = walkback_evidence(bogus, store)

    assert tree.direct_evidence is not None
    assert tree.direct_evidence.found is False
    assert tree.direct_evidence.kind is None
    assert tree.n_evidence_missing == 1
    assert tree.n_evidence_total == 1


def test_walkback_analyst_view_returns_evidence_per_id() -> None:
    e1, e2 = str(uuid4()), str(uuid4())
    store = _MockStore(rows={e1: _row(e1), e2: _row(e2, kind="orderbook")})
    view = _make_view("microstructure", evidence_ids=(e1, e2))

    tree = walkback_analyst_view(view, store)

    assert tree.root_kind == "analyst_view"
    assert tree.direct_analyst is not None
    assert tree.direct_analyst.analyst == "microstructure"
    assert len(tree.direct_analyst.evidence) == 2
    assert all(ev.found for ev in tree.direct_analyst.evidence)
    kinds = {ev.kind for ev in tree.direct_analyst.evidence}
    assert kinds == {"ohlcv", "orderbook"}


def test_walkback_analyst_view_handles_empty_evidence_ids() -> None:
    store = _MockStore(rows={})
    view = _make_view("ta", evidence_ids=())

    tree = walkback_analyst_view(view, store)

    assert tree.direct_analyst is not None
    assert tree.direct_analyst.evidence == ()
    assert tree.n_evidence_total == 0
    assert tree.n_evidence_missing == 0


def test_walkback_aggregated_signal_walks_each_component() -> None:
    e1, e2, e3 = str(uuid4()), str(uuid4()), str(uuid4())
    store = _MockStore(rows={e1: _row(e1), e2: _row(e2), e3: _row(e3)})
    v1 = _make_view("ta", evidence_ids=(e1,))
    v2 = _make_view("kronos", evidence_ids=(e2, e3))
    signal = _make_signal((v1, v2))

    tree = walkback_aggregated_signal(signal, store)

    assert tree.root_kind == "aggregated_signal"
    assert tree.aggregated_signal is not None
    assert tree.aggregated_signal.aggregator == "bma"
    assert tree.aggregated_signal.component_count == 2
    assert len(tree.aggregated_signal.components) == 2
    # First component has 1 evidence; second has 2
    assert len(tree.aggregated_signal.components[0].evidence) == 1
    assert len(tree.aggregated_signal.components[1].evidence) == 2
    assert tree.n_evidence_total == 3
    assert tree.n_evidence_missing == 0


def test_walkback_aggregated_signal_counts_missing_evidence_correctly() -> None:
    present = str(uuid4())
    missing1 = str(uuid4())
    missing2 = str(uuid4())
    store = _MockStore(rows={present: _row(present)})
    v1 = _make_view("ta", evidence_ids=(present, missing1))
    v2 = _make_view("kronos", evidence_ids=(missing2,))
    signal = _make_signal((v1, v2))

    tree = walkback_aggregated_signal(signal, store)

    assert tree.n_evidence_total == 3
    assert tree.n_evidence_missing == 2
    # Verify the actual found flags
    flags = [ev.found for c in tree.aggregated_signal.components for ev in c.evidence]
    assert flags.count(True) == 1
    assert flags.count(False) == 2


def test_audit_tree_to_json_roundtrip() -> None:
    eid = str(uuid4())
    store = _MockStore(rows={eid: _row(eid)})
    view = _make_view("ta", evidence_ids=(eid,))
    signal = _make_signal((view,))

    tree = walkback_aggregated_signal(signal, store)
    payload = json.loads(tree.to_json())

    assert isinstance(payload, dict)
    assert payload["root_kind"] == "aggregated_signal"
    assert payload["aggregated_signal"]["aggregator"] == "bma"
    assert payload["aggregated_signal"]["components"][0]["analyst"] == "ta"
    assert payload["n_evidence_total"] == 1
    assert payload["n_evidence_missing"] == 0


def test_audit_tree_to_tree_text_marks_missing_evidence_with_bang() -> None:
    present = str(uuid4())
    missing = str(uuid4())
    store = _MockStore(rows={present: _row(present)})
    view = _make_view("ta", evidence_ids=(present, missing))
    signal = _make_signal((view,))

    tree = walkback_aggregated_signal(signal, store)
    text = tree.to_tree_text()

    # The line for the missing evidence should be marked with "!"
    lines = text.split("\n")
    missing_lines = [ln for ln in lines if "!" in ln and "Evidence(" in ln]
    assert len(missing_lines) == 1, f"Expected 1 bang-marked line, got: {missing_lines}"
    # Sanity: human-readable header present
    assert any("AggregatedSignal" in ln for ln in lines)
    assert "1 missing" in text


def test_supersedes_history_returns_most_recent_first() -> None:
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    # Chain: C -> B -> A (C is the most recent correction; A is the original)
    store = _MockStore(
        rows={
            a: _row(a, supersedes=None),
            b: _row(b, supersedes=a),
            c: _row(c, supersedes=b),
        }
    )

    history = supersedes_history(c, store)

    assert len(history) == 3
    assert history[0].evidence_id == c
    assert history[1].evidence_id == b
    assert history[2].evidence_id == a
    assert all(isinstance(n, EvidenceAuditNode) for n in history)
    assert history[0].supersedes == b
    assert history[2].supersedes is None


def test_supersedes_history_terminates_on_broken_chain() -> None:
    # Cycle: A -> B -> A. Mock store's chain walker uses `seen` set to guard.
    a, b = str(uuid4()), str(uuid4())
    store = _MockStore(
        rows={
            a: _row(a, supersedes=b),
            b: _row(b, supersedes=a),
        }
    )

    history = supersedes_history(a, store)

    # Cycle protection should terminate after visiting each node at most once.
    assert len(history) == 2
    assert {n.evidence_id for n in history} == {a, b}
