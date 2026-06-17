---
status: proposed
date: 2026-06-17
deciders: [codeseys]
consulted: [deep-work-loop session 2026-06-17 (AEGIS strategy research wf_0f064078, 5 agents/552K tokens, academic-first)]
amends: null
supersedes: null
---

# ADR-0098: AEGIS strategy taxonomy (admissible structures) + the 2-level multi-leg model

> The operator mandate: AEGIS trades stock AND options AND multi-leg stock/option combos,
> with the system able to **execute a whole combo OR break it and manage each leg**
> (decompose / convert / risk-adjust). This ADR fixes (1) WHICH structures are admissible
> under the no-naked-leverage posture, (2) how each maps to the discrete NAV-fraction
> ladder, and (3) the 2-level data model that makes a composite play and its legs coexist
> and be managed at either level. Default-OFF + eval-gated per increment.

**Cites:** [ADR-0027](ADR-0027-options-gate.md) (the options gate that sizes/admits — the
collateral/defined-risk rules this taxonomy obeys), [ADR-0029](ADR-0029-multi-leg-paper-reactor.md)
(the multi-leg reactor + the evidence-before-live gate + MultiLegProposal.max_loss),
[ADR-0082](ADR-0082-deterministic-structure-selection-layer.md) (the deterministic
structure-select table — no LLM picks legs), [ADR-0091](ADR-0091-reactors-emit-traded-delta.md)
(Option-E absolute-target fold the leg children obey), [ADR-0085](ADR-0085-ledger-authority-and-state-derivation.md)
(executions.jsonl is authority; state.db is a derived projection — the fold this model extends),
[ADR-0097](ADR-0097-paper-vs-live-slippage-haircut.md) (the live-realistic execution cost
the multi-leg penalty consumes).

---

## Context and Problem Statement

The producer today builds ONLY `covered_call`/`cash_secured_put`/`wheel` and is
structurally single-short-leg (`options/recipes.py:build_multi_leg_proposal`). The
deterministic `structure_select` table deliberately abstains on every defined-risk
multi-leg because the producer cannot build them. To realize the full vision we must
(a) decide the admissible structure SET (and which are permanently excluded), and (b)
model a composite play AND its legs together so the system can act at either level.

A full-tier research pass (wf_0f064078, academic-first: Whaley 2002, Black & Szado, the
CBOE BXM/PUT/CNDR/BFLY index family, Wysocki & Slepaczuk 2024 arXiv:2407.13908, McKeon
2016, Chaput & Ederington 2003, Niblock 2017) produced the taxonomy + the 2-level model
below. Raw output: `docs/research/2026-06-17-aegis-strategy-research-raw.json`.

## Decision Drivers

- **No naked / undefined-risk leverage gambling** (charter + ADR-0027 O2): every admitted
  structure must be defined-risk or fully collateral-secured.
- **The deterministic gate is final + the LLM never picks legs** (ADR-0082): structure
  selection is a deterministic table; the committee may only silence.
- **One ledger, one fold** (ADR-0085/0091): the money-state projection must not fork; a
  multi-leg model that double-counts a leg, or that lets the fold see the composite as a
  position, re-introduces the divergence class.
