"""tests/research/test_orchestrator.py — HypothesisRunner orchestrator tests (ADR-0048).

Coverage:
 - Full lifecycle: open→running→validated (success criteria met).
 - Full lifecycle: open→running→falsified (falsification criterion fires).
 - Inconclusive verdict when no criteria defined.
 - Inconclusive verdict when partial success (not all success criteria pass).
 - dry_run=True: strategy callable receives dry_run=True; no LLM calls.
 - Hypothesis transitions to validated/falsified in registry post-run.
 - Inconclusive hypothesis left in 'running' state.
 - Strategy exception: metrics fallback to NaN, verdict is inconclusive.
 - Auto-evaluation: _eval_criterion unit tests for various expressions.
 - RunCard artifacts correctly recorded in RunCardLog.
 - run_id format check.
 - strategy_config_hash is deterministic.
 - Criterion evaluation with missing metric keys raises ValueError.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from hermes_quant.research.hypothesis import (
    Hypothesis,
    HypothesisRegistry,
)
from hermes_quant.research.orchestrator import (
    HypothesisRunner,
    _eval_criterion,
    _evaluate_criteria,
)
from hermes_quant.research.run_card import RunCardLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_hypothesis(
    success_criteria=None,
    falsification_criteria=None,
    **overrides,
) -> Hypothesis:
    defaults = dict(
        author="test-orchestrator",
        claim="Test claim",
        null_hypothesis="Test null",
        success_criteria=success_criteria or ["sharpe >= 0.5"],
        falsification_criteria=falsification_criteria or ["sharpe < 0.0"],
        experiment_design="Synthetic walk-forward",
        duration_target_days=90,
        scope={"universe": ["AAPL"]},
    )
    defaults.update(overrides)
    return Hypothesis(**defaults)


def _good_strategy(universe, window_start, window_end, dry_run=True):
    """Returns metrics that pass success_criteria (sharpe >= 0.5)."""
    return {
        "sharpe": 0.85,
        "sortino": 1.20,
        "max_drawdown": -0.05,
        "vs_buyhold_alpha": 0.03,
        "n_decisions": 30.0,
        "total_return": 0.12,
    }


def _bad_strategy(universe, window_start, window_end, dry_run=True):
    """Returns metrics that fire falsification_criteria (sharpe < 0.0)."""
    return {
        "sharpe": -0.30,
        "sortino": -0.50,
        "max_drawdown": -0.22,
        "vs_buyhold_alpha": -0.10,
        "n_decisions": 15.0,
        "total_return": -0.08,
    }


def _mediocre_strategy(universe, window_start, window_end, dry_run=True):
    """Sharpe between 0.0 and 0.5: doesn't fail falsification or pass success."""
    return {
        "sharpe": 0.20,
        "sortino": 0.30,
        "max_drawdown": -0.10,
        "vs_buyhold_alpha": 0.01,
        "n_decisions": 20.0,
        "total_return": 0.04,
    }


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(path=tmp_path / "hypotheses.jsonl")


@pytest.fixture
def run_card_log(tmp_path):
    return RunCardLog(path=tmp_path / "run_cards.jsonl")


@pytest.fixture
def runner(registry, run_card_log):
    return HypothesisRunner(registry=registry, run_card_log=run_card_log)


# ---------------------------------------------------------------------------
# _eval_criterion unit tests
# ---------------------------------------------------------------------------


def test_eval_sharpe_pass():
    assert _eval_criterion("sharpe >= 0.5", {"sharpe": 0.85}) is True


def test_eval_sharpe_fail():
    assert _eval_criterion("sharpe >= 0.5", {"sharpe": 0.30}) is False


def test_eval_negative_sharpe():
    assert _eval_criterion("sharpe < 0.0", {"sharpe": -0.3}) is True


def test_eval_alpha_pass():
    assert _eval_criterion("vs_buyhold_alpha > 0.0", {"vs_buyhold_alpha": 0.05}) is True


def test_eval_alpha_fail():
    assert _eval_criterion("vs_buyhold_alpha > 0.0", {"vs_buyhold_alpha": -0.01}) is False


def test_eval_builtins_blocked():
    """eval() must not allow builtin access."""
    with pytest.raises((ValueError, NameError)):
        _eval_criterion("__import__('os').getcwd()", {"sharpe": 1.0})


def test_eval_unknown_name_raises():
    with pytest.raises(ValueError):
        _eval_criterion("nonexistent_metric >= 0.5", {"sharpe": 1.0})


# ---------------------------------------------------------------------------
# _evaluate_criteria unit tests
# ---------------------------------------------------------------------------


def test_evaluate_criteria_validated():
    verdict, reasons = _evaluate_criteria(
        success_criteria=["sharpe >= 0.5", "vs_buyhold_alpha > 0.0"],
        falsification_criteria=["sharpe < 0.0"],
        metrics={"sharpe": 0.85, "vs_buyhold_alpha": 0.05},
    )
    assert verdict == "validated"
    assert any("PASSED" in r for r in reasons)


