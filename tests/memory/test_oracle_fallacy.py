"""tests/memory/test_oracle_fallacy.py — Canonical Oracle Fallacy guard regression
test (ADR-0042, arxiv:2605.19337 §4.2).

This test MUST exist verbatim per ADR-0042's specification.  It is the
canonical regression test for the memory-augmented financial-agent failure
mode named in arxiv:2605.19337 §4.2 ("Oracle Fallacy in episodic memory").

Do NOT modify the docstring of ``test_retriever_excludes_reflections_with_tau_observable_at_or_after_asof``.
It is quoted in ADR-0042 as a specification artifact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.memory.retriever import get_past_context


# ---------------------------------------------------------------------------
# Canonical regression test (ADR-0042 §"Compliance with the Oracle Fallacy guard")
# ---------------------------------------------------------------------------


def test_retriever_excludes_reflections_with_tau_observable_at_or_after_asof(
    tmp_path: Path,
) -> None:
    """Oracle Fallacy guard: a reflection whose outcome became knowable AT or
    AFTER the decision-asof MUST NOT be retrievable for that decision.

    This is the canonical regression test for the memory-augmented
    financial-agent failure mode named in arxiv:2605.19337 §4.2.
    """
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    rpath = tmp_path / "reflections.jsonl"
    dpath = tmp_path / "decisions.jsonl"
    spath = tmp_path / "sector-cache.json"

    # Inject a reflection whose tau_observable is exactly asof (MUST be excluded)
    _inject_reflection(
        rpath,
        reflection_id="ref_at_asof",
        ticker="AAPL",
        tau_observable=asof,  # == asof → excluded
        asof_resolution=(asof - timedelta(hours=6)).isoformat(),
    )

    # And one that is exactly 1 second before asof (MUST be included)
    _inject_reflection(
        rpath,
        reflection_id="ref_before_asof",
        ticker="AAPL",
        tau_observable=asof - timedelta(seconds=1),  # < asof → included
        asof_resolution=(asof - timedelta(hours=6, seconds=1)).isoformat(),
    )

    ctx = get_past_context(
        ticker="AAPL",
        asof=asof,
        reflections_path=rpath,
        decisions_path=dpath,
        sector_cache_path=spath,
    )

    # Only the strictly-prior one is visible
    assert len(ctx.same_ticker) == 1, (
        f"Expected 1 reflection (tau < asof), got {len(ctx.same_ticker)}. "
        "Oracle Fallacy guard failure: a reflection with tau_observable >= asof "
        "was allowed through."
    )
    from hermes_quant.memory.reflector import _parse_dt as _parse
    assert _parse(ctx.same_ticker[0].tau_observable) < asof, (
        "The visible reflection must have tau_observable strictly before asof."
    )


# ---------------------------------------------------------------------------
# Additional boundary tests (complement to the canonical test above)
# ---------------------------------------------------------------------------


def test_tau_observable_exactly_equal_to_asof_is_excluded(tmp_path: Path) -> None:
    """Boundary: tau_observable == asof must be EXCLUDED (strict less-than)."""
    asof = datetime(2026, 7, 4, 15, 0, 0, tzinfo=UTC)
    rpath = tmp_path / "reflections.jsonl"
    dpath = tmp_path / "decisions.jsonl"
    spath = tmp_path / "sector-cache.json"

    _inject_reflection(rpath, reflection_id="ref_eq", ticker="SPY",
                       tau_observable=asof)

    ctx = get_past_context("SPY", asof, reflections_path=rpath,
                           decisions_path=dpath, sector_cache_path=spath)
    assert ctx.same_ticker == [], "tau_observable == asof should be excluded"


def test_tau_observable_in_future_is_excluded(tmp_path: Path) -> None:
    """A reflection from the future (tau > asof) must be excluded."""
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    rpath = tmp_path / "reflections.jsonl"
    dpath = tmp_path / "decisions.jsonl"
    spath = tmp_path / "sector-cache.json"

    _inject_reflection(rpath, reflection_id="ref_future", ticker="AAPL",
                       tau_observable=asof + timedelta(days=1))

    ctx = get_past_context("AAPL", asof, reflections_path=rpath,
                           decisions_path=dpath, sector_cache_path=spath)
    assert ctx.same_ticker == [], "tau_observable > asof should be excluded"


def test_tau_observable_none_is_excluded(tmp_path: Path) -> None:
    """A reflection without tau_observable (None) must never be returned."""
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    rpath = tmp_path / "reflections.jsonl"
    dpath = tmp_path / "decisions.jsonl"
    spath = tmp_path / "sector-cache.json"

    row = {
        "schema_version": 1,
        "reflection_id": "ref_no_tau",
        "decision_id": "dec_no_tau",
        "asof_resolution": "2026-05-01T10:00:00+00:00",
        "tau_observable": None,  # missing → excluded
        "ticker": "AAPL",
        "raw_return": 0.02,
        "alpha_return": 0.01,
        "benchmark": "SPY",
        "holding_days": 10,
        "outcome_quality": 3,
        "reflection_text": "Null tau test.",
        "lesson_category": "unknown",
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:000000",
        "rating": "Hold",
    }
    with open(rpath, "a") as fh:
        fh.write(json.dumps(row) + "\n")

    ctx = get_past_context("AAPL", asof, reflections_path=rpath,
                           decisions_path=dpath, sector_cache_path=spath)
    assert ctx.same_ticker == [], "tau_observable=None should be excluded"


def test_multiple_valid_reflections_all_visible(tmp_path: Path) -> None:
    """All reflections with tau < asof must appear (up to k_same_ticker)."""
    asof = datetime(2026, 8, 1, tzinfo=UTC)
    rpath = tmp_path / "reflections.jsonl"
    dpath = tmp_path / "decisions.jsonl"
    spath = tmp_path / "sector-cache.json"

    for i in range(3):
        _inject_reflection(
            rpath,
            reflection_id=f"ref_valid_{i}",
            ticker="AAPL",
            tau_observable=asof - timedelta(days=i + 1),
            asof_resolution=(asof - timedelta(days=i + 1, hours=6)).isoformat(),
        )

    ctx = get_past_context("AAPL", asof, k_same_ticker=10,
                           reflections_path=rpath, decisions_path=dpath,
                           sector_cache_path=spath)
    assert len(ctx.same_ticker) == 3


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _inject_reflection(
    path: Path,
    *,
    reflection_id: str,
    ticker: str,
    tau_observable: datetime | None,
    asof_resolution: str | None = None,
    alpha_return: float = 0.01,
) -> None:
    """Write a minimal reflection row directly to a JSONL file."""
    row = {
        "schema_version": 1,
        "reflection_id": reflection_id,
        "decision_id": f"dec_{reflection_id}",
        "asof_resolution": asof_resolution or "2026-05-15T10:00:00+00:00",
        "tau_observable": tau_observable.isoformat() if tau_observable is not None else None,
        "ticker": ticker.upper(),
        "raw_return": 0.02,
        "alpha_return": alpha_return,
        "benchmark": "SPY",
        "holding_days": 10,
        "outcome_quality": 3,
        "reflection_text": f"Test reflection for {ticker}.",
        "lesson_category": "unknown",
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:test",
        "rating": "Hold",
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
