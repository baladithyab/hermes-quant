# ADR-0077: Pre-trade admissibility engine + ShortabilityOracle (paper→live fidelity foundation)

**Status:** Accepted (2026-05-30), implemented
**Date:** 2026-05-30
**Wave:** B (paper→live fidelity foundation — the six-model P0)
**Supersedes:** nothing
**Cites:** [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate, final authority), [ADR-0005](ADR-0005-data-layer.md) (alpaca-py provider), [ADR-0011](ADR-0011-portfolio-reconstruction-sign-convention.md) (short sign convention), [ADR-0049](ADR-0049-shadow-account-counterfactual.md) (shadow-account counterfactual replay), [ADR-0062](ADR-0062-rollout-playbook.md) (default-OFF rollout playbook), [ADR-0070](ADR-0070-paper-execution-fidelity.md) (paper-execution slippage fidelity — sibling fidelity ADR), [ADR-0071](ADR-0071-portfolio-aware-dynamic-kelly.md) (exposure caps / sizing)
**Grounded in:** `docs/research/2026-05-30-r-admissibility-shortability.md`; six-model critique summary in `docs/research/2026-05-30-understanding-wiki.md` §3.B.

---

## Context

### The synthetic-short fidelity lie

The paper book currently holds **38 synthetic SHORT equity positions that would be untradeable live.** A 6-model cross-family critique (`six-model-critique-2026-05-28.md`, summarized in understanding-wiki §3.B) **unanimously (6/6) flagged this as the true P0** — ahead of the multi-leg options rail that the operator's stated vision prioritizes — because shipping options execution now would "stack a bigger lie on the unfixed short-book lie."

Concretely, the simulator books P&L on shorts that a real Alpaca account would either **reject at submission** or **auto-cancel overnight**:

- Alpaca **only opens short positions in easy-to-borrow (ETB) securities** (`docs.alpaca.markets/us/docs/margin-and-short-selling`). Many of the 38 names are mid/small-caps that are almost certainly HTB/NTB or non-marginable — i.e. Alpaca would never have opened them.
- An order in a name that flips **ETB→HTB overnight is automatically cancelled before market open**. A held short that goes HTB is not force-closed, but accrues a daily borrow fee Alpaca does **not** expose via API.
- Alpaca **does not support fractional shorts**: a fractional sell that would open/increase a short is rejected with **HTTP 422** (`fractional orders are not allowed to short`). Our discrete `±0.05…±0.20 × NAV` ladder converts to a share count that must be `floor()`-ed to whole shares for shorts — today it is not.
- Margin/short requires **≥ \$2,000 account equity**; below that there is no short capability at all.
- Reg-T initial short margin is **150% of market value** (100% covered by proceeds + 50% initial margin; Cornell LII 12 CFR §220.12(c), FINRA 4210). The sim does not debit this buying power.
- **Paper does not charge Borrow Fees** — Alpaca's own paper-vs-live feature table lists *"Borrow Fees: ⛔️ (Coming Soon!)"* for paper. So even an *admissible* short accrues zero carry in paper; live shorts pay an APR daily on notional.

The compounding harm is behavioral, not just accounting: the post-trade **reflector is learning a reflexive-shorting habit the broker would refuse**. The +5.96% paper return is partly fiction sourced from positions that could not exist live. This directly violates the operator's fidelity north-star: *"we want paper trading to be as accurate as live env so we capture issues during paper trading rather than in live."*

### Two distinct holes

The research isolates two fidelity holes the same oracle closes:

1. **Admissibility** — the 38 shorts likely violate Alpaca's ETB-only / marginable / whole-share / BP rules; live would reject or auto-cancel them.
2. **Carry** — even admissible shorts accrue **zero borrow fee** in paper, so short P&L is fictitiously free.

### Posture constraint (the rails)

Per AGENTS.md and ADR-0004, the **deterministic risk gate is the FINAL authority**; everything else is evidence that can only *silence* (multiply toward 0.0), never amplify or override. An admissibility check is a **hard, deterministic precondition** that sits *upstream* of the risk gate. It can only **REJECT** a proposed order (or flatten an inadmissible held short → 0.0); it can **never force a trade** and never relaxes a gate decision. It is admissibility, not authority.

