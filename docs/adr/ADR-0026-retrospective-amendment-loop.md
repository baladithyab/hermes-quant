# ADR-0026: Retrospective amendment loop — deterministic postmortems + LLM weekly/monthly retro, proposal-only

**Status**: Proposed
**Date**: 2026-05-24
**Target**: v0.5.0 (Wave D of `docs/plans/2026-05-23-options-daily-retro.md`)
**Related**: ADR-0001 (sidecar/reproducibility), ADR-0002 (analyst protocol/calibration), ADR-0003 (aggregator + EpisodeOutcome), ADR-0004 (risk gate, ¼-Kelly, halts), ADR-0009 (executions back-channel, durable halt), ADR-0010 (settlement journal), ADR-0011 (portfolio reconstruction), ADR-0015 (HITL propose-decide-react), ADR-0016 (autonomous mode), ADR-0021 (PDR recipe runtime), ADR-0023 (deliberative committee aggregator)
**Cross-cuts**: All ADRs (the retro loop reads all-of-system; its outputs may *propose* changes anywhere)

---

## Context

hermes-quant has a complete perceive-decide-react pipeline (ADR-0001 through ADR-0025) and now produces real settlement records: `executions.jsonl`, `journal.md`, and per-trade `EpisodeOutcome` rows in the tick DB. The system can trade. What it cannot yet do is **learn from itself in a structured, auditable way**.

The user directive that motivates this ADR — verbatim from `docs/plans/2026-05-23-options-daily-retro.md` — is that the system should "propose (but not auto-apply) architectural amendments based on its own track record." The plan's headline framing: *the retrospective loop is the architectural change*; the options work is downstream.

Two failure modes we are trying to avoid:

1. **A check-valve-only HITL that never updates the system.** ADR-0015 D8 already established that HITL must be a learning surface (the calibrator updates from rejections). This ADR extends the same principle to the system as a whole: the human shouldn't have to spot patterns across dozens of trades manually. The system should surface candidate patterns; the human should approve or reject the *amendments*, not be the lone pattern-matcher.

2. **An auto-applying "self-improvement" loop that mutates code or risk parameters at runtime.** This is the textbook way money software loses money. Per AGENTS.md: silence-by-default, hard rules over learned policy, money never goes through tools. A retro loop that auto-relaxes the cost-gate threshold or auto-tweaks Kelly multipliers would violate every one of those. The retro loop must be **proposal-only**, with HITL approval that goes through git + ADR amendment, not a runtime patch.

R3 (`docs/research/2026-05-23-r3-retrospective-loop-architectures.md`) surveyed Reflexion, Voyager, MetaGPT, CAMEL, RLHF/Constitutional AI, AlphaGo Zero, military AAR doctrine, and Google SRE blameless postmortems. The closest matches by far are the last two — neither is a learned policy, both are structured human review processes powered by *consistent data capture*. R3's recommendation is to port that pattern, with LLM-driven pattern-spotting layered on top of deterministic data capture.

### What the loop must NOT do (non-negotiable)

Per the plan doc's "Hard rules (preserved from existing AGENTS.md, NOT mutable by retro loop)" section, the retro loop **cannot** propose changes to:

1. Silence-by-default posture (when uncertain, hold cash).
2. Money never goes through plugin tools — CLI + HITL approval only.
3. Discrete action space `{0, ±0.05, ±0.10, ±0.15, ±0.20}` of NAV.
4. No-look-ahead bias (the CI gate stays on).
5. Calibrator update mechanics (per-analyst isotonic + cold-start shrinkage per ADR-0009 §P0-2).
6. Risk envelope: 0.5% NAV daily-loss circuit breaker, 5% strategy drawdown halt, ¼-Kelly sizing.
7. The postmortem schema itself (extensible, not reducible — see §4 below).

These are the system's constitution, in the Constitutional-AI sense (R3 §1.5). They are injected as a preamble into every LLM prompt the loop runs. Any amendment proposal that contradicts them is automatically rejected at the synthesizer stage, and a defense-in-depth re-check fires at the `hermes quant retro approve` boundary.

### What the loop CAN propose