- **Act at either level** (operator decision #5): execute the whole combo, OR decompose /
  convert / risk-adjust a single leg, without orphaning the structure.

## Decision Outcome — Part A: the admissible structure set

**15 admissible structures** (the 25-variant taxonomy minus naked/undefined-risk):

*Defined-risk (Group 1):* Long Call, Long Put, Long Straddle, Long Strangle, Bull Call
Spread, Bear Put Spread, Bull Put Spread, Bear Call Spread, Iron Condor, Iron Butterfly,
Long Butterfly, Diagonal/PMCC, Protective Put, Collar, Back Spread, Stock Replacement.

*Collateral-secured (Group 2):* Covered Call, Cash-Secured Put, Wheel, Covered Strangle
(admitted only when BOTH legs independently clear their collateral).

**Permanently EXCLUDED (never admissible):** naked Short Call, naked Short Put, Short
Straddle, Short Strangle, the naked portion of a Ratio Spread, Box Spread. The gate
REJECTS these structurally (not a flag — a posture invariant).

**Ladder → contract-count mapping.** The discrete NAV-fraction ladder
{0, ±0.05, ±0.10, ±0.15, ±0.20} maps to contracts via
`floor(target_nav_fraction × NAV / collateral_per_contract)`, where
`collateral_per_contract` = strike×100 (CSP), basis_per_share×100 (CC), spread_width×100
(verticals/condors), net_debit (PMCC/diagonals). Any target that floors to 0 contracts is
SILENCED (identical to fractional-equity rounding to zero). The 0.05 step is the minimum
tranche unit — no fractional ladder positions are created to enable prettier tranche ratios.

## Decision Outcome — Part B: the 2-level multi-leg model

A composite play and its legs COEXIST in the bus, ledger, and monitor:

- **Level-1 (COMPOSITE):** one ExecutionRecord, `asset_class="multi_leg"`, `asset`=underlying,
  `role="parent"`, carrying `multi_leg_id` (= proposal_id), `strategy_kind`, `outer_qty`,
  `net_greeks`, signed `net_fill` (net debit/credit), `fill_size_pct` (NAV-fraction sized
  against the WHOLE structure). It is an AUDIT ROLLUP — **the PortfolioState fold SKIPS it**
  (`_is_multileg_family_parent`); it carries NO position-moving quantity.
- **Level-2 (LEGS):** N child ExecutionRecords, each with its own OCC-21 `asset` + its own
  `asset_class` ("us_option"/"equity"), the same `multi_leg_id` back-link, `leg_index`,
  `role` ("leg"/"equity_leg"), signed true-unit quantity, per-contract `fill_price`,
  `position_intent`. **The fold reads ONLY the children.**

**Gate sizes at the composite level** (`OptionsGateResult.contracts = outer_qty`; BPR +
max_loss are structure-level); it accepts/rejects the WHOLE structure atomically (no
partial-leg pass). **The monitor watches BOTH levels:** composite net greeks (refreshed
each tick from live leg prices via `aggregate_net_greeks`, never the stale parent snapshot)
AND each leg's own risk.

**New `composite_plays` table** (state.db) keyed on `multi_leg_id` with
`state ∈ {open, decomposed, closed, partial}`, `outer_qty`, `net_entry_price`,
`fill_size_pct`, timestamps. The legs stay on the bus; this table is the composite's
lifecycle record the monitor + decompose/convert logic read.

**Decompose / convert / risk-adjust** (operator decision #5) operate by closing/opening
LEG children while transitioning the composite `state`:
- *Decompose:* break the combo into independently-managed legs (a leg breaches its own
  risk; the thesis on one leg invalidates; assignment looms on a short leg).
- *Convert:* roll/leg-into another structure (a tested short put → a spread) — atomic per
  the MLEG order class where possible, else with the H1 partial-state guard.
- *Risk-adjust:* close/roll/hedge ONE leg without orphaning the structure.

**Hazards + mitigations (H1–H6, all wired before any multi-leg increment):**
- **H1 leg-orphan on partial decompose:** if some legs close but the broker rejects the
  rest, write composite `state="partial"` on the FIRST leg close in a decompose sequence;
  a "partial" composite triggers mandatory operator attention and is NEVER auto-closed.
- **H2 double-count a leg at both levels:** the fold skips `asset_class=="multi_leg"`
  parents (only legs move position) — a parity test asserts the parent contributes 0 to NAV.
- **H3 fold sees composite vs leg:** the `role`/`asset_class` discriminator is the single
  source of truth; `_is_multileg_family_parent` is the one classifier.
- **H4 convert atomicity:** prefer the broker MLEG order class; a non-atomic convert uses
  the H1 partial guard.
- **H5/H6:** stale-greeks (refresh from live leg prices, never the cached parent snapshot)
  and assignment-cash double-reservation (ADR-0027 D7 composite_intent budgets collateral
  once for the wheel) — carried forward.

### Consequences

- **Positive:** the full stock/options/combo vision becomes buildable under one posture-safe
  structure set; the 2-level model lets the system manage a combo whole OR per-leg without
  forking the ledger.
- **Positive:** naked/undefined-risk is excluded by construction, not by a flag.
- **Negative / accepted:** the producer must be extended structure-by-structure (each a
  default-OFF increment with its own eval) — large but bounded; sequenced in ADR-0099 +
  the build-order seeds.
- **Negative / accepted:** the composite_plays table + the partial-state machine are new
  money-state surface; they get the BEGIN-IMMEDIATE + finite-guard + parity-test discipline
  the rest of the ledger has.

### Confirmation

Satisfied by: (1) a gate test that each EXCLUDED structure is rejected; (2) a ladder→contracts
property test (floor-to-zero silences); (3) a fold parity test that a multi_leg parent
contributes 0 to NAV and only legs move position (H2/H3); (4) an H1 test that a partial
decompose writes state="partial" and is never auto-closed; (5) per-structure eval gates
(ADR-0099) before each producer extension goes live.

## More Information

- Build order + the TP/SL + clean-window gates are in [ADR-0099](ADR-0099-tpsl-strategy-and-clean-window-gates.md).
- Raw research (sources + per-structure detail): `docs/research/2026-06-17-aegis-strategy-research-raw.json`.
- Seeds: the `aegis-ao*` epic + per-structure build seeds reference this ADR's structure set.
