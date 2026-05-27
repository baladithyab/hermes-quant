# ADR-0043 — Three-Way Risk Committee (Aggressive / Conservative / Neutral)

**Status:** Accepted
**Date:** 2026-05-27
**Wave:** 3 (TradingAgents pattern backfill — gap #1 + gap #5)

## Context

Wave 2 (ADR-0044) introduced the TraderNode and `TraderProposal` structured
output between the research committee and the deterministic risk gate. The
TauricResearch reference architecture (research notes, `/tmp/research-tradingagents.md`)
runs an additional **3-way risk debate** between the trader and the portfolio
manager: an **Aggressive** persona (champions high-reward, high-risk),
a **Conservative** persona (prioritizes asset protection), and a
**Neutral** persona (balanced perspective).

This is gap #1 ("3-way Risk Committee — Aggressive / Conservative / Neutral
debate, round-robin"), and the wrapper that drives it via the trader is
gap #5 ("Trader intermediary stage").

The CV5 anti-pattern from earlier hermes-quant experiments — where a
"rejecting trader" subordinate could *amplify* sizing past what the rest of
the system had agreed to — must be excluded by construction here.

## Decision

We add a `RiskCommittee` orchestrator under
`hermes_quant/agents/risk_committee/` plus a `TraderNodeWithRisk` wrapper
under `hermes_quant/agents/trader_node.py`. Wave 3 ships the
**deterministic v0.1**; the **LLM-driven v0.2** is deferred behind an
`llm_caller: Callable | None = None` parameter.

### v0.1 — Deterministic decision rules

| Persona      | Rule                                                                                                         |
|--------------|--------------------------------------------------------------------------------------------------------------|
| Aggressive   | `amplify` if `size_fraction < 0.15` and `action != HOLD`; else `neutral`                                     |
| Conservative | `silence` if `stop_loss is None`; `silence` if `\|entry - stop\| / entry > 5%`; else `neutral`               |
| Neutral      | `silence` only if **both** Aggressive AND Conservative voted `silence` in the most recent round; else `neutral` |

Personas operate in fixed round-robin order (`aggressive → conservative
→ neutral`). The loop terminates at `count < 3 * max_rounds`
(TauricResearch's `should_continue_risk_analysis` convention). `max_rounds`
defaults to **1**, capped at **3**, overridable via env var
`HERMES_QUANT_RISK_ROUNDS`.

### v0.2 — LLM-driven (deferred)

Each persona owns a `SYSTEM_PROMPT_TEMPLATE` carrying the verbatim
TauricResearch preamble:

> Output conversationally as if you are speaking without any special
> formatting

This wording forces real debate text rather than bullet-point hiding (gap
#2 anti-pattern fix from the research notes). When v0.2 lands, the
prompts are already finalized and `RiskCommittee(llm_caller=...)` will
route per-turn calls through the supplied callable instead of
`persona.decide(...)`.

### Silence-multiplier-only invariant (the CV5 anti-pattern guard)

`silence_multiplier` starts at **1.0** and can **only ever decrease**.
Each `silence` vote multiplies it by 0.5. `amplify` votes are
**recorded** in the debate trail (for audit) but **never** raise the
multiplier above 1.0.

Pydantic enforces `silence_multiplier ∈ [0.0, 1.0]` on
`RiskDebateSummary`; the `TraderNodeWithRisk._apply_silence_multiplier`
also clamps defensively in case a future change weakens the upstream
constraint.

This means the worst the committee can do is reduce the trade to zero
(silence). It can never override the deterministic-gate-approved size
upward.

### Interaction with the deterministic risk gate (ADR-0004)

```
research_plan + advisor_signal
       ↓
   TraderNode  →  TraderProposal (size_fraction = ladder_size)
       ↓
   RiskCommittee.debate  →  RiskDebateSummary (silence_multiplier ≤ 1.0)
       ↓
   apply silence_multiplier  →  TraderProposal' (scaled or HOLD)
       ↓
   Deterministic Risk Gate (ADR-0004)  ←  FINAL AUTHORITY
       ↓
   PortfolioManager / PaperReactor
```

The committee runs **before** the gate. The gate retains final authority
— it can still veto, set position to HOLD, or lower size further. The
committee can only silence; it cannot bypass the gate.

## Schema

`RiskCommitteeTurn`:
- `persona: str`
- `turn_index: int (≥0)`
- `critique_text: str (1..2048 chars)`
- `evidence_ids: list[str] (≤32)`
- `risk_assessment: Literal["amplify", "silence", "neutral"]`
- `confidence: float (0..1)`

`RiskDebateSummary`:
- `trader_proposal_id: str`
- `turns: list[RiskCommitteeTurn] (≤32)`
- `silence_multiplier: float (0..1, default 1.0)`
- `final_recommendation: str (1..1024 chars)`
- `n_rounds: int (0..3)`
- `terminated_reason: str`

The summary is embedded inside the existing `Proposal.advisor_result`
field (key `risk_debate_summary`) — no schema migration to the
`Proposal` record itself.

## Brief script integration

`scripts/quant-daily-interim.py` (and the live
`~/.hermes/scripts/quant-daily-interim.py` copy) constructs a
`RiskCommittee` once per run and calls `.debate(trader_proposal, plan)`
for each actionable pick. The brief output adds a "🛡️ Risk debate" line
per pick listing the silence multiplier, round count, and final
recommendation text.

## Consequences

* **Positive**: Catches obvious-bad proposals (no stop, too-wide stop)
  before they reach the gate. The 3-persona structure also gives the
  human reviewer a clearer "what could go wrong" trail than a single
  scalar from the gate.
* **Positive**: Wave 3 ships *without* requiring API access. The v0.2
  LLM path is fully optional and behind a parameter rename.
* **Negative**: One additional Pydantic model + 38 tests to maintain.
* **Negative**: Personas are deterministic and may be too conservative
  for some setups (especially the 5%-stop threshold). Tunable via
  module-level constants.

## References

* `/tmp/research-tradingagents.md` — gap #1 (3-way Risk Committee),
  gap #2 (conversational preamble anti-pattern fix), gap #5 (Trader
  intermediary stage).
* `docs/adr/ADR-0044-trader-stage-and-structured-output.md` — Wave 2
  TraderProposal schema (which this committee critiques).
* `docs/adr/ADR-0037-llm-backed-committee-turns.md` — bull/bear/judge
  committee whose schema convention this ADR mirrors.
* `docs/adr/ADR-0004` — deterministic risk gate (final authority).