1. Analyst weights per regime (e.g., "kronos = 0.35 when VIX > 25").
2. Gate threshold parameters within their allowed configurable range (e.g., cost threshold `2×` → `2.5×` round-trip cost).
3. New analyst proposals (e.g., "add VIX-based regime classifier").
4. Recipe changes (e.g., "remove symbol X from daily universe").
5. ADR amendments (e.g., "ADR-0004 §Sizing: add 0.025 increment" — which itself requires an ADR amendment in source).
6. New ADR proposals (e.g., "ADR-0031: regime-adaptive risk gating").

Each surfaces as a record in `proposed_amendments.jsonl`. None are applied by the loop. A human runs `hermes quant retro approve <id>` and the change lands as a normal git commit (potentially editing a YAML recipe, potentially editing an ADR markdown via `$EDITOR`, never patching live runtime state).

---

## Decision

Adopt R3's two-layer architecture verbatim, with three implementation specifics:

1. **Layer 0 — deterministic per-trade postmortem.** Zero LLM. Fires from `settlement_loop` when a trade closes. Computes a fixed structured-feature set, appends one record to `~/.hermes/quant/postmortems.jsonl`. Cost: $0 per trade. Latency: <10ms.
2. **Layer 1 — weekly LLM scatter audit.** Sunday 00:00 UTC cron. Reads the last 7 days of postmortems, runs a 3-model adversarial scatter (bull / bear / synthesizer, cross-family per `model-roster` skill), writes a markdown report to `~/.hermes/quant/retro/weekly/<YYYY-WW>.md`, promotes any `MAJOR`+ findings to `proposed_amendments.jsonl`.
3. **Layer 2 — monthly meta-retro.** 1st-of-month 00:00 UTC cron. Same scatter shape but reads the *weekly reports* as input. Detects cross-week patterns, validates whether week-N findings persist into week-N+1, drafts meta-amendments.
4. **HITL approval gate.** A new CLI surface (`hermes quant retro {list,show,approve,reject,week,month}`) with explicit confirmation prompts and git-tracked artifact mutation only.

```
┌──────────────────────────────────────────────────────────────┐
│ LAYER 0  — Deterministic per-trade postmortem (no LLM)       │
│   settlement_loop → 20+ structured fields → postmortems.jsonl│
│   cost: $0   latency: <10ms   replayable: yes                │
└──────────────────────┬───────────────────────────────────────┘
                       │  (Sunday 00:00 UTC cron)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ LAYER 1  — Weekly LLM scatter audit                          │
│   Bull (family A) → finds positive patterns                  │
│   Bear (family B) → falsifies the Bull's findings            │
│   Synthesizer (family C) → consolidates, checks immutables,  │
│                            classifies severity, drafts       │
│                            amendment proposals               │
│   cost: ~$0.50/week (mixed-family); ~$3/month at worst       │
└──────────────────────┬───────────────────────────────────────┘
                       │  (1st-of-month 00:00 UTC cron)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ LAYER 2  — Monthly meta-retro                                │
│   Same scatter shape, input = last 4 weekly reports          │
│   Validates persistence; drafts meta-amendments              │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ HITL gate  — `hermes quant retro approve <id>`               │
│   Re-checks immutables. Patches recipe YAML or opens $EDITOR │
│   on ADR markdown. Auto-commits via git. Records approver,   │
│   timestamp, commit hash in proposed_amendments.jsonl.       │
│                                                              │
│   NO automatic application. Human reviews every amendment.   │
└──────────────────────────────────────────────────────────────┘
```

### D1 — Layer 0: PostmortemRecord schema

Per R3 §2, the postmortem captures enough structured features that the weekly LLM audit can spot patterns without needing raw market data. Defining 20+ fields in seven groups (identity, predicted-vs-actual, slippage/execution, gate interaction, per-analyst contributions, market regime at decision time, deterministic outcome class). Pydantic for validation, atomic line-append per the `signals.jsonl` protocol from ADR-0009 §P0-8 (≤4096 byte lines, `O_APPEND`, single-writer, fsync).

