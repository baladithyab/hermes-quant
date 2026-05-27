"""hermes_quant.factors.alpha_zoo — Alpha Factor registry with purity gates.

Provides an append-only registry of programmatically-described factors.
Every factor admitted through :meth:`AlphaZoo.register` must pass BOTH:

  1. The AST purity gate — rejects forbidden APIs, imports, and I/O.
  2. The lookahead sentinel — rejects negative-shift and forward-iloc patterns.

The registry is backed by an append-only JSONL file
(``~/.hermes/quant/factors/alpha_zoo.jsonl``) and a summary index
(``~/.hermes/quant/factors/registry.json``).

The compute() sandbox evaluates factor source code with ``{"__builtins__": {}}``
to strip eval/exec/import, while still exposing ``pd``, ``np``, and ``bars``.
See ADR-0050 for the security rationale.

References:
    HKUDS/Vibe-Trading — 452-factor Alpha Zoo (Wave 8c, ADR-0050)
    WorldQuant Alpha Catalog — starter factor set
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from hermes_quant.factors.ast_purity import (
    PurityViolation,
    check_factor_purity,
)
from hermes_quant.factors.lookahead_sentinel import (
    LookaheadDetected,
    check_no_lookahead,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump when breaking changes are made to the JSONL format
# ---------------------------------------------------------------------------
_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Default storage path (overridable in tests via env var)
# ---------------------------------------------------------------------------
import os as _os

_DEFAULT_DIR = Path(
    _os.environ.get(
        "HERMES_QUANT_ALPHA_ZOO_DIR",
        Path.home() / ".hermes" / "quant" / "factors",
    )
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FactorExecutionError(RuntimeError):
    """Raised when a factor's compute() call fails at runtime."""

    def __init__(self, factor_id: str, original: Exception) -> None:
        super().__init__(
            f"Factor {factor_id!r} execution failed: {type(original).__name__}: {original}"
        )
        self.factor_id = factor_id
        self.original = original


class AppendOnlyViolation(RuntimeError):
    """Raised when caller tries to mutate or truncate the append-only registry."""


# ---------------------------------------------------------------------------
# AlphaFactor model
# ---------------------------------------------------------------------------


class AlphaFactor(BaseModel):
    """Descriptor of a single alpha factor.

    All fields are validated on construction.  Extra fields are forbidden
    to prevent silent data loss or injection.
    """

    factor_id: str = Field(default="", description="Auto-generated hex ID")
    name: str = Field(..., max_length=128)
    description: str = Field(..., max_length=512)
    source_code: str = Field(..., max_length=8192)
    author: str = Field(default="unknown")
    created_at: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1)

    model_config = {"extra": "forbid"}

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("tags")
    @classmethod
    def _max_tags(cls, v: list[str]) -> list[str]:
        if len(v) > 10:
            raise ValueError("tags may contain at most 10 entries")
        return v

    @field_validator("params")
    @classmethod
    def _max_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 10:
            raise ValueError("params may contain at most 10 keys")
        return v

    @model_validator(mode="after")
    def _fill_defaults(self) -> "AlphaFactor":
        if not self.factor_id:
            # Deterministic ID from name + source_code
            digest = hashlib.sha256(
                (self.name + self.source_code).encode()
            ).hexdigest()[:6]
            object.__setattr__(self, "factor_id", f"alpha_{digest}")
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(),
            )
        return self


# ---------------------------------------------------------------------------
# AlphaZoo
# ---------------------------------------------------------------------------


