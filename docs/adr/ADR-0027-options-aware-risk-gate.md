# ADR-0027: Options-aware risk gate — extends ADR-0004 with Greek limits, BPR, and assignment risk

**Status**: Accepted (2026-05-24), implemented
**Date**: 2026-05-24
**Target**: v0.5.0 (Wave B of `docs/plans/2026-05-23-options-daily-retro.md`)
**Extends**: ADR-0004 (deterministic risk gate), ADR-0009 §P0-1 (Kelly σ² fix), §P0-5 (rule ordering), §P1-9 (asset-class isolation)
**Related**: ADR-0028 (options data layer), ADR-0029 (multi-leg paper reactor), ADR-0015 (HITL), ADR-0026 (retro loop)
**Cross-cuts**: ADR-0002 (analyst protocol — option-aware analysts emit composite directions), ADR-0011 (portfolio reconstruction, MTM), ADR-0021 (recipe runtime — risk profile selection)

---

## Context

ADR-0004 ships a deterministic equity/crypto risk gate. ADR-0009 §P1-9 partitions `Portfolio` and `RiskConfig` per `(account_id, asset_class)` and reserves `asset_class="option"` for v0.2. The plan doc (`docs/plans/2026-05-23-options-daily-retro.md`) now lands options as v0.5.0. This ADR extends the gate to options.

The user's Alpaca paper account is Level-3-enabled with $100k equity and $200k buying power. The priority strategy mix is: covered call → cash-secured put → wheel state machine → 30–90 DTE directional swing → LEAPS thesis. None of these involve uncovered short options. Per AGENTS.md, the project's three discipline principles (silence-by-default, hard rules over learned policy, reproducibility) extend verbatim into the options gate — the gate must not be loosened for options, only refined.

R2 (`docs/research/2026-05-23-r2-options-risk-gate-prior-art.md`) surveyed LEAN, NautilusTrader, hftbacktest, Tastyworks, and IBKR. The consensus pattern across professional venues:

1. Margin/BPR (Buying Power Reduction) is the binding constraint for options, not notional exposure (R2 §2.4–2.5).
2. Net-Greeks aggregation is mandatory before any per-position check; spreads' net-delta is materially smaller than naive sum-of-leg deltas (R2 §3, §4.5).
3. Stress-testing the underlying ±10–15% to compute worst-case loss is the standard approach (Tastyworks BPR; IBKR Exposure Analysis). For v0.5.0 we use simpler closed-form max-loss calculations; full stress-testing deferred.
4. Pin risk and gamma spikes are kill-switch territory, not just-warning territory (R2 §4.4).
5. Discrete-action sizing must translate to *contract count*, not notional shares — R2 §4.3 specifies the mapping.

### Specific gaps in ADR-0004 that options expose

ADR-0004's Rule 6 sizing assumes:
- Linear delta (1.0 for stock, signed direction for short).
- Notional exposure equals position value.
- Single instrument per signal.

Each fails for options:
- **Linear delta** breaks immediately. A 25Δ call has 0.25 stock-equivalent exposure; a 50Δ has 0.5; gamma changes these on every bar. Naively sizing on `signal.direction * notional` would over-allocate a covered-call strategy by 4× and under-allocate a deep-ITM long call by 2×.
- **Notional** breaks even harder. A short put's "notional" is `strike × 100` (assignment exposure); a long call's "notional" is the premium (max loss); a vertical spread's "notional" is the strike width minus net premium. Three different numerators.
- **Single instrument** breaks for any multi-leg strategy: covered call (long stock + short call), CSP (cash + short put), wheel (state machine across CC ↔ CSP ↔ assignment), spreads.

The fix is not to throw out ADR-0004's rule sequence but to extend it with options-specific pre-checks (max-loss validation, BPR, Greek caps) and replace the sizing rule with a contract-count translation.

---

## Decision

The options risk gate extends ADR-0004's rule sequence rather than replacing it. The implementation adds an `options_default` profile that the recipe selects when `asset_class == "equity_options"`. Equity-only and crypto-only profiles continue unchanged.

### D1 — Rule sequence (extends ADR-0009 §P0-5 supersede)

The ordering preserves "circuit breakers FIRST" from ADR-0009 §P0-5. Options-specific rules sit between Rule 5a (edge-sign alignment from ADR-0004 amendment 2026-05-13) and Rule 6 (sizing).

