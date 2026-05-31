"""W1 eval gate — the self-evolution loop is LIVE (capability-map O1).

The keystone fix: ``DecisionLog.record_decision`` had zero production callers, so the
reflection→retriever→PM-prompt edge (the one closed-in-code feedback edge) was DARK —
``read_pending()`` was always empty and nothing could be reflected on. W1 wires the
open-side recorder (``maybe_record_decision_on_open``) symmetric with the existing
close-side hook, so the loop has source-water.

This test is scored on LOOP LIVENESS (a pending→resolved chain produces a non-empty
lessons block), NOT on any alpha claim — W1 is plumbing. It also re-asserts the
Oracle-Fallacy guard (tau_observable < asof) still excludes future reflections, so
igniting the loop did not weaken the lookahead-honesty rail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from hermes_quant.memory._paper_reflection_hook import (
    maybe_record_decision_on_open,
    maybe_reflect_on_close,
)
from hermes_quant.memory.decisions import DecisionLog
from hermes_quant.memory.retriever import format_context_block, get_past_context


def _record(*, asset: str, fill_size_pct: float, decision_price: float, fill_price: float,
            asof_execution: str, asset_class: str = "equity"):
    """Minimal stand-in for the ExecutionRecord the reactor passes the hooks."""
    return SimpleNamespace(
        asset=asset, fill_size_pct=fill_size_pct, decision_price=decision_price,
        fill_price=fill_price, asof_execution=asof_execution, asof_decision=asof_execution,
        asset_class=asset_class,
    )


def _proposal(*, asof: str, direction: int, confidence: float = 0.7):
    """Minimal stand-in for the Proposal (carries advisor_result)."""
    return SimpleNamespace(advisor_result={
        "as_of": asof,
        "asset_class": "equity",
        "aggregated_signal": {"direction": direction, "confidence": confidence,
                              "rationale": "loop-liveness test thesis"},
        "risk_gate": {"recommended_action": "long_with_stop" if direction > 0 else "short_with_stop"},
    })


def _patch_paths(monkeypatch, tmp_path: Path):
    """Point both the decision log and the reflector at tmp_path (no ~/.hermes writes)."""
    import hermes_quant.memory.decisions as dmod
    import hermes_quant.memory.reflector as rmod
    monkeypatch.setattr(dmod, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(rmod, "REFLECTIONS_PATH", tmp_path / "reflections.jsonl")
    return tmp_path / "decisions.jsonl", tmp_path / "reflections.jsonl"


def test_w1_open_records_pending_decision(monkeypatch, tmp_path) -> None:
    """An OPENING fill writes a `pending` decision (the source-water that was missing)."""
    dpath, _ = _patch_paths(monkeypatch, tmp_path)
    rec = _record(asset="AAPL", fill_size_pct=0.10, decision_price=150.0, fill_price=150.0,
                  asof_execution="2026-05-20T16:00:00+00:00")
    prop = _proposal(asof="2026-05-20T16:00:00+00:00", direction=1)

    maybe_record_decision_on_open(rec, prop)

    pending = [r for r in DecisionLog().read_pending() if r["ticker"] == "AAPL"]
    assert len(pending) == 1, "open fill must record exactly one pending decision"
    assert pending[0]["direction"] == 1
    assert pending[0]["state"] == "pending"


def test_w1_zero_fill_records_nothing(monkeypatch, tmp_path) -> None:
    """A 0-fill (e.g. admissibility reject) opens no decision."""
    _patch_paths(monkeypatch, tmp_path)
    rec = _record(asset="AAPL", fill_size_pct=0.0, decision_price=150.0, fill_price=0.0,
                  asof_execution="2026-05-20T16:00:00+00:00")
    maybe_record_decision_on_open(rec, _proposal(asof="2026-05-20T16:00:00+00:00", direction=1))
    assert list(DecisionLog().read_pending()) == []


def test_w1_full_loop_liveness_open_close_reflect_readback(monkeypatch, tmp_path) -> None:
    """THE eval gate: open → pending → close → reflection → readback lessons_block.

    Drives the exact two hooks the reactor calls. After a long open and a closing
    fill, get_past_context at a LATER asof must surface the reflection in a
    non-empty lessons block — proving the loop is end-to-end live.
    """
    _patch_paths(monkeypatch, tmp_path)
    refl_path = tmp_path / "reflections.jsonl"
    dec_path = tmp_path / "decisions.jsonl"

    # 1. OPEN: long AAPL at 150.
    open_rec = _record(asset="AAPL", fill_size_pct=0.10, decision_price=150.0, fill_price=150.0,
                       asof_execution="2026-05-20T16:00:00+00:00")
    maybe_record_decision_on_open(open_rec, _proposal(asof="2026-05-20T16:00:00+00:00", direction=1))
    maybe_reflect_on_close(open_rec, _proposal(asof="2026-05-20T16:00:00+00:00", direction=1))

    # 2. CLOSE: opposite-sign fill at 160 (a win). The close hook resolves the pending
    #    decision and writes a reflection.
    close_rec = _record(asset="AAPL", fill_size_pct=-0.10, decision_price=160.0, fill_price=160.0,
                        asof_execution="2026-06-05T16:00:00+00:00")
    maybe_record_decision_on_open(close_rec, _proposal(asof="2026-06-05T16:00:00+00:00", direction=-1))
    maybe_reflect_on_close(close_rec, _proposal(asof="2026-06-05T16:00:00+00:00", direction=-1))

    # The decision must now be resolved (loop closed on the write side).
    resolved = [r for (d, r) in DecisionLog().read_resolved()]
    assert resolved, "close fill must resolve the pending decision into a reflection link"
    assert refl_path.exists() and refl_path.read_text().strip(), "a reflection must be persisted"

    # 3. READBACK: a FUTURE decision asof must surface the reflection (the closed edge).
    future_asof = datetime(2026, 7, 1, tzinfo=UTC)
    ctx = get_past_context(
        "AAPL", future_asof,
        reflections_path=refl_path, decisions_path=dec_path,
        only_resolved=True,
    )
    block = format_context_block(ctx)
    assert block and block.strip(), "the closed loop must yield a non-empty lessons_block"
    assert "AAPL" in block


def test_w1_oracle_guard_excludes_future_reflection(monkeypatch, tmp_path) -> None:
    """Igniting the loop must NOT weaken the Oracle-Fallacy guard: a reflection whose
    tau_observable >= asof is excluded from retrieval (lookahead-honesty preserved)."""
    _patch_paths(monkeypatch, tmp_path)
    refl_path = tmp_path / "reflections.jsonl"
    dec_path = tmp_path / "decisions.jsonl"

    open_rec = _record(asset="AAPL", fill_size_pct=0.10, decision_price=150.0, fill_price=150.0,
                       asof_execution="2026-05-20T16:00:00+00:00")
    maybe_record_decision_on_open(open_rec, _proposal(asof="2026-05-20T16:00:00+00:00", direction=1))
    close_rec = _record(asset="AAPL", fill_size_pct=-0.10, decision_price=160.0, fill_price=160.0,
                        asof_execution="2026-06-05T16:00:00+00:00")
    maybe_reflect_on_close(close_rec, _proposal(asof="2026-06-05T16:00:00+00:00", direction=-1))

    # asof BEFORE the outcome became knowable (tau_observable ~ the 2026-06-05 close):
    # the reflection MUST be excluded.
    early_asof = datetime(2026, 5, 25, tzinfo=UTC)
    ctx = get_past_context("AAPL", early_asof, reflections_path=refl_path,
                           decisions_path=dec_path, only_resolved=True)
    block = format_context_block(ctx)
    assert "AAPL" not in block, (
        "Oracle guard: a reflection whose tau_observable >= asof must NOT leak into a "
        "decision dated before the outcome was knowable"
    )
