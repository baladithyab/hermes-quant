"""B21 / G15 seam test: the llm_committee same-ticker-rich vs cross-ticker-lean
render split, wired into the PM prompt behind HERMES_QUANT_MEMORY_SPLIT=1.

Three invariants:
  - DEFAULT-OFF byte-identical no-op: with HERMES_QUANT_MEMORY_SPLIT unset/0 (and
    MEMORY_INJECT=1), the rendered PM prompt is byte-for-byte identical — the split
    flag's OFF path uses the original combined renderer (format_context_block).
  - ON-state asymmetric verbosity: with MEMORY_INJECT=1 and MEMORY_SPLIT=1, the
    same-ticker lesson survives RICH while the cross-ticker lesson is dropped to a
    LEAN one-liner (its lesson text does NOT appear, but its ticker/alpha do).
  - Oracle-Fallacy no-lookahead guard intact THROUGH the split renderer: a reflection
    whose tau_observable >= asof never leaks into the split-rendered block.

Reuses the deterministic fixtures from test_weekly_retro_injection.py so the prompt
is byte-stable, and monkeypatches retriever.REFLECTIONS_PATH onto a tmp JSONL so the
test never touches ~/.hermes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from hermes_quant.aggregators.llm_committee import _render_prompt
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

# asof is the decision timestamp; reflections must have tau_observable < this asof.
_ASOF = pd.Timestamp("2026-06-10", tz="UTC")
_ASOF_DT = datetime(2026, 6, 10, tzinfo=UTC)


def _fixed_ctx() -> MarketContext:
    ts = pd.date_range("2026-06-08", periods=3, freq="1d", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [1_000_000, 1_100_000, 900_000],
        }
    )
    return MarketContext(
        asset="AAPL", timeframe="1d", asset_class="equity", exchange=None,
        bars=bars, last_close=102.5, last_volume=900_000.0, asof=_ASOF,
    )


def _fixed_views() -> list[AnalystView]:
    return [
        AnalystView(analyst="classical_ta", direction=1, magnitude=0.012, confidence=0.7,
                    confidence_raw=0.7, horizon="1d", rationale="Bullish breakout"),
    ]


def _fixed_baseline() -> AggregatedSignal:
    return AggregatedSignal(
        asset="AAPL", timeframe="1d", asset_class="equity", asof=_ASOF, direction=1,
        magnitude=0.009, confidence=0.62, confidence_raw=0.62, horizon="1d",
        components=tuple(_fixed_views()), aggregator="bma",
    )


def _render_pm() -> str:
    sys_text, user_text = _render_prompt(
        role="portfolio_manager",
        market_context=_fixed_ctx(),
        analyst_views=_fixed_views(),
        baseline_signal=_fixed_baseline(),
        prior_turns=[],
    )
    return sys_text + "\n\n" + user_text


def _reflection_row(
    *,
    reflection_id: str,
    ticker: str,
    lesson: str,
    tau_observable: datetime,
    alpha_return: float = 0.04,
    rating: str = "Buy",
) -> dict:
    return {
        "schema_version": 1,
        "reflection_id": reflection_id,
        "decision_id": f"dec_{reflection_id}",
        "asof_resolution": (tau_observable - timedelta(hours=6)).isoformat(),
        "tau_observable": tau_observable.isoformat(),
        "ticker": ticker.upper(),
        "raw_return": 0.05,
        "alpha_return": alpha_return,
        "benchmark": "SPY",
        "holding_days": 5,
        "outcome_quality": 4,
        "reflection_text": lesson,
        "lesson_category": "thesis_correct",
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:x",
        "rating": rating,
    }


def _seed_reflections(rpath: Path, rows: list[dict]) -> None:
    with open(rpath, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def _point_retriever_at(monkeypatch, rpath: Path) -> None:
    """Redirect the retriever's default reflection/decision/sector paths to tmp.

    get_past_context() reads `reflections_path or REFLECTIONS_PATH` at call time, so
    patching the module attribute is sufficient. We also steer the decisions/sector
    defaults to non-existent tmp files so the test never reads ~/.hermes.
    """
    from hermes_quant.memory import decisions as _decisions
    from hermes_quant.memory import retriever as _retriever

    monkeypatch.setattr(_retriever, "REFLECTIONS_PATH", rpath)
    monkeypatch.setattr(_retriever, "SECTOR_CACHE_PATH", rpath.parent / "absent-sector.json")
    monkeypatch.setattr(_decisions, "DECISIONS_PATH", rpath.parent / "absent-decisions.jsonl")


# ---------------------------------------------------------------------------
# 1. DEFAULT-OFF byte-identical no-op
# ---------------------------------------------------------------------------


def test_split_flag_off_is_byte_identical_noop(monkeypatch, tmp_path) -> None:
    """With MEMORY_INJECT=1, MEMORY_SPLIT unset and =0 render the EXACT same PM prompt.

    Off-state for the split flag uses the original combined renderer, so the rendered
    PM prompt must be byte-for-byte identical to the pre-G15 baseline.
    """
    rpath = tmp_path / "reflections.jsonl"
    _seed_reflections(rpath, [
        _reflection_row(reflection_id="same", ticker="AAPL",
                        lesson="SAME_LESSON held thesis through earnings",
                        tau_observable=_ASOF_DT - timedelta(days=10)),
        _reflection_row(reflection_id="cross", ticker="MSFT",
                        lesson="CROSS_LESSON faded the gap up",
                        tau_observable=_ASOF_DT - timedelta(days=10), alpha_return=0.11),
    ])
    _point_retriever_at(monkeypatch, rpath)

    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "1")
    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)

    monkeypatch.delenv("HERMES_QUANT_MEMORY_SPLIT", raising=False)
    rendered_unset = _render_pm()

    monkeypatch.setenv("HERMES_QUANT_MEMORY_SPLIT", "0")
    rendered_zero = _render_pm()

    assert rendered_unset == rendered_zero, "MEMORY_SPLIT=0 must be byte-identical to unset"
    # The combined (OFF) renderer surfaces the cross-ticker lesson verbatim.
    assert "CROSS_LESSON" in rendered_unset


# ---------------------------------------------------------------------------
# 2. ON-state asymmetric verbosity (the split actually splits)
# ---------------------------------------------------------------------------


def test_split_flag_on_keeps_same_ticker_rich_cross_lean(monkeypatch, tmp_path) -> None:
    """MEMORY_SPLIT=1: same-ticker lesson stays RICH; cross-ticker lesson goes LEAN.

    Proves the split is wired end-to-end through get_past_context + the split renderer.
    """
    rpath = tmp_path / "reflections.jsonl"
    _seed_reflections(rpath, [
        _reflection_row(reflection_id="same", ticker="AAPL",
                        lesson="SAME_LESSON held thesis through earnings",
                        tau_observable=_ASOF_DT - timedelta(days=10)),
        _reflection_row(reflection_id="cross", ticker="MSFT",
                        lesson="CROSS_LESSON faded the gap up",
                        tau_observable=_ASOF_DT - timedelta(days=10), alpha_return=0.11),
    ])
    _point_retriever_at(monkeypatch, rpath)

    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "1")
    monkeypatch.setenv("HERMES_QUANT_MEMORY_SPLIT", "1")
    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)

    rendered = _render_pm()

    # Same-ticker (AAPL) keeps the full lesson + category (RICH).
    assert "SAME_LESSON" in rendered
    assert "thesis_correct" in rendered
    # Cross-ticker (MSFT) drops the lesson text (LEAN) but keeps ticker + alpha.
    assert "CROSS_LESSON" not in rendered
    assert "MSFT" in rendered
    assert "+11.0%" in rendered


# ---------------------------------------------------------------------------
# 3. Oracle-Fallacy no-lookahead guard intact THROUGH the split renderer
# ---------------------------------------------------------------------------


def test_split_flag_on_does_not_leak_future_asof_reflection(monkeypatch, tmp_path) -> None:
    """No-lookahead: a reflection with tau_observable >= asof must NOT appear in the
    split-rendered PM block (the Oracle-Fallacy guard is upstream and survives the
    render-asymmetry change).
    """
    rpath = tmp_path / "reflections.jsonl"
    _seed_reflections(rpath, [
        # PAST same-ticker reflection (tau < asof) — MUST appear.
        _reflection_row(reflection_id="past", ticker="AAPL",
                        lesson="PAST_KNOWABLE_LESSON",
                        tau_observable=_ASOF_DT - timedelta(days=1)),
        # FUTURE same-ticker reflection (tau > asof) — MUST be excluded (lookahead).
        _reflection_row(reflection_id="future", ticker="AAPL",
                        lesson="FUTURE_LEAK_LESSON",
                        tau_observable=_ASOF_DT + timedelta(days=1)),
    ])
    _point_retriever_at(monkeypatch, rpath)

    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "1")
    monkeypatch.setenv("HERMES_QUANT_MEMORY_SPLIT", "1")
    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)

    rendered = _render_pm()

    assert "PAST_KNOWABLE_LESSON" in rendered, "a knowable (tau < asof) lesson must surface"
    assert "FUTURE_LEAK_LESSON" not in rendered, (
        "Oracle-Fallacy guard breach: a reflection with tau_observable >= asof leaked "
        "into the split-rendered PM block."
    )
