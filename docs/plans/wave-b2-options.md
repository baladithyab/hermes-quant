# Wave B2 — Options foundation (data layer + options-aware gate + reactor scaffold)

**Date:** 2026-05-30
**Author:** plan subagent (deep-work backlog loop, Wave B2)
**Status:** implementation-ready
**Posture rails (NON-NEGOTIABLE):** paper-only; silence-by-default; the deterministic
risk gate is FINAL authority (LLM/committee is evidence, never authority — can silence
via 0.0 multiplier, never amplify/override); discrete sizing `{0, ±0.05, ±0.10, ±0.15,
±0.20}` of NAV stays unchanged; every new capability ships **DEFAULT-OFF** behind a
`HERMES_QUANT_*` env flag, eval-gated before any live influence; all times UTC; `asof =
publication/decision time`; no look-ahead.

**Grounded in:**
- `docs/research/2026-05-30-r-options-execution.md` (the refreshed Alpaca/gate research; supersedes R1 on the corrected points).
- The existing 6-PR plan `~/.hermes/plans/2026-05-28_multi-leg-options-implementation.md`. **NOTE: that file is not present on this filesystem** (`/root/.hermes/plans` does not exist; no copy under `/mnt/e`). This plan therefore reconciles against the 6-PR plan **as summarized in the research note's §5 reconciliation table** (Phase A data layer Tasks A1–A4 → Phase B gate → Phase C reactor → Phase D observation) plus the eleven corrections R1–R11. If the actual 6-PR file resurfaces, cross-check task IDs against §"Reconciliation with the 6-PR plan" below.
- ADRs: `docs/adr/ADR-0027-options-aware-risk-gate.md`, `ADR-0028-options-data-layer.md`, `ADR-0029-multi-leg-paper-reactor.md` (read with **all amendments** — the 2026-05-24 amendments are authoritative over the ADR bodies and over R1).
- Already-vendored optlib pricing kernel at `hermes_quant/options/pricing/` (exposed via `hermes_quant/options/greeks.py`: `european_greeks`, `american_greeks`, `implied_vol`, `OptionGreeks`).
- Existing shadow PMCC tracker `hermes_quant/shadow/pmcc.py` (the counterfactual harness that activates implicitly once the reactor lands — do NOT modify it in this wave).

---

## 0. Scope of THIS loop (and what is explicitly OUT)

This wave builds the **options foundation** as three default-OFF, NOT-live-wired pieces.
None of them can fire an order. The point is **safe construction behind flags**; the
flip is the operator's call after the fidelity foundation (Wave B) + the 60-day paper
evidence window (ADR-0029 D7) land.

**IN scope (this loop):**

1. **Options data layer** — `OptionLeg` / `NetGreeks` / `StockLeg` dataclasses, OCC-21
   parse/format, a **read-only** chain-snapshot reader (replay-from-disk + a thin live
   adapter that stays inert unless credentials + a flag are present).
   → `hermes_quant/options/data.py`, `hermes_quant/options/occ.py`.
2. **Options-aware risk gate** — three-bucket **collateral-secured** classifier (NOT
   defined-risk-only — that bug rejects every CC/CSP), net-greeks aggregation, BPR /
   max-loss / net-delta / net-vega / net-gamma caps. Behind
   `HERMES_QUANT_OPTIONS_GATE=1`. **Can only REJECT** (returns `None`/silence or a
   reason); it never sizes up, never authorizes, never amplifies.
   → `hermes_quant/risk/options_gate.py`.
3. **Multi-leg reactor SCAFFOLD** — a class whose interface matches `PaperReactor`
   (`name`, `requires_credentials`, `execute(...)`) but which **raises / no-ops** unless
   `HERMES_QUANT_MULTILEG_REACTOR=1` (which **stays OFF** in this wave and is not set
   anywhere). It writes nothing to `executions.jsonl` while disabled.
   → `hermes_quant/react/multileg.py`.

**OUT of scope (deferred; do NOT build here):**

- Live broker `submit_mleg_order` HTTP path / `LiveBroker` / `LiveTradingApproval`
  (ADR-0029 D7 amendment — gated by a future promotion ADR; type-level absent).
- Autonomous-mode multi-leg (ADR-0016).
- Historical option-chain backtest / `PolygonOptionsProvider` / `SyntheticChainProvider`
  (ADR-0028 D4 — no historical chains in any Alpaca tier; **still true**, R10). Paper loop
  is live-snapshot-only.
- Wiring the gate or reactor into `advisor.py` / `autonomous.py` / the proposal flow.
  This wave does **not** route any real proposal through either. (B01 — the reactor
  go-live — is a *later* wave; this is its B02+B03 foundation.)
