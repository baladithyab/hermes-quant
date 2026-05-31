# W4 — Factor-verdict → BMA-weight proposer (silence-only) — implementation-ready plan

**Date:** 2026-05-30
**Status:** ready-to-execute
**Wave:** W4 (self-evolution rollout, capability-map §4 / ADR-0080 §D80.6)
**Closes:** O4 (factor half of evolve: `factor_verdicts.jsonl` is display-only today). **Unblocks** O6 (BMA Beta-posterior learning) — documents the `slippage_only` unblock seam; does NOT lift it (that ships with v0.1.2 entry+exit fill joining).
**Flag:** `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER` (default-OFF, byte-identical off-state).
**Depends-on:** W1 (`08326e1`, shipped). **Parallelizable** with W2/W3/W5/W7 once W1 lands. Independent of W2/W3.
**Grounded in:** `docs/research/2026-05-30-selfevolve-capability-map.md` §4 (W4 spec, lines 113–118), §5 (safety frame); `docs/adr/ADR-0080-self-evolution-framework.md` §D80.1–D80.6 (advisory plane, eval-gate contract, propose-only invariant).

> **Posture (non-negotiable, inherited from ADR-0080 §D80.1–D80.3).** This wave writes to the **ADVISORY PLANE only** — a *candidate-weight diff* on disk that NOTHING reads into live policy. It PROPOSES; the deterministic risk gate (ADR-0004), the hard risk limits, the discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}`, and the kill-switch sit OUTSIDE this loop and are IMMUTABLE by it. The only path from a proposed weight to live policy is the existing operator/eval-gated promotion machinery (ADR-0052). Silence-only: a `premium` verdict raises weight **within a hard cap**; a `rejected` verdict drives weight **toward 0**; the proposer NEVER amplifies above the cap. External-truth scored only (realized OOS DSR/walk-forward from market data — never an LLM self-score, never the proposer's own output re-ingested).

---

## 0. State-of-the-world (verified against HEAD, `file:line`)

| Fact | Evidence |
|---|---|
| `FactorOracle.evaluate_all(bars)` evaluates every registered factor → `{factor_id: FactorVerdict}`; each verdict carries `tier ∈ {premium, standard, experimental, rejected}` + `ic_panel` dict (`ic_mean`, `icir`, `hit_rate`, `turnover`, `n_periods`). | `hermes_quant/factors/factor_oracle.py:450-478`; `FactorVerdict` model `:191-225`; tier thresholds `:105-183`. |
| Verdicts are **append-only display-only**: `_append_verdict` writes `factor_verdicts.jsonl` (`:274-282`); `latest_verdict` (`:507-509`) + `AlphaZoo.verdict_for` (`alpha_zoo.py:468-487`) are read-only bridges. **No live factor weight responds to a verdict** — grep for `verdict_for`/`latest_verdict` consumers outside the oracle returns only `cli/status.py` + `reporting/daily_report.py` (display). | repo-wide grep, §0 of this plan. This is exactly O4. |
| There is **no existing `factor_weights` map applied to a portfolio.** | grep `factor_weight\|FactorWeight\|candidate_weight` in `hermes_quant/` returns nothing. W4 therefore creates the advisory artifact from scratch; it does NOT mutate an existing live weight surface (there is none). |
| **The seed of R4 already in code:** `catalyst/profitability.py` is SkillOpt's held-out gate instantiated once — per-relation-class `PROFITABLE / UNPROFITABLE_CONSIDER_PRUNE / INSUFFICIENT_SAMPLE / MARGINAL_HOLD`, `MIN_SAMPLE=20`, `MIN_HIT_RATE=0.6`. | `hermes_quant/catalyst/profitability.py:32-62`. W4 generalizes this verdict→silence-only-weight pattern from one relation class to the whole factor surface (ADR-0080 More-Information §"The seed already in code"). |
| **The honesty rails to mirror** are codified in `graph_mining.py` (DESIGN-ONLY, B10): PROPOSE-only, never auto-mutate the curated artifact, `confidence_multiplier` SILENCE-ONLY (`<= 1.0`, can pull toward 0, never amplify), change-detecting no_agent watchdog cron. | `hermes_quant/catalyst/graph_mining.py:41-66`. W4 copies this rail verbatim onto factor weights. |
| **O6 is blocked upstream, not by W4.** `settlement_loop` tags every single-fill outcome `_calibration_quality="slippage_only"` and `dispatch_settlement` SKIPS `analyst.update()` + `aggregator.update()` for those tags. | docstring `daemon/settlement_loop.py:35-42`; the actual skips at `:315-317` (realized) and `:335-337` (episode); constants `:77-78` (`CALIBRATION_QUALITY_SLIPPAGE_ONLY`, `CALIBRATION_QUALITY_HORIZON_RETURN = "horizon_return"  # v0.1.2+`). BMA `update()` (`aggregators/bma.py:638-656`) is correct and waiting; it only ever runs when the gate lifts. |
| **DSR + walk-forward eval surface exists.** `deflated_sharpe(observed_sharpe, *, n_trials, n_observations, skew, kurtosis)` returns P(Sharpe is not a false discovery). `WalkForwardBacktestResult` carries `mean_sharpe_delta`, `positive_excess_fold_rate`, per-fold OOS results. | `hermes_quant/evaluation/dsr.py:21-96`; `hermes_quant/backtest/walk_forward.py:48-110`. The eval gate reuses these — no new metric code. |
| **The default-OFF flag idiom** is read-at-call-time (so tests monkeypatch env): `os.environ.get("HERMES_QUANT_X", "0") == "1"`. | canonical examples: `react/multileg.py:102`, `options_gate.py:377`, `alpha_zoo.py:331` (IC-dedup default-OFF; the closest sibling — a factor-surface gate). |
| **W1 (the dependency) is shipped** and gives the pattern for a default-OFF advisory-plane side-effect: `maybe_record_decision_on_open` writes a `pending` row only under `HERMES_QUANT_REFLECTION=1`, never raises, byte-identical off-state. | `hermes_quant/memory/_paper_reflection_hook.py:27-60`; call-site `react/paper.py:245-254`. W4's proposer mirrors this "best-effort, default-OFF, never touches the hot path" discipline. |
| **The change-detecting no_agent watchdog cron pattern** to copy: load a per-key baseline JSON, project current state, diff transitions, save baseline, emit ONLY on a transition (silent otherwise). | `ops/scripts/quant-catalyst-profitability.py:30-138` (baseline at `:31`, `_transitions` `:92-111`, silent-unless-changed `main` `:114-138`). |
| **The promotion-readiness gate field** O3 writes is `weekly_retro_promotion_readiness` (a `bool` the gate ANDs in). W4 does NOT write it — W2 does — but W4's proposal artifacts ARE the candidate set an operator promotes through `governance/promotion.py`. | `governance/promotion.py:82,158-159,235-236`. Documented here so W4's output slots into the existing operator-promotion path, not a new one. |

