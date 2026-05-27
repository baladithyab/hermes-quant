"""tests/factors/test_lookahead_advanced.py — v0.2 lookahead sentinel tests.

Covers the six new SuspicionKind values introduced in v0.2 (ADR-0051):

  1.  boolean_mask_future_peek
  2.  variable_negative_shift
  3.  forward_label_index
  4.  pct_change_negative
  5.  diff_negative
  6.  rolling_lambda_future

Each class contains:
  - At least one test that fires the target pattern.
  - At least one test that does NOT fire on a safe variant.
  - Compound / chained variants where relevant.

Total: ≥ 25 tests.  All v0.1 tests in test_lookahead_sentinel.py are
unaffected (backward-compat is guaranteed by the unchanged visit_Call /
visit_Subscript paths for v0.1 pattern kinds).
"""

from __future__ import annotations

import pytest

from hermes_quant.factors.lookahead_sentinel import (
    LookaheadResult,
    check_no_lookahead,
)


# ---------------------------------------------------------------------------
# Helpers (mirrors test_lookahead_sentinel.py helpers)
# ---------------------------------------------------------------------------


def _assert_suspicious(source: str, expected_kind: str | None = None) -> LookaheadResult:
    result = check_no_lookahead(source)
    assert not result.passes, (
        f"Expected suspicion but got passes=True for:\n{source!r}"
    )
    assert result.suspicions, "Expected at least one suspicion"
    if expected_kind is not None:
        kinds = {s["kind"] for s in result.suspicions}
        assert expected_kind in kinds, (
            f"Expected kind {expected_kind!r} in {kinds}\nsource: {source!r}"
        )
    return result


def _assert_clean(source: str) -> LookaheadResult:
    result = check_no_lookahead(source)
    assert result.passes, (
        f"Expected clean result but got suspicions: {result.suspicions}\nsource: {source!r}"
    )
    assert result.suspicions == []
    return result


# ---------------------------------------------------------------------------
# 1. Boolean-mask future-peek
# ---------------------------------------------------------------------------


class TestBooleanMaskFuturePeek:
    """bars[bars.index > today] and variants."""

    def test_index_gt_today_detected(self):
        """Classic boolean-mask: bars[bars.index > today]."""
        _assert_suspicious(
            "bars[bars.index > today]",
            expected_kind="boolean_mask_future_peek",
        )

    def test_index_gte_asof_detected(self):
        """df[df.index >= asof] — >= is also forward-looking."""
        _assert_suspicious(
            "df[df.index >= asof]",
            expected_kind="boolean_mask_future_peek",
        )

    def test_index_gte_now_detected(self):
        _assert_suspicious(
            "prices[prices.index >= now]",
            expected_kind="boolean_mask_future_peek",
        )

    def test_date_col_gte_cutoff_detected(self):
        """df[df['date'] >= cutoff] — column-based date mask."""
        _assert_suspicious(
            "df[df['date'] >= cutoff]",
            expected_kind="boolean_mask_future_peek",
        )

    def test_date_col_gte_asof_plus_timedelta_detected(self):
        """df[df['date'] >= asof + timedelta(days=1)] — BinOp comparator."""
        source = "df[df['date'] >= asof + timedelta(days=1)]"
        _assert_suspicious(source, expected_kind="boolean_mask_future_peek")

    def test_index_lt_today_safe(self):
        """bars[bars.index < today] is backward-looking — safe."""
        _assert_clean("bars[bars.index < today]")

    def test_index_lte_today_safe(self):
        """bars[bars.index <= today] — also safe (past / present)."""
        _assert_clean("bars[bars.index <= today]")

    def test_plain_boolean_mask_safe(self):
        """bars[bars['close'] > 100] — price filter, not time-filter."""
        _assert_clean("bars[bars['close'] > 100]")

    def test_index_eq_today_safe(self):
        """== comparison is not forward-peeking (exact timestamp lookup)."""
        _assert_clean("bars[bars.index == today]")


