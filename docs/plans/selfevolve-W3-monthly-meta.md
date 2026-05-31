# W3 — Monthly Meta-Retro (the missing T3 tier)

**Status:** implementation-ready plan (build with no further research)
**Date:** 2026-05-30
**Wave:** W3 (self-evolution rollout; depends on W2)
**Flag:** `HERMES_QUANT_MONTHLY_META_RETRO` (default-OFF, byte-identical off-state)
**Closes:** O7 (`research_debate` audit rows, write-only today) + O8 (`HypothesisRunner` RunCards, display-only today)
**Grounds:**
- `docs/research/2026-05-30-selfevolve-capability-map.md` §4 W3 (the wave spec), §2 (O7/O8), §5 (safety frame)
- `docs/adr/ADR-0081-belief-store-and-distillation-tiers.md` §3 "Monthly meta tier" + §4 deterministic promote/expire (READ — this is the decision record this plan implements)
- `docs/adr/ADR-0080-self-evolution-framework.md` D80.2 (T3 row), D80.3 (universal eval-gate), D80.5 (propose-only)

> **One sentence.** W3 is a deterministic monthly cron that *reads* the now-live W2 weekly belief digests, the `research_debate`/`promotion_event` audit rows, and the RunCard/hypothesis registry, and *writes* exactly three advisory-plane artifacts — (1) a meta-retro report, (2) **candidate** hypotheses (novelty/dedup-gated, registered `status="open"`, never auto-run), and (3) **persona-calibration telemetry** (proposed persona weights, telemetry-only). It PROPOSES; it never touches a limit, a size, the gate, the kill-switch, or live policy. The deterministic risk gate + operator-gated promotion stay the sole path to live.

---

## 0. What W3 may write vs. must never touch (the SAFETY frame, applied)

This is non-negotiable and inherited verbatim from capability-map §5 / ADR-0080 D80.1.

**MAY write (advisory plane only):**
| Artifact | Path | Plane object | Authority over live policy |
|---|---|---|---|
| Meta-retro report | `~/.hermes/quant/memory/meta_retros.jsonl` (append-only) | report (recommendations-only) | **none** |
| Candidate hypotheses | `HypothesisRegistry` → `~/.hermes/quant/research/hypotheses.jsonl`, registered `status="open"`, `author="quant-monthly-meta-retro"` | hypothesis | **none** — a human/`HypothesisRunner` (W6) must move it `open→running` |
| Persona-calibration telemetry | inside the meta-retro report row, field `persona_calibration` | candidate persona weights, **telemetry-only** | **none** — never read by any aggregator until ≥M months agree |
| Belief promote/expire (weekly→monthly) | `~/.hermes/quant/memory/beliefs.jsonl` (the W2 store; append-only `expired`/promoted rows) | belief | **none** — only changes which beliefs the retriever *may surface*, still Oracle-guarded |

