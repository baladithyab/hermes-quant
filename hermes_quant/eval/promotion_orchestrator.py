"""hermes_quant.eval.promotion_orchestrator — PromotionOrchestrator (ADR-0052).

Wires :class:`PromotionGate` to operational use via a full run-and-record
lifecycle:

    harness.run() → STOCKBENCHResult
        → gate.check()  → PromotionDecision
        → PromotionRecord (persisted to promotion_decisions.jsonl)

The orchestrator is a DECISION SUPPORT layer. It does not modify hypothesis
status — that is an explicit operator action using :class:`HypothesisRegistry`
after reviewing the emitted :class:`PromotionRecord`.

Storage
-------
~/.hermes/quant/research/promotion_decisions.jsonl  (append-only, schema_version=1)

Append-only enforcement
-----------------------
Same pattern as ``run_cards.jsonl`` and ``decisions.jsonl``: truncate() and
update() raise :class:`AppendOnlyViolation` from
:mod:`hermes_quant.research.hypothesis`.

References
----------
ADR-0052  — Promotion Orchestrator and Cron
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hermes_quant.eval.promotion_gate import PromotionDecision, PromotionGate
from hermes_quant.eval.stockbench import STOCKBENCHHarness, STOCKBENCHResult
from hermes_quant.research.hypothesis import AppendOnlyViolation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
RESEARCH_HOME = QUANT_HOME / "research"
PROMOTION_LOG_PATH = RESEARCH_HOME / "promotion_decisions.jsonl"

CURRENT_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Record ID helper
# ---------------------------------------------------------------------------


def _make_record_id() -> str:
    """Return a short unique record ID, e.g. ``prom_a1b2c3d4``."""
    return f"prom_{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# PromotionRecord — Pydantic v2 model
# ---------------------------------------------------------------------------


class PromotionRecord(BaseModel):
    """Immutable evidence artifact produced by :class:`PromotionOrchestrator`.

    Attributes
    ----------
    record_id:
        Auto-generated unique identifier (``prom_<8-hex-chars>``).
    hypothesis_id:
        Optional reference to a registered hypothesis in
        :class:`~hermes_quant.research.hypothesis.HypothesisRegistry`.
        Operators use this to look up the hypothesis and call
        ``HypothesisRegistry.update_status('validated' | 'falsified')``
        after reviewing the decision — the orchestrator does NOT do this
        automatically (see ADR-0052 §Operator Workflow).
    strategy_name:
        Human-readable name of the strategy under evaluation.
    window_start:
        First date of the STOCKBENCH evaluation window.
    window_end:
        Last date of the STOCKBENCH evaluation window.
    stockbench_result_summary:
        Condensed summary of :class:`~hermes_quant.eval.stockbench.STOCKBENCHResult`
        (max 20 keys — heavy ``metadata`` sub-fields are excluded).
    decision:
        Serialised :class:`~hermes_quant.eval.promotion_gate.PromotionDecision`
        (promote, reasons, suggested_action).
    recorded_at:
        ISO-8601 UTC timestamp of record creation.
    recorded_by:
        Identity string for audit trail (default ``"system"``).
    schema_version:
        Always 1 — bump when the model changes.
    """

    model_config = {"extra": "forbid"}

    record_id: str = Field(default_factory=_make_record_id)
    hypothesis_id: str | None = None
    strategy_name: str
    window_start: date
    window_end: date
    stockbench_result_summary: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any]
    # OUT-OF-SAMPLE fold-rate the decision was made on (seed 3767). None when no
    # walk-forward evidence was supplied (in-sample-only decision). Recorded so an
    # operator can audit WHY a strong-in-sample candidate was held. Optional with a
    # default → old rows (which lack the key) still deserialize at schema_version 1.
    oos_fold_rate: float | None = None
    recorded_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    recorded_by: str = "system"
    schema_version: int = 1

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_result_and_decision(
        cls,
        *,
        result: STOCKBENCHResult,
        decision: PromotionDecision,
        strategy_name: str,
        hypothesis_id: str | None = None,
        recorded_by: str = "system",
        oos_fold_rate: float | None = None,
    ) -> "PromotionRecord":
        """Build a record from a harness result + gate decision."""
        summary = _summarise_result(result)
        return cls(
            hypothesis_id=hypothesis_id,
            strategy_name=strategy_name,
            window_start=result.window_start,
            window_end=result.window_end,
            stockbench_result_summary=summary,
            decision={
                "promote": decision.promote,
                "reasons": list(decision.reasons),
                "suggested_action": decision.suggested_action,
            },
            oos_fold_rate=oos_fold_rate,
            recorded_by=recorded_by,
        )


# ---------------------------------------------------------------------------
# Result summariser (≤ 20 keys, no deep metadata)
# ---------------------------------------------------------------------------


def _summarise_result(result: STOCKBENCHResult) -> dict[str, Any]:
    """Return a flat, ≤ 20-key summary dict from a STOCKBENCHResult."""
    return {
        "universe": result.universe,
        "window_start": result.window_start.isoformat(),
        "window_end": result.window_end.isoformat(),
        "benchmark": result.benchmark,
        "cumulative_return": round(result.cumulative_return, 6),
        "max_drawdown": round(result.max_drawdown, 6),
        "sortino": round(result.sortino, 6) if result.sortino == result.sortino else None,  # NaN → None
        "n_decisions": result.n_decisions,
        "decisions_per_day_avg": round(result.decisions_per_day_avg, 6),
        "vs_buyhold_alpha": round(result.vs_buyhold_alpha, 6),
        "contamination_guard_fired": result.contamination_guard_fired,
        "buyhold_cumulative_return": round(
            result.metadata.get("buyhold_cumulative_return", 0.0), 6
        ),
    }


# ---------------------------------------------------------------------------
# PromotionLog — append-only JSONL wrapper
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _append_row(path: Path, row: dict[str, Any]) -> None:
    """Thread-safe append of a single JSON line to *path*."""
    line = json.dumps(row, default=str) + "\n"
    with _write_lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


class PromotionLog:
    """Append-only log wrapping ``~/.hermes/quant/research/promotion_decisions.jsonl``.

    Parameters
    ----------
    path:
        Override the default path (useful in tests via ``tmp_path`` fixtures).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or PROMOTION_LOG_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def record(self, promotion_record: PromotionRecord) -> str:
        """Append a PromotionRecord to the log; return its ``record_id``."""
        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "promotion_record",
            **promotion_record.model_dump(),
        }
        # Normalise date fields to ISO strings for JSON serialisation
        row["window_start"] = str(promotion_record.window_start)
        row["window_end"] = str(promotion_record.window_end)

        _append_row(self._path, row)
        logger.info(
            "promotion-log: recorded %s strategy=%s promote=%s",
            promotion_record.record_id,
            promotion_record.strategy_name,
            promotion_record.decision.get("promote"),
        )
        return promotion_record.record_id

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def read(self, record_id: str) -> PromotionRecord | None:
        """Return a PromotionRecord by record_id, or None if not found."""
        for row in self._iter_rows():
            if row.get("record_id") == record_id:
                return _row_to_record(row)
        return None

    def read_all(self) -> list[PromotionRecord]:
        """Return all PromotionRecords in insertion order."""
        results: list[PromotionRecord] = []
        for row in self._iter_rows():
            rec = _row_to_record(row)
            if rec is not None:
                results.append(rec)
        return results

    def read_for_strategy(self, strategy_name: str) -> list[PromotionRecord]:
        """Return all PromotionRecords for a given strategy name."""
        results: list[PromotionRecord] = []
        for row in self._iter_rows():
            if row.get("strategy_name") == strategy_name:
                rec = _row_to_record(row)
                if rec is not None:
                    results.append(rec)
        return results

    # ------------------------------------------------------------------
    # Append-only enforcement
    # ------------------------------------------------------------------

    def truncate(self) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "promotion_decisions.jsonl is append-only; truncate() is not supported"
        )

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Forbidden — append-only log."""
        raise AppendOnlyViolation(
            "promotion_decisions.jsonl is append-only; update() is not supported"
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "promotion-log: skipping malformed line in %s", self._path
                    )
                    continue
                if not isinstance(obj, dict):
                    logger.warning(
                        "promotion-log: skipping non-dict line (type=%s) in %s",
                        type(obj).__name__,
                        self._path,
                    )
                    continue
                yield obj


def _row_to_record(row: dict[str, Any]) -> PromotionRecord | None:
    """Deserialise a JSONL row back to a PromotionRecord, or None on error."""
    try:
        data = dict(row)
        # Strip envelope keys not part of the model
        data.pop("schema_version", None)
        data.pop("kind", None)
        # Coerce date fields
        for field_name in ("window_start", "window_end"):
            if isinstance(data.get(field_name), str):
                data[field_name] = date.fromisoformat(data[field_name])
        return PromotionRecord(**data)
    except Exception:
        logger.exception("promotion-log: failed to deserialise row")
        return None


# ---------------------------------------------------------------------------
# PromotionOrchestrator
# ---------------------------------------------------------------------------


class PromotionOrchestrator:
    """Orchestrate the full promotion-evaluation lifecycle.

    Parameters
    ----------
    gate:
        :class:`PromotionGate` instance.  Defaults to ``PromotionGate()``
        with standard thresholds.
    log:
        :class:`PromotionLog` instance.  Defaults to the standard path.
    harness:
        :class:`STOCKBENCHHarness` instance.  Defaults to a harness with
        ``strict_contamination=True``.

    Usage
    -----
    .. code-block:: python

        from hermes_quant.eval.promotion_orchestrator import PromotionOrchestrator
        from hermes_quant.eval.stockbench import _BuyAndHoldStrategy

        orch = PromotionOrchestrator()
        record = orch.run(
            strategy=_BuyAndHoldStrategy(),
            universe=["AAPL", "MSFT"],
            window_start=date(2025, 6, 1),
            window_end=date(2025, 8, 31),
            hypothesis_id="hyp_AAPL_20250601_abc123",
            auto_record=True,
        )
        print(record.decision)

    Note on hypothesis status
    -------------------------
    The orchestrator does NOT transition hypothesis status.  That is an
    explicit operator action: after reviewing the emitted PromotionRecord,
    an operator calls ``HypothesisRegistry.update_status('validated')`` or
    ``HypothesisRegistry.update_status('falsified')`` (see ADR-0052 §Operator
    Workflow).
    """

    def __init__(
        self,
        gate: PromotionGate | None = None,
        log: PromotionLog | None = None,
        harness: STOCKBENCHHarness | None = None,
    ) -> None:
        self.gate: PromotionGate = gate or PromotionGate()
        self.log: PromotionLog = log or PromotionLog()
        self.harness: STOCKBENCHHarness = harness or STOCKBENCHHarness()

    def run(
        self,
        *,
        strategy: Any,
        universe: list[str],
        window_start: date,
        window_end: date,
        hypothesis_id: str | None = None,
        strategy_name: str | None = None,
        auto_record: bool = True,
        recorded_by: str = "system",
        oos_fold_rate: float | None = None,
    ) -> PromotionRecord:
        """Run harness → gate → record; return the PromotionRecord.

        Parameters
        ----------
        strategy:
            Any object implementing :class:`~hermes_quant.eval.stockbench.StrategyProtocol`
            (i.e. has a ``decide(ticker, as_of, price_history) -> float`` method).
        universe:
            List of ticker symbols to evaluate.
        window_start:
            First date of the evaluation window (must be ≥ knowledge cutoff).
        window_end:
            Last date of the evaluation window.
        hypothesis_id:
            Optional reference to a registered hypothesis.  The operator uses
            this to locate the hypothesis in :class:`HypothesisRegistry` and
            decide whether to update its status.
        strategy_name:
            Human-readable strategy label for the record.  Defaults to the
            class name of *strategy*.
        auto_record:
            When True (default), append the PromotionRecord to the log.
        recorded_by:
            Identity string for audit trail (default ``"system"``).
        oos_fold_rate:
            Optional out-of-sample walk-forward fold-rate (seed 3767) — the
            fraction of folds whose excess-return beats buy-and-hold, from
            ``walk_forward_replay(...).positive_excess_fold_rate``. When supplied,
            it is forwarded to ``gate.check`` (the candidate must clear the gate's
            ``oos_fold_rate_floor`` IN ADDITION to the in-sample criteria) and
            recorded on the PromotionRecord for audit. Default None reproduces the
            in-sample-only behavior. The orchestrator does NOT compute this itself
            (it has no bars) — the walk-forward caller passes it in.

        Returns
        -------
        PromotionRecord
            Fully populated record.  If ``auto_record=True`` the record is
            also persisted to ``promotion_decisions.jsonl``.
        """
        eff_name = strategy_name or type(strategy).__name__

        logger.info(
            "promotion-orchestrator: running %s universe=%s window=%s:%s",
            eff_name,
            universe,
            window_start,
            window_end,
        )

        # Step 1 — run STOCKBENCH harness
        result: STOCKBENCHResult = self.harness.run(
            strategy=strategy,
            universe=universe,
            window_start=window_start,
            window_end=window_end,
        )

        # Step 2 — evaluate through PromotionGate. Only forward oos_fold_rate when
        # supplied, so the default path calls gate.check(result) exactly as before
        # (preserves compatibility with gates whose check() predates seed 3767).
        if oos_fold_rate is None:
            decision: PromotionDecision = self.gate.check(result)
        else:
            decision = self.gate.check(result, oos_fold_rate=oos_fold_rate)

        logger.info(
            "promotion-orchestrator: gate decision promote=%s reasons=%d oos_fold_rate=%s",
            decision.promote,
            len(decision.reasons),
            oos_fold_rate,
        )

        # Step 3 — build PromotionRecord
        record = PromotionRecord.from_result_and_decision(
            result=result,
            decision=decision,
            strategy_name=eff_name,
            hypothesis_id=hypothesis_id,
            recorded_by=recorded_by,
            oos_fold_rate=oos_fold_rate,
        )

        # Step 4 — persist if requested
        if auto_record:
            self.log.record(record)

        return record