---

## Decision drivers

- **D-1 Fidelity north-star.** Paper must be as honest as live so bugs surface in paper. An untradeable-short book is the single largest current breach.
- **D-2 Silence-by-default / fail-closed.** When shortability is unknown or data is missing, the safe answer is REJECT, never "assume admissible." The current de-facto `NullShortableProvider` (everything admissible) is exactly the bug.
- **D-3 Authority boundary.** Must not touch or weaken ADR-0004. Filter can only subtract (REJECT), never add.
- **D-4 Default-OFF, eval-gated, reversible.** Build behind a `HERMES_QUANT_*` flag; with the flag OFF, behavior is preserved bit-for-bit so in-flight reflectors/calibrators see no regime change until the operator audits a side-by-side and flips one line. The flip is the operator's call.
- **D-5 `asof` honesty.** Shortability and CBR are point-in-time. Backtest admissibility must use the value as of decision time, not today's `easy_to_borrow`. Alpaca only exposes *current* ETB, so historical admissibility needs a recorded snapshot / static allowlist path — and the limitation must be documented, not faked.
- **D-6 Don't fake precision we can't get.** Alpaca exposes shortability as a **boolean**, not a borrowable-share count, and does not publish HTB rates. The honest model is binary at the admissibility layer (ETB-admissible / else-reject) plus a coarse borrow-APR accrual — not a phantom precise fee.
- **D-7 Reuse a proven contract.** QuantConnect Lean's `IShortableProvider` is the canonical reference design; adopt its shape rather than inventing one.

---

## Considered options

### Option A — Full pre-trade admissibility engine, default-OFF, gating proposals (CHOSEN)

A `hermes_quant/admissibility/` package with a `ShortabilityOracle` that classifies each proposed order **ACCEPTED / PARTIAL / REJECTED** before the ADR-0004 risk gate sees it, behind `HERMES_QUANT_ADMISSIBILITY=1` (default OFF). Carry accrual lives behind a separate `HERMES_QUANT_BORROW_COST=1` flag so the gate and the P&L carry flip independently.

- **Pros:** Closes the real bug at the right seam (pre-trade, upstream of the gate). Mirrors live broker behavior — the same predicate Alpaca applies. Reusable across paper, shadow, backtest, and (eventually) live reactors. Default-OFF + eval-gated satisfies the rails. Adopting Lean's tri-state contract gives a battle-tested interface.
- **Cons:** Most engineering effort of the three. Introduces a new gate surface that must be carefully constrained to "REJECT-only" so it never becomes an amplifier. Historical/backtest admissibility is limited by the lack of a point-in-time ETB feed (mitigated by the static-allowlist path + documented limitation).

### Option B — Offline-only borrow-aware P&L restatement, no live/pre-trade gate

Write a one-shot analysis job that re-runs the 38-short book under live constraints (ETB check + borrow accrual + dividend-on-short) and **restates** the +5.96%, producing a per-symbol live-valid-qty / fee / accept-reject report. No runtime gate; nothing changes in the decision path.

- **Pros:** Cheapest; zero runtime risk; immediately quantifies how much of the return is fiction; no new authority surface; directly answers "how big is the lie."
- **Cons:** Does **not stop new untradeable shorts from being booked** tomorrow — the reflector keeps learning the broker-refused habit. It's a measurement, not a fix. It would have to be re-run forever and would still let the daemon propose inadmissible orders. Solves the *carry* hole's accounting but leaves the *admissibility* hole open going forward.

### Option C — Do nothing / annotate only

Leave the decision path untouched; add a documentation note and a metadata flag on shorts marking them "possibly untradeable live."

- **Pros:** No code risk; trivial.
- **Cons:** Leaves the 6/6 P0 unaddressed. Paper P&L stays fictional; the reflector keeps reinforcing reflexive shorting; the fidelity north-star stays violated. Blocks the agreed sequencing (fidelity foundation must precede the options rail). Rejected.

