# R3: Retrospective Loop Architectures — Research & Recommendation

> **Anchor model:** deepseek/deepseek-v4-pro (math/correctness lens)
> **Target ADR:** ADR-0026 (Retrospective Amendment Loop)
> **Posture:** PROPOSAL-ONLY, HITL-gated, deterministic postmortem layer, LLM scatter on weekly+ cadence

---

## Executive Summary: The Recommended Architecture

**Build a two-layer retrospective loop:**

1. **Layer 0 — Per-trade deterministic postmortem** (no LLM, fires from `settlement_loop`). Computes a fixed set of 20+ structured features per closed trade. Appends to `postmortems.jsonl`. Zero cost, infinitely replayable.
2. **Layer 1 — Weekly LLM scatter audit** (3-model cross-family scatter: bull, bear, synthesizer). Reads the last week's postmortems, produces a markdown report with findings. Findings with severity ≥ `MAJOR` are promoted to amendment proposals in `proposed_amendments.jsonl`.
3. **Layer 2 — Monthly meta-retro** (same scatter shape, but reads weekly reports as input). Detects cross-week patterns, overfit trends, and contradictions with ADRs.

**The loop is NOT:**
- A critique-then-rerun pattern (Reflexion-style). hermes-quant cannot rerun settled trades.
- A self-play improvement loop (AlphaGo-style). The system does not play against itself.
- An auto-apply mechanism. All amendments are proposals only. Approval is CLI + git commit.

**The loop IS:**
- A structured after-action review (AAR) with deterministic data capture (Layer 0) feeding pattern-spotting LLM audits (Layer 1) with cross-family adversarial diversity (bull/bear/synthesizer).

---

## 1. Architectural Pattern Survey

### 1.1 Reflexion (Shinn et al., NeurIPS 2023)

**What it does:** An LLM agent receives binary/scalar feedback from an environment, generates a verbal self-reflection ("I should have done X instead of Y"), and stores this reflection in an episodic memory buffer. The reflection is injected as context in the next trial. This is "verbal reinforcement learning" — no weight updates, just linguistic feedback.

**Loop-back mechanism:** Critique-then-rerun. The agent gets another attempt at the SAME task with reflection context added. Memory persists across episodes so the agent accumulates lessons.

**Relevance to hermes-quant: LOW.** Reflexion assumes (a) identical tasks on retry, (b) binary success/failure signals, (c) immediate feedback. Trading has none of these: each trade is a unique market moment, feedback is delayed (settlement), and there's no "rerun with better strategy" — the market has moved on.

**What we SHOULD steal:** The episodic memory buffer concept. Instead of injecting reflections into the next trade attempt (which would break reproducibility), we inject LAST WEEK'S POSTMORTEM SUMMARY into the weekly audit prompt. The "memory" is a flat tail of structured postmortems, not a learned latent space.

### 1.2 Voyager (Wang et al., 2023)

**What it does:** A Minecraft agent with three components: (1) automatic curriculum for exploration, (2) skill library of executable code, (3) iterative prompting mechanism with environment feedback + execution errors + self-verification critic. Skills that pass self-verification are committed to the library.

**Loop-back mechanism:** Critique-into-skill-commit. The self-verification critic confirms task completion before a program is added to the library. The skill library grows over time, enabling composition of complex behaviors from simpler ones.

**Relevance to hermes-quant: MEDIUM.** The self-verification critic pattern is directly applicable. Voyager uses a separate LLM call to verify whether a program achieved its task. We can use a separate LLM (the "bear" in our scatter) to verify whether the "bull" findings hold up under adversarial scrutiny.

**What we SHOULD steal:**
- **Self-verification critic:** A second model that tries to falsify the first model's findings.
- **Skill library as amendment library:** Instead of code skills, we accumulate `proposed_amendments.jsonl` as a library of "what we learned." Approved amendments become the system's learned improvements.
- **Iterative refinement with max retries:** Voyager caps at 4 rounds of code generation. We cap the weekly audit at 1 pass (no iterative refinement — costs balloon).

### 1.3 MetaGPT (Hong et al., 2024)

**What it does:** A multi-agent software company with specialized roles (PM, architect, engineer). Uses executable feedback — the Engineer writes unit tests, runs code, and self-corrects based on test results. Also explores a "self-referential mechanism" where agents review past project feedback and update their constraint prompts before new projects.

**Loop-back mechanism:** Critique-into-constraint-prompt-update. Each agent's system prompt is revised based on lessons from previous projects. The loop is "at the start of each new project, review feedback and adjust."

