---
status: proposed
date: 2026-06-05
deciders: [codeseys]
---

# ADR-0088: Pluggable broker backends + stock-and-options across both reactors

## Context and Problem Statement

Two operator requirements converged:

1. **"Fix the paper reactor so it properly works for when we want to try to trade
   with other exchange configurations but don't have direct API access."** The
   home-grown `PaperReactor` (`react/paper.py`) is an append-only `executions.jsonl`
   writer with **no buying-power enforcement** and stored positions as cumulative
   **NAV-fractions** (the "0da3" unit class; partially fixed in PR #69 for the
   shares-carrying path). It is not a trustworthy simulator — it cannot be relied
   on to paper-track a strategy on an exchange we lack a live API for, because it
   never refuses an over-capital order and its cash accounting was wrong.

2. **"Both reactors must handle stock AND options plays; make sure the Alpaca paper
   reactor can handle options."** Today:
   - `AlpacaPaperReactor` (PR #69) is **equity-only**.
   - `MultiLegPaperReactor` (`react/multileg.py`) fills options, but only via the
     **deterministic local model** in `mleg_fill.py::PaperBroker`; its
     `_submit_live_paper()` (the Alpaca options submit/poll) is a deferred
     `NotImplementedError` stub. So options never reach the real broker even on the
     Alpaca path.

The underlying structural fact: the system already has the right *layering* —
`OptionLeg` / `StockLeg` / `LegFill` / `MlegFillResult` are broker-agnostic shapes,
the `Reactor` protocol is clean, and `PaperBroker` has a mode-selection seam
(`_live_paper_available()` → deterministic vs live-paper) — but the seam was never
filled in, and "which venue executes a fill" is decided by scattered env-flag
checks rather than a first-class abstraction.

## Decision

Introduce a **`BrokerBackend` abstraction**: a small protocol that owns
*account state, order submission, fill polling, and cancellation* for one venue.
Two implementations ship:

- **`DeterministicBackend`** — a correctness-complete *local simulator* (no
  network, no creds). It enforces **buying power** (rejects over-capital orders
  like a real broker), tracks **true units** (shares / contracts, never
  NAV-fractions), and models fills against last-known decision prices (+ the
  existing ADR-0070 slippage envelope). This is the fallback ANY exchange config
  routes to when there is no live API — the literal answer to requirement (1).
- **`AlpacaBackend`** — wraps the Alpaca paper `TradingClient` for equity AND
  options (`OrderClass.MLEG` + `OptionLegRequest`), reusing the auth pattern
  already in `admissibility/oracle.py`. This finishes the deferred
  `_submit_live_paper` path and is the literal answer to requirement (2).

The reactors become **thin orchestrators over a backend**:
- `PaperReactor` (equity) and `MultiLegPaperReactor` (options) keep their
  precondition chains (admissibility, gate-is-final, idempotency, slippage,
  two-row reconciliation) but delegate the *fill mechanics* to a selected backend.
- `select_reactor()` (dispatch) continues to route by `proposal_kind`
  (equity → equity reactor, multi_leg → multi-leg reactor); a NEW backend selector
  (`select_backend()`) picks the venue by config: Alpaca when its flag is on AND
  creds are present, else the deterministic simulator.

### Backend interface (shape)

```
class BrokerBackend(Protocol):
    name: str
    def account_equity(self) -> float | None        # NAV for sizing; None = unknown (fail-closed)
    def buying_power(self) -> float | None           # available BP; None = unknown
    def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id) -> FillResult
    def submit_option_single(self, leg, *, qty, limit_price, client_order_id) -> FillResult
    def submit_option_mleg(self, legs, *, outer_qty, net_limit_price, client_order_id) -> FillResult
    # poll/cancel folded into submit for the deterministic backend (already terminal);
    # the Alpaca backend polls to terminal + cancels-on-timeout (PR #69 P1-C semantics).
```

`FillResult` reuses the existing `LegFill` / `MlegFillResult` vocabulary so the
record-building + reconciliation code is backend-independent.

## Decision Drivers

- **Extensibility ("other exchange configurations").** Adding IBKR or a crypto
  venue later is a new `BrokerBackend` class + a `select_backend` case — no reactor
  surgery. The operator's phrasing is taken literally.
- **Correctness fallback.** The deterministic backend is a *trustworthy* simulator
  (BP-enforced, true-unit) so a strategy can be paper-tracked honestly on a venue
  we cannot hit live — not a fabricated append-log.
- **Options parity.** Both reactors execute options via whichever backend is
  selected; the Alpaca options submit/poll path is finally built.
- **Reuse, not rebuild.** `OptionLeg`/`LegFill`/`MlegFillResult`/`PaperBroker`
  mode-seam/`Reactor` protocol already exist; this ADR formalizes the seam they
  imply rather than inventing a parallel stack.
- **Reversible rollout (money-software).** Every new path ships DEFAULT-OFF behind
  a flag; the deterministic backend remains the default so flag-off behavior is
  bit-identical to today's (post-PR-#69) reactor.

## Rails (non-negotiable, inherited from ADR-0015/0029/0070/0077/0079)

- **Gate is final authority.** Backends fill *already-gated, already-approved*
  proposals; a backend never re-runs or bypasses the deterministic risk gate /
  options gate, and never widens a size.
- **Fail-closed.** Missing creds / unknown NAV / unknown BP / submit reject / poll
  error → a clear raise or a no-fill record; NEVER a fabricated fill. A
  buying-power rejection is a legitimate surfaced outcome.
- **True units.** Positions reconcile in real shares/contracts via
  `reactor_metadata["quantity"]`; cash uses real notional (PR #69 P1-A fix).
- **Idempotency.** `client_order_id` derived from the proposal id so retries
  collide at the venue instead of double-submitting (PR #69 P2-A).
- **Asymmetric slippage** stays (ADR-0070): equity legs get the v0.2 envelope,
  option legs pass through at NBBO mid on the deterministic backend.
- **Live (real-money) stays gated** behind `LiveTradingApproval` (ADR-0029 D7);
  this ADR is paper + simulator only.

## Consequences

- The portfolio-cap band-aid (`HERMES_QUANT_PORTFOLIO_CAPS`) becomes **redundant on
  the Alpaca backend** (broker enforces BP) and **enforced natively by the
  deterministic backend** (BP check), so it can be retired on both paths after a
  validation window — the cap was always re-implementing a broker function.
- One backend test surface instead of per-reactor fill logic; a future venue is an
  additive class.
- Short-term: the deterministic backend's BP enforcement will *reject* fires that
  the old append-log silently accepted — intended (that was the 880%-gross bug),
  but it changes paper-book behavior, so it ships default-OFF and is validated
  against a side-by-side tick log before the synthetic path adopts it.

## Flags

- `HERMES_QUANT_ALPACA_PAPER=1` — route equity (and now options) fills through the
  Alpaca backend (default-OFF; PR #69).
- `HERMES_QUANT_MULTILEG_REACTOR=1` — enable the multi-leg reactor (default-OFF;
  ADR-0029 D7).
- `HERMES_QUANT_BROKER_BACKEND` — optional explicit backend override
  (`deterministic` | `alpaca`); unset = auto-select (Alpaca if its flag+creds, else
  deterministic).
- `HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2` — asymmetric slippage (unchanged).

## Status

Proposed. Built incrementally behind the flags above; the deterministic backend's
correctness completion + the Alpaca options submit/poll are the two load-bearing
deliverables. Squash-merge of the PR-#69 Alpaca-equity work is gated on the
deterministic backend being a trustworthy generic fallback (operator requirement 1)
and the Alpaca options path existing (operator requirement 2).
