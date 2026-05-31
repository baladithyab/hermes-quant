"""PDR-3 rails proof: flag-OFF synthesize is byte-identical (plan §5.3).

HERMES_QUANT_CONVERGENCE absent => synthesize_packets output AND propagation_log
are bit-for-bit the pre-PDR-3 baseline (no haircut, no drop, no
metadata['convergence'] key). The golden was captured on current HEAD BEFORE the
two-pass refactor (tests/fixtures/socialarb/_pdr3_golden.json) so this is a true
baseline, not a self-fulfilling snapshot. This guards the rail: the two-pass split
must not reorder/duplicate propagate() calls or alter the emitted packets.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import load_graph
from hermes_quant.catalyst.synthesize import synthesize_packets

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "socialarb"
GRAPH, ALIASES = load_graph()

_GOLDEN = json.loads((FIXT / "_pdr3_golden.json").read_text())
_GOLDEN_OUTPUT = _GOLDEN["golden_output"]
_GOLDEN_PROP_LOG = _GOLDEN["golden_prop_log"]


def _all_items() -> list[CatalystItem]:
    """The flattened fixture item set in FIXTURE ORDER (the golden input order)."""
    cases = json.loads((FIXT / "convergence_items.json").read_text())["cases"]
    out: list[CatalystItem] = []
    for case in cases:
        published = dt.datetime.fromisoformat(case["date"]).replace(tzinfo=dt.UTC)
        for raw in case["items"]:
            out.append(
                CatalystItem(
                    title=raw["title"],
                    published_at=published,
                    source=raw["source"],
                    link=raw["link"],
                    query="convergence-eval",
                )
            )
    return out


def _normalize(obj):
    """Round-trip through json with default=str so the comparison matches how the
    golden was frozen (datetimes etc. serialized identically)."""
    return json.loads(json.dumps(obj, default=str))


def test_flag_off_synthesize_byte_identical(monkeypatch):
    """Flag absent => output is bit-for-bit the pre-PDR-3 golden; no convergence key."""
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    pkts = synthesize_packets(_all_items(), graph=GRAPH, aliases=ALIASES)
    out = _normalize([p.to_dict(include_hash=True) for p in pkts])
    assert out == _GOLDEN_OUTPUT, "flag-OFF synthesize output diverged from golden"
    assert all("convergence" not in (p.metadata or {}) for p in pkts)


def test_flag_off_explicit_zero_byte_identical(monkeypatch):
    """An explicit '0' is identical to absent (default-OFF discipline)."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "0")
    pkts = synthesize_packets(_all_items(), graph=GRAPH, aliases=ALIASES)
    out = _normalize([p.to_dict(include_hash=True) for p in pkts])
    assert out == _GOLDEN_OUTPUT


def test_flag_off_propagation_log_order_unchanged(monkeypatch):
    """The two-pass refactor must not change propagation_log ordering/contents."""
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    log_new: list[dict] = []
    synthesize_packets(
        _all_items(), graph=GRAPH, aliases=ALIASES, propagation_log=log_new
    )
    assert _normalize(log_new) == _GOLDEN_PROP_LOG, (
        "propagation_log ordering/contents changed (two-pass reordered propagate())"
    )


def test_flag_on_drops_single_source_but_validated_survive(monkeypatch):
    """Flag ON: single-source (TPR, NWL) dropped; validated multi-source survive
    with a convergence stamp. Confirms the OFF baseline is not vacuous."""
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    pkts = synthesize_packets(_all_items(), graph=GRAPH, aliases=ALIASES)
    assets = {p.asset for p in pkts}
    assert assets == {"CELH", "CROX", "DIIBF"}, f"unexpected survivors: {assets}"
    assert all((p.metadata or {}).get("convergence", {}).get("validated") for p in pkts)
    # strictly fewer packets than flag-OFF golden (PDR-3 can only SUBTRACT).
    assert len(pkts) < len(_GOLDEN_OUTPUT)


def test_flag_on_never_amplifies(monkeypatch):
    """Rail: PDR-3 can only SUBTRACT. A surviving validated packet's confidence is
    UNCHANGED vs flag-OFF (no amplification)."""
    items = _all_items()
    monkeypatch.delenv("HERMES_QUANT_CONVERGENCE", raising=False)
    off = {(p.asset, p.summary): p.confidence for p in synthesize_packets(items, graph=GRAPH, aliases=ALIASES)}
    monkeypatch.setenv("HERMES_QUANT_CONVERGENCE", "1")
    on = synthesize_packets(items, graph=GRAPH, aliases=ALIASES)
    for p in on:
        assert p.confidence <= off[(p.asset, p.summary)] + 1e-9, (
            f"{p.asset} confidence amplified by PDR-3 (rail violation)"
        )
