"""tests/research/test_research_loop.py — W6 research-loop eval gate (ADR-0080).

The seven gate criteria from selfevolve-W6-research-loop.md §4, each a test:

  1. Reproducible Run-Cards (strategy_config_hash identical for same config).
  2. Lookahead sentinel clean AND load-bearing — a LookaheadViolation candidate
     gets contamination_guard_fired=True + verdict='falsified' and NEVER reaches
     the PromotionGate.
  3. ZERO auto-promotion to live — a validated+promote=True candidate yields only
     a review-only PromotionRecord; no operator transition the cron wrote, no flag
     flipped.
  4. Byte-identical off-state (flag unset → empty summary, writes nothing).
  5. External-truth-only advancement (validated only when pre-declared
     success_criteria pass against deterministic metrics).
  6. Bounded per cycle (max_candidates honoured).
  7. Halt fail-closed (non-empty halts → abort, nothing run/written).

Plus an advisory-plane test: the module imports nothing from the risk gate /
sizing ladder / kill-switch for mutation.

Fixtures mirror tests/research/test_orchestrator.py (registry/run_card_log on
tmp_path) and tests/eval/test_promotion_orchestrator.py (inject a stub
promotion_run so no STOCKBENCH data feed is needed).
"""

from __future__ import annotations

from datetime import date

import pytest

from hermes_quant.research.hypothesis import Hypothesis, HypothesisRegistry
from hermes_quant.research.orchestrator import HypothesisRunner
from hermes_quant.research.research_loop import ResearchLoop
from hermes_quant.research.run_card import RunCardLog

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _open_candidate(
    registry,
    *,
    success=("vs_buyhold_alpha > 0.0",),
    falsify=("sharpe < 0.0",),
    ticker="AAPL",
):
    hyp = Hypothesis(
        author="w3-meta-retro",
        claim="candidate from monthly meta-retro",
        null_hypothesis="no edge",
        success_criteria=list(success),
        falsification_criteria=list(falsify),
        experiment_design="walk-forward OOS",
        duration_target_days=90,
        scope={"universe": [ticker]},
    )
    return registry.register(hyp)


def _good_strategy(universe, window_start, window_end, dry_run=True):
    return {
        "sharpe": 0.85,
        "sortino": 1.2,
        "max_drawdown": -0.05,
        "vs_buyhold_alpha": 0.03,
        "n_decisions": 30.0,
        "total_return": 0.12,
    }


def _bad_strategy(universe, window_start, window_end, dry_run=True):
    return {
        "sharpe": -0.3,
        "sortino": -0.5,
        "max_drawdown": -0.22,
        "vs_buyhold_alpha": -0.1,
        "n_decisions": 15.0,
        "total_return": -0.08,
    }


def _contaminating_strategy(universe, window_start, window_end, dry_run=True):
    from hermes_quant.backtest.engine import LookaheadViolation

    raise LookaheadViolation("synthetic contamination")


class _StubPromotionRun:
    """Records calls; returns a fake PromotionRecord-like object with a promote flag."""

    def __init__(self, promote=True):
        self.calls: list[str | None] = []
        self._promote = promote

    def __call__(
        self,
        *,
        strategy,
        universe,
        window_start,
        window_end,
        hypothesis_id=None,
        auto_record=True,
        **kw,
    ):
        self.calls.append(hypothesis_id)
        promote = self._promote

        class _Rec:
            record_id = "prom_stub01"
            decision = {
                "promote": promote,
                "reasons": [],
                "suggested_action": "review",
            }

        return _Rec()


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(path=tmp_path / "hypotheses.jsonl")


@pytest.fixture
def run_card_log(tmp_path):
    return RunCardLog(path=tmp_path / "run_cards.jsonl")


@pytest.fixture
def loop(registry, run_card_log, tmp_path):
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    return ResearchLoop(
        registry=registry,
        runner=runner,
        promotion_run=_StubPromotionRun(promote=True),
        strategy_factory=lambda hyp: _good_strategy,
        audit_path=tmp_path / "research_loop.jsonl",
    )


# ---------------------------------------------------------------------------
# Gate criterion 4: byte-identical off-state
# ---------------------------------------------------------------------------


