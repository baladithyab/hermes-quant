"""Tests for ADR-0038 §D.6 (P12) — VendorConfig two-level resolution.

Coverage:
- Empty config: resolution raises (no default).
- Category-only resolution.
- Per-method override beats category default.
- Unknown method raises.
- Unknown category in vendors_by_category fails at construction.
- Unknown vendor in either dict fails at construction.
- ConfigDict frozen + extra=forbid.
- model_validate from dict-shaped config (round-trip).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_quant.config.vendor_config import VendorConfig


def test_empty_config_resolve_raises_lookuperror() -> None:
    """No category default, no override → resolve raises LookupError.

    ADR-0038 §D.6: ``resolve()`` returns the configured vendor for a
    method. With nothing configured, there is no answer; raise rather
    than return a silent default. The implementation raises
    ``LookupError`` (KeyError's parent class) so callers can catch
    either.
    """
    cfg = VendorConfig()
    with pytest.raises(LookupError):
        cfg.resolve("fetch_bars")


def test_unknown_method_raises_lookuperror() -> None:
    """A method not in any category raises LookupError on resolve()."""
    cfg = VendorConfig(vendors_by_category={"core_ohlcv": "yfinance"})
    with pytest.raises(LookupError):
        cfg.resolve("not_a_method")


def test_category_default_resolves() -> None:
    """A method in a configured category resolves to that category's vendor."""
    cfg = VendorConfig(vendors_by_category={"core_ohlcv": "yfinance"})
    assert cfg.resolve("fetch_bars") == "yfinance"


def test_per_method_override_beats_category() -> None:
    """vendor_overrides_by_method wins over vendors_by_category."""
    cfg = VendorConfig(
        vendors_by_category={"core_ohlcv": "yfinance"},
        vendor_overrides_by_method={"fetch_bars": "ccxt"},
    )
    assert cfg.resolve("fetch_bars") == "ccxt"


def test_unknown_category_fails_at_construction() -> None:
    """Typo in vendors_by_category fails Pydantic validation at __init__.

    The point is to surface the error at config-load time, not at the
    first resolve() that needs that category.
    """
    with pytest.raises(ValidationError) as exc_info:
        VendorConfig(vendors_by_category={"phantom_category": "yfinance"})
    assert "unknown category" in str(exc_info.value)


def test_unknown_vendor_in_category_fails_at_construction() -> None:
    """Typo in the vendor name (category side) fails at __init__."""
    with pytest.raises(ValidationError) as exc_info:
        VendorConfig(vendors_by_category={"core_ohlcv": "phantom_vendor"})
    assert "unknown vendor" in str(exc_info.value)


def test_unknown_vendor_in_override_fails_at_construction() -> None:
    """Typo in the vendor name (override side) fails at __init__."""
    with pytest.raises(ValidationError) as exc_info:
        VendorConfig(vendor_overrides_by_method={"fetch_bars": "phantom_vendor"})
    assert "unknown vendor" in str(exc_info.value)


def test_unknown_method_in_override_fails_at_construction() -> None:
    """A method that's not in any category cannot be overridden."""
    with pytest.raises(ValidationError) as exc_info:
        VendorConfig(vendor_overrides_by_method={"phantom_method": "yfinance"})
    assert "unknown method" in str(exc_info.value)


def test_model_is_frozen() -> None:
    """ConfigDict(frozen=True) prevents post-construction mutation."""
    cfg = VendorConfig(vendors_by_category={"core_ohlcv": "yfinance"})
    with pytest.raises(ValidationError):
        cfg.vendors_by_category = {"core_ohlcv": "ccxt"}  # type: ignore[misc]


def test_extra_keys_forbidden() -> None:
    """ConfigDict(extra='forbid') rejects unknown keys.

    A typo like ``vendor_by_category`` (singular) would silently fall
    back to default if extra were ``allow`` or ``ignore``; ``forbid``
    surfaces the typo at parse time.
    """
    with pytest.raises(ValidationError):
        VendorConfig.model_validate(
            {
                "vendors_by_category": {"core_ohlcv": "yfinance"},
                "wrong_key_name": "noise",
            }
        )


def test_model_validate_round_trip() -> None:
    """model_validate(dict) == direct constructor with kwargs."""
    payload = {
        "vendors_by_category": {"core_ohlcv": "yfinance"},
        "vendor_overrides_by_method": {"fetch_bars": "ccxt"},
    }
    cfg_a = VendorConfig.model_validate(payload)
    cfg_b = VendorConfig(**payload)
    assert cfg_a == cfg_b
    assert cfg_a.resolve("fetch_bars") == "ccxt"