**Decision:** Option A, with Option B's offline restatement folded in as the **first rollout phase** (the eval-gate / promotion artifact). Option B alone is insufficient because it does not prevent future inadmissible proposals; Option A's offline replay subsumes B's value while also closing the forward-looking hole.

---

## Decision

Create a new package **`hermes_quant/admissibility/`**, gated **DEFAULT-OFF** behind `HERMES_QUANT_ADMISSIBILITY=1`, that classifies each proposed order **ACCEPTED / PARTIAL / REJECTED** via a `ShortabilityOracle`. It is **deterministic, fail-closed, silence-by-default**, sits **upstream of the ADR-0004 risk gate as a hard precondition**, and **can only REJECT (or flatten an inadmissible held short → 0.0) — never force, amplify, or override a trade.**

### D77.1 The `ShortabilityOracle` contract (Lean `IShortableProvider` shape)

```python
# hermes_quant/admissibility/shortability.py   (gated by HERMES_QUANT_ADMISSIBILITY)

class AdmissibilityState(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL  = "PARTIAL"
    REJECTED = "REJECTED"

@dataclass(frozen=True)
class ShortabilityVerdict:
    state: AdmissibilityState
    reason: str | None        # typed: "NOT_SHORTABLE" | "NOT_ETB" | "FRACTIONAL_SHORT"
                              # | "NOT_MARGINABLE" | "INSUFFICIENT_BPR" | "EQUITY_BELOW_2K"
                              # | "PTP_BLOCKED" | "SSR_MARKETABLE_SHORT" | None
    annual_cbr: float         # cost-to-borrow APR for carry accrual (0.0 for longs)

class ShortabilityOracle(Protocol):
    def verdict(self, symbol: str, side: str, qty: int, asof: datetime,
                ctx: AdmissibilityContext) -> ShortabilityVerdict: ...
```

Three implementations, mirroring Lean's provider tiers:

