# W6 — Hypothesis→backtest→promote driving cron (`HERMES_QUANT_RESEARCH_LOOP`)

**Status:** ready-to-build (implementation-ready; no further research required)
**Date:** 2026-05-30
**Wave:** W6 (capability-map §4) — "Hypothesis→backtest→promote driving cron [QuantAgent inner/outer + RD-Agent Trace]"
**Closes:** O8 (`HypothesisRunner` RunCard display-only, no driving cron) + O9-wiring (`PromotionOrchestrator.log.record` operator-review-only — kept by design; W6 only *produces* records, never auto-promotes).
**Flag:** `HERMES_QUANT_RESEARCH_LOOP` (default-OFF).
**Depends on:** W3 (`HERMES_QUANT_MONTHLY_META_RETRO`) — consumes W3's candidate hypotheses. Benefits from W4/W5 but does not require them.
**Grounds:** `docs/research/2026-05-30-selfevolve-capability-map.md` §4 (W6 spec) + §5 (safety frame); `docs/adr/ADR-0080-self-evolution-framework.md` (§D80.1 two planes, §D80.2 tiers, §D80.3 the 5-point eval-gate contract, §D80.6 W6 row); ADR-0048 (Hypothesis registry + Run-Cards), ADR-0052 (PromotionOrchestrator, operator-action-only), ADR-0055 (FactorOracle 4-tier).

---

## 0. The one-paragraph rail (this is what W6 *is*)

W6 adds **ignition**, not new judgment. The engine already exists and works in isolation
(`HypothesisRunner.run` at `hermes_quant/research/orchestrator.py:182`; `FactorOracle.evaluate_all` at
`hermes_quant/factors/factor_oracle.py:450`; `PromotionOrchestrator.run` at
`hermes_quant/eval/promotion_orchestrator.py:373`). W6 is a **default-OFF deterministic cron** that, on a
cadence, drains W3's candidate hypotheses through that engine and emits reproducible Run-Cards +
PromotionRecords. It makes the **QuantAgent inner/outer rail explicit in code**:

- **INNER, cheap judge (advisory plane):** the committee / LLM strategy that *generated* the candidate
  hypothesis (W3) and any LLM strategy callable. Evidence only. Writes nothing live.
- **OUTER, standard-of-truth (immutable by the loop):** the deterministic OOS backtest
  (`StubLLMCommittee`/walk-forward) + the lookahead sentinel (`orchestrator.py:313-318`) + the
  `PromotionGate` (`hermes_quant/eval/promotion_gate.py:72` `check()`). Only this path scores truth.
- **PROMOTION TO LIVE STAYS THE OPERATOR ACTION.** The cron NEVER transitions a hypothesis to
  `validated`/`falsified` toward live influence and NEVER flips a flag. It only *produces*
  `PromotionRecord`s for the operator to review (`promotion_orchestrator.py:354-360` documents this; W6
  honors it verbatim — `auto_record=True` writes the JSONL, but the orchestrator already "does NOT
  transition hypothesis status"). The advisory plane proposes; the outer gate + a human ship.

With `HERMES_QUANT_RESEARCH_LOOP` unset/`!=1`, the cron exits 0 with empty stdout (silence-by-default,
no_agent contract) and writes nothing — byte-identical off-state (ADR-0080 §D80.8).

---

## 1. Advisory plane vs. outer standard-of-truth (the SAFETY frame, applied)

Per ADR-0080 §D80.1 and capability-map §5. W6 writes ONLY to the advisory plane.

| Surface | W6 access | Why safe |
|---|---|---|
| `hypotheses.jsonl` — read `open` candidates; append `running` then terminal `status_change` rows | **read + lifecycle-advance** | Lifecycle status is *registry bookkeeping*, NOT live policy. A `validated` hypothesis raises **no** live weight by itself — promotion to live influence is a separate operator action (ADR-0052; capability-map O4 "a `premium` verdict raises no live weight"). The transition is driven by the deterministic auto-eval in `orchestrator.py:343-353`, scored on external truth, not by an LLM. |
| `run_cards.jsonl` — append RunCards | **append-only** | Evidence artifact (ADR-0048). Display/audit only. |
| `factor_verdicts.jsonl` — append via `FactorOracle.evaluate_all` | **append-only** | 4-tier verdict (ADR-0055). Display only — no live weight responds (O4 is W4's job, not W6's). |
| `promotion_decisions.jsonl` — append PromotionRecords | **append-only** | Operator-review-only by design (ADR-0052; `promotion_orchestrator.py:354-360`). |
| `~/.hermes/quant/research/research_loop.jsonl` — NEW cron audit log | **append-only** | New W6 telemetry: one row per cycle + per-candidate outcome. |

**MUST NEVER touch (outside the loop, immutable by it — ADR-0080 §D80.1, capability-map §5):**

- the deterministic **risk gate** (`hermes_quant/risk/` / `silence_bias_gate`) and the **hard risk
  limits** (max loss, position caps, exposure);
- the **discrete sizing ladder** `{0, ±0.05, ±0.10, ±0.15, ±0.20}`;
- the **kill-switch** (`halt_state.json` — a separate process the runtime cannot signal);
- **promotion to live** — the cron produces records; the operator promotes.

W6 imports none of these for mutation. It reads `halt_state.json` **fail-closed** (abort the cycle if any
active halt), mirroring `quant-autonomous-tick.py:102-111`, but never writes it.

**External-truth-only (ADR-0080 §D80.3.1):** the only score that advances a hypothesis is the
deterministic backtest metrics dict (`vs_buyhold_alpha`, `sortino`, etc.) auto-evaluated against the
hypothesis's pre-declared `success_criteria`/`falsification_criteria` (declared BEFORE the run — ADR-0048
anti-post-hoc). No LLM self-score is ever read as truth.