def test_evaluate_criteria_falsified():
    verdict, reasons = _evaluate_criteria(
        success_criteria=["sharpe >= 0.5"],
        falsification_criteria=["sharpe < 0.0"],
        metrics={"sharpe": -0.3},
    )
    assert verdict == "falsified"
    assert any("FALSIFIED" in r for r in reasons)


def test_evaluate_criteria_inconclusive():
    verdict, reasons = _evaluate_criteria(
        success_criteria=["sharpe >= 0.5"],
        falsification_criteria=["sharpe < 0.0"],
        metrics={"sharpe": 0.20},
    )
    assert verdict == "inconclusive"


def test_evaluate_criteria_no_criteria():
    verdict, reasons = _evaluate_criteria([], [], {"sharpe": 1.0})
    assert verdict == "inconclusive"
    assert any("No criteria" in r for r in reasons)


# ---------------------------------------------------------------------------
# HypothesisRunner lifecycle tests
# ---------------------------------------------------------------------------


def test_full_lifecycle_validated(registry, run_card_log, runner):
    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    card = runner.run(
        hyp_id,
        strategy=_good_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card.verdict == "validated"
    assert card.hypothesis_id == hyp_id
    assert registry.read(hyp_id).status == "validated"
    # RunCard recorded in log
    cards = run_card_log.read_for_hypothesis(hyp_id)
    assert len(cards) == 1
    assert cards[0].verdict == "validated"


def test_full_lifecycle_falsified(registry, run_card_log, runner):
    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    card = runner.run(
        hyp_id,
        strategy=_bad_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card.verdict == "falsified"
    assert registry.read(hyp_id).status == "falsified"


def test_full_lifecycle_inconclusive(registry, run_card_log, runner):
    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    card = runner.run(
        hyp_id,
        strategy=_mediocre_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card.verdict == "inconclusive"
    # Inconclusive: hypothesis stays in 'running' state
    assert registry.read(hyp_id).status == "running"


def test_strategy_callable_receives_dry_run_flag(registry, run_card_log, runner):
    """Verify strategy receives dry_run=True and no LLM calls are needed."""
    calls = []

    def spy_strategy(universe, window_start, window_end, dry_run=True):
        calls.append({"dry_run": dry_run})
        return {
            "sharpe": 0.6,
            "sortino": 0.9,
            "max_drawdown": -0.07,
            "vs_buyhold_alpha": 0.02,
            "n_decisions": 10.0,
            "total_return": 0.08,
        }

    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    runner.run(
        hyp_id,
        strategy=spy_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True


def test_strategy_exception_metrics_nan(registry, run_card_log, runner):
    """If strategy raises, metrics are NaN and verdict is inconclusive."""

    def exploding_strategy(universe, window_start, window_end, dry_run=True):
        raise RuntimeError("synthetic crash")

    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    card = runner.run(
        hyp_id,
        strategy=exploding_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card.verdict == "inconclusive"
    assert math.isnan(card.metrics["sharpe"])
    # Hypothesis status remains "running" (inconclusive)
    assert registry.read(hyp_id).status == "running"


def test_run_id_format(registry, run_card_log, runner):
    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    card = runner.run(
        hyp_id,
        strategy=_good_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card.run_id.startswith(f"run_{hyp_id}_")


def test_strategy_config_hash_deterministic(registry, run_card_log, runner):
    """Same strategy + universe + window produces same config hash."""

    def deterministic_strategy(universe, window_start, window_end, dry_run=True):
        return {
            "sharpe": 0.6,
            "sortino": 0.9,
            "max_drawdown": -0.07,
            "vs_buyhold_alpha": 0.02,
            "n_decisions": 10.0,
            "total_return": 0.08,
        }

    hyp1 = _make_hypothesis()
    hyp2 = _make_hypothesis(claim="second run same config")
    id1 = registry.register(hyp1)
    id2 = registry.register(hyp2)
    card1 = runner.run(
        id1,
        strategy=deterministic_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    # Need to go through running for id2 already — register a fresh registry
    card2 = runner.run(
        id2,
        strategy=deterministic_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    assert card1.strategy_config_hash == card2.strategy_config_hash


def test_run_not_open_raises(registry, run_card_log, runner):
    """Running against a hypothesis already in terminal state must raise."""
    from hermes_quant.research.hypothesis import InvalidStatusTransition

    hyp = _make_hypothesis()
    hyp_id = registry.register(hyp)
    runner.run(
        hyp_id,
        strategy=_good_strategy,
        universe=["AAPL"],
        window_start=date(2025, 1, 1),
        window_end=date(2025, 3, 31),
        dry_run=True,
    )
    # Now hypothesis is validated → cannot run again
    with pytest.raises(InvalidStatusTransition):
        runner.run(
            hyp_id,
            strategy=_good_strategy,
            universe=["AAPL"],
            window_start=date(2025, 1, 1),
            window_end=date(2025, 3, 31),
            dry_run=True,
        )