**MUST NEVER touch (outside the loop, immutable by it — ADR-0080 D80.1):**
- The deterministic risk gate (ADR-0004) and the hard risk limits (max loss, position caps, exposure).
- The discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}`.
- The kill-switch (a separate process the runtime cannot signal).
- Any **live** factor/BMA/persona weight, the seed catalyst YAML, or any code. (W3 emits *proposed* persona weights as telemetry; the aggregator at `hermes_quant/aggregators/deliberative.py` and `.../bma.py` are NOT modified by W3 and never read `persona_calibration`.)
- `reflections.jsonl` and `beliefs.jsonl` are NEVER re-ingested as ground truth. Every meta-retro input that originated from the agent (beliefs, debate rationales, reflection text) is carried with its Oracle provenance and the `tau_observable < asof` guard is preserved end-to-end (ADR-0081 §1; see §5).

**Propose-only invariant (state it in code + in the report):** the meta-retro's `candidate_hypotheses` are registered `status="open"` and `persona_calibration` is `telemetry_only=true`. The ONLY path from either to live policy runs through the existing operator/eval-gated promotion machinery (`HypothesisRunner` W6 → `FactorOracle` → `PromotionOrchestrator` → operator sign-off). W3 closes a loop by *producing evidence*, not by *applying* it.

---

## 1. Verified seams (grepped `file:line`; do NOT rebuild these)

| Seam | Location | What W3 does with it |
|---|---|---|
| `research_debate` audit rows (persona source) | `hermes_quant/agents/research_debate/stage.py:345` emits `kind="research_debate"`; payload carries `bull_turns_summary`/`bear_turns_summary` each `{stance, confidence, rationale_chars}`, `final_recommendation`, `proposal_id`, `asset`, `asof` | **READ** via `audit_log.read(kinds=["research_debate"])`. Join each row's `final_recommendation` (and bull/bear `confidence`) to realized alpha → persona calibration. |
| Audit-log read API | `hermes_quant/governance/audit_log.py:150` `read(since=None, kinds=None)` yields `GovernanceEvent`; `VALID_KINDS` at `audit_log.py:50` | **READ** debate + `promotion_event` + `fill` rows. **No new EventKind needed** for W3 (report goes to its own JSONL, telemetry rides inside it; promotion stays W2's job). |
| `promotion_event` rows | written by `hermes_quant/governance/promotion.py:245`; W2 writes `weekly_retro_promotion_readiness` here | **READ** to count promotion-readiness flips per month (a meta signal: "did the weekly tier keep clearing?"). W3 does **not** write this field — W2 owns O3. |
| Weekly belief store (W2 output) | `~/.hermes/quant/memory/beliefs.jsonl` (ADR-0081 §1 schema: `tier`,`role`,`lesson_category`,`verbal_delta`,`alpha_evidence`,`support_n`,`access_counter`,`importance`,`recency`,`oracle_provenance`,`asof_distilled`,`status`) | **READ** trailing-4-weeks `tier="weekly"` `status="active"` beliefs; apply the deterministic weekly→monthly promote/expire (ADR-0081 §4). **WRITE** new `tier="monthly"` and `status="expired"` append rows. |
| Reflection corpus (external truth) | `hermes_quant/memory/reflector.py:59` `REFLECTIONS_PATH`; `Reflection.alpha_return` (`reflector.py:102`), `tau_observable` (`reflector.py:99`), `LessonCategory` (`reflector.py:75-83`) | **READ** to recompute realized alpha per `lesson_category` and per decision (external-truth evaluator — never an LLM self-score). |
| Decision log (ticker↔decision_id join) | `hermes_quant/memory/decisions.py:35` `MEMORY_HOME`, `DECISIONS_PATH`; `_to_utc_iso` | **READ** to map `proposal_id`/`decision_id` ↔ ticker so debate rows join to reflections. |
| Hypothesis registry (candidate sink) | `hermes_quant/research/hypothesis.py:272` `HypothesisRegistry.register(Hypothesis)`; `Hypothesis` model `hypothesis.py:95`; AST-purity gate on criteria `hypothesis.py:181` (`_purity_check_criterion`) | **WRITE** candidate hypotheses (`status="open"`). Criteria strings pass the existing AST-purity gate automatically. |
| RunCard config_hash (reproducibility gate) | `hermes_quant/research/run_card.py:105` `strategy_config_hash` (SHA-256 of config); `RunCardLog.read_for_hypothesis` (`run_card.py:218`) | The meta-retro's own `config_hash` mirrors this idiom (§3). Also **READ** RunCards to know which hypotheses already ran (so we don't re-propose a tested one). |
| Novelty/dedup concept (extend, don't import 1:1) | `hermes_quant/factors/ic_dedup.py:69` `ICDedupGate` (numeric IC corr ≥0.99 → reject) | W3 needs **textual** novelty over hypothesis *claims*, not numeric IC. Build a sibling `hypothesis_novelty.py` (§3) modeled on this gate's shape (`check()→Result`, env-tunable threshold) but token/Jaccard-based. |
| Flag idiom (copy verbatim) | `hermes_quant/react/paper.py:242` `os.environ.get("HERMES_QUANT_REFLECTION", "0") == "1"`; canonical multi-value form `hermes_quant/regime/regime_aware_confidence.py:26` `in ("1","true","True","yes","on")` | Copy for `HERMES_QUANT_MONTHLY_META_RETRO`. |
| Cron-script template (no_agent watchdog + silence-by-default) | `ops/scripts/quant-catalyst-profitability.py` (baseline/diff/silent), `ops/scripts/quant-catalyst-eval-gate.py` (the hard-gate script form) | Model the cron + the eval-gate script on these two. |

**Confirmed: no W3 producer exists yet.** `grep -rn "MONTHLY_META\|meta_retro\|persona_calibration"` returns nothing in `hermes_quant/`. `quant-wave3-candidates.py` is an unrelated watchlist sleeve (NOT this wave) — do not touch it.

---

## 2. New / modified files

### NEW — core module
`hermes_quant/memory/meta_retro.py`
The deterministic monthly meta-retro engine. Pure functions over JSONL inputs; no network, no LLM in the scoring path (an LLM may only phrase a candidate-hypothesis *claim* string — never grade anything). All randomness/order is sorted for reproducibility.

### NEW — novelty gate
`hermes_quant/research/hypothesis_novelty.py`
Textual novelty/dedup over hypothesis `claim` strings (extends the `ic_dedup` *concept*; does not import it). Used so the meta-retro never re-proposes a near-duplicate of an existing registry hypothesis.

### NEW — cron entrypoint
`ops/scripts/quant-monthly-meta-retro.py`
Default-OFF, `no_agent=True`, silence-by-default. Flag-gated; when OFF returns 0 with empty stdout (byte-identical off-state).

### NEW — eval-gate script (the hard gate before flipping the flag)
`ops/scripts/quant-monthly-meta-retro-eval-gate.py`
Modeled on `quant-catalyst-eval-gate.py`. Proves the four gate conditions (§4) and prints `GATE: ✅ PASS — safe to flip HERMES_QUANT_MONTHLY_META_RETRO=1` or fails loudly.

### NEW — tests
- `tests/memory/test_meta_retro.py` — engine unit + the eval-gate acceptance criteria as pytest (§6).
- `tests/research/test_hypothesis_novelty.py` — novelty gate.
- `tests/unit/test_monthly_meta_retro_offstate.py` — byte-identical off-state + propose-only invariants.

### MODIFIED — docs only (no code seam changes)
- `docs/operations/CRON-REGISTRY.md` — add the `quant-monthly-meta-retro-monthly` row (operator registers it; this agent cannot).
- `docs/operations/FEATURE-ENABLEMENT.md` — add the flag-flip one-liner + its eval gate.

**Explicitly NOT modified:** `stage.py`, `promotion.py`, `audit_log.py` (no new EventKind), `deliberative.py`, `bma.py`, `reflector.py`, `retriever.py`, the seed catalyst YAML, any risk module. W3 is pure read-of-existing-seams + write-to-new-advisory-artifacts.

---

## 3. Function signatures (implementation contract)

### `hermes_quant/memory/meta_retro.py`

```python
from __future__ import annotations
import hashlib, json, os, logging
from dataclasses import dataclass, field, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_quant.memory.decisions import MEMORY_HOME  # ~/.hermes/quant/memory

