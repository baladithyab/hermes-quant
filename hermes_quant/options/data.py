"""hermes_quant.options.data — options hot-path dataclasses + read-only chain reader.

Carries the frozen hot-path dataclasses (ADR-0028 D1 rationale: Pydantic
overhead at tick cadence is unacceptable) and a READ-ONLY chain-snapshot
reader (ADR-0028 D5/D7 amendments). Reconciles ADR-0028 D1 (contract-based)
with ADR-0029 D5 amendment (position_intent + ratio_qty).

Dependency-light: stdlib + ``hermes_quant.options.greeks`` (vendored optlib,
R6) + optional ``pyarrow`` (a core dep) for parquet replay. **No alpaca-py
import at module top level** — the live adapter imports it lazily inside the
method so the package imports cleanly without the ``[alpaca]`` extra.

Pure helpers (``aggregate_net_greeks``) and the replay path need no network,
no credentials, and no flag. The live adapter (``fetch_chain_live``) stays
inert unless ``HERMES_QUANT_OPTIONS_LIVE_CHAIN=1`` AND credentials are present.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .occ import parse_occ

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .occ import OccComponents

# ADR-0028 D1: OCC right ("C"/"P") <-> contract type ("call"/"put"). occ.py owns
# the "C"/"P" namespace; data.py owns the conversion to the "call"/"put"
# namespace used by ADR-0028 D1 OptionContract.type.
_RIGHT_TO_TYPE: dict[str, str] = {"C": "call", "P": "put"}

# US equity option contract multiplier (shares per contract).
_CONTRACT_MULTIPLIER = 100


# ---------------------------------------------------------------------------
# Errors (ADR-0028 boundary rules)
# ---------------------------------------------------------------------------


class ChainQualityError(ValueError):
    """Raised when <2 valid contracts remain after the liquidity filter."""


class GreekComputationError(ValueError):
    """Raised when greeks cannot be computed honestly (mid<=0 / dte<=0 / spot<=0,
    or a leg consumed by aggregation has greeks_at_decision is None). Fail-closed:
    never return zero-greeks."""


class LiveChainDisabled(RuntimeError):  # noqa: N818 — plan/ADR-0028-mandated name
    """Raised when the live chain path is hit without the flag + credentials."""


class DataIntegrityError(ValueError):
    """Writer-side invariant breach (fetched_at > asof). Defined now for the
    reader's belt-and-suspenders filter; only reachable once a writer lands."""