| # | Rule | Trigger | Outcome |
|---|------|---------|---------|
| 0 | Halt active | Halt scope `(account, asset_class, symbol?)` matches | silence |
| 1 | Drawdown breaker | `drawdown_pct > max_drawdown_pct` (5% per envelope) | flatten + halt indefinite |
| 2 | Daily-loss breaker | `daily_loss_pct > max_daily_loss_pct` (0.5% per envelope) | flatten + halt-until-next-session |
| 3 | Cooldown after recent loss | `last_loss_minutes_ago < cooldown` | silence |
| 4 | Signal-flat / zero-confidence | `direction == 0 or confidence < 1e-6` | silence |
| 5 | Cost-gate threshold | `\|edge\| < cost_multiple × round_trip_cost` | silence |
| 5a | Edge-sign alignment | `expected_signed_edge × direction <= 0` | silence |
| **O1** | **Max-loss / margin validation** | `max_theoretical_loss > max_position_pct × NAV` | **silence** |
| **O2** | **No naked uncovered (v0.5.0)** | short legs without covered position or wider long leg | **silence** |
| **O3** | **Portfolio gamma cap** | `\|net_portfolio_gamma\| > gamma_cap` | **silence** |
| **O4** | **Theta budget** | `daily_net_theta_pct > theta_budget_pct` | **silence** if theta-burning entry; **pass** if theta-collecting |
| **O5** | **Vega cap** | `\|net_portfolio_vega\| > vega_cap` | **silence** |
| **O6** | **BPR buffer** | `total_BPR + new_BPR > 0.80 × NAV` | **silence** |
| **O7** | **Pin-risk filter** | `min(DTE) ≤ 3 AND \|moneyness\| ≤ 0.02` | **silence** for new entries |
| 6 | Sizing → contract count | per D3 below | proceed |
| 7 | Min-trade-size guard | `\|delta_contracts\| < 1` | silence |

Rules `O3`/`O5` apply at the **portfolio** level, not per-trade. Rules `O1`/`O2`/`O4`/`O6`/`O7` apply per-trade.

The kill-switch conditions (D5 below) sit *outside* the gate — they fire from `settlement_loop` on every tick, can flatten existing positions, and install durable halts.

### D2 — Concrete numeric defaults (`options_default` profile)

Per R2 §4 and the user's stated risk envelope:

```yaml
quant:
  risk:
    profiles:
      options_default:                  # equity_options profile, $100k paper
        # Inherited from ADR-0004 / ADR-0009:
        max_position_pct: 0.10          # cap one position at 10% NAV per envelope
        action_step: 0.05               # discrete sizing (NOT widened)
        cost_multiple: 2.0              # round-trip cost gate
        max_drawdown_pct: 0.05          # 5% strategy DD halt (envelope)
        max_daily_loss_pct: 0.005       # 0.5% NAV daily-loss halt (envelope)
        quarter_kelly: 0.25
        cooldown_after_loss_minutes: 60

        # Options-specific (per R2 §4):
        max_short_call_delta_per_position: 0.30   # CC: short the 30Δ or lower
        max_short_put_delta_per_position: 0.30    # CSP: short the 30Δ or lower
        max_net_delta_pct_nav: 0.50               # |net Δ × spot| ≤ 50% NAV
        gamma_cap_pct_nav: 0.05                   # R2 §4.1 rule 2
        vega_cap_pct_nav: 0.10                    # |net vega × 1pt IV| ≤ 10% NAV
        theta_budget_pct_nav_per_day: 0.02        # max 2% NAV/day decay (R2 §4.4)
        bpr_buffer_pct_nav: 0.80                  # leave 20% cash buffer (R2 §4.1 rule 3)
        bpr_kill_switch_pct: 0.95                 # 5% absolute floor (R2 §4.4 rule 2)
        max_assignment_risk_pct_nav: 0.20         # cumulative cash risk if all CSPs assign
        min_dte_for_new_entry: 7                  # never open new positions <7 DTE
        pin_risk_dte_threshold: 3                 # within 3 DTE + 2% moneyness → silence
        pin_risk_moneyness_threshold: 0.02
        max_strategies_per_underlying: 1          # one CC OR one CSP, not both, until D7
        max_concurrent_open_positions: 8          # envelope (cross asset class)
```

These values are tuned for a $100k paper account with the user's accepted envelope. The `max_short_call_delta_per_position: 0.30` follows the wheel-strategy convention (30Δ shorts have ~70% probability of expiring worthless). `gamma_cap_pct_nav: 0.05` is R2's recommended portfolio-level gamma normalized to underlying-price moves. The `bpr_kill_switch_pct: 0.95` leaves a 5% absolute floor against margin call (R2 §4.4 rule 2).

### D3 — Sizing translation: ¼-Kelly NAV → contract count

ADR-0004's Kelly formula (with the σ² fix from ADR-0009 §P0-1) produces a target *NAV fraction*. Per R2 §4.3, options translate this to contracts via two paths depending on strategy class:

**Directional (Long Calls / Long Puts / LEAPS / Long Verticals)** — sizing tracks *premium spent* (max loss = premium):

```python
edge = signal.magnitude * signal.calibrated_probability
kelly_fraction = (edge / max(market.volatility ** 2, 1e-8)) * cfg.quarter_kelly
target_nav = signal.direction * min(cfg.max_position_pct, abs(kelly_fraction)) * nav
target_nav = round_to_step(target_nav, cfg.action_step * nav)

# For a long call/put leg:
contracts = math.floor(abs(target_nav) / (premium * 100))
```

**Income / Assignment-Risk (Covered Calls, CSPs, Short Puts)** — sizing tracks *underlying collateral* (max loss = strike × 100 minus premium):