- `StrategyBuilder` recipe emitters, `reconcile_options_ntas` (next-day NTA loop),
  `MultiLegProposal` HITL rendering. Stubs only where the scaffold's interface demands
  a type; full bodies are deferred.
- `py_vollib` dependency. **R6: superseded by vendored optlib.** The `[options]` extra in
  `pyproject.toml` is intentionally empty (line 63); this wave keeps it empty. Greek
  completion uses `hermes_quant/options/greeks.py` (optlib) only.

---

## 1. File-by-file specification

### 1.1 `hermes_quant/options/occ.py` (NEW) — OCC-21 format/parse

Authoritative per ADR-0029 D1 + research §1.2. **`Decimal` strikes** (float `145.005 *
1000` rounds wrong). Pure module — no I/O, no network, no global state.

```python
"""hermes_quant.options.occ — OCC-21 symbol format/parse (ADR-0029 D1).

OCC-21: ROOT(<=6, left-justified, space-padded on the wire but we emit/accept
the compact form) + YYMMDD + {C|P} + STRIKE*1000 zero-padded to 8 digits.

Example: NVDA260526C00145000 == NVDA 2026-05-26 $145.00 Call.

Pure module: no I/O, no network, no global state. Safe on the gate hot path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal


class OccParseError(ValueError):
    """Raised when a string is not a well-formed OCC-21 symbol."""


@dataclass(frozen=True)
class OccComponents:
    underlying: str                 # uppercased root, no padding
    expiry: date
    right: Literal["C", "P"]
    strike: Decimal                 # exact, e.g. Decimal("145.00")


def format_occ(
    underlying: str,
    expiry: date,
    right: Literal["C", "P"],
    strike: Decimal,
) -> str:
    """Build an OCC-21 symbol. Strike *1000 zero-padded to 8 digits.

    Raises:
        OccParseError: empty/too-long root (>6), non-C/P right,
            non-positive or non-representable strike (strike*1000 must be a
            non-negative integer < 1e8), expiry on a weekend (Alpaca only
            lists Mon-Fri expiries; reject early per ADR-0029 test plan #1).
    """


def parse_occ(symbol: str) -> OccComponents:
    """Inverse of format_occ. Raises OccParseError on malformed input.

    Accepts both the compact form (no internal spaces) and the
    space-padded 21-char wire form (root left-justified to 6).
    """
```

Implementation notes:
- `right` is `"C"`/`"P"` (ADR-0029 convention). Note ADR-0028 D1's `OptionContract.type`
  uses `"call"`/`"put"` — keep these two namespaces distinct; `data.py` owns the
  conversion (`_RIGHT_TO_TYPE = {"C": "call", "P": "put"}`).
- Strike round-trip: `strike_int = int((strike * 1000).to_integral_value())`; reconstruct
  as `Decimal(strike_int) / 1000` and normalize. Add a guard that
  `Decimal(strike_int) / 1000 == strike` exactly, else `OccParseError` (rejects
  un-representable strikes like a half-cent).
- Validate root `1 <= len <= 6`, alnum, uppercased.

### 1.2 `hermes_quant/options/data.py` (NEW) — dataclasses + read-only chain reader

Carries the hot-path dataclasses (frozen, per ADR-0028 D1 rationale: Pydantic overhead at
tick cadence is unacceptable) and a **read-only** chain-snapshot reader. Dependency-light:
stdlib + `hermes_quant.options.greeks` (optlib) + optional `pyarrow` (already a core dep,
line 34 of pyproject) for parquet replay. **No alpaca-py import at module top level** — the
live adapter imports it lazily inside the method, so the module imports cleanly without the
`[alpaca]` extra.

#### Dataclasses (canonical for this wave — both ADR-0028 D1 and ADR-0029 D5 amendments reconciled)

