"""tests/factors/test_ast_purity.py — Unit tests for the AST purity gate.

Covers:
  - Each FORBIDDEN_NAME triggers PurityResult.passes == False
  - Pure pandas/numpy factor expressions pass
  - import / from-import statements are rejected
  - getattr / setattr attribute access is rejected
  - FORBIDDEN_PD_METHODS are rejected
  - Nested / compound expressions are scanned recursively
"""

from __future__ import annotations

import pytest

from hermes_quant.factors.ast_purity import (
    FORBIDDEN_ATTRIBUTES,
    FORBIDDEN_NAMES,
    FORBIDDEN_PD_METHODS,
    PurityResult,
    PurityViolation,
    check_factor_purity,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_rejects(source: str, expected_kind: str | None = None) -> PurityResult:
    result = check_factor_purity(source)
    assert not result.passes, f"Expected rejection but got passes=True for: {source!r}"
    assert result.violations, "Expected at least one violation"
    if expected_kind is not None:
        kinds = {v["kind"] for v in result.violations}
        assert expected_kind in kinds, f"Expected kind {expected_kind!r} in {kinds}"
    return result


def _assert_passes(source: str) -> PurityResult:
    result = check_factor_purity(source)
    assert result.passes, f"Expected passes=True but got violations: {result.violations}"
    assert result.violations == []
    return result


# ---------------------------------------------------------------------------
# Pure factors — should pass
# ---------------------------------------------------------------------------


class TestPurePasses:
    def test_simple_arithmetic(self):
        _assert_passes('bars["close"] - bars["open"]')

    def test_rolling_mean(self):
        _assert_passes('bars["close"].rolling(20).mean()')

    def test_numpy_log(self):
        _assert_passes('np.log(bars["close"] / bars["close"].shift(5))')

    def test_pct_change(self):
        _assert_passes('bars["close"].pct_change(60)')

    def test_rolling_std(self):
        _assert_passes(
            '(bars["volume"] - bars["volume"].rolling(20).mean()) '
            '/ bars["volume"].rolling(20).std()'
        )

    def test_comparison_astype(self):
        _assert_passes(
            '(bars["close"] > bars["close"].rolling(20).mean()).astype(float)'
        )

    def test_concat_max(self):
        _assert_passes(
            'pd.concat([\n'
            '    (bars["high"] - bars["low"]).abs(),\n'
            '    (bars["high"] - bars["close"].shift(1)).abs(),\n'
            '], axis=1).max(axis=1).rolling(14).mean()'
        )

    def test_corr(self):
        _assert_passes('bars["close"].rolling(20).corr(bars["volume"])')

    def test_np_sign_cumsum(self):
        _assert_passes(
            '(bars["volume"] * np.sign(bars["close"].diff())).cumsum()'
        )


# ---------------------------------------------------------------------------
# Forbidden names — each must trigger rejection
# ---------------------------------------------------------------------------


class TestForbiddenNames:
    @pytest.mark.parametrize(
        "name",
        [
            "open",
            "exec",
            "eval",
            "compile",
            "__import__",
            "globals",
            "locals",
            "vars",
            "breakpoint",
            "input",
            "requests",
            "urllib",
            "socket",
            "subprocess",
            "os",
            "sys",
            "pickle",
            "shelve",
            "random",
        ],
    )
    def test_forbidden_name_rejected(self, name: str):
        source = f"result = {name}"
        result = check_factor_purity(source)
        assert not result.passes, f"Expected {name!r} to be rejected"
        assert any(v["name"] == name for v in result.violations), (
            f"Violation for {name!r} not found; got {result.violations}"
        )

    def test_eval_call_rejected(self):
        _assert_rejects('eval("bars[\\"close\\"]")')

    def test_exec_call_rejected(self):
        _assert_rejects('exec("import os")')

    def test_os_name_access_rejected(self):
        _assert_rejects("os.path.join('/tmp', 'x')")

    def test_nested_eval_rejected(self):
        """eval buried inside a list comprehension must still be caught."""
        _assert_rejects('[eval(x) for x in ["1","2"]]')


# ---------------------------------------------------------------------------
# Forbidden attributes
# ---------------------------------------------------------------------------


class TestForbiddenAttributes:
    def test_getattr_call_rejected(self):
        _assert_rejects('getattr(obj, "method")()', expected_kind="forbidden_name_ref")

    def test_setattr_call_rejected(self):
        _assert_rejects('setattr(obj, "x", 42)', expected_kind="forbidden_name_ref")

    def test_popen_attribute_rejected(self):
        _assert_rejects('subprocess.popen("ls")')

    def test_loads_attribute_rejected(self):
        _assert_rejects('pickle.loads(data)')

    def test_dunder_class_rejected(self):
        _assert_rejects('bars.__class__')

    def test_subclasses_rejected(self):
        _assert_rejects('int.__subclasses__()')


# ---------------------------------------------------------------------------
# Forbidden pandas methods
# ---------------------------------------------------------------------------


class TestForbiddenPdMethods:
    @pytest.mark.parametrize(
        "method",
        [
            "to_csv",
            "to_pickle",
            "to_sql",
            "read_csv",
            "read_pickle",
            "read_sql",
        ],
    )
    def test_forbidden_pd_method_rejected(self, method: str):
        source = f'bars.{method}("/tmp/test.csv")'
        result = check_factor_purity(source)
        assert not result.passes, f"Expected {method!r} to be rejected"

    def test_to_csv_attribute_access_rejected(self):
        """Even bare attribute access (without call) must be caught."""
        _assert_rejects('fn = bars.to_csv')


# ---------------------------------------------------------------------------
# Import statements
# ---------------------------------------------------------------------------


class TestImportRejected:
    def test_import_os_rejected(self):
        _assert_rejects("import os", expected_kind="import_statement")

    def test_import_numpy_rejected(self):
        """Even importing numpy explicitly is forbidden (it's in scope already)."""
        _assert_rejects("import numpy as np", expected_kind="import_statement")

    def test_from_os_import_rejected(self):
        _assert_rejects(
            "from os import path", expected_kind="import_from_statement"
        )

    def test_from_pathlib_import_rejected(self):
        _assert_rejects(
            "from pathlib import Path", expected_kind="import_from_statement"
        )


# ---------------------------------------------------------------------------
# Violation metadata
# ---------------------------------------------------------------------------


class TestViolationMetadata:
    def test_violation_has_line_col(self):
        result = check_factor_purity("import os\nbars.to_csv('/tmp/x')")
        assert not result.passes
        for v in result.violations:
            assert "line" in v
            assert "col" in v
            assert "kind" in v
            assert "name" in v

    def test_multiple_violations_captured(self):
        result = check_factor_purity("import os\nimport sys\nbars.to_csv('/tmp/x')")
        assert not result.passes
        assert len(result.violations) >= 2

    def test_purity_result_type(self):
        result = check_factor_purity('bars["close"]')
        assert isinstance(result, PurityResult)
        assert result.passes is True

    def test_syntax_error_propagates(self):
        with pytest.raises(SyntaxError):
            check_factor_purity("def (broken syntax !!!")
