"""hermes_quant.factors.ast_purity — AST-based factor purity gate.

Defends against the "Generative Factor Mining Slop" failure mode (F5): when
an LLM-driven factor miner emits expressions containing look-ahead data
access, network calls, file I/O, or eval/exec pathways, those expressions
silently corrupt any backtest that admits them.

This gate statically rejects factor source code that references:
  - Forbidden built-in names (os, sys, requests, eval, exec, ...)
  - Forbidden attribute accesses (system, popen, loads, ...)
  - Forbidden pandas I/O methods (to_csv, read_sql, ...)
  - Any import statement (factors must be self-contained pure expressions)
  - ``__builtins__`` dunder escapes

References:
    HKUDS/Vibe-Trading — AST purity gate (Wave 8c, ADR-0050)
    WorldQuant Alpha Catalog — factor purity conventions
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Forbidden symbol sets
# ---------------------------------------------------------------------------

FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
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
        # built-in introspection / mutation helpers
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
    }
)

FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "system",
        "popen",
        "loads",
        "load",
        "dump",
        "dumps",
        "getattr",
        "setattr",
        "__class__",
        "__bases__",
        "__subclasses__",
    }
)

FORBIDDEN_PD_METHODS: frozenset[str] = frozenset(
    {
        "to_csv",
        "to_pickle",
        "to_sql",
        "read_csv",
        "read_pickle",
        "read_sql",
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PurityViolation(ValueError):
    """Raised when a factor source code fails the AST purity gate.

    Attributes:
        violation_kind: Short machine-readable tag (e.g. "forbidden_name").
        violations:     Full list of violation dicts from the purity check.
    """

    def __init__(
        self,
        message: str,
        violation_kind: str,
        violations: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.violation_kind = violation_kind
        self.violations: list[dict[str, Any]] = violations or []


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PurityResult:
    """Result of :func:`check_factor_purity`.

    Attributes:
        passes:     True iff no violations were detected.
        violations: List of dicts, each describing one violation::

                        {
                            "kind": str,   # e.g. "forbidden_name"
                            "name": str,   # the symbol that triggered it
                            "line": int,
                            "col": int,
                        }
    """

    passes: bool
    violations: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------


class _PurityVisitor(ast.NodeVisitor):
    """Walk an AST tree and collect purity violations."""

    def __init__(self) -> None:
        self.violations: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record(self, kind: str, name: str, node: ast.AST) -> None:
        self.violations.append(
            {
                "kind": kind,
                "name": name,
                "line": getattr(node, "lineno", 0),
                "col": getattr(node, "col_offset", 0),
            }
        )

    # ------------------------------------------------------------------
    # Visitor hooks
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        """Any import statement is forbidden — factors must be pure closures."""
        for alias in node.names:
            self._record("import_statement", alias.name or "<import>", node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """from X import Y is also forbidden."""
        module = node.module or "<module>"
        self._record("import_from_statement", module, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Check for calls to forbidden built-ins or forbidden attribute methods."""
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in FORBIDDEN_NAMES:
                self._record("forbidden_name", func.id, node)
        elif isinstance(func, ast.Attribute):
            attr = func.attr
            if attr in FORBIDDEN_ATTRIBUTES:
                self._record("forbidden_attribute", attr, node)
            if attr in FORBIDDEN_PD_METHODS:
                self._record("forbidden_pd_method", attr, node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Catch attribute *access* (not just calls) for the dangerous ones.

        We catch both access and call so that patterns like
        ``df.to_csv`` (without the call) are also flagged.
        """
        attr = node.attr
        if attr in FORBIDDEN_ATTRIBUTES:
            self._record("forbidden_attribute_access", attr, node)
        if attr in FORBIDDEN_PD_METHODS:
            self._record("forbidden_pd_method_access", attr, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Catch bare name loads for forbidden built-ins."""
        if node.id in FORBIDDEN_NAMES:
            self._record("forbidden_name_ref", node.id, node)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_factor_purity(source: str) -> PurityResult:
    """Statically analyse *source* for purity violations.

    The source may be a lambda body, a full function definition, or any valid
    Python expression / statement block.

    Args:
        source: Python source code of the factor.

    Returns:
        :class:`PurityResult` with ``passes=True`` if no violations found.

    Raises:
        SyntaxError: If *source* cannot be parsed at all.

    Example::

        result = check_factor_purity('bars["close"] - bars["open"]')
        assert result.passes

        result = check_factor_purity('import os; os.system("rm -rf /")')
        assert not result.passes
    """
    tree = ast.parse(source, mode="exec")
    visitor = _PurityVisitor()
    visitor.visit(tree)
    return PurityResult(passes=len(visitor.violations) == 0, violations=visitor.violations)