```python
@dataclass(frozen=True)
class NetGreeks:
    """Net (signed, dollarized-on-demand) greeks for a structure.

    Convention (ADR-0027 D6 + amendment 2026-05-24): each field is the sum over
    legs of sign * per-unit-greek * units, where sign = +1 long / -1 short and
    units = ratio_qty * order_qty * 100 (option) or signed share count (stock).
    delta/gamma/theta/vega are stored as the aggregated per-$1-move numbers;
    callers multiply by spot (delta/gamma) or 1pt (vega) when applying caps.
    """
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0   # per-day
    vega: float = 0.0
    rho: float = 0.0

    @classmethod
    def zero(cls) -> "NetGreeks": ...
    def __add__(self, other: "NetGreeks") -> "NetGreeks": ...   # vector add (gate uses portfolio.net + candidate.net)


@dataclass(frozen=True)
class OptionGreeksSnapshot:
    """Per-contract greeks at a point in time. Nullable for incremental
    completion (ADR-0028 D1). iv_source tags provenance."""
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    iv: float | None = None
    iv_source: Literal["provider", "computed", "stale_provider",
                       "py_vollib_european_approximation"] | None = None
    # NOTE name kept for ADR-0028 D3 string-compat ("py_vollib_*") even though
    # we synthesize via optlib not py_vollib; the tag means "European BSM approx".


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg proposal/position.

    RECONCILES ADR-0028 D1 (contract-based) WITH ADR-0029 D5 amendment
    (position_intent + ratio_qty). The amendment is authoritative on the
    order-shape fields; ADR-0028 contributes the greeks-at-decision slot.
    """
    symbol: str                          # OCC-21 (the single source of identity)
    side: Literal["buy", "sell"]
    position_intent: Literal["buy_to_open", "buy_to_close",
                             "sell_to_open", "sell_to_close"]
    ratio_qty: int = 1                   # leg multiplier within the spread
    greeks_at_decision: OptionGreeksSnapshot | None = None
    fill_price: float | None = None      # filled by reactor (None at proposal time)

    @property
    def right(self) -> Literal["C", "P"]: ...     # via parse_occ(self.symbol)
    @property
    def strike(self) -> Decimal: ...
    @property
    def expiry(self) -> date: ...
    @property
    def underlying(self) -> str: ...


@dataclass(frozen=True)
class StockLeg:
    """Stock leg of a covered structure (covered call, collar).

    Per ADR-0027 D6 amendment 2026-05-24: stock projects to synthetic greeks
    (delta=1.0/share, gamma=theta=vega=rho=0), scaled by signed share qty.
    Required so the net-delta cap is enforced correctly on covered calls
    (the highest-priority strategy)."""
    underlying: str
    qty: int                             # signed: +long shares, -short shares
    basis_per_share: float | None = None # for collateral/sizing; None at proposal time
```

`OptionGreeksSnapshot` is deliberately named differently from the optlib
`hermes_quant.options.greeks.OptionGreeks` (which is non-nullable and price-bearing) to
avoid a name clash; `data.py` does NOT re-export `OptionGreeks`.

#### Snapshot / chain containers (read-only)

```python
@dataclass(frozen=True)
class OptionSnapshot:
    symbol: str
    asof: datetime                       # UTC; the as_of the snapshot is valid at
    fetched_at: datetime                 # UTC wall-clock when provider returned it (ADR-0028 D7 amendment)
    bid: float | None
    ask: float | None
    last: float | None
    volume: int | None
    open_interest: int | None            # R8: from /v2/options/contracts, NOT the snapshot greeks payload
    greeks: OptionGreeksSnapshot
    underlying_spot: float
    risk_free_rate: float

    @property
    def mid(self) -> float | None: ...
    @property
    def dte(self) -> int: ...             # (expiry - asof.date()).days


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    asof: datetime
    underlying_spot: float
    risk_free_rate: float
    snapshots: tuple[OptionSnapshot, ...]

    def find(self, expiry: date, strike: Decimal, right: Literal["C", "P"]) -> OptionSnapshot | None: ...
    def by_dte(self, dte_min: int, dte_max: int) -> "OptionChain": ...
```

#### Read-only chain reader

```python
class ChainSnapshotReader:
    """READ-ONLY options-chain reader. Two modes:

      1. replay_chain(underlying, asof) — reads from parquet on disk
         (~/.hermes/quant/option_chains/<u>/<YYYY-MM-DD>.parquet, ADR-0028 D7).
         Enforces fetched_at <= asof at load (ADR-0028 D5 amendment: drops
         look-ahead rows, counts drops). This is the DEFAULT path and needs no
         credentials, no network, no flag.

      2. fetch_chain_live(underlying) — thin Alpaca read-only adapter. INERT
         unless HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 AND credentials present;
         otherwise raises LiveChainDisabled. Joins the chain greeks endpoint
         (R5: OptionsChainRequest / GET /v1beta1/options/snapshots/{u}) with
         /v2/options/contracts for open_interest (R8). Greek completion via
         optlib (R6) for the ~41% no-greeks tier. NEVER writes orders; NEVER
         called by anything in this wave.
    """
    def __init__(self, chains_dir: Path | None = None) -> None: ...

    def replay_chain(self, underlying: str, asof: datetime) -> OptionChain: ...
    def fetch_chain_live(self, underlying: str) -> OptionChain: ...   # inert by default
```