logger = logging.getLogger(__name__)

META_RETROS_PATH = MEMORY_HOME / "meta_retros.jsonl"   # append-only, NEW artifact
BELIEFS_PATH = MEMORY_HOME / "beliefs.jsonl"            # W2's store (read + promote/expire)
CURRENT_SCHEMA_VERSION: int = 1

ENV_FLAG = "HERMES_QUANT_MONTHLY_META_RETRO"

# RD-Agent failure-tag rubric (capability-map §3 / ADR-0080): why did a hypothesis
# or belief-category fail — was the IDEA wrong (abandon) or the EXECUTION wrong (retry)?
FAILURE_TAG_APPROACH = "approach"           # the thesis itself was wrong -> do not re-propose
FAILURE_TAG_IMPLEMENTATION = "implementation"  # execution/sizing/timing -> a retry candidate is admissible


def _flag_on() -> bool:
    """Copy of the canonical idiom (regime_aware_confidence.py:26)."""
    return os.environ.get(ENV_FLAG, "0") in ("1", "true", "True", "yes", "on")


# ---- inputs (all READ-ONLY, all external-truth) ---------------------------

@dataclass(frozen=True)
class PersonaCalibration:
    """Per-persona realized-calibration telemetry. PROPOSED weight only — never applied."""
    role: str                      # "bull_researcher" | "bear_researcher" | "judge" | "risk_*"
    n_calls: int                   # debate rows this persona appeared in (joined to a resolved trade)
    n_correct: int                 # times the persona's stance matched realized alpha sign
    hit_rate: float                # n_correct / n_calls (0 when n_calls == 0)
    mean_alpha_when_followed: float  # external truth: mean alpha when the persona's stance was the judge call
    proposed_weight_delta: float   # advisory ONLY; centred at 0; clamped to [-0.10, +0.10]
    telemetry_only: bool = True    # ALWAYS True in W3 — flips only after >=M months agree (W6/operator)