```python
# hermes_quant/retro/postmortem.py
from pydantic import BaseModel
from typing import Literal, Optional
from datetime import datetime

class AnalystContribution(BaseModel):
    analyst_name: str
    direction: Literal[-1, 0, 1]
    confidence: float          # calibrated, post-shrinkage
    weight: float              # weight in aggregator mix
    direction_correct: bool
    calibration_delta: float   # ECE delta post-update

class PostmortemRecord(BaseModel):
    schema_version: int = 1

    # Identity
    trade_id: str              # mirrors signal_id from signal bus
    symbol: str
    asset_class: Literal["equity", "crypto", "etf", "fx", "option"]
    asof_decision: datetime    # UTC, signal emission
    asof_settlement: datetime  # UTC, exit fill
    hold_minutes: float

    # Predicted vs actual
    predicted_direction: Literal[-1, 0, 1]
    realized_direction: Literal[-1, 0, 1]
    direction_correct: bool
    predicted_magnitude_pct: float
    realized_return_pct: float        # NET of fees
    magnitude_error_pct: float
    predicted_confidence: float
    time_to_target_minutes: Optional[float]  # null if never reached

    # Slippage & execution
    entry_slippage_bps: float
    exit_slippage_bps: float
    total_fees_pct: float
    realized_pnl_net: float

    # Gate interaction
    gate_action: Literal["pass", "reject"]
    gate_rejection_reason: Optional[str]
    kelly_fraction: float
    position_size_pct: float
    gate_override: bool        # human override per ADR-0015

    # Per-analyst (typically 4-6 entries)
    analyst_contributions: list[AnalystContribution]

    # Market regime at decision
    regime_vix_level: Optional[float]
    regime_volatility_percentile: float
    regime_trend_strength: float
    regime_correlation_to_spy: float
    regime_time_of_day_utc: Literal["pre_market", "regular", "after_hours"]
    regime_day_of_week: int    # 0=Mon..4=Fri (UTC)

    # Outcome classification (deterministic)
    thesis_held: bool          # direction_correct AND realized_return_pct > 0
    outcome_class: Literal["win", "loss", "scratch"]  # scratch = ±0.1%
    was_gate_rejection_correct: Optional[bool]  # only if gate_action=="reject"
```

`settlement_loop` writes one `PostmortemRecord` per closed trade. The fields are computable from `signal_bus + execution_bus + market_data` — no external state, no LLM call, fully replayable. The `was_gate_rejection_correct` field requires a counterfactual (would the rejected trade have been profitable?), computed from realized bars over the trade's would-be hold window — a deterministic post-hoc check.

### D2 — Layer 1: Weekly scatter shape (Bull / Bear / Synthesizer)

Per R3 §3.1: **role-decomposition adversarial scatter, NOT confirmation-bias-prone single-prompt-3-models, NOT iterative critique chains.**

| Role | Family | Posture | Output |
|------|--------|---------|--------|
| **Bull** | Family A (e.g., anthropic) | "Find systematic patterns of CORRECT predictions. What is the system doing right? Propose amendments that AMPLIFY strengths." | `bull_findings.json` |
| **Bear** | Family B (e.g., google) | "For each Bull finding, attempt to FALSIFY it. Check sample size, alternative explanations, regime confounding, ADR contradictions." | `bear_falsifications.json` |
| **Synthesizer** | Family C (e.g., deepseek) | "Read both. Promote only findings that survived adversarial scrutiny. Re-check against immutables. Classify severity. Draft amendment proposals." | Final weekly markdown report + `amendment_proposals.json` |

Critical invariants:

- **Bull and Bear NEVER see each other's outputs.** Synthesizer is the first stage where they meet. This prevents sycophancy.
- **Cross-family enforcement.** The audit fails (no fallback, no substitute) if three different families per `model-roster` cannot be selected. Per R3 §6.2: same-family scatters produce convergent-but-wrong findings.
- **Immutable rules injected as preamble** in all three prompts. Synthesizer has an explicit contradiction check; defense-in-depth re-check fires at approve time.
- **Postmortems are shuffled** before going into the prompts so the LLM can't anchor on chronology. Aggregate stats (win rate, average return, Sharpe-like ratio for the period) are provided as a header so the LLM has a distributional anchor.
- **Minimum sample size enforcement** in the Bear's prompt: reject any finding based on <5 trades; flag findings based on 5–15 trades as `LOW CONFIDENCE` regardless of the Bull's enthusiasm.
- **Severity classification** in the Synthesizer: only `CRITICAL` or `MAJOR` findings become amendment proposals. `MINOR` and `COSMETIC` are recorded in the weekly report but not promoted.

Prompt templates ship as version-pinned files in `hermes_quant/retro/prompts/{bull,bear,synthesizer}_v1.md` so any change to the prompt bumps a version and is itself a git-tracked artifact.

### D3 — Layer 2: Monthly meta-retro