**Relevance to hermes-quant: HIGH.** The self-referential constraint-prompt update is exactly what the monthly meta-retro should do: review weekly findings, detect systemic patterns, and propose amendments to how the system operates (ADR parameters, analyst weights, gate thresholds).

**What we SHOULD steal:**
- **Executable feedback for code:** Already partly done (settlement loop verifies directional correctness). Extend to: "did the risk gate reject a trade that would have been profitable? did it allow one that lost?" — these are executable checks against realized outcomes.
- **Self-referential prompt updates:** The monthly retro reads weekly reports and asks: "What recurring patterns are visible across weeks that a single-week audit would miss?"
- **SOP-based review structure:** MetaGPT's structured workflow (requirements → design → code → test → review) maps to our AAR structure (what was planned → what happened → why → what to change).

### 1.4 CAMEL Critic-in-the-Loop (Li et al., 2023)

**What it does:** A role-playing framework with AI User, AI Assistant, and optional AI Critic. The Critic evaluates proposals from both agents and provides verbal feedback with reasoning. The Critic's feedback is injected as additional context for subsequent rounds.

**Loop-back mechanism:** Critique-inject-into-conversation. The Critic is a third agent that observes the conversation and interjects with evaluations. It selects among options and explains its reasoning.

**Relevance to hermes-quant: MEDIUM.** The critic-in-the-loop pattern maps to our scatter design. But CAMEL's critic operates in real-time conversation; our "critic" operates in a batched weekly audit. The key insight: having a distinct agent whose ONLY job is to evaluate (not propose) reduces sycophancy.

**What we SHOULD steal:**
- **Dedicated critic role with explicit criteria:** The weekly scatter's "bear" is a critic. It doesn't propose solutions — it tries to shoot down the bull's findings. Only findings that survive adversarial critique are promoted.
- **Critic criteria as a structured prompt:** Instead of "find problems," the bear gets: "For each finding below, attempt to falsify it by: (1) checking if the sample size is sufficient, (2) checking if an alternative explanation fits the same data, (3) checking if the finding contradicts any existing ADR."

### 1.5 RLHF / Constitutional AI (Anthropic, 2022-2023)

**What it does:** Two-phase self-improvement: (1) SL phase: model generates self-critiques and revisions based on constitutional principles, (2) RL phase: AI-generated preference labels train a reward model, which trains the policy via PPO. Anthropic's iterated online RLHF re-deploys fresh models weekly to collect new comparison data.

**Loop-back mechanism:** Week-over-week model retraining. Each week's policy is used to collect new preference data at the upper end of the score distribution, which trains a better reward model for the next week's policy.

**Relevance to hermes-quant: LOW-MEDIUM.** We are not retraining models. But the cadence pattern is spot-on: weekly iteration with fresh data. And the "constitution" concept — a fixed set of principles the system cannot violate — maps to hermes-quant's non-negotiable rules (silence-by-default, money-never-through-tools, etc.).

**What we SHOULD steal:**
- **Weekly cadence with fresh data:** Each week's audit sees new postmortems, not a cumulative corpus that drowns recent signals.
- **Fixed constitution (non-negotiable rules):** The retro loop prompt MUST include the immutable rules as a preamble. Any amendment proposal that contradicts them is an automatic rejection (flagged by the synthesizer).
- **AI feedback replacing human labels:** The weekly scatter IS the AI feedback. Human approval is the final gate but the analysis is AI-driven.

### 1.6 AlphaGo Zero / AlphaZero (DeepMind, 2017-2018)

**What it does:** A neural network plays games against itself. MCTS search uses the current network to evaluate positions. Game outcomes (win/loss/draw) are used to train the next iteration of the network. The new network plays against the old; if it wins, it replaces it.

**Loop-back mechanism:** Self-play → outcome → retrain → stronger network → better self-play. The improvement signal is the binary game outcome. The policy improvement theorem guarantees monotonic improvement under ideal conditions.

**Relevance to hermes-quant: LOW.** Trading is not a perfect-information game. There is no "opponent" to self-play against. Outcomes are noisy and non-binary. The policy improvement guarantee from self-play does not transfer.

**What we SHOULD steal (carefully):**
- **The evaluator pattern:** AlphaGo Zero used a separate evaluator to determine whether the new network was better than the old. We can adopt this: the monthly retro compares the current system's performance to the previous month's, using the same metrics.
- **Win/loss as signal, not reward:** In trading, "did this trade's direction match the signal's direction" is the closest analogue to a game outcome. The per-trade postmortem captures this binary.

### 1.7 Military AAR (US Army FM 25-101 / TC 7-0)