**Net:** W4 reads a producer that already runs (`evaluate_all`), applies the proven `profitability.py` verdict→action pattern with the proven `graph_mining.py` honesty rails, writes ONE new advisory artifact (a candidate weight diff JSON), and gates promotion on the existing DSR/walk-forward eval surface. It builds two files and one cron. It mutates nothing live.

---

## 1. What W4 may write vs. must never touch (the safety frame, applied)

Per ADR-0080 §D80.1 (the two planes) and capability-map §5 (the two lists):

**MAY write (ADVISORY PLANE only):**
- A `FactorWeightProposal` set — a list of `(factor_id, current_weight, proposed_weight, verdict_tier, reason)` rows — serialized to a single candidate-diff JSON at `~/.hermes/quant/factors/weight-candidates.json`. This file is the advisory plane. **Nothing in the trading hot path reads it.**
- A `rejected-buffer` JSONL at `~/.hermes/quant/factors/weight-rejected-buffer.jsonl` (SkillOpt rejected-edit buffer) so a losing config is not re-proposed.
- A per-factor watchdog baseline JSON (cron-internal state, same role as `profitability-baseline.json`).

**MUST NEVER touch (outside the loop, immutable by it):**
- The deterministic risk gate (ADR-0004) and the hard risk limits (max loss, position caps, exposure).
- The discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}` — no continuous re-optimization of sizes; the ladder is fixed.
- The kill-switch (a separate process the agent runtime cannot signal).
- Any live config: the proposer NEVER writes `registry.json`, NEVER edits a strategy config, NEVER calls `aggregator.update()`. It does not even have a live "factor weight" target to write to (none exists — §0).

**Silence-only invariant (ADR-0080 §D80.5, mirrors `graph_mining.py:50-52`):** the proposed weight is clamped `0.0 <= proposed_weight <= WEIGHT_CAP`. A `premium` verdict raises the proposed weight toward the cap; `rejected` drives it toward 0; the multiplier can pull an over-weighted factor *down* but can NEVER push above `WEIGHT_CAP`. There is no amplification path.

**External-truth-only (ADR-0080 §D80.3):** the eval gate scores the proposed weight set on realized OOS DSR / walk-forward from market data (`bars`). The proposer's own verdict text is never the grading signal. The proposer cannot author the number that grades it.

---

## 2. New / modified files (exact)

### NEW — `hermes_quant/factors/weight_proposer.py` (~180 LoC)

The library: pure, offline, deterministic, no network, no env-read at import. Reads verdicts (in-memory dict from `evaluate_all`, or replayed from `factor_verdicts.jsonl`), emits a `FactorWeightProposalSet`, runs the held-out eval gate, applies checkpoint-fallback + plateau selection.

```python
"""hermes_quant.factors.weight_proposer — W4 factor-verdict → candidate BMA-weight proposer.

SILENCE-ONLY, PROPOSE-ONLY (capability-map §4 W4 / ADR-0080 §D80.1, §D80.5).

Generalizes the catalyst/profitability.py seed (per-relation-class verdict → raise/prune)
to the whole factor surface: a FactorOracle 4-tier verdict (premium/standard/experimental/
rejected) maps to a CANDIDATE weight diff. premium↑ within WEIGHT_CAP; rejected→silence-toward-0.

The output is an ADVISORY-PLANE artifact only. Nothing in the trading hot path reads it.
Promotion to any live weight is the operator/eval-gated promotion action (ADR-0052), never this
module. Mirrors graph_mining.py honesty rails: PROPOSE only, never auto-mutate a curated
artifact, confidence multiplier silence-only (<= cap, never amplifies).

External-truth: the eval gate scores a proposed set on realized OOS DSR / walk-forward from
market bars (evaluation/dsr.py + backtest/walk_forward.py). Never an LLM self-score; never the
proposer's own verdict re-ingested as truth (ADR-0080 §D80.3).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Hard caps (the silence-only rail; never widened by the loop) -----------
WEIGHT_CAP: float = 1.0          # a factor's candidate weight is clamped to [0, CAP]. NEVER amplifies above.
WEIGHT_FLOOR: float = 0.0        # rejected → silence-toward-0.
MAX_STEP_PER_CYCLE: float = 0.10 # bounded per-cycle change (SkillOpt textual learning-rate analog).
MIN_OBSERVATIONS: int = 30       # DSR is meaningless below this (dsr.py raises < 30); mirror it here.
# Tier → target weight (the proposal direction). premium gets the most headroom; rejected → 0.
_TIER_TARGET: dict[str, float] = {
    "premium": 1.00,
    "standard": 0.60,
    "experimental": 0.30,
    "rejected": 0.00,    # silence-toward-0
}

_DEFAULT_DIR = Path.home() / ".hermes" / "quant" / "factors"
_CANDIDATES_FILE = "weight-candidates.json"
_REJECTED_BUFFER = "weight-rejected-buffer.jsonl"


@dataclass(frozen=True)
class FactorWeightProposal:
    factor_id: str
    current_weight: float
    proposed_weight: float
    verdict_tier: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "factor_id": self.factor_id,
            "current_weight": round(self.current_weight, 4),
            "proposed_weight": round(self.proposed_weight, 4),
            "verdict_tier": self.verdict_tier,
            "reason": self.reason,
        }


@dataclass
class FactorWeightProposalSet:
    proposals: list[FactorWeightProposal] = field(default_factory=list)
    generated_at: str = ""
    # eval-gate provenance — filled by evaluate_against_holdout, read by the operator/promotion gate.
    held_out_dsr: float | None = None
    held_out_sharpe_delta: float | None = None
    prior_best_dsr: float | None = None
    beats_prior_best: bool | None = None
    plateau_stable: bool | None = None
    eval_passed: bool = False

    def to_dict(self) -> dict: ...   # full serialization incl. eval provenance


def _clamp(w: float) -> float:
    """Silence-only clamp: [WEIGHT_FLOOR, WEIGHT_CAP]. NEVER returns > WEIGHT_CAP."""
    return max(WEIGHT_FLOOR, min(WEIGHT_CAP, w))


def propose_weights(
    verdicts: dict,                       # {factor_id: FactorVerdict} from FactorOracle.evaluate_all
    current_weights: dict[str, float] | None = None,
) -> FactorWeightProposalSet:
    """Map 4-tier verdicts → a CANDIDATE weight diff. Pure, deterministic, silence-only.

    For each factor: target = _TIER_TARGET[tier]; proposed = current + clamp(step toward target,
    |step| <= MAX_STEP_PER_CYCLE); then _clamp to [FLOOR, CAP]. rejected drives toward 0.
    A factor with n_periods < MIN_OBSERVATIONS is left at current_weight (INSUFFICIENT — no move),
    mirroring profitability.py INSUFFICIENT_SAMPLE.
    """


def evaluate_against_holdout(
    proposal_set: FactorWeightProposalSet,
    *,
    holdout_dsr: float,                   # realized OOS DSR of the PROPOSED weight set (caller computes from bars)
    holdout_sharpe_delta: float,          # OOS Sharpe delta vs benchmark of the proposed set
    prior_best_dsr: float,                # the prior-best checkpoint's held-out DSR (checkpoint-fallback baseline)
    plateau_stable: bool,                 # jitter-stable plateau across folds (robustness-not-peak)
) -> FactorWeightProposalSet:
    """Apply the universal eval-gate contract (ADR-0080 §D80.3). Sets eval_passed.

    eval_passed iff ALL of:
      (1) held-out scored (holdout_dsr from market data — caller guarantees external-truth);
      (2) STRICTLY beats prior-best on held-out: holdout_dsr > prior_best_dsr (checkpoint-fallback —
          if not, revert: the returned set keeps proposals but eval_passed=False so the operator
          does NOT promote, and the set is appended to the rejected buffer);
      (3) robustness-not-peak: plateau_stable is True;
      (4) bounded: every proposed_weight in [FLOOR, CAP] (asserted; a violation is a hard error).
    Propose-only (5) is structural: this function never applies anything; it only annotates.
    """


def write_candidates(proposal_set: FactorWeightProposalSet, *, path: Path | None = None) -> Path:
    """Write the ADVISORY-PLANE candidate diff JSON. Atomic write. Never touches live config."""


def append_rejected(proposal_set: FactorWeightProposalSet, *, path: Path | None = None) -> None:
    """SkillOpt rejected-edit buffer: a set that fails the gate is recorded so it is not re-proposed."""


def load_prior_best_dsr(*, path: Path | None = None) -> float:
    """Read the prior-best checkpoint DSR for checkpoint-fallback. Missing → -inf (first run: any pass strictly beats)."""
```

Key design points (each maps to a safety primitive):
- `_clamp` is the structural silence-only guarantee — the cap cannot be exceeded by construction (D80.5, mirrors `graph_mining.py:50-52`).
- `MAX_STEP_PER_CYCLE = 0.10` bounds per-cycle change (SkillOpt textual-learning-rate; capability-map §3).
- `evaluate_against_holdout` is the *only* place `eval_passed` is set, and it requires **strictly** beating prior-best (`>`, not `>=`) — checkpoint-fallback (D80.5 / capability-map §5 primitive 3). A non-strict pass reverts.
- `plateau_stable` is a caller-supplied input computed from cross-fold jitter (robustness-not-peak; the cron computes it from `WalkForwardBacktestResult` folds, NOT from the in-sample peak — directly applying the AMZN-weight lesson at `ops/scripts/quant-amzn-weight-oos.py:79-84`).
- The function NEVER applies — it annotates. The propose-only invariant (D80.7) is structural, not policy.

### NEW — `ops/scripts/quant-factor-weight-propose.py` (~150 LoC)

The weekly cron. Default-OFF, change-detecting no_agent watchdog (copies `quant-catalyst-profitability.py` structure exactly). Pseudostructure:

```python
"""quant-factor-weight-propose.py — W4 weekly factor-weight proposer cron (DEFAULT-OFF).