Same scatter shape (3 cross-family models). Input = last 4 weekly reports + current month's `proposed_amendments.jsonl`. Synthesizer questions:

- Do weekly findings persist or fade across weeks?
- Are amendments from week N validated by week N+1's data?
- Any systemic pattern invisible at weekly granularity?
- Any proposed amendment contradicting an existing ADR?
- Is the retro loop itself drifting (over-weighting recent wins, sycophancy markers)?

Per R3 §6.1, this is a built-in holdout test against recency bias.

### D4 — `proposed_amendments.jsonl` schema

Reuses the proposals.py pattern from ADR-0015 D2 (JSONL append-only source-of-truth + SQLite index). Per R3 §4, the schema:

```python
class AmendmentProposal(BaseModel):
    schema_version: int = 1

    # Identity
    amendment_id: str          # "amd_2026-05-31T000000Z_a1b2c3"
    state: Literal["proposed", "approved", "rejected", "superseded"]
    proposed_at: datetime
    proposing_audit_id: str    # "weekly-2026-W22" or "monthly-2026-05"

    # Severity & scope
    severity: Literal["CRITICAL", "MAJOR", "MINOR", "COSMETIC"]
    scope_type: Literal[
        "adr_amendment", "parameter_change", "analyst_weight",
        "gate_threshold", "recipe_change", "new_analyst",
        "remove_analyst", "code_change", "other"
    ]
    scope_target: str          # "ADR-0004 §Sizing" or "recipes/socalminh-cc.yaml::filters[2]"
    scope_adr_refs: list[str]

    # Evidence
    evidence_summary: str
    evidence_postmortem_ids: list[str]  # immutable link to specific trades
    evidence_statistic: str    # "7/9 high-VIX trades had wrong direction (binom p=0.04)"
    evidence_sample_size: int
    evidence_confidence: Literal["HIGH", "MEDIUM", "LOW"]

    # Proposed change
    proposed_change: str
    proposed_diff: Optional[str]  # unified diff if scope is code or YAML
    proposed_parameter_before: Optional[float | str]
    proposed_parameter_after: Optional[float | str]

    # Predicted impact
    predicted_impact_summary: str
    predicted_metric: str      # "win_rate" | "sharpe" | "max_drawdown" | ...
    predicted_direction: Literal["increase", "decrease", "stabilize"]
    predicted_magnitude_estimate: str   # "~2-5% improvement"

    # PREMORTEM (mandatory; per ADR template)
    premortem_what_could_go_wrong: str
    premortem_worst_case: str
    premortem_detection: str   # observable metric for failure detection
    premortem_reversibility: str  # specific revert steps

    # Review provenance
    bull_model: str            # "anthropic/claude-opus-4.7"
    bear_model: str
    synthesizer_model: str
    bear_falsification_result: Literal["CONFIRMED", "WEAK", "FALSIFIED"]

    # Approval (filled on approve/reject)
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_commit_hash: Optional[str] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    # Defense-in-depth
    passes_immutable_rules: bool   # synthesizer-verified; re-checked at approve
```

The `evidence_postmortem_ids` field is the crucial integrity link: an amendment cannot be retroactively justified by trades that didn't exist. If those postmortems are later flagged as anomalous (e.g. data-quality issue), the amendment's evidence is automatically weakened in the next monthly retro.

### D5 — HITL approval CLI

Per R3 §5:

```
hermes quant retro list   [--severity MAJOR] [--state proposed]
hermes quant retro show   <amendment_id>
hermes quant retro approve <amendment_id> [--yes]
hermes quant retro reject  <amendment_id> --reason "..."
hermes quant retro week    [--dry-run] [--since YYYY-MM-DD]
hermes quant retro month   [--dry-run]
```

`approve` workflow (the critical one):

1. Validate amendment is in `proposed` state (else error).
2. **Re-check immutable rules** (defense-in-depth: don't trust the synthesizer's `passes_immutable_rules` flag alone — recompute).
3. Print proposed change, predicted impact, premortem, bear falsification result.
4. Prompt for confirmation (bypassed by `--yes` for scripting).
5. Apply by `scope_type`:
   - **`parameter_change` / `analyst_weight` / `gate_threshold` / `recipe_change`** → patch the user's recipe at `~/.hermes/quant/recipes/<profile>.yaml`. Recipe is git-tracked. CLI auto-commits with message referencing `amendment_id`. Daemon reads the recipe at next tick (no runtime mutation).
   - **`adr_amendment`** → generate a draft ADR amendment markdown stub, open in `$EDITOR` for the user to finalize, on save: `git add` + `git commit` referencing `amendment_id`. ADRs always go through human editing — no auto-generated ADR text.
   - **`code_change`** → apply stored diff via `git apply`, then `git add` + `git commit`. Defense-in-depth here: if the stored diff fails to apply cleanly, abort and require human intervention rather than 3-way-merge auto-resolution.
