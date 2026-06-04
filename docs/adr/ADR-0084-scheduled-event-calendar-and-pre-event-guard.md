# ADR-0084: Scheduled-event calendar as asof-honest perception + a default-OFF pre-event REJECT/abstain guard

**Status:** Accepted (2026-05-31), implemented
**Date:** 2026-05-31
**Wave:** Perception/Reaction extension (forward-event awareness; default-OFF, eval-gated)
**Supersedes:** nothing
**Cites:** [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate, silence-by-default — the FINAL, IMMUTABLE authority; this ADR only ADDS a reject condition), [ADR-0027](ADR-0027-options-aware-risk-gate.md) (options-aware gate O1-O7 — this ADR adds O8), [ADR-0033](ADR-0033-evidence-store.md) (EvidenceStore `available_at` — the asof primitive a calendar event reuses), [ADR-0074](ADR-0074-catalyst-sense-semantic-fusion.md) (catalyst ingest adapter shape this clones), [ADR-0079](ADR-0079-perception-decision-reaction-architecture.md) (PerceptionFrame + extras carrier; consumers ignore unknown fields)
**Grounded in:** `docs/research/2026-05-31-r-financial-calendar-event-risk.md` (the study this ADR ratifies).

> **This ADR is default-OFF and eval-gated.** With `HERMES_QUANT_EVENT_RISK` absent/`0`, the calendar is a NullCalendar, the gate adds no reject condition, no `event_risk` field is populated, and behavior is byte-identical to today.

---

## Context

hermes-quant has **no scheduled-event awareness** (verified, file-grounded in the study §1). The catalyst layer ingests news that OCCURRED (`CatalystItem.published_at` is a single past-event anchor, ingest.py:33); there is no forward, *scheduled* event type and no FOMC/CPI/NFP/earnings blackout. The deterministic gate (gate.py) silences on halt/drawdown/cooldown/lookahead but is event-blind. The options gate (O1-O7) computes net theta/vega but is earnings-DATE blind — the IV-crush trap (a long-premium structure spanning earnings) is unmodeled. `scorers.py` is the only earnings-aware code and it uses `days_since_earnings` (PAST) with the `covered_call days_since_earnings>=5` convention.

External consensus (study §research) is unanimous: scheduled-event risk is a **hard pre-trade control independent of strategy logic** (ESMA hard-block; FIA/NexusFi), not a soft signal — which is precisely hermes-quant's immutable REJECT-only gate. The earnings-IV-crush literature (Dubinsky 2019 RFS; Alexiou 2025; VolRadar/ApexVol) is equally clear that the trap is on the **net-theta-paying / net-vega-long** side; premium sellers *harvest* the crush and must not be blocked.

The operator's question: add a financial/economic calendar as a risk + perception data point, and can agents deliberate on it?

## Decision drivers

- **D-1 The deterministic gate is the FINAL, IMMUTABLE authority (ADR-0004).** Any new capability may only ADD a reject/abstain condition; it must never touch the `{0,±0.05,±0.10,±0.15,±0.20}` ladder, amplify, widen, or flip a side. `abs(adjusted) <= abs(target)` must hold.
- **D-2 asof-honesty.** A forward event has TWO timestamps: `scheduled_for` (forward payload, must be gated) and `announced_at` (when the schedule became public — the asof anchor). The system must never consume an event's OUTCOME before `scheduled_for`, nor its mere EXISTENCE before `announced_at`.
- **D-3 Default-OFF + eval-gated.** Ships behind `HERMES_QUANT_EVENT_RISK=0`; byte-identical to today when OFF; promoted only after a replay eval proves honesty + monotone-non-increasing behavior.
- **D-4 No-key, low-dep, government-primary first.** Mirror catalyst/ingest.py's stdlib posture; no new hard dependency; keyed sources are optional fallbacks that degrade to silence-by-default when the key is absent.
- **D-5 Separation of authority between advisory perception and the hard backstop.** Agents may *read* event risk to shape conviction and produce auditable rationale, but the abstain decision must not *depend* on any agent reading it.

## Considered options

### Option A — Do nothing (status quo)
No event awareness. **Rejected:** leaves the documented IV-crush gap and the open-into-FOMC/earnings hazard, and the operator explicitly asked for this capability. The cost of inaction is real (long-premium-into-earnings losses, directional-into-print whipsaw).

