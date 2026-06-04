"""tests/integration/test_advisor_grounding_seam.py — seed 24ba wire-up.

Proves that advisor.recommend() actually INVOKES the grounding-enforcement seam
between the analyst loop and the aggregator:

  * a grounded analyst emitting an ungrounded numeric claim is DROPPED from the
    vote — its contribution does not reach the aggregated signal;
  * a fully-grounded analyst is kept and aggregates exactly as if no verifier ran
    (byte-identical aggregated_signal);
  * with no ground_truth_block injected (today's default path), the result is
    byte-identical to a run with the kill-switch off.

Deterministic: a stub provider returns a fixed DataFrame, analysts are injected,
and asof is pinned, so no network / wall-clock enters the decision math.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.advisor import recommend
from hermes_quant.grounding.data_grounding import Bar, build_ground_truth_block
from hermes_quant.protocol import AnalystView, MarketContext


# --- Fixtures ----------------------------------------------------------------


def _daily_df(n: int = 120, start: str = "2026-01-02") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    rng = np.random.default_rng(seed=11)
    close = 170.0 + rng.normal(0, 0.5, size=n).cumsum()
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 5_000_000.0),
        }
    )


class _StubProvider:
    name = "stub"
    asset_classes = ["equity", "etf"]
    timeframes = ["1d"]

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def fetch_bars(self, asset, timeframe, start, end, *, as_of=None, **_: Any):
        df = self._df.copy()
        if as_of is not None:
            cutoff = as_of.tz_convert("UTC").tz_localize(None) if as_of.tzinfo else as_of
            df = df[df["timestamp"] <= cutoff].reset_index(drop=True)
        return df


class _GroundedAnalyst:
    """Emits a grounded view (declares grounding) with a configurable rationale."""

    def __init__(self, name: str, rationale: str, direction: int = 1, conf: float = 0.7):
        self.name = name
        self._rationale = rationale
        self._direction = direction
        self._conf = conf

    def analyze(self, ctx: MarketContext) -> AnalystView:
        return AnalystView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.02,
            confidence=self._conf,
            confidence_raw=self._conf,
            horizon="1d",
            rationale=self._rationale,
            metadata={"with_grounding": True, "ground_truth_symbol": "AAPL"},
        )


class _PlainAnalyst:
    """A deterministic analyst that never opts into grounding."""

    def __init__(self, name: str, direction: int = 1, conf: float = 0.7):
        self.name = name
        self._direction = direction
        self._conf = conf

    def analyze(self, ctx: MarketContext) -> AnalystView:
        return AnalystView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.02,
            confidence=self._conf,
            confidence_raw=self._conf,
            horizon="1d",
            rationale="[plain] rsi=+1@0.75",
            metadata={"sub_signals": []},
        )


def _block():
    from datetime import date, timedelta

    bars = []
    d = date(2026, 5, 1)
    for i in range(10):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = 170.00 + i * 0.50
        bars.append(Bar(date_str=d.isoformat(), open=close - 0.25, high=close + 0.5,
                        low=close - 0.5, close=close, volume=5_000_000))
        d += timedelta(days=1)
    return build_ground_truth_block("AAPL", "2026-05-27", ohlcv_bars=bars)


_ASOF = "2026-06-01"


# --- Tests -------------------------------------------------------------------


def test_ungrounded_grounded_view_dropped_from_vote():
    """A grounded analyst with a fabricated number does not reach the aggregate."""
    provider = _StubProvider(_daily_df())
    bad = _GroundedAnalyst("hermes_semantic", "Moonshot target 9999.00 imminent.", direction=1)
    plain = _PlainAnalyst("classical_ta", direction=-1)
    out = recommend(
        "AAPL", asset_class="equity", timeframe="1d", as_of=_ASOF,
        provider=provider, analysts=[bad, plain],
        market_extras={"ground_truth_block": _block()},
        include_lessons=False,
    )
    voting = {v["analyst"] for v in out["analyst_views"]}
    # The fabricated grounded analyst's view must have been verified out before
    # aggregation (it does not appear among the aggregator's components).
    agg = out["aggregated_signal"]
    assert agg is not None
    assert agg["n_components"] == 1, "only the plain analyst should vote"
    # Audit trail surfaces the drop.
    assert any("grounding" in c.lower() for c in out["caveats"]), out["caveats"]


def test_audit_annotation_matches_the_actually_dropped_view():
    """Annotation must mark the DROPPED view, not a same-named kept view.

    Code review (Issue 1): when two views share an analyst name and only the
    LATER one is dropped, name-based annotation falsely stamped the first
    (kept) view as grounding_dropped and left the dropped one unannotated.
    Identity-based matching must annotate exactly the dropped entry.
    """
    block = _block()
    real_close = block.ohlcv_60d[-1].close
    good = _GroundedAnalyst("hermes_semantic", f"Close confirmed at {real_close:.4f}.", direction=1)
    bad = _GroundedAnalyst("hermes_semantic", "Target 9999.00 imminent.", direction=1)
    provider = _StubProvider(_daily_df())
    out = recommend(
        "AAPL", asset_class="equity", timeframe="1d", as_of=_ASOF,
        provider=provider, analysts=[good, bad],
        market_extras={"ground_truth_block": block},
        include_lessons=False,
    )
    avs = out["analyst_views"]
    assert len(avs) == 2, "both views are recorded in the audit trail"
    # entry[0] is the cited view (kept, voted) — must NOT be marked dropped.
    assert not avs[0].get("grounding_dropped"), (
        "the KEPT (cited) view must not be falsely marked grounding_dropped"
    )
    # entry[1] is the fabricated view (dropped) — must be marked dropped.
    assert avs[1].get("grounding_dropped") is True, (
        "the actually-dropped view must be annotated grounding_dropped"
    )
    assert avs[1].get("grounding_uncited_claims"), "dropped entry carries its uncited claims"
    # And the surviving vote is the cited one.
    assert out["aggregated_signal"]["n_components"] == 1


def test_fully_grounded_view_byte_identical_to_no_verifier():
    """A grounded view whose numbers all trace to GT aggregates identically.

    Compare the aggregated_signal of an ENFORCE-ON run against a KILL-SWITCH-OFF
    run with the same fully-grounded inputs: they must be byte-identical (the
    verifier accepts the view, so nothing changes).
    """
    block = _block()
    real_close = block.ohlcv_60d[-1].close
    rationale = f"Close confirmed at {real_close:.4f} on ground truth; bullish."

    def _run(enforce: str):
        import os
        prev = os.environ.get("HERMES_QUANT_GROUNDING_ENFORCE")
        os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = enforce
        try:
            provider = _StubProvider(_daily_df())
            good = _GroundedAnalyst("hermes_semantic", rationale)
            plain = _PlainAnalyst("classical_ta")
            return recommend(
                "AAPL", asset_class="equity", timeframe="1d", as_of=_ASOF,
                provider=provider, analysts=[good, plain],
                market_extras={"ground_truth_block": block},
                include_lessons=False,
            )
        finally:
            if prev is None:
                os.environ.pop("HERMES_QUANT_GROUNDING_ENFORCE", None)
            else:
                os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = prev

    on = _run("1")
    off = _run("0")
    assert on["aggregated_signal"] == off["aggregated_signal"], (
        "fully-grounded inputs must aggregate byte-identically with the seam on/off"
    )
    # Both analysts vote in both runs.
    assert on["aggregated_signal"]["n_components"] == 2


def test_multi_horizon_drops_ungrounded_view():
    """recommend_multi_horizon must ALSO enforce grounding (second entry point).

    Adversarial review (A-sneak-ungrounded): recommend_multi_horizon returned
    views straight to the caller's aggregator without the Step-5.5 seam, so a
    grounded view with a fabricated number bypassed enforcement entirely.
    """
    from hermes_quant.advisor import recommend_multi_horizon

    provider = _StubProvider(_daily_df())
    bad = _GroundedAnalyst("hermes_semantic", "Moonshot target 9999.00 imminent.")
    plain = _PlainAnalyst("classical_ta")
    views = recommend_multi_horizon(
        "AAPL", horizons=("1d",), asset_class="equity", as_of=_ASOF,
        provider=provider, analysts=[bad, plain],
        market_extras={"ground_truth_block": _block()},
    )
    analysts_voting = {v.analyst for v in views}
    assert "hermes_semantic" not in analysts_voting, (
        "ungrounded grounded view must be dropped from the multi-horizon fan-out"
    )
    assert "classical_ta" in analysts_voting, "non-grounded analyst must survive"


def test_multi_horizon_grounded_view_kept_byte_identical():
    """A fully-grounded multi-horizon view is kept identically with the seam on/off."""
    from hermes_quant.advisor import recommend_multi_horizon
    import os

    block = _block()
    rationale = f"Close confirmed at {block.ohlcv_60d[-1].close:.4f} on ground truth."

    def _run(enforce: str):
        prev = os.environ.get("HERMES_QUANT_GROUNDING_ENFORCE")
        os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = enforce
        try:
            provider = _StubProvider(_daily_df())
            good = _GroundedAnalyst("hermes_semantic", rationale)
            plain = _PlainAnalyst("classical_ta")
            return recommend_multi_horizon(
                "AAPL", horizons=("1d",), asset_class="equity", as_of=_ASOF,
                provider=provider, analysts=[good, plain],
                market_extras={"ground_truth_block": block},
            )
        finally:
            if prev is None:
                os.environ.pop("HERMES_QUANT_GROUNDING_ENFORCE", None)
            else:
                os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = prev

    on = {v.analyst for v in _run("1")}
    off = {v.analyst for v in _run("0")}
    assert on == off == {"hermes_semantic", "classical_ta"}, (
        "fully-grounded inputs must survive identically with the seam on/off"
    )


def test_no_block_path_unaffected_by_seam():
    """With no ground_truth_block (default advisor path), seam on==off byte-identical."""
    def _run(enforce: str):
        import os
        prev = os.environ.get("HERMES_QUANT_GROUNDING_ENFORCE")
        os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = enforce
        try:
            provider = _StubProvider(_daily_df())
            # Even a "grounded"-marked analyst with a fabricated number: with no
            # block to verify against, nothing is dropped.
            bad = _GroundedAnalyst("hermes_semantic", "Target 9999.00.")
            plain = _PlainAnalyst("classical_ta")
            return recommend(
                "AAPL", asset_class="equity", timeframe="1d", as_of=_ASOF,
                provider=provider, analysts=[bad, plain],
                include_lessons=False,
            )
        finally:
            if prev is None:
                os.environ.pop("HERMES_QUANT_GROUNDING_ENFORCE", None)
            else:
                os.environ["HERMES_QUANT_GROUNDING_ENFORCE"] = prev

    on = _run("1")
    off = _run("0")
    assert on["aggregated_signal"] == off["aggregated_signal"]
    assert on["aggregated_signal"]["n_components"] == 2, "no block → no drop"