6. Mark amendment `approved` in JSONL, record `approved_at`, `approved_by` (CLI user / Discord ID), `approved_commit_hash`.

**Why recipe YAML patching, not a runtime mutation, for parameter changes** (per R3 §5.2):
- Runtime mutation creates non-reproducible state — direct ADR-0001 violation.
- The recipe IS the config. Changing it produces a git diff.
- Rollback is `git revert` of a single commit.
- The retro loop's actions are auditable through normal source-control history.

**Why not always a PR** (for solo operators):
- For a one-person paper-trade operator, PR overhead is excessive.
- For parameter tweaks, the git commit IS the review trail.
- For ADR amendments and code changes, the `$EDITOR` flow forces human review before the commit.

### D6 — Cron schedule

Two cron entries land in `~/.hermes/cron/jobs/`:

```yaml
# hermes-quant-retro-week.yaml
schedule: "0 0 * * 0"      # Sunday 00:00 UTC
command: "hermes quant retro week"
delivery: "discord:home"
no_agent: true              # the LLM scatter happens INSIDE the command, not via Hermes chat
notify_on_complete: true

# hermes-quant-retro-month.yaml
schedule: "0 0 1 * *"       # 1st of month 00:00 UTC
command: "hermes quant retro month"
delivery: "discord:home"
no_agent: true
notify_on_complete: true
```

Both produce a Discord brief on completion: count of `MAJOR`+ findings, count of new amendment proposals, link to the report markdown, suggested next action (`hermes quant retro list --state proposed`).

### D7 — Cost ceiling

Per R3 §7, even at worst-case Claude-for-all-roles pricing, total monthly cost is ~$3. With the recommended mixed-family approach (cheaper models for Bull/Bear, Claude or DeepSeek for Synthesizer), it's ~$1.40/month. The architecture documents a hard cost ceiling of **$30/month** for the retro loop in `~/.hermes/quant/retro/budget.yaml`; if the running 30-day cost exceeds this ceiling, the next scheduled audit aborts with a Discord alert and requires manual `hermes quant retro week --force`. This is a slow-failure protection, not a fast-fix.

```yaml
# ~/.hermes/quant/retro/budget.yaml
hard_monthly_cap_usd: 30.00
soft_warn_at_usd: 15.00
trip_action: abort_next_audit_unless_forced
recompute_window_days: 30
```

The audit code records token counts and provider unit prices into `~/.hermes/quant/retro/cost_log.jsonl` per call so the running 30-day cost is computed deterministically from disk.

### D8 — First retro: Sunday 2026-05-31

Per R3 §8 and the plan doc §Phase 6: the first weekly retro fires Sunday 2026-05-31 with whatever paper trades have accumulated. Expected sample: 0–5 trades. The audit may produce zero findings — this is fine and expected. The pipeline runs end-to-end and the Bear correctly rejects underpowered findings as `<5 trades → reject`. By Week 4 (~20–40 trades), the first `MAJOR` findings should emerge.

---

## Consequences

### Positive

- **Postmortems are deterministic and free.** Every closed trade emits one record, on-disk, structured, replayable. The data layer for retrospection exists even if Layers 1 and 2 are disabled.
- **Two-layer cost shape.** The expensive layer is bounded by cron cadence, not trade volume. Adding more analysts or symbols does not scale retro cost.
- **Adversarial scatter prevents sycophancy.** Bull/Bear/Synthesizer with cross-family enforcement is a structurally stronger guarantee than single-prompt-with-self-critique.
- **Proposal-only output.** No surprise runtime mutation. Every amendment passes through `git commit` and a confirmation prompt. The system cannot loosen its own risk rules.
- **Cost ceiling.** Hard $30/month cap; running cost actually ~$1.40/month at typical mixed-family pricing.
- **Loops back into existing infrastructure.** Reuses ADR-0015's proposals.py pattern; reuses ADR-0021's recipe runtime; reuses ADR-0023's cross-family scatter discipline. Not a new parallel system.