---

## 2. Exact new / modified files

### NEW — `hermes_quant/research/research_loop.py` (the orchestration core — testable, no I/O cron concerns)

The cron is a thin wrapper; the logic lives here so it is unit-testable with `tmp_path` and injected
deps (mirrors how `tests/research/test_orchestrator.py:108-110` and
`tests/eval/test_promotion_orchestrator.py:161` construct their subjects with explicit paths/stubs).

```python
"""hermes_quant.research.research_loop — W6 quant-research driving loop (ADR-0080).

The INNER/OUTER rail made explicit (QuantAgent / FunSearch):
  INNER  (advisory, cheap judge): the candidate hypothesis + its LLM/committee strategy.
  OUTER  (standard-of-truth): deterministic OOS backtest + lookahead sentinel + PromotionGate.

This module PROPOSES ONLY. It NEVER promotes to live and NEVER flips a flag. Promotion
to live influence remains an explicit operator action (ADR-0052,
promotion_orchestrator.py:354-360). The cron produces RunCards + PromotionRecords; a human
reviews and (separately) promotes.

Flag: HERMES_QUANT_RESEARCH_LOOP (default-OFF). Off-state is byte-identical (returns an
empty ResearchLoopSummary, writes nothing).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_quant.research.hypothesis import (
    Hypothesis,
    HypothesisRegistry,
    HypothesisNotFound,
    InvalidStatusTransition,
)
from hermes_quant.research.orchestrator import HypothesisRunner
from hermes_quant.research.run_card import RunCard, RunCardLog

logger = logging.getLogger(__name__)

QUANT_HOME = Path.home() / ".hermes" / "quant"
RESEARCH_HOME = QUANT_HOME / "research"
RESEARCH_LOOP_LOG_PATH = RESEARCH_HOME / "research_loop.jsonl"

# Default OOS window length when a candidate hypothesis does not pin its own window.
# Deliberately a RANGE-friendly default, never a tuned peak (ADR-0080 §D80.3.3).
_DEFAULT_OOS_DAYS = 90
# Hard per-cycle cap so a flood of candidates cannot blow the LLM budget / runtime.
_MAX_CANDIDATES_PER_CYCLE = 8


def flag_on() -> bool:
    """W6 master flag. Default-OFF. Copy of the repo idiom
    (cf. autonomous.py:394 HERMES_QUANT_PORTFOLIO_CAPS,
    llm_committee.py:977 HERMES_QUANT_RESEARCH_DEBATE)."""
    return os.environ.get("HERMES_QUANT_RESEARCH_LOOP", "0") == "1"


@dataclass
class CandidateOutcome:
    hypothesis_id: str
    run_id: str | None
    verdict: str | None                      # validated | falsified | inconclusive | error
    contamination_guard_fired: bool
    config_hash: str | None
    promotion_record_id: str | None
    promote: bool | None                     # gate decision; None if promotion step skipped
    error: str | None = None


@dataclass
class ResearchLoopSummary:
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
    promotions_recommended: int = 0          # gate.promote == True (still operator-gated)
    errors: int = 0
    outcomes: list[CandidateOutcome] = field(default_factory=list)


class ResearchLoop:
    """Drive W3 candidate hypotheses through the OUTER standard-of-truth on a cadence.

    Parameters
    ----------
    registry:        HypothesisRegistry (W3 writes open candidates here).
    runner:          HypothesisRunner (orchestrator.py:163) — owns the lookahead sentinel.
    promotion_run:   Callable matching PromotionOrchestrator.run signature; injected so the
                     cron and tests share the path. Default: a real PromotionOrchestrator.run.
    factor_eval:     Optional callable (FactorOracle.evaluate_all) run once per cycle for the
                     factor half of evolve (O4 telemetry; verdicts are display-only here).
    strategy_factory: Callable[[Hypothesis], strategy_callable] building the OOS strategy for a
                     candidate. Default: a deterministic StubLLMCommittee-backed strategy so a
                     dry-run cycle makes ZERO real LLM calls (orchestrator.py:204-205 contract).
    audit_path:      research_loop.jsonl override (tests pass tmp_path).
    """

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        runner: HypothesisRunner,
        promotion_run: Callable[..., Any] | None = None,
        factor_eval: Callable[..., dict[str, Any]] | None = None,
        strategy_factory: Callable[[Hypothesis], Callable[..., dict[str, float]]] | None = None,
        audit_path: Path | None = None,
    ) -> None: ...

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

        Steps (per candidate, in registry order, capped at max_candidates):
          1. INNER: take an `open` candidate hypothesis (W3 output; registry.read_all_open()).
          2. OUTER: runner.run(hyp_id, strategy=..., universe, window, dry_run) — this
             auto-evaluates the PRE-DECLARED criteria, fires the lookahead sentinel on
             contamination (orchestrator.py:313-318 forces verdict='falsified'), writes a
             reproducible RunCard (config_hash at orchestrator.py:299), and advances the
             hypothesis to its terminal registry status.
          3. OUTER: if verdict == 'validated' AND not contaminated, run the PromotionGate via
             promotion_run(...) to PRODUCE a PromotionRecord (operator-review-only; ADR-0052).
             A 'falsified'/'inconclusive'/contaminated candidate NEVER reaches the gate.
          4. Append a research_loop.jsonl audit row; accumulate the summary.

        If `halts` is non-empty → abort immediately (fail-closed), summary.halt_aborted=True,
        nothing run. Mirrors quant-autonomous-tick.py:213-225.

        With flag_on() False this returns an empty summary and writes nothing.
        """
        ...
```