Errors raised by `data.py`: `ChainQualityError` (<2 valid contracts after liquidity
filter, per ADR-0028 boundary rule), `GreekComputationError` (mid<=0 / dte<=0 / spot<=0 —
fail closed, never zero-greeks, ADR-0028 D3), `LiveChainDisabled` (live path hit without
flag+creds), `DataIntegrityError` (writer-side fetched_at>asof; only reachable if a writer
lands later — define the exception now for the reader's belt-and-suspenders filter).

Helper functions (module-level, pure):

```python
def aggregate_net_greeks(legs: Sequence[OptionLeg | StockLeg]) -> NetGreeks:
    """Aggregate per-leg greeks into NetGreeks (ADR-0027 D6 + both amendments).

      - OptionLeg: sign(side) * per-contract-greek * (ratio_qty * 100).
        sign = +1 buy / -1 sell. greeks_at_decision MUST be non-None for every
        option leg; raise GreekComputationError if any is None (fail-closed —
        the gate refuses to evaluate missing greeks, ADR-0027 D6).
      - StockLeg: delta += 1.0 * qty (signed); gamma/theta/vega/rho contribute 0.
      - Unknown leg type: TypeError.
    """
```

### 1.3 `hermes_quant/risk/options_gate.py` (NEW) — collateral-secured gate, REJECT-only

Behind `HERMES_QUANT_OPTIONS_GATE=1`. The gate **only ever returns silence (None) or a
pass-through `OptionsGateResult` with a rejection reason** — it has no path that increases
size, authorizes, or overrides anything. It extends ADR-0004's sequence with O1–O7; it does
**not** replace `gate.py` (which stays the equity/crypto authority).

```python
"""hermes_quant.risk.options_gate — options-aware deterministic risk gate (ADR-0027).

EXTENDS ADR-0004's rule sequence with O1-O7. COLLATERAL-SECURED, not
defined-risk-only (research §2 / Gemini catch: a strict max_gain-is-None reject
rejects every CC and CSP — the exact strategies the effort exists to enable).

Three admissible buckets; everything else is rejected-as-naked:
  - covered_call:     admit iff held_shares[underlying] >= 100 * contracts
  - cash_secured_put: admit iff options_buying_power >= strike*100*c - premium
  - defined_risk:     admit iff max_loss finite AND <= caps (vertical/condor/fly)

DEFAULT-OFF behind HERMES_QUANT_OPTIONS_GATE=1. When the flag is absent the
public entrypoint raises OptionsGateDisabled (this wave never sets it live).
The gate can ONLY reject (silence) or pass-through; it never amplifies, never
sizes up, never overrides the equity gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class OptionsGateDisabled(RuntimeError):
    """Raised when options_gate() is called without HERMES_QUANT_OPTIONS_GATE=1."""


class StructureBucket(str, Enum):
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    DEFINED_RISK = "defined_risk"
    NAKED = "naked"            # the ONLY reject-as-naked terminal bucket


@dataclass(frozen=True)
class OptionsGateResult:
    """REJECT-only result. `admitted=False` => silence with `reason`.
    `admitted=True` carries the deterministic sizing the caller MAY use
    (contract count), but the gate never *raises* a size — contracts is a
    floor() of a discrete-step NAV target, identical posture to gate.py."""
    admitted: bool
    bucket: StructureBucket
    reason: str | None              # populated iff not admitted
    net_greeks: "NetGreeks"
    bpr_estimate: float             # buying-power reduction, USD
    max_loss: float | None          # USD; None only for covered (share-collateralized)
    contracts: int                  # deterministic floor() sizing; 0 => silence
    warnings: tuple[str, ...] = ()  # soft, non-silencing (ADR-0027 D4)


def options_gate(
    legs,                  # Sequence[OptionLeg | StockLeg]
    *,
    strategy_kind: str,    # 'covered_call'|'cash_secured_put'|'vertical_spread'|...
    underlying: str,
    spot: float,
    nav: float,
    held_shares: int,                 # current long shares of `underlying`
    options_buying_power: float,
    premium_received: float,          # net credit collected (>=0); 0 for debit
    portfolio_net_greeks,             # NetGreeks; existing options book
    total_bpr: float,                 # existing BPR across options book
    cfg: "OptionsRiskConfig",
    composite_intent: str | None = None,   # 'wheel' budgets collateral ONCE (ADR-0027 D7)
) -> OptionsGateResult:
    """Run the O1-O7 sequence. Returns silence (admitted=False) on any
    violation. Rules in order: O-classify -> O1 max-loss/margin -> O2 no-naked
    -> O3 gamma -> O4 theta -> O5 vega -> O6 BPR buffer -> O7 pin-risk ->
    sizing -> min-contract guard.

    Raises OptionsGateDisabled unless HERMES_QUANT_OPTIONS_GATE=1.
    """
    if os.environ.get("HERMES_QUANT_OPTIONS_GATE", "0") != "1":
        raise OptionsGateDisabled(...)
    ...
```

Internal predicates (pure, fixture-tested, no LLM):

```python
def _classify_structure(legs, *, held_shares, options_buying_power,
                        premium_received, strike, contracts) -> StructureBucket: ...
    # three-bucket classifier per research §2.1; the ONLY NAKED case is a short
    # leg with neither >=100 covering shares/contract, NOR strike*100 cash,
    # NOR a wider long leg.

def _max_loss(bucket, legs, *, width, net_credit, net_debit, contracts) -> float | None: ...
    # debit vertical: net_debit*100*c ; credit vertical/condor/fly:
    # (width - net_credit)*100*c ; covered_call: None (share-collateralized)

def _bpr(bucket, *, strike, contracts, premium_received, width, net_credit) -> float: ...
    # CSP: strike*100*c - premium_received ; CC: 0 incremental ;
    # credit vertical/condor: (width - net_credit)*100*c ; long: premium_paid

def _size_contracts(bucket, *, target_nav, max_loss_per_contract,
                    collateral_per_contract, held_shares, composite_intent) -> int: ...
    # ADR-0027 D3 + amendment: CC initiation uses basis_per_share*100 denominator;
    # CC overlay (composite_intent='wheel') sizes against held_shares//100.
    # All integer floor(); residual recorded by caller as sizing_residual_pct.
```

`OptionsRiskConfig` — a frozen dataclass mirroring the `options_default` profile (ADR-0027
D2). Envelope keys (`max_position_pct`, `max_drawdown_pct`, `max_daily_loss_pct`,
`bpr_kill_switch_pct`) are NOT overridable below their floor — same as gate.py. Defaults
exactly as ADR-0027 D2 lists them (`max_net_delta_pct_nav=0.50`, `gamma_cap_pct_nav=0.05`,
`vega_cap_pct_nav=0.10`, `theta_budget_pct_nav_per_day=0.02`, `bpr_buffer_pct_nav=0.80`,
`min_dte_for_new_entry=7`, `pin_risk_dte_threshold=3`, `pin_risk_moneyness_threshold=0.02`,
`max_short_call_delta_per_position=0.30`, etc.).

**Reconcile-with-real-broker note (research §5 OQ):** gate on our own §2.2 formula and
fail-closed if ours says reject; record `options_buying_power` for drift but never let the
broker number *loosen* our decision.

This wave wires NOTHING into the proposal flow. `options_gate` is callable only from tests.

### 1.4 `hermes_quant/react/multileg.py` (NEW) — reactor SCAFFOLD, default-OFF

Interface matches `PaperReactor` (`hermes_quant/react/base.py` `Reactor` Protocol:
`name: str`, `requires_credentials: bool`, `execute(proposal, *, fill_size_pct,
approver_user_id=None) -> ExecutionRecord`). The scaffold **raises / no-ops** unless
`HERMES_QUANT_MULTILEG_REACTOR=1` — which **stays OFF** this wave and is set nowhere.

```python
"""hermes_quant.react.multileg — multi-leg paper reactor SCAFFOLD (ADR-0029).

DEFAULT-OFF. Until HERMES_QUANT_MULTILEG_REACTOR=1 (which is NOT set anywhere in
this wave), every execute() call raises MultiLegReactorDisabled and NOTHING is
written to executions.jsonl. This is the B01 foundation; the go-live wave flips
the flag after the Wave B fidelity foundation + ADR-0029 D7's 60-day paper
evidence window.

The class mirrors PaperReactor's Reactor-Protocol surface so it drops into the
react dispatch without touching proposals/store. It does NOT touch a live
broker: any code path toward a live mleg order raises LiveMultiLegNotAuthorized
(ADR-0029 D7) — but no such path exists in this scaffold.
"""
from __future__ import annotations

import os
from typing import Any

from .base import ExecutionRecord, Reactor


class MultiLegReactorDisabled(RuntimeError):
    """Raised by execute() when HERMES_QUANT_MULTILEG_REACTOR != 1."""


class LiveMultiLegNotAuthorized(RuntimeError):
    """Hard refusal: live multi-leg is gated behind a future promotion ADR
    (ADR-0029 D7). Not a config flag."""


class MultiLegPaperReactor:
    """Paper-only multi-leg reactor scaffold. Interface-compatible with
    PaperReactor; inert unless HERMES_QUANT_MULTILEG_REACTOR=1."""

    name = "multileg-paper"
    requires_credentials = False

    def __init__(self, executions_path=None) -> None:
        # Mirror PaperReactor.__init__ (default EXECUTION_BUS_PATH, mkdir, touch)
        # but DO NOT open/write anything until enabled.
        ...

    @staticmethod
    def _enabled() -> bool:
        return os.environ.get("HERMES_QUANT_MULTILEG_REACTOR", "0") == "1"

    def execute(
        self,
        proposal: Any,                 # MultiLegProposal once it lands; Any for now
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
    ) -> ExecutionRecord:
        """Default-OFF: raises MultiLegReactorDisabled. When enabled (NOT this
        wave), would: aggregate net greeks, build the mleg leg array
        (position_intent + ratio_qty per leg, outer qty/type/limit_price per
        ADR-0029 D2 amendment — research §1.3 shape), write ONE atomic
        ExecutionRecord per proposal to executions.jsonl (paper fill_price =
        net debit/credit). NEVER calls a live broker."""
        if not self._enabled():
            raise MultiLegReactorDisabled(
                "multi-leg reactor is default-OFF; set "
                "HERMES_QUANT_MULTILEG_REACTOR=1 to enable (gated by ADR-0029 D7)"
            )
        raise NotImplementedError(
            "multi-leg execution body is deferred to the B01 go-live wave"
        )
```

`isinstance(MultiLegPaperReactor(), Reactor)` must be `True` (Protocol conformance test).
Do NOT add it to `hermes_quant/react/__init__.py`'s `__all__` yet (keeps it un-dispatched;
import path is `from hermes_quant.react.multileg import MultiLegPaperReactor`).

