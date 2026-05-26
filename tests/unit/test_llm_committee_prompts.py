"""Golden-file tests for rendered LLM committee prompts.

Per ADR-0037 §"Implementation notes": these tests catch prompt drift in
PR review by storing the rendered prompt to a fixture file on first run
and asserting byte-equal on subsequent runs.

Update procedure: when an intentional prompt change is made, delete the
corresponding ``tests/unit/fixtures/llm_committee_prompts/<role>.txt``
file and re-run the tests once. The new rendered prompt is captured.
The PR diff then shows the prompt change for human review.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.aggregators.llm_committee import _render_prompt
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "llm_committee_prompts"


def _fixed_ctx() -> MarketContext:
    """Build a deterministic context. All timestamps and floats are fixed
    so the rendered prompt is byte-stable across runs."""
    ts = pd.date_range("2026-01-15", periods=3, freq="1d", tz="UTC")
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
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=102.5,
        last_volume=900_000.0,
        asof=ts[-1],
    )


def _fixed_views() -> list[AnalystView]:
    return [
        AnalystView(
            analyst="classical_ta",
            direction=1,
            magnitude=0.012,
            confidence=0.7,
            confidence_raw=0.7,
            horizon="1d",
            rationale="Bullish breakout above 50d MA with rising volume",
        ),
        AnalystView(
            analyst="microstructure_lite",
            direction=1,
            magnitude=0.008,
            confidence=0.6,
            confidence_raw=0.6,
            horizon="1d",
            rationale="Order book imbalance favors bid",
        ),
        AnalystView(
            analyst="kronos_forecast",
            direction=-1,
            magnitude=0.005,
            confidence=0.55,
            confidence_raw=0.55,
            horizon="1d",
            rationale="Mean-reversion signal",
        ),
    ]


def _fixed_baseline() -> AggregatedSignal:
    return AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2026-01-17", tz="UTC"),
        direction=1,
        magnitude=0.009,
        confidence=0.62,
        confidence_raw=0.62,
        horizon="1d",
        components=tuple(_fixed_views()),
        aggregator="bma",
    )


def _golden_compare(fixture_name: str, rendered: str) -> None:
    """Compare a rendered string against a golden fixture.

    On first run (fixture missing): write the rendered string and pass.
    On subsequent runs: assert byte-equal against the stored fixture.
    Setting the env var ``HERMES_QUANT_UPDATE_GOLDENS=1`` overwrites
    fixtures unconditionally (use only for intentional prompt updates).
    """
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path = _FIXTURE_DIR / fixture_name
    if not fixture_path.exists() or os.environ.get("HERMES_QUANT_UPDATE_GOLDENS") == "1":
        fixture_path.write_text(rendered, encoding="utf-8")
        # On capture, still pass — the next run will assert equality.
        return
    expected = fixture_path.read_text(encoding="utf-8")
    if expected != rendered:
        # Provide a useful diff in the failure message.
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                rendered.splitlines(),
                fromfile=str(fixture_path),
                tofile="rendered",
                lineterm="",
            )
        )
        pytest.fail(
            f"Prompt drift detected vs golden fixture {fixture_path.name}.\n"
            f"To intentionally update: delete the fixture file and re-run, "
            f"or set HERMES_QUANT_UPDATE_GOLDENS=1.\n\n{diff}"
        )


@pytest.mark.parametrize(
    "role,fixture",
    [
        ("bull_researcher", "bull_researcher.txt"),
        ("bear_researcher", "bear_researcher.txt"),
        ("research_manager", "research_manager.txt"),
        ("risk_aggressive", "risk_aggressive.txt"),
        ("risk_conservative", "risk_conservative.txt"),
        ("risk_neutral", "risk_neutral.txt"),
        ("portfolio_manager", "portfolio_manager.txt"),
    ],
)
def test_rendered_prompt_matches_golden(role: str, fixture: str) -> None:
    sys_text, user_text = _render_prompt(
        role=role,
        market_context=_fixed_ctx(),
        analyst_views=_fixed_views(),
        baseline_signal=_fixed_baseline(),
        prior_turns=[],  # empty for the bull/bear first turn; OK for all roles
    )
    rendered = "=== SYSTEM ===\n" + sys_text + "\n\n=== USER ===\n" + user_text + "\n"
    _golden_compare(fixture, rendered)


def test_prompt_hash_is_deterministic_and_changes_on_input_change() -> None:
    """The prompt_hash audit trail in CommitteeTurn.metadata must be
    stable for identical inputs and shift on any input change."""
    from hermes_quant.aggregators.llm_committee import _prompt_hash, _render_prompt

    sys1, user1 = _render_prompt(
        role="bull_researcher",
        market_context=_fixed_ctx(),
        analyst_views=_fixed_views(),
        baseline_signal=_fixed_baseline(),
        prior_turns=[],
    )
    sys2, user2 = _render_prompt(
        role="bull_researcher",
        market_context=_fixed_ctx(),
        analyst_views=_fixed_views(),
        baseline_signal=_fixed_baseline(),
        prior_turns=[],
    )
    h1 = _prompt_hash(sys1, user1)
    h2 = _prompt_hash(sys2, user2)
    assert h1 == h2
    assert len(h1) == 64

    # Mutate one analyst view -> hash must change.
    mutated_views = list(_fixed_views())
    mutated_views[0] = AnalystView(
        analyst="classical_ta",
        direction=1,
        magnitude=0.999,  # changed
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="changed",
    )
    sys3, user3 = _render_prompt(
        role="bull_researcher",
        market_context=_fixed_ctx(),
        analyst_views=mutated_views,
        baseline_signal=_fixed_baseline(),
        prior_turns=[],
    )
    assert _prompt_hash(sys3, user3) != h1


def test_silence_by_default_instruction_present_in_all_role_prompts() -> None:
    """Every prompt must carry the ADR-0037 silence-by-default instruction.

    This is a safety property: the prompt structure must explicitly tell
    the LLM that calibrated abstention is preferred over manufactured
    confidence. If the instruction is removed, this test fails.
    """
    for role in (
        "bull_researcher",
        "bear_researcher",
        "research_manager",
        "risk_aggressive",
        "risk_conservative",
        "risk_neutral",
        "portfolio_manager",
    ):
        sys_text, user_text = _render_prompt(
            role=role,
            market_context=_fixed_ctx(),
            analyst_views=_fixed_views(),
            baseline_signal=_fixed_baseline(),
            prior_turns=[],
        )
        full = sys_text + user_text
        assert "Silence-by-default" in full or "silence-by-default" in full, (
            f"Role {role!r}: missing silence-by-default instruction"
        )
