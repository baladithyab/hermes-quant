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
    """Off-switch: WEEKLY_RETRO=0 is a bit-for-bit no-op even with a belief present.

    The default was promoted to ON (FLAGS.md Tier A), so the inert path is requested
    explicitly with =0. The =0 render with a belief in the store must be byte-identical
    to the render with the store ABSENT (the digest is fully suppressed either way).
    MEMORY_INJECT is pinned =0 so we isolate the W2 flag's no-op property deterministically
    (no read of the real ~/.hermes reflections store).
    """
    from hermes_quant.memory import weekly_retro

    # MEMORY_INJECT pinned off so the lessons base is constant + machine-independent.
    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "0")
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "0")

    bpath = tmp_path / "beliefs.jsonl"
    _seed_belief(bpath, datetime(2026, 6, 5, tzinfo=UTC))
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    rendered_with_belief = _render_pm()

    # Same flags, but the belief store is absent: the off-switch makes these identical.
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", tmp_path / "absent.jsonl")
    rendered_no_belief = _render_pm()

    assert rendered_with_belief == rendered_no_belief, (
        "WEEKLY_RETRO=0 must be byte-identical whether or not a belief is present"
    )
    # Even with beliefs present, OFF must not surface the digest header.
    assert "Distilled beliefs (weekly retro)" not in rendered_with_belief


def test_flag_on_prepends_selective_digest(monkeypatch, tmp_path) -> None:
    """Flag=1 with a stored belief -> the PM lessons_block carries the digest header."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "beliefs.jsonl"
    _seed_belief(bpath, datetime(2026, 6, 5, tzinfo=UTC))
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    # Pin MEMORY_INJECT=0 (its default is now ON) so the per-trade lessons base is
    # constant and we never read the real ~/.hermes reflections store.
    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "0")
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "1")

    rendered = _render_pm()
    assert "Distilled beliefs (weekly retro)" in rendered
    assert "regime_shift_invalidation" in rendered


def test_flag_on_with_empty_store_is_noop(monkeypatch, tmp_path) -> None:
    """NO-DATA SAFETY: flag ON-by-default but NO beliefs -> empty digest -> the prompt
    matches the explicitly-OFF render. ON-by-default with an empty store never raises
    and surfaces nothing (silence-by-default; FLAGS.md Tier A no-data safety check)."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "absent.jsonl"  # never created
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    # Pin MEMORY_INJECT=0 (default now ON) so the lessons base is deterministic.
    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "0")

    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "0")
    off = _render_pm()
    # Default ON (no env var) with an empty store must equal the explicit-off render.
    monkeypatch.delenv("HERMES_QUANT_WEEKLY_RETRO", raising=False)
    on_default_empty = _render_pm()

    assert off == on_default_empty, (
        "empty belief store under ON-by-default must be a byte-identical no-op"
    )
    assert "Distilled beliefs (weekly retro)" not in on_default_empty


def test_stale_belief_is_dropped_at_read_site(monkeypatch, tmp_path) -> None:
    """STALENESS GUARD: a belief older than STALE_BELIEF_HALF_LIVES x its tier
    half-life (weekly => 2x14 = 28d) is NOT injected, even with WEEKLY_RETRO ON.

    This is the silence-by-default rail for a paused/dead weekly-retro PRODUCER
    cron: if the producer stops refreshing/expiring beliefs, the consumer refuses
    to leak stale beliefs into a live capital decision. Seed a belief distilled
    40 days before the consumer's asof (_ASOF=2026-06-10 => distilled 2026-05-01,
    age 40d > 28d) and confirm the digest is suppressed. Contrast with
    test_flag_on_prepends_selective_digest (5d old => fresh => injected).
    """
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "beliefs.jsonl"
    # Distill the belief 40 days before the read asof => stale past 2x weekly HL.
    _seed_belief(bpath, datetime(2026, 5, 1, tzinfo=UTC))  # 40d before _ASOF=2026-06-10
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "0")
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "1")

    rendered = _render_pm()
    assert "Distilled beliefs (weekly retro)" not in rendered, (
        "a belief older than 2x its tier half-life must be dropped at the read "
        "site (stale-producer safety), not injected into the PM prompt"
    )


def test_fresh_belief_still_injected_after_guard(monkeypatch, tmp_path) -> None:
    """Guard is age-gated, not a blanket suppressor: a belief well within the
    freshness horizon (5d << 28d) still injects under ON. Locks the guard's
    boundary so it can't regress into dropping fresh beliefs too."""
    from hermes_quant.memory import weekly_retro

    bpath = tmp_path / "beliefs.jsonl"
    _seed_belief(bpath, datetime(2026, 6, 5, tzinfo=UTC))  # 5d before _ASOF -> fresh
    monkeypatch.setattr(weekly_retro, "BELIEFS_PATH", bpath)
    monkeypatch.setenv("HERMES_QUANT_MEMORY_INJECT", "0")
    monkeypatch.setenv("HERMES_QUANT_WEEKLY_RETRO", "1")

    rendered = _render_pm()
    assert "Distilled beliefs (weekly retro)" in rendered, (
        "a fresh belief (5d, well under the 28d weekly horizon) must still inject"
    )
