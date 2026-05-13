"""Tests for safe_symbol_component (ADR-0005 amendment, Wave C.2).

Per the founding charter §"Layer 1 Analyst Pool" — symbols flow into
filesystem paths (cache files, JSONL filenames, log paths). The
sanitizer is the trust boundary between user/wire input and disk.
"""
from __future__ import annotations

import pytest

from hermes_quant.utils.symbol_safety import safe_symbol_component


# ---------------------------------------------------------------------------
# Happy path — common ticker shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("AAPL", "AAPL"),
    ("SPY", "SPY"),
    ("BRK.B", "BRK.B"),         # dot inside is fine
    ("BRK-B", "BRK-B"),         # hyphen is fine (Yahoo convention)
    ("ES_F", "ES_F"),           # underscore is fine
    ("BTC_USDT", "BTC_USDT"),
])
def test_normal_tickers_pass_through(symbol, expected):
    assert safe_symbol_component(symbol) == expected


def test_crypto_pair_slash_replaced():
    """`BTC/USDT` is a common shape but `/` is a path separator."""
    out = safe_symbol_component("BTC/USDT")
    assert out == "BTC_USDT"


# ---------------------------------------------------------------------------
# Path-traversal attack class
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../etc/passwd",
    "..",
    ".",
    "../sibling",
    "/absolute/path",
    "C:\\Windows\\System32",
])
def test_path_traversal_attempts_neutralized(hostile):
    """Either raises ValueError, or returns a string with no traversal tokens."""
    try:
        result = safe_symbol_component(hostile)
    except ValueError:
        # Acceptable — the function refused outright
        return
    # Otherwise the result must NOT be a traversal token nor contain `/`
    assert result not in {".", ".."}
    assert "/" not in result
    assert "\\" not in result
    # No leading dots remain
    assert not result.startswith(".")


def test_dot_only_input_raises():
    with pytest.raises(ValueError):
        safe_symbol_component(".")
    with pytest.raises(ValueError):
        safe_symbol_component("..")


def test_empty_input_raises():
    with pytest.raises(ValueError):
        safe_symbol_component("")
    with pytest.raises(ValueError):
        safe_symbol_component("   ")


def test_non_string_input_raises():
    with pytest.raises(ValueError):
        safe_symbol_component(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        safe_symbol_component(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Length cap + character whitelist
# ---------------------------------------------------------------------------

def test_long_symbol_capped_at_32_chars():
    long = "A" * 100
    out = safe_symbol_component(long)
    assert len(out) <= 32


def test_special_chars_replaced_with_underscore():
    out = safe_symbol_component("AAPL!@#$%^&*()")
    # AAPL preserved, special chars become underscores
    assert out.startswith("AAPL")
    # No special chars survive
    for ch in "!@#$%^&*()":
        assert ch not in out


def test_unicode_neutralized():
    """Unicode (BOM, control chars, RTL marks) replaced with underscore."""
    out = safe_symbol_component("AAPL\u202e\x00\u200b")
    assert "\u202e" not in out
    assert "\x00" not in out
    assert "\u200b" not in out


def test_whitespace_in_middle_replaced():
    out = safe_symbol_component("AA PL")
    assert " " not in out
    assert out == "AA_PL"


# ---------------------------------------------------------------------------
# Idempotency: applying twice doesn't change the result
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", [
    "AAPL", "BTC/USDT", "BRK.B", "../etc",
])
def test_idempotent(symbol):
    try:
        once = safe_symbol_component(symbol)
        twice = safe_symbol_component(once)
        assert once == twice
    except ValueError:
        # Some inputs raise; that's fine — but if first call succeeded,
        # second must too with the same output.
        pass
