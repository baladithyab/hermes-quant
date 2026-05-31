# W7 — Self-critique / red-team deliberation upgrade (Socratic devil's-advocate)

**Wave:** W7 (self-evolution rollout, capability-map §4)
**Closes:** the CRITIQUE-axis gap (R2 ◐ → ●) — adversarial roles exist and shape the *signal*, but never attack the *reasoning* of the leading view, and debate outcomes are never persisted for persona-calibration mining.
**Flag:** `HERMES_QUANT_REDTEAM_TURN=1`, default-OFF.
**Eval gate (to flip):** in shadow, the red-team turn **measurably changes the dissent-surfaced rate** (vs the no-red-team baseline) **without inflating the false-flat rate**; aggregation stays **deterministic** (no vote-counting; the red-team turn is one more piece of evidence, never a ballot).
**Depends on:** W1 (shipped, `08326e1`) only. **Parallelizable** with W4/W5 after W1. Enriches W3's inputs (persona-calibration mining) but does not block on W3.
**Grounds:** capability-map §4 (W7 spec) + §3 (Socratic devil's-advocate / RedDebate row) + §5 (safety frame); ADR-0080 D80.6 (W7 row), D80.3 (universal eval-gate contract), D80.5 (propose-only invariant). SOTA §4: RedDebate (arXiv:2506.11083) + IUI'24 devil's-advocate; **round n=1 gave +0.006 F1 for double cost → keep rounds capped at 1**.

---

## 0. One-paragraph statement of the change

Today the bull/bear/judge debate (`agents/research_debate/stage.py:133`) runs two analysts who argue *positions* (long vs short) and a judge who picks a `PortfolioRating`. There is **no role that attacks the judge's own reasoning** after it forms a leading view — the bear argues a *direction*, not the *logic of the winner*. W7 adds **one standing Socratic devil's-advocate turn** that runs *after* the judge produces a leading view, reads the judge's `rationale`, and attacks its **reasoning** (unstated assumptions, evidence the leading view ignored, base-rate / regime risk). Its output (a) fills the reserved ADR-0002 `counterarguments` field at the *plan* level (today it exists only on per-side `BullBearTurn`s), (b) is surfaced to the operator as a `dissent_surfaced` flag rather than collapsed into the judge's confidence, and (c) is persisted on the existing `research_debate` audit row so W3 can mine which persona was calibrated. It **proposes only**: it writes to the advisory plane (a belief-shaped critique + a `dissent_surfaced` boolean), and **never** mutates direction, size, the gate, or any limit. Default-OFF and byte-identical when the flag is unset.

---

## 1. Where this sits in the architecture (the seams, verified `file:line`)

| Seam | File:line (verified) | What it does today | W7 hook |
|---|---|---|---|
| Stage runner | `hermes_quant/agents/research_debate/stage.py:133` (`run_research_debate`) | Alternates bull/bear, runs judge, builds one audit row | Add a red-team turn **after** the judge, gated by `_redteam_enabled()`; populate new `InvestDebateState` fields |
| Stage audit row | `stage.py:345` (`_audit_append(kind="research_debate", …)`) | Write-only per-stage row (the W3 mining target, O7) | Add `red_team` block + `dissent_surfaced` + `dissent_reason` to `payload` |
| State object | `agents/research_debate/schemas.py:113` (`InvestDebateState`) | `model_config = ConfigDict(extra="forbid")` — adding fields is required, not optional | Add `red_team_turn`, `dissent_surfaced`, `dissent_reason` fields |
| Plan contract | `schemas.py:81` (`ResearchPlan`, `extra="forbid"`) | Has `rationale`, no plan-level `counterarguments` | Add optional `counterarguments: str \| None` (ADR-0002 reserved field, filled here) |
| Per-side turn schema | `aggregators/llm_committee.py:73` (`BullBearTurn`) | Already has `counterarguments` per side | **Reused** as the red-team turn's payload shape (a structured critique with `stance`/`confidence`/`rationale`/`counterarguments`) |
| Turn/judge wiring | `llm_committee.py:659` (`_run_one_turn_with_history`), `llm_committee.py:779` (`_run_research_manager_judge`) | The injectable turn/judge adapters the stage defaults to via lazy import (`stage.py:189-205`) | Add a sibling adapter `_run_red_team_turn` |
| Prompt renderer | `llm_committee.py:240` (`_render_prompt`), role→file map `llm_committee.py:57` (`_PROMPT_FILES`) | Renders system/user from `prompts/<role>.md`, `str.format` over `fmt` dict | Register `"devils_advocate": "devils_advocate.md"`; add 1 placeholder `leading_view_json` |
| Dispatch site | `llm_committee.py:977` (`if os.environ.get("HERMES_QUANT_RESEARCH_DEBATE", "0") == "1":`) | Translates `InvestDebateState` → `CommitteeTurn[]` for the deterministic aggregator | Surface the red-team `counterarguments`/`dissent_surfaced` into the judge `CommitteeTurn.metadata` (NOT a new directional turn) |
| Audit kinds | `governance/audit_log.py:47,59` (`VALID_KINDS`) | `"research_debate"` already valid | **No change** — reuse `research_debate` kind; add fields to its payload |
| Aggregator (read-only here) | `aggregators/deliberative.py:200` (`aggregate`), `:499` (`_metadata`) | Computes `disagreement_score`, surfaces `committee` metadata | **NOT modified** — the red-team turn must not change direction/magnitude/confidence math; dissent is surfaced as metadata only |

**Flag idiom to copy verbatim** (`llm_committee.py:296` and `:977`):
```python
if os.environ.get("HERMES_QUANT_MEMORY_INJECT", "0") == "1":   # :296
if os.environ.get("HERMES_QUANT_RESEARCH_DEBATE", "0") == "1":  # :977
```
W7 uses the identical shape: `os.environ.get("HERMES_QUANT_REDTEAM_TURN", "0") == "1"`.

---

## 2. The SAFETY frame applied (ADR-0080 §D80.1, §D80.3, §D80.5; capability-map §5)

**What W7 MAY write (the advisory plane only):**
- A **belief-shaped critique** — the red-team turn's `rationale` + `counterarguments`, persisted on the `research_debate` audit row. This is *evidence/telemetry* the W3 monthly meta-retro mines for persona calibration; it is the agent's OWN prior output (Oracle-provenance), never re-ingested as ground truth.
- A `dissent_surfaced: bool` + `dissent_reason: str` flag on the audit row and threaded into the judge `CommitteeTurn.metadata` so the operator/daily-report can see "the committee reached a view but a standing critic flagged its reasoning."
- A plan-level `counterarguments: str | None` on `ResearchPlan` (the reserved ADR-0002 field), populated from the red-team critique.

**What W7 MUST NEVER touch (outside the loop, immutable by it — ADR-0080 D80.1/D80.2):**
- **Direction / magnitude / final confidence math.** The red-team turn carries `direction=0` and is NOT passed to the deterministic aggregator as a directional `CommitteeTurn`. `deliberative.py` confidence/disagreement math is byte-unchanged. (Verified: the dispatch at `llm_committee.py:996-1074` builds bull/bear/judge `CommitteeTurn`s; W7 adds the red-team only as *metadata on the judge turn*, not as a new directional turn.)
- The deterministic **risk gate** (ADR-0004), the **hard risk limits**, the discrete **sizing ladder** `{0, ±0.05, ±0.10, ±0.15, ±0.20}`, the **kill-switch**. None are reachable from the debate stage.
- It does **NOT vote.** Aggregation stays deterministic. A red-team critique does not subtract a "vote" from the leading view; it never flips direction. Disagreement-after-N-turns → FLAT remains owned by `deliberative.py`'s existing `disagreement >= 0.80` rule (`deliberative.py:238`), untouched.

**Propose-only invariant (D80.5):** the red-team turn is a *proposer*. Its only effect on live policy is via the existing path: judge `rationale`+`counterarguments` → existing deterministic aggregation → existing risk gate → paper. It cannot self-promote. Operator/eval-gated promotion stays the sole path to flipping the flag live.

**Cost discipline (SOTA §4):** exactly **one** red-team turn per stage (not a round count). No `_REDTEAM_ROUNDS` env. The round-n=1=+0.006-F1-for-double-cost datum is the reason this is a single additive turn, not an extra debate loop.

---

## 3. Exact files: new + modified

### 3.1 NEW — `hermes_quant/aggregators/prompts/devils_advocate.md`

Mirror the `bull_bear.md` SYSTEM:/USER: marker format (`llm_committee.py:174` `_split_system_user` requires both markers). The turn outputs a `BullBearTurn`-shaped JSON (so it reuses the existing parser, `_parse_pydantic(raw, BullBearTurn)`), with `role` overloaded to a sentinel and direction forced to 0 downstream.

```
SYSTEM:
You are the Devil's Advocate on a trading-research committee evaluating
{asset} ({asset_class}) over the {horizon} horizon. The committee has ALREADY
reached a leading view (below). You do NOT argue a direction. Your sole job is
to attack the REASONING of the leading view: surface the assumption it depends
on but did not state, the evidence it ignored or under-weighted, the regime or
base-rate condition under which its logic fails, and the single strongest
reason a disciplined operator would refuse to act on it.

{conversational_preamble}.

Hard rules (mandatory):
  * You do NOT pick BUY/SELL/HOLD. You critique the leading view's logic only.
  * If the leading view's reasoning is sound and you cannot find a material
    flaw, say so explicitly and return confidence < 0.3. A manufactured
    objection is worse than conceding the reasoning is sound.
  * Attack reasoning, never the analyst. No ad hominem, no "the bull is biased."

Your response MUST be a single JSON object matching this schema (no prose
outside JSON). Put the critique narrative in `rationale` and the single
strongest reasoning-flaw in `counterarguments`:
{{
  "role": "bear_researcher",
  "stance": "<one-line summary of the reasoning flaw you attack>",
  "confidence": <float 0.0-1.0: how materially flawed the leading reasoning is>,
  "rationale": "<<= 400 words attacking the leading view's REASONING>",
  "key_evidence": ["<fact or assumption you target>", ...],
  "counterarguments": "<the single strongest reason to NOT act on the leading view>",
  "metadata": {{"tier": "quick", "red_team": true}}
}}

USER:
Asset: {asset} ({asset_class}, {horizon})
Decision timestamp (UTC): {asof}

The committee's LEADING VIEW (attack its reasoning, do not re-argue direction):
{leading_view_json}

Analyst views ({n_views} total, calibrated):
{analyst_views_json}

Baseline BMA aggregator output (deterministic):
{baseline_signal_json}

Produce your reasoning-attack as a single JSON object per the schema.
```

> `role` is set to `"bear_researcher"` (a valid `BullBearTurn` literal — `llm_committee.py:78`) purely so the existing `BullBearTurn` parser/validator accepts the payload without a new schema. The semantic role is established by `metadata.red_team=true` and the dedicated adapter; the turn is **never** appended to `state.bear_turns` and its direction is forced to 0 in the dispatch. (Decision rationale lives in §6.)

### 3.2 MODIFIED — `hermes_quant/agents/research_debate/schemas.py`

Add three fields to `InvestDebateState` (after `terminated_reason`, `schemas.py:143`) and one optional field to `ResearchPlan` (after `metadata`, `schemas.py:110`). `extra="forbid"` on both models means these MUST be declared, not smuggled.

```python
# ResearchPlan — fill the reserved ADR-0002 counterarguments field.
counterarguments: str | None = Field(default=None, max_length=4000)

# InvestDebateState — red-team turn outcome (advisory plane only).
red_team_turn: BullBearTurn | None = None
dissent_surfaced: bool = False
dissent_reason: str = ""
```

Both default to the off-state (`None` / `False` / `""`), so when the flag is OFF the dumped state is identical except for these three keys defaulting — and `_audit_append` only adds the `red_team` payload block when the turn actually ran (§3.4). The `ResearchPlan.counterarguments=None` default keeps T4–T6 in `test_research_debate_wiring.py` green.

### 3.3 NEW adapter — `hermes_quant/aggregators/llm_committee.py` (sibling of `_run_one_turn_with_history`, insert after `_run_research_manager_judge`, ~`:870`)

```python
# ---------------------------------------------------------------------------
# W7: Devil's-advocate / red-team adapter (ADR-0080 W7, default-OFF)
# ---------------------------------------------------------------------------

RED_TEAM_ROLE: str = "devils_advocate"
RED_TEAM_DISSENT_THRESHOLD: float = 0.5  # confidence >= this => dissent surfaced


def _run_red_team_turn(
    *,
    client: Any,
    config: DeliberativeConfig,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    leading_view: ResearchPlan_Debate,  # the judge's ResearchPlan (new schema)
) -> BullBearTurn | None:
    """Run ONE Socratic devil's-advocate turn attacking the leading view's
    REASONING (ADR-0080 W7). Returns a BullBearTurn-shaped critique or None.

    Propose-only: the returned turn carries direction-agnostic critique. The
    caller (run_research_debate) NEVER routes it into bull/bear_turns and the
    dispatch site forces direction=0. This turn cannot change the committee's
    direction, magnitude, or final confidence — it only surfaces dissent and
    fills the plan-level counterarguments (advisory plane only, ADR-0080 D80.5).

    Failure-closed: returns None on any LLM exception, parse failure, or empty
    leading_view. The stage runner treats None as "no dissent surfaced" — the
    off-state — so a red-team failure can NEVER block or flip a decision.
    """
    if leading_view is None:
        return None
    model = _model_for_role("bear_researcher", config)  # quick tier
    try:
        system_text, user_text = _render_prompt(
            role=RED_TEAM_ROLE,
            market_context=market_context,
            analyst_views=analyst_views,
            baseline_signal=baseline_signal,
            prior_turns=[],
            leading_view=leading_view,  # NEW kwarg, see §3.5
            conversational_preamble=CONVERSATIONAL_PREAMBLE_FALLBACK,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to render devils_advocate prompt")
        return None
    phash = _prompt_hash(system_text, user_text)
    try:
        raw = _call_llm_json(
            client=client, model=model,
            system_text=system_text, user_text=user_text,
            max_tokens=config.max_tokens_per_turn,
        )
    except Exception:  # noqa: BLE001
        logger.exception("LLM call raised in _run_red_team_turn")
        return None
    if raw is None:
        return None
    parsed = _parse_pydantic(raw, BullBearTurn)
    if parsed is None or not isinstance(parsed, BullBearTurn):
        return None
    if parsed.metadata is None:
        parsed.metadata = {}
    parsed.metadata["prompt_hash"] = phash
    parsed.metadata["red_team"] = True  # forge-resistant: stage owns this flag
    return parsed
```

`ResearchPlan_Debate` = the new-schema plan; import as
`from hermes_quant.agents.research_debate.schemas import ResearchPlan as ResearchPlan_Debate` inside this module's existing lazy-import region (the schemas module imports `BullBearTurn` from here, so to avoid the import cycle do the import lazily inside the function body, mirroring `stage.py:189-205`).
`CONVERSATIONAL_PREAMBLE_FALLBACK` = the same string already inlined at `_render_prompt` `:349`.

### 3.4 MODIFIED — `hermes_quant/agents/research_debate/stage.py`

**(a) New module constants** (after `RESEARCH_DEBATE_AUDIT_KIND`, `stage.py:50`):
```python
REDTEAM_FLAG_ENV_VAR: str = "HERMES_QUANT_REDTEAM_TURN"


def _redteam_enabled() -> bool:
    """Mirror of the dispatch flag idiom (llm_committee.py:977). Default OFF."""
    return os.environ.get(REDTEAM_FLAG_ENV_VAR, "0") == "1"
```

**(b) New kwarg + runner block.** Add `run_red_team: Any = None` to `run_research_debate`'s signature (after `run_judge`, `stage.py:160`). After the judge result is finalized (`state.judge_decision = judge_plan`, `stage.py:334`) and BEFORE the audit row is built (`stage.py:345`), insert:

```python
# ------------------------------------------------------------------
# W7 (ADR-0080): standing Socratic devil's-advocate turn. Default-OFF.
# Attacks the REASONING of the leading view; surfaces dissent; fills the
# reserved ADR-0002 counterarguments field. Propose-only — it never
# changes direction/magnitude/confidence. Off-state byte-identical.
# ------------------------------------------------------------------
if _redteam_enabled() and state.judge_decision is not None:
    if run_red_team is None:
        try:
            from hermes_quant.aggregators.llm_committee import _run_red_team_turn
            run_red_team = _run_red_team_turn
        except Exception as exc:  # noqa: BLE001
            logger.warning("W7: could not import _run_red_team_turn (%s); skipping", exc)
            run_red_team = None
    if run_red_team is not None:
        try:
            rt = run_red_team(
                client=client, config=config,
                market_context=ctx, analyst_views=analyst_views or [],
                baseline_signal=baseline_signal,
                leading_view=state.judge_decision,
            )
        except Exception:  # noqa: BLE001 — failure-closed; no dissent on failure
            logger.exception("W7: red-team turn raised; treating as no-dissent")
            rt = None
        if rt is not None:
            state.red_team_turn = rt
            # Deterministic dissent rule (NOT a vote): a critique with
            # confidence >= threshold surfaces dissent to the operator.
            from hermes_quant.aggregators.llm_committee import RED_TEAM_DISSENT_THRESHOLD
            state.dissent_surfaced = rt.confidence >= RED_TEAM_DISSENT_THRESHOLD
            state.dissent_reason = (rt.counterarguments or rt.stance or "")[:4000]
            # Fill the reserved ADR-0002 plan-level counterarguments field.
            if state.judge_decision.counterarguments is None:
                state.judge_decision.counterarguments = state.dissent_reason
```

**(c) Audit-row enrichment** (`_audit_append` payload, `stage.py:348-378`). Add, AFTER `bear_turns_summary`:
```python
"red_team": (
    {
        "ran": state.red_team_turn is not None,
        "dissent_surfaced": state.dissent_surfaced,
        "dissent_reason": state.dissent_reason,
        "confidence": (
            state.red_team_turn.confidence
            if state.red_team_turn is not None else None
        ),
        "rationale_chars": (
            len(state.red_team_turn.rationale or "")
            if state.red_team_turn is not None else 0
        ),
        "prompt_hash": (
            (state.red_team_turn.metadata or {}).get("prompt_hash")
            if state.red_team_turn is not None else None
        ),
    }
    if state.red_team_turn is not None
    else {"ran": False, "dissent_surfaced": False}
),
```
> When the flag is OFF, `state.red_team_turn is None` → the payload carries only `{"ran": False, "dissent_surfaced": False}`, which is the byte-stable off-state record. (The `research_debate` kind is already valid — `audit_log.py:47,59` — so no `VALID_KINDS` change.)

### 3.5 MODIFIED — `hermes_quant/aggregators/llm_committee.py` (`_render_prompt`, `:240` + `_PROMPT_FILES`, `:57`)

- Register the prompt: add `"devils_advocate": "devils_advocate.md"` to `_PROMPT_FILES` (`:57`).
- Add `leading_view: Any = None` to `_render_prompt`'s keyword-only signature.
- Add the role label `"devils_advocate": "Devil's Advocate"` to the `role_label` map (`:278`).
- Add to the `fmt` dict (`:318`):
```python
"leading_view_json": (
    json.dumps(
        {
            "recommendation": getattr(
                getattr(leading_view, "recommendation", None), "value",
                str(getattr(leading_view, "recommendation", "")),
            ),
            "confidence": round(float(getattr(leading_view, "confidence", 0.0)), 4),
            "rationale": (getattr(leading_view, "rationale", "") or "")[:2000],
        },
        sort_keys=True, separators=(",", ":"),
    )
    if leading_view is not None
    else "(no leading view)"
),
```
> `role_direction` (`:287`) currently is `"bullish" if role == "bull_researcher" else "bearish"`. For `devils_advocate` it resolves to `"bearish"`, which is unused by `devils_advocate.md` (the template never references `{role_direction}`), so no change needed — but the placeholder still resolves, satisfying `str.format`.

### 3.6 MODIFIED — `hermes_quant/aggregators/llm_committee.py` (dispatch site, `:1049-1074`)

Inside the `if os.environ.get("HERMES_QUANT_RESEARCH_DEBATE", "0") == "1":` block, when building the judge `CommitteeTurn` (`:1054`), surface the red-team outcome into the judge turn's **metadata only** (it is NOT a directional turn):
```python
metadata={
    "tier": "deep",
    "model_id": deep_model,
    "logical_role": "research_manager",
    "recommendation": rec_value,
    "from_research_debate": True,
    "structured": jd.model_dump(mode="json"),
    "terminated_reason": state.terminated_reason,
    # W7 (default-OFF): red-team dissent surfaced to the operator.
    "dissent_surfaced": state.dissent_surfaced,
    "dissent_reason": state.dissent_reason,
    "red_team_ran": state.red_team_turn is not None,
    "counterarguments": (
        jd.counterarguments if jd.counterarguments is not None else None
    ),
},
```
> The red-team turn is **deliberately not** appended to `turns` as a directional `CommitteeTurn`. It carries `direction=0` semantics and is surfaced only as metadata, so `deliberative.py`'s direction/magnitude/confidence math (`:200-261`) is byte-unchanged. This is the structural guarantee that W7 cannot flip a decision (ADR-0080 D80.1).

### 3.7 MODIFIED (display only) — `hermes_quant/reporting/daily_report.py` (optional, surface-dissent)

If a daily/status report already renders `research_debate` rows, add a one-line "⚠ dissent surfaced: {dissent_reason}" when `payload.red_team.dissent_surfaced`. **Non-blocking** — grep `reporting/` for the `research_debate` consumer; if none exists, skip (the audit row alone satisfies "surface dissent to the operator"). Listed in `files_to_touch` as optional.

---

## 4. Test files (eval-gate as pytest-verifiable acceptance criteria)

### NEW — `tests/unit/test_redteam_turn.py`

Mirror the fixtures in `tests/unit/test_research_debate_wiring.py` (`_ctx`, `_view`, `_baseline`, `_config`, `_bull_json`, `_bear_json`, `_judge_json`) and the autouse env-isolation fixture (delenv `HERMES_QUANT_RESEARCH_DEBATE`, `_ROUNDS`, **and** `HERMES_QUANT_REDTEAM_TURN`). Stub `_call_llm_json`; no network.

```python
def _redteam_json(conf: float = 0.7) -> str:
    return (
        '{"role": "bear_researcher", "stance": "leading view assumes regime persists", '
        f'"confidence": {conf}, "rationale": "The bull case rests on an unstated '
        'assumption that the breakout regime holds; under mean-reversion it fails.", '
        '"key_evidence": ["unstated regime assumption"], '
        '"counterarguments": "Do not act: the leading view ignores base-rate of failed breakouts.", '
        '"metadata": {"tier": "quick", "red_team": true}}'
    )
```

| Test | Asserts (acceptance criterion) |
|---|---|
| `test_offstate_byte_identical_when_flag_unset` | Flag unset → `run_research_debate(...).red_team_turn is None`, `dissent_surfaced is False`, `judge_decision.counterarguments is None`; audit payload `red_team == {"ran": False, "dissent_surfaced": False}`. **(D80.8 off-state)** |
| `test_redteam_runs_when_flag_on` | `HERMES_QUANT_REDTEAM_TURN=1` + judge present → `state.red_team_turn is not None`, `red_team_turn.metadata["red_team"] is True`, `metadata["prompt_hash"]` is 64-hex. |
| `test_dissent_surfaced_above_threshold` | Red-team conf 0.7 (≥0.5) → `state.dissent_surfaced is True`, `state.dissent_reason` == the `counterarguments` text. |
| `test_dissent_not_surfaced_below_threshold` | Red-team conf 0.2 (<0.5) → `state.dissent_surfaced is False`. (Deterministic threshold, not a vote.) |
| `test_counterarguments_field_filled` | `state.judge_decision.counterarguments` is non-None and equals `dissent_reason` (the reserved ADR-0002 field, now filled). |
| `test_redteam_failure_is_no_dissent` | `_run_red_team_turn` stub returns None (or raises) → stage completes, `dissent_surfaced is False`, **judge_decision unchanged** (direction/confidence identical to no-red-team run). **(failure-closed)** |
| `test_redteam_never_changes_direction` | Run the FULL dispatch (`run_llm_committee`, flag ON) with a red-team that maximally dissents (conf 1.0) → the emitted `portfolio_manager` judge `CommitteeTurn.direction` is **identical** to the same run with the red-team stubbed to None. **(D80.1: aggregation deterministic, no vote)** |
| `test_redteam_turn_not_in_directional_turns` | After dispatch, no `CommitteeTurn` in `turns` has `metadata.red_team is True`; the only place red-team data appears is the judge turn's `dissent_*` metadata keys. **(D80.1 structural)** |
| `test_audit_row_carries_red_team_block` | Patch `stage._audit_append`; assert captured `payload["red_team"]["dissent_surfaced"]` and `payload["red_team"]["confidence"]` are present (the W3-mineable record, O7). |
| `test_rounds_capped_single_turn` | The red-team adapter is invoked **exactly once** per stage regardless of `HERMES_QUANT_RESEARCH_DEBATE_ROUNDS` (count `_run_red_team_turn` calls via a spy). **(SOTA §4 cost discipline)** |

### NEW — `tests/unit/test_redteam_eval_gate.py` (the shadow eval-gate, the criterion to FLIP the flag)

The eval gate is "dissent-surfaced rate measurably changes without inflating false-flat rate." Encode it deterministically over a fixed synthetic corpus of N debate states (no LLM):

```python
def _dissent_surfaced_rate(states): return mean(s.dissent_surfaced for s in states)

def _false_flat_rate(states_no_rt, states_rt):
    """A 'false flat' would be a tick that went FLAT *because of* the red-team.
    W7 cannot cause a flat (it never touches direction), so this MUST stay 0."""
    ...
```

| Test | Asserts (flip criterion) |
|---|---|
| `test_eval_gate_dissent_rate_changes` | Over a fixed 50-state corpus, dissent-surfaced rate with the red-team ON is **strictly different** from OFF (OFF rate == 0.0; ON rate > 0.0 by construction of the corpus). |
| `test_eval_gate_false_flat_rate_not_inflated` | The set of FLAT decisions is **identical** between red-team-ON and red-team-OFF runs over the same corpus (red-team cannot create a flat). `false_flat_rate == 0.0`. **This is the hard gate.** |
| `test_eval_gate_aggregation_deterministic` | For each state, the judge `CommitteeTurn.direction`/`confidence` are bit-identical ON vs OFF (re-asserts no-vote at the eval level). |

> **Flip rule encoded:** the flag may be turned on in shadow iff `test_eval_gate_dissent_rate_changes` passes (effect is real) AND `test_eval_gate_false_flat_rate_not_inflated` passes (no harm) AND `test_eval_gate_aggregation_deterministic` passes (no vote-counting). All three are pure-Python, no network, run in CI.

### MODIFIED — `tests/unit/test_research_debate_wiring.py`

Add to the autouse `_isolate_research_debate_env` fixture: `monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)` so the existing T1–T11 stay byte-identical (off-state) regardless of suite ordering. Confirm T4/T5/T6 still pass with the new `ResearchPlan.counterarguments=None` default (they will — `extra="forbid"` permits the new optional field).

---

## 5. Eval-gate contract mapping (ADR-0080 §D80.3 — all five)

| Contract item | How W7 satisfies it |
|---|---|
| **(1) External-truth** | W7 emits no reward signal; persona-calibration is mined later (W3) against realized alpha. The red-team critique is telemetry, never an LLM self-score that grades a decision. |
| **(2) Held-out** | The flip test (`test_redteam_eval_gate.py`) runs over a held-out shadow corpus the component never tuned on; passing is necessary, not sufficient → operator flips the flag. |
| **(3) Robustness-not-peak** | The dissent threshold (`0.5`) is a fixed, non-optimized constant; no per-decimal tuning. Checkpoint-fallback is trivial: flag-OFF is the prior-best, and W7 must *strictly* add dissent-signal without false-flats to ship. |
| **(4) Bounded + provenance** | Exactly ONE red-team turn per stage (bounded, SOTA §4 cost discipline). The critique is tagged the agent's own prior output via `metadata.red_team=true` on the audit row; never re-ingested as ground truth. |
| **(5) Propose-only / deterministic / surface-dissent** | The turn proposes a critique + a `dissent_surfaced` flag; the deterministic aggregator and risk gate are untouched (no vote); dissent is surfaced to the operator (audit row + judge metadata + optional report line) rather than collapsed to consensus. |

---

## 6. Design decisions resolved (so the building agent needs no further research)

1. **Reuse `BullBearTurn` as the red-team payload, role overloaded to `"bear_researcher"`.** Avoids a new Pydantic schema + parser branch. The *semantic* role is carried by `metadata.red_team=true` (stage-owned, forge-resistant — overwritten in the adapter regardless of LLM output). The turn is never appended to `state.bear_turns` and direction is forced to 0. Alternative (new `RedTeamTurn` schema) rejected: more surface, no behavioral gain, breaks the "reuse, don't rebuild" mandate.
2. **Red-team runs AFTER the judge, attacking the judge's leading view** — not interleaved into bull/bear rounds. This is the IUI'24 / RedDebate distinction: bull/bear argue *positions*, the devil's-advocate attacks the *reasoning of the winner*. Running it post-judge is the only point where a "leading view" exists to attack.
3. **Dissent as metadata, never a directional turn.** This is the load-bearing safety property: by *not* appending a `CommitteeTurn` with nonzero direction, `deliberative.py` is structurally unable to let the red-team flip a decision. The test `test_redteam_never_changes_direction` pins it.
4. **Single turn, no round count.** SOTA §4: round n=1 gave +0.006 F1 for double cost. One additive turn is the rails-compliant, cost-disciplined choice. No `HERMES_QUANT_REDTEAM_ROUNDS`.
5. **`research_debate` audit kind reused** (not a new kind). The red-team data is a sub-block of the existing per-stage row, which is exactly the O7 mining target W3 already reads. No `VALID_KINDS` migration.
6. **Failure-closed = no dissent.** A red-team LLM failure must never block or alter a decision; `rt is None` → `dissent_surfaced=False`, judge untouched. Pinned by `test_redteam_failure_is_no_dissent`.

---

## 7. Verification commands (the building agent runs these)

```bash
# Off-state + all W7 unit tests
python -m pytest tests/unit/test_redteam_turn.py -q
# The flip eval-gate (the criterion to enable in shadow)
python -m pytest tests/unit/test_redteam_eval_gate.py -q
# Regression: existing debate wiring unchanged with flag OFF
python -m pytest tests/unit/test_research_debate_wiring.py -q
# Off-state byte-identity across the committee suite
python -m pytest tests/unit/test_llm_committee_caller.py tests/unit/test_llm_committee_prompts.py -q
# Prompt template splits cleanly (SYSTEM:/USER: markers present)
python -c "from hermes_quant.aggregators.llm_committee import _load_prompt, _split_system_user; _split_system_user(_load_prompt('devils_advocate'))"
```

**Done = all green, flag default-OFF, off-state byte-identical, and the three `test_redteam_eval_gate.py` flip criteria pass.**
