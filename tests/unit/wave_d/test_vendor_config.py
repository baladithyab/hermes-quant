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
    """vendor_overrides_by_method wins over vendors_by_category.

    Currently VENDOR_LIST has only ``yfinance`` (ccxt was removed due to
    fetch_bars signature mismatch — see commit 95173a6 follow-up). This
    test demonstrates the resolution path with the available vendor as
    both category default and override; once a second vendor is added,
    a sharper test asserting ``override != category`` resolution will
    replace this one.
    """
    cfg = VendorConfig(
        vendors_by_category={"core_ohlcv": "yfinance"},
        vendor_overrides_by_method={"fetch_bars": "yfinance"},
    )
    assert cfg.resolve("fetch_bars") == "yfinance"


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
        cfg.vendors_by_category = {"core_ohlcv": "yfinance"}  # type: ignore[misc]


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
        "vendor_overrides_by_method": {"fetch_bars": "yfinance"},
    }
    cfg_a = VendorConfig.model_validate(payload)
    cfg_b = VendorConfig(**payload)
    assert cfg_a == cfg_b
    assert cfg_a.resolve("fetch_bars") == "yfinance"


# ---------------------------------------------------------------------------
# YAML auto-loader (closes ADR-0038 "Correction 3" deviation)
# ---------------------------------------------------------------------------


def test_from_yaml_missing_file_returns_empty_config(tmp_path) -> None:
    """No config file → empty VendorConfig."""
    cfg = VendorConfig.from_yaml(tmp_path / "does-not-exist.yaml")
    assert cfg.vendors_by_category == {}
    assert cfg.vendor_overrides_by_method == {}


def test_from_yaml_empty_file_returns_empty_config(tmp_path) -> None:
    """Empty config file → empty VendorConfig."""
    p = tmp_path / "config.yaml"
    p.write_text("", encoding="utf-8")
    cfg = VendorConfig.from_yaml(p)
    assert cfg.vendors_by_category == {}


def test_from_yaml_no_quant_section_returns_empty_config(tmp_path) -> None:
    """Config without `quant.data` section → empty VendorConfig."""
    p = tmp_path / "config.yaml"
    p.write_text("other:\n  thing: 1\n", encoding="utf-8")
    cfg = VendorConfig.from_yaml(p)
    assert cfg.vendors_by_category == {}


def test_from_yaml_loads_vendors_by_category(tmp_path) -> None:
    """`quant.data.vendors_by_category` is loaded."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "quant:\n"
        "  data:\n"
        "    vendors_by_category:\n"
        "      core_ohlcv: yfinance\n",
        encoding="utf-8",
    )
    cfg = VendorConfig.from_yaml(p)
    assert cfg.vendors_by_category == {"core_ohlcv": "yfinance"}
    assert cfg.resolve("fetch_bars") == "yfinance"


def test_from_yaml_loads_overrides_too(tmp_path) -> None:
    """Both `vendors_by_category` and `vendor_overrides_by_method` load."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "quant:\n"
        "  data:\n"
        "    vendors_by_category:\n"
        "      core_ohlcv: yfinance\n"
        "    vendor_overrides_by_method:\n"
        "      fetch_bars: yfinance\n",
        encoding="utf-8",
    )
    cfg = VendorConfig.from_yaml(p)
    assert cfg.vendor_overrides_by_method == {"fetch_bars": "yfinance"}


def test_from_yaml_validates_unknown_vendor(tmp_path) -> None:
    """A typo in the YAML vendor name fails at load time, not first resolve."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "quant:\n"
        "  data:\n"
        "    vendors_by_category:\n"
        "      core_ohlcv: phantom_vendor\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as exc_info:
        VendorConfig.from_yaml(p)
    assert "unknown vendor" in str(exc_info.value)


def test_from_yaml_ignores_unrelated_quant_data_keys(tmp_path) -> None:
    """Sibling keys under `quant.data` (added by other features) don't trip extra=forbid."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "quant:\n"
        "  data:\n"
        "    vendors_by_category:\n"
        "      core_ohlcv: yfinance\n"
        "    cache_dir: /tmp/somewhere  # unrelated key\n"
        "    retry_budget: 3            # unrelated key\n",
        encoding="utf-8",
    )
    cfg = VendorConfig.from_yaml(p)
    assert cfg.vendors_by_category == {"core_ohlcv": "yfinance"}


def test_get_vendor_config_module_helper_calls_from_yaml(tmp_path) -> None:
    """The module-level `get_vendor_config()` is a thin wrapper."""
    from hermes_quant.config.vendor_config import get_vendor_config

    p = tmp_path / "config.yaml"
    p.write_text("quant:\n  data: {}\n", encoding="utf-8")
    cfg = get_vendor_config(p)
    assert isinstance(cfg, VendorConfig)
    assert cfg.vendors_by_category == {}