**What it does:** A structured, facilitated discussion after every training event or combat operation. Four questions: What was supposed to happen? What actually happened? Why did it happen? What will we do differently? Three organization techniques: chronological, warfighting functions, key events. Output is an After Action Report in observation-discussion-recommendation format.

**Loop-back mechanism:** Discussion → lessons learned → SOP update → retrain → next operation. The AAR is NOT a critique (no blame). It is a professional discussion where participants discover for themselves what happened and why.

**Relevance to hermes-quant: VERY HIGH.** This is the closest match to what we need. The AAR's structure maps directly:
- "What was supposed to happen" = the signal's predicted direction + magnitude + confidence
- "What actually happened" = the realized return + direction correctness + slippage
- "Why did it happen" = the LLM scatter's analysis of patterns
- "What will we do differently" = proposed amendments

**Key insight from AAR doctrine:** The facilitator does NOT lecture. They ask open-ended questions and let participants discover insights. Our LLM scatter should be prompted the same way — ask questions, don't dictate findings.

### 1.8 Google SRE Blameless Postmortems

**What it does:** A written record after every significant incident with: impact summary, timeline, root cause(s), action items, lessons learned. Blameless culture: focus on systemic causes, not individual errors. Action items are tracked to closure. Postmortems are shared widely. Trend analysis across postmortems identifies systemic weaknesses.

**Loop-back mechanism:** Incident → postmortem → action items → bug fixes → monitoring improvements → next incident detection. The postmortem template ensures consistent data capture for cross-incident trend analysis.

**Relevance to hermes-quant: VERY HIGH.** The SRE postmortem is the template for our per-trade postmortem. Key parallels:
- **Consistent template with metadata fields:** Every postmortem captures the same fields, enabling trend analysis.
- **Blameless culture:** The retro loop does not blame analysts — it identifies which components of the system underperformed. This is critical: we don't want the loop to converge on "just mute the classical_ta analyst" — we want it to say "classical_ta underperforms in high-VIX regimes."
- **Action item tracking:** Amendments are action items. They need owners, priorities, and closure tracking.
- **Trend analysis:** The monthly retro is the trend analysis across weekly postmortems.

---

## 2. Deterministic Per-Trade Postmortem: Required Signals

The per-trade postmortem MUST capture enough structured features that the weekly LLM audit can spot patterns without needing raw market data. Here is the minimum signal set:

### 2.1 Trade Identity
| Field | Type | Description |
|-------|------|-------------|
| `trade_id` | str | Unique ID (mirrors signal_id from signal bus) |
| `symbol` | str | e.g., "AAPL", "BTC/USDT" |
| `asset_class` | str | "equity", "crypto", "option" |
| `asof_decision` | ISO8601 | UTC timestamp of signal |
| `asof_settlement` | ISO8601 | UTC timestamp of exit fill |
| `hold_minutes` | float | Actual holding period |

### 2.2 Predicted vs Actual
| Field | Type | Description |
|-------|------|-------------|
| `predicted_direction` | int | -1, 0, 1 |
| `realized_direction` | int | -1, 0, 1 (sign of realized return) |
| `direction_correct` | bool | `predicted_direction == realized_direction` |
| `predicted_magnitude_pct` | float | Expected return in % |
| `realized_return_pct` | float | Actual return in %, net of fees |
| `magnitude_error_pct` | float | `abs(realized - predicted)` |
| `predicted_confidence` | float | Aggregated confidence [0, 1] |
| `time_to_target_minutes` | float | How long until price hit predicted target (or NaN if never) |

### 2.3 Slippage & Execution
| Field | Type | Description |
|-------|------|-------------|
| `entry_slippage_bps` | float | Decision price vs fill price, basis points |
| `exit_slippage_bps` | float | Exit decision vs exit fill, basis points |
| `total_fees_pct` | float | Commissions + spread cost as % of notional |
| `realized_pnl_net` | float | Net P&L in account currency |

### 2.4 Gate Interaction
| Field | Type | Description |
|-------|------|-------------|
| `gate_action` | str | "pass" or "reject" |
| `gate_rejection_reason` | str \| null | e.g., "cost_threshold", "drawdown_halt", "disagreement" |
| `kelly_fraction` | float | ¼-Kelly fraction actually used |
| `position_size_pct` | float | Actual position size as % NAV |
| `gate_override` | bool | Did human override the gate? |

### 2.5 Analyst Contributions (per analyst)
| Field | Type | Description |
|-------|------|-------------|
| `analyst_name` | str | e.g., "classical_ta", "kronos" |
| `analyst_direction` | int | -1, 0, 1 |
| `analyst_confidence` | float | [0, 1] |
| `analyst_weight` | float | Weight in aggregator mix |
| `analyst_direction_correct` | bool | Did this analyst get direction right? |
| `analyst_calibration_delta` | float | Change in ECE for this analyst post-update |