Key implementation notes (load-bearing — a builder must honor these):

1. **Flag-gate at the top of `run_cycle`** — `if not flag_on(): return ResearchLoopSummary(cycle_id=..., flag_on=False)`. No registry reads, no writes. Byte-identical off-state.
2. **Candidate selection = `registry.read_all_open()`** (`hypothesis.py:386`). W3 registers its candidate hypotheses as `status="open"` rows (the only producer of open hypotheses in the system — verified: `HypothesisRegistry.register` at `hypothesis.py:272` is the sole writer). Cap at `max_candidates`; skip any whose `scope` already pins a `window_start` earlier than the knowledge cutoff (defensive — the sentinel will catch it anyway).
3. **OOS window** — prefer `hyp.scope["window_start"]/["window_end"]` if present (ADR-0048 scope dict, `hypothesis.py:145`); else `window_end = today`, `window_start = today − _DEFAULT_OOS_DAYS`. NEVER tune the window to maximize pass-rate (ADR-0080 §D80.3.3, robustness-not-peak).
4. **`strategy_factory` default = StubLLMCommittee-backed** (`hermes_quant/backtest/stub_llm.py:64`) so a `dry_run=True` cycle makes ZERO real LLM calls — the orchestrator's documented contract (`orchestrator.py:204-205`). The cron runs `dry_run=True` by default; `--armed` (real LLM strategy) is opt-in and still never promotes to live.
5. **Lookahead sentinel is the OUTER guard** — W6 does not re-implement it; it RELIES on `orchestrator.py:313-318` forcing `verdict='falsified'` + `contamination_guard_fired=True` on `LookaheadViolation`. A contaminated candidate is counted (`summary.contaminated += 1`) and NEVER reaches the PromotionGate (step 3 short-circuits on `contamination_guard_fired`).
6. **Promotion is PRODUCE-only** — call `promotion_run(strategy=..., universe=..., window_start=..., window_end=..., hypothesis_id=hyp_id, auto_record=True)`. The orchestrator writes a `PromotionRecord` and, per `promotion_orchestrator.py:354-360`, "does NOT transition hypothesis status." W6 adds NO transition beyond what the deterministic auto-eval in `runner.run` already did. A `decision.promote == True` increments `promotions_recommended` for the operator's attention — it does NOT promote.
7. **`factor_eval`** — when supplied, call `FactorOracle.evaluate_all(bars)` once per cycle and log the tier histogram into the audit row (O4 telemetry; verdicts stay display-only — raising a live factor weight is W4, not W6). Default `None` so the core loop has no AlphaZoo/bars dependency.
8. **Audit row** — append one `kind="research_loop_cycle"` summary row + one `kind="candidate_outcome"` row per candidate to `research_loop.jsonl`, append-only, fsync (copy `hypothesis.py:234-241` `_append_row`).

