"""hermes_quant.admissibility.oracle — pre-trade admissibility + ShortabilityOracle (ADR-0077).

A hard, deterministic, fail-closed precondition that sits UPSTREAM of the ADR-0004 risk gate.
It can only REJECT a proposed order (or flatten an inadmissible held short -> 0.0). It can NEVER
force, amplify, or override a trade. Adopts QuantConnect Lean's IShortableProvider tri-state shape.

Gated by HERMES_QUANT_ADMISSIBILITY (default OFF -> NullShortabilityOracle == today's behavior,
bit-for-bit). Borrow carry is a SEPARATE flag (HERMES_QUANT_BORROW_COST) in borrow_pnl.py.

This module writes NOTHING to executions.jsonl / state.db. It is a pure pre-trade predicate.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ETB default borrow APR used for carry accrual when easy_to_borrow=True.
# Research §3: ETB ~ 0.25-1.00% APR; we use a single coarse 0.30% default and
# REJECT (never fake a rate for) non-ETB names.
ETB_DEFAULT_ANNUAL_CBR: float = 0.0030

# Reg-T short minimum account equity (research §2). Below this there is no short capability.
MIN_SHORT_ACCOUNT_EQUITY_USD: float = 2_000.0

# Reg-T short initial buying-power requirement multiple (150% of short market value).
REG_T_SHORT_INITIAL_BPR_MULT: float = 1.50

# Alpaca opening-short order value = MAX(limit, 1.03 * ask) * qty (research §2).
ALPACA_SHORT_ASK_MULT: float = 1.03


class AdmissibilityState(StrEnum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


# Typed rejection/partial reasons. None only on ACCEPTED.
REASON_NOT_SHORTABLE = "NOT_SHORTABLE"
REASON_NOT_ETB = "NOT_ETB"
REASON_FRACTIONAL_SHORT = "FRACTIONAL_SHORT"
REASON_NOT_MARGINABLE = "NOT_MARGINABLE"
REASON_INSUFFICIENT_BPR = "INSUFFICIENT_BPR"
REASON_EQUITY_BELOW_2K = "EQUITY_BELOW_2K"
REASON_PTP_BLOCKED = "PTP_BLOCKED"
REASON_SSR_MARKETABLE_SHORT = "SSR_MARKETABLE_SHORT"
REASON_UNKNOWN_SHORTABILITY = "UNKNOWN_SHORTABILITY"  # fail-closed: ctx/oracle could not determine
# fail-closed: shortability resolved, but the account/quote inputs the BP+equity hard
# checks REQUIRE are absent -> we cannot prove the order fits, so we REJECT (never assume).
REASON_MISSING_ACCOUNT_CONTEXT = "MISSING_ACCOUNT_CONTEXT"  # account_equity / available_bp absent
REASON_MISSING_QUOTE = "MISSING_QUOTE"  # current_ask absent (cannot value the short)


@dataclass(frozen=True)
class AdmissibilityContext:
    """Point-in-time inputs the oracle needs. All optional EXCEPT what the
    requested side needs; a missing field that the side requires => fail-closed REJECT.

    asset fields mirror alpaca.trading.models.Asset (research §1).
    """

    # --- asset shortability (from get_asset() or the static allowlist) ---
    tradable: bool | None = None
    marginable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    fractionable: bool | None = None
    attributes: tuple[str, ...] = ()  # e.g. ("ptp_no_exception", "ipo", "has_options")
    margin_requirement_short: float | None = (
        None  # per-asset short margin % (equities); 1.50 if None
    )

    # --- pricing / account (buying-power check) ---
    current_ask: float | None = None  # for the MAX(limit, 1.03*ask)*qty BP charge
    limit_price: float | None = None  # None => market order
    available_bp: float | None = None  # account buying power
    account_equity: float | None = None  # for the < $2,000 gate

    # --- borrow APR for carry (longs => 0.0; ETB => ETB_DEFAULT_ANNUAL_CBR) ---
    annual_cbr: float | None = None

    # --- SSR (Reg SHO Rule 201): conservative posture, no NBB tick data ---
    ssr_active: bool = False  # latched true once intraday low <= prev_close*0.90
    is_marketable: bool = True  # marketable short during SSR => deferred/partial


@dataclass(frozen=True)
class ShortabilityVerdict:
    state: AdmissibilityState
    reason: str | None  # one of REASON_* above, or None on ACCEPTED
    annual_cbr: float  # cost-to-borrow APR for carry accrual (0.0 for longs / non-shorts)


@runtime_checkable
class ShortabilityOracle(Protocol):
    def verdict(
        self, symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
    ) -> ShortabilityVerdict: ...


@dataclass(frozen=True)
class ETBSnapshotEntry:
    symbol: str
    asof: str  # ISO-8601 UTC date the snapshot applies to
    easy_to_borrow: bool
    shortable: bool
    marginable: bool
    annual_cbr: float  # ETB rate for this name as of `asof`


def _is_short_side(side: str) -> bool:
    return str(side).strip().lower() in {"short", "sell_short", "ss"}


def _is_whole_share(qty: float) -> bool:
    # Tolerance-free integer check; fractional shorts are rejected (live HTTP 422).
    return float(qty) == math.floor(float(qty))


def evaluate_admissibility(
    symbol: str,
    side: str,
    qty: float,
    asof: datetime,
    ctx: AdmissibilityContext,
    *,
    require_account_context: bool = True,
) -> ShortabilityVerdict:
    """The shared deterministic core all oracles delegate to once ctx is populated.

    Evaluation order (research §4, roughly Alpaca's order). LONG / BUY side is always ACCEPTED
    (annual_cbr=0.0) — admissibility only constrains opening SHORTS:

      1. side != 'short'              -> ACCEPTED, cbr 0.0
      2. any required ctx field None  -> REJECTED, UNKNOWN_SHORTABILITY   (FAIL-CLOSED)
      3. not tradable/shortable/ETB   -> REJECTED, NOT_SHORTABLE / NOT_ETB
      4. not marginable               -> REJECTED, NOT_MARGINABLE
      5. account_equity unknown        -> REJECTED, MISSING_ACCOUNT_CONTEXT  (FAIL-CLOSED*)
      5b. account_equity < $2,000      -> REJECTED, EQUITY_BELOW_2K
      6. not whole-share              -> REJECTED, FRACTIONAL_SHORT
      7. ptp_no_exception in attrs    -> REJECTED, PTP_BLOCKED
      8. current_ask unknown          -> REJECTED, MISSING_QUOTE             (FAIL-CLOSED*)
      8b. available_bp unknown         -> REJECTED, MISSING_ACCOUNT_CONTEXT  (FAIL-CLOSED*)
      8c. insufficient BP              -> REJECTED, INSUFFICIENT_BPR
      9. ssr_active and is_marketable -> PARTIAL,  SSR_MARKETABLE_SHORT
      10. else                         -> ACCEPTED, cbr = ctx.annual_cbr or ETB_DEFAULT_ANNUAL_CBR

    The BP+equity hard checks (5, 8) are LIVE-ORDER PRECONDITIONS, not optional refinements:
    on the live path an opening short whose account/quote context is unknown is REJECTED (we
    never fabricate sufficiency / never "assume admissible"). This is the fail-closed default
    (``require_account_context=True``), used by the live AlpacaShortabilityOracle and any
    direct caller.

    (*) ``require_account_context=False`` is for the OFFLINE shortability AUDIT only (the
    StaticETBAllowlistOracle restatement): it answers the narrower question "is this name
    short-ELIGIBLE as of the snapshot?" and has no per-position live quote/BP. In that mode a
    missing equity/quote/BP input is SKIPPED rather than treated as a precondition failure —
    but a PRESENT-and-failing input (equity < $2k, BP < required) still REJECTS. This never
    relaxes the live path; it only scopes the audit to shortability. The NullShortabilityOracle
    (flag OFF) does NOT call this core at all, so flag-OFF behavior is unchanged bit-for-bit.
    """
    # 1. Longs / buys are never constrained by the short-admissibility predicate.
    if not _is_short_side(side):
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)

    # 2. Fail-closed: any shortability field the predicate needs being unknown => REJECT.
    if (
        ctx.tradable is None
        or ctx.shortable is None
        or ctx.easy_to_borrow is None
        or ctx.marginable is None
    ):
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_UNKNOWN_SHORTABILITY, 0.0)

    # 3. Tradability / shortability / ETB.
    if not ctx.tradable or not ctx.shortable:
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_SHORTABLE, 0.0)
    if not ctx.easy_to_borrow:
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_ETB, 0.0)

    # 4. Marginability (short REQUIRES a margin account).
    if not ctx.marginable:
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_MARGINABLE, 0.0)

    # 5. Account equity floor for any short capability. FAIL-CLOSED on the live path: unknown
    # equity is a missing hard-precondition input, not an implicit pass -> REJECT (never assume
    # capable). The offline audit (require_account_context=False) scopes itself to shortability
    # and skips this precondition when equity is absent — but still REJECTS a PRESENT sub-floor.
    if ctx.account_equity is None:
        if require_account_context:
            return ShortabilityVerdict(
                AdmissibilityState.REJECTED, REASON_MISSING_ACCOUNT_CONTEXT, 0.0
            )
    elif ctx.account_equity < MIN_SHORT_ACCOUNT_EQUITY_USD:
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_EQUITY_BELOW_2K, 0.0)

    # 6. Whole-share enforcement (no fractional shorts; live HTTP 422).
    if not _is_whole_share(qty):
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_FRACTIONAL_SHORT, 0.0)

    # 7. PTP names blocked by default.
    if "ptp_no_exception" in ctx.attributes:
        return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_PTP_BLOCKED, 0.0)

    # 8. Buying-power check. FAIL-CLOSED on the live path: the BP hard check is a PRECONDITION
    # for an opening short, not an optional refinement we skip when inputs are absent. Without a
    # quote we cannot value the order; without BP we cannot prove it fits. Either unknown =>
    # REJECT. The offline audit skips the check ONLY when an input is missing; a PRESENT-and-
    # -failing BP still REJECTS (INSUFFICIENT_BPR), so the audit never under-reports a breach.
    if ctx.current_ask is None:
        if require_account_context:
            return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_MISSING_QUOTE, 0.0)
    elif ctx.available_bp is None:
        if require_account_context:
            return ShortabilityVerdict(
                AdmissibilityState.REJECTED, REASON_MISSING_ACCOUNT_CONTEXT, 0.0
            )
    else:
        order_value = max(ctx.limit_price or 0.0, ALPACA_SHORT_ASK_MULT * ctx.current_ask) * qty
        reg_t_required = (ctx.margin_requirement_short or REG_T_SHORT_INITIAL_BPR_MULT) * (
            ctx.current_ask * qty
        )
        if ctx.available_bp < max(order_value, reg_t_required):
            return ShortabilityVerdict(
                AdmissibilityState.REJECTED, REASON_INSUFFICIENT_BPR, 0.0
            )

    # 9. SSR: a marketable short during an SSR window is not immediately fillable.
    if ctx.ssr_active and ctx.is_marketable:
        cbr = ctx.annual_cbr if ctx.annual_cbr is not None else ETB_DEFAULT_ANNUAL_CBR
        return ShortabilityVerdict(AdmissibilityState.PARTIAL, REASON_SSR_MARKETABLE_SHORT, cbr)

    # 10. Admissible ETB short.
    cbr = ctx.annual_cbr if ctx.annual_cbr is not None else ETB_DEFAULT_ANNUAL_CBR
    return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, cbr)


class NullShortabilityOracle:
    """Today's behavior == the bug. Everything ACCEPTED. Selected ONLY when the
    HERMES_QUANT_ADMISSIBILITY flag is OFF, preserving current outputs bit-for-bit."""

    def verdict(
        self, symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
    ) -> ShortabilityVerdict:
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)


class AlpacaShortabilityOracle:
    """Live source of truth. ctx is populated from TradingClient.get_asset(symbol).

    The class accepts an injectable `get_asset` callable (default: lazy real client) so the
    unit suite can drive it with a fake. On ANY error fetching the asset => fail-closed REJECT
    (UNKNOWN_SHORTABILITY).
    """

    def __init__(self, get_asset=None, *, etb_cbr: float = ETB_DEFAULT_ANNUAL_CBR) -> None:
        self._get_asset = get_asset
        self._etb_cbr = etb_cbr
        self._client = None  # cached TradingClient (account fetch reuses get_asset's client)

    def _resolve_client(self):
        """Lazily build + cache the paper TradingClient. Raises if creds absent."""
        if self._client is not None:
            return self._client
        # Lazy import so the package imports without alpaca-py installed.
        import os as _os

        from alpaca.trading.client import TradingClient

        key = _os.environ.get("ALPACA_API_KEY") or _os.environ.get("ALPACA_API_KEY_ID")
        secret = _os.environ.get("ALPACA_API_SECRET") or _os.environ.get("ALPACA_API_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_API_SECRET required")
        self._client = TradingClient(api_key=key, secret_key=secret, paper=True)
        return self._client

    def _resolve_get_asset(self):
        if self._get_asset is not None:
            return self._get_asset
        # Reuse the cached client so get_asset + get_account share one connection.
        self._get_asset = self._resolve_client().get_asset
        return self._get_asset

    def is_tradeable_long(self, symbol: str) -> bool:
        """Read-only long-tradeability predicate (ADR-0075 catalyst onboarding dep).

        The verdict() above is short-focused (it constrains opening shorts).
        Catalyst onboarding needs a LONG-tradeable read: a name is admissible to
        the candidate set only if it is ``tradable AND fractionable`` on the
        broker (a long admit needs no short borrow). Reuses the same get_asset
        plumbing instead of duplicating a TradingClient. Fail-closed: any error,
        missing client, or unknown field -> False (reject — never admit an
        unfillable name).
        """
        try:
            get_asset = self._resolve_get_asset()
            asset = get_asset(symbol)
            return bool(getattr(asset, "tradable", False)) and bool(
                getattr(asset, "fractionable", False)
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed on any live error.
            logger.warning("AlpacaShortabilityOracle.is_tradeable_long(%s) failed: %s", symbol, exc)
            return False

    def verdict(
        self, symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
    ) -> ShortabilityVerdict:
        # Longs short-circuit without any network call.
        if not _is_short_side(side):
            return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)
        try:
            get_asset = self._resolve_get_asset()
            asset = get_asset(symbol)
            margin_req = _parse_float(getattr(asset, "margin_requirement_short", None))
            attributes = tuple(getattr(asset, "attributes", None) or ())
            populated = AdmissibilityContext(
                tradable=getattr(asset, "tradable", None),
                marginable=getattr(asset, "marginable", None),
                shortable=getattr(asset, "shortable", None),
                easy_to_borrow=getattr(asset, "easy_to_borrow", None),
                fractionable=getattr(asset, "fractionable", None),
                attributes=attributes,
                margin_requirement_short=margin_req,
                current_ask=ctx.current_ask,
                limit_price=ctx.limit_price,
                available_bp=ctx.available_bp,
                account_equity=ctx.account_equity,
                annual_cbr=ctx.annual_cbr if ctx.annual_cbr is not None else self._etb_cbr,
                ssr_active=ctx.ssr_active,
                is_marketable=ctx.is_marketable,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed on any live error.
            logger.warning("AlpacaShortabilityOracle: get_asset(%s) failed: %s", symbol, exc)
            return ShortabilityVerdict(
                AdmissibilityState.REJECTED, REASON_UNKNOWN_SHORTABILITY, 0.0
            )
        return evaluate_admissibility(symbol, side, qty, asof, populated)


class StaticETBAllowlistOracle:
    """Offline / backtest SHORTABILITY AUDIT. A point-in-time ETB set + per-name CBR table keyed
    by `asof`, so historical admissibility uses the value as of decision time (D-5). Honest about
    the limitation: a name with NO snapshot for `asof` => REJECT(NOT_ETB) (fail-closed, not
    'assume').

    `snapshot` maps SYMBOL -> ETBSnapshotEntry. The entry's `asof` (an ISO date) must match the
    requested decision date; a snapshot for a different date does NOT apply (no look-ahead).

    This oracle answers "is this name short-ELIGIBLE as of the snapshot?" — it has no per-position
    live quote/BP, so it delegates with ``require_account_context=False``: a MISSING account/quote
    input is scoped out (not a precondition failure), while a PRESENT-and-failing one (equity<$2k,
    BP<required) still REJECTS. The LIVE path (AlpacaShortabilityOracle / select_oracle when the
    flag is ON) uses the fail-closed default and REJECTS on any missing account context.
    """

    def __init__(self, snapshot: dict[str, ETBSnapshotEntry]) -> None:
        self._snapshot = dict(snapshot)

    def verdict(
        self, symbol: str, side: str, qty: float, asof: datetime, ctx: AdmissibilityContext
    ) -> ShortabilityVerdict:
        if not _is_short_side(side):
            return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)
        entry = self._snapshot.get(symbol)
        asof_date = asof.date().isoformat()
        # Fail-closed: no snapshot for this name, or snapshot is for a different date.
        if entry is None or entry.asof != asof_date:
            return ShortabilityVerdict(AdmissibilityState.REJECTED, REASON_NOT_ETB, 0.0)
        populated = AdmissibilityContext(
            tradable=True,
            marginable=entry.marginable,
            shortable=entry.shortable,
            easy_to_borrow=entry.easy_to_borrow,
            fractionable=ctx.fractionable,
            attributes=ctx.attributes,
            margin_requirement_short=ctx.margin_requirement_short,
            current_ask=ctx.current_ask,
            limit_price=ctx.limit_price,
            available_bp=ctx.available_bp,
            account_equity=ctx.account_equity,
            annual_cbr=entry.annual_cbr,
            ssr_active=ctx.ssr_active,
            is_marketable=ctx.is_marketable,
        )
        # Offline shortability audit: missing live quote/BP is scoped out, not a precondition
        # failure. The live oracle (above) keeps the fail-closed default.
        return evaluate_admissibility(
            symbol, side, qty, asof, populated, require_account_context=False
        )


def _parse_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def select_oracle() -> ShortabilityOracle:
    """Factory honoring the flag. HERMES_QUANT_ADMISSIBILITY != '1' -> NullShortabilityOracle
    (bit-identical to today). When '1', returns AlpacaShortabilityOracle (live default).
    Offline callers (the restatement script, backtest) construct StaticETBAllowlistOracle directly.
    """
    if os.environ.get("HERMES_QUANT_ADMISSIBILITY", "0") == "1":
        return AlpacaShortabilityOracle()
    return NullShortabilityOracle()


def live_buying_power() -> float | None:
    """Live paper-account buying power (USD), or None on ANY failure.

    Reuses the AlpacaShortabilityOracle's lazy paper TradingClient (same creds:
    ALPACA_API_KEY[_ID] + ALPACA_API_SECRET[_KEY], paper=True). Fetches
    get_account().buying_power. FAIL-CLOSED: missing alpaca-py, missing creds, a
    network/API error, or a non-positive value all return None so the caller's
    admissibility BP check fails-closed (MISSING_ACCOUNT_CONTEXT) rather than
    admitting a short on a fabricated sufficiency (ADR-0077 D77, the documented
    H-adm #1 gap this closes). Never raises.
    """
    try:
        oracle = AlpacaShortabilityOracle()
        client = oracle._resolve_client()
        account = client.get_account()
        bp = float(getattr(account, "buying_power", 0) or 0)
        return bp if bp > 0 else None
    except Exception as exc:  # noqa: BLE001 — fail-closed: unknown BP => None.
        logger.warning("admissibility: live buying-power fetch failed (fail-closed): %s", exc)
        return None
