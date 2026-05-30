"""hermes_quant.options.occ — OCC-21 symbol format/parse (ADR-0029 D1).

OCC-21: ROOT(<=6, left-justified, space-padded on the wire but we emit/accept
the compact form) + YYMMDD + {C|P} + STRIKE*1000 zero-padded to 8 digits.

Example: NVDA260526C00145000 == NVDA 2026-05-26 $145.00 Call.

Pure module: no I/O, no network, no global state. Safe on the gate hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

# OCC strike is encoded as strike*1000 zero-padded to 8 digits, so the
# representable strike domain is [0, 1e8 / 1000) = [0, 100_000).
_STRIKE_SCALE = Decimal(1000)
_MAX_STRIKE_INT = 100_000_000  # 8 digits, exclusive upper bound


class OccParseError(ValueError):
    """Raised when a string is not a well-formed OCC-21 symbol."""


@dataclass(frozen=True)
class OccComponents:
    """Parsed OCC-21 components.

    Attributes:
        underlying: Uppercased root, no padding (1-6 alnum chars).
        expiry: Expiration date.
        right: ``"C"`` (call) or ``"P"`` (put).
        strike: Exact strike, e.g. ``Decimal("145.00")``.
    """

    underlying: str
    expiry: date
    right: Literal["C", "P"]
    strike: Decimal


def _validate_root(underlying: str) -> str:
    root = underlying.strip().upper()
    if not (1 <= len(root) <= 6):
        raise OccParseError(
            f"OCC root must be 1-6 chars, got {len(root)} ({underlying!r})"
        )
    if not root.isalnum():
        raise OccParseError(f"OCC root must be alphanumeric, got {underlying!r}")
    return root


def _strike_to_int(strike: Decimal) -> int:
    """Convert a Decimal strike to its zero-padded *1000 integer encoding.

    Raises OccParseError on non-positive, non-representable (sub-tenth-cent),
    or out-of-range strikes.
    """
    if not isinstance(strike, Decimal):  # defensive: float would round wrong
        raise OccParseError(f"strike must be a Decimal, got {type(strike).__name__}")
    if strike <= 0:
        raise OccParseError(f"strike must be > 0, got {strike}")
    scaled = strike * _STRIKE_SCALE
    strike_int = int(scaled.to_integral_value())
    # Round-trip guard: rejects un-representable strikes (e.g. a half-cent like
    # 145.005, whose *1000 has a fractional part). Decimal(strike_int)/1000 must
    # equal the input exactly.
    if Decimal(strike_int) / _STRIKE_SCALE != strike:
        raise OccParseError(
            f"strike {strike} is not representable in OCC (strike*1000 must be "
            f"an exact integer)"
        )
    if not (0 < strike_int < _MAX_STRIKE_INT):
        raise OccParseError(
            f"strike {strike} out of OCC range (strike*1000 must be < 1e8)"
        )
    return strike_int


def format_occ(
    underlying: str,
    expiry: date,
    right: Literal["C", "P"],
    strike: Decimal,
) -> str:
    """Build an OCC-21 symbol. Strike *1000 zero-padded to 8 digits.

    Raises:
        OccParseError: empty/too-long root (>6), non-C/P right,
            non-positive or non-representable strike (strike*1000 must be a
            non-negative integer < 1e8), expiry on a weekend (Alpaca only
            lists Mon-Fri expiries; reject early per ADR-0029 test plan #1).
    """
    root = _validate_root(underlying)
    if right not in ("C", "P"):
        raise OccParseError(f"right must be 'C' or 'P', got {right!r}")
    # Alpaca only lists Mon-Fri expiries; reject weekend expiries at the boundary.
    if expiry.weekday() >= 5:  # 5=Sat, 6=Sun
        raise OccParseError(f"expiry must be a weekday, got {expiry} (weekend)")
    strike_int = _strike_to_int(strike)
    return f"{root}{expiry:%y%m%d}{right}{strike_int:08d}"


def parse_occ(symbol: str) -> OccComponents:
    """Inverse of format_occ. Raises OccParseError on malformed input.

    Accepts both the compact form (no internal spaces) and the
    space-padded 21-char wire form (root left-justified to 6).
    """
    if not isinstance(symbol, str):
        raise OccParseError(f"symbol must be a str, got {type(symbol).__name__}")
    raw = symbol.strip()
    # The wire form is exactly 21 chars with the root left-justified to 6 and
    # space-padded. The trailing 15 chars (YYMMDD + C/P + 8-digit strike) are
    # fixed-width; the root is everything before them.
    if len(raw) < 16:
        raise OccParseError(f"OCC symbol too short: {symbol!r}")
    tail = raw[-15:]
    root_part = raw[:-15].strip()  # strip wire-form right padding
    root = _validate_root(root_part)

    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    right = tail[6]
    strike_digits = tail[7:]

    if right not in ("C", "P"):
        raise OccParseError(f"OCC right must be 'C' or 'P', got {right!r}")
    if not (yy + mm + dd).isdigit():
        raise OccParseError(f"OCC date segment must be digits, got {tail[0:6]!r}")
    if not strike_digits.isdigit():
        raise OccParseError(f"OCC strike segment must be 8 digits, got {strike_digits!r}")

    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError as exc:
        raise OccParseError(f"OCC date is invalid: {tail[0:6]!r} ({exc})") from exc
    if expiry.weekday() >= 5:
        raise OccParseError(f"OCC expiry must be a weekday, got {expiry} (weekend)")

    strike_int = int(strike_digits)
    if not (0 < strike_int < _MAX_STRIKE_INT):
        raise OccParseError(f"OCC strike segment out of range: {strike_digits!r}")
    try:
        strike = (Decimal(strike_int) / _STRIKE_SCALE).normalize()
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise OccParseError(f"OCC strike decode failed: {strike_digits!r}") from exc
    # Re-expand normalized() exponent so e.g. Decimal("1.5E+2") -> Decimal("150").
    if strike == strike.to_integral_value():
        strike = strike.quantize(Decimal(1))

    return OccComponents(
        underlying=root,
        expiry=expiry,
        right=right,  # type: ignore[arg-type]
        strike=strike,
    )