### NEW — `ops/scripts/quant-research-loop.py` (the cron entrypoint)

Thin operator-facing wrapper. Copies the established cron idiom from `quant-autonomous-tick.py` and
`quant-catalyst-profitability.py`:

- **venv re-exec** header (`quant-catalyst-profitability.py:17-19`).
- **`no_agent` silence-by-default:** flag OFF → print nothing, exit 0. Flag ON + a cycle with no
  candidates and no halts → print nothing, exit 0. Only print on a *transition* (a candidate ran, a
  promotion was recommended, a contamination fired, or a halt aborted) — matching the change-detecting
  watchdog contract in CRON-REGISTRY.md §0/§2.
- **`--dry-run` (DEFAULT)** = StubLLMCommittee strategy, zero real LLM cost; **`--armed`** = real LLM
  strategy (still never promotes to live); **`--json`** = single-line JSON summary.
- **`--universe`** arg (default: read the active watchlist via the same `play-fit.json` loader pattern,
  or a fixed research sleeve); `--max-candidates` (default 8).
- **halt fail-closed** before any work (copy `quant-autonomous-tick.py:102-111` + `:213-225`).
- Wrap everything in try/except → last-resort audit + stderr (copy `quant-autonomous-tick.py:480-493`).

```python
#!/usr/bin/env python3
"""quant-research-loop.py — W6 hypothesis→backtest→promote driving cron (ADR-0080).

Schedule (proposed): weekly, AFTER the W3 monthly-meta-retro has had a chance to seed
candidates. e.g. `0 8 * * 1` (11:00 ET Mon) — see CRON-REGISTRY row 19 below.

The cron PRODUCES RunCards + PromotionRecords. It NEVER promotes to live and NEVER flips a
flag — promotion to live influence is an explicit operator action (ADR-0052). Default-OFF
behind HERMES_QUANT_RESEARCH_LOOP; with the flag unset it exits 0, silent, writing nothing.
"""
```

`main()` returns the summary line `research-loop: candidates=N run=M validated=A falsified=B
inconclusive=C contaminated=D promo_records=E promo_recommended=F` (only when non-silent).

### MODIFIED — `docs/operations/CRON-REGISTRY.md`

Add row 19 to the §1 table (the table currently ends at row 18, both NEW/unregistered):

```
| **19** | **research-loop-weekly** *(NEW — not yet registered; default-OFF)* | `0 8 * * 1` | 11:00 ET, Mon | `quant-research-loop.py` | discord:#hq | ✓ | `HERMES_QUANT_RESEARCH_LOOP` (default-OFF) | Weekly: drain W3 candidate hypotheses → OOS backtest + lookahead sentinel → PromotionGate; PRODUCES Run-Cards + PromotionRecords (operator promotes) | silent unless a candidate ran / promotion recommended / contamination / halt |
```

Note in the row's purpose cell that it is a *proposer*: zero auto-promotion to live.

### MODIFIED — `docs/operations/feature-enablement-runbook.md` (if present; else CRON-REGISTRY §5)

