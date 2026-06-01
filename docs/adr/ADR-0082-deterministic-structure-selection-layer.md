# ADR-0082: Deterministic structure-selection layer + registry-open plays

- **Status:** proposed
- **Date:** 2026-05-31
- **Deciders:** operator (Codeseys), hermes-quant architect
- **Supersedes / amends:** none (additive to ADR-0027 options-aware risk gate, ADR-0029 multi-leg
  reactor, ADR-0065/0066 research debate, ADR-0079 PDR)
- **Context study:** [docs/research/2026-05-31-r-strategy-openness-and-horizon.md](../research/2026-05-31-r-strategy-openness-and-horizon.md)

## Context and problem statement

Two recon verdicts surfaced an architecture question on the "strategy" axis:

1. **Plays are a hardcoded, frozen, code-level rule registry** (`playbook/profiles.py:235`), with
   3-4 hand-maintained parallel lists (`scorers.py:337`, `watchlist_evolution.py:55`) that must all be
   edited to add a play. Analysts and aggregators already have an entry-point/YAML discovery seam
   (`recipes.py:256-279`); plays do not.
2. **The agentic deliberation + risk layer is 100% equity/direction-only.** Multi-leg option structure is
   gated structurally by the deterministic `options_gate` (`risk/options_gate.py:345`) via the
   `from_gate_result` unrepresentable-pass seam (`options/multileg.py:99-185`) and is **never
   deliberated**. The judge's `ResearchPlan` (`research_debate/schemas.py:105`) carries only a 5-tier
   `PortfolioRating` + confidence + horizon; there is no leg/Greek/structure field, and `structure_intent`
   does not exist in the codebase (greenfield).

The question: should hermes become "strategy-open" — a pluggable play registry and/or multi-leg structure
chosen inside deliberation? The hard constraint: the deterministic gate must remain the final authority;
no LLM may become a money-path structural authority.

External evidence (4 named agentic-trading repos + production specs + industry) converges: the field
separates "LLM proposes / deterministic layer decides." Structure-selection in production is a
deterministic **stance × IV-regime rule matrix** (ROT-TECH Stage-8, Iyer, VolatilityBox), not debate
output. The one repo that lets an LLM pick structure freely (Vibe-Trading) has **no gate** — the
cautionary case, not the template.

## Decision drivers

- **Rails:** deterministic gate is final authority; LLM/committee = evidence that can only silence
  (ADR-0004/0079 D-1). The `risk_gate_pass=True`-unrepresentable seam must stay intact.
- **Silence-by-default:** absent/ambiguous input must yield the equity path (no structure), never a fire.
- **No new selection authority:** a cross-play argmax/optimizer would compete with the gate (out of scope).
- **Eval-gated, default-OFF, reversible:** money-software discipline.
- **No-lookahead honesty:** any IV-regime input must be as-of-honest (no future IV leakage).

## Considered options

### Option 1 — Status quo (do nothing)
Plays stay hardcoded; multi-leg stays gate-only, never deliberated.
- Good: zero risk; matches TradingAgents (the field default).
- Bad: adding a play remains a 4-file edit; the qualitative reasoning the debate is genuinely good at
  ("range-bound → prefer premium capture") is unreachable; the system cannot express a defined-risk
  structure intent even though the gate already supports the buckets.

### Option 2 — Free LLM structure choice in the debate (the Vibe-Trading model)
Let the bull/bear/judge (or risk committee) pick legs/Greeks/strikes directly.
- Good: maximally expressive.
- Bad: **violates the rails.** Makes the LLM a money-path structural authority; reintroduces
  prompt-sensitivity; risks naked/undefined-risk proposals the personas cannot validate. Deepwiki
  confirmed Vibe-Trading has no deterministic validator over the choice. **Rejected.**

### Option 3 (CHOSEN) — Registry-open plays + a deterministic structure-selection layer fed by a coarse intent
Two parts, both default-OFF and eval-gated:

**Part A — registry-open plays (mechanical):**
- Derive `score_all()`/`PLAY_NAMES`/scorer wrappers from `PROFILES` (single source of truth; kills the
  parallel lists). Pure refactor, zero behavior change.