---

## 2. Test files

All deterministic, no network, no live API (AGENTS.md testing discipline). Place under
`tests/unit/`. Integration probes (live Alpaca) go under `tests/integration/` and are
skipped by default.

### 2.1 `tests/unit/test_options_occ.py` (NEW)
- `parse_occ(format_occ(...))` identity over ≥30 fuzz cases incl. strike `0.50`, `9999.999`,
  `145.00`, `145.005`-rejected (un-representable → `OccParseError`).
- `format_occ("NVDA", date(2026,5,26), "C", Decimal("145.00")) == "NVDA260526C00145000"`
  (the research §1.2 golden).
- Weekend expiry → `OccParseError`. Root >6 chars → `OccParseError`. Lowercase root
  normalized to upper. Malformed length → `OccParseError`.

### 2.2 `tests/unit/test_options_data.py` (NEW)
- `aggregate_net_greeks` golden cases from ADR-0027 amendments:
  - covered call (100 long shares + 1 short 0.30Δ call) → `net.delta == 70`
    (`test_aggregate_covered_call_includes_stock_delta`).
  - CSP only (1 short 0.30Δ put) → `net.delta == 30`.
  - short 100 shares + 1 long 0.30Δ call → `net.delta == -70`.
  - debit call vertical (buy 0.30Δ / sell 0.18Δ same expiry) → `net.delta == +12`
    (research §2.3 worked example: 0.30−0.18 = 0.12, ×100 = 12).
  - unknown leg type → `TypeError`; `StockLeg(qty=0)` contributes nothing.
  - any `OptionLeg` with `greeks_at_decision is None` → `GreekComputationError` (fail-closed).