### 2.6 Market Regime at Decision Time
| Field | Type | Description |
|-------|------|-------------|
| `regime_vix_level` | float \| null | VIX at decision time (equities) |
| `regime_volatility_percentile` | float | 20-day HV percentile vs 1-year lookback |
| `regime_trend_strength` | float | ADX or similar at decision time |
| `regime_correlation_to_spy` | float | Rolling beta to SPY |
| `regime_time_of_day_utc` | str | "pre_market", "regular", "after_hours" |
| `regime_day_of_week` | int | 0=Mon..4=Fri |

### 2.7 Outcome Classification (deterministic)
| Field | Type | Description |
|-------|------|-------------|
| `thesis_held` | bool | Direction correct AND return > 0 |
| `outcome_class` | str | "win", "loss", "scratch" (return within ±0.1%) |
| `was_gate_rejection_correct` | bool \| null | If gate rejected: would the trade have been profitable? |

These 20+ fields are ALL computable from existing data: signal bus → execution bus → market data. No LLM. Every field is replayable from disk. Cost: zero.

---

## 3. Weekly LLM Scatter Audit: Prompt Engineering

### 3.1 Scatter Shape: Role-Decomposition (Bull / Bear / Synthesizer)

**NOT** single-prompt-3-models (confirmation bias risk). **NOT** iterative critique chains (cost explosion, diminishing returns).

**RECOMMENDED:** **3-model adversarial scatter with role decomposition.**

| Role | Model Family | Prompt Posture | Output |
|------|-------------|----------------|--------|
| **Bull** | Family A (e.g., anthropic/claude) | "Find systematic patterns in these postmortems. What is the system doing RIGHT? What patterns of success are statistically significant? What amendments would AMPLIFY these strengths?" | `bull_findings` list |
| **Bear** | Family B (e.g., google/gemini) | "For each finding the Bull made, attempt to FALSIFY it. Check sample size, alternative explanations, regime confounding, ADR contradictions. What is the system doing WRONG?" | `bear_falsifications` list |
| **Synthesizer** | Family C (e.g., deepseek/deepseek-v4-pro) | "Read the Bull's findings and the Bear's falsifications. Produce a consolidated report. Only promote findings that survived adversarial scrutiny. Check ALL findings against the immutable rules. Classify severity." | Final weekly report |

**Why this works:**
- The Bull and Bear NEVER see each other's outputs until the Synthesizer stage. This prevents sycophancy.
- Three different model families prevent convergent-but-wrong findings.
- The Synthesizer checks against the immutable rules as a hard constraint.
- The Synthesizer classifies severity — only `MAJOR` or `CRITICAL` findings become amendment proposals.

### 3.2 Anti-Patterns to Defend Against

| Anti-Pattern | Defense |
|-------------|---------|
| **Confirmation bias from recent wins** | Postmortems are shuffled before the audit. The LLM sees ALL postmortems in the period, not just the interesting ones. Explicit prompt instruction: "You are also given trades that had neutral or ambiguous outcomes. Do not over-index on extreme wins or losses." |
| **Sycophancy from same-family scatters** | Enforce cross-family diversity: Anthropic, Google, DeepSeek (or substitutes). Never use two models from the same provider in the same scatter. |
| **Recency bias** | The prompt includes aggregate statistics for the period: win rate, average return, Sharpe-like ratio. This anchors the LLM in the overall distribution before it sees individual trades. |
| **Narrative gloss** | The prompt explicitly asks for NUMBERS: "How many trades support this finding? What is the p-value if we treat direction-correct as a binomial test?" |
| **Over-interpreting noise** | Minimum sample size requirement in the Bear's prompt: "Reject any finding based on fewer than 5 trades. Flag findings based on 5-15 trades as LOW CONFIDENCE regardless of the Bull's enthusiasm." |
| **ADR contradictions** | The immutable rules are injected as a preamble to EVERY prompt. The Synthesizer has an explicit check: "For each finding, verify it does not contradict any of these rules." |

### 3.3 Prompt Template Outline (Bull)

