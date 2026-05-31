# Implementation Plan — W2: Weekly pattern-mining retro (FINCON CVRF lower-half)

**Date:** 2026-05-30
**Wave:** W2 (depends on W1 — commit `08326e1`, the now-live decision/reflection corpus)
**Closes:** O2 (no weekly distillation tier) + O3 (dangling `weekly_retro_promotion_readiness` gate field)
**Flag:** `HERMES_QUANT_WEEKLY_RETRO=1`, default-OFF (off-state byte-identical to today)
**Decision record:** ADR-0081 (read it — this plan IMPLEMENTS its §1–§4)
**Capability-map:** §2 (O2/O3), §3 (CVRF / FINMEM / Oracle-provenance), §4 W2, §5 (safety frame)

A fresh agent can build this with no further research. Every existing seam is cited
`file:line` (verified against the tree on 2026-05-30). Read ADR-0081 first; this plan
is the build, not the decision.

---

## 0. What this wave is, in one paragraph

W1 ignited the per-trade edge: `maybe_record_decision_on_open` (`hermes_quant/memory/_paper_reflection_hook.py:27`)
writes a `pending` decision on every opening paper fill, and `maybe_reflect_on_close`
(`_paper_reflection_hook.py:110`) resolves it into a row in `reflections.jsonl`. The
retriever already injects those raw per-trade rows into the PM prompt
(`hermes_quant/aggregators/llm_committee.py:294-316`, gated `HERMES_QUANT_MEMORY_INJECT=1`).
W2 adds the **distillation tier above the raw corpus**: a weekly batch job that reads
`reflections.jsonl`, splits winners vs losers **by realized alpha** (`Reflection.alpha_return`,
`reflector.py:104`/`495`), groups by `lesson_category` + ticker/sector, and distills ≤N
**verbal belief-deltas per role** into a new derived artifact `beliefs.jsonl`. Those beliefs
are (a) prepended into the PM `lessons_block`, and (b) the successful completion of the pass
emits the **single missing producer** for `weekly_retro_promotion_readiness` — a
`promotion_event` audit row that un-blocks the permanently-`False` gate at `promotion.py:158`.

It **PROPOSES only.** A belief is verbal context injected into one role's prompt — never a
parameter, never a limit, never a sizing-ladder entry. The deterministic risk gate, the hard
risk limits, the discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`, and the kill-switch
sit OUTSIDE this loop and are immutable by it. The only path to live policy remains the
deterministic OOS backtest + the existing operator/eval-gated promotion machinery
(`governance/promotion.py`).

---

## 1. The safety frame applied to W2 (capability-map §5 — non-negotiable)

### MAY write (the advisory plane)
- **`~/.hermes/quant/memory/beliefs.jsonl`** — a derived, rebuildable projection of the
  immutable `reflections.jsonl`. Each row is a verbal belief-delta (≤1–2 sentences) tagged
  with one committee `role`, its `alpha_evidence`, `support_n`, FINMEM counters, and Oracle
  provenance. This file is **never source-of-truth** — deleting it and re-running the weekly
  retro reproduces it deterministically from the (immutable, append-only) reflection corpus.
- **`promotion_event` audit rows** with `payload["weekly_retro_promotion_readiness"]=true`
  via the existing `audit_log.append()` (`governance/audit_log.py:129`). `promotion_event`
  is already a `VALID_KIND` (`audit_log.py:44,56`) — **no schema bump required.**

### MUST NEVER touch (outside the loop, immutable by it)
- The deterministic risk gate, the hard risk limits (max loss, position caps, exposure).
- The discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}`.
- The kill-switch (a separate process the agent runtime cannot signal).
- `reflections.jsonl` / `decisions.jsonl` — read-only inputs; W2 never mutates them.
- `governance/promotion.py` thresholds or logic — W2 only **feeds it a field**; the gate's
  arithmetic is unchanged. Passing the gate stays **necessary, not sufficient** → operator
  sign-off remains the sole path to live (`promotion_orchestrator.py:357-359`, unchanged).