### Negative

- **Adds cognitive load.** Operators must understand: weekly retros, monthly retros, amendment proposals, and an approval CLI. This is real complexity. Mitigation: `quant_doctor` (ADR-0007) gets a "retro status" section showing pending amendments and last weekly summary.
- **LLM scatter latency.** Weekly audit takes ~2–5 minutes wall clock. Monthly meta-retro takes ~10 minutes. Bound but visible.
- **Sample-size anxiety in early weeks.** Until ~20–40 settled paper trades, weekly retros will produce mostly empty reports. Operator must trust the silence.
- **The loop can find spurious patterns.** Even with adversarial scatter, three LLMs can converge on a pattern that isn't real. Defense: every amendment is gated by `evidence_sample_size`, `bear_falsification_result`, and human approval. The loop can propose nonsense; it cannot apply it.
- **Failure of the cron leaves no observable signal.** Mitigation: the cron entries use `notify_on_complete: true` which surfaces success or failure. If the audit silently never runs, that itself is detectable from `~/.hermes/quant/retro/weekly/` lacking new entries — `quant_doctor` includes a "last successful weekly retro" timestamp.

### Risk: the loop converges on "just mute the worst analyst"

Per R3 §1.8 (Google SRE blameless postmortems): the prompts explicitly instruct *systemic* causes, not individual blame. The Synthesizer is told: "Findings of the form 'analyst X is bad, remove it' are flagged as `LOW_CONFIDENCE` and require ≥30 trades of evidence and a regime breakdown showing the analyst is bad in *all* regimes, not just one." We want findings like "classical_ta underperforms in high-VIX regimes" (regime-conditioned), not "classical_ta is bad" (blame-driven).

### Risk: an amendment retroactively justifies a bad trade

Per R3 §6.3: amendments are forward-looking proposals, not retroactive fixes. The `evidence_postmortem_ids` field creates an immutable link to specific trades. Trades cannot be rewritten. If those trades were anomalous (e.g. data-quality issue surfaced later), the amendment's evidence is weakened in the next monthly retro and may be flagged for reversion.

### Risk: amendment proposals stack up unreviewed

If the operator never runs `hermes quant retro list`, proposals accumulate. Mitigation: weekly Discord summary includes "N pending amendments awaiting review (oldest: <date>)". After 30 days unreviewed, an amendment auto-transitions to `state: superseded` (as if rejected for staleness) — avoids permanent dangling state.

---

## Alternatives Considered

### A1: Reflexion-style critique-then-rerun (rejected)

Reflexion (R3 §1.1) injects a verbal self-reflection into the next attempt at the same task. Trading violates all of Reflexion's preconditions: tasks aren't identical (each market moment is unique), feedback is delayed, and there's no "rerun." Adopting Reflexion would mean injecting last week's reflection into next week's tick-loop context — which would create a non-reproducible decision path (the daemon's behavior would depend on retro output, which depends on settlement, which depends on prior decisions). This breaks ADR-0001's reproducibility constraint.

### A2: AlphaGo Zero-style self-play (rejected)

R3 §1.6: trading isn't a perfect-information zero-sum game. There is no opponent. Outcome signal is noisy. The policy improvement guarantee of self-play does not transfer.

### A3: Auto-applying proposals with a "high-confidence" threshold (rejected)

Tempting: "if the synthesizer says CRITICAL with >0.95 confidence and the bear says CONFIRMED, just apply it." This is exactly the pattern that destroys money software. The system would eventually find a way to game its own confidence outputs (RLHF reward-hacking, but for amendments). Hard rule: no auto-apply, ever, regardless of stated confidence.

### A4: Single-LLM critic instead of Bull/Bear/Synthesizer scatter (rejected)

Single-LLM is cheaper but vulnerable to confirmation bias and sycophancy (R3 §3.1). The cost difference is ~3× ($0.50/week vs $0.17/week), and we are operating at <2% of the $30/month budget. Spend the money on adversarial diversity.

### A5: Vector-store memory of past trades (rejected)

R3 explicitly cites TradingAgents v2's removal of `FinancialSituationMemory` (Chroma-backed) in favor of a flat tail. ADR-0010 already established hermes-quant's stance: no embeddings, no vector store, no BM25, flat append-only markdown with deterministic retrieval. This ADR continues that stance — postmortems are flat JSONL; weekly audit reads the flat tail, not a vector index.

