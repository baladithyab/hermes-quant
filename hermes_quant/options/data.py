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
from datetime import UTC, date, datetime
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

# Risk-free rate stamped on a live snapshot. The replay path reads rfr back from
# the parquet; the live writer needs ONE value to record (ADR-0028 D3 greek
# completion uses it). Conservative US short-rate constant; matches the greeks
# module docstring example (0.05 = 5%). No live yield-curve lookup in this wave.
_DEFAULT_RISK_FREE_RATE = 0.05

# US equities carry a (small, near-flat) dividend yield; we do not source a live
# per-name yield in this wave, so greek synthesis uses 0.0 (slightly conservative
# on call delta). The provider-greeks tier — the common case — never consults it.
_DEFAULT_DIVIDEND_YIELD = 0.0


def _make_option_client(key: str, secret: str):  # noqa: ANN202 - alpaca type is lazy
    """Module-level factory for the read-only Alpaca options data client.

    Isolated so unit tests can monkeypatch it (or inject a client directly) and
    never touch the network. The alpaca-py import is lazy here so importing this
    module never requires the [alpaca] extra.
    """
    from alpaca.data.historical.option import OptionHistoricalDataClient

    return OptionHistoricalDataClient(api_key=key, secret_key=secret)


def _make_stock_client(key: str, secret: str):  # noqa: ANN202 - alpaca type is lazy
    """Module-level factory for the read-only Alpaca equity data client (used to
    source the underlying spot for greek completion). Monkeypatchable in tests."""
    from alpaca.data.historical.stock import StockHistoricalDataClient

    return StockHistoricalDataClient(api_key=key, secret_key=secret)


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

    def fetch_chain_live(
        self,
        underlying: str,
        *,
        client: object | None = None,
        stock_client: object | None = None,
        asof: datetime | None = None,
    ) -> OptionChain:
        """Thin Alpaca read-only chain adapter — INERT by default (AG-PERC-3).

        Raises LiveChainDisabled unless HERMES_QUANT_OPTIONS_LIVE_CHAIN=1 AND
        Alpaca credentials are present in the environment. The alpaca-py import
        is lazy (via ``_make_option_client``) so importing this module never
        requires the [alpaca] extra.

        When enabled, pulls ``OptionHistoricalDataClient.get_option_chain`` (R5:
        latest NBBO + IV + greeks per OCC contract), sources the underlying spot
        from the equity client (for greek completion + the snapshot's
        ``underlying_spot``), completes the ~no-greeks tier via optlib through the
        fail-closed validator (``_complete_greeks_or_fail`` — a contract whose
        mid<=0 / dte<=0 is dropped, a chain-wide spot<=0 raises; NEVER zero-greeks),
        stamps ``fetched_at`` (provider-return wall-clock; ADR-0028 D7), and
        persists the chain to
        ``~/.hermes/quant/option_chains/<u>/<YYYY-MM-DD>.parquet`` atomically
        (tempfile + os.replace) in the exact schema ``replay_chain`` reads back.

        ``client`` / ``stock_client`` are injectable test seams (default: built
        from credentials via the module-level factories). NEVER submits orders.
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

        underlying = underlying.upper()
        # asof = the decision time the chain is valid AS-OF (caller-supplied so
        # the snapshot is no-lookahead-pinned). Default to fetch wall-clock.
        if asof is None:
            asof = datetime.now(UTC)

        # Build clients lazily from the factory (monkeypatchable). A missing
        # [alpaca] extra surfaces as LiveChainDisabled, not an import explosion.
        if client is None:
            try:
                client = _make_option_client(key, secret)
            except ImportError as exc:  # pragma: no cover - optional dep
                raise LiveChainDisabled(
                    "alpaca-py is not installed; `pip install hermes-quant[alpaca]`"
                ) from exc
        if stock_client is None:
            try:
                stock_client = _make_stock_client(key, secret)
            except ImportError as exc:  # pragma: no cover - optional dep
                raise LiveChainDisabled(
                    "alpaca-py is not installed; `pip install hermes-quant[alpaca]`"
                ) from exc

        spot = self._fetch_underlying_spot(stock_client, underlying)
        raw_chain = self._get_option_chain(client, underlying)
        # Provider-return wall-clock. NOT a scored/decided feature (it gates the
        # no-lookahead filter at replay; ADR-0028 D7), so reading the clock here
        # is honest — never fed into a model.
        fetched_at = datetime.now(UTC)

        snapshots: list[OptionSnapshot] = []
        for symbol, snap in raw_chain.items():
            built = self._build_snapshot(
                symbol=str(symbol),
                provider_snap=snap,
                asof=asof,
                fetched_at=fetched_at,
                spot=spot,
            )
            if built is not None:
                snapshots.append(built)

        chain = OptionChain(
            underlying=underlying,
            asof=asof,
            underlying_spot=spot,
            risk_free_rate=_DEFAULT_RISK_FREE_RATE,
            snapshots=tuple(snapshots),
        )
        self._write_chain_parquet(chain)
        return chain

    # -- live helpers ------------------------------------------------------

    @staticmethod
    def _get_option_chain(client: object, underlying: str) -> dict:
        """Call the read-only chain endpoint and return Dict[occ_symbol, snapshot].

        Builds the request lazily (alpaca import); if the [alpaca] extra is
        missing the lazy import fails fast as LiveChainDisabled."""
        try:
            from alpaca.data.requests import OptionChainRequest
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LiveChainDisabled(
                "alpaca-py is not installed; `pip install hermes-quant[alpaca]`"
            ) from exc
        req = OptionChainRequest(underlying_symbol=underlying)
        return dict(client.get_option_chain(req))  # type: ignore[attr-defined]

    @staticmethod
    def _fetch_underlying_spot(stock_client: object, underlying: str) -> float:
        """Latest equity trade price for ``underlying`` (the greek-completion spot
        + snapshot underlying_spot). Returns 0.0 on a missing/unreadable trade so
        the chain-wide spot<=0 fail-closed branch trips honestly in completion."""
        try:
            from alpaca.data.requests import StockLatestTradeRequest
        except ImportError as exc:  # pragma: no cover - optional dep
            raise LiveChainDisabled(
                "alpaca-py is not installed; `pip install hermes-quant[alpaca]`"
            ) from exc
        req = StockLatestTradeRequest(symbol_or_symbols=underlying)
        trades = stock_client.get_stock_latest_trade(req)  # type: ignore[attr-defined]
        trade = trades.get(underlying) if hasattr(trades, "get") else None
        price = getattr(trade, "price", None)
        return float(price) if price is not None else 0.0

    def _build_snapshot(
        self,
        *,
        symbol: str,
        provider_snap: object,
        asof: datetime,
        fetched_at: datetime,
        spot: float,
    ) -> OptionSnapshot | None:
        """Map one Alpaca OptionsSnapshot into our frozen OptionSnapshot.

        Provider greeks (the common tier) are carried through verbatim. The
        no-greeks tier is completed via optlib behind the fail-closed validator:
        a contract whose mid<=0 / dte<=0 cannot be honestly priced and is DROPPED
        (returns None) — never admitted with zero-greeks. A chain-wide spot<=0
        raises GreekComputationError (the whole chain is untradeable)."""
        quote = getattr(provider_snap, "latest_quote", None)
        bid = getattr(quote, "bid_price", None) if quote is not None else None
        ask = getattr(quote, "ask_price", None) if quote is not None else None
        trade = getattr(provider_snap, "latest_trade", None)
        last = getattr(trade, "price", None) if trade is not None else None
        iv = getattr(provider_snap, "implied_volatility", None)

        bid = _coerce_float(bid)
        ask = _coerce_float(ask)
        last = _coerce_float(last)
        mid = None if bid is None or ask is None else (bid + ask) / 2.0

        provider_greeks = getattr(provider_snap, "greeks", None)
        if provider_greeks is not None:
            greeks = OptionGreeksSnapshot(
                delta=_coerce_float(getattr(provider_greeks, "delta", None)),
                gamma=_coerce_float(getattr(provider_greeks, "gamma", None)),
                theta=_coerce_float(getattr(provider_greeks, "theta", None)),
                vega=_coerce_float(getattr(provider_greeks, "vega", None)),
                rho=_coerce_float(getattr(provider_greeks, "rho", None)),
                iv=_coerce_float(iv),
                iv_source="provider",
            )
        else:
            # No-greeks tier: synthesize via optlib behind the fail-closed gate.
            dte_days = (parse_occ(symbol).expiry - asof.date()).days
            if mid is None or mid <= 0:
                # Cannot price honestly without a usable mid -> drop (fail-closed,
                # never zero-greeks). spot<=0 (a chain-wide defect) still raises
                # so the whole untradeable chain fails loudly.
                if spot <= 0:
                    _complete_greeks_or_fail(mid=1.0, dte_days=1, spot=spot)
                return None
            greeks = self._synthesize_greeks(
                symbol=symbol, mid=mid, dte_days=dte_days, spot=spot, iv=_coerce_float(iv)
            )

        return OptionSnapshot(
            symbol=symbol,
            asof=asof,
            fetched_at=fetched_at,
            bid=bid,
            ask=ask,
            last=last,
            volume=None,  # not on the chain snapshot; R8 OI/volume join deferred
            open_interest=None,
            greeks=greeks,
            underlying_spot=spot,
            risk_free_rate=_DEFAULT_RISK_FREE_RATE,
        )

    @staticmethod
    def _synthesize_greeks(
        *, symbol: str, mid: float, dte_days: int, spot: float, iv: float | None
    ) -> OptionGreeksSnapshot:
        """Complete the no-greeks tier via optlib (European BSM approx, ADR-0028
        D3). Fail-closed FIRST through ``_complete_greeks_or_fail`` (mid/dte/spot
        > 0), then price. Never returns zero-greeks for a bad input."""
        _complete_greeks_or_fail(mid=mid, dte_days=dte_days, spot=spot)
        from .greeks import european_greeks
        from .occ import parse_occ as _parse

        comp = _parse(symbol)
        opt_type = "c" if comp.right == "C" else "p"
        strike = float(comp.strike)
        # Recover IV from the observed mid when the provider did not supply one
        # (the no-greeks tier frequently lacks IV too); fall back to a recovered
        # solve so greeks are never priced off a fabricated vol.
        vol = iv
        if vol is None or vol <= 0:
            from .greeks import implied_vol

            vol = implied_vol(
                opt_type,
                spot=spot,
                strike=strike,
                dte_years=dte_days / 365.0,
                rfr=_DEFAULT_RISK_FREE_RATE,
                dividend_yield=_DEFAULT_DIVIDEND_YIELD,
                market_price=mid,
            )
        g = european_greeks(
            opt_type,
            spot=spot,
            strike=strike,
            dte_years=dte_days / 365.0,
            rfr=_DEFAULT_RISK_FREE_RATE,
            dividend_yield=_DEFAULT_DIVIDEND_YIELD,
            iv=vol,
        )
        return OptionGreeksSnapshot(
            delta=g.delta,
            gamma=g.gamma,
            # optlib theta/vega/rho are per-year/per-unit; the provider tier
            # reports per-day theta + per-1pt vega. Match the provider convention
            # so completed and provider greeks aggregate on the same scale.
            theta=g.theta / 365.0,
            vega=g.vega * 0.01,
            rho=g.rho * 0.01,
            iv=vol,
            iv_source="py_vollib_european_approximation",
        )

    def _write_chain_parquet(self, chain: OptionChain) -> None:
        """Persist ``chain`` to <dir>/<u>/<YYYY-MM-DD>.parquet ATOMICALLY.

        Writes a NamedTemporaryFile in the SAME directory, then os.replace onto
        the final path so a reader never sees a torn file (the replay reader's
        invariant). Schema matches ``_row_to_snapshot`` exactly so a write ->
        replay_chain round-trips."""
        import os as _os
        import tempfile

        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [
            {
                "contract_symbol": s.symbol,
                "asof": s.asof,
                "fetched_at": s.fetched_at,
                "underlying_spot": s.underlying_spot,
                "risk_free_rate": s.risk_free_rate,
                "bid": s.bid,
                "ask": s.ask,
                "last": s.last,
                "volume": s.volume,
                "open_interest": s.open_interest,
                "delta": s.greeks.delta,
                "gamma": s.greeks.gamma,
                "theta": s.greeks.theta,
                "vega": s.greeks.vega,
                "rho": s.greeks.rho,
                "iv": s.greeks.iv,
                "iv_source": s.greeks.iv_source,
            }
            for s in chain.snapshots
        ]
        path = self._path_for(chain.underlying, chain.asof.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        import pandas as pd

        table = pa.Table.from_pandas(pd.DataFrame(rows))
        tmp = tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".parquet.tmp", delete=False
        )
        try:
            tmp.close()
            pq.write_table(table, tmp.name)
            _os.replace(tmp.name, path)
        finally:
            if _os.path.exists(tmp.name):  # pragma: no cover - replace usually wins
                _os.unlink(tmp.name)


def _coerce_float(value: object) -> float | None:
    """Coerce a provider field to float, mapping None/NaN -> None (mirrors the
    replay reader's ``_opt_float`` NaN-as-None convention so a missing provider
    field never silently becomes a 0.0 greek)."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN -> None


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