- `OptionLeg.right/strike/expiry/underlying` derive correctly via `parse_occ`.
- `replay_chain` drops look-ahead rows (`fetched_at > asof`) and returns the rest
  (ADR-0028 D5 amendment) — build a small in-memory/temp-parquet fixture; assert dropped
  count.
- `replay_chain` raises `ChainQualityError` when <2 valid contracts remain.
- `fetch_chain_live` raises `LiveChainDisabled` when the flag/creds are absent (the default).
- `GreekComputationError` on `mid<=0`/`dte<=0`/`spot<=0` in the greek-completion path
  (never returns zero-greeks).

### 2.3 `tests/unit/test_options_gate.py` (NEW)
- `options_gate(...)` raises `OptionsGateDisabled` without `HERMES_QUANT_OPTIONS_GATE=1`
  (use `monkeypatch.setenv`/`delenv`; the disabled path is the most-tested per silence rail).
- **Three-bucket classifier** (the load-bearing fix):
  - CC with `held_shares >= 100*contracts` → admitted, bucket COVERED_CALL.
  - CC with `held_shares < 100*contracts` → silence, bucket NAKED, reason mentions covering shares.
  - CSP with `options_buying_power >= strike*100*c - premium` → admitted, bucket CASH_SECURED_PUT.
  - CSP with insufficient BP → silence, bucket NAKED.
  - debit/credit vertical with finite `max_loss <= caps` → admitted, bucket DEFINED_RISK.
  - lone short call, no cover/cash/wider-long → silence, bucket NAKED (the ONLY naked reject).
- **Caps** (each independently silences): net-delta cap (`|net_delta*spot| > 0.50*NAV`),
  net-vega cap, gamma cap, theta budget (silence only for theta-*burning* entry; theta-collecting
  CC/CSP passes O4), BPR buffer (`total_bpr + new_bpr > 0.80*NAV`), pin-risk
  (`min_dte<=3 AND |moneyness|<=0.02`).