Add the flag-flip one-liner + the W6 eval-gate checklist (§4 below) as the precondition to flip
`HERMES_QUANT_RESEARCH_LOOP=1`.

---

## 3. The default-OFF flag-gating idiom (copied from existing code)

Exact pattern, lifted verbatim from `hermes_quant/aggregators/llm_committee.py:977`
(`HERMES_QUANT_RESEARCH_DEBATE`) and `hermes_quant/autonomous.py:394`
(`HERMES_QUANT_PORTFOLIO_CAPS`):

```python
import os

def flag_on() -> bool:
    return os.environ.get("HERMES_QUANT_RESEARCH_LOOP", "0") == "1"
```

- Default value `"0"` → OFF when the env var is absent.
- Strict `== "1"` → only the literal string `"1"` enables it.
- Checked at the **single entry point** (`ResearchLoop.run_cycle` first line AND the cron `main`),
  so the off-state has no side effects and is byte-identical.

---

## 4. Eval gate to flip `HERMES_QUANT_RESEARCH_LOOP=1` (pytest-verifiable acceptance criteria)

The flag flips to ON only after ALL of these pass. Each maps to a test in §5 and to ADR-0080 §D80.3.

1. **Reproducible Run-Cards (config_hash / strategy_hash).** Two cycles over the *same* candidate +
   universe + window + `dry_run` produce RunCards with **identical** `strategy_config_hash`
   (`orchestrator.py:299-301` hashes `{strategy_name, sorted(universe), window_start, window_end,
   dry_run}`). → `test_research_loop_run_cards_reproducible`.
2. **Lookahead sentinel clean (and load-bearing).** A candidate whose strategy raises
   `LookaheadViolation` (`hermes_quant/backtest/engine.py:64`) yields a RunCard with
   `contamination_guard_fired=True` and `verdict="falsified"` (forced by `orchestrator.py:313-318`),
   and that candidate is **counted in `summary.contaminated` and NEVER reaches the PromotionGate**. →
   `test_research_loop_contaminated_candidate_never_promoted`.
3. **ZERO auto-promotion to live.** Across a full cycle — including a `validated` candidate whose
   PromotionGate returns `promote=True` — the registry contains NO operator-only transition the cron
   wrote, no flag is flipped, and the ONLY live-policy-adjacent artifact is a `PromotionRecord` in
   `promotion_decisions.jsonl` (review-only). The hypothesis registry status reflects only the
   deterministic auto-eval verdict, identical to calling `runner.run` directly. →
   `test_research_loop_never_auto_promotes_to_live`.
4. **Byte-identical off-state.** With the flag unset, `run_cycle` returns an empty summary
   (`flag_on=False`, all counters 0), reads no candidates, and writes nothing to any JSONL. →
   `test_research_loop_off_state_is_silent_and_writes_nothing`.
5. **External-truth-only advancement.** A candidate advances to `validated` ONLY when the deterministic
   backtest metrics satisfy its PRE-DECLARED `success_criteria`; no LLM self-score is read. (Inherited
   from `orchestrator.py:304-308`; W6 adds no alternate path.) →
   `test_research_loop_advances_only_on_external_truth`.
6. **Bounded per cycle.** `run_cycle(max_candidates=K)` runs at most K candidates even if more are open
   (budget cap, ADR-0080 §D80.3.4). → `test_research_loop_respects_max_candidates`.
7. **Halt fail-closed.** A non-empty `halts` arg aborts the cycle (`halt_aborted=True`, nothing run,
   nothing written). → `test_research_loop_aborts_on_active_halt`.

---

## 5. Test files (the eval gate, as pytest)

### NEW — `tests/research/test_research_loop.py`

Fixtures mirror `tests/research/test_orchestrator.py:98-110` (registry/run_card_log on `tmp_path`) and
`tests/eval/test_promotion_orchestrator.py:161` (inject a stub `promotion_run` so no STOCKBENCH data feed
is needed). Use the existing `_good_strategy`/`_bad_strategy` shapes
(`tests/research/test_orchestrator.py:62-95`) for deterministic verdicts.

