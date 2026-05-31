"""hermes_quant.research.research_loop — W6 quant-research driving loop (ADR-0080).

The INNER/OUTER rail made explicit (QuantAgent / FunSearch two-loop):

  INNER  (advisory plane, cheap judge): the candidate hypothesis (W3 output) and
         its LLM/committee strategy. Evidence only. Writes nothing live.
  OUTER  (standard-of-truth, immutable by this loop): the deterministic OOS
         backtest + the lookahead sentinel (orchestrator.py:313-318) + the
         PromotionGate (eval/promotion_gate.py). Only this path scores truth.

This module PROPOSES ONLY. It NEVER promotes to live and NEVER flips a flag.
Promotion to live influence remains an explicit operator action (ADR-0052;
promotion_orchestrator.py:354-360 — the orchestrator "does NOT transition
hypothesis status"). The cron produces RunCards + PromotionRecords; a human
reviews and (separately) promotes.

The ONLY way a candidate's registry status advances is the deterministic
auto-eval inside ``HypothesisRunner.run`` (orchestrator.py:303-353), which scores
the strategy's backtest metrics against the hypothesis's PRE-DECLARED
success/falsification criteria (external truth, ADR-0080 §D80.3.1). No LLM
self-score is ever read as truth. W6 adds NO alternate advancement path.

Advisory-plane surfaces written (ADR-0080 §D80.1) — and NOTHING else:
  - hypotheses.jsonl   (lifecycle status_change rows, via HypothesisRunner)
  - run_cards.jsonl    (RunCards, via RunCardLog)
  - promotion_decisions.jsonl (PromotionRecords, review-only, via PromotionLog)
  - research_loop.jsonl (NEW W6 cron audit log — one cycle row + per-candidate rows)

Surfaces this module imports NOTHING from for mutation (outside the loop,
immutable by it — ADR-0080 §D80.1 / capability-map §5):
  - the deterministic risk gate and the hard risk limits;
  - the discrete sizing ladder {0, ±0.05, ±0.10, ±0.15, ±0.20};
  - the kill-switch (halt_state.json — a separate process; read fail-closed via
    the cron wrapper, never written here);
  - promotion to live (the cron produces records; the operator promotes).

Flag: HERMES_QUANT_RESEARCH_LOOP (default-OFF). With the flag unset/!=1 the
off-state is byte-identical: ``run_cycle`` returns an empty summary, reads no
candidates, and writes nothing to any JSONL.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hermes_quant.research.hypothesis import Hypothesis, HypothesisRegistry
from hermes_quant.research.orchestrator import HypothesisRunner
from hermes_quant.research.run_card import RunCard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

QUANT_HOME = Path.home() / ".hermes" / "quant"
RESEARCH_HOME = QUANT_HOME / "research"
RESEARCH_LOOP_LOG_PATH = RESEARCH_HOME / "research_loop.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

# Default OOS window length when a candidate hypothesis does not pin its own
# window. Deliberately a RANGE-friendly default, never a tuned peak
# (ADR-0080 §D80.3.3 robustness-not-peak). The window is NEVER tuned to
# maximise pass-rate.
_DEFAULT_OOS_DAYS = 90
# Hard per-cycle cap so a flood of candidates cannot blow the LLM budget /
# runtime (ADR-0080 §D80.3.4 bounded).
_MAX_CANDIDATES_PER_CYCLE = 8


def flag_on() -> bool:
    """W6 master flag. Default-OFF.

    Copy of the repo idiom (cf. aggregators/llm_committee.py
    HERMES_QUANT_RESEARCH_DEBATE, autonomous.py HERMES_QUANT_PORTFOLIO_CAPS):
    default ``"0"`` → OFF when absent; strict ``== "1"`` → only the literal
    string ``"1"`` enables it.
    """
    return os.environ.get("HERMES_QUANT_RESEARCH_LOOP", "0") == "1"


# ---------------------------------------------------------------------------
# Default strategy factories (deterministic; ZERO real LLM calls in dry_run)
# ---------------------------------------------------------------------------


def _default_research_strategy(hyp: Hypothesis) -> Callable[..., dict[str, float]]:
    """Build the deterministic OOS strategy for the HypothesisRunner half.

    The HypothesisRunner strategy callable has signature
    ``(universe, window_start, window_end, dry_run) -> dict[str, float]``
    (orchestrator.py:199-202). In ``dry_run=True`` it MUST make ZERO real LLM
    calls (orchestrator.py:204-205 contract).

    The default here is a deterministic neutral StubLLMCommittee-backed
    strategy that returns inconclusive-by-default metrics (no edge). It exists
    so a dry-run cycle is fully reproducible and costs nothing; an ``--armed``
    cron run injects a real-LLM strategy_factory instead (still never promotes
    to live).
    """

    def _strategy(
        universe: list[str],
        window_start: date,
        window_end: date,
        dry_run: bool = True,
    ) -> dict[str, float]:
        # Touch the StubLLMCommittee so the dry-run path is provably LLM-free
        # and import-clean. Its deterministic neutral signal carries no edge.
        from hermes_quant.backtest.stub_llm import StubLLMCommittee

        _ = StubLLMCommittee()
        # Neutral, no-edge metrics: does not pass any plausible success
        # criterion and does not fire a typical falsification criterion.
        # External truth advances the hypothesis, not this stub.
        return {
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "vs_buyhold_alpha": 0.0,
            "n_decisions": 0.0,
            "total_return": 0.0,
        }

    _strategy.__name__ = "research_loop_dry_run_stub"
    return _strategy


def _default_promotion_strategy(hyp: Hypothesis) -> Any:
    """Build the StrategyProtocol object for the PromotionOrchestrator half.

    ``PromotionOrchestrator.run`` expects a StrategyProtocol object (has a
    ``decide(ticker, as_of, price_history) -> float`` method), a DIFFERENT
    shape from the HypothesisRunner strategy. Default to the deterministic
    buy-and-hold reference strategy so the promotion step is reproducible and
    LLM-free. Still produces only a review-only PromotionRecord.
    """
    from hermes_quant.eval.stockbench import _BuyAndHoldStrategy

    return _BuyAndHoldStrategy()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CandidateOutcome:
    """One candidate's journey through the OUTER standard-of-truth."""

    hypothesis_id: str
    run_id: str | None
    verdict: str | None  # validated | falsified | inconclusive | error
    contamination_guard_fired: bool
    config_hash: str | None
    promotion_record_id: str | None
    promote: bool | None  # gate decision; None if the promotion step was skipped
    error: str | None = None