@dataclass(frozen=True)
class LessonCategoryTrend:
    """Which lesson_category repeats across the trailing weeks (FINCON over-episode)."""
    lesson_category: str
    weeks_present: int             # of the trailing N weekly belief sets, how many contained it
    cumulative_support_n: int
    mean_alpha_evidence: float     # external truth (mean over the backing weekly beliefs)
    repeats: bool                  # weeks_present >= REPEAT_THRESHOLD
    failure_tag: str | None        # FAILURE_TAG_* when mean_alpha_evidence < 0, else None


@dataclass(frozen=True)
class CandidateHypothesis:
    """A PROPOSED, novelty-gated hypothesis. Registered status='open' only."""
    claim: str
    null_hypothesis: str
    rationale: str
    source_lesson_category: str
    support_n: int
    novelty_max_sim: float         # from hypothesis_novelty gate
    failure_tag: str | None        # implementation-vs-approach (RD-Agent rubric)


@dataclass
class MetaRetroReport:
    schema_version: int
    meta_retro_id: str             # SHA-stable over (asof_month, config_hash)
    asof: str                      # ISO-8601 UTC distillation tick
    window_start: str
    window_end: str
    config_hash: str               # SHA-256 over the deterministic config (see _config_hash); the REPRODUCIBILITY gate
    lesson_category_trends: list[dict[str, Any]]
    persona_calibration: list[dict[str, Any]]
    candidate_hypotheses: list[dict[str, Any]]
    beliefs_promoted: list[str]    # belief_ids weekly->monthly
    beliefs_expired: list[str]     # belief_ids expired this tick
    promotion_readiness_flips: int # count of weekly_retro_promotion_readiness=True promotion_events in window
    telemetry_only: bool = True    # INVARIANT: the whole report is advisory


# ---- the deterministic engine ---------------------------------------------

