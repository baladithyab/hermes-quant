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
# B22 (R-B22, 2026-05-31): _CCXT singleton restored. CcxtProvider.fetch_bars
# now exposes the canonical Protocol signature, so ccxt can re-join the
# dispatch table (closes the signature-split TODO below).
_CCXT: Any = None


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


def _get_ccxt() -> Any:
    """Lazily instantiate the CcxtProvider singleton (default exchange).

    Lazy construction is REQUIRED here: CcxtProvider.__init__ imports ccxt
    and constructs an exchange client, which must not happen at module import
    (would break test collection on machines without ccxt configured).
    """
    global _CCXT
    if _CCXT is None:
        from hermes_quant.data.ccxt_provider import CcxtProvider

        _CCXT = CcxtProvider()
    return _CCXT


# ADR-0100 (aegis-ob1): OpenBB joins as the 2nd OHLCV tier behind yfinance.
# DEFAULT-OFF (HERMES_QUANT_OPENBB). Lazy construction is REQUIRED here so
# `import vendor_routing` stays side-effect-free on a venv lacking openbb:
# OpenBBProvider.__init__ does NOT import openbb (the SDK is lazy-imported
# only inside its `obb` property, gated on the flag), so constructing the
# singleton is cheap and import-safe even with the flag off / openbb absent.
_OPENBB: Any = None


def _get_openbb() -> Any:
    """Lazily instantiate the OpenBBProvider singleton.

    Lazy construction keeps ``import vendor_routing`` cheap. OpenBBProvider
    never imports the ``openbb`` SDK at construction (only inside its ``obb``
    property, flag-gated), so this is safe even when ``HERMES_QUANT_OPENBB``
    is unset and ``openbb`` is not installed.
    """
    global _OPENBB
    if _OPENBB is None:
        from hermes_quant.data.openbb_provider import OpenBBProvider

        _OPENBB = OpenBBProvider()
    return _OPENBB


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


def _ccxt_fetch_bars(*args: Any, **kwargs: Any) -> Any:
    # B22: CcxtProvider.fetch_bars now conforms to the canonical Protocol
    # signature (asset, timeframe, start, end, *, use_cache, as_of), so it can
    # sit in the same dispatch entry as yfinance.
    return _get_ccxt().fetch_bars(*args, **kwargs)


def _openbb_fetch_bars(*args: Any, **kwargs: Any) -> Any:
    # ADR-0100 (aegis-ob1): OpenBBProvider.fetch_bars exposes the canonical
    # signature `(asset, timeframe, start, end, *, use_cache, as_of)` — same
    # call shape as yfinance. DEFAULT-OFF: with HERMES_QUANT_OPENBB unset the
    # call raises DataProviderError (flag off) at fetch time, which
    # fetch_with_chain treats as a transient fall-through, so openbb is a
    # silent no-op tier until the operator flips the flag.
    return _get_openbb().fetch_bars(*args, **kwargs)


VENDOR_METHODS: dict[str, dict[str, Callable[..., Any]]] = {
    "fetch_bars": {
        "yfinance": _yfinance_fetch_bars,
        # ADR-0100 (aegis-ob1): openbb is the 2nd OHLCV tier behind yfinance.
        "openbb": _openbb_fetch_bars,
        # B22 (R-B22, 2026-05-31): ccxt re-added. CcxtProvider.fetch_bars now
        # exposes the canonical signature
        # `(asset, timeframe, start, end, *, use_cache, as_of)` — identical to
        # YFinanceProvider — so ``route_to_vendor("fetch_bars", "ccxt")`` works
        # with the same call shape. The legacy crypto-specific path moved to
        # CcxtProvider._fetch_crypto_bars. Closes the signature-split TODO from
        # commit 95173a6 (Wave D, P11/P12).
        "ccxt": _ccxt_fetch_bars,
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


VENDOR_LIST: list[str] = ["yfinance", "openbb", "ccxt"]
# B22 (R-B22, 2026-05-31): "ccxt" re-added now that CcxtProvider.fetch_bars
# conforms to the canonical DataProvider Protocol signature (see note above).
# ADR-0100 (aegis-ob1, 2026-06-17): "openbb" inserted as the 2nd OHLCV tier
# (behind yfinance, ahead of ccxt). DEFAULT-OFF (HERMES_QUANT_OPENBB) — a
# silent no-op fall-through tier until the operator flips the flag.


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