@dataclass
class ResearchLoopSummary:
    """One cycle's accumulated counters + per-candidate outcomes."""

    cycle_id: str
    flag_on: bool
    halt_aborted: bool = False
    candidates_seen: int = 0
    candidates_run: int = 0
    validated: int = 0
    falsified: int = 0
    inconclusive: int = 0
    contaminated: int = 0
    promotion_records: int = 0
    promotions_recommended: int = 0  # gate.promote == True (still operator-gated)
    errors: int = 0
    outcomes: list[CandidateOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Audit log (append-only, fsync — copy hypothesis.py:234-241 pattern)
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _append_row(path: Path, row: dict[str, Any]) -> None:
    """Append a JSON row to the JSONL file with fsync (crash-safe)."""
    line = json.dumps(row, sort_keys=True, default=str) + "\n"
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# ResearchLoop
# ---------------------------------------------------------------------------


class ResearchLoop:
    """Drive W3 candidate hypotheses through the OUTER standard-of-truth on a cadence.

    Parameters
    ----------
    registry:
        HypothesisRegistry. W3's monthly meta-retro is the sole producer of
        ``status="open"`` candidate hypotheses (memory/meta_retro.py registers
        them; hypothesis.py:272 ``register`` is the only writer).
    runner:
        HypothesisRunner (orchestrator.py:163) — owns the lookahead sentinel and
        the deterministic external-truth auto-eval. W6 RELIES on it; it does not
        re-implement either.
    promotion_run:
        Callable matching ``PromotionOrchestrator.run`` (kwargs ``strategy``,
        ``universe``, ``window_start``, ``window_end``, ``hypothesis_id``,
        ``auto_record``). Injected so the cron and tests share the path.
        Default: a real ``PromotionOrchestrator().run`` (operator-review-only;
        ADR-0052). PRODUCES a PromotionRecord; NEVER promotes to live.
    factor_eval:
        Optional callable (``FactorOracle.evaluate_all``) run once per cycle for
        the factor half of evolve (O4 telemetry; verdicts are display-only here —
        raising a live factor weight is W4, not W6). Default ``None``.
    factor_bars:
        Bars DataFrame passed to ``factor_eval`` when supplied.
    strategy_factory:
        ``Callable[[Hypothesis], runner_strategy]`` building the deterministic
        OOS strategy (metrics-dict shape) for a candidate. Default: a neutral
        StubLLMCommittee-backed strategy so a dry-run cycle makes ZERO real LLM
        calls (orchestrator.py:204-205 contract).
    promotion_strategy_factory:
        ``Callable[[Hypothesis], StrategyProtocol]`` building the StrategyProtocol
        object for the PromotionOrchestrator (a different shape from the runner
        strategy). Default: deterministic buy-and-hold reference.
    audit_path:
        ``research_loop.jsonl`` override (tests pass ``tmp_path``).
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        runner: HypothesisRunner,
        promotion_run: Callable[..., Any] | None = None,
        factor_eval: Callable[..., dict[str, Any]] | None = None,
        factor_bars: Any | None = None,
        strategy_factory: Callable[[Hypothesis], Callable[..., dict[str, float]]]
        | None = None,
        promotion_strategy_factory: Callable[[Hypothesis], Any] | None = None,
        audit_path: Path | None = None,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._promotion_run = promotion_run
        self._factor_eval = factor_eval
        self._factor_bars = factor_bars
        self._strategy_factory = strategy_factory or _default_research_strategy
        self._promotion_strategy_factory = (
            promotion_strategy_factory or _default_promotion_strategy
        )
        self._audit_path = audit_path or RESEARCH_LOOP_LOG_PATH
        # Mirror RunCardLog/HypothesisRegistry: ensure the (empty) audit file
        # exists at construction. This is flag-independent and content-free, so
        # the off-state remains byte-identical (no ROWS are ever written when
        # the flag is OFF — run_cycle returns before any _append_row).
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._audit_path.exists():
            self._audit_path.touch()

    # ------------------------------------------------------------------
    # Lazy promotion_run default (real PromotionOrchestrator)
    # ------------------------------------------------------------------

    def _get_promotion_run(self) -> Callable[..., Any]:
        if self._promotion_run is not None:
            return self._promotion_run
        # Lazy import so the core loop carries no STOCKBENCH dependency unless a
        # validated candidate actually reaches the promotion step.
        from hermes_quant.eval.promotion_orchestrator import PromotionOrchestrator

        self._promotion_run = PromotionOrchestrator().run
        return self._promotion_run

    # ------------------------------------------------------------------
    # Window resolution (robustness-not-peak)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_window(
        hyp: Hypothesis,
        window_start: date | None,
        window_end: date | None,
    ) -> tuple[date, date]:
        """Resolve the OOS window for a candidate.

        Precedence: explicit args > hypothesis.scope window > default. The
        default is a fixed-length lookback, NEVER tuned to maximise pass-rate
        (ADR-0080 §D80.3.3).
        """
        scope = hyp.scope or {}

        def _coerce(v: Any) -> date | None:
            if isinstance(v, date):
                return v
            if isinstance(v, str):
                try:
                    return date.fromisoformat(v)
                except ValueError:
                    return None
            return None

        ws = window_start or _coerce(scope.get("window_start"))
        we = window_end or _coerce(scope.get("window_end"))
        if we is None:
            we = datetime.now(UTC).date()
        if ws is None:
            from datetime import timedelta

            ws = we - timedelta(days=_DEFAULT_OOS_DAYS)
        return ws, we

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        *,
        universe: list[str],
        window_start: date | None = None,
        window_end: date | None = None,
        dry_run: bool = True,
        max_candidates: int = _MAX_CANDIDATES_PER_CYCLE,
        halts: Iterable[dict[str, Any]] | None = None,
    ) -> ResearchLoopSummary:
        """One full cycle: drain open candidates → backtest → record → (optional) gate.

        Steps (per candidate, in registry order, capped at ``max_candidates``):
          1. INNER: take an ``open`` candidate (W3 output; ``read_all_open()``).
          2. OUTER: ``runner.run(...)`` — auto-evaluates the PRE-DECLARED
             criteria against external-truth metrics, fires the lookahead
             sentinel on contamination (orchestrator.py:313-318 forces
             verdict='falsified' + contamination_guard_fired=True), writes a
             reproducible RunCard (config_hash at orchestrator.py:299), and
             advances the hypothesis to its terminal registry status.
          3. OUTER: ONLY if verdict == 'validated' AND not contaminated, call
             ``promotion_run(...)`` to PRODUCE a PromotionRecord
             (operator-review-only; ADR-0052). A 'falsified' / 'inconclusive' /
             contaminated candidate NEVER reaches the gate.
          4. Append a research_loop.jsonl audit row; accumulate the summary.

        With ``flag_on()`` False this returns an empty summary and writes
        nothing (byte-identical off-state).

        A non-empty ``halts`` aborts immediately (fail-closed): ``halt_aborted``
        is True, nothing runs, nothing is written.
        """
        cycle_id = f"rl_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"

        # --- 0. Flag-gate at the top: byte-identical off-state. No reads,
        #        no writes, no side effects. ---
        if not flag_on():
            return ResearchLoopSummary(cycle_id=cycle_id, flag_on=False)

        summary = ResearchLoopSummary(cycle_id=cycle_id, flag_on=True)

        # --- 1. Halt fail-closed: abort BEFORE any work or write
        #        (mirrors quant-autonomous-tick.py:213-225). ---
        halts_list = list(halts) if halts else []
        if halts_list:
            summary.halt_aborted = True
            logger.warning(
                "research-loop: %s ABORTED — active halts: %s", cycle_id, halts_list
            )
            return summary

        # --- 2. Optional factor-oracle telemetry (display-only; O4 is W4). ---
        factor_tier_histogram: dict[str, int] | None = None
        if self._factor_eval is not None and self._factor_bars is not None:
            try:
                verdicts = self._factor_eval(self._factor_bars)
                factor_tier_histogram = {}
                for v in verdicts.values():
                    tier = str(getattr(v, "tier", "unknown"))
                    factor_tier_histogram[tier] = factor_tier_histogram.get(tier, 0) + 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("research-loop: factor_eval failed: %s", exc)
                factor_tier_histogram = {"error": 1}

        # --- 3. Drain open candidates (capped). W3 is the sole producer. ---
        cap = max(0, min(max_candidates, _MAX_CANDIDATES_PER_CYCLE))
        open_candidates = list(self._registry.read_all_open())
        summary.candidates_seen = len(open_candidates)
        selected = open_candidates[:cap]

        for hyp in selected:
            outcome = self._run_candidate(
                hyp,
                universe=universe,
                window_start=window_start,
                window_end=window_end,
                dry_run=dry_run,
            )
            summary.candidates_run += 1
            self._accumulate(summary, outcome)
            self._write_candidate_row(cycle_id, outcome)

        # --- 4. Cycle summary audit row. ---
        self._write_cycle_row(summary, factor_tier_histogram, dry_run=dry_run)
        logger.info(
            "research-loop: %s seen=%d run=%d validated=%d falsified=%d "
            "inconclusive=%d contaminated=%d promo_records=%d promo_recommended=%d",
            cycle_id,
            summary.candidates_seen,
            summary.candidates_run,
            summary.validated,
            summary.falsified,
            summary.inconclusive,
            summary.contaminated,
            summary.promotion_records,
            summary.promotions_recommended,
        )
        return summary

    # ------------------------------------------------------------------
    # Per-candidate execution
    # ------------------------------------------------------------------

    def _run_candidate(
        self,
        hyp: Hypothesis,
        *,
        universe: list[str],
        window_start: date | None,
        window_end: date | None,
        dry_run: bool,
    ) -> CandidateOutcome:
        hyp_id = hyp.hypothesis_id
        ws, we = self._resolve_window(hyp, window_start, window_end)

        # --- OUTER step A: deterministic backtest + lookahead sentinel +
        #     external-truth auto-eval (HypothesisRunner owns all three). ---
        try:
            strategy = self._strategy_factory(hyp)
            card: RunCard = self._runner.run(
                hyp_id,
                strategy=strategy,
                universe=universe,
                window_start=ws,
                window_end=we,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("research-loop: candidate %s errored: %s", hyp_id, exc)
            return CandidateOutcome(
                hypothesis_id=hyp_id,
                run_id=None,
                verdict="error",
                contamination_guard_fired=False,
                config_hash=None,
                promotion_record_id=None,
                promote=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        contaminated = bool(card.contamination_guard_fired)
        verdict = card.verdict

        outcome = CandidateOutcome(
            hypothesis_id=hyp_id,
            run_id=card.run_id,
            verdict=verdict,
            contamination_guard_fired=contaminated,
            config_hash=card.strategy_config_hash,
            promotion_record_id=None,
            promote=None,
        )

        # --- OUTER step B: PromotionGate ONLY for a clean, validated candidate.
        #     A contaminated OR non-validated candidate NEVER reaches the gate.
        #     This is the load-bearing short-circuit (eval-gate criterion 2/3). ---
        if verdict == "validated" and not contaminated:
            self._produce_promotion_record(outcome, hyp, universe=universe, ws=ws, we=we)

        return outcome

    def _produce_promotion_record(
        self,
        outcome: CandidateOutcome,
        hyp: Hypothesis,
        *,
        universe: list[str],
        ws: date,
        we: date,
    ) -> None:
        """PRODUCE-ONLY: emit a review-only PromotionRecord. NEVER promotes.

        The orchestrator writes a PromotionRecord and, per
        promotion_orchestrator.py:354-360, "does NOT transition hypothesis
        status". W6 adds NO transition beyond the deterministic auto-eval the
        runner already did. A ``decision.promote == True`` only flags the
        record for the OPERATOR's attention — it does NOT promote to live.
        """
        promotion_run = self._get_promotion_run()
        try:
            promo_strategy = self._promotion_strategy_factory(hyp)
            record = promotion_run(
                strategy=promo_strategy,
                universe=universe,
                window_start=ws,
                window_end=we,
                hypothesis_id=hyp.hypothesis_id,
                auto_record=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "research-loop: promotion step failed for %s: %s",
                hyp.hypothesis_id,
                exc,
            )
            outcome.error = (outcome.error or "") + f" promo:{type(exc).__name__}"
            return

        outcome.promotion_record_id = getattr(record, "record_id", None)
        decision = getattr(record, "decision", {}) or {}
        promote = decision.get("promote") if isinstance(decision, dict) else None
        outcome.promote = bool(promote) if promote is not None else None

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    @staticmethod
    def _accumulate(summary: ResearchLoopSummary, outcome: CandidateOutcome) -> None:
        summary.outcomes.append(outcome)
        if outcome.contamination_guard_fired:
            summary.contaminated += 1
        if outcome.verdict == "validated":
            summary.validated += 1
        elif outcome.verdict == "falsified":
            summary.falsified += 1
        elif outcome.verdict == "inconclusive":
            summary.inconclusive += 1
        elif outcome.verdict == "error":
            summary.errors += 1
        if outcome.promotion_record_id is not None:
            summary.promotion_records += 1
        if outcome.promote is True:
            summary.promotions_recommended += 1

    # ------------------------------------------------------------------
    # Audit rows
    # ------------------------------------------------------------------

    def _write_candidate_row(self, cycle_id: str, outcome: CandidateOutcome) -> None:
        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "candidate_outcome",
            "cycle_id": cycle_id,
            "asof": datetime.now(UTC).isoformat(),
            **asdict(outcome),
        }
        _append_row(self._audit_path, row)

    def _write_cycle_row(
        self,
        summary: ResearchLoopSummary,
        factor_tier_histogram: dict[str, int] | None,
        *,
        dry_run: bool,
    ) -> None:
        row: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "kind": "research_loop_cycle",
            "cycle_id": summary.cycle_id,
            "asof": datetime.now(UTC).isoformat(),
            "dry_run": dry_run,
            "halt_aborted": summary.halt_aborted,
            "candidates_seen": summary.candidates_seen,
            "candidates_run": summary.candidates_run,
            "validated": summary.validated,
            "falsified": summary.falsified,
            "inconclusive": summary.inconclusive,
            "contaminated": summary.contaminated,
            "promotion_records": summary.promotion_records,
            "promotions_recommended": summary.promotions_recommended,
            "errors": summary.errors,
            # Documented IN-CODE: the cron NEVER auto-promotes; promotion to
            # live is an operator action (ADR-0052).
            "auto_promoted_to_live": False,
            "factor_tier_histogram": factor_tier_histogram,
        }
        _append_row(self._audit_path, row)
