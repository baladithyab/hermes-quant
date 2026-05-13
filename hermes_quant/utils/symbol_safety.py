"""hermes_quant.utils.symbol_safety — Symbol path-safety guard (ADR-0005).

Per ADR-0005 amendment 2026-05-13 (Wave C.2): when symbols flow into
filesystem paths (cache files, JSONL filenames, log paths), they MUST
pass through this whitelist first. Pattern stolen from
TauricResearch/TradingAgents §"safe_symbol_component" — prevents the
attack class where a user-supplied ticker like `"../../etc/passwd"`
escapes the cache directory.

The function is conservative: only ASCII alphanumerics + a few crypto
separators (`/`, `-`, `_`, `.`) are allowed. Other characters are
replaced with `_`. Length is capped at 32. Empty / whitespace-only
input raises ValueError.

We replace `/` with `_` because crypto pairs like `BTC/USDT` are common
ticker shapes but `/` is a path separator. Callers that want the
original symbol for display can keep it; this is for the FILESYSTEM
component only.
"""
from __future__ import annotations

import re

_SAFE_CHARS = re.compile(r"[A-Za-z0-9._-]")
_MAX_LEN = 32


def safe_symbol_component(symbol: str) -> str:
    """Return a filesystem-safe component derived from `symbol`.

    Args:
        symbol: arbitrary user-supplied or wire-supplied ticker string.

    Returns:
        ASCII-clean string ≤32 chars. `/`, whitespace, and any non
        `[A-Za-z0-9._-]` character is replaced with `_`. Leading dots
        are stripped to prevent `.htaccess`-style hidden-file traps.

    Raises:
        ValueError: if `symbol` is empty, whitespace-only, or `..` /
            `.` after sanitization (the path-traversal attack class).
    """
    if not isinstance(symbol, str):
        raise ValueError(
            f"symbol must be str, got {type(symbol).__name__}"
        )
    s = symbol.strip()
    if not s:
        raise ValueError("symbol must be non-empty")

    # Replace each non-safe char with underscore; keep safe chars as-is.
    cleaned = "".join(c if _SAFE_CHARS.match(c) else "_" for c in s)

    # Strip leading dots (prevents `.htaccess`, `.ssh`, etc. traps and
    # collapses `..` to `__`).
    cleaned = cleaned.lstrip(".")

    # Cap length AFTER cleaning so we don't accidentally produce a
    # truncated `..` from a long traversal-style input.
    if len(cleaned) > _MAX_LEN:
        cleaned = cleaned[:_MAX_LEN]

    # Final guard: refuse if cleaning produced empty or pure-traversal.
    if not cleaned:
        raise ValueError(
            f"symbol {symbol!r} sanitizes to empty string"
        )
    if cleaned in {".", ".."}:
        raise ValueError(
            f"symbol {symbol!r} resolves to traversal token {cleaned!r}"
        )

    return cleaned
