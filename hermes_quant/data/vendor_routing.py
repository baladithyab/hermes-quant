"""hermes_quant.data.vendor_routing — 2D method × vendor dispatch table.

ADR-0038 §D.5 (P11) — TradingAgents pattern backfill, Wave D Track A.

This module exposes a flat dispatch table mapping (method, vendor) → callable,
keyed off the existing data-provider classes. It is **additive** — it does
not replace `hermes_quant.data.base.fetch_with_chain`. Call sites can opt
into the dispatch table via :func:`route_to_vendor`; existing chain-based
call sites are unchanged.

Categories
----------
``TOOLS_CATEGORIES`` groups methods so per-method overrides can fall back
to a category default (see :class:`hermes_quant.config.vendor_config.VendorConfig`).
For Wave D we ship a single ``core_ohlcv`` category containing the
intersection of methods every listed vendor implements.

Deviation from ADR-0038 §D.5 (documented)
----------------------------------------
The ADR example lists both ``fetch_bars`` and ``fetch_latest`` under
``core_ohlcv``. ``CcxtProvider`` does not implement ``fetch_latest`` in
the current tree (only ``YFinanceProvider`` does), and the Track A scope
explicitly forbids edits outside ``vendor_routing.py`` /
``vendor_config.py`` / fixtures (see plan, "Edit surface"). To preserve
``test_vendor_completeness`` as a static guarantee that every vendor in
``VENDOR_LIST`` implements every method in its category, we ship only
``fetch_bars`` in ``core_ohlcv`` for v0.4. Adding ``fetch_latest`` to
``CcxtProvider`` (or any other category expansion) is a follow-up wave.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hermes_quant.data.yfinance_provider import YFinanceProvider

# ---------------------------------------------------------------------------
# Provider singletons.
#
# We instantiate one provider per vendor at import time. Providers are
# stateless w.r.t. configuration (constructor takes only retry knobs with
# defaults) and lazy-load their heavy SDK on first call. Sharing a single
# instance per vendor lets tests and call sites observe the same retry/health
# counters across the dispatch table.
# ---------------------------------------------------------------------------
_YFINANCE: YFinanceProvider | None = None
# NOTE: _CCXT singleton + _get_ccxt() helper removed in commit 95173a6
# follow-up — see signature-compatibility note in VENDOR_METHODS below.


def _get_yfinance() -> YFinanceProvider:
    """Lazily instantiate the YFinanceProvider singleton.

    Lazy construction keeps ``import vendor_routing`` cheap and side-
    effect-free — important because some providers (notably
    ``CcxtProvider``) hit network or config at __init__ time, which
    breaks test collection if the dispatch table is built eagerly.
    """
    global _YFINANCE
    if _YFINANCE is None:
        _YFINANCE = YFinanceProvider()
    return _YFINANCE


# ---------------------------------------------------------------------------
# Dispatch table: method_name -> {vendor_name -> callable}.
#
# Type: dict[str, dict[str, Callable[..., Any]]]. The return type of the
# inner callable is ``Any`` because per-vendor methods may diverge (e.g.,
# fetch_bars returns pd.DataFrame; future fetch_orderbook might return a
# dict). Concrete return types are documented on each provider method.
#
# We use closures around the lazy getters so the actual provider instance
# is only constructed on first call. Closures capture the lazy lookup
# rather than a bound method, so import time stays free of side effects.
# ---------------------------------------------------------------------------


def _yfinance_fetch_bars(*args: Any, **kwargs: Any) -> Any:
    return _get_yfinance().fetch_bars(*args, **kwargs)


# NOTE: _ccxt_fetch_bars closure removed in commit 95173a6 follow-up —
# CcxtProvider.fetch_bars signature differs from YFinanceProvider's, so it
# cannot sit in the same dispatch entry. Reintroduce when DataProvider
# Protocol unifies the signature (future ADR).


VENDOR_METHODS: dict[str, dict[str, Callable[..., Any]]] = {
    "fetch_bars": {
        "yfinance": _yfinance_fetch_bars,
        # NOTE: CcxtProvider.fetch_bars signature is currently
        # `(symbol, asset_class, timeframe, *, lookback_bars, as_of)` —
        # NOT compatible with YFinanceProvider.fetch_bars
        # `(asset, timeframe, start, end, ...)`. Calling
        # ``route_to_vendor("fetch_bars", "ccxt")`` and passing
        # yfinance-style args would raise TypeError. Until the
        # DataProvider Protocol unifies these signatures (planned for a
        # future ADR), ccxt is intentionally absent from the dispatch
        # table; callers that need ccxt klines should construct a
        # CcxtProvider directly. Caught by Codex review of commit
        # 95173a6 (Wave D, P11/P12).
    },
}


# ---------------------------------------------------------------------------
# Categories (method groupings) and the canonical vendor list.
#
# A category default lets users say "use yfinance for all core_ohlcv
# methods" without enumerating each method. Per-method overrides
# (configured via VendorConfig) win over category defaults.
# ---------------------------------------------------------------------------
TOOLS_CATEGORIES: dict[str, list[str]] = {
    "core_ohlcv": ["fetch_bars"],
}


VENDOR_LIST: list[str] = ["yfinance"]
# NOTE: "ccxt" is intentionally NOT in VENDOR_LIST as of commit 95173a6
# follow-up — see signature-compatibility note above. Add when the
# DataProvider Protocol unifies fetch_bars across vendors.


def route_to_vendor(method: str, vendor: str) -> Callable[..., Any]:
    """Resolve ``(method, vendor)`` to the implementing callable.

    Args:
        method: method name, e.g. ``"fetch_bars"``. Must be a key of
            :data:`VENDOR_METHODS`.
        vendor: vendor name, e.g. ``"yfinance"``. Must be a key of
            ``VENDOR_METHODS[method]``.

    Returns:
        The bound provider method ready to be called.

    Raises:
        KeyError: if ``method`` is unknown, or if ``vendor`` does not
            implement ``method``. The error message names the missing
            half so the caller can distinguish "no such method" from
            "vendor doesn't implement this method".
    """
    if method not in VENDOR_METHODS:
        raise KeyError(
            f"unknown method {method!r}; known methods: {sorted(VENDOR_METHODS)}"
        )
    vendors_for_method = VENDOR_METHODS[method]
    if vendor not in vendors_for_method:
        raise KeyError(
            f"vendor {vendor!r} does not implement {method!r}; "
            f"available vendors: {sorted(vendors_for_method)}"
        )
    return vendors_for_method[vendor]


def category_for_method(method: str) -> str:
    """Return the category that contains ``method``.

    Raises:
        KeyError: if ``method`` is not registered in any category.
    """
    for category, methods in TOOLS_CATEGORIES.items():
        if method in methods:
            return category
    raise KeyError(
        f"method {method!r} is not in any category; "
        f"known categories: {sorted(TOOLS_CATEGORIES)}"
    )


__all__ = [
    "VENDOR_METHODS",
    "TOOLS_CATEGORIES",
    "VENDOR_LIST",
    "route_to_vendor",
    "category_for_method",
]
