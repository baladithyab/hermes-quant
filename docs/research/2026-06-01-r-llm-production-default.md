# R / Decision: The path to make LLM stages production-default

**Date:** 2026-06-01
**Seed:** hermes-quant-4665 (B41 — research-then-document decision)
**Author:** agent (deep-work-loop research-then-document task)
**Type:** DECISION + CRITERIA doc. **This is NOT a flag flip.** It sets the bar a
gated LLM stage must clear before its default flips from OFF to ON, gives a
per-stage recommendation, and states the safety frame.
**Status:** Proposed (informs a future ADR amendment; does not change any flag default today)
**Reads first (cited):** ADR-0031 (silence-by-default / governance plane), ADR-0054
(LLMCaller + TraderNode v0.2), ADR-0056 (RiskCommittee v0.2), ADR-0057 (Reflector v0.2),
ADR-0062 (rollout playbook), ADR-0065/0066 (research debate), `docs/operations/ROLLOUT.md`,
`AGENTS.md` §"Anti-patterns from reference projects" (§313–329), `hermes_quant/eval/promotion_gate.py`,
`hermes_quant/observability/fallback_probe.py`, `tests/unit/test_redteam_eval_gate.py`.

---

## 0. TL;DR (the decision)

**No stage flips to production-default on the strength of "the unit gate is green."** A
stage's default flips from OFF→ON only when it clears **five gates simultaneously**, in
order, and **none of them is "the LLM produced plausible output"**:

1. **Determinism/reproducibility under the byte-identical rail** — when the flag is OFF the
   path is byte-identical to today; when ON, the *decision* the gate sees is reproducible
   from the audit log (prompt_hash + parsed_dump), not the prose.
2. **Cost ceiling** — a per-decision USD/token budget with a zero-call local kill-switch,
   surviving restart, enforced *before* the call.
3. **OOS eval gate: the LLM stage beats its own heuristic fallback out-of-sample** — not
   in-sample, not on one window, not on prose quality; on the stage's decision-relevant
   metric across ≥2 market regimes incl. a drawdown regime, with the contamination guard clean.
4. **HITL / gate-still-final invariant intact** — the deterministic risk gate (ADR-0004),
   the discrete sizing ladder, the 3-of-5 committee quorum, and per-order human confirmation
   remain downstream of and authoritative over every LLM stage. The LLM is evidence, never
   the ballot and never the executor.
5. **Silence-by-default proven live** — the fallback probe (ADR-0060) passes for the stage
   AND a live observation window shows the fallback firing rate is low and bounded.

**Per-stage verdict (detail in §5):**

| Stage | Flag | Closest to default-ON? | Recommendation |
|---|---|---|---|
| **Reflector v0.2** | `HERMES_QUANT_REFLECTOR_LLM` | **Closest.** Write-only, off the decision path, Oracle-Fallacy-guarded. | **Candidate first** — but it has *no decision metric*, so its "OOS gate" is a different shape (§5.1). Default-ON after a clean observation week + a faithfulness/no-leakage check. |
| **RiskCommittee v0.2** | `HERMES_QUANT_RISK_COMMITTEE_LLM` | Medium. Affects approval but quorum invariant preserved. | **Stay OFF** until an approval-quality OOS gate exists (does LLM-voted approval beat deterministic-voted approval on realized alpha/Sortino OOS?). That gate **is not built yet.** |
| **TraderNode v0.2** | `HERMES_QUANT_TRADER_LLM` | Lower (highest visibility, real numeric-override gap, §5.3). | **Stay OFF.** Close the numeric-override gap first, then needs the same OOS gate as the committee. |
| **ResearchDebate** | `HERMES_QUANT_RESEARCH_DEBATE` | Lowest. Most LLM calls, conversational, highest cost/latency. | **Stay OFF.** Most expensive, least-evaluated; gate on cost ceiling + a dissent-quality OOS gate. The W7 red-team shadow eval-gate (`test_redteam_eval_gate.py`) is the *template* for what this looks like. |

The honest current state: **all four stages clear gate 1 (default-OFF/byte-identical) and
the silence-by-default half of gate 5; none of them clears gate 3 (the OOS-beats-fallback
gate), because that gate does not yet exist for any LLM stage.** Building those eval axes is
the gating work — see the follow-up seeds in §7.

---

## 1. Why this is a decision doc and not a flag flip