- Add a YAML/entry-point play loader mirroring `recipes.py:256-279`. New plays load default-OFF; a play
  with no `bias` is treated incompatible (default-bullish silences SHORT, unchanged). **No cross-play
  optimizer** — keep "eligible-on-many."

**Part B — deterministic structure-selection layer:**
- Additive OPTIONAL `structure_intent` enum on `ResearchPlan`
  (`{none, defined_risk_credit, defined_risk_debit, premium_capture, long_premium}`) + a `defined_risk`
  flag. Coarse INTENT, not legs. Absent → equity path. The debate ARGUES intent; it never picks legs.
- A new `options/structure_select.py` maps `(direction, structure_intent, IV-rank/regime) → StrategyKind`
  via a codified table (ROT-TECH Stage-8 / Iyer style; no LLM, no optimization). Selects only among
  gate-admissible buckets (`covered_call/cash_secured_put/defined_risk`); never naked. Out-of-table /
  non-defined-risk → `none` → silence.
- The selected `StrategyKind` feeds the EXISTING producer `recipes.build_multi_leg_proposal` →
  `options_gate` → `from_gate_result`. The gate stays the sole authority on legs/Greeks/max-loss/BPR/
  pin-risk/sizing.
- Risk committee unchanged (no leg/Greek reasoning; only lever = `silence_multiplier ∈ [0,1]`).

- Good: strategy-open at the eligibility layer + structure-aware in deliberation, while the deterministic
  selection table + gate remain final authority — exactly where the production field has converged
  (thesis-agent "math decides, LLM explains"; ai-hedge-fund "LLM picks only from pre-validated set").
  Rails fully preserved.
- Bad: a new contract field + a new module to maintain; the IV-regime matrix is codified assumption (not
  validated edge) and must be eval-gated on hermes' own labeled data; an IV-rank lookahead hazard must be
  controlled.

## Decision

Adopt **Option 3**, in two independently-shippable, dependency-ordered phases, both default-OFF and
eval-gated. Part A first (no money-path rail touched). Part B behind a new structure flag, blocked on the
hardening already owed to `OPTIONS_GATE`/`MULTILEG_REACTOR` (FEATURE-ENABLEMENT.md §2.6-2.8) and on an
as-of-honest IV-regime eval.

## Consequences

**Positive:**
- Plays become pluggable via the same seam analysts/aggregators already use; the parallel-list footgun is
  eliminated.
- Deliberation gains a legitimate, rails-safe role in structure selection at intent granularity.
- The gate remains the only thing that decides what trades.

**Negative / risks (and mitigations):**
- **IV-rank lookahead** — a stance×IV matrix is only honest if IV-rank is computed as-of (no future IV in
  the 52-week window). Mitigation: hold the options/IV path to the same `lookahead_gate`/`bar_alignment`
  discipline; eval must use as-of IV. **A selector that peeks at post-decision IV is a silent leakage bug.**
- **Theta/per-day budget mismatch** — the table must only emit structures whose DTE/theta fit the gate's
  per-day budgets (`options_gate.py:74,79`), or the gate silences everything and the feature looks broken.
  Validate the table against gate dimensions before enabling.
- **Contract drift** — `structure_intent` absence MUST default to the equity path (Pydantic optional,
  default `none`); any consumer assuming the old shape stays safe.
- **Eval honesty** — the matrix thresholds (IV-rank>50 condor, <30 calendar/straddle, >30 CSP) are
  STARTING POINTS to be eval-gated on hermes' own data, not vendor-backtested ground truth.
- **No cross-play optimizer** — deliberately omitted; a best-fit argmax would be a new selection authority
  competing with the gate.

## Rails preserved (test-asserted on implementation)

- `options_gate` remains the sole authority; `from_gate_result` is the only mint path;
  `risk_gate_pass=True` stays unrepresentable elsewhere.
- The discrete sizing ladder `{0,±0.05,±0.10,±0.15,±0.20}` and kill-switch are untouched.
- `structure_intent` absent / no-table-match / non-defined-risk → SILENCE.
- Risk-committee personas gain no leg/Greek lever.
- Off-state of every new flag is byte-identical to today.