```
You are a trading system auditor in "Bull" mode. Your job is to find
POSITIVE patterns in a week of trading postmortems.

INPUT: {aggregate_stats} + {postmortems_json} (shuffled)

IMMUTABLE RULES (cannot propose changes to these):
- Silence-by-default: when uncertain, hold cash
- Money never goes through tools
- Action space: {0, ±0.05, ±0.10, ±0.15, ±0.20} NAV
- No look-ahead bias
- Calibrators must stay calibrated
- Risk envelope: 0.5% daily-loss halt, 5% drawdown halt, ¼-Kelly

TASK:
1. Identify patterns of CORRECT predictions. Which analysts, regimes,
   time-of-day, symbol classes show above-chance accuracy?
2. For each pattern, state the SAMPLE SIZE and the STATISTIC.
3. For each pattern, propose an amendment that would AMPLIFY this
   strength (e.g., increase weight of analyst X in regime Y).
4. Classify each finding severity: CRITICAL / MAJOR / MINOR / COSMETIC.

OUTPUT: JSON array of findings with {pattern, evidence, sample_size,
statistic, proposed_amendment, severity}.
```

### 3.4 Prompt Template Outline (Bear)

```
You are a trading system auditor in "Bear" mode. Your job is to
FALSIFY the Bull's findings.

INPUT: {aggregate_stats} + {postmortems_json} + {bull_findings_json}

IMMUTABLE RULES: (same as Bull)

TASK:
For EACH Bull finding:
1. Check sample size. Reject if <5 trades. Flag as LOW CONFIDENCE if <15.
2. Propose at least one alternative explanation that fits the same data.
3. Check if the finding is confounded by market regime (e.g., "analyst
   X was right" but all trades were in a trending market).
4. Check if the proposed amendment contradicts any immutable rule.
5. Rate the finding: CONFIRMED / WEAK / FALSIFIED.

OUTPUT: JSON array of falsification attempts.
```

### 3.5 Prompt Template Outline (Synthesizer)

```
You are a trading system auditor in "Synthesizer" mode. Your job is
to produce the final weekly audit report.

INPUT: {aggregate_stats} + {bull_findings_json} + {bear_falsifications_json}

TASK:
1. Consolidate findings. Only include those where Bear rated CONFIRMED
   or where WEAK findings have compelling aggregate evidence.
2. For each CONFIRMED finding with severity >= MAJOR, draft an amendment
   proposal in the format for proposed_amendments.jsonl.
3. Write a narrative summary of the week: what went well, what went
   wrong, what changed from last week.
4. Flag any ADR contradictions you found.
5. Recommend which findings should be promoted to amendment proposals.

OUTPUT: Markdown report + JSON array of amendment proposals.
```

---

## 4. Schema for `proposed_amendments.jsonl`

Each amendment proposal reuses the existing `proposals.py` pattern (JSONL + SQLite dual-write, atomic append, state machine). The schema:

```python
from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime

class AmendmentProposal(BaseModel):
    # ── Identity ──────────────────────────────────────────────────
    amendment_id: str                # "amd_2026-05-31T000000Z_a1b2c3"
    state: Literal["proposed", "approved", "rejected", "superseded"]
    proposed_at: datetime            # UTC, when the synthesizer created this
    proposing_audit_id: str          # "weekly-2026-W22" or "monthly-2026-05"

    # ── Severity & Scope ──────────────────────────────────────────
    severity: Literal["CRITICAL", "MAJOR", "MINOR", "COSMETIC"]
    scope_type: Literal["adr_amendment", "parameter_change", "analyst_weight",
                        "gate_threshold", "recipe_change", "new_analyst",
                        "remove_analyst", "code_change", "other"]
    scope_target: str                # "ADR-0004 §Sizing" or "hermes_quant/risk/gate.py:42"
    scope_adr_refs: list[str]        # ["ADR-0004", "ADR-0009"]

    # ── Evidence ──────────────────────────────────────────────────
    evidence_summary: str            # 1-3 sentence human-readable summary
    evidence_postmortem_ids: list[str]  # specific trade_ids backing this finding
    evidence_statistic: str          # "7/9 trades in high-VIX regime had incorrect direction"
    evidence_sample_size: int
    evidence_confidence: Literal["HIGH", "MEDIUM", "LOW"]

    # ── Proposed Change ───────────────────────────────────────────
    proposed_change: str             # Human-readable description of what to change
    proposed_diff: Optional[str]     # If code change: unified diff (stored, not applied)
    proposed_parameter_before: Optional[float | str]
    proposed_parameter_after: Optional[float | str]

    # ── Predicted Impact ──────────────────────────────────────────
    predicted_impact_summary: str    # What we expect to happen if this is applied
    predicted_metric: str            # "win_rate", "sharpe", "max_drawdown"
    predicted_direction: Literal["increase", "decrease", "stabilize"]
    predicted_magnitude_estimate: str  # "~2-5% improvement in win rate"

    # ── Premortem ─────────────────────────────────────────────────
    premortem_what_could_go_wrong: str  # Failure modes of THIS amendment
    premortem_worst_case: str           # Worst reasonable outcome
    premortem_detection: str            # "We'll know this failed if: [observable metric]"
    premortem_reversibility: str        # "To reverse: [specific steps]"

    # ── Review ────────────────────────────────────────────────────
    bull_model: str                  # e.g., "anthropic/claude-opus-4.7"
    bear_model: str
    synthesizer_model: str
    bear_falsification_result: Literal["CONFIRMED", "WEAK", "FALSIFIED"]

    # ── Human Approval (filled on approve/reject) ─────────────────
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_commit_hash: Optional[str] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    # ── Immutable Rules Check ─────────────────────────────────────
    passes_immutable_rules: bool     # Synthesizer verified no contradiction
```