### The five enforcement primitives, instantiated here
1. **External-truth evaluator only.** Winner/loser split and `alpha_evidence` use realized
   `alpha_return` from market data (already computed deterministically at `reflector.py:421`).
   No LLM self-score; no narrative re-ingested as truth.
2. **Held-out gate is necessary AND the optimizer never sees it.** The flag flips only after
   the W2 eval gate (§7) passes on an OOS window the distiller never read. Operator promotion
   stays in the loop.
3. **Select on robustness/plateau, never the peak.** The `half_life_days` / budget-cap `N`
   constants are jitter-tested (§7 test `test_weekly_retro_halflife_is_plateau_not_peak`),
   never decimal-optimized. Checkpoint-fallback: if the digest-injected prompt does not
   strictly beat the no-digest baseline on held-out, the flag stays OFF.
4. **Bounded, decaying, Oracle-provenance-tagged store.** FINMEM access-counter + half-life;
   per-role budget cap `N`; every belief carries `oracle_provenance.tau_observable_max` and
   the retriever applies the same `tau_observable < asof` guard at the belief level.
5. **Propose-only, deterministic aggregation, surface-dissent.** No LLM decides what to keep
   — promote/expire is pure arithmetic (§4). The digest is appended to context, never voted
   into a decision.

---

## 2. New & modified files (exact list)

### NEW
| Path | Purpose |
|---|---|
| `hermes_quant/memory/weekly_retro.py` | The distillation engine + belief store I/O + the FINMEM promote/expire rule. Pure, offline, deterministic, network-free, unit-testable. |
| `ops/scripts/quant-weekly-retro.py` | `no_agent` cron wrapper with the change-detecting silence contract (mirrors `quant-catalyst-profitability.py`). Flag-gated. |
| `tests/memory/test_weekly_retro.py` | Distillation + FINMEM + Oracle-provenance + O3-emission unit tests. |
| `tests/memory/test_weekly_retro_eval_gate.py` | The SkillOpt eval gate as pytest-verifiable acceptance criteria (§7). |

