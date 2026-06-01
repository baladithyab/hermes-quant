"""Tests for ADR-0038 §D.5 (P11) — VENDOR_METHODS 2D dispatch table.

Coverage:
- Registry shape: VENDOR_METHODS, TOOLS_CATEGORIES, VENDOR_LIST.
- ``route_to_vendor`` happy path + both classes of KeyError.
- ``category_for_method`` happy path + KeyError.
- Vendor completeness: every vendor in VENDOR_LIST that's listed for a
  category implements every method in that category.
- Dispatch closures wrap a lazy provider singleton.
"""

from __future__ import annotations

import inspect

import pytest

from hermes_quant.data import vendor_routing as vr


def test_vendor_methods_is_two_level_dict() -> None:
    """VENDOR_METHODS keys are method names, inner dict keys are vendor names."""
    assert isinstance(vr.VENDOR_METHODS, dict)
    for method_name, vendor_dict in vr.VENDOR_METHODS.items():
        assert isinstance(method_name, str), method_name
        assert isinstance(vendor_dict, dict), method_name
        for vendor_name, callable_obj in vendor_dict.items():
            assert isinstance(vendor_name, str), (method_name, vendor_name)
            assert callable(callable_obj), (method_name, vendor_name)


def test_tools_categories_keys_are_method_lists() -> None:
    """TOOLS_CATEGORIES values are lists of method names that exist in VENDOR_METHODS."""
    assert isinstance(vr.TOOLS_CATEGORIES, dict)
    for category, methods in vr.TOOLS_CATEGORIES.items():
        assert isinstance(category, str), category
        assert isinstance(methods, list), category
        for method in methods:
            assert method in vr.VENDOR_METHODS, (category, method)


def test_vendor_list_matches_registered_vendors() -> None:
    """Every vendor that appears in VENDOR_METHODS is in VENDOR_LIST."""
    seen_vendors: set[str] = set()
    for vendor_dict in vr.VENDOR_METHODS.values():
        seen_vendors.update(vendor_dict)
    assert seen_vendors.issubset(set(vr.VENDOR_LIST)), (
        f"vendors in VENDOR_METHODS ({sorted(seen_vendors)}) not all in "
        f"VENDOR_LIST ({sorted(vr.VENDOR_LIST)})"
    )


def test_route_to_vendor_returns_callable() -> None:
    """Happy path: resolves to a bound provider method."""
    fn = vr.route_to_vendor("fetch_bars", "yfinance")
    assert callable(fn)
    # It should be a bound method (or function) with a signature
    sig = inspect.signature(fn)
    assert sig is not None


def test_route_to_vendor_unknown_method_raises_keyerror() -> None:
    """Unknown method names produce a KeyError that names the missing half."""
    with pytest.raises(KeyError, match="unknown method"):
        vr.route_to_vendor("nonexistent_method", "yfinance")


def test_route_to_vendor_unknown_vendor_raises_keyerror() -> None:
    """Unknown vendor for a known method produces a different-shape KeyError."""
    with pytest.raises(KeyError, match="does not implement"):
        vr.route_to_vendor("fetch_bars", "phantom_exchange")


def test_route_to_vendor_yfinance_dispatches_to_yfinance_provider() -> None:
    """The dispatched callable is a closure that wraps YFinanceProvider.fetch_bars.

    The closure is module-level (so it can sit in the dispatch dict) and
    forwards to the lazy provider getter. We assert by name to avoid
    coupling tests to the closure shape.
    """
    fn = vr.route_to_vendor("fetch_bars", "yfinance")
    assert fn is vr._yfinance_fetch_bars
    # First call would lazy-instantiate the provider; we don't trigger
    # that here to keep the test offline-safe.


def test_route_to_vendor_ccxt_resolves() -> None:
    """B22 (R-B22, 2026-05-31): ccxt re-added to the dispatch table.

    CcxtProvider.fetch_bars now conforms to the canonical Protocol signature
    `(asset, timeframe, start, end, *, use_cache, as_of)`, so
    route_to_vendor("fetch_bars", "ccxt") resolves to the _ccxt_fetch_bars
    closure (no longer a KeyError). The closure does not construct a provider
    until called, so this stays side-effect-free.
    """
    fn = vr.route_to_vendor("fetch_bars", "ccxt")
    assert fn is vr._ccxt_fetch_bars
    assert callable(fn)


def test_category_for_method_happy_path() -> None:
    """Every method known to the registry has a category."""
    for category, methods in vr.TOOLS_CATEGORIES.items():
        for method in methods:
            assert vr.category_for_method(method) == category


def test_category_for_method_unknown_raises_keyerror() -> None:
    """An unregistered method raises KeyError with category list in message."""
    with pytest.raises(KeyError, match="not in any category"):
        vr.category_for_method("definitely_not_a_method")


def test_vendor_completeness() -> None:
    """For every category, every vendor in VENDOR_METHODS[method] is the same set.

    This is the static guarantee TradingAgents codifies: \"every vendor
    implements every method in its category.\" If a future vendor is
    added but only implements a subset, this test forces the maintainer
    to either fill the gap or remove the partial vendor from the
    category's method registry.
    """
    for category, methods in vr.TOOLS_CATEGORIES.items():
        if not methods:
            continue
        # The vendor set for the first method is the canonical set
        canonical = set(vr.VENDOR_METHODS[methods[0]])
        for method in methods[1:]:
            method_vendors = set(vr.VENDOR_METHODS[method])
            assert method_vendors == canonical, (
                f"category {category}: method {method!r} has vendors "
                f"{sorted(method_vendors)} but canonical is "
                f"{sorted(canonical)}"
            )


def test_dispatch_closures_are_callable_without_provider_instantiation() -> None:
    """Importing vendor_routing must not eagerly construct providers.

    The dispatch table holds module-level closures (``_yfinance_fetch_bars``
    et al.) rather than bound methods on a real instance. Until the
    closure is actually called, no provider is built — keeping
    ``import vendor_routing`` side-effect free for tests and CI.
    """
    # The closure objects themselves are real callables
    assert callable(vr._yfinance_fetch_bars)
    assert callable(vr._ccxt_fetch_bars)
    # And they appear in the dispatch table
    assert vr.VENDOR_METHODS["fetch_bars"]["yfinance"] is vr._yfinance_fetch_bars
    # B22: ccxt re-added now that fetch_bars conforms to the canonical
    # Protocol signature. The closure still does NOT construct a provider at
    # import — _get_ccxt() lazy-imports ccxt only on first call.
    assert vr.VENDOR_METHODS["fetch_bars"]["ccxt"] is vr._ccxt_fetch_bars
    assert "ccxt" in vr.VENDOR_LIST
    # Importing vendor_routing must not have built the ccxt singleton yet.
    assert vr._CCXT is None