### A6: Per-trade LLM postmortem instead of deterministic (rejected)

Cost: ~$0.05 per trade × ~80 trades/month = ~$4/month. Acceptable in absolute terms but breaks reproducibility (LLM output varies across calls; postmortems would not replay byte-identically from disk). Also: the LLM has no information the deterministic computation lacks at the per-trade level. The LLM's value-add is *cross-trade pattern spotting*, which is a weekly-cadence concern.

---

## Open Questions

1. **Should the monthly meta-retro be allowed to amend the retro loop's own prompts?** Currently no — prompts are version-pinned files. This means a bug in the bull's prompt (e.g. it consistently misses a class of pattern) requires human-author intervention. Alternative: meta-retro can propose prompt amendments via `scope_type: "code_change"` with the prompt file as `scope_target`. Open for v0.6.0 deliberation.

2. **What's the right cadence for newly-onboarded recipes?** A new recipe with <30 trades should not be amended by retro findings (sample too small). But the loop currently applies the same MIN_SAMPLES logic across all recipes. Consider per-recipe maturity gates.

3. **Should the retro loop have access to backtest results, not just live paper?** Mixing backtest + paper would dilute the live signal but increase sample size. Lean toward: keep them separate, run separate retro tracks for backtest vs paper. Defer to first month of paper-trade data.

4. **Cross-symbol pattern spotting at weekly granularity vs monthly.** Current design: weekly is per-recipe + per-symbol; monthly is cross-symbol/cross-recipe. May need a third tier (e.g., daily → weekly → monthly) for high-volume strategies. Defer.

5. **Should rejected amendments enter a "lessons" memory that informs future audits?** Per R3's caveat against vector stores: probably yes, but as a flat list of "previously rejected" amendment IDs that the next synthesizer is told to check against ("don't re-propose this"). Defer to v0.6.0.

---

## Implementation Sketch

```
hermes_quant/retro/
├── __init__.py
├── postmortem.py          # PostmortemRecord + writer + replay
├── postmortem_writer.py   # settlement_loop hook
├── audit.py               # weekly + monthly audit drivers
├── scatter.py             # Bull/Bear/Synthesizer dispatch + cost log
├── amendment.py           # AmendmentProposal + JSONL+SQLite store
├── prompts/
│   ├── bull_v1.md
│   ├── bear_v1.md
│   ├── synthesizer_v1.md
│   ├── monthly_bull_v1.md
│   ├── monthly_bear_v1.md
│   └── monthly_synthesizer_v1.md
├── budget.py              # cost-cap enforcement
└── cli.py                 # hermes quant retro {list,show,approve,reject,week,month}

~/.hermes/quant/
├── postmortems.jsonl      # Layer 0 output
├── proposed_amendments.jsonl
├── proposed_amendments.db (SQLite index, derivable)
├── retro/
│   ├── weekly/<YYYY-WW>.md
│   ├── monthly/<YYYY-MM>.md
│   ├── cost_log.jsonl
│   └── budget.yaml
```

Implementation order matches plan doc Wave D:

- **D0** (Wave A): `PostmortemRecord` dataclass + `settlement_loop` hook + replay-from-disk test (Layer 0 only). Ships independently of Layers 1–2.
- **D1**: `hermes quant retro week` CLI + Bull/Bear/Synthesizer prompt files + scatter dispatch.
- **D2**: `hermes quant retro month` CLI + monthly prompts.
- **D3**: `proposed_amendments.jsonl` + SQLite index + `AmendmentProposal` schema.
- **D4**: `hermes quant retro {list,show,approve,reject}` CLI + recipe-YAML patcher + `$EDITOR` flow for ADR amendments.
- **D5**: Sunday + monthly cron entries; first retro fires 2026-05-31.

Hot-path cost: only D0 runs in the daemon settlement_loop. D1–D5 are out-of-band cron-driven processes; the daemon never blocks on them.

---

## Test Plan

### Layer 0 (deterministic postmortem)

1. **Schema round-trip**: `PostmortemRecord` serializes and deserializes without loss; line-length stays ≤4096 bytes for the realistic-max field set.
2. **Atomic-write fence**: under simulated SIGKILL during write, `postmortems.jsonl` ends on a complete line; partial lines are detectable on read.
3. **Replay-from-disk**: feed a fixture of 50 settled trades through `settlement_loop.write_postmortem`, then read back: identical records, byte-equal.
4. **`was_gate_rejection_correct` correctness**: for synthetic trades where the gate rejected and the realized bars showed a profitable would-be position, the field is `False` (gate rejected a winner); for trades where rejection was correct, `True`. Property test via hypothesis.