```python
# Covered call: collateral = current basis of long stock
# CSP: collateral = strike × 100 (cash needed if assigned)
contracts = math.floor(abs(target_nav) / (collateral_per_contract))
```

**Defined-Risk Spreads (Vertical, Iron Condor)** — sizing tracks *width minus net credit* (max loss):

```python
max_loss_per_contract = (strike_width - net_credit_received) * 100
contracts = math.floor(abs(target_nav) / max_loss_per_contract)
```

**Critical**: Per R2 §4.3 caveat, do NOT size on "delta equivalents" in v0.5.0. Sizing 2× 50Δ options to match 100 shares is a path the retro loop may eventually surface as a tunable amendment, but as a default it's a footgun (gamma re-prices the equivalence on every bar). Stay on **capital-at-risk** for v0.5.0.

The discrete `action_step` (0.05) still applies to the *NAV target* before contract-count translation. Contract count is integer-floor; the residual is recorded as `sizing_residual_pct` in the signal record so the gate's discreteness is auditable.

### D4 — Per-strategy hard rules + soft warnings

| Strategy | Hard rule (silence on violation) | Soft warning (Discord, no halt) |
|---|---|---|
| `covered_call` | Long-stock leg's basis exists in portfolio AND `qty_long_stock ≥ 100 × short_call_contracts` (truly covered, not "almost covered"). Short call delta ≤ `max_short_call_delta_per_position`. DTE ∈ `[7, 60]`. | Earnings ≤ DTE; ex-div ≤ DTE; bid-ask spread of short call > 10% of premium. |
| `cash_secured_put` | Cash collateral `≥ strike × 100 × contracts` reserved (not just nominally available — earmarked in `Portfolio.cash_reserved_for_csp`). Short put delta ≤ `max_short_put_delta_per_position`. DTE ∈ `[7, 60]`. | Earnings ≤ DTE; bid-ask spread > 10%; underlying within 5% of 52-week low (assignment risk elevated). |
| `wheel` | At any time, exactly ONE active leg per underlying — either a CSP or a CC (post-assignment). The wheel state machine (ADR-0029 §D5) cannot enter a new CSP while a CC is open on the same name, and vice versa. Sum of CSP cash reservations + CC stock basis ≤ `max_assignment_risk_pct_nav`. | Wheel has been on the same underlying >90 days without rotation; stock has drifted >15% from initial entry basis. |
| `swing_directional` (30–90 DTE) | `cfg.min_dte_for_new_entry ≤ DTE ≤ 90`. Long premium only (no naked short). Strike OTM ≤ 1 standard deviation. | IV rank > 80 (paying high IV on entry); event risk (earnings ≤ 14 days) before exit; IV percentile rising. |
| `leaps_thesis` | DTE ≥ 365. Delta ≥ 0.70 (deep ITM, equity-like). Premium ≤ `max_position_pct × NAV` (capital-at-risk = full premium). | IV at entry > 40% (LEAPS in high-IV regime risks vega crush); annualized roll cost > 8%. |

The hard rules are encoded in `hermes_quant/risk/options_gate.py::_check_strategy_hard_rules(strategy, legs, portfolio, cfg)`. Each strategy's check is a pure function of `(legs, portfolio_state, cfg)` — no LLM, deterministic, fixture-tested.

The soft warnings surface in the HITL Discord proposal as a `warnings` array; they do NOT silence. Per AGENTS.md and ADR-0026, the loop can later propose elevating a warning to a hard rule if data supports it — that's the retro loop's job, not the gate's.

### D5 — Kill-switch conditions (auto-halt, fires from settlement_loop)

These run independently of the gate, on every tick, scoped per `(account, asset_class)` per ADR-0009 §P1-9:

1. **Gamma spike (the widowmaker — R2 §4.4 rule 1):**
   - Trigger: `|net_portfolio_gamma × spot²|` exceeds `2 × gamma_cap_pct_nav × NAV` for any single underlying.
   - Action: install durable halt for `(account, "equity_options", underlying)`. Existing positions are NOT auto-flattened (forced unwinds at gamma extremes are typically worse than holding); HITL `hermes quant flatten <symbol>` is required.

2. **Margin-call proximity (R2 §4.4 rule 2):**
   - Trigger: `total_BPR > bpr_kill_switch_pct × available_equity` (default 95%).
   - Action: install indefinite halt across ALL options strategies. Reactor flattens out-of-the-money short legs first (lowest reactor latency, highest BPR-relief-per-action), then logs and waits for human.

3. **Theta bleed limit (R2 §4.4 rule 3):**
   - Trigger: `daily_realized_theta_decay > 2 × theta_budget_pct_nav_per_day × NAV` (twice the soft budget).
   - Action: halt new entries. Existing positions held. Soft warning fired to Discord.

4. **Pin-risk lockout:**
   - Trigger: any open position is within `pin_risk_moneyness_threshold` of strike with `DTE ≤ pin_risk_dte_threshold`.
   - Action: silence new entries on the same underlying; flag the position for HITL review.

