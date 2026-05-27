"""hermes_quant.factors.lookahead_sentinel — Heuristic lookahead detector.

Detects implicit time-travel in factor source code that would contaminate a
backtest — specifically:

  (a) ``series.shift(-N)`` or ``.shift(periods=-N)`` — negative shift peeks
      into the future.
  (b) ``df.iloc[i+1:]`` or similar forward-slicing patterns.
  (c) Any ``iloc`` index expression built from a positive offset.
  (d) Shifting common target-column names (y, ret, fwd_return, target,
      forward_return) with a negative period.

v0.2 additionally detects (MoA review I1 false-negatives):

  (e) Boolean-mask future-peek — ``bars[bars.index > today]`` or
      ``df[df['date'] >= asof + timedelta(days=1)]``.
  (f) Variable negative shift — ``n = -1; df.shift(n)`` where the argument
      is a Name whose assigned constant is negative.
  (g) Forward label index — ``df.loc[asof_plus_1:]`` where the label matches
      a forward-looking name heuristic.
  (h) ``pct_change(-N)`` or ``diff(-N)`` with negative periods argument.
  (i) Rolling-lambda future-peek — ``rolling(N).apply(lambda x: x[-1])``
      using a negative index inside a rolling apply, which can access the
      last (most recent, i.e. future) element of the window.
  (j) Chained future-peeks — ``cumsum().shift(-N)``,
      ``expanding().mean().shift(-N)``.

This is a **v0.2 conservative heuristic**.  False positives are possible
(e.g. legitimate use of shift(-1) in a look-forward-normalised feature
pipeline, or a rolling lambda using x[-1] on a purely historical window).
False negatives remain possible for obfuscated patterns (e.g. dynamically
constructed negative integers, variable reassignment chains).  Document
limitations in ADR-0051.

**v0.2 heuristic limits (documented, will improve in v0.3):**

  * Variable-shift tracking (F): only single-level, no-reassignment
    assignments are tracked (``n = -1; df.shift(n)``).  Two-step chains like
    ``m = 2; n = -m; df.shift(n)`` are NOT caught.
  * Boolean-mask heuristic (E): only fires when the comparator Name is in the
    set ``{'today', 'asof', 'now', 'current', 'future', 'cutoff', 'horizon',
    'end_date', 'forecast_date'}``.  Custom variable names are not caught.
  * Forward-label-index heuristic (G): only fires when the ``loc`` label is a
    Name whose id contains a forward-looking substring (``future``, ``next``,
    ``ahead``, ``fwd``, ``plus``, ``forward``, ``asof_plus``).
  * Rolling-lambda future-peek (I): only fires on ``lambda x: x[-1]``
    (constant negative index); deeper lambda bodies are not analysed.
  * All detectors are heuristic — a v0.3 symbolic-execution pass is planned
    for full coverage (see ADR-0051 §v0.3 deferred plan).

The sentinel runs *in addition to* the AST purity gate; it does not replace it.

References:
    HKUDS/Vibe-Trading — lookahead sentinel (Wave 8c, ADR-0050)
    MoA review I1 false-negative list → v0.2 hardening (ADR-0051)
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
# v0.2: Heuristic name-sets for new pattern detectors
# ---------------------------------------------------------------------------

# Names that plausibly represent "the current timestamp / boundary"
# Used by the boolean-mask future-peek detector (pattern E).
_TEMPORAL_BOUNDARY_NAMES: frozenset[str] = frozenset(
    {
        "today",
        "asof",
        "now",
        "current",
        "future",
        "cutoff",
        "horizon",
        "end_date",
        "forecast_date",
    }
)

# Substrings that suggest a label variable refers to a future index.
# Used by the forward-label-index detector (pattern G).
_FORWARD_LABEL_SUBSTRINGS: tuple[str, ...] = (
    "future",
    "next",
    "ahead",
    "fwd",
    "plus",
    "forward",
    "asof_plus",
)

# Methods whose negative first argument means "look N periods *forward*".
_NEGATIVE_PERIOD_METHODS: frozenset[str] = frozenset({"pct_change", "diff"})


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
    """Walk an AST and collect lookahead suspicions.

    v0.2 additions over v0.1:
      - ``_neg_assigned_names``: maps Name → negative constant for variable-
        shift detection.
      - ``visit_Assign``: pre-scan phase that populates ``_neg_assigned_names``
        for single-assignment negative constants.
      - Extended ``visit_Call``: catches ``pct_change(-N)``, ``diff(-N)``,
        variable-shift, and rolling-lambda patterns.
      - Extended ``visit_Subscript``: catches boolean-mask future-peek and
        forward-label-index (``df.loc[...]``).
    """

    def __init__(self) -> None:
        self.suspicions: list[dict[str, Any]] = []
        # Maps Name.id → negative int value for single-assign tracking (v0.2).
        self._neg_assigned_names: dict[str, int] = {}
        # Maps Name.id → positive int value for -n detection (v0.2).
        self._pos_assigned_names: dict[str, int] = {}

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
    # v0.2 pre-scan: collect single-assignment negative constants.
    # This runs as part of the first-pass generic_visit traversal because
    # visit_Assign is called by the NodeVisitor dispatch mechanism.
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track ``n = -K`` and ``n = K`` assignments for variable-shift detection."""
        val = self._const_int(node.value)
        if val is not None and val < 0:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._neg_assigned_names[target.id] = val
                    # Clear from positive table if overwritten
                    self._pos_assigned_names.pop(target.id, None)
        elif val is not None and val > 0:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._pos_assigned_names[target.id] = val
                    # Cancel any negative tracking (first assignment wins heuristic
                    # broken by explicit overwrite)
                    self._neg_assigned_names.pop(target.id, None)
        elif val is not None and val == 0:
            # Zero assignment: remove from both tables
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._neg_assigned_names.pop(target.id, None)
                    self._pos_assigned_names.pop(target.id, None)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Detect .shift(-N) and variable-shift, pct_change(-N), diff(-N),
    # rolling(N).apply(lambda x: x[-1])
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: C901  (acceptable complexity)
        func = node.func
        if isinstance(func, ast.Attribute):
            attr = func.attr

            # ----------------------------------------------------------------
            # v0.1: .shift(-N) / .shift(periods=-N)
            # ----------------------------------------------------------------
            if attr == "shift":
                # Positional arg
                if node.args:
                    period_node = node.args[0]
                    period_val = self._const_int(period_node)
                    if period_val is not None and period_val < 0:
                        col_name = self._extract_column_name(func.value)
                        tag = (
                            "negative_shift_on_target"
                            if col_name in _TARGET_NAMES
                            else "negative_shift"
                        )
                        self._record(
                            tag,
                            f".shift({period_val}) peeks into the future",
                            node,
                        )
                    elif period_val is None:
                        # v0.2 (F): variable-shift — check assignment table
                        var_name = self._extract_shifted_var_name(period_node)
                        if var_name is not None:
                            neg_val = self._resolve_negative_var(
                                period_node, var_name
                            )
                            if neg_val is not None:
                                self._record(
                                    "variable_negative_shift",
                                    (
                                        f".shift({var_name}) resolves to "
                                        f"{neg_val} — peeks into the future"
                                    ),
                                    node,
                                )
                # Keyword arg: .shift(periods=-N) or .shift(periods=n)
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
                        elif period_val is None:
                            # v0.2 (F): variable shift via keyword
                            var_name = self._extract_shifted_var_name(kw.value)
                            if var_name is not None:
                                neg_val = self._resolve_negative_var(
                                    kw.value, var_name
                                )
                                if neg_val is not None:
                                    self._record(
                                        "variable_negative_shift",
                                        (
                                            f".shift(periods={var_name}) resolves to "
                                            f"{neg_val} — peeks into the future"
                                        ),
                                        node,
                                    )

            # ----------------------------------------------------------------
            # v0.2 (H): pct_change(-N) and diff(-N)
            # ----------------------------------------------------------------
            elif attr in _NEGATIVE_PERIOD_METHODS:
                # Positional arg
                if node.args:
                    period_val = self._const_int(node.args[0])
                    if period_val is not None and period_val < 0:
                        kind = (
                            "pct_change_negative"
                            if attr == "pct_change"
                            else "diff_negative"
                        )
                        self._record(
                            kind,
                            f".{attr}({period_val}) with negative period peeks forward",
                            node,
                        )
                # Keyword arg: pct_change(periods=-N)
                for kw in node.keywords:
                    if kw.arg == "periods":
                        period_val = self._const_int(kw.value)
                        if period_val is not None and period_val < 0:
                            kind = (
                                "pct_change_negative"
                                if attr == "pct_change"
                                else "diff_negative"
                            )
                            self._record(
                                kind,
                                (
                                    f".{attr}(periods={period_val}) with negative "
                                    f"period peeks forward"
                                ),
                                node,
                            )

            # ----------------------------------------------------------------
            # v0.2 (I): rolling(N).apply(lambda x: x[-1])
            # ----------------------------------------------------------------
            elif attr == "apply":
                # Check if this is chained on a .rolling(...) call
                if self._is_rolling_receiver(func.value) and node.args:
                    lambda_node = node.args[0]
                    if isinstance(lambda_node, ast.Lambda):
                        if self._lambda_uses_negative_index(lambda_node):
                            self._record(
                                "rolling_lambda_future",
                                (
                                    "rolling(...).apply(lambda x: x[-N]) "
                                    "accesses the last element of the window "
                                    "(future bar relative to the anchor point)"
                                ),
                                node,
                            )

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Detect df.iloc[i+1:] or df.iloc[i+N:]  (v0.1)
    # Detect df[df.index > today]              (v0.2 E — boolean-mask)
    # Detect df.loc[future_idx:]               (v0.2 G — forward label)
    # ------------------------------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: C901
        val = node.value

        # v0.1: iloc forward-slicing
        if isinstance(val, ast.Attribute) and val.attr == "iloc":
            slc = node.slice
            # Unwrap ast.Index wrapper (Python 3.8 and earlier)
            if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]

            if isinstance(slc, ast.Slice):
                if slc.lower is not None:
                    self._check_forward_offset(slc.lower, node)
            elif isinstance(slc, ast.Tuple):
                for elt in slc.elts:
                    if isinstance(elt, ast.Slice) and elt.lower is not None:
                        self._check_forward_offset(elt.lower, node)

        # v0.2 (G): df.loc[future_label:] forward label indexing
        elif isinstance(val, ast.Attribute) and val.attr == "loc":
            slc = node.slice
            if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]

            label_node: ast.expr | None = None
            if isinstance(slc, ast.Slice):
                label_node = slc.lower  # e.g. df.loc[future_idx:]
            elif isinstance(slc, ast.Name):
                label_node = slc  # e.g. df.loc[future_idx]

            if label_node is not None and isinstance(label_node, ast.Name):
                if self._is_forward_label_name(label_node.id):
                    self._record(
                        "forward_label_index",
                        (
                            f"df.loc[{label_node.id}...] uses a forward-looking "
                            f"label — may access future rows"
                        ),
                        node,
                    )

        # v0.2 (E): df[df.index > today] boolean-mask future-peek
        else:
            slc = node.slice
            if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]
            if isinstance(slc, ast.Compare):
                self._check_boolean_mask_future_peek(slc, node)

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
    # v0.2 (E): Boolean-mask future-peek helpers
    # ------------------------------------------------------------------

    def _check_boolean_mask_future_peek(
        self, compare_node: ast.Compare, parent: ast.AST
    ) -> None:
        """Detect ``df[df.index > today]`` or ``df[df['date'] >= asof+...]``."""
        left = compare_node.left
        # Check comparators — the temporal boundary name should appear on
        # either side as a Name node.
        all_nodes = [left] + list(compare_node.comparators)
        ops = compare_node.ops

        # Only flag forward-looking comparisons: >, >= (not <, <=, ==)
        has_forward_op = any(
            isinstance(op, (ast.Gt, ast.GtE)) for op in ops
        )
        if not has_forward_op:
            return

        # Check left is an index/date accessor and right contains a temporal name
        if not self._is_index_or_date_access(left):
            return

        for comparator in compare_node.comparators:
            if self._contains_temporal_boundary_name(comparator):
                self._record(
                    "boolean_mask_future_peek",
                    (
                        "df[df.index > today/asof/...] boolean mask filters to "
                        "future rows — lookahead contamination"
                    ),
                    parent,
                )
                return  # Only record once per Compare node

    @staticmethod
    def _is_index_or_date_access(node: ast.expr) -> bool:
        """Return True if node looks like ``df.index`` or ``df['date']``."""
        if isinstance(node, ast.Attribute) and node.attr in ("index", "dates", "date"):
            return True
        if isinstance(node, ast.Subscript):
            slc = node.slice
            if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]
            if isinstance(slc, ast.Constant) and slc.value in (
                "date",
                "dates",
                "timestamp",
                "datetime",
            ):
                return True
        return False

    @staticmethod
    def _contains_temporal_boundary_name(node: ast.expr) -> bool:
        """Return True if *node* is or contains a temporal-boundary Name."""
        # Direct Name match
        if isinstance(node, ast.Name) and node.id in _TEMPORAL_BOUNDARY_NAMES:
            return True
        # BinOp like ``asof + timedelta(days=1)``
        if isinstance(node, ast.BinOp):
            return _LookaheadVisitor._contains_temporal_boundary_name(
                node.left
            ) or _LookaheadVisitor._contains_temporal_boundary_name(node.right)
        # Call like ``timedelta(days=N)`` — don't recurse further to avoid noise
        return False

    # ------------------------------------------------------------------
    # v0.2 (G): Forward-label-index helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_forward_label_name(name: str) -> bool:
        """Return True if *name* contains a forward-looking substring."""
        name_lower = name.lower()
        return any(sub in name_lower for sub in _FORWARD_LABEL_SUBSTRINGS)

    # ------------------------------------------------------------------
    # v0.2 (F): Variable-shift helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_shifted_var_name(node: ast.expr) -> str | None:
        """Return the Name.id from a shift argument if it is a bare Name
        or a UnaryOp(USub, Name) — the two forms we track.

        Returns None for anything else (constants are handled by _const_int).
        """
        if isinstance(node, ast.Name):
            return node.id
        # -n form: UnaryOp(USub, Name)
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Name)
        ):
            return node.operand.id
        return None

    def _resolve_negative_var(
        self, node: ast.expr, var_name: str
    ) -> int | None:
        """Return a negative effective value if *node* resolves to < 0.

        Handles two cases:
          * ``shift(n)``  — bare Name: look up n in ``_neg_assigned_names``.
          * ``shift(-n)`` — UnaryOp(USub, Name): look up n; if n > 0, -n < 0.
        """
        if isinstance(node, ast.Name):
            # n = -K; shift(n) → negative
            return self._neg_assigned_names.get(var_name)  # None if not tracked
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Name)
        ):
            # shift(-n) where n is a tracked positive constant → -n < 0
            pos_val = self._pos_assigned_names.get(var_name)
            if pos_val is not None and pos_val > 0:
                return -pos_val  # effective value is negative
        return None

    # ------------------------------------------------------------------
    # v0.2 (I): Rolling-lambda helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_rolling_receiver(node: ast.expr) -> bool:
        """Return True if *node* is a call chain ending in ``.rolling(...)``."""
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "rolling":
            return True
        # Also catch chains like expanding().mean().rolling() — unlikely but safe
        return False

    @staticmethod
    def _lambda_uses_negative_index(lambda_node: ast.Lambda) -> bool:
        """Return True if lambda body does ``x[-K]`` (negative constant index)."""
        body = lambda_node.body
        # Simple form: lambda x: x[-1]
        if isinstance(body, ast.Subscript):
            slc = body.slice
            if isinstance(slc, ast.Index):  # type: ignore[attr-defined]
                slc = slc.value  # type: ignore[attr-defined]
            val = _LookaheadVisitor._const_int(slc)
            if val is not None and val < 0:
                return True
        return False

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

    This is a **v0.2 conservative** detector.  False positives are expected
    in legitimate pipelines (e.g. forward-normalised labels, rolling windows
    that use ``x[-1]`` on purely historical data).  False negatives remain
    possible for obfuscated code.  Run alongside
    :func:`~hermes_quant.factors.ast_purity.check_factor_purity`.

    **v0.1 patterns (backward-compatible):**

    * ``series.shift(-N)`` / ``.shift(periods=-N)`` — kind: ``negative_shift``,
      ``negative_shift_periods``, ``negative_shift_on_target``,
      ``negative_shift_periods_on_target``.
    * ``df.iloc[i+N:]`` forward-slice — kind: ``forward_iloc_slice``.

    **v0.2 new patterns (MoA review I1):**

    * ``df[df.index > today]`` — kind: ``boolean_mask_future_peek``.
    * ``n = -1; df.shift(n)`` — kind: ``variable_negative_shift``.
    * ``df.loc[future_idx:]`` — kind: ``forward_label_index``.
    * ``pct_change(-N)`` — kind: ``pct_change_negative``.
    * ``diff(-N)`` — kind: ``diff_negative``.
    * ``rolling(N).apply(lambda x: x[-1])`` — kind: ``rolling_lambda_future``.

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

        # Boolean-mask future-peek (v0.2)
        result = check_no_lookahead('bars[bars.index > today]')
        assert not result.passes

        # Variable negative shift (v0.2)
        result = check_no_lookahead('n = -1\\nbars["close"].shift(n)')
        assert not result.passes

        # pct_change with negative period (v0.2)
        result = check_no_lookahead('bars["close"].pct_change(-1)')
        assert not result.passes
    """
    tree = ast.parse(source, mode="exec")
    visitor = _LookaheadVisitor()
    visitor.visit(tree)
    return LookaheadResult(
        passes=len(visitor.suspicions) == 0,
        suspicions=visitor.suspicions,
    )