class AlphaZoo:
    """Append-only registry of alpha factors with AST purity + lookahead gates.

    Storage layout::

        <base_dir>/alpha_zoo.jsonl   — one JSON object per line (factor records)
        <base_dir>/registry.json     — summary index {factor_id: name, ...}

    Usage::

        zoo = AlphaZoo()
        fid = zoo.register(AlphaFactor(
            name="close_minus_open",
            description="Intraday range proxy",
            source_code='bars["close"] - bars["open"]',
            author="starter_set",
        ))
        series = zoo.compute(fid, bars_df)

    Both gates run synchronously inside :meth:`register`.  Pass only on
    clean source code.

    Args:
        base_dir: Directory for JSONL and registry JSON files.
                  Defaults to ``~/.hermes/quant/factors/``.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir is not None else _DEFAULT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._dir / "alpha_zoo.jsonl"
        self._registry_path = self._dir / "registry.json"
        # In-memory cache: factor_id -> AlphaFactor
        self._cache: dict[str, AlphaFactor] = {}
        self._load_existing()

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """Populate in-memory cache from JSONL on disk."""
        if not self._jsonl_path.exists():
            return
        with open(self._jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # Strip schema_version envelope if present
                    if "schema_version" in record and "factor" in record:
                        record = record["factor"]
                    factor = AlphaFactor.model_validate(record)
                    self._cache[factor.factor_id] = factor
                except Exception as exc:  # noqa: BLE001
                    logger.warning("AlphaZoo: skipping malformed line: %s", exc)

    def _append_record(self, factor: AlphaFactor) -> None:
        """Append a new factor to the JSONL file and update registry.json."""
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "factor": factor.model_dump(),
        }
        with open(self._jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope) + "\n")

        # Rebuild summary registry
        index = {fid: f.name for fid, f in self._cache.items()}
        with open(self._registry_path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2)

    # ------------------------------------------------------------------
    # Gate helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_purity_gate(factor: AlphaFactor) -> None:
        result = check_factor_purity(factor.source_code)
        if not result.passes:
            first = result.violations[0]
            raise PurityViolation(
                f"Factor {factor.name!r} failed AST purity gate: "
                f"{first['kind']} — {first['name']!r} at line {first['line']}",
                violation_kind=first["kind"],
                violations=result.violations,
            )

    @staticmethod
    def _run_lookahead_gate(factor: AlphaFactor) -> None:
        result = check_no_lookahead(factor.source_code)
        if not result.passes:
            first = result.suspicions[0]
            raise LookaheadDetected(
                f"Factor {factor.name!r} failed lookahead sentinel: "
                f"{first['kind']} — {first['detail']} at line {first['line']}",
                violation_kind=first["kind"],
                suspicions=result.suspicions,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, factor: AlphaFactor) -> str:
        """Validate, gate-check, and register *factor*.

        Runs the AST purity gate and lookahead sentinel before accepting.
        The registry is APPEND-ONLY; re-registering the same factor_id
        logs a warning and overwrites the in-memory cache but appends to JSONL.

        Args:
            factor: The :class:`AlphaFactor` to register.

        Returns:
            The ``factor_id`` assigned to (or already held by) the factor.

        Raises:
            PurityViolation:   If the AST purity gate rejects the source.
            LookaheadDetected: If the lookahead sentinel rejects the source.
        """
        self._run_purity_gate(factor)
        self._run_lookahead_gate(factor)

        if factor.factor_id in self._cache:
            logger.warning(
                "AlphaZoo: factor %r already registered; appending new version",
                factor.factor_id,
            )

        self._cache[factor.factor_id] = factor
        self._append_record(factor)
        logger.debug("AlphaZoo: registered factor %r (%s)", factor.factor_id, factor.name)
        return factor.factor_id

    def read(self, factor_id: str) -> AlphaFactor | None:
        """Retrieve a factor by ID from the in-memory cache.

        Returns None if not found.
        """
        return self._cache.get(factor_id)

    def list_all(self) -> list[AlphaFactor]:
        """Return all registered factors as a list (unordered)."""
        return list(self._cache.values())

    def compute(
        self,
        factor_id: str,
        bars: pd.DataFrame,
        **params: Any,
    ) -> pd.Series:
        """Evaluate a registered factor on *bars*.

        The factor is executed in a sandboxed scope::

            scope = {"pd": pd, "np": np, "bars": bars, "params": params}
            eval(source_code, {"__builtins__": {}}, scope)

        The ``__builtins__`` is stripped to prevent escape from sandbox.
        ``pd``, ``np``, and ``bars`` are explicitly provided.

        Args:
            factor_id: The factor to evaluate.
            bars:      OHLCV DataFrame with at least a DatetimeIndex.
            **params:  Runtime parameter overrides merged into ``params``.

        Returns:
            A ``pd.Series`` with the same index as *bars*.

        Raises:
            KeyError:            If *factor_id* is not registered.
            FactorExecutionError: If the factor expression raises at runtime.
        """
        factor = self._cache.get(factor_id)
        if factor is None:
            raise KeyError(f"Factor {factor_id!r} not found in registry")

        merged_params = {**factor.params, **params}
        # Safe builtins: numeric types + math helpers needed by pure expressions.
        # We explicitly list them rather than passing all __builtins__ to keep
        # the sandbox as tight as possible.
        _safe_builtins = {
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "sum": sum,
            "len": len,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "list": list,
            "tuple": tuple,
            "dict": dict,
            "set": set,
            "bool": bool,
            "int": int,
            "float": float,
            "str": str,
            "isinstance": isinstance,
            "type": type,
            "print": print,  # harmless for debugging
        }
        scope: dict[str, Any] = {
            "pd": pd,
            "np": np,
            "bars": bars,
            "params": merged_params,
        }
        try:
            result = eval(  # noqa: S307  (intentional sandboxed eval)
                factor.source_code,
                {"__builtins__": _safe_builtins},
                scope,
            )
        except Exception as exc:  # noqa: BLE001
            raise FactorExecutionError(factor_id, exc) from exc

        if not isinstance(result, pd.Series):
            result = pd.Series(result, index=bars.index)
        return result

    # ------------------------------------------------------------------
    # Append-only guard
    # ------------------------------------------------------------------

    def truncate(self, *_: Any, **__: Any) -> None:
        """Blocked — the registry is append-only."""
        raise AppendOnlyViolation(
            "AlphaZoo is append-only. truncate() is not permitted."
        )

    def update(self, *_: Any, **__: Any) -> None:
        """Blocked — the registry is append-only."""
        raise AppendOnlyViolation(
            "AlphaZoo is append-only. update() is not permitted. "
            "Register a new version with an incremented version field instead."
        )