- **Sizing** (ADR-0027 D3 + amendment): `test_cc_initiation_sizing_includes_x100`
  (basis 100, mid 2.50, nav 100k, kelly 0.25, max_pos 0.10 → collateral_per_contract 10_000 →
  contracts 0; assert the buggy `25` is NOT produced); `nav 1M → contracts 2`; CC overlay
  (`composite_intent='wheel'`, `held_shares=300` → `max_contracts_by_held_shares=3`).
- **REJECT-only invariant:** a property/parametrized test asserting `options_gate` never
  returns `contracts` such that `max_loss/nav > max_position_pct + eps`, and never returns
  `admitted=True` with `contracts > floor(target_nav / collateral)` (it cannot size up).
- **Wheel composite (ADR-0027 D7):** CSP proposed on a name with an open CC →
  `bucket`/`reason` reflect single-collateral budgeting; a third leg → silence.

### 2.4 `tests/unit/test_multileg_reactor_scaffold.py` (NEW)
- `MultiLegPaperReactor().execute(...)` raises `MultiLegReactorDisabled` when the flag is
  unset (default), and writes NOTHING to a temp executions path (assert file empty / unchanged).
- With `HERMES_QUANT_MULTILEG_REACTOR=1` set, `execute(...)` raises `NotImplementedError`
  (body deferred) — proving the flag gate is the *first* check and the live body is absent.
- `isinstance(MultiLegPaperReactor(), Reactor) is True` (Protocol conformance with
  `react.base.Reactor`).
- `name == "multileg-paper"`, `requires_credentials is False`.
- It is NOT in `react.__all__` (regression guard that it stays un-dispatched this wave).

### 2.5 `tests/integration/test_options_chain_live.py` (NEW, skipped by default)
- Marked `requires_network`; runs only with `--run-integration`. `fetch_chain_live("NVDA")`
  with `HERMES_QUANT_OPTIONS_LIVE_CHAIN=1` + paper creds returns ≥20 contracts, all greeks
  complete post-completion, sample symbol round-trips through `parse_occ`. This is the only
  place the calendar-spread sandbox probe (research §5 R7 / ADR-0029 OQ1) is recorded; it is
  observational and never runs in CI.

---

## 3. Reconciliation with the existing 6-PR plan

The 6-PR plan (per research §5: Phase A data → Phase B gate → Phase C reactor → Phase D
observation) maps onto this wave as follows. **This wave covers the foundation of PRs 1–3a,
default-OFF; it does NOT cover the go-live PRs (3b–6).**

| 6-PR plan element | Covered HERE? | This-wave file(s) | Notes / corrections applied |
|---|---|---|---|
| **PR-1 / Phase A (Tasks A1–A4): options data layer** — OCC format/parse, `OptionLeg`/`NetGreeks` dataclasses, chain-snapshot reader | **YES (foundation)** | `occ.py`, `data.py` | A1 OCC + Decimal-strike + fuzz test **unchanged/correct** (research confirms). **R3 fix applied:** `OptionLeg.position_intent` uses `buy_to_open/buy_to_close/sell_to_open/sell_to_close`, NOT `open`/`close`. **R8 fix:** `open_interest` sourced from `/v2/options/contracts`, not the snapshot greeks payload. **R9:** rho IS present (no rho-synthesis-always). |
| **PR-2 / Phase B: options-aware risk gate** — net-greeks + collateral checks | **YES** | `options_gate.py`, `data.py::aggregate_net_greeks` | **R4 fix applied:** collateral-secured three-bucket classifier (CC=share-check, CSP=options_BP-check, defined-risk=max-loss), NOT defined-risk-only. **R6 fix:** greek completion via vendored optlib, NOT py_vollib. Stock-leg projection + x100 sizing per ADR-0027 amendments. **REJECT-only** posture enforced by test. |
| **PR-3a / Phase C: multi-leg order request shape + reactor** | **SCAFFOLD ONLY** | `react/multileg.py` | **R1 fix carried into the scaffold's deferred body docstring:** outer `qty`/`type`/`limit_price`; per-leg `position_intent` + `ratio_qty`; NO equity leg inside an mleg `legs[]` array. **R5 fix:** CC/CSP = single-leg L1 path, NOT an mleg order with an equity leg; only ≥2-option-leg spreads use mleg. The scaffold raises until `HERMES_QUANT_MULTILEG_REACTOR=1`. |
| **PR-3b: live `submit_mleg_order` + order-lifecycle polling** | **NO (deferred)** | — | ADR-0029 D7: live gated by `LiveTradingApproval` + future promotion ADR; type-level absent. |
| **PR-4: `reconcile_options_ntas` (next-day NTA loop)** | **NO (deferred)** | — | Settlement-loop step; needs live fills first. |
| **PR-5: `StrategyBuilder` recipe emitters + HITL `MultiLegProposal` rendering** | **NO (deferred)** | — | Needs the gate + reactor wired into the proposal flow (B01 go-live). |
| **PR-6: kill-switches (gamma-spike / BPR / theta-bleed / pin-risk) in settlement_loop** | **NO (deferred)** | — | ADR-0027 D5; fires from settlement_loop on live positions. Pin-risk *new-entry* silence is in this wave's gate (O7); the *position-flatten* kill-switch is deferred. |