```python
from __future__ import annotations
from datetime import date
from pathlib import Path
import json

import pytest

from hermes_quant.research.hypothesis import Hypothesis, HypothesisRegistry
from hermes_quant.research.orchestrator import HypothesisRunner
from hermes_quant.research.run_card import RunCardLog, RunCardLog as _RCL
from hermes_quant.research.research_loop import ResearchLoop, ResearchLoopSummary


def _open_candidate(registry, *, success=("vs_buyhold_alpha > 0.0",),
                    falsify=("sharpe < 0.0",), ticker="AAPL"):
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
    return {"sharpe": 0.85, "sortino": 1.2, "max_drawdown": -0.05,
            "vs_buyhold_alpha": 0.03, "n_decisions": 30.0, "total_return": 0.12}


def _bad_strategy(universe, window_start, window_end, dry_run=True):
    return {"sharpe": -0.3, "sortino": -0.5, "max_drawdown": -0.22,
            "vs_buyhold_alpha": -0.1, "n_decisions": 15.0, "total_return": -0.08}


def _contaminating_strategy(universe, window_start, window_end, dry_run=True):
    from hermes_quant.backtest.engine import LookaheadViolation
    raise LookaheadViolation("synthetic contamination")


class _StubPromotionRun:
    """Records calls; returns a fake PromotionRecord-like object with a promote flag."""
    def __init__(self, promote=True):
        self.calls = []
        self._promote = promote
    def __call__(self, *, strategy, universe, window_start, window_end,
                 hypothesis_id=None, auto_record=True, **kw):
        self.calls.append(hypothesis_id)
        class _Rec:
            record_id = "prom_stub01"
            decision = {"promote": self._promote, "reasons": [], "suggested_action": "review"}
        return _Rec()


@pytest.fixture
def registry(tmp_path): return HypothesisRegistry(path=tmp_path / "hypotheses.jsonl")

@pytest.fixture
def run_card_log(tmp_path): return RunCardLog(path=tmp_path / "run_cards.jsonl")

@pytest.fixture
def loop(registry, run_card_log, tmp_path):
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    return ResearchLoop(
        registry=registry, runner=runner,
        promotion_run=_StubPromotionRun(promote=True),
        strategy_factory=lambda hyp: _good_strategy,
        audit_path=tmp_path / "research_loop.jsonl",
    )


# ---- Gate criterion 4: byte-identical off-state ----
def test_research_loop_off_state_is_silent_and_writes_nothing(loop, tmp_path, monkeypatch, registry):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_LOOP", raising=False)
    _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.flag_on is False
    assert summ.candidates_run == 0 and summ.outcomes == []
    assert not (tmp_path / "research_loop.jsonl").read_text().strip()  # nothing written


# ---- Gate criterion 1: reproducible Run-Cards ----
def test_research_loop_run_cards_reproducible(loop, run_card_log, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    hid = _open_candidate(registry)
    loop.run_cycle(universe=["AAPL"], window_start=date(2025, 6, 1),
                   window_end=date(2025, 8, 31), dry_run=True)
    cards = run_card_log.read_for_hypothesis(hid)
    assert len(cards) >= 1
    # Same config across an independent run yields the same hash (orchestrator.py:299).
    # (A second open candidate with identical scope/window must hash-match.)
    hid2 = _open_candidate(registry, ticker="AAPL")
    loop.run_cycle(universe=["AAPL"], window_start=date(2025, 6, 1),
                   window_end=date(2025, 8, 31), dry_run=True)
    cards2 = run_card_log.read_for_hypothesis(hid2)
    assert cards[0].strategy_config_hash == cards2[0].strategy_config_hash


# ---- Gate criterion 2: lookahead sentinel clean, contaminated never promoted ----
def test_research_loop_contaminated_candidate_never_promoted(registry, run_card_log, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    stub_promo = _StubPromotionRun(promote=True)
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    loop = ResearchLoop(registry=registry, runner=runner, promotion_run=stub_promo,
                        strategy_factory=lambda hyp: _contaminating_strategy,
                        audit_path=tmp_path / "research_loop.jsonl")
    hid = _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.contaminated == 1
    card = run_card_log.read_for_hypothesis(hid)[0]
    assert card.contamination_guard_fired is True and card.verdict == "falsified"
    assert stub_promo.calls == []  # PromotionGate NEVER reached


# ---- Gate criterion 3: ZERO auto-promotion to live ----
def test_research_loop_never_auto_promotes_to_live(loop, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    hid = _open_candidate(registry)  # _good_strategy → validated → gate promote=True
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.promotions_recommended >= 1     # gate said promote=True ...
    assert summ.promotion_records >= 1          # ... and a record was PRODUCED
    # ... but the registry status is exactly the deterministic auto-eval verdict
    # (validated via external-truth criteria), NOT an operator promotion. No flag flip.
    assert registry.read(hid).status == "validated"
    # No "promoted_to_live"/operator transition row exists — W6 wrote none.


# ---- Gate criterion 5: external-truth-only advancement ----
def test_research_loop_advances_only_on_external_truth(registry, run_card_log, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)
    loop = ResearchLoop(registry=registry, runner=runner,
                        promotion_run=_StubPromotionRun(),
                        strategy_factory=lambda hyp: _bad_strategy,  # fires falsification
                        audit_path=tmp_path / "research_loop.jsonl")
    hid = _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True)
    assert summ.falsified == 1 and registry.read(hid).status == "falsified"


# ---- Gate criterion 6: bounded per cycle ----
def test_research_loop_respects_max_candidates(loop, registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    for _ in range(5):
        _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True, max_candidates=2)
    assert summ.candidates_run == 2


# ---- Gate criterion 7: halt fail-closed ----
def test_research_loop_aborts_on_active_halt(loop, registry, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_LOOP", "1")
    _open_candidate(registry)
    summ = loop.run_cycle(universe=["AAPL"], dry_run=True,
                          halts=[{"reason": "operator halt", "scope": "global"}])
    assert summ.halt_aborted is True and summ.candidates_run == 0
    assert not (tmp_path / "research_loop.jsonl").read_text().strip()
```

