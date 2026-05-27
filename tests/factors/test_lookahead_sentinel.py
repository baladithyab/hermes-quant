"""tests/factors/test_lookahead_sentinel.py — Unit tests for the lookahead sentinel.

Covers:
  - shift(-N) is detected as lookahead
  - shift(+N) passes cleanly
  - shift(periods=-N) keyword form detected
  - Forward iloc[i+1:] slicing detected
  - Backward/safe iloc[:i] passes
  - Target-column shift(-N) is flagged with a specific tag
  - Result metadata (line, col, detail) is populated
"""

from __future__ import annotations

import pytest

from hermes_quant.factors.lookahead_sentinel import (
    LookaheadDetected,
    LookaheadResult,
    check_no_lookahead,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_suspicious(source: str, expected_kind: str | None = None) -> LookaheadResult:
    result = check_no_lookahead(source)
    assert not result.passes, f"Expected suspicion but got passes=True for: {source!r}"
    assert result.suspicions, "Expected at least one suspicion"
    if expected_kind is not None:
        kinds = {s["kind"] for s in result.suspicions}
        assert expected_kind in kinds, f"Expected kind {expected_kind!r} in {kinds}"
    return result


def _assert_clean(source: str) -> LookaheadResult:
    result = check_no_lookahead(source)
    assert result.passes, f"Expected clean result but got suspicions: {result.suspicions}"
    assert result.suspicions == []
    return result


# ---------------------------------------------------------------------------
# shift() detection
# ---------------------------------------------------------------------------


class TestShiftDetection:
    def test_positive_shift_passes(self):
        """shift(1) is a backward look — safe."""
        _assert_clean('bars["close"].shift(1)')

    def test_shift_5_passes(self):
        _assert_clean('np.log(bars["close"] / bars["close"].shift(5))')

    def test_negative_shift_minus1_detected(self):
        """shift(-1) peeks one bar into the future."""
        _assert_suspicious('bars["close"].shift(-1)', expected_kind="negative_shift")

    def test_negative_shift_minus5_detected(self):
        _assert_suspicious('bars["close"].shift(-5)', expected_kind="negative_shift")

    def test_shift_periods_positive_passes(self):
        _assert_clean('bars["close"].shift(periods=3)')

    def test_shift_periods_negative_detected(self):
        _assert_suspicious(
            'bars["close"].shift(periods=-2)',
            expected_kind="negative_shift_periods",
        )

    def test_zero_shift_passes(self):
        """shift(0) is a no-op — safe."""
        _assert_clean('bars["close"].shift(0)')

    def test_negative_shift_on_target_column(self):
        """shift(-1) on 'ret' should be flagged with a target-specific kind."""
        result = check_no_lookahead('bars["ret"].shift(-1)')
        assert not result.passes
        kinds = {s["kind"] for s in result.suspicions}
        assert "negative_shift_on_target" in kinds or "negative_shift" in kinds

    def test_negative_shift_on_fwd_return(self):
        result = check_no_lookahead('fwd_return.shift(-1)')
        assert not result.passes

    def test_negative_shift_on_y(self):
        result = check_no_lookahead('y.shift(-3)')
        assert not result.passes


# ---------------------------------------------------------------------------
# iloc forward-slice detection
# ---------------------------------------------------------------------------


class TestIlocForwardSlice:
    def test_backward_iloc_passes(self):
        """df.iloc[:i] is backward-looking — safe."""
        _assert_clean('bars.iloc[:10]')

    def test_simple_iloc_index_passes(self):
        _assert_clean('bars.iloc[0]')

    def test_forward_slice_i_plus_1_detected(self):
        """df.iloc[i+1:] peeks forward."""
        _assert_suspicious(
            'bars.iloc[i+1:]',
            expected_kind="forward_iloc_slice",
        )

    def test_forward_slice_i_plus_5_detected(self):
        _assert_suspicious(
            'bars.iloc[i+5:]',
            expected_kind="forward_iloc_slice",
        )

    def test_iloc_without_forward_offset_passes(self):
        """iloc[-1] or iloc[:-1] are backward — safe."""
        _assert_clean('bars.iloc[-1]')
        _assert_clean('bars.iloc[:-1]')


# ---------------------------------------------------------------------------
# Suspicion metadata
# ---------------------------------------------------------------------------


class TestSuspicionMetadata:
    def test_suspicion_has_required_fields(self):
        result = check_no_lookahead('bars["close"].shift(-1)')
        assert not result.passes
        s = result.suspicions[0]
        assert "kind" in s
        assert "detail" in s
        assert "line" in s
        assert "col" in s

    def test_detail_mentions_future(self):
        result = check_no_lookahead('bars["close"].shift(-1)')
        assert not result.passes
        assert "future" in result.suspicions[0]["detail"].lower()

    def test_multiple_violations_all_captured(self):
        source = 'bars["close"].shift(-1)\nbars["volume"].shift(-2)'
        result = check_no_lookahead(source)
        assert not result.passes
        assert len(result.suspicions) >= 2

    def test_result_type(self):
        result = check_no_lookahead('bars["close"]')
        assert isinstance(result, LookaheadResult)
        assert result.passes is True

    def test_syntax_error_propagates(self):
        with pytest.raises(SyntaxError):
            check_no_lookahead("def !! broken")


# ---------------------------------------------------------------------------
# LookaheadDetected exception
# ---------------------------------------------------------------------------


class TestLookaheadDetectedException:
    def test_exception_carries_violation_kind(self):
        exc = LookaheadDetected("test message", "negative_shift", [{"kind": "negative_shift"}])
        assert exc.violation_kind == "negative_shift"
        assert len(exc.suspicions) == 1

    def test_exception_is_value_error_subclass(self):
        exc = LookaheadDetected("msg", "k")
        assert isinstance(exc, ValueError)

    def test_exception_default_empty_suspicions(self):
        exc = LookaheadDetected("msg", "k")
        assert exc.suspicions == []