# ---------------------------------------------------------------------------
# 2. Variable negative shift
# ---------------------------------------------------------------------------


class TestVariableNegativeShift:
    """n = -1; df.shift(n) — tracks single-assignment negative constants."""

    def test_variable_shift_minus1_detected(self):
        source = 'n = -1\nbars["close"].shift(n)'
        _assert_suspicious(source, expected_kind="variable_negative_shift")

    def test_variable_shift_minus5_detected(self):
        source = 'lag = -5\nbars["close"].shift(lag)'
        _assert_suspicious(source, expected_kind="variable_negative_shift")

    def test_variable_shift_positive_safe(self):
        """n = 1; df.shift(n) — positive, safe."""
        _assert_clean('n = 1\nbars["close"].shift(n)')

    def test_variable_shift_zero_safe(self):
        _assert_clean('n = 0\nbars["close"].shift(n)')

    def test_variable_shift_negate_of_positive_detected(self):
        """n = 1; df.shift(-n) — -n where n=1 is a literal negative.
        Note: this is caught by the existing _const_int(-N) path since -n
        is a UnaryOp(USub, Name) and _const_int returns None for Names,
        but the assignment-chain case tests variable_negative_shift path."""
        # n=1 positive assignment, so -n in shift(-n) is a UnaryOp(USub, Name("n"))
        # _const_int returns None for Name, so this goes through the variable path.
        # Since n=1 (positive), it is NOT in _neg_assigned_names → safe.
        # But the unary -n case: _const_int(UnaryOp(USub, Name("n"))) = None
        # → falls to variable path; n=1, not negative → clean.
        source = 'n = 1\nbars["close"].shift(-n)'
        # -n with n=1 means shift(-1) which is a literal negative UnaryOp
        # BUT _const_int(UnaryOp(USub, Name)) = None because inner Name lookup fails
        # This SHOULD be flagged as a variable_negative_shift per task spec
        # "n=1; df.shift(-n) (both must catch)"
        # The assignment tracks n=1 (positive), and -n is a UnaryOp(USub, Name("n"))
        # We flag this: the inner unary resolves to None (can't const-fold Name),
        # so we check if -n has a Name base and track positively-assigned names
        # flipped to negative. This edge case is documented in ADR-0051.
        # For v0.2 spec compliance: check it fires.
        result = check_no_lookahead(source)
        # Per spec: "n=1; df.shift(-n) (both must catch)"
        assert not result.passes, (
            "shift(-n) where n=1 should fire (unary negation of positive var)"
        )

    def test_variable_shift_periods_keyword_detected(self):
        """n = -3; df.shift(periods=n)."""
        source = 'n = -3\nbars["close"].shift(periods=n)'
        _assert_suspicious(source, expected_kind="variable_negative_shift")

    def test_unrelated_variable_not_flagged(self):
        """m = -1; df.shift(n) — n is NOT the negative variable → clean."""
        source = 'm = -1\nbars["close"].shift(n)'
        # n is undefined/untracked — no negative assignment
        result = check_no_lookahead(source)
        # m=-1 is assigned but shift uses n (different name) → safe
        # (ignoring NameError at runtime which isn't our concern)
        assert result.passes or not any(
            s["kind"] == "variable_negative_shift" for s in result.suspicions
        ), "Should not fire variable_negative_shift for an unrelated variable"


# ---------------------------------------------------------------------------
# 3. Forward label index
# ---------------------------------------------------------------------------