### NEW — `tests/ops/test_quant_research_loop_cron.py` (smoke; the cron wrapper)

- `test_cron_off_state_silent`: invoke `main()` via `subprocess`/import with the flag unset and an open
  candidate present → exit 0, empty stdout.
- `test_cron_dry_run_no_real_llm`: flag ON, `--dry-run` → the default `strategy_factory` uses
  `StubLLMCommittee` (assert no network / monkeypatched LLM client is never called), exit 0.
- `test_cron_json_summary_shape`: `--json` emits a single parseable JSON line with the summary keys.

Run gate: `pytest tests/research/test_research_loop.py tests/ops/test_quant_research_loop_cron.py -q`
must be green. Full-suite must stay green (`pytest -q`) — heed the documented order-dependent-pollution
note (task #12): the loop must use injected `tmp_path` for all JSONL paths, never the real `~/.hermes`.

---

## 6. Build order (for the executing agent)

1. `hermes_quant/research/research_loop.py` — `ResearchLoop` + dataclasses + `flag_on()`. No cron concerns.
2. `tests/research/test_research_loop.py` — write the 7 gate tests FIRST (TDD); make them pass.
3. `ops/scripts/quant-research-loop.py` — thin wrapper (venv re-exec, halt fail-closed, silence-by-default, argparse).
4. `tests/ops/test_quant_research_loop_cron.py` — cron smoke tests.
5. `docs/operations/CRON-REGISTRY.md` — add row 19 (default-OFF, not-yet-registered).
6. Verify: `pytest tests/research/test_research_loop.py tests/ops/test_quant_research_loop_cron.py -q` green; `pytest -q` green; `ruff check hermes_quant/research/research_loop.py ops/scripts/quant-research-loop.py`.

**Do NOT** register the cron (the building agent has no `cronjob` tool — CRON-REGISTRY §0). **Do NOT**
flip the flag. Both are explicit operator actions after the eval gate (§4) passes.

---

## 7. Invariants the reviewer must confirm (the rail, restated)

- W6 writes ONLY to the advisory plane (`hypotheses.jsonl` lifecycle, `run_cards.jsonl`,
  `factor_verdicts.jsonl`, `promotion_decisions.jsonl`, `research_loop.jsonl`). It imports nothing from
  the risk gate / sizing ladder / kill-switch for mutation.
- The ONLY path from a candidate to live policy runs through the OUTER standard-of-truth + a separate
  operator promotion (ADR-0052; `promotion_orchestrator.py:354-360`). W6 produces records; it never
  promotes and never flips a flag.
- The lookahead sentinel (`orchestrator.py:313-318`) is load-bearing and untouched: contaminated runs are
  forced `falsified` and can never reach the PromotionGate.
- Off-state is byte-identical; the cron is silent-by-default (no_agent contract).
