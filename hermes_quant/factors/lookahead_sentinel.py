"""hermes_quant.factors.lookahead_sentinel — Heuristic lookahead detector.

Detects implicit time-travel in factor source code that would contaminate a
backtest — specifically:

  (a) ``series.shift(-N)`` or ``.shift(periods=-N)`` — negative shift peeks
      into the future.
  (b) ``df.iloc[i+1:]`` or similar forward-slicing patterns.
  (c) Any ``iloc`` index expression built from a positive offset.
  (d) Shifting common target-column names (y, ret, fwd_return, target,
      forward_return) with a negative period.

This is a **v0.1 conservative heuristic**.  False positives are possible
(e.g. legitimate use of shift(-1) in a look-forward-normalised feature
pipeline).  False negatives are rare but possible for obfuscated patterns.
Document limitations in ADR-0050.

The sentinel runs *in addition to* the AST purity gate; it does not replace it.

References:
    HKUDS/Vibe-Trading — lookahead sentinel (Wave 8c, ADR-0050)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Column names treated as "target" variables (shift(-N) on these = look-ahead)
# ---------------------------------------------------------------------------

_TARGET_NAMES: frozenset[str] = frozenset(
    {"y", "ret", "fwd_return", "target", "forward_return", "label", "returns"}
)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class LookaheadDetected(ValueError):
    """Raised when the lookahead sentinel rejects a factor.

    Attributes:
        violation_kind: Short machine-readable tag (e.g. "negative_shift").
        suspicions:     Full list of suspicion dicts from the sentinel check.
    """

    def __init__(
        self,
        message: str,
        violation_kind: str,
        suspicions: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.violation_kind = violation_kind
        self.suspicions: list[dict[str, Any]] = suspicions or []


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class LookaheadResult:
    """Result of :func:`check_no_lookahead`.

    Attributes:
        passes:     True iff no suspicious patterns were detected.
        suspicions: List of dicts, each describing one detected pattern::

                        {
                            "kind":    str,   # e.g. "negative_shift"
                            "detail":  str,   # human-readable explanation
                            "line":    int,
                            "col":     int,
                        }
    """

    passes: bool
    suspicions: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------


class _LookaheadVisitor(ast.NodeVisitor):
    """Walk an AST and collect lookahead suspicions."""

    def __init__(self) -> None:
        self.suspicions: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record(self, kind: str, detail: str, node: ast.AST) -> None:
        self.suspicions.append(
            {
                "kind": kind,
                "detail": detail,
                "line": getattr(node, "lineno", 0),
                "col": getattr(node, "col_offset", 0),
            }
        )

    # ------------------------------------------------------------------
    # Utility: evaluate a simple constant integer from an AST node.
    # Returns None if it can't be determined statically.
    # ------------------------------------------------------------------

    @staticmethod
    def _const_int(node: ast.expr) -> int | None:
        """Return int value if node is a constant integer, else None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        # Python 3.7 compat: ast.Num
        if isinstance(node, ast.Num):  # type: ignore[attr-defined]  # pragma: no cover
            if isinstance(node.n, int):
                return node.n
        # Unary minus: -N
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
        ):
            inner = _LookaheadVisitor._const_int(node.operand)
            if inner is not None:
                return -inner
        return None

    # ------------------------------------------------------------------
    # Detect .shift(-N)
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "shift":
            # Check positional arg: .shift(-N)
            if node.args:
                period_node = node.args[0]
                period_val = self._const_int(period_node)
                if period_val is not None and period_val < 0:
                    # Extra: check if the receiver name looks like a target col
                    col_name = self._extract_column_name(func.value)
                    tag = "negative_shift_on_target" if col_name in _TARGET_NAMES else "negative_shift"
                    self._record(
                        tag,
                        f".shift({period_val}) peeks into the future",
                        node,
                    )
            # Check keyword arg: .shift(periods=-N)
            for kw in node.keywords:
                if kw.arg == "periods":
                    period_val = self._const_int(kw.value)
                    if period_val is not None and period_val < 0:
                        col_name = self._extract_column_name(func.value)
                        tag = (
                            "negative_shift_periods_on_target"
                            if col_name in _TARGET_NAMES
                            else "negative_shift_periods"
                        )
                        self._record(
                            tag,
                            f".shift(periods={period_val}) peeks into the future",
                            node,
                        )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Detect df.iloc[i+1:] or df.iloc[i+N:]
    # ------------------------------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Detect forward iloc slicing: df.iloc[N:] where N > 0 (as addition)."""
        if not (
            isinstance(node.value, ast.Attribute) and node.value.attr == "iloc"
        ):
            self.generic_visit(node)
            return

        # node.slice may be a Slice, Index (py3.8), or expression
        slc = node.slice

        # Unwrap ast.Index wrapper (Python 3.8 and earlier)
        if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
            slc = slc.value  # type: ignore[attr-defined]

        if isinstance(slc, ast.Slice):
            # Check lower bound for positive offset
            if slc.lower is not None:
                self._check_forward_offset(slc.lower, node)
        elif isinstance(slc, ast.Tuple):
            # Multi-dimensional: df.iloc[a:b, c:d]
            for elt in slc.elts:
                if isinstance(elt, ast.Slice) and elt.lower is not None:
                    self._check_forward_offset(elt.lower, node)

        self.generic_visit(node)

    def _check_forward_offset(self, lower: ast.expr, parent: ast.AST) -> None:
        """Record a suspicion if *lower* looks like i+N (positive forward offset)."""
        # Pattern: i + N or N + i where N > 0
        if not isinstance(lower, ast.BinOp):
            return
        if not isinstance(lower.op, ast.Add):
            return
        # Try to extract the constant part
        left_val = self._const_int(lower.left)
        right_val = self._const_int(lower.right)
        offset = left_val if left_val is not None else right_val
        if offset is not None and offset > 0:
            self._record(
                "forward_iloc_slice",
                f"iloc[i+{offset}:] may access future rows",
                parent,
            )

    # ------------------------------------------------------------------
    # Helper: extract a column name string from a node if it's a subscript
    # like df["colname"] or a Name like `returns`.
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_column_name(node: ast.expr) -> str | None:
        if isinstance(node, ast.Subscript):
            slc = node.slice
            # Unwrap ast.Index (py3.8)
            if hasattr(slc, "value") and isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]
            if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
                return slc.value
        if isinstance(node, ast.Name):
            return node.id
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_no_lookahead(source: str) -> LookaheadResult:
    """Heuristically scan *source* for implicit lookahead patterns.

    This is a **v0.1 conservative** detector.  False positives are expected
    in legitimate pipelines (e.g. forward-normalised labels).  False
    negatives are rare but possible for obfuscated code.  Run alongside
    :func:`~hermes_quant.factors.ast_purity.check_factor_purity`.

    Args:
        source: Python source code of the factor.

    Returns:
        :class:`LookaheadResult` — ``passes=True`` when no patterns found.

    Raises:
        SyntaxError: If *source* cannot be parsed.

    Examples::

        # Positive shift is fine
        result = check_no_lookahead('bars["close"].shift(1)')
        assert result.passes

        # Negative shift is suspicious
        result = check_no_lookahead('bars["close"].shift(-1)')
        assert not result.passes
    """
    tree = ast.parse(source, mode="exec")
    visitor = _LookaheadVisitor()
    visitor.visit(tree)
    return LookaheadResult(
        passes=len(visitor.suspicions) == 0,
        suspicions=visitor.suspicions,
    )
