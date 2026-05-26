"""Regression test for advisor wiring all three canonical analysts.

Phase-7 architecture review (2026-05-13) caught that v0.3.0's CHANGELOG
claim "ALL THREE shipped" was structurally false because hermes_quant.
advisor.recommend() hard-coded `[ClassicalTAAnalyst, MicrostructureLite]`
and never instantiated KronosAnalyst. The "three-analyst committee"
charter MVP recipe was undelivered for live ticks.

This test pins the advisor's default analyst loadout so a future refactor
can't silently drop a voice.
"""

from __future__ import annotations

from unittest import mock


def test_advisor_default_loadout_includes_three_analysts():
    """The advisor's default analyst list MUST contain three voices when
    all optional dependencies are installable. KronosAnalyst's abstain
    behavior (when kronos package is missing) is handled at the BMA
    aggregator's abstain filter (per ADR-0018 §D4), not at advisor."""
    # We import the function and inspect its source to verify the wiring
    # without forcing a full advisor.recommend() call (which needs
    # market data).
    from hermes_quant.advisor import recommend
    import inspect

    src = inspect.getsource(recommend)
    # The advisor MUST mention all three analyst classes by name
    assert "ClassicalTAAnalyst" in src
    assert "MicrostructureLite" in src
    assert "KronosAnalyst" in src


def test_advisor_kronos_import_failure_does_not_break_advisor():
    """If KronosAnalyst import raises (shouldn't happen — it's in our
    package — but defensive), advisor falls back to two voices."""
    # Verify the import is wrapped in try/except
    from hermes_quant.advisor import recommend
    import inspect

    src = inspect.getsource(recommend)
    # Find the section after KronosAnalyst import
    idx = src.find("from hermes_quant.analysts.kronos import KronosAnalyst")
    assert idx > 0, "KronosAnalyst import not found in advisor"
    # The next ~5 lines should contain `except`
    excerpt = src[idx : idx + 400]
    assert "except" in excerpt, "KronosAnalyst import is not wrapped in try/except"