Flag-gated by HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1 (default-OFF: byte-identical no-op when unset).
Runs FactorOracle.evaluate_all on real OHLCV bars, maps verdicts → a CANDIDATE weight diff
(silence-only, capped), scores the proposed set on a held-out OOS DSR / walk-forward window the
proposer never saw, and writes an ADVISORY-PLANE candidate JSON for OPERATOR review. Promotes
NOTHING. Mirrors the catalyst-profitability watchdog: silent unless a factor crosses a tier
boundary or the eval verdict flips.

External-truth: forward returns / OOS DSR come from market bars; the proposer never sees them at
propose time. Honesty rails = graph_mining.py. Promotion path = operator + ADR-0052 only.
"""
from __future__ import annotations
import os, sys
from pathlib import Path

# venv re-exec (copy quant-catalyst-profitability.py:17-19 verbatim)
_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

# DEFAULT-OFF flag gate (read at call time). Off-state = byte-identical no-op.
if os.environ.get("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "0") != "1":
    sys.exit(0)   # silence-by-default; the cron is a no-op until explicitly enabled

def main() -> int:
    # 1. Build the factor surface + real bars (yfinance fetch helper copied from
    #    quant-catalyst-profitability.py:34-60 / amzn-weight-oos basket pattern).
    # 2. verdicts = FactorOracle(zoo).evaluate_all(bars_train)   # T2 trigger of evaluate_all
    # 3. proposals = propose_weights(verdicts, current_weights=_load_current_or_zero())
    # 4. HELD-OUT: run WalkForward / OOS DSR on the PROPOSED set vs prior-best on the OOS slice
    #    the optimizer never saw; compute plateau_stable from cross-fold jitter (NOT the IS peak).
    # 5. proposals = evaluate_against_holdout(proposals, holdout_dsr=..., prior_best_dsr=load_prior_best_dsr(),
    #                                         holdout_sharpe_delta=..., plateau_stable=...)
    # 6. if proposals.eval_passed: write_candidates(proposals); update prior-best checkpoint.
    #    else:                     append_rejected(proposals)   # checkpoint-fallback: do NOT write candidates
    # 7. Change-detecting watchdog: load baseline, diff tier transitions + eval-verdict flip,
    #    save baseline, print ONLY on a transition (silent otherwise — no_agent contract).
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Cron registration (operator-applied, documented here): job `quant-factor-weight-propose-weekly`, `0 7 * * 6` (Sat 07:00 PT — AFTER the catalyst-graph-mine slot at 06:00, so the two weekly miners don't collide), `deliver=origin` no_agent. Silent unless a factor crosses a tier boundary or the eval verdict flips (same change-detecting watchdog as coverage + profitability). The cron is a no-op until `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1` is set in the env.

### NOT modified (deliberate — documents the O6 unblock seam, does not act on it)

`hermes_quant/daemon/settlement_loop.py` is **left unchanged**. The `slippage_only` gate (`:315-317`, `:335-337`) that blocks `aggregator.update()` (BMA Beta-posterior learning) is the O6 blocker, and it is blocked **upstream** on v0.1.2 entry+exit fill joining — not on W4. **The unblock seam (for whoever lands v0.1.2):** when `construct_realized_outcomes` / `construct_episode_outcomes` can compute a true horizon return (entry+exit joined), set `_calibration_quality = CALIBRATION_QUALITY_HORIZON_RETURN` (`:78`) instead of `..._SLIPPAGE_ONLY`; `dispatch_settlement`'s two skip branches then fall through and `aggregator.update(episode)` (`:339`) runs, evolving the per-analyst Beta posteriors that `BMAAggregator._weight_for` (`bma.py:289-295`) already consumes. **BMA persistence is also missing** (posteriors are per-process only — no on-disk save in `bma.py`); v0.1.2 must add a persist/load of `_stats` for the learned weights to survive a restart. W4 does not touch any of this; it only documents the seam so the factor-weight half (this wave) and the BMA-weight half (O6) meet cleanly later.

---

## 3. The default-OFF flag-gating idiom (copy verbatim)

Two enforcement points, both read-at-call-time so tests can monkeypatch env (the canonical project idiom — `alpha_zoo.py:331`, `react/multileg.py:102`, `options_gate.py:377`):

1. **Cron entry (hard no-op):** at the top of `quant-factor-weight-propose.py`, after venv re-exec:
   ```python
   if os.environ.get("HERMES_QUANT_FACTOR_WEIGHT_PROPOSER", "0") != "1":
       sys.exit(0)
   ```
   This is the byte-identical off-state (ADR-0080 §D80.8): with the flag unset, the cron exits 0 having read nothing, written nothing.

2. **Library is flag-agnostic by design:** `weight_proposer.py` reads NO env var (mirrors `profitability.py`, which is pure). The flag lives at the cron boundary only. This keeps the library unit-testable without env juggling and matches the W1 split (`_paper_reflection_hook.py` does the work; the reactor reads `HERMES_QUANT_REFLECTION` at the call-site `paper.py:242`).

---

## 4. The eval gate (pytest-verifiable acceptance criteria)

The universal eval-gate contract (ADR-0080 §D80.3) instantiated for W4. Every item below is a unit test. The gate to flip `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1` live is: **all tests green AND an operator audits the candidate JSON.**

### NEW — `tests/unit/test_factor_weight_proposer.py`

| # | Acceptance criterion (test name) | Asserts |
|---|---|---|
| AC-1 | `test_silence_only_never_amplifies_above_cap` | For any verdict mix and any `current_weights` (incl. weights already at `WEIGHT_CAP`), every `proposed_weight <= WEIGHT_CAP` and `>= WEIGHT_FLOOR`. Property-style over random verdict sets. **The silence-only rail (D80.5).** |
| AC-2 | `test_rejected_verdict_drives_toward_zero` | A `rejected` verdict yields `proposed_weight < current_weight` (toward 0), never up. |
| AC-3 | `test_premium_raises_within_cap_bounded_step` | A `premium` verdict raises weight but by `<= MAX_STEP_PER_CYCLE` per cycle, never exceeding `WEIGHT_CAP`. |
| AC-4 | `test_insufficient_observations_no_move` | A factor whose `ic_panel.n_periods < MIN_OBSERVATIONS` keeps `proposed_weight == current_weight` (mirrors `profitability.py` INSUFFICIENT_SAMPLE). |
| AC-5 | `test_eval_gate_requires_strictly_beat_prior_best` | `evaluate_against_holdout` sets `eval_passed=True` only when `holdout_dsr > prior_best_dsr` (STRICT). `holdout_dsr == prior_best_dsr` → `eval_passed=False` (checkpoint-fallback: a tie reverts). **D80.3 #3.** |
| AC-6 | `test_eval_gate_requires_plateau_stable` | With `holdout_dsr > prior_best_dsr` but `plateau_stable=False`, `eval_passed=False` (robustness-not-peak: a sharp in-sample peak that isn't jitter-stable across folds is rejected — the AMZN-weight lesson). |
| AC-7 | `test_failed_eval_appends_to_rejected_buffer_not_candidates` | A set with `eval_passed=False` is written to `weight-rejected-buffer.jsonl` and `write_candidates` is NOT called (the cron path). SkillOpt rejected-edit buffer + checkpoint-fallback. |
| AC-8 | `test_passed_eval_writes_advisory_candidates_only` | A passing set writes `weight-candidates.json` and touches NO live config file (assert `registry.json` / any strategy config unchanged; assert no `aggregator.update` import is exercised). **Advisory-plane-only (D80.1).** |
| AC-9 | `test_proposer_reads_no_env_and_is_pure` | Importing + calling `propose_weights` / `evaluate_against_holdout` with env scrubbed produces identical output (library is flag-agnostic; flag lives at the cron). |
| AC-10 | `test_external_truth_only_no_self_score` | The eval-gate inputs are numeric OOS metrics; assert there is no code path where a verdict's `reason` text or `tier` feeds back into `holdout_dsr` (the proposer cannot grade itself — D80.3 #1). Structural test: `evaluate_against_holdout` signature takes only floats/bool, never the proposal's own tier. |

### NEW — `tests/unit/test_quant_factor_weight_propose_cron.py`

| # | Acceptance criterion | Asserts |
|---|---|---|
| AC-11 | `test_cron_is_noop_when_flag_off` | With `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER` unset/`"0"`, invoking the cron `main`/entry exits 0 and writes no candidate file (byte-identical off-state, D80.8). Use `monkeypatch.delenv` + a tmp `~/.hermes` and assert the candidates file does not exist. |
| AC-12 | `test_cron_silent_unless_transition` | With the flag on but no tier boundary crossed and no eval-verdict flip vs the saved baseline, stdout is empty (no_agent watchdog — copies `quant-catalyst-profitability.py` transition logic). |
| AC-13 | `test_cron_emits_on_tier_transition` | When a factor crosses a tier boundary vs baseline, the cron prints exactly one transition summary line, then the table. |

### REUSE / regression — existing suites must stay green
- `tests/` for `factor_oracle`, `alpha_zoo`, `dsr`, `walk_forward`, `governance/promotion` — W4 adds files; it modifies none, so these are pure regression. Run the full unit suite; the wave is additive (ADR-0080 "additive and reversible").
- A `settlement_loop` regression test must confirm the `slippage_only` skip is **still** in place (W4 must NOT have lifted it): assert `dispatch_settlement` skips a `slippage_only`-tagged outcome (`n_skipped_slippage_only > 0`, `n_aggregator_updates == 0`). This guards O6 staying upstream-blocked.

---

## 5. Build order (single PR, ~4 files)

1. `hermes_quant/factors/weight_proposer.py` (library: `propose_weights`, `evaluate_against_holdout`, `write_candidates`, `append_rejected`, `load_prior_best_dsr`).
2. `tests/unit/test_factor_weight_proposer.py` (AC-1..AC-10) — TDD: write alongside (1).
3. `ops/scripts/quant-factor-weight-propose.py` (cron, default-OFF, watchdog).
4. `tests/unit/test_quant_factor_weight_propose_cron.py` (AC-11..AC-13) + the settlement-loop O6-still-blocked regression assert.

No existing file is modified. The cron registration line is operator-applied, documented in §2.

---

## 6. Propose-only — the explicit statement

W4 PROPOSES. It writes a candidate weight diff to an advisory-plane JSON that nothing in the trading hot path reads. There is no auto-apply, no env flag that promotes, no edit to any live weight (there is no live factor-weight surface to edit — §0). The **only** path from a W4 proposal to live policy is: operator reads `weight-candidates.json` → operator runs the existing promotion machinery (`governance/promotion.py` / ADR-0052 `PromotionOrchestrator`, which is operator-action-only by design) → the deterministic risk gate (ADR-0004) remains the FINAL authority on any resulting trade. The risk gate, hard limits, the discrete sizing ladder, and the kill-switch are structurally outside this loop and immutable by it (ADR-0080 §D80.1, the defining invariant). Eval-gaming is recursive: the held-out gate is necessary, not sufficient — the operator-in-the-loop promotion never goes away (ADR-0080 §Consequences).