---

## 5. CLI Shape for HITL Approval

### 5.1 Recommended Design

```
hermes quant retro list [--severity MAJOR] [--state proposed]
    → Lists all proposed amendments with ID, severity, summary

hermes quant retro show <amendment_id>
    → Full details: evidence, proposed change, premortem, bear falsification

hermes quant retro approve <amendment_id> [--yes]
    → APPROVAL WORKFLOW:
      1. Validate amendment is in "proposed" state
      2. Re-check immutable rules (defense-in-depth: verify no contradiction)
      3. Show the proposed change, predicted impact, and premortem
      4. Prompt user for confirmation (bypassed by --yes for scripting)
      5. On confirmation:
         a. If scope_type == "parameter_change":
            → Patch the user's recipe YAML (~/.hermes/quant/recipes/<profile>.yaml)
            → The recipe is the runtime config; no code mutation needed
         b. If scope_type == "adr_amendment":
            → Generate a draft ADR amendment as a markdown file
            → Open it in $EDITOR for the user to finalize
            → On save: git add + git commit with message referencing amendment_id
         c. If scope_type in ("analyst_weight", "gate_threshold"):
            → Patch the recipe YAML (same as parameter_change)
         d. If scope_type == "code_change":
            → Apply the stored diff via git apply
            → git add + git commit
      6. Mark amendment as "approved" in proposed_amendments.jsonl
      7. Record approval metadata (timestamp, user, commit hash)

hermes quant retro reject <amendment_id> --reason "..."
    → Mark as rejected with reason

hermes quant retro week [--dry-run] [--since YYYY-MM-DD]
    → Manually trigger weekly audit (normally cron-driven)

hermes quant retro month [--dry-run]
    → Manually trigger monthly meta-retro
```

### 5.2 Safest Path: Recipe YAML Patching

For parameter changes (the most common amendment type), the safest mechanism is:

1. The user's recipe at `~/.hermes/quant/recipes/<profile>.yaml` is the runtime config.
2. `hermes quant retro approve <id>` writes a new section to the recipe:

```yaml
# In the recipe YAML:
analyst_weights:
  classical_ta: 0.25
  microstructure: 0.25
  kronos: 0.25
  hermes_semantic: 0.25
  # Approved amendment amd_2026-05-31T000000Z_a1b2c3 (2026-05-31):
  # "Increase kronos weight to 0.35 in high-VIX regime based on 7/9 correct"
  kronos_high_vix: 0.35  # overrides kronos when VIX > 25
```

3. The recipe is under git version control. The CLI auto-commits the change.
4. The daemon reads the recipe at startup. No runtime mutation.

**Why not a runtime config mutation?**
- Runtime mutation creates non-reproducible state (ADR-0001 violation).
- The recipe IS the config. Changing it is a git-tracked artifact.
- Rollback is `git revert`.

**Why not always a PR?**
- For a solo operator, PR overhead is excessive.
- For parameter tweaks, the git commit IS the review trail.
- For ADR amendments or code changes, the CLI opens `$EDITOR` — the human reviews before saving.

---

## 6. Failure Mode Defenses

### 6.1 Overfit Rules from Small Sample (Recency Bias)

**Defense:** The Bear's falsification prompt enforces minimum sample size (reject <5, flag <15). The monthly retro explicitly tests whether weekly findings persist across weeks. The prompt asks: "Of the amendments proposed in Week N, how many remain supported by data in Week N+1?" This is a built-in holdout test.

### 6.2 LLM Scatter Collusion (Convergent-but-Wrong)

**Defense:** Cross-family diversity is non-negotiable. The scatter uses ≥3 different model families. Models are selected from the `model-roster` skill at audit time — if a family is unavailable, the audit fails rather than substituting a same-family model. The Bull and Bear NEVER see each other's outputs. The Synthesizer sees both and must explain disagreements.

### 6.3 Amendment Retroactively Justifies a Bad Trade