The prior self-evolution flag-flip decision (`docs/operations/2026-05-31-selfevolve-flag-flip-decision.md`)
established the house discipline that B41 must inherit: **a flag flip is a change to the
running system and must clear its gate AND not degrade current behavior.** That doc flipped
nothing beyond what was already live, because every candidate flip was either inert
(no live precondition) or harmful (it would silence real signal). The same logic governs
the LLM stages, with one addition: the LLM stages are not just *gated*, they are
*more expensive and less reproducible* than their fallbacks. So the bar to flip their
default is strictly higher than "the precondition is met" — it is "the precondition is met
AND the LLM is demonstrably better OOS AND it is affordable AND it cannot become the
decision authority."

`AGENTS.md` §329 states the load-bearing invariant: **every reference project we studied lets
the LLM be the final execution authority somewhere** (TradingAgents at the trader role,
AI-Trader at the copy-trading cascade, moon-dev at the override boundary). Our pattern is the
*inverse*: deterministic risk gate + HITL downstream of the LLM. "Production-default LLM
stage" must never be allowed to quietly become "LLM is the decision." This doc exists so
that bar is written down before, not during, any rollout.

---

## 2. What the reference projects actually do (research findings)

The seed asked specifically how TradingAgents / AI-Trader / Vibe-Trading gate LLM-in-the-loop
for production. Findings (deepwiki on the repos + web research):

**TradingAgents (TauricResearch).** Multi-agent deliberation (Analyst → Researcher debate →
Risk team debate → Portfolio Manager). Key controls they *do* have, that we should keep:
- **Structured output** for the Research Manager / Trader / Portfolio Manager (Pydantic), with
  graceful free-text fallback when a provider can't do structured output.
- **Deterministic signal processing**: the PM's structured output is rendered to markdown, then
  a `SignalProcessor` *deterministically* extracts the 5-tier rating via `parse_rating`. The
  final tradeable signal is a deterministic projection of LLM prose, not the prose itself.
- **Dual-model thinking** (deep vs quick model) to manage cost/latency.
- **Deferred reflection** as an implicit eval loop (store decision → resolve with realized
  return + alpha → reflect → inject as context).

What they *lack* (and `AGENTS.md` §322–323 rejects): free-text `position_sizing`, and a
`FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` string-grep contract on stochastic output.
And critically: it is **research software, explicitly "not financial advice,"** executing on a
*simulated* exchange with the PM as the final gate — i.e. they have no production HITL or
deterministic risk gate; the PM (an LLM) is the authority. That is exactly the failure mode we
invert.

**AI-Trader (HKUDS).** "100% fully-automated agent-native trading." The LLM/agent **is** the
trade authority: an agent publishes a realtime signal and it is *immediately executed for
followers* — **no human approval step.** It does have *deterministic risk gates* (cash-balance
check, position validation, server-side price enforcement to prevent manipulation), and a
post-hoc data-integrity cleanup (`cleanup_dirty_trade_data.py`). But there is **no pre-live
backtest/eval gate** an agent must pass, and the blind 1:1 copy-trading cascade (`AGENTS.md`
§324) removes per-order confirmation entirely.

**Vibe-Trading (HKUDS).** The lone reference exception (`AGENTS.md` §329): it draws the boundary
at **"no live execution"** by *omission of broker SDKs from the tool registry*. That is a
capability-separation pattern (no execution surface at all), which we already implement more
strongly via ADR-0007 (read-only plugin surface vs CLI-only execution).