- **`AlpacaShortabilityOracle`** — live source of truth. Calls `TradingClient.get_asset(symbol)` (ADR-0005 provider). Admissible-short predicate (the honest live predicate from the research):
  ```python
  admissible_short = (asset.tradable and asset.marginable and asset.shortable
                      and asset.easy_to_borrow and qty == floor(qty))
  ```
  `annual_cbr` defaults to a low fixed ETB rate (~0.30% APR) when `easy_to_borrow=True`; not-ETB ⇒ REJECT (we do not invent an HTB rate Alpaca won't give us).
- **`StaticETBAllowlistOracle`** — offline/backtest. A point-in-time ETB set + per-name CBR table keyed by `asof`, so historical admissibility uses the value as of decision time (D-5). Honest about the limitation where no snapshot exists.
- **`NullShortabilityOracle`** — today's behavior (everything admissible) == the bug. Selected **only when the flag is OFF**, preserving current outputs bit-for-bit.

### D77.2 Admissibility states (deterministic, in roughly Alpaca's evaluation order)

Exact Alpaca fields and rules from the research (`docs/research/2026-05-30-r-admissibility-shortability.md` §1, §2, §4):

**REJECTED if any:**
- Not shortable for an opening short: `asset.shortable == False` **OR** `asset.easy_to_borrow == False` → `NOT_SHORTABLE` / `NOT_ETB`.
- Not marginable (`asset.marginable == False`) or account equity < \$2,000 → `NOT_MARGINABLE` / `EQUITY_BELOW_2K`.
- **Fractional short** (non-integer short qty) → `FRACTIONAL_SHORT` (live: HTTP 422).
- **Insufficient buying power.** Alpaca prices an opening short at `MAX(limit_price, 1.03 × current_ask) × qty` against available BP; open orders also consume BP until filled. Reg-T initial requirement is `1.50 × short_market_value`. → `INSUFFICIENT_BPR`.
- `attributes` contains `ptp_no_exception` (blocked by default), or `ipo` attribute with a non-limit order → `PTP_BLOCKED`.

**PARTIAL if:**
- Marketable but liquidity-constrained (Alpaca paper random ~10% partial-fill then re-evaluates the remainder).
- **SSR active** (Reg SHO Rule 201: intraday low ≤ `prev_close × 0.90`, latched rest-of-day + entire next trading day) **+ marketable short** → only the above-NBB slice can fill ⇒ deferred/partial. We have no NBB tick data, so the conservative posture is to flag SSR and treat marketable shorts as not-immediately-fillable. → `SSR_MARKETABLE_SHORT`.

**ACCEPTED:** all hard gates pass AND (for shorts) `easy_to_borrow=True` AND whole-share AND BP sufficient.

> Per-asset margin is read from `margin_requirement_short` (string %, equities only); `maintenance_margin_requirement` is deprecated in favor of it. `attributes` is the enum list (`ptp_no_exception`, `ptp_with_exception`, `ipo`, `has_options`, `overnight_tradable`, `overnight_halted`, …). **No `shortable_shares` quantity field exists** — Alpaca's shortability is a boolean, so the oracle is boolean at the admissibility layer.

### D77.3 Borrow-aware carry (separate flag `HERMES_QUANT_BORROW_COST=1`)

A daily accrual on short notional, so short P&L is no longer fictitiously free (closes the carry hole that paper does not charge):

```
daily_borrow_fee = abs(short_shares) * close_price * annual_cbr / 360   # /360 stock-loan basis
                                                                        # Friday accrues ×3 (weekend)
pil = abs(short_shares) * cash_dividend_per_share   # payment-in-lieu, debited pay date if short across ex-div
```

ETB ⇒ ~0.30% APR default; not-ETB ⇒ rejected upstream (so no fake HTB rate). The two flags are independent so the operator can flip the admissibility gate and the P&L carry separately.

### D77.4 Wiring (rails-compliant)

- The oracle is invoked **upstream of the ADR-0004 risk gate** as a hard precondition (it runs before sizing). On REJECTED it emits no order; on an inadmissible *held* short it flattens via a 0.0 multiplier. It **never** calls a path that increases size and **never** overrides a gate REJECT.
- With both flags OFF, `NullShortabilityOracle` is selected and behavior is identical to today.
- The verdict (`state`, `reason`, `annual_cbr`) is written to the audit trail / `executions.jsonl` metadata so "did admissibility fire and why?" is queryable (same observability discipline as the BMA-discriminator backfill).

---

## Consequences

**Positive:**
- The largest fidelity breach is closed: untradeable shorts are rejected pre-trade exactly as live would; the reflector stops learning a broker-refused shorting habit.
- Short P&L stops being fictitiously free (borrow carry + dividend-on-short).
- An honest restatement of the +5.96% becomes possible and repeatable.
- Unblocks the agreed sequencing: the fidelity foundation lands before the multi-leg options rail, so options don't stack on the short-book lie.
- Reusable contract (Lean's `IShortableProvider` shape) across paper, shadow, backtest, and future live reactors.

**Negative / risks (real downsides):**
- **Regime shift on flag-flip.** Reflectors/calibrators/Sharpe estimates consuming `executions.jsonl` see a discontinuity the day the flag flips (fewer/no shorts, lower short P&L). Mitigated by default-OFF + a side-by-side burn-in before promotion, but it is a real break in the time series.
- **Most of the current short book may vanish.** If the bulk of the 38 are NOT_ETB, the gross/net exposure and apparent return drop materially. That is honest, but it removes a chunk of the existing track record and may make the book look thin.
- **Historical/backtest admissibility is approximate.** Alpaca exposes only *current* `easy_to_borrow`; the static-allowlist path is a best-effort point-in-time reconstruction. Backtests over names without a recorded snapshot carry documented uncertainty — we must not present them as ground truth.
- **CBR is coarse.** ETB ⇒ a single ~0.30% default; we deliberately refuse HTB names rather than fake a precise rate. This under-models the rare case of a name that is genuinely borrowable at a modest HTB fee, biasing slightly toward rejection (acceptable under fail-closed).
- **New gate surface to police.** The REJECT-only constraint must be enforced by tests forever; a future careless change could let it influence sizing upward — which would violate ADR-0004. Property tests must assert it can only subtract.
- **No live NBB data for SSR.** SSR handling is conservative-but-blunt (flag + treat marketable shorts as non-fillable); it may over-reject during an SSR window.
- **Live latency cost.** `AlpacaShortabilityOracle.verdict` adds a `get_asset` call per proposed short on the live path (cacheable, but a network dependency that must fail closed on error).

**Out of scope (future amendments):**
- Real HTB borrow rates (Alpaca does not expose them via API).
- A point-in-time historical ETB feed (would need recorded daily snapshots).
- Options-leg admissibility (covered by the multi-leg options ADR once that lands).
- Tightening ADR-0004 cost-gate parameters from realized borrow carry (a calibration follow-up).

---

## Rollout

1. **Default-OFF construction.** Ship `hermes_quant/admissibility/` with `NullShortabilityOracle` selected when `HERMES_QUANT_ADMISSIBILITY` is unset. No decision-path behavior changes. Property tests assert the oracle can only REJECT/flatten, never amplify (ADR-0004 boundary), and that flag-OFF preserves outputs bit-for-bit.
2. **Offline restatement of the 38-short book (the eval-gate / promotion artifact).** Replay the existing 38 synthetic shorts through `AlpacaShortabilityOracle` + `StaticETBAllowlistOracle` and the borrow accrual. Expected outcome: the bulk flag `NOT_ETB` / `NOT_SHORTABLE`, and the borrow carry measurably degrades the fictitious short P&L. Produce a per-symbol report (live-valid qty / fee / accept-reject) and a restated return. This side-by-side tick log is the promotion artifact (same pattern as ADR-0070/0071 promotion).
3. **Operator audit.** The operator reviews the restatement. Arming is a separate, explicit human decision — never bundled with the build.
4. **Flip on the cron wrapper.** Set `HERMES_QUANT_ADMISSIBILITY=1` (and, independently, `HERMES_QUANT_BORROW_COST=1`) in the cron wrapper — one reversible line — so new proposals are gated and short carry accrues. After one clean trading day of side-by-side validation, promote to default.

---

## Verification

```python
from hermes_quant.admissibility.shortability import (
    AdmissibilityState, AlpacaShortabilityOracle, NullShortabilityOracle,
)

# Fail-closed: a non-ETB name is REJECTED for an opening short.
v = oracle.verdict("SMALLCAP", side="short", qty=100, asof=t, ctx=ctx_not_etb)
assert v.state is AdmissibilityState.REJECTED and v.reason == "NOT_ETB"

# Fractional short is rejected (mirrors live HTTP 422).
v = oracle.verdict("AAPL", side="short", qty=10.5, asof=t, ctx=ctx_etb)  # type: ignore[arg-type]
assert v.state is AdmissibilityState.REJECTED and v.reason == "FRACTIONAL_SHORT"

# ETB whole-share short with sufficient BP is ACCEPTED with a low CBR.
v = oracle.verdict("AAPL", side="short", qty=100, asof=t, ctx=ctx_etb_bp_ok)
assert v.state is AdmissibilityState.ACCEPTED and 0.0 < v.annual_cbr < 0.02

# Authority boundary: the oracle can only subtract. No verdict ever increases size.
# (property test) for any verdict, resulting target_pct is <= the pre-admissibility target.

# Flag OFF == today's behavior, bit-for-bit.
null = NullShortabilityOracle()
assert null.verdict("ANY", "short", 100, t, ctx).state is AdmissibilityState.ACCEPTED
```

```bash
# Offline restatement probe (rollout phase 2): how much of the short book is real?
~/.hermes/hermes-agent/venv/bin/python3 ops/scripts/quant-admissibility-restate.py \
  --book ~/.hermes/quant/state.db --asof-snapshot etb_2026-05-30.json
# Expect: a per-symbol accept/reject table; count(REJECTED NOT_ETB) on the 38 shorts;
#         restated return after borrow carry. This is the operator-audit artifact.
```