def test_research_loop_off_state_is_silent_and_writes_nothing(
    loop, tmp_path, monkeypatch, registry
):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_LOOP", raising=False)
    _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.flag_on is False
    assert summ.candidates_run == 0 and summ.outcomes == []
    assert summ.candidates_seen == 0  # no registry read in off-state
    # Nothing written (file exists empty from construction; off-state writes no rows).
    assert not (tmp_path / "research_loop.jsonl").read_text().strip()


# ---------------------------------------------------------------------------
# Gate criterion 1: reproducible Run-Cards
# ---------------------------------------------------------------------------


def test_research_loop_run_cards_reproducible(loop, run_card_log, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    hid = _open_candidate(registry)
    loop.run_cycle(
        universe=["AAPL"],
        window_start=date(2025, 6, 1),
        window_end=date(2025, 8, 31),
        dry_run=True,
    )
    cards = run_card_log.read_for_hypothesis(hid)
    assert len(cards) >= 1
    # Same config across an independent run yields the same hash
    # (orchestrator.py:299 hashes {strategy_name, sorted(universe),
    # window_start, window_end, dry_run}).
    hid2 = _open_candidate(registry, ticker="AAPL")
    loop.run_cycle(
        universe=["AAPL"],
        window_start=date(2025, 6, 1),
        window_end=date(2025, 8, 31),
        dry_run=True,
    )
    cards2 = run_card_log.read_for_hypothesis(hid2)
    assert cards[0].strategy_config_hash == cards2[0].strategy_config_hash


# ---------------------------------------------------------------------------
# Gate criterion 2: lookahead sentinel clean, contaminated never promoted
# ---------------------------------------------------------------------------


def test_research_loop_contaminated_candidate_never_promoted(
    registry, run_card_log, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    stub_promo = _StubPromotionRun(promote=True)
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    loop = ResearchLoop(
        registry=registry,
        runner=runner,
        promotion_run=stub_promo,
        strategy_factory=lambda hyp: _contaminating_strategy,
        audit_path=tmp_path / "research_loop.jsonl",
    )
    hid = _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.contaminated == 1
    card = run_card_log.read_for_hypothesis(hid)[0]
    assert card.contamination_guard_fired is True and card.verdict == "falsified"
    assert stub_promo.calls == []  # PromotionGate NEVER reached


# ---------------------------------------------------------------------------
# Gate criterion 3: ZERO auto-promotion to live
# ---------------------------------------------------------------------------


def test_research_loop_never_auto_promotes_to_live(loop, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    hid = _open_candidate(registry)  # _good_strategy → validated → gate promote=True
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.promotions_recommended >= 1  # gate said promote=True ...
    assert summ.promotion_records >= 1  # ... and a record was PRODUCED
    # ... but the registry status is exactly the deterministic auto-eval verdict
    # (validated via external-truth criteria), NOT an operator promotion.
    assert registry.read(hid).status == "validated"
    # No "promoted_to_live"/operator transition row exists — W6 wrote none.
    # Verify by replaying the raw event log: the only status_change rows are
    # those HypothesisRunner emitted (open→running→validated). There is no
    # status beyond the terminal auto-eval verdict, and no flag was flipped.
    import json

    raw = (registry._path).read_text().splitlines()
    status_changes = [
        json.loads(line)
        for line in raw
        if line.strip() and json.loads(line).get("kind") == "status_change"
        and json.loads(line).get("hypothesis_id") == hid
    ]
    new_statuses = {sc["new_status"] for sc in status_changes}
    assert new_statuses <= {"running", "validated"}
    assert "promoted_to_live" not in new_statuses


def test_research_loop_no_live_artifact_beyond_review_record(loop, registry, monkeypatch):
    """Provably zero auto-promotion: the ONLY live-policy-adjacent artifact is a
    review-only PromotionRecord. The cron's own audit row asserts it never
    auto-promoted, and no flag was flipped by the loop."""
    import json

    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    # The cron does not flip HERMES_QUANT_RESEARCH_LOOP or any other flag.
    import os

    assert os.environ.get("HERMES_QUANT_RESEARCH_LOOP") == "1"  # unchanged by loop
    # The audit cycle row records auto_promoted_to_live=False.
    rows = [
        json.loads(line)
        for line in loop._audit_path.read_text().splitlines()
        if line.strip()
    ]
    cycle_rows = [r for r in rows if r.get("kind") == "research_loop_cycle"]
    assert cycle_rows and cycle_rows[-1]["auto_promoted_to_live"] is False
    assert summ.promotion_records >= 1


# ---------------------------------------------------------------------------
# Gate criterion 5: external-truth-only advancement
# ---------------------------------------------------------------------------


def test_research_loop_advances_only_on_external_truth(
    registry, run_card_log, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    loop = ResearchLoop(
        registry=registry,
        runner=runner,
        promotion_run=_StubPromotionRun(),
        strategy_factory=lambda hyp: _bad_strategy,  # fires falsification
        audit_path=tmp_path / "research_loop.jsonl",
    )
    hid = _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.falsified == 1 and registry.read(hid).status == "falsified"


# ---------------------------------------------------------------------------
# Gate criterion 6: bounded per cycle
# ---------------------------------------------------------------------------


def test_research_loop_respects_max_candidates(loop, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    for _ in range(5):
        _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True, max_candidates=2)
    assert summ.candidates_run == 2
    assert summ.candidates_seen == 5


# ---------------------------------------------------------------------------
# Gate criterion 7: halt fail-closed
# ---------------------------------------------------------------------------


def test_research_loop_aborts_on_active_halt(loop, registry, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    _open_candidate(registry)
    summ = loop.run_cycle(
        universe=["AAPL"],
        dry_run=True,
        halts=[{"reason": "operator halt", "scope": "global"}],
    )
    assert summ.halt_aborted is True and summ.candidates_run == 0
    assert not (tmp_path / "research_loop.jsonl").read_text().strip()


# ---------------------------------------------------------------------------
# Advisory-plane invariant: NEVER imports the risk gate / sizing ladder /
# kill-switch for mutation.
# ---------------------------------------------------------------------------


def test_research_loop_does_not_import_risk_gate_or_killswitch():
    """The module source imports nothing from risk gate / kill-switch / sizing
    ladder. It writes ONLY to the advisory plane (ADR-0080 §D80.1)."""
    import ast

    import hermes_quant.research.research_loop as rl

    src = __import__("pathlib").Path(rl.__file__).read_text()
    tree = ast.parse(src)

    # Collect every imported module name (the AST ignores docstring prose).
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # No import from the risk gate package (the deterministic gate / hard
    # limits / sizing ladder are immutable by this loop — ADR-0080 §D80.1).
    assert not any(m.startswith("hermes_quant.risk") for m in imported), imported

    # The core module never references the kill-switch as a FILE PATH literal.
    # 'halt_state' appears in docstring prose (explaining the cron wrapper's
    # read-only, fail-closed read) but never as an executable path the loop
    # opens/writes. Docstrings are the .body[0] string Expr of module / func /
    # class nodes — exclude them, then assert no remaining string mentions it.
    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstring_ids.add(doc)
    non_doc_strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value not in docstring_ids
    ]
    assert not any("halt_state" in s for s in non_doc_strings), (
        "no halt_state path literal in executable code"
    )


# ---------------------------------------------------------------------------
# Extra: dry-run makes ZERO real LLM calls via the default strategy_factory.
# ---------------------------------------------------------------------------


def test_research_loop_default_strategy_is_llm_free(registry, run_card_log, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    # No strategy_factory override → the default StubLLMCommittee-backed strategy.
    loop = ResearchLoop(
        registry=registry,
        runner=runner,
        promotion_run=_StubPromotionRun(promote=False),
        audit_path=tmp_path / "research_loop.jsonl",
    )
    hid = _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    # Neutral no-edge stub → not validated (alpha == 0.0 does not satisfy
    # "vs_buyhold_alpha > 0.0"); not falsified (sharpe 0.0 is not < 0.0).
    assert summ.candidates_run == 1
    assert summ.validated == 0
    assert registry.read(hid).status in ("running", "inconclusive")