**Defense:** Amendments are proposals, not retroactive fixes. The per-trade postmortem captures what ACTUALLY happened. An amendment cannot rewrite history. The `evidence_postmortem_ids` field creates an immutable link to specific trades — if those trades were later found to be anomalous, the amendment's evidence is weakened.

### 6.4 Retro Loop Becomes the Most Expensive Component

**Defense:**
- Layer 0 (per-trade postmortem) costs $0 — no LLM.
- Layer 1 (weekly scatter): 3 LLM calls × ~50K input tokens each (postmortems + prompts) + ~5K output tokens each. At DeepSeek prices (~$0.27/M input, ~$1.10/M output): ~$0.06/week. At Claude prices (~$3/M input, ~$15/M output): ~$0.68/week.
- Layer 2 (monthly meta-retro): same shape but ~150K input tokens (weekly reports): ~$0.18/month (DeepSeek) to ~$2.25/month (Claude).
- **Worst case: ~$5/month.** Well under the $30/month budget.

### 6.5 Amendment Contradicts ADRs

**Defense:** The immutable rules are injected into EVERY prompt as a preamble. The Synthesizer has an explicit contradiction check. Before approval, `hermes quant retro approve` re-runs the immutable rules check. If an amendment proposes changing an ADR, the scope is explicitly `adr_amendment` and the human must manually edit the ADR markdown — no auto-generated ADR changes.

---

## 7. Cost Model

### Assumptions
- ~20 trades/week (paper trading, daily picks)
- ~1KB per postmortem (JSON)
- 3-model scatter: bull + bear + synthesizer

### Weekly Audit
| Component | Input Tokens | Output Tokens | Cost (DeepSeek) | Cost (Claude) |
|-----------|-------------|---------------|-----------------|---------------|
| Bull prompt | ~30K | ~3K | $0.011 | $0.135 |
| Bear prompt | ~35K | ~4K | $0.014 | $0.165 |
| Synthesizer prompt | ~40K | ~5K | $0.017 | $0.195 |
| **Total/week** | ~105K | ~12K | **$0.04** | **$0.50** |

### Monthly Meta-Retro
| Component | Input Tokens | Output Tokens | Cost (DeepSeek) | Cost (Claude) |
|-----------|-------------|---------------|-----------------|---------------|
| Bull prompt | ~60K | ~5K | $0.022 | $0.255 |
| Bear prompt | ~70K | ~6K | $0.026 | $0.300 |
| Synthesizer prompt | ~80K | ~8K | $0.031 | $0.360 |
| **Total/month (meta)** | ~210K | ~19K | **$0.08** | **$0.92** |

### Grand Total
| Cost Tier | Weekly × 4 | Monthly Meta | **Total/Month** |
|-----------|-----------|--------------|-----------------|
| DeepSeek family | $0.16 | $0.08 | **$0.24** |
| Claude family | $2.00 | $0.92 | **$2.92** |
| Mixed families | ~$1.00 | ~$0.40 | **~$1.40** |

**Conclusion:** Even at worst-case Claude pricing for all roles, the total is ~$3/month. With the recommended mixed-family approach (cheaper models for Bull/Bear, Claude for Synthesizer), it's ~$1.40/month. Well under the $30/month cap.

---

## 8. Concrete Architecture Recommendation

### The Two-Layer AAR Loop

```
┌─────────────────────────────────────────────────────────────┐
│ LAYER 0: Deterministic Per-Trade Postmortem (every trade)   │
│                                                              │
│ settlement_loop                                              │
│   │                                                          │
│   ├─ execution filled → join to signal record                │
│   ├─ compute 20+ structured fields (Section 2)                │
│   ├─ append to postmortems.jsonl                             │
│   └─ cost: $0, latency: <10ms, replayable                   │
│                                                              │
│ Output: postmortems.jsonl (append-only, ~1KB per record)    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼ (Sunday 00:00 UTC cron)
┌─────────────────────────────────────────────────────────────┐
│ LAYER 1: Weekly LLM Scatter Audit                           │
│                                                              │
│ 1. Load last 7 days of postmortems.jsonl                     │
│ 2. Shuffle + compute aggregate stats                         │
│ 3. BULL (family A): find positive patterns + propose fixes   │
│ 4. BEAR (family B): falsify Bull's findings                  │
│ 5. SYNTHESIZER (family C): consolidate, check immutable      │
│    rules, classify severity, draft amendment proposals       │
│ 6. Write weekly report → ~/.hermes/quant/retro/weekly/       │
│ 7. Promote MAJOR+ findings → proposed_amendments.jsonl       │
│                                                              │
│ Cost: ~$0.05 (DeepSeek) to ~$0.50 (Claude)                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼ (1st of month 00:00 UTC cron)
┌─────────────────────────────────────────────────────────────┐
│ LAYER 2: Monthly Meta-Retro                                  │
│                                                              │
│ 1. Load last 4 weekly reports                                │
│ 2. Same scatter shape (bull/bear/synthesizer)                │
│ 3. Questions:                                                │
│    - Do weekly findings persist or fade?                     │
│    - Are amendments from week N validated by week N+1?       │
│    - Any systemic pattern invisible at weekly granularity?   │
│    - Any proposed amendment contradicting ADRs?              │
│ 4. Draft meta-amendments (e.g., "retro loop itself is        │
│    overweighting recent wins — adjust severity threshold")   │
│ 5. Write monthly report → ~/.hermes/quant/retro/monthly/     │
│                                                              │
│ Cost: ~$0.08 (DeepSeek) to ~$0.92 (Claude)                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ HITL Approval Gate                                           │
│                                                              │
│ hermes quant retro list   → see proposed amendments          │
│ hermes quant retro show   → read details + premortem         │
│ hermes quant retro approve → apply to recipe, git commit     │
│ hermes quant retro reject  → record rejection reason         │
│                                                              │
│ NO automatic application. Human reviews every amendment.     │
└─────────────────────────────────────────────────────────────┘
```

