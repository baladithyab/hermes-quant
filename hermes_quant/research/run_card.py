"""hermes_quant.research.run_card — RunCard model + append-only log (ADR-0048).

A RunCard is emitted AFTER a strategy run to document:
  - Which hypothesis was tested (hypothesis_id reference).
  - Strategy configuration (hashed for reproducibility).
  - Full backtest metrics.
  - Paths to artifact files (walk-forward log, audit log slice, reflections slice).
  - Verdict: validated | falsified | inconclusive.
  - Reasons supporting the verdict.

Together with the Hypothesis registration, the RunCard prevents post-hoc
rationalisation: the success/falsification criteria were declared BEFORE the
run, and the RunCard just records what happened.

Storage
-------
~/.hermes/quant/research/run_cards.jsonl

schema_version=1 on every row.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
RESEARCH_HOME = QUANT_HOME / "research"
RUN_CARDS_PATH = RESEARCH_HOME / "run_cards.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Errors — re-use AppendOnlyViolation from hypothesis module
# ---------------------------------------------------------------------------

from hermes_quant.research.hypothesis import AppendOnlyViolation  # noqa: E402

# ---------------------------------------------------------------------------
# RunCard model
# ---------------------------------------------------------------------------


class RunCard(BaseModel):
    """Post-run evidence artifact linking a backtest to a registered hypothesis.

    Fields
    ------
    run_id:
        Unique run identifier. Auto-generated as
        ``f"run_{hypothesis_id}_{ISO8601-min}"`` when not provided.
    hypothesis_id:
        Must reference a hypothesis registered in HypothesisRegistry.
    started_at:
        ISO-8601 UTC timestamp when the run started.
    ended_at:
        ISO-8601 UTC timestamp when the run ended.
    strategy_name:
        Human-readable strategy name.
    strategy_config_hash:
        SHA-256 of the serialised strategy config. Used for exact
        reproducibility (re-running the same hash must yield identical metrics
        given the same data).
    universe:
        List of ticker symbols used in the run.
    window_start:
        Holdout window start date.
    window_end:
        Holdout window end date.
    contamination_guard_fired:
        True if the WalkForwardEngine raised LookaheadViolation during the run
        (should always be False in production; True means the run is tainted).
    metrics:
        Backtest metrics dict. Required keys: sharpe, sortino, max_drawdown,
        vs_buyhold_alpha, n_decisions, total_return.
    artifacts:
        Paths to supporting evidence files, e.g.:
        {"backtest_log": "...", "audit_log": "...", "reflections": "..."}.
    verdict:
        Post-run verdict: "validated" | "falsified" | "inconclusive".
    verdict_reasons:
        Up to 10 reasons (each max 512 chars) explaining the verdict, one per
        evaluated criterion.
    """

    run_id: str = Field(default="")
    hypothesis_id: str
    started_at: str
    ended_at: str
    strategy_name: str
    strategy_config_hash: str
    universe: list[str]
    window_start: date
    window_end: date
    contamination_guard_fired: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    verdict: Literal["validated", "falsified", "inconclusive"]
    verdict_reasons: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("verdict_reasons")
    @classmethod
    def _check_verdict_reasons(cls, v: list[str]) -> list[str]:
        if len(v) > 10:
            raise ValueError("verdict_reasons may have at most 10 entries")
        for i, item in enumerate(v):
            if len(item) > 512:
                raise ValueError(
                    f"verdict_reasons[{i}] exceeds max_length=512 ({len(item)} chars)"
                )
        return v

    @field_validator("metrics")
    @classmethod
    def _check_metrics(cls, v: dict[str, float]) -> dict[str, float]:
        """All values must be numeric (float/int)."""
        for k, val in v.items():
            if not isinstance(val, (int, float)):
                raise ValueError(f"metrics[{k!r}] must be numeric, got {type(val).__name__}")
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _append_row(path: Path, row: dict[str, Any]) -> None:
    line = json.dumps(row, sort_keys=True, default=str) + "\n"
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# RunCardLog
# ---------------------------------------------------------------------------


class RunCardLog:
    """Append-only log wrapping ~/.hermes/quant/research/run_cards.jsonl.

    Parameters
    ----------
    path:
        Override the default path (useful in tests via tmp_path fixtures).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or RUN_CARDS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def record(self, run_card: RunCard) -> str:
        """Append a RunCard to the log; return its run_id.

        If ``run_card.run_id`` is empty, a unique ID is generated.
        """
        from datetime import UTC, datetime

        run_id = run_card.run_id or _make_run_id(run_card.hypothesis_id)

        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "run_card",
            **run_card.model_dump(),
        }
        # Normalise date fields to ISO strings for JSON serialisation
        row["window_start"] = str(run_card.window_start)
        row["window_end"] = str(run_card.window_end)
        row["run_id"] = run_id

        _append_row(self._path, row)
        logger.info(
            "run-card-log: recorded %s hyp=%s verdict=%s",
            run_id,
            run_card.hypothesis_id,
            run_card.verdict,
        )
        return run_id

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def read(self, run_id: str) -> RunCard | None:
        """Return a RunCard by run_id, or None if not found."""
        for row in self._iter_rows():
            if row.get("run_id") == run_id:
                return _row_to_run_card(row)
        return None

    def read_for_hypothesis(self, hypothesis_id: str) -> list[RunCard]:
        """Return all RunCards linked to a hypothesis_id, in insertion order."""
        results: list[RunCard] = []
        for row in self._iter_rows():
            if row.get("hypothesis_id") == hypothesis_id:
                card = _row_to_run_card(row)
                if card is not None:
                    results.append(card)
        return results

    # ------------------------------------------------------------------
    # Append-only enforcement
    # ------------------------------------------------------------------

    def truncate(self) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "run_cards.jsonl is append-only; truncate() is not supported"
        )

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "run_cards.jsonl is append-only; update() is not supported"
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
                    logger.warning("run-card-log: skipping malformed row")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_run_id(hypothesis_id: str) -> str:
    """Generate a run ID: run_{hypothesis_id}_{ISO8601-min}."""
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    return f"run_{hypothesis_id}_{ts}"


def _row_to_run_card(row: dict[str, Any]) -> RunCard | None:
    """Deserialise a JSONL row into a RunCard, handling date fields."""
    try:
        data = {k: v for k, v in row.items() if k not in ("schema_version", "kind")}
        # Parse date strings back to date objects
        for field in ("window_start", "window_end"):
            if isinstance(data.get(field), str):
                from datetime import date
                data[field] = date.fromisoformat(data[field])
        return RunCard(**data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run-card-log: failed to deserialise RunCard: %s", exc)
        return None
