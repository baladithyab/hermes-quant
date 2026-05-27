"""hermes_quant.research.hypothesis — Hypothesis model + append-only registry (ADR-0048).

Design
------
Every strategy variant or alpha factor is registered as a HYPOTHESIS *before* it runs.
The hypothesis captures:
  - The falsifiable claim and its null hypothesis.
  - Concrete success + falsification criteria (evaluated post-backtest).
  - Experiment design description.
  - Scope (universe, time window, env vars, etc.).

Status transitions are append-only NEW rows (kind='status_change').
The original registration row is NEVER mutated — same contract as audit_log.py and
decisions.jsonl (ADR-0031 / ADR-0042).

Storage
-------
~/.hermes/quant/research/hypotheses.jsonl

  Row kinds:
    "hypothesis"     : the initial registration row.
    "status_change"  : a status transition with optional evidence dict.

schema_version=1 on every row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
RESEARCH_HOME = QUANT_HOME / "research"
HYPOTHESES_PATH = RESEARCH_HOME / "hypotheses.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppendOnlyViolation(Exception):
    """Raised on attempts to truncate or update an append-only registry."""


class HypothesisNotFound(KeyError):
    """Raised when a hypothesis_id does not exist in the registry."""


class HypothesisIDCollision(ValueError):
    """Raised when registering a hypothesis_id that already exists."""


class InvalidStatusTransition(ValueError):
    """Raised when a requested status transition is not allowed."""


# ---------------------------------------------------------------------------
# Status transition graph
# ---------------------------------------------------------------------------

# open → running → {validated | falsified | abandoned}
# abandoned is reachable from any non-terminal state.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "open": {"running", "abandoned"},
    "running": {"validated", "falsified", "abandoned"},
    "validated": set(),  # terminal
    "falsified": set(),  # terminal
    "abandoned": set(),  # terminal
}

_TERMINAL_STATUSES = {"validated", "falsified", "abandoned"}

# ---------------------------------------------------------------------------
# Hypothesis model
# ---------------------------------------------------------------------------


class Hypothesis(BaseModel):
    """A falsifiable research hypothesis registered before any backtest.

    Fields
    ------
    hypothesis_id:
        Unique ID. Auto-generated as ``f"hyp_{ticker}_{YYYYMMDD}_{6-char-hex}"``
        when not provided. Caller may supply their own.
    created_at:
        ISO-8601 UTC timestamp of creation.
    author:
        Who registered this hypothesis, e.g. "aria", "codeseys", or
        a model name like "claude-sonnet-4.6".
    claim:
        The falsifiable positive claim, e.g.
        "Adding sentiment analyst will increase Sharpe by >=0.10 over 6mo backtest".
    null_hypothesis:
        The null to be rejected, e.g.
        "Sentiment analyst makes no difference (alpha <= 0)".
    success_criteria:
        Concrete pass conditions evaluated post-backtest against the metrics dict.
        Up to 5 entries, each max 256 chars.
        Expression syntax: ``"sharpe >= 0.5"``, ``"vs_buyhold_alpha > 0.0"``.
        These are eval()'d in a restricted scope — see ADR-0048 §Safety.
    falsification_criteria:
        Concrete fail conditions. Up to 5 entries.
    experiment_design:
        Walk-forward backtest, A/B comparison, etc. (max 2 048 chars).
    duration_target_days:
        Target experiment duration in calendar days [1 .. 730].
    scope:
        Flexible dict of experiment parameters: universe, time_window,
        env_vars, etc.  Max 10 keys.
    related_adrs:
        List of related ADR identifiers, e.g. ["ADR-0044", "ADR-0046"].
    status:
        Current lifecycle status. Valid transitions:
        open → running → {validated | falsified | abandoned}.
        Status transitions are append-only new rows — NEVER mutations.
    """

    hypothesis_id: str = Field(default="")
    created_at: str = Field(default="")
    author: str
    claim: str = Field(max_length=512)
    null_hypothesis: str = Field(max_length=512)
    success_criteria: list[str] = Field(default_factory=list)
    falsification_criteria: list[str] = Field(default_factory=list)
    experiment_design: str = Field(default="", max_length=2048)
    duration_target_days: int = Field(ge=1, le=730)
    scope: dict[str, Any] = Field(default_factory=dict)
    related_adrs: list[str] = Field(default_factory=list)
    status: Literal["open", "running", "validated", "falsified", "abandoned"] = "open"

    model_config = {"extra": "forbid"}

    @field_validator("success_criteria")
    @classmethod
    def _check_success_criteria(cls, v: list[str]) -> list[str]:
        if len(v) > 5:
            raise ValueError("success_criteria may have at most 5 entries")
        for i, item in enumerate(v):
            if len(item) > 256:
                raise ValueError(
                    f"success_criteria[{i}] exceeds max_length=256 ({len(item)} chars)"
                )
        return v

    @field_validator("falsification_criteria")
    @classmethod
    def _check_falsification_criteria(cls, v: list[str]) -> list[str]:
        if len(v) > 5:
            raise ValueError("falsification_criteria may have at most 5 entries")
        for i, item in enumerate(v):
            if len(item) > 256:
                raise ValueError(
                    f"falsification_criteria[{i}] exceeds max_length=256 ({len(item)} chars)"
                )
        return v

    @field_validator("scope")
    @classmethod
    def _check_scope_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 10:
            raise ValueError("scope may have at most 10 keys")
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_hypothesis_id(ticker: str = "UNKNOWN") -> str:
    """Generate a unique hypothesis ID: hyp_{TICKER}_{YYYYMMDD}_{6-hex}."""
    ts = datetime.now(UTC).strftime("%Y%m%d")
    h = hashlib.sha1(f"{ticker}{_now_iso()}".encode()).hexdigest()[:6]
    return f"hyp_{ticker.upper()}_{ts}_{h}"


def _append_row(path: Path, row: dict[str, Any]) -> None:
    """Append a JSON row to the JSONL file with fsync."""
    line = json.dumps(row, sort_keys=True, default=str) + "\n"
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# HypothesisRegistry
# ---------------------------------------------------------------------------


class HypothesisRegistry:
    """Append-only registry wrapping ~/.hermes/quant/research/hypotheses.jsonl.

    Every write is a NEW row — the registry never mutates existing rows.
    Status transitions are recorded as separate ``kind="status_change"`` rows
    so the complete event history is always preserved.

    Parameters
    ----------
    path:
        Override the default path (useful in tests via tmp_path fixtures).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or HYPOTHESES_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def register(self, hypothesis: Hypothesis) -> str:
        """Register a new hypothesis; return its hypothesis_id.

        If ``hypothesis.hypothesis_id`` is empty, a unique ID is generated
        from the first ticker in ``hypothesis.scope`` (or "UNKNOWN").

        Raises
        ------
        HypothesisIDCollision:
            If a hypothesis with the same ID already exists in the registry.
        """
        ticker = _extract_ticker(hypothesis)
        hyp_id = hypothesis.hypothesis_id or _make_hypothesis_id(ticker)
        created = hypothesis.created_at or _now_iso()

        # Collision check
        existing = self.read(hyp_id)
        if existing is not None:
            raise HypothesisIDCollision(
                f"hypothesis_id {hyp_id!r} already exists in the registry"
            )

        # Build a canonical copy with filled-in defaults
        hyp_dict = hypothesis.model_dump()
        hyp_dict["hypothesis_id"] = hyp_id
        hyp_dict["created_at"] = created

        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "hypothesis",
            **hyp_dict,
        }
        _append_row(self._path, row)
        logger.info("hypothesis-registry: registered %s author=%s", hyp_id, hypothesis.author)
        return hyp_id

    def update_status(
        self,
        hypothesis_id: str,
        new_status: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Append a status_change row; never mutates the original registration.

        Valid transitions:
          open → running → {validated | falsified | abandoned}
          open → abandoned
          running → abandoned

        Raises
        ------
        HypothesisNotFound:
            If hypothesis_id does not exist.
        InvalidStatusTransition:
            If the requested transition is not permitted.
        """
        hyp = self.read(hypothesis_id)
        if hyp is None:
            raise HypothesisNotFound(hypothesis_id)

        current = hyp.status
        allowed = _VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise InvalidStatusTransition(
                f"Cannot transition {hypothesis_id!r} from {current!r} to {new_status!r}. "
                f"Allowed: {sorted(allowed) or 'none (terminal state)'}"
            )

        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "status_change",
            "hypothesis_id": hypothesis_id,
            "previous_status": current,
            "new_status": new_status,
            "asof": _now_iso(),
            "evidence": evidence or {},
        }
        _append_row(self._path, row)
        logger.info(
            "hypothesis-registry: %s %s → %s", hypothesis_id, current, new_status
        )

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def read(self, hypothesis_id: str) -> Hypothesis | None:
        """Return the current state of a hypothesis, or None if not found.

        The state is materialized by replaying the event log:
          1. Find the registration row.
          2. Apply all status_change rows in order.
        """
        registration: dict[str, Any] | None = None
        latest_status: str | None = None

        for row in self._iter_rows():
            if row.get("kind") == "hypothesis" and row.get("hypothesis_id") == hypothesis_id:
                registration = row
                latest_status = row.get("status", "open")
            elif (
                row.get("kind") == "status_change"
                and row.get("hypothesis_id") == hypothesis_id
            ):
                latest_status = row.get("new_status", latest_status)

        if registration is None:
            return None

        # Reconstruct with applied status
        data = {k: v for k, v in registration.items() if k not in ("schema_version", "kind")}
        data["status"] = latest_status or data.get("status", "open")
        return Hypothesis(**data)

    def read_all_open(self) -> Iterator[Hypothesis]:
        """Yield all hypotheses with status='open'."""
        yield from self._read_by_status("open")

    def read_all_running(self) -> Iterator[Hypothesis]:
        """Yield all hypotheses with status='running'."""
        yield from self._read_by_status("running")

    def read_all_resolved(self) -> Iterator[Hypothesis]:
        """Yield all hypotheses with terminal status (validated|falsified|abandoned)."""
        seen_ids: set[str] = set()
        for row in self._iter_rows():
            if row.get("kind") != "hypothesis":
                continue
            hyp_id = row.get("hypothesis_id", "")
            if hyp_id in seen_ids:
                continue
            seen_ids.add(hyp_id)
            hyp = self.read(hyp_id)
            if hyp is not None and hyp.status in _TERMINAL_STATUSES:
                yield hyp

    # ------------------------------------------------------------------
    # Append-only enforcement
    # ------------------------------------------------------------------

    def truncate(self) -> None:
        """Forbidden — append-only registry."""
        raise AppendOnlyViolation(
            "hypotheses.jsonl is append-only; truncate() is not supported"
        )

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Forbidden — append-only registry."""
        raise AppendOnlyViolation(
            "hypotheses.jsonl is append-only; update() is not supported"
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with open(self._path) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("hypothesis-registry: skipping malformed row")

    def _read_by_status(self, status: str) -> Iterator[Hypothesis]:
        seen_ids: set[str] = set()
        for row in self._iter_rows():
            if row.get("kind") != "hypothesis":
                continue
            hyp_id = row.get("hypothesis_id", "")
            if hyp_id in seen_ids:
                continue
            seen_ids.add(hyp_id)
            hyp = self.read(hyp_id)
            if hyp is not None and hyp.status == status:
                yield hyp


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _extract_ticker(hypothesis: Hypothesis) -> str:
    """Extract a ticker from hypothesis.scope for ID generation."""
    scope = hypothesis.scope or {}
    universe = scope.get("universe", [])
    if isinstance(universe, list) and universe:
        return str(universe[0]).upper()
    ticker = scope.get("ticker", "")
    if ticker:
        return str(ticker).upper()
    return "UNKNOWN"
