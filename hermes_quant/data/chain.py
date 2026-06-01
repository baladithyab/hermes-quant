"""hermes_quant.data.chain — default-OFF multi-provider fallback chain builder (B22).

R-B22 (2026-05-31): the auto-fallback ENGINE (``fetch_with_chain``) already
exists and is tested. This module only assembles the ORDERED provider list per
asset class, behind a default-OFF flag, so the single-provider path stays
byte-identical when the flag is unset.

Rails:
- Default-OFF: the multi-provider chain is gated behind
  ``HERMES_QUANT_DATA_FALLBACK=1`` (read at CALL time, mirroring the repo's
  flag convention — HERMES_QUANT_PORTFOLIO_CAPS / HERMES_QUANT_SEMANTIC_ENABLED).
  When unset -> a single-element list with TODAY's canonical provider
  (yfinance for equity/etf, ccxt for crypto). Existing single-provider callers
  see no behavior change.
- Fail-closed: AlphaVantage is appended (equity/etf only) ONLY when its key
  resolves AND construction succeeds; a missing key / import failure silently
  drops the tier rather than crashing the chain.
- Order: yfinance/ccxt FIRST, AlphaVantage LAST (AV free tier is 25/day —
  last-resort only, never primary).
- No crypto AV equivalent on the free tier; crypto chain is ccxt-only.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DATA_FALLBACK_FLAG = "HERMES_QUANT_DATA_FALLBACK"
_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"

_EQUITY_CLASSES = {"equity", "etf"}
_CRYPTO_CLASSES = {"crypto"}


def data_fallback_enabled() -> bool:
    """Whether the multi-provider fallback chain is enabled (read at call time)."""
    return os.environ.get(DATA_FALLBACK_FLAG, "0") == "1"


def _build_alphavantage_tier() -> list:
    """Return ``[AlphaVantageProvider()]`` iff a key resolves, else ``[]``.

    Fail-closed: never raises. A missing key, missing requests, or any
    construction error silently drops the AV tier from the chain.
    """
    if not os.environ.get(_API_KEY_ENV):
        return []
    try:
        from hermes_quant.data.alphavantage_provider import AlphaVantageProvider

        return [AlphaVantageProvider()]
    except Exception as e:  # noqa: BLE001
        logger.warning("alphavantage tier unavailable, skipping: %s", e)
        return []


def build_provider_chain(asset_class: str, **kwargs) -> list:
    """Build the ordered DataProvider chain for ``asset_class``.

    Default-OFF: returns a SINGLE canonical provider unless
    ``HERMES_QUANT_DATA_FALLBACK=1`` is set, in which case fallback tiers are
    appended (AlphaVantage last, equity/etf only, and only if its key resolves).

    Args:
        asset_class: 'equity' | 'etf' | 'crypto'.
        **kwargs: forwarded to the primary provider constructor (e.g.
            ``exchange_id`` for crypto/ccxt).

    Returns:
        Ordered list of DataProvider instances suitable for ``fetch_with_chain``.

    Raises:
        ValueError: unknown asset_class.
    """
    if asset_class in _EQUITY_CLASSES:
        from hermes_quant.data.yfinance_provider import YFinanceProvider

        chain = [YFinanceProvider()]
        if data_fallback_enabled():
            chain += _build_alphavantage_tier()
        return chain

    if asset_class in _CRYPTO_CLASSES:
        from hermes_quant.data.ccxt_provider import CcxtProvider

        # ccxt-only; no free-tier AV crypto equivalent. The flag is honored
        # for symmetry but adds no extra tier for crypto today.
        return [CcxtProvider(**kwargs)]

    raise ValueError(
        f"unknown asset_class {asset_class!r}; expected one of "
        f"{sorted(_EQUITY_CLASSES | _CRYPTO_CLASSES)}"
    )


__all__ = [
    "DATA_FALLBACK_FLAG",
    "data_fallback_enabled",
    "build_provider_chain",
]