### Option B — Deliberation-only event signal (agents reason about events; no deterministic guard)
Inject an `event_risk` field into `PerceptionFrame.extras`; let analysts + bull/bear/judge attenuate conviction. **Rejected as the *sole* mechanism:** agents are non-deterministic and can be argued out of caution (an LLM bull rationalizes trading into earnings). Violates D-1/D-5 and the unanimous external "hard control independent of strategy logic" guidance. Useful as a *complement*, not a backstop.

### Option C — Pure deterministic guard only (no perception field)
A `in_event_blackout` silence rule in the gate + an O8 options rule; no agent-visible field. **Safe but rejected as the *sole* mechanism:** it is mute (the debate can't explain *why* it abstained), causes whipsaw (full-size proposal → hard reject with no pre-softening), and forgoes the explainability/auditability the operator values.

### Option D — CHOSEN: stdlib calendar adapter + advisory `event_risk` field + independent deterministic backstop
1. **Data:** a stdlib `CalendarEvent` (two timestamps, `outcome: None`) adapter cloning ingest.py. No-key primary = vendored `fomc_calendar.seed.yaml` + BLS iCal; keyed fallbacks (FRED) and best-effort earnings (yfinance `.calendar`, SEC EDGAR 8-K 2.02) degrade to silence.
2. **Perception (advisory):** an optional, read-only, outcome-free `event_risk` key on `PerceptionFrame.extras`, filtered to `announced_at <= decision_asof`, that analysts + the committee MAY read (frame.py "consumers ignore unknown fields").
3. **Reaction (authority):** a pure `in_event_blackout` predicate consumed by the gate as a SILENCE rule (HIGH tier only, opening/increasing trades only, never blocks de-risking) + an options O8 rule firing only on net-theta-paying/net-vega-long structures (premium sellers exempt). The guard does NOT depend on any agent having read the field.

All behind `HERMES_QUANT_EVENT_RISK`, default-OFF, eval-gated, wired at ALL the seams the admissibility note flags (autonomous tick, daily brief, HITL, PaperReactor.execute).

## Decision

**Adopt Option D.** It is the only option that is simultaneously safe (D-1/D-5: hard backstop independent of agents), honest (D-2: two timestamps, outcome-free), low-risk (D-3/D-4: default-OFF, no-key primary, byte-identical when OFF), and useful (explainable: agents read the advisory field). The advisory field carries NO authority; the deterministic guard ADDS only a reject condition. ADR-0004 stays immutable.

## Consequences

### Positive
- Closes the documented options IV-crush gap (O8) and the open-into-event hazard with a textbook hard pre-trade control.
- asof-honest by construction: events without both timestamps are dropped; the gate's existing `available_at` lookahead silence enforces honesty for free if events are registered as EvidenceStore rows.
- Explainable: bull/bear/judge rationales can cite event proximity; analysts can pre-soften conviction, reducing whipsaw.
- Zero behavioral change when OFF; no new hard dependency; no-key primary path.

### Negative / risks
- **Over-rejection.** US Tier-1 macro affects nearly every USD symbol; a naive guard could blackout the whole book on ~12 FOMC + 12 CPI + 12 NFP days/yr. Mitigation: HIGH tier only, opening/increasing only, narrow N, track abstain-rate as a guardrail metric.
- **Earnings-date data is unreliable.** yfinance `.earnings_dates` future path is broken upstream; `.calendar` is date-only and best-effort. Mitigation: missing data ⇒ NO blackout (never fabricate one) + log a coverage gap; yfinance forward dates may only WIDEN risk, never inform direction.
- **Seed staleness.** FOMC dates rarely but occasionally change (emergency meetings). Mitigation: annual refresh script + freshness assertion (warn if latest seeded meeting is past).
- **Keyed-fallback / ADP gap.** FRED needs a free key (degrade to silence when absent); ADP is private and out-of-scope for the no-key tier — documented, not promised.
- **Two new surfaces to keep in sync** (the advisory field + the deterministic guard). Mitigation: the guard is the single source of the `in_blackout` truth; the field merely *reports* it.
- **Seam-coverage debt.** If the guard is wired at fewer seams than listed it inherits the existing admissibility hole. Mitigation: single shared predicate called from every seam; flag-off byte-identical test.

### Neutral
- Tier table and N windows are config-driven (the impact hierarchy shifts by regime); defaults `N_earnings=5` (matches the existing covered_call convention), `N_macro=1`.