**Convergent academic pattern (AgenticAITA, arXiv 2605.12532 — "deliberative multi-agent for
autonomous trading").** This is the closest external mirror of our intended design:
- **Layer A = deterministic hard gates executed BEFORE any LLM call.** The risk manager is an
  explicit *hybrid* deterministic/LLM gate.
- **Inference Gating Protocol (IGP):** a mutex that regulates concurrent LLM access for
  *deterministic execution and auditability*.
- **Event-driven selective LLM activation** (a statistical trigger decides *when* the LLM is
  even consulted) — the LLM is not on every tick.
- Typed JSON interaction contracts; episodic memory injected as context.

**The synthesis for us:** the production-safe pattern in the literature is exactly
**deterministic-gate-first, LLM-as-one-input, structured output, deterministic projection of
the LLM's text into the tradeable decision, LLM not the executor.** We already have all of
these as architecture (ADR-0004 gate, ADR-0015 propose-decide-react, ADR-0031 silence-by-default,
structured-output + LLMCaller). The remaining gap is purely the **evidence** layer: nobody has
shown OOS that turning a stage ON *improves* the decision, and nobody has put a *cost ceiling*
in front of the call.

---

## 3. LLM determinism / cost / latency best practices (research findings)

**Determinism — `temperature=0` is necessary but NOT sufficient.** Multiple sources converge:
- Greedy decoding still varies from floating-point + GPU parallelism + batch-scheduling
  (batch-invariance is the real lever; "same hardware + same version" is the only firm
  guarantee). One test: Qwen-3-235B produced 80 unique completions over 1,000 temp-0 prompts.
- API `seed` is **best-effort** (OpenAI: "mostly" deterministic; Gemini: best-effort; **Anthropic
  has no public seed**). Provider snapshot changes silently break it; log `system_fingerprint`.
- **The effective lever is to constrain the output space, not tweak probabilities:** structured
  outputs / schema-constrained generation collapse the variance that lives in phrasing/ordering
  (Humanloop: 35.9% → ~100% schema conformance with strict mode). OpenAI structured outputs use
  token-level constrained decoding (~100% schema conformance); Anthropic tool_use is 95–99%.
- **Plan/execute split:** allow exploration in a planning phase, then *lock the plan* and run
  execution at low temperature against fixed tools — isolates non-determinism to planning, makes
  execution reproducible, limits prompt-injection blast radius. Disable self-critique loops in
  production (they "murder reproducibility").
- **Golden-response regression testing** + **idempotency test** (run twice, expect identical) is
  the canonical determinism check.

**What this means for hermes-quant's byte-identical rail.** We cannot promise the *LLM's bytes*
are reproducible (no provider guarantees it). We **can and must** promise:
- (a) flag-OFF is byte-identical to the prior deterministic path (already true; enforced by the
  per-stage `*_v02` tests and `fallback_probe`); and
- (b) the **decision** the deterministic gate consumes is a *deterministic projection* of the
  LLM output (TradingAgents' `parse_rating` pattern; our `bind_structured` + deterministic
  numeric recompute), and is fully **replayable from the audit log** (prompt_hash + raw_response
  + parsed_dump per ADR-0054 §4). Reproducibility here = "given the logged LLM output, the
  decision is recomputable bit-for-bit," NOT "the LLM emits the same bytes." This is the
  achievable, honest definition of the rail for an LLM stage, and it is the one the eval gate
  must measure against.

**Cost.** Research is blunt: the canonical failure is the **runaway loop with no ceiling**
($12K weekend; $47K LangChain A2A loop over 264h). The controls that matter:
- **Per-call-chain budget enforced BEFORE the call** (zero-call kill-switch: if the next call
  would exceed remaining budget, reject locally — `$0` spent, no network). Provider account
  limits do NOT propagate across sub-agents; child cost must count against the parent.
- **`max_tokens` on every call** (without it a flagship can emit 8K+ tokens at 10–100× expected
  cost). Structured/JSON output also caps length at the schema boundary.
- **Prompt caching** (stable system prompt + schema is the cacheable prefix; 50–90% input
  discount, *also reduces latency*; two reads pay for one Anthropic write).
- **Model routing / cascade** (cheap model first, escalate only on a *reliable* verifier;
  cascade is strictly worse than flagship-only if the verifier is broken).
- Dashboard `cache_read / (cache_read + cache_creation)` (want >0.8); alert on hourly spend
  rate, session cost, and budget burn at 70/90%.

**Latency.** Structured-output FSM compile adds 50–200ms on first use; routing classifiers add
10–50ms; caching is the main win (Anthropic cites 11.5s→2.4s on a 100K-token prefix). For a
once-per-tick trading loop this is acceptable; for the *conversational* research debate (N rounds
× M roles) it compounds and is the binding cost/latency constraint (§5.4).

---

## 4. The five gates a stage must clear to become production-default

A stage's flag default flips OFF→ON only when **all five** pass. Each is checkable; where the
check does not yet exist, that is itself the gating work (§7).

### Gate 1 — Determinism / reproducibility under the byte-identical rail
- **OFF = byte-identical.** Flag-OFF (or `llm_caller=None`, or no API key) MUST be bit-for-bit
  identical to the prior deterministic path. Enforced today by the `*_v02` test suites and the
  `fallback_probe` happy-path-off case. **Already met by all four stages.**
- **ON = decision is a deterministic projection + replayable.** The tradeable decision is
  recomputable from the logged LLM output (prompt_hash + raw_response + parsed_dump), via
  structured output + deterministic post-processing. The LLM's free-text must never be the
  command channel (no string-grep; no `int(filter(isdigit, line))`-style parsing — `AGENTS.md`
  §321/§323).
- **Call config pinned + logged:** pinned model snapshot, `temperature=0`, `top_p=1`,
  `max_tokens` set, `system_fingerprint` (where available) recorded in the audit event.
- **No self-critique loops in the production path** (exploration only; locked in execution).

### Gate 2 — Cost ceiling
- A **per-decision (and per-tick) USD/token budget** with a **zero-call local kill-switch**:
  if the next call would exceed remaining budget, reject locally and fall back to v0.1 —
  `$0` spent, no network. **Must survive process restart** (durable, like the halt SQLite).
- Child/stage costs count against the parent budget (the research debate's N×M turns are one
  budgeted unit).
- `max_tokens` on every call; prompt caching enabled on the stable system-prompt+schema prefix;
  a documented per-stage cost-per-decision figure measured in the smoke test.
- A spend-rate circuit breaker (interrupt the call loop, preserve state, surface a structured
  error — do not kill the process) wired to the existing kill-switch.
- **Not built yet** for any stage; this is the second-largest gap after Gate 3.

### Gate 3 — OOS eval gate: the LLM stage beats its own heuristic fallback (the keystone)
This is the gate that does not exist yet and is the reason no stage flips today. Shape, by
analogy to `PromotionGate` and the W7 red-team shadow eval-gate:
- **Decision-relevant metric, OOS.** For decision-shaping stages (trader, committee): the
  LLM-ON path must beat the deterministic fallback on the stage's *realized* decision quality
  (e.g. realized alpha / Sortino / approval-precision against later outcomes), **out-of-sample**,
  not in-sample and not on prose quality.
- **≥2 market regimes incl. a drawdown/bear regime.** Per the overfitting-trap finding
  (NexusTrade): a single-window outlier is the textbook signature of overfitting; require
  evidence across distinct regimes including a bear market. This mirrors `PromotionGate`'s
  `max_drawdown_floor` and the recommendation to run an additional 6-month window before live.
- **Contamination guard clean.** `PromotionGate` already disqualifies on
  `contamination_guard_fired` (evaluation window may overlap LLM training data). For an LLM
  stage this guard is *more* important — the eval window must post-date the model snapshot's
  training cutoff, or the "edge" is memorized.
- **Effect is real AND harmless.** Borrow the W7 template (`test_redteam_eval_gate.py`): the ON
  path must (1) measurably change the decision-relevant rate vs OFF, (2) without inflating a
  harm rate (there, false-flat-rate == 0), (3) while a downstream invariant stays bit-identical
  (there, the judge direction/confidence — i.e. no vote-counting). A stage that changes nothing
  is inert (don't pay for it); a stage that changes things *and degrades a harm metric* is worse
  than the fallback.
- **Deterministic over a fixed corpus, no live network** (the W7 gate runs 50 synthetic debate
  states with no LLM/network — the *gate* is offline-deterministic even though the *stage* calls
  an LLM). This keeps the gate itself byte-identical and CI-safe.

### Gate 4 — HITL / gate-still-final invariant
Non-negotiable, overrides any seed (`AGENTS.md` §385):
- The **deterministic risk gate (ADR-0004)** runs and is authoritative *downstream* of the LLM.
- The **discrete sizing ladder** {0, ±0.05, ±0.10, ±0.15, ±0.20} is the only sizing channel;
  no free-text sizing (`AGENTS.md` §322).
- The **3-of-5 committee quorum** (ADR-0043) is preserved regardless of how votes are produced.
- **Per-order human confirmation** (ADR-0015) for every live order; no LLM-output→money path.
- The **kill-switch / halt** (ADR-0009/0031) sits below every LLM stage and is non-overridable
  by any LLM (the explicit inverse of moon-dev `risk_agent.py:319`).
- **The LLM is one piece of evidence, never the ballot and never the executor.**

### Gate 5 — Silence-by-default proven, in test AND live
- The `fallback_probe` (ADR-0060) passes for the stage across all failure modes (timeout,
  rate-limit, server-error, malformed-JSON, schema-invalid, empty) → each reverts to v0.1.
  **Already met** (probe covers all four surfaces).
- PLUS a **live observation window** (per the ROLLOUT.md dwell times) shows the
  `fallback_event_count` per surface is low and bounded — sustained fallback means the provider
  is misconfigured and the "ON" is theater (you're paying for the call and getting v0.1 anyway).

---

## 5. Per-stage recommendation

### 5.1 Reflector v0.2 — `HERMES_QUANT_REFLECTOR_LLM` — CLOSEST, but a different gate shape
**Why closest:** it is **write-only and off the decision path** (writes
`reflections.jsonl`; the gate logic is unchanged — ROLLOUT.md Step 2 / lowest blast-radius after
HMM). It already has two strong safety properties most stages lack:
- **Oracle-Fallacy guard (ADR-0057 §5, verified in `reflector.py:543-547`):** `tau_observable`
  is ALWAYS taken from the deterministic helper; the LLM only supplies `reflection_text` +
  `lesson_category`, so it cannot embed future knowledge via a crafted timestamp.
- **Self-grade refusal invariant (`reflector.py:569-579`):** refuses to reflect on a decision its
  own model made (normalized model-id comparison), preventing self-grading.

**The catch:** the reflector has **no decision metric**, so Gate 3 ("beats fallback OOS") does not
apply in its trading-return form. Its eval gate is a *different shape*: a **faithfulness /
no-leakage** check — does the LLM reflection (a) stay grounded in the logged trade facts (no
hallucinated specifics), (b) leak no post-trade information into a field that feeds future
decisions, and (c) produce `lesson_category` labels that are stable and useful to the retriever?
This is closer to an LLM-as-judge / golden-response check than a backtest.

**Recommendation:** **Reflector is the first candidate for default-ON.** It clears Gates 1, 4, 5
already; it needs Gate 2 (a cost ceiling — cheap, one call per closed trade) and a *reframed*
Gate 3 (faithfulness + no-leakage, not OOS-alpha) plus a clean observation week. Flip its default
only after that faithfulness axis is built and green and the observation week shows bounded
fallback. **Do not flip today** — the faithfulness axis is not built.

### 5.2 RiskCommittee v0.2 — `HERMES_QUANT_RISK_COMMITTEE_LLM` — STAY OFF
**State:** first stage that can shift approval outcomes; the **3-of-5 quorum + silence-by-default
rejection invariants are preserved** (ROLLOUT.md Step 3; ADR-0056). Clears Gate 4 by construction
and Gate 1-OFF / Gate 5-probe.
**Blocker:** Gate 3 has no instantiation here. The right gate: *does LLM-voted approval beat
deterministic-voted approval on realized outcome quality (alpha/Sortino/approval-precision) OOS,
across ≥2 regimes, contamination-clean?* That gate is **not built.** ROLLOUT.md's "approval_rate
within ±10% of baseline" is a *drift* check, not a *better-than* check — staying within ±10% only
says it didn't blow up, not that it helped.
**Recommendation:** **STAY OFF** until the approval-quality OOS axis exists and is green, plus
Gate 2.

### 5.3 TraderNode v0.2 — `HERMES_QUANT_TRADER_LLM` — STAY OFF (close a gap first)
**State:** highest-visibility surface (proposal text + structured fields). ROLLOUT.md Step 4 /
ADR-0054 §D2 assert that the deterministic stop-loss/target/alpha helpers are *always recomputed*
and override the LLM's numbers, so "P&L math does not depend on the LLM."
**Honest gap found in code:** `TraderNodeLLM.__call__` on the success path returns the LLM's
`TraderProposal` **directly** (`trader.py:596-599`) — it does NOT re-run the v0.1 deterministic
helpers and overwrite the numeric fields inside `TraderNodeLLM` itself. The "always recompute"
guarantee, as written, lives at a *downstream* layer (or is design intent not yet enforced at this
seam). **Before this stage can be a default-ON candidate, Gate 1 requires that the numeric
override be enforced and tested at the TraderNodeLLM boundary** (so the LLM's stop-loss/target/
alpha can never reach the gate un-recomputed). Until then the "P&L math is LLM-independent" claim
is not enforced where the proposal is produced. (This is a real follow-up, §7.)
**Recommendation:** **STAY OFF.** (1) Close the numeric-override gap + test it; (2) then the same
OOS-beats-fallback gate as the committee; (3) Gate 2.

### 5.4 ResearchDebate — `HERMES_QUANT_RESEARCH_DEBATE` — STAY OFF (most expensive, least-evaluated)
**State:** the most LLM-intensive surface — conversational, N rounds × M roles (bull/bear +
research-manager judge), per ADR-0065/0066. It already falls back to the legacy committee path on
any uncaught exception, and the **W7 red-team shadow eval-gate
(`tests/unit/test_redteam_eval_gate.py`) is the model template** for what a stage-level eval gate
looks like here (effect-is-real + no-harm + aggregation-stays-deterministic, over a fixed synthetic
corpus, no network).
**Blockers:** (a) **cost/latency is the binding constraint** — N×M LLM calls per tick is exactly
the "3–10× more calls than a chatbot, context grows quadratically" regime the cost research warns
about; Gate 2 is mandatory and non-trivial here (budget the whole debate as one unit, cap rounds,
cache the shared system prompt). (b) Gate 3 = a **dissent-quality OOS axis** (does the debate
surface *useful* dissent that improves the downstream decision OOS, vs the legacy committee), which
does not exist beyond the W7 shadow gate's narrower "dissent-surfaced rate" effect check.
**Recommendation:** **STAY OFF**, last in line. Default-ON only after Gate 2 (debate-level budget)
and a dissent-quality OOS gate are built and green.

### Ordering note
This per-stage ordering matches the ROLLOUT.md blast-radius order (HMM → Reflector →
RiskCommittee → Trader), with ResearchDebate appended as the most expensive. The HMM regime stage
(`HERMES_QUANT_REGIME_HMM`) is **not an LLM stage** (it loads an HMM model file, not an LLM) and is
out of scope for B41, though it shares the default-OFF/byte-identical rail.

---

## 6. The safety frame (what flipping a default must never do)

1. **Never let "production-default LLM stage" become "LLM is the decision authority."** The
   deterministic risk gate, sizing ladder, committee quorum, per-order HITL, and kill-switch are
   downstream of and authoritative over every LLM stage — the inverse of all four reference
   projects (`AGENTS.md` §329). This is the single invariant that, if violated, makes every other
   gate moot.
2. **Default-OFF is the safe rollback at all times.** Because flag-OFF is byte-identical and the
   v0.1 path reads/writes the same event stores (ROLLOUT.md §3), rollback is "unset the env var,
   restart" with no state migration. A default-ON stage must preserve this property: unsetting it
   returns to the exact prior behavior.
3. **A green unit gate is necessary, not sufficient.** The self-evolve flag-flip decision proved
   that a unit-gate-green flag can still be inert or harmful in live state. For LLM stages the
   additional sufficiency conditions are Gate 2 (cost) and Gate 3 (OOS-beats-fallback). Flipping a
   default without Gate 3 is paying more for a possibly-worse, definitely-less-reproducible
   decision — a degrading flip, which the seeds policy forbids (`AGENTS.md` §387).
4. **Silence-by-default is the schema-failure kill-switch; the halt is the logical-failure
   kill-switch** (ROLLOUT.md §5). Both remain non-overridable by any LLM.
5. **The eval gate itself must be offline-deterministic** (no live network; fixed corpus), so
   that *certifying* a stage does not introduce the non-determinism we are trying to gate. The W7
   gate is the proof-of-pattern.
6. **Cost is a safety property here, not just an ops concern.** An unbounded LLM loop is a
   money-software incident (the $12K/$47K cases). The cost ceiling with a zero-call kill-switch is
   part of the safety frame, not an optimization.

---

## 7. Follow-up seeds the decision implies

These are the *gating work* — until they exist and are green, no LLM stage default flips. Mirror
into the seeds tracker (children/relatives of B41 = `hermes-quant-4665`):

1. **B41-a — Per-stage cost ceiling + zero-call kill-switch (Gate 2).** A pre-call budget guard
   (per-decision + per-tick USD/token), durable across restart, `max_tokens` on every call,
   prompt-caching on the stable prefix, child-cost-counts-against-parent for the research debate.
   Falls back to v0.1 when budget is exhausted (`$0`/no-network). *Highest priority — blocks every
   stage and is independently valuable as a safety rail.*
2. **B41-b — OOS "LLM-beats-fallback" eval axis for decision-shaping stages (Gate 3, keystone).**
   A `PromotionGate`-shaped, offline-deterministic gate over a fixed corpus: LLM-ON vs
   deterministic-fallback on realized decision quality, ≥2 regimes incl. drawdown,
   contamination-guard clean, effect-real-and-harmless (W7 template). One axis instantiated for
   RiskCommittee (approval-quality) and one for Trader (proposal-quality). *Blocks 5.2 and 5.3.*
3. **B41-c — Reflector faithfulness / no-leakage eval axis (reframed Gate 3 for 5.1).** LLM-as-judge
   + golden-response check: reflection grounded in logged trade facts, no post-trade leakage into
   decision-feeding fields, stable `lesson_category`. *Blocks 5.1 (the closest stage).*
4. **B41-d — Enforce + test the TraderNodeLLM numeric override at the producing seam (Gate 1 gap,
   §5.3).** Re-run the deterministic stop-loss/target/alpha helpers inside `TraderNodeLLM` on the
   v0.2 success path and overwrite the LLM's numeric fields, with a test that the LLM's numbers
   can never reach the gate un-recomputed. *Blocks 5.3 independently of Gate 3.*
5. **B41-e — Dissent-quality OOS axis + debate-level budget for ResearchDebate (Gates 2+3 for
   5.4).** Extend the W7 shadow eval-gate from "dissent-surfaced rate effect" to "useful dissent
   improves the downstream decision OOS vs legacy committee," and budget the whole N×M debate as
   one unit with a round cap. *Blocks 5.4.*
6. **B41-f — Pin + log LLM call config (Gate 1).** Pin model snapshot, `temperature=0`, `top_p=1`,
   `max_tokens`; record `system_fingerprint` (where available) in the LLMCaller audit event;
   add an idempotency/golden-response regression check on the deterministic *projection* of a
   fixed logged LLM output. *Cross-cuts all stages; small.*
7. **B41-g — ADR amendment.** Once B41-a/-b exist, amend ADR-0062 (rollout playbook) to add the
   five-gate criteria and the per-stage default-flip checklist, replacing the implicit "dwell time
   + ±10% drift" criteria with the explicit OOS-beats-fallback gate. *Governance close-out; gated
   on B41-a/-b landing (respects the ADR-freeze seed hermes-quant-d9d8 if still active).*

---

## 8. Appendix — sources

- `AGENTS.md` §313–329 (reference-project anti-patterns + the convergent-failure synthesis),
  §385–388 (rails / seeds policy).
- ADR-0054 §D2/§D4/§D5 (TraderNodeLLM fallback chain, 8-field audit, silence-by-default);
  ADR-0056/0057 (committee/reflector wiring); ADR-0062 (rollout playbook); ADR-0065/0066 (debate).
- `docs/operations/ROLLOUT.md` (blast-radius order, dwell times, KPIs, kill-switch);
  `docs/operations/2026-05-31-selfevolve-flag-flip-decision.md` (the flip-discipline precedent).
- `hermes_quant/eval/promotion_gate.py` (the gate shape: alpha>0, Sortino>0.5, drawdown>-0.20,
  contamination-guard disqualifier, "one more 6-month window before live").
- `hermes_quant/observability/fallback_probe.py` (ADR-0060 failure-mode matrix);
  `tests/unit/test_redteam_eval_gate.py` (the offline-deterministic stage-level eval-gate template).
- `hermes_quant/memory/reflector.py:543-579` (Oracle-Fallacy guard + self-grade refusal).
- `hermes_quant/agents/trader.py:596-613` (the LLM-success path that returns the proposal directly
  — the numeric-override gap, §5.3 / B41-d).
- External research (2026): TradingAgents/AI-Trader deepwiki; AgenticAITA (arXiv 2605.12532,
  deterministic hard gates + IGP + selective LLM activation); LLM-determinism best practices
  (temp=0 insufficient; structured output collapses variance; plan/execute split; golden-response
  + idempotency testing); cost-engineering (per-call-chain budget + zero-call kill-switch;
  `max_tokens`; prompt caching; cascade-needs-reliable-verifier; the $12K/$47K runaway-loop cases);
  overfitting trap (single-window outlier ⇒ require ≥2 regimes incl. bear).
