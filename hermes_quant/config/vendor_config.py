"""hermes_quant.config.vendor_config — two-level vendor selection.

ADR-0038 §D.6 (P12) — TradingAgents pattern backfill, Wave D Track A.

Provides :class:`VendorConfig`, a frozen Pydantic model that resolves a
method name to the configured vendor using two layers:

1. ``vendor_overrides_by_method`` (per-method override) — wins if set.
2. ``vendors_by_category`` (category default) — used when no per-method
   override exists; the method's category is looked up via
   :func:`hermes_quant.data.vendor_routing.category_for_method`.

Example ``~/.hermes/config.yaml`` snippet (NOT yet read by this class —
see "Deviation" below)::

    quant:
      data:
        vendors_by_category:
          core_ohlcv: yfinance
        vendor_overrides_by_method:
          fetch_latest: alpaca

Deviation from ADR-0038 §D.6 (documented)
----------------------------------------
The ADR describes wiring the resolver to read ``~/.hermes/config.yaml::
quant.data``. For Wave D Track A we ship the model + ``.resolve()`` only;
callers construct a ``VendorConfig`` from a dict (for example, by reading
the YAML themselves and passing the ``quant.data`` slice to
``VendorConfig.model_validate(...)``). The YAML auto-loader is deferred
to v0.4 so this module stays testable in isolation and free of stdlib
filesystem side-effects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hermes_quant.data.vendor_routing import (
    TOOLS_CATEGORIES,
    VENDOR_LIST,
    VENDOR_METHODS,
    category_for_method,
)


class VendorConfig(BaseModel):
    """Two-level vendor selection: category default + per-method override.

    The model is frozen and forbids extra keys so a typo in
    ``~/.hermes/config.yaml`` surfaces at construction time rather than as
    a silent fallback at resolution time.

    Attributes:
        vendors_by_category: maps a category name (key of
            :data:`hermes_quant.data.vendor_routing.TOOLS_CATEGORIES`) to
            the vendor name to use for every method in that category.
        vendor_overrides_by_method: maps a specific method name to a
            vendor name; wins over the category default.

    Validation:
        - Every category in ``vendors_by_category`` must be a known
          category.
        - Every method in ``vendor_overrides_by_method`` must be a known
          method.
        - Every vendor name (in either dict) must be a member of
          :data:`hermes_quant.data.vendor_routing.VENDOR_LIST`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vendors_by_category: dict[str, str] = Field(default_factory=dict)
    vendor_overrides_by_method: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        """Validate category / method / vendor names against the registry.

        We use ``model_post_init`` instead of a field validator so the
        check has access to both fields at once and produces a single
        coherent error for cross-field issues.
        """
        for category, vendor in self.vendors_by_category.items():
            if category not in TOOLS_CATEGORIES:
                raise ValueError(
                    f"unknown category {category!r} in vendors_by_category; "
                    f"known categories: {sorted(TOOLS_CATEGORIES)}"
                )
            if vendor not in VENDOR_LIST:
                raise ValueError(
                    f"unknown vendor {vendor!r} for category {category!r}; "
                    f"known vendors: {sorted(VENDOR_LIST)}"
                )

        for method, vendor in self.vendor_overrides_by_method.items():
            if method not in VENDOR_METHODS:
                raise ValueError(
                    f"unknown method {method!r} in vendor_overrides_by_method; "
                    f"known methods: {sorted(VENDOR_METHODS)}"
                )
            if vendor not in VENDOR_LIST:
                raise ValueError(
                    f"unknown vendor {vendor!r} for method {method!r}; "
                    f"known vendors: {sorted(VENDOR_LIST)}"
                )

    def resolve(self, method: str) -> str:
        """Return the configured vendor name for ``method``.

        Resolution order (first match wins):

        1. ``vendor_overrides_by_method[method]`` — explicit per-method
           override.
        2. ``vendors_by_category[category_for_method(method)]`` — category
           default for the method's category.

        Args:
            method: a known method name (key of
                :data:`hermes_quant.data.vendor_routing.VENDOR_METHODS`).

        Returns:
            Vendor name (member of
            :data:`hermes_quant.data.vendor_routing.VENDOR_LIST`).

        Raises:
            KeyError: if ``method`` is unknown.
            LookupError: if no override and no category default applies
                (the model is valid but doesn't cover this method).
        """
        if method not in VENDOR_METHODS:
            raise KeyError(
                f"unknown method {method!r}; "
                f"known methods: {sorted(VENDOR_METHODS)}"
            )

        if method in self.vendor_overrides_by_method:
            return self.vendor_overrides_by_method[method]

        category = category_for_method(method)
        if category in self.vendors_by_category:
            return self.vendors_by_category[category]

        raise LookupError(
            f"no vendor configured for method {method!r} "
            f"(category {category!r}); set vendors_by_category[{category!r}] "
            f"or vendor_overrides_by_method[{method!r}]"
        )


__all__ = ["VendorConfig"]