5. **Assignment cash shortfall (paper-mode-specific, R1 §4 paper NTA delay):**
   - Trigger: a paper assignment fired but `Portfolio.available_cash < strike × 100 × contracts_assigned`.
   - Action: indefinite halt on the underlying; HITL must `hermes quant resume` AND increase paper cash before resuming. This catches the case where the gate's `max_assignment_risk_pct_nav` was violated through the paper NTA-next-day delay.

Kill-switch halts are `(account, asset_class, optional symbol)` scoped per ADR-0009 §P1-9 and persist via the SQLite `halts` table. They never auto-clear from a successful trade; only `hermes quant resume` clears them, with confirmation.

### D6 — Multi-leg net-Greeks aggregation (per R2 §4.5)

The gate evaluates legs as a unit. For each candidate proposal:

```python
def aggregate_legs(legs: list[OptionLeg]) -> NetGreeks:
    """Aggregate per-leg greeks into portfolio-level net greeks.

    Side convention: long = +1, short = -1.
    Multiplier: 100 shares per contract (US equity options).
    """
    net = NetGreeks()
    for leg in legs:
        side = +1 if leg.side == "buy" else -1
        n = side * leg.contracts * 100
        net.delta += leg.greeks.delta * n
        net.gamma += leg.greeks.gamma * n
        net.theta += leg.greeks.theta * n
        net.vega += leg.greeks.vega * n
    return net

def evaluate_options_proposal(proposal, portfolio, cfg):
    candidate_net = aggregate_legs(proposal.legs)
    new_portfolio_net = portfolio.net_greeks + candidate_net  # vector add

    # Rule O3: gamma cap
    spot = portfolio.spot_for(proposal.underlying)
    portfolio_gamma_dollar = abs(new_portfolio_net.gamma * spot * spot)
    if portfolio_gamma_dollar > cfg.gamma_cap_pct_nav * portfolio.nav:
        return Silence("portfolio_gamma_cap")

    # Rule O5: vega cap
    if abs(new_portfolio_net.vega) > cfg.vega_cap_pct_nav * portfolio.nav / 100:
        return Silence("portfolio_vega_cap")
    # ... etc
```

Greeks are sourced from the data layer (ADR-0028) which guarantees they are filled (Alpaca-provided when present, py_vollib-synthesized when missing). The gate refuses to evaluate a proposal whose legs have `greeks is None` — this is a fail-closed posture, not a fail-open one.

### D7 — Composite-strategy recognition (wheel intent)

R4 §4 (the methodology DSL note) flagged the case where a covered_call methodology and a cash_secured_put methodology both fire on the same underlying. This is a *wheel intent* — the operator implicitly wants to play one side until assignment, then rotate. The gate must recognize this:

