"""tests/grounding/test_enforcement.py — seam that wires ClaimVerifier into decisions.

Seed 24ba (REVIEW-SYNTHESIS-20260604): ClaimVerifier was built + tested but had
ZERO instantiating callers outside grounding/. The citation HARD_RULE had no
teeth — an analyst claim with a fabricated/ungrounded numeric reached the
decision ANNOTATED but not DROPPED.

`enforce_grounding(views, ctx)` is the enforcement seam: for any analyst view
that OPTED INTO grounding (metadata carries a grounding marker), it verifies the
view's numeric claims against the GroundTruthBlock in ``ctx.extras`` and DROPS
the view from the vote when verification fails. Fail-CLOSED: an ungrounded claim
never reaches the aggregator.

Invariants asserted here:
  - A grounded view with an uncited fabricated number is DROPPED.
  - A grounded view whose numbers all trace to ground truth is KEPT (unchanged).
  - A grounded view with no numeric claims is KEPT.
  - A NON-grounded view (deterministic analyst, no grounding marker) is KEPT even
    if its rationale contains decimals (those are internal scores, not claims).
  - No GroundTruthBlock in ctx.extras → identity passthrough (today's default
    advisor path → byte-identical).
  - Unset or HERMES_QUANT_GROUNDING_ENFORCE=0 → identity passthrough.
  - HERMES_QUANT_GROUNDING_ENFORCE=1 → enforcement active.
  - A view whose grounding ERRORED (with_grounding=False marker) is still verified
    (fail-closed — it wanted grounding, so it does not get a free pass).
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.grounding.data_grounding import Bar, build_ground_truth_block
from hermes_quant.grounding.enforcement import enforce_grounding
from hermes_quant.protocol import AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_block(symbol: str = "AAPL", n_bars: int = 10):
    """A small GroundTruthBlock with closes 170.00 .. 174.50 (predictable)."""
    from datetime import date, timedelta

    bars = []
    d = date(2026, 5, 1)
    for i in range(n_bars):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = 170.00 + i * 0.50
        bars.append(
            Bar(
                date_str=d.isoformat(),
                open=close - 0.25,
                high=close + 0.50,
                low=close - 0.50,
                close=close,
                volume=5_000_000,
            )
        )
        d += timedelta(days=1)
    return build_ground_truth_block(symbol, "2026-05-27", ohlcv_bars=bars)


def _ctx(*, block=None, extra=None) -> MarketContext:
    extras: dict = {}
    if block is not None:
        extras["ground_truth_block"] = block
    if extra:
        extras.update(extra)
    return MarketContext(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-05-27", tz="UTC")],
                "open": [170.0],
                "high": [175.0],
                "low": [169.0],
                "close": [174.5],
                "volume": [5_000_000.0],
            }
        ),
        last_close=174.5,
        last_volume=5_000_000.0,
        asof=pd.Timestamp("2026-05-27", tz="UTC"),
        extras=extras,
    )


def _grounded_view(rationale: str, *, analyst: str = "hermes_semantic", marker=True) -> AnalystView:
    """A view that opted into grounding (carries a grounding marker in metadata)."""
    if marker is True:
        meta = {"with_grounding": True, "ground_truth_symbol": "AAPL"}
    elif marker == "error":
        meta = {"with_grounding": False, "grounding_error": True}
    else:
        meta = {}
    return AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.75,
        horizon="1d",
        rationale=rationale,
        metadata=meta,
    )


def _plain_view(rationale: str, *, analyst: str = "classical_ta") -> AnalystView:
    """A deterministic-analyst view that never opted into grounding (no marker)."""
    return AnalystView(
        analyst=analyst,
        direction=1,
        magnitude=0.01,
        confidence=0.6,
        confidence_raw=0.75,
        horizon="1d",
        rationale=rationale,
        metadata={"sub_signals": []},
    )


@pytest.fixture
def grounding_enforced(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_GROUNDING_ENFORCE", "1")


# ---------------------------------------------------------------------------
# Drop / keep behavior
# ---------------------------------------------------------------------------


def test_drops_grounded_view_with_uncited_claim(grounding_enforced):
    """A grounded view citing a fabricated price (999.99 ∉ block) is dropped."""
    block = _make_block("AAPL")
    view = _grounded_view("Target price is 999.99 with strong upside.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [], "ungrounded grounded-view must be dropped from the vote"
    assert len(dropped) == 1
    assert dropped[0]["analyst"] == "hermes_semantic"


def test_keeps_grounded_view_with_all_cited_claims(grounding_enforced):
    """A grounded view whose only number is a real GT close is kept, unchanged."""
    block = _make_block("AAPL")
    real_close = block.ohlcv_60d[-1].close  # 174.50
    view = _grounded_view(f"Close confirmed at {real_close:.4f} on ground truth.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [view], "fully-cited grounded view must survive identically"
    assert dropped == []
    assert kept[0] is view, "kept view must be the SAME object (no copy)"


def test_keeps_grounded_view_with_no_numeric_claims(grounding_enforced):
    """A grounded view with zero numbers has nothing to verify → kept."""
    block = _make_block("AAPL")
    view = _grounded_view("The thesis is bullish on improving sentiment.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [view]
    assert dropped == []


def test_keeps_non_grounded_view_even_with_decimals(grounding_enforced):
    """A deterministic-analyst view (no grounding marker) is never verified.

    Its rationale decimals (e.g. rsi=0.75) are internal sub-scores, not sourced
    numeric claims — verifying them against a price block would wrongly drop a
    legitimate analyst.
    """
    block = _make_block("AAPL")
    view = _plain_view("[microstructure] rsi=+1@0.75, macd=+1@0.62")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [view], "non-grounded view must pass through untouched"
    assert dropped == []


def test_passthrough_when_no_block_present():
    """No GroundTruthBlock in ctx.extras → identity passthrough (default advisor path)."""
    view = _grounded_view("Target price is 999.99 — would fail if a block existed.")
    kept, dropped = enforce_grounding([view], _ctx(block=None))
    assert kept == [view], "with no block to verify against, nothing is dropped"
    assert dropped == []


def test_killswitch_off_is_passthrough(monkeypatch):
    """HERMES_QUANT_GROUNDING_ENFORCE=0 disables the seam (identity passthrough)."""
    monkeypatch.setenv("HERMES_QUANT_GROUNDING_ENFORCE", "0")
    block = _make_block("AAPL")
    view = _grounded_view("Target price is 999.99.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [view]
    assert dropped == []


def test_unset_env_is_passthrough(monkeypatch):
    """With the flag absent, enforcement is OFF for byte-identical rollout."""
    monkeypatch.delenv("HERMES_QUANT_GROUNDING_ENFORCE", raising=False)
    block = _make_block("AAPL")
    view = _grounded_view("Target price is 999.99.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [view]
    assert dropped == []


def test_env_one_enforces(monkeypatch):
    """HERMES_QUANT_GROUNDING_ENFORCE=1 keeps the fail-closed behavior available."""
    monkeypatch.setenv("HERMES_QUANT_GROUNDING_ENFORCE", "1")
    block = _make_block("AAPL")
    view = _grounded_view("Target price is 999.99.")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == []
    assert len(dropped) == 1


def test_grounding_error_view_is_still_verified(grounding_enforced):
    """A view whose grounding ERRORED still declares participation → verified.

    Fail-closed: a semantic view that wanted grounding but hit an error must not
    get a free pass to vote with ungrounded numbers.
    """
    block = _make_block("AAPL")
    view = _grounded_view("Target 999.99 anyway.", marker="error")
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == []
    assert len(dropped) == 1


def test_dropped_record_carries_audit_fields(grounding_enforced):
    """Dropped records preserve WHY (coverage + uncited claims) for the audit trail."""
    block = _make_block("AAPL")
    view = _grounded_view("Targets 999.99 and 1050.00, resistance 1100.00.")
    _, dropped = enforce_grounding([view], _ctx(block=block))
    rec = dropped[0]
    assert rec["analyst"] == "hermes_semantic"
    assert "citation_coverage" in rec
    assert isinstance(rec["uncited_claims"], list)
    assert rec["uncited_claims"], "fabricated numbers must be listed as uncited"
    assert rec["reason"]


def test_mixed_batch_drops_only_failing_grounded_view(grounding_enforced):
    """A mixed batch keeps the good views and drops only the failing grounded one."""
    block = _make_block("AAPL")
    good_grounded = _grounded_view(
        f"Close at {block.ohlcv_60d[-1].close:.4f} from ground truth."
    )
    bad_grounded = _grounded_view("Moonshot to 9999.00.", analyst="hermes_semantic_2")
    plain = _plain_view("[microstructure] vwap=+1@0.80")
    kept, dropped = enforce_grounding([good_grounded, bad_grounded, plain], _ctx(block=block))
    assert good_grounded in kept
    assert plain in kept
    assert bad_grounded not in kept
    assert {r["analyst"] for r in dropped} == {"hermes_semantic_2"}


def test_strict_threshold_drops_partial_citation(grounding_enforced):
    """Strict (default 1.0): one real + one fabricated number → still dropped."""
    block = _make_block("AAPL")
    real = block.ohlcv_60d[-1].close
    # one cited (real GT close) + one fabricated, separated so no proximity credit
    view = _grounded_view(
        f"Close {real:.4f} is solid. " + ("x" * 120) + " But target is 999.99."
    )
    kept, dropped = enforce_grounding([view], _ctx(block=block))
    assert kept == [], "strict threshold must drop a view with ANY uncited number"
    assert len(dropped) == 1