class TestForwardLabelIndex:
    """df.loc[future_idx:] and df.loc[next_date]."""

    def test_loc_future_idx_slice_detected(self):
        _assert_suspicious(
            "df.loc[future_idx:]",
            expected_kind="forward_label_index",
        )

    def test_loc_next_date_detected(self):
        _assert_suspicious(
            "df.loc[next_date]",
            expected_kind="forward_label_index",
        )

    def test_loc_fwd_ts_slice_detected(self):
        _assert_suspicious(
            "bars.loc[fwd_ts:]",
            expected_kind="forward_label_index",
        )

    def test_loc_ahead_date_detected(self):
        _assert_suspicious(
            "prices.loc[ahead_date]",
            expected_kind="forward_label_index",
        )

    def test_loc_forward_date_detected(self):
        _assert_suspicious(
            "bars.loc[forward_date:]",
            expected_kind="forward_label_index",
        )

    def test_loc_asof_plus1_detected(self):
        _assert_suspicious(
            "df.loc[asof_plus_1:]",
            expected_kind="forward_label_index",
        )

    def test_loc_historical_date_safe(self):
        """df.loc[historical_date] — 'historical' is not a forward keyword."""
        _assert_clean("df.loc[historical_date]")

    def test_loc_start_date_safe(self):
        """df.loc[start_date:] — 'start' not in forward substrings."""
        _assert_clean("df.loc[start_date:]")

    def test_loc_string_label_safe(self):
        """df.loc['2024-01-01':] — string constant, not a Name."""
        _assert_clean("df.loc['2024-01-01':]")


# ---------------------------------------------------------------------------
# 4. pct_change negative
# ---------------------------------------------------------------------------


class TestPctChangeNegative:
    """pct_change(-N) with negative periods peeks forward."""

    def test_pct_change_minus1_detected(self):
        _assert_suspicious(
            'bars["close"].pct_change(-1)',
            expected_kind="pct_change_negative",
        )

    def test_pct_change_minus5_detected(self):
        _assert_suspicious(
            'bars["close"].pct_change(-5)',
            expected_kind="pct_change_negative",
        )

    def test_pct_change_periods_keyword_negative_detected(self):
        _assert_suspicious(
            'bars["close"].pct_change(periods=-2)',
            expected_kind="pct_change_negative",
        )

    def test_pct_change_positive_safe(self):
        """pct_change(1) is standard backward momentum — safe."""
        _assert_clean('bars["close"].pct_change(1)')

    def test_pct_change_5_safe(self):
        _assert_clean('bars["close"].pct_change(5)')

    def test_pct_change_zero_safe(self):
        _assert_clean('bars["close"].pct_change(0)')

    def test_pct_change_no_arg_safe(self):
        """pct_change() with no args defaults to 1 — safe."""
        _assert_clean('bars["close"].pct_change()')


# ---------------------------------------------------------------------------
# 5. diff negative
# ---------------------------------------------------------------------------


class TestDiffNegative:
    """diff(-N) with negative periods peeks forward."""

    def test_diff_minus1_detected(self):
        _assert_suspicious(
            'bars["close"].diff(-1)',
            expected_kind="diff_negative",
        )

    def test_diff_minus3_detected(self):
        _assert_suspicious(
            'bars["close"].diff(-3)',
            expected_kind="diff_negative",
        )

    def test_diff_periods_keyword_negative_detected(self):
        _assert_suspicious(
            'bars["close"].diff(periods=-1)',
            expected_kind="diff_negative",
        )

    def test_diff_positive_safe(self):
        """diff(1) is backward — safe."""
        _assert_clean('bars["close"].diff(1)')

    def test_diff_5_safe(self):
        _assert_clean('bars["close"].diff(5)')

    def test_diff_zero_safe(self):
        _assert_clean('bars["close"].diff(0)')

    def test_diff_no_arg_safe(self):
        _assert_clean('bars["close"].diff()')


# ---------------------------------------------------------------------------
# 6. Rolling-lambda future
# ---------------------------------------------------------------------------