def _config_hash(window_days: int, repeat_threshold: int, novelty_threshold: float,
                 max_candidates: int, weekly_to_monthly_half_life_days: float) -> str:
    """SHA-256 over the sorted config dict — the reproducibility handle.
    Re-running the same month with the same config + same input corpus MUST yield
    the same meta_retro_id and the same candidate set (RunCard.strategy_config_hash idiom)."""
    payload = json.dumps({
        "window_days": window_days, "repeat_threshold": repeat_threshold,
        "novelty_threshold": novelty_threshold, "max_candidates": max_candidates,
        "weekly_to_monthly_half_life_days": weekly_to_monthly_half_life_days,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_debate_rows(asof: datetime, window_start: datetime) -> list[dict[str, Any]]:
    """READ research_debate audit rows in [window_start, asof). Oracle guard:
    only rows whose asof < the distillation tick (no future debate)."""
    from hermes_quant.governance import audit_log
    return [e.payload for e in audit_log.read(since=window_start, kinds=["research_debate"])
            if e.asof < asof]


def compute_persona_calibration(
    debate_rows: list[dict[str, Any]],
    realized_alpha_by_proposal: Callable[[str], float | None],
) -> list[PersonaCalibration]:
    """Join each debate row's stance/recommendation to realized alpha (external truth).
    A persona is 'correct' when its stance sign matches realized alpha sign on the
    resolved trade. proposed_weight_delta is centred at 0, clamped [-0.10,+0.10], and
    is TELEMETRY ONLY (PersonaCalibration.telemetry_only stays True)."""


def compute_lesson_trends(
    weekly_beliefs: list[dict[str, Any]],   # trailing-N weekly tier beliefs (status=active)
    repeat_threshold: int,
) -> list[LessonCategoryTrend]:
    """FINCON over-episode: which lesson_category appears in >= repeat_threshold of the
    trailing weekly belief sets. Tags failure as approach-vs-implementation when alpha<0."""


def synthesize_candidate_hypotheses(
    trends: list[LessonCategoryTrend],
    existing_claims: list[str],             # claims already in the registry (read once)
    *,
    novelty_threshold: float,
    max_candidates: int,
    llm_claim_writer: Callable[[LessonCategoryTrend], tuple[str, str, str]] | None = None,
) -> list[CandidateHypothesis]:
    """Emit <= max_candidates candidate hypotheses from REPEATING, positive-or-implementation
    -tagged trends. Each candidate claim is passed through hypothesis_novelty.check()
    against existing_claims; rejected if max_sim >= novelty_threshold. When
    llm_claim_writer is None (CI/default), a deterministic template phrases the claim
    (no LLM, fully reproducible). The LLM, if present, writes ONLY the claim/null/rationale
    strings — it scores nothing (external-truth-only rail)."""


def apply_weekly_to_monthly(
    beliefs: list[dict[str, Any]],
    asof: datetime,
    trends: list[LessonCategoryTrend],
    *,
    weekly_to_monthly_half_life_days: float,
) -> tuple[list[str], list[str]]:
    """Deterministic FINMEM promote/expire (ADR-0081 §4), NON-LLM:
      - a weekly belief whose lesson_category repeats (trend.repeats) is PROMOTED to
        tier='monthly' (append a new monthly row, longer half_life_days);
      - weekly beliefs that did not recur AND whose recency<eps or importance<thr are
        EXPIRED (append a status='expired' row).
    Returns (promoted_belief_ids, expired_belief_ids). Oracle provenance is COPIED
    forward unchanged (never re-tagged as ground truth)."""


def run_meta_retro(
    asof: datetime,
    *,
    window_days: int = 28,
    repeat_threshold: int = 2,
    novelty_threshold: float = 0.85,
    max_candidates: int = 5,
    weekly_to_monthly_half_life_days: float = 90.0,
    realized_alpha_by_proposal: Callable[[str], float | None] | None = None,
    register_candidates: bool = False,        # propose-only: registry write gated here
    llm_claim_writer: Callable | None = None,
    beliefs_path: Path | None = None,
    meta_retros_path: Path | None = None,
) -> MetaRetroReport:
    """The full monthly pass. PURE + DETERMINISTIC given (asof, config, input corpus).
    Writes the report to meta_retros.jsonl (append-only). When register_candidates=True
    AND the flag is on, registers candidates as Hypothesis(status='open',
    author='quant-monthly-meta-retro'). NEVER auto-promotes; NEVER touches a limit.
    realized_alpha_by_proposal defaults to a reflections.jsonl-backed lookup (external truth)."""
```

Reproducibility contract (the eval gate, condition 1): two calls to `run_meta_retro(asof, **same_config)` over the same immutable corpus return reports whose `(meta_retro_id, config_hash, sorted candidate claims, beliefs_promoted, beliefs_expired)` are byte-identical. No `datetime.now()`, no unsorted dict iteration, no RNG in the scoring path — `asof` is always passed in.

### `hermes_quant/research/hypothesis_novelty.py`

```python
from dataclasses import dataclass

_DEFAULT_THRESHOLD = float(os.environ.get("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "0.85"))

@dataclass(frozen=True)
class NoveltyResult:
    passes: bool            # True == novel == admissible
    max_sim: float          # max Jaccard/token-set similarity to any existing claim
    nearest_claim: str | None
    reason: str

def _normalize(claim: str) -> frozenset[str]:
    """Lowercase, strip punctuation, drop stopwords, return token set."""

def token_jaccard(a: str, b: str) -> float:
    """|A∩B| / |A∪B| over normalized token sets; 0.0 when either is empty."""

def check_novelty(candidate_claim: str, existing_claims: list[str],
                  threshold: float | None = None) -> NoveltyResult:
    """Reject (passes=False) when max_sim >= threshold. Mirrors ICDedupGate.check()
    shape; textual (Jaccard) instead of numeric (IC corr). Empty library -> passes."""
```

### `ops/scripts/quant-monthly-meta-retro.py` (cron entrypoint, shape)

```python
"""quant-monthly-meta-retro.py — T3 monthly meta-retro (W3). Default-OFF.

no_agent=True, silence-by-default. When HERMES_QUANT_MONTHLY_META_RETRO != 1 this
script is a no-op (returns 0, empty stdout) so the off-state is byte-identical.
PROPOSES ONLY: registers candidate hypotheses status='open' and emits persona
telemetry inside the report; never promotes, never touches a limit/size/the gate."""
from __future__ import annotations
import os, sys
from datetime import UTC, datetime
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

def main() -> int:
    if os.environ.get("HERMES_QUANT_MONTHLY_META_RETRO", "0") != "1":
        return 0  # OFF -> byte-identical no-op, empty stdout (no Discord message)
    from hermes_quant.memory.meta_retro import run_meta_retro
    report = run_meta_retro(
        asof=datetime.now(UTC),
        register_candidates=True,   # only reached when flag is on
    )
    # Silence-by-default: announce only deltas (new candidates / promotions / expiries).
    deltas = (len(report.candidate_hypotheses)
              + len(report.beliefs_promoted) + len(report.beliefs_expired))
    if deltas == 0:
        return 0
    print(f"📅 monthly-meta-retro: {len(report.candidate_hypotheses)} candidate hyp, "
          f"{len(report.beliefs_promoted)} promoted, {len(report.beliefs_expired)} expired "
          f"(config_hash={report.config_hash[:12]})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

---

## 4. The eval gate (must pass to flip the flag) — `quant-monthly-meta-retro-eval-gate.py`

Implements ADR-0080 D80.3 (universal eval-gate) specialized for W3, which is *report-and-candidate-only* (no live A/B). Per capability-map §4 W3, the gate is:

1. **Reproduces (Run-Card `config_hash`).** Run `run_meta_retro(asof, **cfg)` twice over a frozen fixture corpus → assert identical `meta_retro_id`, `config_hash`, sorted candidate claims, `beliefs_promoted`, `beliefs_expired`. (External-truth + determinism rails: no LLM in the scoring path, `asof` injected.)
2. **Candidate hypotheses pass novelty/dedup.** Every emitted candidate has `novelty_max_sim < novelty_threshold` against the existing registry; assert no candidate is a near-duplicate of a prior hypothesis. (Extends `ic_dedup` concept; anti-Correlation-Red-Sea.)
3. **Persona-weight deltas are telemetry-only.** Assert every `PersonaCalibration.telemetry_only is True`, every `|proposed_weight_delta| <= 0.10`, and that **no aggregator reads `persona_calibration`** (grep-assert: `deliberative.py`/`bma.py` contain no `persona_calibration` reference). Deltas stay telemetry until **≥M months** of agreement with realized calibration (recommend `M=3`; tracked by the operator, NOT auto-flipped by code).
4. **Oracle provenance preserved + nothing live mutated.** Assert (a) every promoted/expired belief carries forward its original `oracle_provenance` unchanged; (b) the debate-row join applied the `evt.asof < asof` guard (no future debate); (c) the off-state is byte-identical (flag OFF → empty stdout, no files written beyond what was there); (d) no write touched any risk module, the sizing ladder, or the seed YAML.

The script prints `GATE: ✅ PASS — safe to flip HERMES_QUANT_MONTHLY_META_RETRO=1` only when all four pass; otherwise it prints each failure and exits non-zero. **Passing is necessary, not sufficient** (ADR-0080 D80.3 #2): promotion of any candidate to live still requires `HypothesisRunner` (W6) + `PromotionOrchestrator` + operator sign-off.

---

## 5. Oracle-provenance / external-truth flow (where the rails live in the data path)

- **External truth only.** Persona calibration and lesson-trend alpha come from `Reflection.alpha_return` (realized alpha-vs-benchmark, `reflector.py:104`) — never an LLM self-score, never the debate's own confidence re-read as truth. The debate `confidence` field is used only to *identify* the stance; the *grade* is always realized alpha.
- **Oracle guard at the debate level.** `_load_debate_rows` excludes any `research_debate` row with `evt.asof >= asof` (the distillation tick) — no future debate informs the month.
- **Oracle guard at the belief level (inherited from W2/ADR-0081 §1).** Promoted monthly beliefs copy `oracle_provenance.tau_observable_max` forward unchanged; the retriever still applies `tau_observable < decision.asof` (`retriever.py:351-362`) when surfacing them. W3 never lowers a `tau_observable`.
- **No re-ingestion as ground truth.** The meta-retro report and candidate hypotheses are tagged `author="quant-monthly-meta-retro"` / `telemetry_only=true`; nothing reads `meta_retros.jsonl` back as a *truth* input — it is an operator-facing report + a candidate sink for W6.

---

## 6. Tests — eval gate as pytest-verifiable acceptance criteria

### `tests/memory/test_meta_retro.py`
- `test_reproduces_byte_identical_config_hash` — build a frozen fixture (debate rows + weekly beliefs + reflections in `tmp_path`), run `run_meta_retro(asof, **cfg)` twice; assert equal `meta_retro_id`, `config_hash`, sorted candidate claims, `beliefs_promoted`, `beliefs_expired`. **(Gate condition 1.)**
- `test_persona_calibration_uses_realized_alpha_not_confidence` — feed a debate row whose bull `confidence=0.9` but realized alpha is negative; assert the bull persona is scored *incorrect* (external truth wins). **(SAFETY: external-truth-only.)**
- `test_persona_deltas_are_telemetry_only_and_clamped` — assert every `PersonaCalibration.telemetry_only is True` and `abs(proposed_weight_delta) <= 0.10`. **(Gate condition 3.)**
- `test_lesson_trend_repeat_threshold` — category present in 2/4 weekly sets with `repeat_threshold=2` → `repeats=True`; present in 1/4 → `repeats=False`.
- `test_failure_tag_implementation_vs_approach` — negative-alpha trend → `failure_tag in {approach, implementation}`; an approach-tagged trend yields NO candidate, an implementation-tagged trend MAY. **(RD-Agent rubric.)**
- `test_weekly_to_monthly_promotion_copies_oracle_provenance` — a repeating weekly belief is promoted to `tier="monthly"` with `oracle_provenance` byte-identical to the source. **(Gate condition 4a.)**
- `test_belief_expiry_is_append_only` — expiry adds a `status="expired"` row; the original `active` row is never mutated in place.
- `test_debate_oracle_guard_excludes_future_rows` — a `research_debate` row with `asof >= tick` is excluded from calibration. **(Gate condition 4b.)**
- `test_candidates_registered_status_open_only` — with `register_candidates=True`, every registered `Hypothesis.status == "open"` and `author == "quant-monthly-meta-retro"`; none is `running`/`validated`. **(Propose-only invariant.)**

### `tests/research/test_hypothesis_novelty.py`
- `test_empty_library_passes` — no existing claims → `passes=True`.
- `test_near_duplicate_rejected` — claim ~identical to an existing one → `max_sim >= threshold`, `passes=False`. **(Gate condition 2.)**
- `test_distinct_claim_passes` — unrelated claim → `passes=True`.
- `test_threshold_env_override` — `HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD` changes the cutoff.

### `tests/unit/test_monthly_meta_retro_offstate.py`
- `test_offstate_is_noop` — with flag unset, `quant-monthly-meta-retro.py` `main()` returns 0, writes nothing, prints nothing (subprocess capture). **(Gate condition 4c; byte-identical off-state.)**
- `test_no_aggregator_reads_persona_calibration` — grep-assert `persona_calibration` does NOT appear in `hermes_quant/aggregators/deliberative.py` or `.../bma.py`. **(Gate condition 3; advisory-plane wall.)**
- `test_no_write_touches_risk_or_ladder` — assert `meta_retro.py` source contains no import of any risk-gate/sizing-ladder/seed-YAML module and writes only to `meta_retros.jsonl`/`beliefs.jsonl`/the hypothesis registry. **(Gate condition 4d; SAFETY wall.)**

### `tests/research/test_hypothesis_novelty.py` reuses the AST-purity guarantee
Candidate `claim`/criteria strings flow through `Hypothesis`'s existing `_purity_check_criterion` (`hypothesis.py:181`); add `test_candidate_criteria_pass_ast_purity` to confirm a malformed criterion is rejected at registration (defense-in-depth, no new code needed).

---

## 7. Build order (for the implementing agent)

1. `hypothesis_novelty.py` + its test (leaf, no deps).
2. `meta_retro.py` pure functions (`_config_hash`, `compute_*`, `apply_weekly_to_monthly`, `synthesize_candidate_hypotheses`) + `run_meta_retro`, with the reflections-backed `realized_alpha_by_proposal` default.
3. `tests/memory/test_meta_retro.py` (drives 1–2; TDD against the gate conditions).
4. `quant-monthly-meta-retro.py` cron + `test_monthly_meta_retro_offstate.py`.
5. `quant-monthly-meta-retro-eval-gate.py` (mirrors the four §4 conditions; reuse the pytest fixtures).
6. Docs: CRON-REGISTRY row + FEATURE-ENABLEMENT flip one-liner.

**Dependency:** requires W2 to have shipped `~/.hermes/quant/memory/beliefs.jsonl` (ADR-0081 §1 schema) and the weekly tier. If W2 is not yet merged, the engine still builds and tests against a *fixture* `beliefs.jsonl`; only the live cron needs the real W2 store.

**Flip command (operator, after the gate passes):** `HERMES_QUANT_MONTHLY_META_RETRO=1` on the `quant-monthly-meta-retro-monthly` cron (suggested schedule: first business day of month, after the weekly retro has run for the trailing weeks). Promotion of any emitted candidate to live remains W6 + operator sign-off — W3 only proposes.
