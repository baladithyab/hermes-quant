"""W2 seam test: the llm_committee belief-digest injection (ADR-0081 §2 / plan §5b).

Two invariants:
  - DEFAULT-OFF byte-identical no-op: with HERMES_QUANT_WEEKLY_RETRO unset/0, the rendered
    PM prompt is byte-for-byte identical to today (the W1 baseline) — the flag-OFF path is
    a bit-for-bit no-op.
  - ON-state selective prepend: with the flag=1 and a belief in the store, the PM
    lessons_block is prepended with the role-selective digest above the raw lessons.

Uses the same deterministic fixtures as test_llm_committee_prompts.py so the prompt is
byte-stable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from hermes_quant.aggregators.llm_committee import _render_prompt
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

_ASOF = pd.Timestamp("2026-06-10", tz="UTC")


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


def _seed_belief(bpath: Path, asof: datetime) -> None:
    from hermes_quant.memory import weekly_retro

    rows = []
    res = asof - timedelta(days=2)
    tau = asof - timedelta(days=1)
    for i in range(weekly_retro.MIN_SUPPORT_N + 1):
        rows.append({
            "schema_version": 1, "reflection_id": f"ref_{i}", "decision_id": f"dec_{i}",
            "asof_resolution": res.isoformat(), "tau_observable": tau.isoformat(),
            "ticker": "AAPL", "raw_return": 0.04, "alpha_return": 0.04, "benchmark": "SPY",
            "holding_days": 5, "outcome_quality": 4,
            "reflection_text": "win", "lesson_category": "regime_shift_invalidation",
            "reflector_model": "stub-v0.1", "reflector_prompt_hash": "stub:x",
        })
    rpath = bpath.parent / "reflections.jsonl"
    with open(rpath, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    weekly_retro.run_weekly_retro(asof, reflections_path=rpath, beliefs_path=bpath,
                                  emit_promotion=False)


def test_flag_off_is_byte_identical_noop(monkeypatch, tmp_path) -> None:
    """Flag UNSET and flag=0 both render the EXACT same PM prompt (bit-for-bit no-op)."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "beliefs.jsonl"
    _seed_belief(bpath, datetime(2026, 6, 5, tzinfo=UTC))
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    # MEMORY_INJECT also off so we isolate the W2 flag's no-op property.
    monkeypatch.delenv("HERMES_QUANT_MEMORY_INJECT", raising=False)

    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)
    rendered_unset = _render_pm()

    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "0")
    rendered_zero = _render_pm()

    assert rendered_unset == rendered_zero, "flag=0 must be byte-identical to flag-unset"
    # Even with beliefs present, OFF must not surface the digest header.
    assert "Distilled beliefs (weekly retro)" not in rendered_unset


def test_flag_on_prepends_selective_digest(monkeypatch, tmp_path) -> None:
    """Flag=1 with a stored belief -> the PM lessons_block carries the digest header."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "beliefs.jsonl"
    _seed_belief(bpath, datetime(2026, 6, 5, tzinfo=UTC))
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    monkeypatch.delenv("HERMES_QUANT_MEMORY_INJECT", raising=False)
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "1")

    rendered = _render_pm()
    assert "Distilled beliefs (weekly retro)" in rendered
    assert "regime_shift_invalidation" in rendered


def test_flag_on_with_empty_store_is_noop(monkeypatch, tmp_path) -> None:
    """Flag=1 but NO beliefs -> empty digest -> the prompt matches the flag-off render."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "absent.jsonl"  # never created
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    monkeypatch.delenv("HERMES_QUANT_MEMORY_INJECT", raising=False)

    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)
    off = _render_pm()
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "1")
    on_empty = _render_pm()

    assert off == on_empty, "empty belief store under flag=1 must be a byte-identical no-op"