**Backlog mapping:** this wave delivers **B02** (ADR-0028 options data layer) and **B03**
(ADR-0027 options-aware risk gate) in default-OFF foundation form, plus the **B01** reactor
scaffold. B01 go-live (flipping `HERMES_QUANT_MULTILEG_REACTOR=1`) remains a later wave,
correctly blocked on the Wave B fidelity foundation and the 60-day evidence window
(research §5 sequencing note: do not "stack a bigger lie on the unfixed short-book lie").

---

## 4. Implementation order (within this wave)

1. `occ.py` + `test_options_occ.py` (no deps; everything else uses it). Run, green.
2. `data.py` dataclasses + `aggregate_net_greeks` + `test_options_data.py` aggregation
   cases. Then the `ChainSnapshotReader.replay_chain` + look-ahead-drop + `ChainQualityError`
   tests. (Live adapter stays inert; `LiveChainDisabled` test only.)
3. `options_gate.py` + `test_options_gate.py` — classifier first, then caps, then sizing,
   then the REJECT-only property test and the disabled-path test.
4. `react/multileg.py` + `test_multileg_reactor_scaffold.py` — flag-gate + Protocol
   conformance.
5. `tests/integration/test_options_chain_live.py` — skipped-by-default stub (no CI impact).
6. Update `hermes_quant/options/__init__.py` to export `OptionLeg`, `StockLeg`, `NetGreeks`,
   `OptionSnapshot`, `OptionChain`, `ChainSnapshotReader`, `aggregate_net_greeks`, and the
   `occ` helpers (`format_occ`, `parse_occ`, `OccComponents`, `OccParseError`). Do NOT export
   the gate or reactor from package `__init__` (keep them import-by-path, un-wired).

## 5. Verification gate (before claiming done — superpowers:verification-before-completion)

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/unit/test_options_occ.py \
  tests/unit/test_options_data.py tests/unit/test_options_gate.py \
  tests/unit/test_multileg_reactor_scaffold.py -q
~/.hermes/hermes-agent/venv/bin/python3 -m ruff check hermes_quant/options/ \
  hermes_quant/risk/options_gate.py hermes_quant/react/multileg.py tests/unit/test_options_*.py
~/.hermes/hermes-agent/venv/bin/python3 -m mypy hermes_quant/options/ \
  hermes_quant/risk/options_gate.py hermes_quant/react/multileg.py
# Default-OFF proof: with NO HERMES_QUANT_OPTIONS_GATE / _MULTILEG_REACTOR set,
# the gate raises OptionsGateDisabled and the reactor raises MultiLegReactorDisabled.
# Smoke: importing hermes_quant.options must NOT require the [alpaca] extra
# (live adapter import is lazy).
~/.hermes/hermes-agent/venv/bin/python3 -c "import hermes_quant.options.data, hermes_quant.options.occ, hermes_quant.risk.options_gate, hermes_quant.react.multileg; print('import-clean')"
```

Acceptance: all four unit files green; ruff+mypy clean; the import-clean smoke passes
without optional extras; grep confirms `HERMES_QUANT_MULTILEG_REACTOR` is set NOWHERE in the
repo or deploy `.env` (the flip is the operator's deliberate later act).

## 6. Rails self-check (every box must be true at merge)

- [ ] Gate `options_gate` can ONLY reject/silence — no code path increases size, authorizes,
      or overrides `gate.py`. (REJECT-only property test green.)
- [ ] All three pieces default-OFF behind `HERMES_QUANT_OPTIONS_GATE` /
      `HERMES_QUANT_OPTIONS_LIVE_CHAIN` / `HERMES_QUANT_MULTILEG_REACTOR`; none set live.
- [ ] Nothing in this wave routes a real proposal through the gate or reactor; no
      `executions.jsonl` write while the reactor flag is off.
- [ ] Discrete sizing ladder untouched; contract count is `floor()` of a discrete-step NAV
      target (no widening, no fractional contracts).
- [ ] No new runtime dependency (`[options]` extra stays empty; optlib + stdlib + pyarrow only;
      alpaca-py import is lazy and inert).
- [ ] All times UTC; `OptionSnapshot.asof`/`fetched_at` UTC; replay enforces
      `fetched_at <= asof` (no look-ahead).
- [ ] No live multi-leg path exists (`LiveMultiLegNotAuthorized` defined but unreachable;
      `submit_mleg_order` is NOT implemented anywhere this wave).