1. If two open legs on the same underlying form a covered_call + CSP pair on the same expiration cycle, the gate budgets margin/BPR ONCE, not twice (the legs are economically a strangle but operationally a wheel; collateral for the CSP and basis for the CC don't double-count).
2. If a new proposal would create such a pair, the gate surfaces the composite intent in `proposal.composite_intent: "wheel"` and applies wheel hard rules (D4 row 3).
3. The gate refuses to enter a third leg (e.g. a new CSP while CC + CSP are already open — that's a strangle/wheel double-up, materially riskier than the user's stated mix).

This logic lives in the gate, not the aggregator (per R4 §7 recommendation — "the cross-strategy correlation case gets first-class support in the risk gate, not the aggregator. Risk gate is where margin/BPR is budgeted").

---

## Consequences

### Positive

- **The five priority strategies map to deterministic per-strategy hard rules.** Every rule is fixture-testable; the entire gate stays free of learned policy.
- **BPR is the binding constraint, not notional.** This matches how professional risk systems work and prevents the leverage-via-spreads class of failure.
- **Net-Greeks aggregation is mandatory.** Naive per-leg checks are explicitly disallowed.
- **Kill-switches are scoped per ADR-0009 §P1-9.** A widowmaker on AMD does not halt unrelated NVDA strategies.
- **Sizing translation is capital-at-risk-based.** No delta-equivalent footgun.
- **Wheel recognition lives in the gate.** No double-budgeting of collateral when two methodologies converge.

### Negative

- **More configuration surface.** The `options_default` profile has 15+ new knobs. Mitigation: default values are tuned for the user's $100k paper, retro loop (ADR-0026) can propose adjustments.
- **Stress-testing is closed-form, not Monte Carlo.** Tastyworks-grade BPR uses ±15% underlying stress + worst-case scenario; we use the simpler closed-form max-loss. Acceptable for v0.5.0 (paper-only, ¼-Kelly, conservative defaults). Can graduate to Monte Carlo in v0.6.0 if retro surfaces evidence the closed-form is too loose.
- **Greeks must be present.** The fail-closed posture means a data outage on the chain provider silences all options activity. This is the correct trade-off (silence-by-default) but it's a hard dependency on ADR-0028's greek-completion strategy.
- **Pin-risk lockout may silence the last day of an otherwise-correct CC/CSP.** This is intentional; closing positions ≤3 DTE adds work but materially reduces gamma-blowout exposure. HITL flatten is available if the operator wants to ride.
- **No naked uncovered shorts in v0.5.0.** Closes off some legitimate strategies (naked puts on tickers you'd be happy to own can be profitable). Defer until v0.6.0 with retro-loop track-record data; for now, "no naked" is the single biggest defense against a junior-Quant blowup.

---

## Alternatives Considered

### A1: Reuse ADR-0004's gate verbatim, treat options as "high-leverage equities"

Just multiply leverage by ~5× and let Kelly sort it out. Rejected: ignores assignment risk, ignores Greeks, would size LEAPS the same as covered calls (categorically different risk profiles), and fails the no-naked rule trivially.

### A2: Stress-test via Monte Carlo on every gate evaluation

Per Tastyworks BPR / IBKR Exposure Analysis: stress underlying ±15% in N=1000 paths and compute worst-case loss. Rejected for v0.5.0: latency budget. Closed-form is <100µs per evaluation; Monte Carlo even with vectorized numpy is ~10–50ms. The closed-form is accurate for defined-risk strategies (which is all of v0.5.0's mix). Defer Monte Carlo to v0.6.0 if retro surfaces evidence the closed-form is loose for new strategies.

### A3: Delta-equivalent sizing instead of capital-at-risk

Tempting because it makes options "look like" stocks. Rejected per R2 §4.3 caveat: gamma re-prices the equivalence on every bar; the gate would silence-then-allow positions on a coin-flip. Capital-at-risk is stable across underlying moves until expiry.

### A4: Allow the LLM aggregator (ADR-0023) to propose a Greek override

I.e., committee deliberation could "override" the gamma cap if conviction is high enough. Rejected. Hard rules over learned policy. The aggregator emits an `AggregatedSignal`; the gate decides. The aggregator cannot loosen the gate. This invariant is the project's single biggest defense against reward hacking.

### A5: Continuous action step for options (e.g. fractional contracts)

US equity options are integer-only on the wire. We respect that: contract count is `math.floor`, the residual is recorded but not traded. Continuous fractional sizing would also break the "discrete action space" invariant from AGENTS.md.

### A6: Trust Alpaca's broker-side margin check

I.e., let the broker reject the order if it violates margin and skip the gate's BPR rule. Rejected. (a) Paper-mode broker-side validation is more lenient than live; we'd be blind in development. (b) Even if reliable, broker-side rejection is post-decision; we want pre-decision silence so the proposal never enters the journal/calibrator as a "would-have-traded." (c) Fail-closed > fail-open for money software.

---

## Open Questions

1. **What's the right `bpr_buffer_pct_nav` for a $100k paper vs a $1M live account?** R2 cited 80% as a generic recommendation. Smaller accounts may need 70% (less buffer = more pressure; smaller accounts hit kill-switches harder); larger accounts can run 85%. Defer to retro-loop after first 30 days.

2. **Should we vega-bucket by tenor?** A position with `+vega @ 30-DTE` and `-vega @ 90-DTE` has a low net-vega but is exposed to a tenor-structure shift (vol-curve steepening). For v0.5.0 we treat vega as a scalar; tenor-bucketing is a future extension.

3. **How does the gate interact with R3's retro loop on widening?** The retro loop can propose, e.g., "increase `max_short_call_delta_per_position` from 0.30 to 0.35." This is `scope_type: "gate_threshold"` in ADR-0026's amendment schema. Hard cap from this ADR: retro cannot widen `max_position_pct`, `max_drawdown_pct`, `max_daily_loss_pct`, `bpr_kill_switch_pct` — those are envelope rules per AGENTS.md.

4. **Pin-risk lockout: silence new entries OR flatten existing?** Currently silence-only. If a position pin-risks the operator's whole NAV, flattening would be safer; but forced unwinds at gamma extremes are typically worse than holding. Lean toward silence + HITL Discord alert. May change after the first observed pin event.

5. **Do we need a "max strategies per underlying" higher than 1 for v0.5.0?** D4 row 3 currently caps at one strategy per underlying. This blocks legitimate multi-leg structures like collars (long stock + protective put + short call). We accept this restriction for v0.5.0 because the user's priority mix is single-strategy; multi-strategy per name is v0.6.0+ work.

---

## Implementation Sketch

```
hermes_quant/risk/
├── gate.py                      # existing equity/crypto gate (unchanged)
├── options_gate.py              # NEW: extends gate.py with O1-O7 + sizing translation
├── greeks.py                    # net-greeks aggregation
├── strategies/
│   ├── __init__.py
│   ├── covered_call.py          # _check_covered_call_hard_rules
│   ├── cash_secured_put.py
│   ├── wheel.py                 # state-machine hard rules
│   ├── swing_directional.py
│   └── leaps_thesis.py
├── kill_switches.py             # gamma-spike / BPR-margin-call / theta-bleed / pin-risk
└── _calendars.py                # existing per ADR-0004 amendment 2026-05-13
```

Public surface:

```python
# hermes_quant/risk/options_gate.py
def options_gate(
    proposal: OptionsProposal,            # from ADR-0029
    market: MarketState,
    portfolio: Portfolio,                  # asset_class="equity_options" partition
    halt_state: HaltState,
    cfg: RiskConfig,
) -> Optional[OptionsAction]:
    """Returns an OptionsAction or None for silence.

    Rules in order: 0, 1, 2, 3, 4, 5, 5a, O1, O2, O3, O4, O5, O6, O7, 6, 7.
    Per-strategy hard rules fire inside O1/O2 dispatch.
    Composite-intent recognition (D7) fires before O1.
    """
```

Recipe selection (per ADR-0021):

```yaml
# ~/.hermes/quant/recipes/socalminh-covered-call.yaml
risk_gate: options_default
risk_gate_config:
  # Tighter than profile default for this conservative strategy:
  max_short_call_delta_per_position: 0.25
  bpr_buffer_pct_nav: 0.70
```

The recipe override merges shallow-into the profile defaults; missing keys inherit from `options_default`. Out-of-envelope overrides (e.g., `max_drawdown_pct: 0.20`) are rejected at recipe-load time with a clear error.

Settlement_loop (ADR-0010 + ADR-0029) wires the kill-switches to fire on every tick after position-state update and before the next gate evaluation.

---

## Test Plan

### Unit tests (deterministic, fixture-driven)

1. **Per-strategy hard rules** — for each of the 5 strategies, fixture-driven test cases for: passes, max-delta-violation, DTE-out-of-range, basis-mismatch (CC), cash-shortfall (CSP). ≥10 cases per strategy.
2. **Net-Greeks aggregation correctness** — for canonical 2-leg verticals (bull call, bear put, iron condor): verify net-delta, net-gamma, net-theta, net-vega match closed-form expectations.
3. **Kelly → contracts translation** — three classes (directional / income / spread): given target_nav and a fixture chain, verify contract count is `math.floor(target_nav / collateral)`.
4. **Rule ordering** — feed a proposal that would violate Rule 5 (cost gate) AND Rule O3 (gamma cap). Verify silence is attributed to Rule 5 (earlier in sequence) — important for `gate_rejection_reason` accuracy in postmortem.
5. **Pin-risk silencing** — with `DTE = 2` and underlying within 1% of strike, the gate silences new entries on the underlying. Existing positions are not auto-flattened.
6. **Wheel recognition** — propose a CSP on a name that already has an open CC; verify gate marks the proposal as `composite_intent: "wheel"` and applies wheel hard rules. Propose a third leg; verify silence with reason `wheel_double_up_blocked`.
7. **Fail-closed on missing greeks** — proposal with `legs[0].greeks is None` → silence with reason `greeks_missing`.

### Property-based tests (hypothesis)

1. **Gate never widens NAV exposure beyond `max_position_pct`** — given any `OptionsProposal`, after gate, `target_max_loss / nav <= max_position_pct + epsilon`.
2. **Net-delta cap holds across all leg-count combinations** — generate random 1–4 leg proposals; verify post-gate `|net_delta| <= max_net_delta_pct_nav × nav`.
3. **Halt scope contains** — given any halt at `(account, asset_class, symbol)` scope, ALL proposals on that scope are silenced; proposals on disjoint scope pass the halt check.

### Kill-switch integration tests

1. **Gamma-spike auto-halt** — fixture: portfolio with growing short-gamma exposure; on the tick where `|gamma$|` exceeds threshold, kill-switch fires, durable halt installed in SQLite, subsequent gate calls silence. Verify halt persists across daemon restart.
2. **BPR margin-call kill-switch** — simulate BPR climbing through 95%; verify halt + reactor flattens lowest-BPR-relief-per-action OTM short legs first.
3. **Theta-bleed limit** — simulate >2× daily theta decay realized; verify new-entry halt + Discord soft warning, existing positions held.

### End-to-end paper smoke (ADR-0029-coupled)

1. Run a 1-day paper session on Alpaca with the `socalminh-covered-call` recipe and a planted "earnings tomorrow" scenario; verify the gate attaches the `earnings_within_dte` warning to the proposal, the proposal still silences via `cost_gate` because the high IV pre-earnings inflates the cost-gate baseline.
2. Run a wheel-state scenario: enter CSP, fast-forward to assignment (paper), verify the wheel state machine flips to "long stock + ready for CC," next tick proposes a CC with `composite_intent: "wheel"`, gate budgets BPR once, settles correctly.

---

## References

- `docs/research/2026-05-23-r2-options-risk-gate-prior-art.md` — full prior-art survey (LEAN, NautilusTrader, Tastyworks, IBKR). **This ADR ports R2 §4 (recommendations) almost verbatim**, with hermes-quant–specific numeric defaults.
- `docs/research/2026-05-23-r1-alpaca-options-api.md` §4 — paper assignment/exercise behavior; R1 §4's "paper NTA delay" motivates kill-switch #5.
- ADR-0004 — original equity gate; this ADR extends without weakening.
- ADR-0009 §P0-1 (Kelly σ² fix), §P0-5 (rule ordering), §P1-9 (asset-class partitioning) — preserved invariants.
- AGENTS.md "Action space is discrete" — stays true; contract count is integer-floor of a discrete-step NAV target.
- `docs/plans/2026-05-23-options-daily-retro.md` "Risk envelope" — the user's accepted defaults (0.5%/5% halts, ¼-Kelly, 10% max single-trade notional, 8 concurrent positions). All present in the profile YAML above.


---

## Amendment 2026-05-24 -- D3 covered_call sizing denominator must include x100 multiplier

**Source**: `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` P0-3
**Reviewer**: DeepSeek-V4-Pro
**Status**: Adopted

### What changed

D3's "Income / Assignment-Risk" sizing block (this ADR, lines 126-130) explicitly states `CSP: collateral = strike x 100` (correct: equity options are 100 shares per contract). The `covered_call` case in the same paragraph references "current basis of long stock" without the `x100` multiplier on the per-contract basis. A literal implementation would size CC contracts against per-share basis and over-count contract sizing by 100x.

Corrected formula, by initiation context:

**Initiation case** (entering a NEW covered call where the long stock is purchased simultaneously or freshly allocated for this strategy):

```
collateral_per_contract = stock_basis_per_share x 100        # CC ties up 100 shares per contract
credit_yield = call_mid x 100 / collateral_per_contract       # premium received vs. capital tied up
target_contracts = floor((nav x kelly_fraction x max_position_pct_nav) / collateral_per_contract)
```

The `x 100` on BOTH numerator (premium credit is per-contract = `mid x 100`) and denominator (collateral is per-contract = `basis_per_share x 100`) means the `x 100` algebraically cancels in `credit_yield`, BUT NOT in `target_contracts` -- sizing must use per-contract collateral, not per-share basis.

**Wheel-overlay case** (overlaying a CC on stock already held from a prior CSP assignment or unrelated long position):

```
# Capital is ALREADY DEPLOYED in the underlying. New capital at risk = 0 for the stock leg.
# Sizing constraint shifts to "how many contracts can I write against held shares?"
max_contracts_by_held_shares = held_share_count // 100   # truly covered, no naked

# Premium yield denominator: the call premium is the only NEW cash flow; the stock basis
# is sunk capital. Express yield against call_mid x 100 (per-contract premium):
credit_yield_overlay = call_mid x 100 / (call_mid x 100)   # trivially 1.0
# What matters in the overlay case is the strike-to-basis ratio, which is the
# already-codified `min_strike_above_basis_pct` rule (kept from the original D3).

# Sizing rule:
target_contracts = min(max_contracts_by_held_shares, max_position_pct_nav-derived cap)
```

The two cases must be distinguished in implementation. The `_check_covered_call_hard_rules` function (referenced at line 308) takes a `composite_intent` flag (already present in D7 wheel logic, lines 226-230) and branches on it.

Unit tests:

- `test_cc_initiation_sizing_includes_x100`: fixture with `stock_basis_per_share=100.0`, `call_mid=2.50`, `nav=100_000`, `kelly_fraction=0.25`, `max_position_pct_nav=0.10`. Expected: `collateral_per_contract=10_000`, `target_contracts=floor((100_000 * 0.25 * 0.10) / 10_000) = 0`. Without x100 the bug would compute `target_contracts=floor(2500/100)=25` -- assert this WRONG value is NOT produced.
- `test_cc_initiation_sizing_correct_when_capital_allows`: fixture with `nav=1_000_000`, same other values; expected `target_contracts=floor(25_000/10_000)=2`.
- `test_cc_overlay_sizes_against_held_shares`: fixture with `held_share_count=300`, `composite_intent="wheel"`; expected `max_contracts_by_held_shares=3`. No NAV/Kelly cap applies because no new capital is deployed by the call leg; only the truly-covered constraint binds.
- `test_cc_per_share_basis_never_used_directly_in_target_contracts`: regression test that asserts the bug shape -- if the implementation is ever reverted to using per-share basis in `target_contracts`, this test fails.

### Why

Equity options are 100 shares per contract; this is a fundamental units fact. The synthesis (P0-3) flagged the original D3 wording as causing implementations following the ADR literally to over-count by 100x. The CSP case got it right (`strike x 100` is explicit at line 130); the CC case must do the same. Distinguishing initiation from wheel-overlay is necessary because the capital-at-risk calculus differs (initiation deploys new capital for the stock leg; wheel-overlay uses already-deployed capital). Without that distinction, a strict "always use per-contract basis" rule would under-allocate wheel rotations.

### Affected sections of this ADR

- D3 "Income / Assignment-Risk (Covered Calls, CSPs, Short Puts)" sizing block (lines 126-130) -- formula corrected and split into initiation vs. overlay.
- D7 wheel-state logic (lines 226-230) -- `composite_intent` flag now feeds into the sizing branch.
- Test plan "Per-strategy hard rules" (line 357) -- adds the four sizing tests above.

---

## Amendment 2026-05-24 -- D6 net-greeks aggregation must project stock-leg synthetic greeks

**Source**: `docs/reviews/2026-05-24-synthesis-adrs-0026-0030.md` P0-4
**Reviewer**: DeepSeek-V4-Pro
**Status**: Adopted

### What changed

D6's `aggregate_legs(legs: list[OptionLeg]) -> NetGreeks` (this ADR, line 190) iterates only `OptionLeg` objects. A `covered_call` proposal carries `(long stock, short call)`; the long-stock leg is NOT an `OptionLeg` and has no `greeks` field. The current spec's `sum(leg.greeks * leg.qty)` silently drops the stock leg's contribution. For a 100-share long-stock leg, the dropped delta is +100 -- a material mis-aggregation. The portfolio net-delta cap (`max_net_delta_pct_nav`, line 94) cannot be enforced correctly until this is fixed.

Specify an explicit "stock-leg projection" rule:

Stock legs contribute synthetic greeks per share:

```
stock_synthetic_greeks = NetGreeks(
    delta = 1.0,    # 1.0 per share long; -1.0 per share short
    gamma = 0.0,    # stock has no second-order price sensitivity
    theta = 0.0,    # stock has no time decay
    vega  = 0.0,    # stock has no IV sensitivity
    rho   = 0.0,    # stock rho is materially zero on the trade horizons we care about
)
```

Aggregation contract:

```python
def aggregate_legs(legs: Sequence[OptionLeg | StockLeg]) -> NetGreeks:
    """Aggregate per-leg greeks into portfolio-level net greeks.

    Stock legs project to synthetic greeks (delta=1.0/share, others=0)
    scaled by signed share quantity. Option legs use their per-contract
    greeks scaled by signed contract quantity x 100 (shares per contract).
    """
    net = NetGreeks.zero()
    for leg in legs:
        if isinstance(leg, OptionLeg):
            # per-contract greeks * 100 shares/contract * signed contract count
            net += leg.greeks * leg.qty * 100
        elif isinstance(leg, StockLeg):
            # delta=1.0/share * signed share count
            net += stock_synthetic_greeks * leg.qty
        else:
            raise TypeError(f"unsupported leg type: {type(leg)}")
    return net
```

`StockLeg` is added as a sibling to `OptionLeg` (defined in the implementation wave alongside the existing `OptionLeg` from ADR-0028). `MultiLegProposal.legs` becomes `tuple[OptionLeg | StockLeg, ...]`.

Alternative shape (rejected): adding an `underlying_stock_position: int` field to `MultiLegProposal` and aggregating it separately. Rejected because it bifurcates the leg list and requires every consumer to remember to include both -- a footgun. The `Union[OptionLeg, StockLeg]` shape with an `isinstance` dispatch is more uniform and harder to forget.

Unit tests:

- `test_aggregate_covered_call_includes_stock_delta`: fixture with `100 shares long stock + 1 short 30-delta call` (`call.delta = 0.30`). Expected `net.delta = 100 * 1.0 + (-1) * 0.30 * 100 = 100 - 30 = 70`. Without the fix, the buggy result is `-30` (only the short call leg contributes).
- `test_aggregate_csp_no_stock_leg_unchanged`: fixture with `1 short 30-delta put` only (no stock). Expected `net.delta = (-1) * (-0.30) * 100 = 30`. Asserts the stock-projection logic doesn't break the no-stock-leg case.
- `test_aggregate_short_stock_negative_delta`: fixture with `-100 shares short stock + 1 long 30-delta call`. Expected `net.delta = -100 + 30 = -70`. Asserts the sign convention on `StockLeg.qty`.
- `test_aggregate_unknown_leg_type_raises`: fixture with a duck-typed bogus leg; assert `TypeError`.
- `test_aggregate_zero_shares_no_contribution`: `StockLeg(qty=0)` contributes nothing; aggregation matches no-stock-leg case.

### Why

The synthesis (P0-4) caught this as a silent drop bug. The covered-call strategy is the highest-priority strategy in the user's mix (per D1 lines 16-17: "covered call -> cash-secured put -> wheel state machine"); the aggregator silently mis-counting it would mean the net-delta cap could be violated by 70%+ and the gate would not catch it. This fix is the minimum sufficient correction; the `StockLeg` projection rule is the smallest schema change that makes the aggregation total-and-correct over the leg union.

### Affected sections of this ADR

- D6 `aggregate_legs` signature and body (line 190) -- leg type widens to `Sequence[OptionLeg | StockLeg]`; stock-leg projection rule added.
- D6 invariant 2 "Net-delta cap holds across all leg-count combinations" (test plan, line 368) -- new fixtures must include covered-call (stock + option) cases, not just option-only mixes.
- Implementation map (line 305) -- `greeks.py` exports both `OptionLeg`-based aggregation and the new `StockLeg` projection helper.
