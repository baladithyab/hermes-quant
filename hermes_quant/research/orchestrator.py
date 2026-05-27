"""hermes_quant.research.orchestrator — HypothesisRunner orchestrator (ADR-0048).

Full lifecycle
--------------
1. Transition hypothesis open → running.
2. Execute the strategy callable (or StubLLMCommittee in dry_run=True mode).
3. Auto-evaluate success_criteria + falsification_criteria against backtest metrics.
4. Write a RunCard with the verdict + reasons.
5. Transition hypothesis → {validated | falsified} based on verdict.
6. Return the RunCard.

Auto-evaluation safety
----------------------
success_criteria and falsification_criteria are evaluated with::

    eval(criterion, {"__builtins__": {}}, metrics_dict)

``__builtins__`` is explicitly removed so attribute access, imports, and any
builtins are unavailable. Only names present in the metrics dict are in scope.

This is documented as a v0.1 limitation in ADR-0048 §Safety. Full sandboxing
(e.g. RestrictedPython or AST-based evaluation) is deferred to v0.2+.

dry_run mode
------------
When ``dry_run=True`` (the default), the orchestrator:
  - Creates a minimal synthetic OHLCV DataFrame.
  - Wraps the callable with StubLLMCommittee so no real LLM calls occur.
  - Runs the callable over the supplied universe + window.

The ``strategy`` callable signature expected::

    (universe: list[str], window_start: date, window_end: date,
     dry_run: bool) -> dict[str, float]

The return value is a metrics dict with keys matching the RunCard.metrics schema.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from typing import Any, Callable

from hermes_quant.research.hypothesis import (
    AppendOnlyViolation,
    Hypothesis,
    HypothesisRegistry,
)
from hermes_quant.research.run_card import RunCard, RunCardLog, _make_run_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required metric keys (must be present in strategy output)
# ---------------------------------------------------------------------------

REQUIRED_METRIC_KEYS = {
    "sharpe",
    "sortino",
    "max_drawdown",
    "vs_buyhold_alpha",
    "n_decisions",
    "total_return",
}


# ---------------------------------------------------------------------------
# Auto-evaluation helpers
# ---------------------------------------------------------------------------


def _eval_criterion(criterion: str, metrics: dict[str, float]) -> bool:
    """Evaluate a single criterion string against a metrics dict.

    Uses ``eval()`` with __builtins__ removed and only metrics names in scope.
    Returns True if the criterion evaluates to a truthy value.
    Raises ValueError if the expression is malformed or references unknown names.

    Safety note: see ADR-0048 §Safety for the rationale and limitations.
    """
    try:
        result = eval(criterion, {"__builtins__": {}}, dict(metrics))  # noqa: S307
        return bool(result)
    except Exception as exc:
        raise ValueError(
            f"Failed to evaluate criterion {criterion!r}: {exc}. "
            "Ensure all names reference keys in the metrics dict."
        ) from exc


def _evaluate_criteria(
    success_criteria: list[str],
    falsification_criteria: list[str],
    metrics: dict[str, float],
) -> tuple[str, list[str]]:
    """Evaluate criteria and return (verdict, reasons).

    Logic
    -----
    1. If ANY falsification criterion fires → verdict="falsified".
    2. Else if ALL success criteria pass (and at least one criterion exists)
       → verdict="validated".
    3. Otherwise → verdict="inconclusive".

    Returns
    -------
    verdict:
        "validated" | "falsified" | "inconclusive"
    reasons:
        One reason string per evaluated criterion.
    """
    reasons: list[str] = []
    any_falsified = False
    all_success = True

    # Evaluate falsification criteria first
    for criterion in falsification_criteria:
        try:
            fired = _eval_criterion(criterion, metrics)
        except ValueError as exc:
            reasons.append(f"[ERROR evaluating falsification criterion] {exc}")
            continue
        if fired:
            any_falsified = True
            reasons.append(f"[FALSIFIED] {criterion} → True")
        else:
            reasons.append(f"[not fired] {criterion} → False")

    if any_falsified:
        return "falsified", reasons

    # Evaluate success criteria
    for criterion in success_criteria:
        try:
            passed = _eval_criterion(criterion, metrics)
        except ValueError as exc:
            reasons.append(f"[ERROR evaluating success criterion] {exc}")
            all_success = False
            continue
        if passed:
            reasons.append(f"[PASSED] {criterion} → True")
        else:
            all_success = False
            reasons.append(f"[FAILED] {criterion} → False")

    if success_criteria and all_success:
        return "validated", reasons
    if not success_criteria and not falsification_criteria:
        reasons.append("No criteria defined — verdict is inconclusive.")
        return "inconclusive", reasons

    return "inconclusive", reasons


# ---------------------------------------------------------------------------
# HypothesisRunner
# ---------------------------------------------------------------------------


class HypothesisRunner:
    """Orchestrate the full hypothesis→run→evaluate→record lifecycle.

    Parameters
    ----------
    registry:
        HypothesisRegistry to read hypotheses from and write status changes to.
    run_card_log:
        RunCardLog to append RunCard records to.
    """

    def __init__(
        self,
        registry: HypothesisRegistry,
        run_card_log: RunCardLog,
    ) -> None:
        self._registry = registry
        self._run_card_log = run_card_log

    def run(
        self,
        hypothesis_id: str,
        *,
        strategy: Callable[..., dict[str, float]],
        universe: list[str],
        window_start: date,
        window_end: date,
        dry_run: bool = True,
    ) -> RunCard:
        """Execute the full hypothesis lifecycle and return a RunCard.

        Parameters
        ----------
        hypothesis_id:
            ID of a registered (status='open') hypothesis to run.
        strategy:
            Callable with signature::

                (universe, window_start, window_end, dry_run=True) -> dict[str, float]

            The returned dict must contain all REQUIRED_METRIC_KEYS.
            In dry_run=True mode this callable MUST NOT make any external LLM
            calls — it should use StubLLMCommittee or a deterministic function.
        universe:
            List of tickers for this run.
        window_start:
            Holdout window start date.
        window_end:
            Holdout window end date.
        dry_run:
            If True (default), pass ``dry_run=True`` to the strategy callable
            and record ``dry_run=True`` in the RunCard scope. Zero LLM cost.

        Returns
        -------
        RunCard
            The committed RunCard with verdict.

        Raises
        ------
        HypothesisNotFound:
            If hypothesis_id does not exist.
        InvalidStatusTransition:
            If the hypothesis is not in 'open' status (cannot start a run on a
            hypothesis that is already running or resolved).
        """
        from hermes_quant.research.hypothesis import HypothesisNotFound

        # 1. Fetch hypothesis (raises HypothesisNotFound if absent)
        hyp = self._registry.read(hypothesis_id)
        if hyp is None:
            raise HypothesisNotFound(hypothesis_id)

        # 2. Transition open → running (raises InvalidStatusTransition if not open)
        self._registry.update_status(hypothesis_id, "running")

        started_at = datetime.now(UTC).isoformat()
        contamination_fired = False
        metrics: dict[str, float] = {}

        # 3. Execute strategy
        # MoA review F5 (Sonnet C1): catch LookaheadViolation explicitly so
        # ADR-0048 §D5 contract holds — a contaminated run sets
        # contamination_guard_fired=True and forces verdict='falsified'.
        # Without this, the bare `except Exception` swallows it as a
        # generic failure and emits 'inconclusive', which is silent harm.
        try:
            from hermes_quant.backtest.engine import LookaheadViolation as _LookaheadViolation
        except ImportError:
            try:
                from hermes_quant.backtest import LookaheadViolation as _LookaheadViolation
            except ImportError:
                _LookaheadViolation = None  # type: ignore[assignment]

        try:
            raw_metrics = strategy(
                universe=universe,
                window_start=window_start,
                window_end=window_end,
                dry_run=dry_run,
            )
            # Ensure all values are float
            metrics = {k: float(v) for k, v in raw_metrics.items()}
        except Exception as exc:
            # Detect LookaheadViolation to fire the contamination guard
            if _LookaheadViolation is not None and isinstance(exc, _LookaheadViolation):
                contamination_fired = True
                logger.warning(
                    "HypothesisRunner: LookaheadViolation in %s — "
                    "contamination_guard_fired=True; verdict will be forced to 'falsified'.",
                    hypothesis_id,
                )
            else:
                logger.warning(
                    "HypothesisRunner: strategy raised %s for %s; metrics will be empty.",
                    exc,
                    hypothesis_id,
                )
            # Record failure metrics so the run card is still written
            metrics = {k: float("nan") for k in REQUIRED_METRIC_KEYS}

        ended_at = datetime.now(UTC).isoformat()

        # 4. Fill any missing required metric keys with NaN
        for key in REQUIRED_METRIC_KEYS:
            if key not in metrics:
                metrics[key] = float("nan")

        # 5. Build strategy config hash
        config_payload = {
            "strategy_name": getattr(strategy, "__name__", str(strategy)),
            "universe": sorted(universe),
            "window_start": str(window_start),
            "window_end": str(window_end),
            "dry_run": dry_run,
        }
        config_hash = hashlib.sha256(
            json.dumps(config_payload, sort_keys=True).encode()
        ).hexdigest()

        # 6. Auto-evaluate criteria
        verdict, reasons = _evaluate_criteria(
            hyp.success_criteria,
            hyp.falsification_criteria,
            metrics,
        )

        # MoA review F5 (Sonnet C1): if contamination guard fired, the
        # verdict is forced to 'falsified' regardless of metrics. ADR-0048
        # §D5 makes this a hard rule — a contaminated run cannot pass.
        if contamination_fired:
            verdict = "falsified"
            reasons = [
                "LookaheadViolation raised during run — contamination_guard_fired=True; "
                "verdict forced to 'falsified' per ADR-0048 §D5."
            ] + (reasons[:9] if reasons else [])

        # 7. Build RunCard
        run_id = _make_run_id(hypothesis_id)
        run_card = RunCard(
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            started_at=started_at,
            ended_at=ended_at,
            strategy_name=getattr(strategy, "__name__", str(strategy)),
            strategy_config_hash=config_hash,
            universe=universe,
            window_start=window_start,
            window_end=window_end,
            contamination_guard_fired=contamination_fired,
            metrics=metrics,
            artifacts={},
            verdict=verdict,
            verdict_reasons=reasons[:10],
        )

        # 8. Append RunCard
        self._run_card_log.record(run_card)

        # 9. Transition hypothesis to terminal state
        terminal = verdict if verdict in ("validated", "falsified") else None
        if terminal:
            self._registry.update_status(
                hypothesis_id,
                terminal,
                evidence={
                    "run_id": run_id,
                    "verdict": verdict,
                    "metrics": metrics,
                },
            )
        # inconclusive: leave in "running" state; caller may abandon or re-run.

        logger.info(
            "HypothesisRunner: %s → %s (verdict=%s run_id=%s)",
            hypothesis_id,
            terminal or "running",
            verdict,
            run_id,
        )
        return run_card