# ---------------------------------------------------------------------------
# Greeks containers
# ---------------------------------------------------------------------------


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
    theta: float = 0.0  # per-day
    vega: float = 0.0
    rho: float = 0.0

    @classmethod
    def zero(cls) -> NetGreeks:
        return cls(0.0, 0.0, 0.0, 0.0, 0.0)

    def __add__(self, other: NetGreeks) -> NetGreeks:
        """Vector add (gate uses portfolio.net + candidate.net)."""
        if not isinstance(other, NetGreeks):
            return NotImplemented
        return NetGreeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
            rho=self.rho + other.rho,
        )


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
    iv_source: (
        Literal[
            "provider",
            "computed",
            "stale_provider",
            "py_vollib_european_approximation",
        ]
        | None
    ) = None
    # NOTE name kept for ADR-0028 D3 string-compat ("py_vollib_*") even though
    # we synthesize via optlib not py_vollib; the tag means "European BSM approx".


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg proposal/position.

    RECONCILES ADR-0028 D1 (contract-based) WITH ADR-0029 D5 amendment
    (position_intent + ratio_qty). The amendment is authoritative on the
    order-shape fields; ADR-0028 contributes the greeks-at-decision slot.
    """

    symbol: str  # OCC-21 (the single source of identity)
    side: Literal["buy", "sell"]
    position_intent: Literal[
        "buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"
    ]
    ratio_qty: int = 1  # leg multiplier within the spread
    greeks_at_decision: OptionGreeksSnapshot | None = None
    fill_price: float | None = None  # filled by reactor (None at proposal time)

    def _occ(self) -> OccComponents:
        return parse_occ(self.symbol)

    @property
    def right(self) -> Literal["C", "P"]:
        return self._occ().right

    @property
    def strike(self) -> Decimal:
        return self._occ().strike

    @property
    def expiry(self) -> date:
        return self._occ().expiry

    @property
    def underlying(self) -> str:
        return self._occ().underlying


@dataclass(frozen=True)
class StockLeg:
    """Stock leg of a covered structure (covered call, collar).

    Per ADR-0027 D6 amendment 2026-05-24: stock projects to synthetic greeks
    (delta=1.0/share, gamma=theta=vega=rho=0), scaled by signed share qty.
    Required so the net-delta cap is enforced correctly on covered calls
    (the highest-priority strategy)."""

    underlying: str
    qty: int  # signed: +long shares, -short shares
    basis_per_share: float | None = None  # for collateral/sizing; None at proposal time


# ---------------------------------------------------------------------------
# Snapshot / chain containers (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionSnapshot:
    symbol: str
    asof: datetime  # UTC; the as_of the snapshot is valid at
    fetched_at: datetime  # UTC wall-clock when provider returned it (ADR-0028 D7)
    bid: float | None
    ask: float | None
    last: float | None
    volume: int | None
    open_interest: int | None  # R8: from /v2/options/contracts, NOT snapshot greeks
    greeks: OptionGreeksSnapshot
    underlying_spot: float
    risk_free_rate: float

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def dte(self) -> int:
        return (parse_occ(self.symbol).expiry - self.asof.date()).days


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    asof: datetime
    underlying_spot: float
    risk_free_rate: float
    snapshots: tuple[OptionSnapshot, ...]

    def find(
        self, expiry: date, strike: Decimal, right: Literal["C", "P"]
    ) -> OptionSnapshot | None:
        for snap in self.snapshots:
            c = parse_occ(snap.symbol)
            if c.expiry == expiry and c.strike == strike and c.right == right:
                return snap
        return None

    def by_dte(self, dte_min: int, dte_max: int) -> OptionChain:
        kept = tuple(s for s in self.snapshots if dte_min <= s.dte <= dte_max)
        return OptionChain(
            underlying=self.underlying,
            asof=self.asof,
            underlying_spot=self.underlying_spot,
            risk_free_rate=self.risk_free_rate,
            snapshots=kept,
        )


# ---------------------------------------------------------------------------
# Net-greeks aggregation (pure)
# ---------------------------------------------------------------------------


def aggregate_net_greeks(
    legs: Sequence[OptionLeg | StockLeg], *, order_qty: int = 1
) -> NetGreeks:
    """Aggregate per-leg greeks into NetGreeks (ADR-0027 D6 + both amendments).

      - OptionLeg: sign(side) * per-contract-greek * (ratio_qty * order_qty * 100).
        sign = +1 buy / -1 sell. ``order_qty`` is the number of times the whole
        structure (the ratio set) is ordered; the real position greeks are
        per-lot greeks * order_qty (ADR-0027 D6 amendment). Scaling by 1 lot
        would let a multi-contract order slip past per-lot caps while the real
        order breaches them. greeks_at_decision MUST be non-None for every
        option leg; raise GreekComputationError if any is None (fail-closed —
        the gate refuses to evaluate missing greeks, ADR-0027 D6).
      - StockLeg: delta += 1.0 * qty (signed); gamma/theta/vega/rho contribute 0.
        Stock qty is an absolute share count, NOT scaled by order_qty.
      - Unknown leg type: TypeError.
    """
    net = NetGreeks.zero()
    qty = max(int(order_qty), 1)
    for leg in legs:
        if isinstance(leg, OptionLeg):
            g = leg.greeks_at_decision
            if g is None:
                raise GreekComputationError(
                    f"option leg {leg.symbol} has no greeks_at_decision; "
                    "the gate refuses to evaluate missing greeks (fail-closed)"
                )
            if g.delta is None or g.gamma is None or g.theta is None or g.vega is None:
                raise GreekComputationError(
                    f"option leg {leg.symbol} has incomplete greeks; fail-closed"
                )
            # cr02: a non-finite greek (NaN from an IV-overflow / ATM-DTE=0 GBS
            # edge case, or inf) must fail closed exactly like a missing greek.
            # The None-only check let NaN through into the net, where it slipped
            # past every `NaN > cap` comparison downstream (NaN-fail-open).
            if not (
                math.isfinite(g.delta)
                and math.isfinite(g.gamma)
                and math.isfinite(g.theta)
                and math.isfinite(g.vega)
            ):
                raise GreekComputationError(
                    f"option leg {leg.symbol} has non-finite greeks; fail-closed"
                )
            sgn = 1 if leg.side == "buy" else -1
            units = sgn * leg.ratio_qty * qty * _CONTRACT_MULTIPLIER
            net = net + NetGreeks(
                delta=g.delta * units,
                gamma=g.gamma * units,
                theta=g.theta * units,
                vega=g.vega * units,
                rho=(g.rho or 0.0) * units,
            )
        elif isinstance(leg, StockLeg):
            # Stock projects to synthetic greeks: delta=1.0/share, others=0.
            net = net + NetGreeks(delta=1.0 * leg.qty)
        else:
            raise TypeError(f"unsupported leg type: {type(leg)!r}")
    return net


# ---------------------------------------------------------------------------
# Read-only chain reader
# ---------------------------------------------------------------------------

_DEFAULT_CHAINS_DIR = Path.home() / ".hermes" / "quant" / "option_chains"


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

    def __init__(self, chains_dir: Path | None = None) -> None:
        self.chains_dir = chains_dir or _DEFAULT_CHAINS_DIR
        self.last_lookahead_drops = 0  # surfaced via quant_doctor (ADR-0028 D5)

    def _path_for(self, underlying: str, day: date) -> Path:
        return self.chains_dir / underlying.upper() / f"{day:%Y-%m-%d}.parquet"

    def replay_chain(self, underlying: str, asof: datetime) -> OptionChain:
        """Read a recorded chain snapshot from parquet for `underlying` at `asof`.

        Drops any row where fetched_at > asof (ADR-0028 D5 amendment: the
        load-time enforcement of the no-look-ahead invariant on stored greeks).
        Raises ChainQualityError if <2 valid contracts remain (ADR-0028 boundary
        rule). Dropped count is recorded on ``self.last_lookahead_drops``.
        """
        import pyarrow.parquet as pq  # core dep; lazy to keep imports light

        path = self._path_for(underlying, asof.date())
        if not path.exists():
            raise ChainQualityError(f"no recorded chain at {path}")
        df = pq.read_table(path).to_pandas()

        pre_count = len(df)
        # Hard filter: contracts/greeks visible at `asof` only (belt + suspenders).
        df = df[df["fetched_at"] <= asof]
        df = df[df["asof"] <= asof]
        dropped = pre_count - len(df)
        self.last_lookahead_drops = int(dropped)

        snapshots = tuple(
            self._row_to_snapshot(row, asof=asof) for _, row in df.iterrows()
        )
        if len(snapshots) < 2:
            raise ChainQualityError(
                f"<2 valid contracts for {underlying} at {asof} after look-ahead "
                f"filter (got {len(snapshots)}, dropped {dropped})"
            )

        spot = float(df["underlying_spot"].iloc[0]) if len(df) else 0.0
        rfr = float(df["risk_free_rate"].iloc[0]) if len(df) else 0.0
        return OptionChain(
            underlying=underlying.upper(),
            asof=asof,
            underlying_spot=spot,
            risk_free_rate=rfr,
            snapshots=snapshots,
        )

    @staticmethod
    def _row_to_snapshot(row: object, *, asof: datetime) -> OptionSnapshot:
        get = row.get  # type: ignore[attr-defined]

        def _opt_float(key: str) -> float | None:
            v = get(key)
            return None if v is None or (isinstance(v, float) and v != v) else float(v)

        def _opt_int(key: str) -> int | None:
            v = get(key)
            return None if v is None or (isinstance(v, float) and v != v) else int(v)

        greeks = OptionGreeksSnapshot(
            delta=_opt_float("delta"),
            gamma=_opt_float("gamma"),
            theta=_opt_float("theta"),
            vega=_opt_float("vega"),
            rho=_opt_float("rho"),
            iv=_opt_float("iv"),
            iv_source=get("iv_source"),
        )
        return OptionSnapshot(
            symbol=str(get("contract_symbol")),
            asof=asof,
            fetched_at=get("fetched_at"),
            bid=_opt_float("bid"),
            ask=_opt_float("ask"),
            last=_opt_float("last"),
            volume=_opt_int("volume"),
            open_interest=_opt_int("open_interest"),
            greeks=greeks,
            underlying_spot=float(get("underlying_spot")),
            risk_free_rate=float(get("risk_free_rate")),
        )

    def fetch_chain_live(self, underlying: str) -> OptionChain:
        """Thin Alpaca read-only chain adapter — INERT by default.

        Raises LiveChainDisabled unless HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 AND
        Alpaca credentials are present in the environment. Nothing in this wave
        calls this method; the alpaca-py import is lazy so importing this module
        never requires the [alpaca] extra.
        """
        if os.environ.get("HERMES_QUANT_OPTIONS_LIVE_CHAIN", "0") != "1":
            raise LiveChainDisabled(
                "live chain fetch is default-OFF; set "
                "HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 (and provide Alpaca "
                "credentials) to enable"
            )
        key = os.environ.get("APCA_API_KEY_ID")
        secret = os.environ.get("APCA_API_SECRET_KEY")
        if not key or not secret:
            raise LiveChainDisabled(
                "live chain fetch requires APCA_API_KEY_ID / APCA_API_SECRET_KEY"
            )
        # Lazy import: keeps the module importable without the [alpaca] extra.
        try:
            from alpaca.data.historical.option import (  # noqa: F401
                OptionHistoricalDataClient,
            )
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LiveChainDisabled(
                "alpaca-py is not installed; `pip install hermes-quant[alpaca]`"
            ) from exc
        # The full live join (chain greeks endpoint + /v2/options/contracts for
        # open_interest + optlib greek completion for the no-greeks tier) is
        # deferred to the go-live wave. This scaffold path is never exercised in
        # this wave.
        raise NotImplementedError(  # pragma: no cover - deferred to go-live wave
            "live chain fetch body is deferred to the options go-live wave"
        )


def _complete_greeks_or_fail(
    *, mid: float, dte_days: int, spot: float
) -> None:
    """Fail-closed validator for the greek-completion path (ADR-0028 D3).

    Raises GreekComputationError on mid<=0 / dte<=0 / spot<=0. Never returns
    zero-greeks. Exposed for the data-layer tests; the full optlib completion
    lives behind the (deferred) live path.
    """
    if mid <= 0:
        raise GreekComputationError(f"mid must be > 0 for greek completion, got {mid}")
    if dte_days <= 0:
        raise GreekComputationError(f"dte must be > 0 for greek completion, got {dte_days}")
    if spot <= 0:
        raise GreekComputationError(f"spot must be > 0 for greek completion, got {spot}")