### MODIFIED
| Path:line | Change |
|---|---|
| `hermes_quant/memory/retriever.py` (add after `format_context_block_split`, ~`:568`) | Add `load_active_beliefs(role, asof, ...)` + `format_beliefs_digest(beliefs, ...)`; both apply the belief-level Oracle guard. No change to existing functions. |
| `hermes_quant/aggregators/llm_committee.py:294-316` | Under `HERMES_QUANT_WEEKLY_RETRO=1`, prepend the beliefs digest above the existing `lessons_block`. Default-OFF: when the flag is absent, the line is skipped and `lessons_block` is byte-identical to today. |
| `docs/runbooks/feature-enablement.md` (or the cron registry doc, task #14) | Register `quant-weekly-retro` + document the flag-flip one-liner. |

**No change** to `decisions.py`, `reflector.py`, `governance/promotion.py`,
`governance/audit_log.py`, `promotion_orchestrator.py`, or any risk-gate / sizing code.

---

## 3. `beliefs.jsonl` schema (ADR-0081 §1, verbatim) + path

Path: `~/.hermes/quant/memory/beliefs.jsonl` (sibling of `reflections.jsonl`; define
`BELIEFS_PATH = MEMORY_HOME / "beliefs.jsonl"` in `weekly_retro.py`, importing `MEMORY_HOME`
from `hermes_quant.memory.decisions` — same idiom as `reflector.py:55,59`).

```python
@dataclass
class Belief:
    schema_version: int          # CURRENT_BELIEF_SCHEMA_VERSION = 1
    belief_id: str               # SHA-stable over (tier, role, lesson_category, asof_distilled)
    tier: str                    # "weekly" | "monthly" — sets the half-life
    role: str                    # the ONE role this is propagated to: "portfolio_manager"
                                 #   | "research_manager" | "bull_researcher" | "bear_researcher"
                                 #   | "risk_aggressive" | "risk_conservative" | "risk_neutral"
    lesson_category: str         # the LessonCategory enum value it generalizes (reflector.py:75-83)
    verbal_delta: str            # ≤1-2 sentences; "what to do differently"
    alpha_evidence: float        # mean realized ALPHA of the winners-vs-losers split (external truth)
    support_n: int               # number of reflections backing it (gates single-trade beliefs)
    half_life_days: float        # by tier (weekly shorter, monthly longer)
    access_counter: int          # FINMEM counter; +1 each time surfaced into a prompt
    importance: float            # FINMEM importance; +K on a pivotal profitable event
    recency: float               # decay value in (0,1]; reset to 1.0 on access
    oracle_provenance: dict      # {"source":"agent_reflection",
                                 #  "tau_observable_max":<ISO>, "decision_ids":[...]}
    asof_distilled: str          # ISO-8601 UTC of the distillation tick
    status: str                  # "active" | "expired" (append-only; expiry is a NEW row)
```

**Belief-level Oracle guard (the load-bearing invariant).** `oracle_provenance.tau_observable_max`
= `max(tau_observable)` over the backing reflections. A belief is eligible for injection ONLY
if `tau_observable_max < asof` of the decision being made — the same rule the retriever already
enforces at the reflection level (`retriever.py:351-362`), lifted to the belief level so
distillation can never smuggle future knowledge into a prompt. `support_n` and the backing
`decision_ids` give a full audit trail back to immutable rows.

**Roles enumerated** from the actual prompt-rendering switch in
`llm_committee.py:278-286` — only `portfolio_manager` and `research_manager` are injection
targets today (`llm_committee.py:295`), so weekly beliefs SHOULD default to `role="portfolio_manager"`
in v1; the schema carries all roles for forward-compat with W3 (monthly meta) and W7 (red-team
persona calibration). **Do not** broadcast a belief to all roles — FINCON selective propagation
(ADR-0081 §1, drivers "Selective propagation, not broadcast").

---

## 4. `weekly_retro.py` — function signatures (ADR-0081 §2 + §4)

```python
"""hermes_quant.memory.weekly_retro — Layer 4: weekly CVRF distillation (ADR-0081).

Gated by HERMES_QUANT_WEEKLY_RETRO=1 at the CRON layer. The library functions are
pure + deterministic + network-free (safe in CI); the flag gate lives in
ops/scripts/quant-weekly-retro.py and in the llm_committee injection site, mirroring
how reflector.py is library-pure and the flag lives in the reactor/_paper_reflection_hook.

PROPOSE-ONLY. Writes ONLY to the advisory plane: beliefs.jsonl (a rebuildable view of
the immutable reflections.jsonl) and a promotion_event audit row. NEVER touches the
risk gate, hard limits, the sizing ladder, or the kill-switch (capability-map §5).
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hermes_quant.memory.decisions import MEMORY_HOME

BELIEFS_PATH = MEMORY_HOME / "beliefs.jsonl"
REFLECTIONS_PATH = MEMORY_HOME / "reflections.jsonl"   # read-only input
CURRENT_BELIEF_SCHEMA_VERSION: int = 1

# --- Tunables (jitter-tested, NOT decimal-optimized — MT3 / AMZN-weight rule) ---
BELIEF_BUDGET_PER_ROLE: int = 3        # Reflexion Ω≈1-3; FINCON small-set. Per-role cap N.
MIN_SUPPORT_N: int = 3                  # a belief needs >=3 backing reflections (no single-trade beliefs)
HALF_LIFE_DAYS = {"weekly": 14.0, "monthly": 60.0}   # weekly decays faster than monthly
RECENCY_EXPIRE_EPSILON: float = 0.10    # recency < eps -> expire
IMPORTANCE_BONUS_K: float = 1.0         # +K on a pivotal positive-alpha event
TRAILING_WINDOW_DAYS: int = 7           # the "weekly" window of reflections to distill


# ---------------------------------------------------------------------------
# I/O (append-only; beliefs.jsonl is a rebuildable projection)
# ---------------------------------------------------------------------------

def load_reflections(asof: datetime, *, path: Path | None = None,
                     window_days: int = TRAILING_WINDOW_DAYS) -> list[dict]:
    """Load trailing-window reflection rows RESOLVABLE as of `asof`.

    Applies the Oracle guard FIRST (same rule as retriever.py:351-362): a row is
    eligible only if its tau_observable < asof. This is the lookahead-honesty rail —
    the distiller must never read an outcome that was not knowable at the distillation
    tick. Rows with tau_observable >= asof are excluded BEFORE any grouping.
    """

def load_belief_rows(*, path: Path | None = None) -> list[dict]:
    """Stream every belief row (active + expired). Malformed rows skipped + logged."""

def materialize_active(rows: list[dict], asof: datetime) -> list["Belief"]:
    """Replay the append-only belief log into the CURRENT active set as of `asof`.

    A belief_id is active iff its latest row has status='active' AND it has not been
    superseded by a later 'expired' row. Mirrors decisions.read_pending() event-replay
    (decisions.py:186-197). Also applies the belief-level Oracle guard
    (oracle_provenance.tau_observable_max < asof).
    """


# ---------------------------------------------------------------------------
# CVRF distillation (ADR-0081 §2)
# ---------------------------------------------------------------------------

def split_winners_losers(reflections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split by realized ALPHA (alpha_return), NOT raw P&L. winners: alpha_return > 0."""

def group_by_pattern(reflections: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group by (lesson_category, ticker). Key is (lesson_category, ticker.upper())."""

def distill_beliefs(reflections: list[dict], *, asof: datetime,
                    role: str = "portfolio_manager",
                    budget: int = BELIEF_BUDGET_PER_ROLE,
                    min_support: int = MIN_SUPPORT_N) -> list["Belief"]:
    """CVRF lower-half: conceptualize winners-vs-losers into <=budget verbal belief-deltas.

    Deterministic, NO LLM. For each (lesson_category, ticker) group with >= min_support
    backing reflections, compute mean winner-alpha vs mean loser-alpha and emit a
    template-built verbal_delta (e.g. "On {ticker} {category}: longs realizing +X% alpha
    held N days; size up / cut faster"). Attach alpha_evidence (= mean alpha of the
    winning split), support_n, oracle_provenance (tau_observable_max + decision_ids).
    Rank groups by |alpha_evidence| * support_n; keep top `budget` per role.
    A LATER weekly pass with a richer corpus may evict a lower-ranked belief — that is
    the bounded-store trade (ADR-0081 Negative consequences).
    """


# ---------------------------------------------------------------------------
# FINMEM deterministic promote/expire (ADR-0081 §4 — NON-LLM, pure arithmetic)
# ---------------------------------------------------------------------------

def decay_and_promote(active: list["Belief"], new: list["Belief"], *, asof: datetime,
                      budget: int = BELIEF_BUDGET_PER_ROLE
                      ) -> tuple[list["Belief"], list["Belief"]]:
    """Apply the FINMEM rule. Returns (kept_active, newly_expired).

    Per belief, per tick:
      - DECAY:   recency *= alpha(tier), where alpha encodes half_life_days
                 (alpha = 0.5 ** (days_since_distilled / half_life_days)).
      - PROMOTE: a belief whose pattern RECURS in `new` gets access_counter += 1,
                 recency = 1.0; a pivotal positive-alpha recurrence gets importance += K
                 and is upgraded weekly->monthly (slower decay).
      - EXPIRE:  append a status='expired' row when recency < RECENCY_EXPIRE_EPSILON
                 OR per-role active count > budget (evict LOWEST
                 access_counter * recency * importance first).
    No LLM participates. Same corpus + asof => same active set (reproducible).
    """

def access_touch(belief_id: str, *, path: Path | None = None) -> None:
    """FINMEM access-counter bump: called by the retriever when a belief is surfaced
    into a prompt. Appends an 'active' row with access_counter+1, recency=1.0. Best-effort,
    never raises (prompt rendering must never break on a belief-store write)."""


# ---------------------------------------------------------------------------
# O3 closer + top-level entry point
# ---------------------------------------------------------------------------

@dataclass
class WeeklyRetroResult:
    asof: str
    n_reflections_read: int
    beliefs_distilled: int
    beliefs_expired: int
    active_belief_count: int          # post-pass total across all roles
    under_budget: bool                # active_belief_count <= BELIEF_BUDGET_PER_ROLE * n_roles
    promotion_readiness_emitted: bool # True iff the promotion_event was written
    transitions: list[str]            # human one-liners for the cron silence contract

def emit_promotion_readiness(result: WeeklyRetroResult, asof: datetime) -> None:
    """Close O3. Emit ONE promotion_event audit row whose payload sets
    weekly_retro_promotion_readiness=True — IFF the pass completed AND under_budget.

    This is the SINGLE missing producer for the gate field consumed at promotion.py:158.
    Wire shape EXACTLY matches the existing test seed (tests/governance/test_promotion.py:39-50):

        from hermes_quant.governance import audit_log
        audit_log.append(audit_log.GovernanceEvent(
            kind="promotion_event",            # already a VALID_KIND (audit_log.py:44)
            asof=asof,
            source="weekly_retro",
            payload={
                "weekly_retro_promotion_readiness": True,
                "active_belief_count": result.active_belief_count,
                "beliefs_distilled": result.beliefs_distilled,
            },
        ))

    NOTE: this writes ONE field the gate reads; it does NOT relax any gate threshold.
    Passing remains necessary-not-sufficient (operator sign-off unchanged).
    """

def run_weekly_retro(asof: datetime, *,
                     reflections_path: Path | None = None,
                     beliefs_path: Path | None = None,
                     emit_promotion: bool = True) -> WeeklyRetroResult:
    """Top-level: load -> Oracle-guard -> split -> group -> distill -> FINMEM
    promote/expire -> persist beliefs.jsonl -> (optionally) close O3. Pure + deterministic;
    the cron wraps this under the flag. `emit_promotion=False` lets tests exercise
    distillation without writing to the shared audit log."""
```

---

## 5. Modified seam: `retriever.py` + `llm_committee.py` (selective injection)

### 5a. `retriever.py` — add two helpers (after `format_context_block_split`, ~`:568`)

```python
def load_active_beliefs(role: str, asof: datetime, *,
                        beliefs_path: Path | None = None) -> list[dict]:
    """Return active beliefs for `role` whose oracle_provenance.tau_observable_max < asof.

    Belief-level Oracle Fallacy guard — the SAME rule applied to reflections at line 351-362,
    lifted to the belief level so a distilled belief never surfaces an outcome that was not
    knowable at the decision asof. Reads beliefs.jsonl via weekly_retro.materialize_active.
    Returns [] when the file is absent (default-OFF path is byte-identical)."""

def format_beliefs_digest(beliefs: list[dict], *, max_chars: int = 768) -> str:
    """Render active beliefs as a compact digest block:

        --- Distilled beliefs (weekly retro) ---
        [PM | thesis_invalidation_at_earnings | AAPL | +1.8% alpha | n=5]
        {verbal_delta}

    Empty -> "" (so the caller can cleanly skip prepending). Clipped to max_chars."""
```

`load_active_beliefs` also calls `weekly_retro.access_touch(belief_id)` for each surfaced
belief (the FINMEM access bump, §4) — best-effort, wrapped so a write failure never breaks
retrieval.

### 5b. `llm_committee.py:294-316` — prepend the digest, flag-gated

The live injection site stores into `lessons_block` (`llm_committee.py:331`). Add, INSIDE the
existing `if role in ("portfolio_manager", "research_manager"):` block, a second flag-gated
prepend that mirrors the established `HERMES_QUANT_MEMORY_INJECT` idiom exactly
(`llm_committee.py:296`):

```python
        # W2 weekly-retro beliefs digest (HERMES_QUANT_WEEKLY_RETRO=1, default OFF).
        # PREPENDED above the raw per-trade lessons. Selective: only the role the
        # belief was distilled FOR. Belief-level Oracle guard applied in the retriever.
        if os.environ.get("HERMES_QUANT_WEEKLY_RETRO", "0") == "1":
            try:
                from hermes_quant.memory.retriever import (
                    format_beliefs_digest, load_active_beliefs,
                )
                _beliefs = load_active_beliefs(role, asof_dt)
                _digest = format_beliefs_digest(_beliefs)
                if _digest:
                    lessons_block = _digest + "\n\n" + lessons_block
            except Exception:
                logger.warning(
                    "Weekly-retro belief injection failed for role=%r "
                    "(non-blocking); using raw lessons only", role)
```

**Off-state guarantee:** when `HERMES_QUANT_WEEKLY_RETRO` is unset, this block is skipped
entirely and `lessons_block` is byte-identical to the W1/today value. The default-OFF idiom is
copied verbatim from `llm_committee.py:296` (`os.environ.get(FLAG, "0") == "1"`).

---

## 6. `ops/scripts/quant-weekly-retro.py` — no_agent cron, change-detecting silence

Mirror `quant-catalyst-profitability.py` exactly (the reference silence contract,
commit `e4ecad5` lineage). Key elements to copy:

1. **venv re-exec header** (`quant-catalyst-profitability.py:17-19`) verbatim.
2. **Flag gate at the top of `main()`** — the cron is the flag boundary (the library is pure):
   ```python
   if os.environ.get("HERMES_QUANT_WEEKLY_RETRO", "0") != "1":
       return 0  # default-OFF: silent no-op (no_agent contract)
   ```
3. **Change-detecting baseline** in `~/.hermes/quant/memory/weekly-retro-baseline.json`
   (mirror `_BASELINE` / `_load_baseline` / `_save_baseline` at
   `quant-catalyst-profitability.py:31,63-77`). Baseline projects
   `{active_belief_count, beliefs_distilled, promotion_readiness_emitted}`.
4. **Silence-by-default**: `run_weekly_retro(datetime.now(UTC))` then a `_transitions()` diff
   (mirror `:92-111`). Emit stdout ONLY when something changed: a new belief was distilled, a
   belief expired, the budget-cap state flipped, or `promotion_readiness_emitted` toggled.
   Standing-state (no change) prints nothing → empty stdout → the no_agent watchdog stays
   silent. `--verbose` always prints the full belief table (operator on-demand pull),
   mirroring `:115,129-131`.
5. **Never raises** out of `main()` — wrap in the same defensive style; the cron returning a
   non-zero/traceback would be a false alarm. Return `0` on empty corpus (silence-by-default,
   mirror `:120-121`).

Register it in the cron registry (task #14) at a **weekly** cadence (e.g. Sunday 23:00 UTC,
after the week's settlements). Document the flag-flip one-liner in the enablement runbook
(task #13): `HERMES_QUANT_WEEKLY_RETRO=1` (+ `HERMES_QUANT_MEMORY_INJECT=1` for the digest to
reach the prompt).

---

## 7. Tests = the eval gate as pytest-verifiable acceptance criteria

### `tests/memory/test_weekly_retro.py` — distillation + FINMEM + O3 (unit)

Use the W1-liveness test idiom (`tests/memory/test_w1_decision_loop_liveness.py`):
`monkeypatch.setattr` the paths to `tmp_path` so nothing touches `~/.hermes`
(`test_w1_decision_loop_liveness.py:50-56`), and synthesize reflection rows directly.

- `test_split_is_by_alpha_not_raw_pnl` — two rows, one with `raw_return>0` but `alpha_return<0`
  and vice-versa; assert the loser-by-alpha lands in the loser split even though its raw P&L is
  positive. (Closes the SOTA tauric gap #8.)
- `test_distill_respects_budget_cap` — feed many groups; assert `len(beliefs for role) <= BELIEF_BUDGET_PER_ROLE`.
- `test_distill_requires_min_support` — a (category,ticker) group with `support_n < MIN_SUPPORT_N`
  produces NO belief (no single-trade beliefs).
- `test_belief_carries_oracle_provenance` — every emitted belief has
  `oracle_provenance.tau_observable_max == max(tau_observable of backers)` and a non-empty
  `decision_ids`.
- `test_belief_level_oracle_guard_excludes_future` — a belief whose `tau_observable_max >= asof`
  is NOT returned by `retriever.load_active_beliefs(role, asof)`. (Mirrors
  `test_w1_oracle_guard_excludes_future_reflection`, `:124`.)
- `test_finmem_decay_expires_stale_belief` — advance `asof` by `>> half_life_days`; assert the
  belief's `recency < RECENCY_EXPIRE_EPSILON` → a `status='expired'` row is appended and
  `materialize_active` no longer returns it.
- `test_finmem_access_bump_resets_recency` — `access_touch` sets `recency==1.0`, `access_counter+1`.
- `test_beliefs_jsonl_is_append_only_projection` — delete `beliefs.jsonl`, re-run
  `run_weekly_retro` on the SAME reflections + asof → byte-identical active set (rebuildable,
  deterministic).
- `test_distill_is_deterministic` — same corpus + asof, two runs → identical `belief_id`s
  (SHA-stable, no LLM).
- **`test_emit_promotion_readiness_closes_O3`** — run `run_weekly_retro(asof, emit_promotion=True)`
  against a tmp audit log, then call `governance.promotion.evaluate(asof)` and assert
  `decision.weekly_retro_promotion_readiness is True` and
  `"weekly_retro_promotion_readiness=False" not in decision.blocked_by`. This is the
  end-to-end O3 closure: the producer W2 writes is consumed by the gate at `promotion.py:158`.
  The emitted row MUST match the shape already asserted at
  `tests/governance/test_promotion.py:39-50` (`source="weekly_retro"`,
  `payload["weekly_retro_promotion_readiness"]=True`).

### `tests/memory/test_weekly_retro_eval_gate.py` — the SkillOpt held-out gate

This is the **flag-flip gate**: the flag may NOT be enabled in production until these pass.

- **`test_digest_does_not_regress_on_held_out_oos`** — THE gate. Build a corpus from an
  in-sample window; distill beliefs. Then on a **held-out OOS window the distiller never read**
  (separate reflection set / later dates), score a digest-injected PM prompt vs the no-digest
  baseline on hit-rate and mean alpha (use the deterministic stub committee path —
  `backtest/stub_llm.py` — so the test is reproducible in CI). Assert the digest variant's
  hit-rate AND mean alpha are `>=` the no-digest baseline (NOT a regression). Necessary, not
  sufficient — this is the checkpoint-fallback rule: if it regresses, the flag stays OFF.
- **`test_belief_count_under_budget_cap`** — after a representative multi-week distillation, the
  active-belief total is `<= BELIEF_BUDGET_PER_ROLE * n_injection_roles`.
- **`test_every_active_belief_oracle_tagged_and_decaying`** — every active belief has a parseable
  `oracle_provenance.tau_observable_max`, a positive `half_life_days`, and `0 < recency <= 1.0`.
- **`test_weekly_retro_halflife_is_plateau_not_peak`** — jitter `half_life_days` by ±20%
  (e.g. 14 → {11.2, 14, 16.8}); assert the held-out hit-rate/alpha is **stable across the
  jitter band** (no single decimal point dominates). Encodes the MT3 / AMZN-weight
  "use a RANGE, not the peak" rule (capability-map §5 primitive 3; ADR-0081 Negative
  "the half-life constants are a tunable that must be jitter-tested, not decimal-optimized").
- **`test_propose_only_never_touches_gate_or_ladder`** — assert the whole module imports/calls
  NOTHING from the risk-gate / sizing-ladder code paths (grep-style: no import of the gate
  module, no write to any sizing constant); the only writes are `beliefs.jsonl` and the
  `promotion_event` row. This is the safety-frame regression guard.

### Run command (eval gate)
```
pytest tests/memory/test_weekly_retro.py tests/memory/test_weekly_retro_eval_gate.py \
       tests/governance/test_promotion.py -q
```
All green = the gate passes; only then may the operator flip `HERMES_QUANT_WEEKLY_RETRO=1`.

---

## 8. Build order (for the executing agent)

1. `weekly_retro.py` — schema (`Belief`), I/O, `split/group/distill`, FINMEM `decay_and_promote`,
   `run_weekly_retro`, `emit_promotion_readiness`. Pure + deterministic; no flag inside.
2. `tests/memory/test_weekly_retro.py` — TDD the above (the O3 test pins the audit wire shape).
3. `retriever.py` — `load_active_beliefs` + `format_beliefs_digest` (belief-level Oracle guard).
4. `llm_committee.py:294-316` — flag-gated prepend (copy the `HERMES_QUANT_MEMORY_INJECT` idiom).
5. `tests/memory/test_weekly_retro_eval_gate.py` — the held-out/plateau/budget/propose-only gate.
6. `ops/scripts/quant-weekly-retro.py` — mirror `quant-catalyst-profitability.py` silence contract.
7. Cron registration + enablement-runbook one-liner.
8. Verify: full `pytest -q` clean; off-state byte-identical (flag unset → `lessons_block`
   unchanged vs W1); `governance.promotion.evaluate` un-blocks on `weekly_retro_promotion_readiness`.

---

## 9. Acceptance criteria (definition of done)

- `weekly_retro.py` distills winners-vs-losers **by realized alpha** into ≤`N` per-role verbal
  belief-deltas; deterministic, no LLM in promote/expire.
- Every belief carries `oracle_provenance.tau_observable_max` + `half_life_days`; the
  belief-level Oracle guard excludes any belief with `tau_observable_max >= asof`.
- FINMEM access-counter + half-life decay/expire works; the store is bounded by the per-role
  budget cap; `beliefs.jsonl` is a rebuildable append-only projection of `reflections.jsonl`.
- **O3 closed:** a successful weekly pass emits a `promotion_event` with
  `weekly_retro_promotion_readiness=True` (matching `tests/governance/test_promotion.py:39-50`);
  `governance.promotion.evaluate` no longer blocks on that field.
- **O2 closed:** under `HERMES_QUANT_WEEKLY_RETRO=1` + `HERMES_QUANT_MEMORY_INJECT=1`, the PM
  `lessons_block` is prepended with the role-selective beliefs digest.
- **Eval gate green:** held-out OOS digest-injected prompt does NOT regress hit-rate/alpha vs
  no-digest baseline; belief count under cap; all beliefs Oracle-tagged + decaying; half-life
  is plateau-stable under ±20% jitter (not a decimal peak).
- **Default-OFF + byte-identical off-state:** flag unset → no `beliefs.jsonl` read, no digest,
  no behavior change. Cron is silent (no_agent contract) unless a transition occurs.
- **Propose-only:** the module writes ONLY `beliefs.jsonl` + a `promotion_event` row; it never
  imports, calls, or mutates the risk gate, hard limits, sizing ladder, or kill-switch.
  Operator/eval-gated promotion remains the sole path to live policy.
```