### Layer 1 (weekly scatter)

1. **Cross-family enforcement**: with two of the three required families unavailable in the model roster, the audit aborts with `RetroFamilyDiversityError`, does NOT substitute same-family.
2. **Bull–Bear isolation**: the Bull's prompt does not contain Bear output; the Bear's prompt does not contain Bull output. Verify via prompt logging.
3. **Immutable rules contradiction trap**: feed a fabricated bull finding that contradicts ADR-0004's silence-by-default (e.g., "always go long on 2σ moves"). The synthesizer must reject it; the test verifies `passes_immutable_rules == False` and the proposal is NOT promoted.
4. **Sample-size guard**: with only 3 postmortems in the input, no amendment is promoted regardless of confidence. Bear's sample-size enforcement fires.
5. **Cost-cap enforcement**: with a fixture cost log showing $29 already spent in the trailing 30 days, the next audit aborts unless `--force` is passed.

### Layer 2 (monthly meta-retro)

1. **Persistence test**: a finding present in week 1 but absent in weeks 2–4 is flagged by the synthesizer as "transient." A finding present in 3+ of 4 weeks is flagged as "persistent."
2. **ADR contradiction detection**: a fabricated weekly amendment that proposes loosening the action step is flagged by the meta-retro synthesizer and recorded with `bear_falsification_result: FALSIFIED`.

### HITL approval

1. **Re-check immutables**: an approved-via-CLI workflow still re-checks immutable rules at the CLI boundary, even when the synthesizer has already verified them. Defense-in-depth.
2. **Recipe-YAML patch round-trip**: approve a `parameter_change` amendment, verify the recipe YAML is patched at the correct field, the change is git-committed with a message containing the `amendment_id`, and the recipe YAML's hash changes (so the daemon picks it up on next read).
3. **ADR amendment $EDITOR flow**: simulated `$EDITOR` (writes a fixed string) — verify the ADR markdown is opened, the operator's edits are committed, the amendment record's `approved_commit_hash` matches.
4. **Reject path**: `hermes quant retro reject <id> --reason "..."` writes `state: rejected` and records `rejection_reason` without any git mutation.
5. **Stale auto-supersede**: an amendment that has been `proposed` for >30 days transitions to `superseded` automatically (cron-driven sweep).
6. **Cannot approve a contradicting amendment**: even with `--yes`, an amendment that fails the re-check is rejected at the CLI boundary with a non-zero exit code.

### End-to-end smoke

1. **First retro on tiny sample**: simulate 4 paper trades over 7 days; run `hermes quant retro week --dry-run`. Expected: report markdown produced, zero amendment proposals (sample too small), no errors. This test runs in CI weekly to verify the pipeline doesn't regress.
2. **First retro on rich sample**: simulate 40 paper trades with a planted regime-conditioned pattern (e.g., classical_ta wrong in 8 of 9 high-VIX trades); run `hermes quant retro week`. Expected: at least one `MAJOR` finding mentioning "high-VIX" and "classical_ta," with `evidence_sample_size: 9` and `bear_falsification_result: CONFIRMED`.

---

## References

- `docs/plans/2026-05-23-options-daily-retro.md` — Phase 4 Wave D maps to this ADR.
- `docs/research/2026-05-23-r3-retrospective-loop-architectures.md` — full prior-art survey, prompt templates, cost model. **This ADR is a near-direct port of R3's recommendations.**
- ADR-0001 §Reproducibility — postmortem replay-from-disk requirement.
- ADR-0009 §P0-8 — JSONL atomic-write protocol that postmortems.jsonl follows.
- ADR-0010 — settlement journal (the *human-readable* sidecar; postmortem is the *structured* sidecar).
- ADR-0015 D2 — proposals.jsonl pattern that proposed_amendments.jsonl mirrors.
- ADR-0023 — cross-family scatter discipline (committee aggregator); the retro loop reuses the same family-diversity invariant.
- AGENTS.md "Things to NEVER do" #6, #7 — preserve the action-space and risk-gate untouchable surfaces. The retro loop respects both.