### What the Loop CANNOT Mutate (Hardcoded in Every Prompt)

1. Silence-by-default posture
2. Money-never-through-tools rule
3. Discrete action space `{0, ±0.05, ±0.10, ±0.15, ±0.20}`
4. No-look-ahead CI gate
5. Calibrator update mechanics
6. Risk envelope: 0.5% daily-loss halt, 5% drawdown halt, ¼-Kelly
7. Postmortem schema (can be extended, not reduced)

### What the Loop CAN Propose (Amendable via HITL)

1. Analyst weights per regime (e.g., "kronos = 0.35 when VIX > 25")
2. Gate threshold parameters (e.g., cost threshold from 2× to 2.5× spread)
3. New analyst proposals (e.g., "add VIX-based regime classifier")
4. Recipe changes (e.g., "remove symbol X from daily universe")
5. ADR amendments (e.g., "ADR-0004 §Sizing: add 0.025 increment")
6. New ADR proposals (e.g., "ADR-0031: regime-adaptive risk gating")

### Implementation Order (matching plan doc Wave D)

1. **D0:** Add `PostmortemRecord` dataclass + `postmortems.jsonl` writer to `settlement_loop.py` (Layer 0)
2. **D1:** `hermes quant retro week` CLI + weekly audit prompt templates (Layer 1)
3. **D2:** `hermes quant retro month` CLI + monthly meta-retro prompts (Layer 2)
4. **D3:** Amendment proposal schema + `proposed_amendments.jsonl` store (reuses `proposals.py` pattern)
5. **D4:** `hermes quant retro list/show/approve/reject` CLI for HITL workflow
6. **D5:** Sunday EOD cron for weekly, 1st-of-month cron for monthly

### First Retro: Sunday 2026-05-31

The first weekly retro fires with whatever paper trades have accumulated. Expected: ~0-5 trades in Week 1. The audit may produce zero findings (sample too small). This is OK — the pipeline runs end-to-end and the Bear correctly rejects underpowered findings. By Week 4 (~20-40 trades), the first MAJOR findings emerge.

---

## References

- Shinn, N. et al. (2023). "Reflexion: Language Agents with Verbal Reinforcement Learning." NeurIPS 2023.
- Wang, G. et al. (2023). "Voyager: An Open-Ended Embodied Agent with Large Language Models." NeurIPS 2023.
- Hong, S. et al. (2024). "MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework." ICLR 2024.
- Li, G. et al. (2023). "CAMEL: Communicative Agents for 'Mind' Exploration of Large Language Model Society." NeurIPS 2023.
- Bai, Y. et al. (2022). "Constitutional AI: Harmlessness from AI Feedback." arXiv:2212.08073.
- Bai, Y. et al. (2022). "Training a Helpful and Harmless Assistant with Reinforcement Learning from Human Feedback." arXiv:2204.05862.
- Silver, D. et al. (2017). "Mastering the game of Go without human knowledge." Nature.
- Silver, D. et al. (2018). "A general reinforcement learning algorithm that masters chess, shogi, and Go through self-play." Science.
- US Army FM 25-101 / TC 7-0: After Action Reviews.
- Google SRE Book, Chapter 15: "Postmortem Culture: Learning from Failure."
- Google SRE Workbook, Appendix C: "Results of Postmortem Analysis."