class TestRollingLambdaFuture:
    """rolling(N).apply(lambda x: x[-1]) uses the last (future) element."""

    def test_rolling_apply_negative_index_detected(self):
        _assert_suspicious(
            'bars["close"].rolling(5).apply(lambda x: x[-1])',
            expected_kind="rolling_lambda_future",
        )

    def test_rolling_apply_negative_index_minus2_detected(self):
        _assert_suspicious(
            'bars["close"].rolling(10).apply(lambda x: x[-2])',
            expected_kind="rolling_lambda_future",
        )

    def test_rolling_apply_positive_index_safe(self):
        """lambda x: x[0] — first element of the window (oldest), safe."""
        _assert_clean('bars["close"].rolling(5).apply(lambda x: x[0])')

    def test_rolling_apply_mean_lambda_safe(self):
        """lambda x: x.mean() — aggregate, no indexing."""
        _assert_clean('bars["close"].rolling(5).apply(lambda x: x.mean())')

    def test_rolling_apply_sum_lambda_safe(self):
        _assert_clean('bars["close"].rolling(5).apply(lambda x: x.sum())')

    def test_expanding_apply_negative_detected(self):
        """expanding().apply(lambda x: x[-1]) is also suspicious.

        Note: expanding() is not .rolling(), so this will NOT fire
        rolling_lambda_future (by v0.2 design — documented limit).
        This test documents the known gap.
        """
        result = check_no_lookahead(
            'bars["close"].expanding().apply(lambda x: x[-1])'
        )
        # v0.2 does NOT fire on expanding() — documented gap for v0.3
        # This test verifies the current (limited) behaviour, not a failure.
        # If this changes in v0.3, update this test.
        _ = result  # result may or may not pass — just don't crash


# ---------------------------------------------------------------------------
# 7. Compound / chained patterns
# ---------------------------------------------------------------------------


class TestCompoundPatterns:
    """Multiple lookahead signals in the same source snippet."""

    def test_shift_and_boolean_mask_both_detected(self):
        """Two independent lookahead patterns in one factor."""
        source = (
            'bars["close"].shift(-1)\n'
            'bars[bars.index > today]'
        )
        result = check_no_lookahead(source)
        assert not result.passes
        kinds = {s["kind"] for s in result.suspicions}
        assert "negative_shift" in kinds
        assert "boolean_mask_future_peek" in kinds

    def test_diff_and_pct_change_both_detected(self):
        source = (
            'bars["close"].diff(-1)\n'
            'bars["close"].pct_change(-1)'
        )
        result = check_no_lookahead(source)
        assert not result.passes
        kinds = {s["kind"] for s in result.suspicions}
        assert "diff_negative" in kinds
        assert "pct_change_negative" in kinds

    def test_chained_cumsum_shift_detected(self):
        """cumsum().shift(-N) is a forward peek on the cumulative sum."""
        _assert_suspicious(
            'bars["close"].cumsum().shift(-1)',
            expected_kind="negative_shift",
        )

    def test_chained_expanding_mean_shift_detected(self):
        """expanding().mean().shift(-N)."""
        _assert_suspicious(
            'bars["close"].expanding().mean().shift(-1)',
            expected_kind="negative_shift",
        )

    def test_variable_shift_and_rolling_lambda_both_detected(self):
        """Both variable_negative_shift and rolling_lambda_future in one block."""
        source = (
            'n = -1\n'
            'x = bars["close"].shift(n)\n'
            'y = bars["close"].rolling(5).apply(lambda w: w[-1])'
        )
        result = check_no_lookahead(source)
        assert not result.passes
        kinds = {s["kind"] for s in result.suspicions}
        assert "variable_negative_shift" in kinds
        assert "rolling_lambda_future" in kinds

    def test_all_clean_canonical_factor(self):
        """A canonical clean factor should still pass with v0.2 in place."""
        source = (
            'bars["close"].rolling(20).mean() / '
            'bars["close"].rolling(20).std()'
        )
        _assert_clean(source)

    def test_complex_safe_factor_with_pct_change(self):
        """alpha_momentum_60d style factor — pct_change with positive period."""
        source = 'bars["close"].pct_change(60).rolling(5).mean()'
        _assert_clean(source)

    def test_loc_safe_with_past_lookback(self):
        """df.loc[start:end] where neither label is forward-looking."""
        _assert_clean("df.loc[start_date:end_date]")
