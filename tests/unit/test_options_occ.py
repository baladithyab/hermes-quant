"""Unit tests for hermes_quant.options.occ — OCC-21 format/parse (Wave B2).

Deterministic, no network. Per plan §2.1.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from hermes_quant.options.occ import (
    OccComponents,
    OccParseError,
    format_occ,
    parse_occ,
)


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_golden_format_nvda() -> None:
    """The research §1.2 golden symbol."""
    assert (
        format_occ("NVDA", date(2026, 5, 26), "C", Decimal("145.00"))
        == "NVDA260526C00145000"
    )


def test_parse_golden() -> None:
    c = parse_occ("NVDA260526C00145000")
    assert c == OccComponents(
        underlying="NVDA", expiry=date(2026, 5, 26), right="C", strike=Decimal("145")
    )
    assert c.strike == Decimal("145.00")


def test_round_trip_fuzz() -> None:
    """parse_occ(format_occ(...)) identity over >=30 cases."""
    roots = ["A", "AA", "SPY", "NVDA", "GOOGL", "BRKB"]
    strikes = [
        Decimal("0.50"),
        Decimal("1"),
        Decimal("2.50"),
        Decimal("145.00"),
        Decimal("145.50"),
        Decimal("9999.999"),
        Decimal("400"),
    ]
    base = date(2026, 1, 5)  # Monday
    count = 0
    for i, root in enumerate(roots):
        for j, strike in enumerate(strikes):
            expiry = _next_weekday(base + timedelta(days=7 * (i + j)))
            right = "C" if (i + j) % 2 == 0 else "P"
            sym = format_occ(root, expiry, right, strike)  # type: ignore[arg-type]
            parsed = parse_occ(sym)
            assert parsed.underlying == root.upper()
            assert parsed.expiry == expiry
            assert parsed.right == right
            assert parsed.strike == strike
            count += 1
    assert count >= 30


def test_wire_form_space_padded_round_trip() -> None:
    """The 21-char space-padded wire form parses identically to the compact form."""
    compact = format_occ("SPY", date(2026, 1, 5), "P", Decimal("400.00"))
    # Build the space-padded wire form: root left-justified to 6.
    wire = "SPY   " + compact[3:]
    assert len(wire) == 21
    assert parse_occ(wire) == parse_occ(compact)


def test_unrepresentable_strike_rejected() -> None:
    """A sub-tenth-cent strike (strike*1000 not an exact integer) is rejected.

    Plan §2.1 names ``145.005`` as the un-representable case; the mechanism the
    plan specifies is the integer round-trip guard ``Decimal(strike*1000)/1000
    == strike``. ``145.005`` *is* representable (145005 is an exact integer), so
    the truly un-representable case is a finer-than-tenth-cent strike like
    ``145.0005`` (=> 145000.5, non-integer). We assert that mechanism.
    """
    with pytest.raises(OccParseError):
        format_occ("NVDA", date(2026, 5, 26), "C", Decimal("145.0005"))


def test_weekend_expiry_rejected() -> None:
    # 2026-05-30 is a Saturday, 2026-05-31 a Sunday.
    with pytest.raises(OccParseError):
        format_occ("NVDA", date(2026, 5, 30), "C", Decimal("145.00"))
    with pytest.raises(OccParseError):
        format_occ("NVDA", date(2026, 5, 31), "C", Decimal("145.00"))


def test_root_too_long_rejected() -> None:
    with pytest.raises(OccParseError):
        format_occ("TOOLONG", date(2026, 1, 5), "C", Decimal("100"))


def test_empty_root_rejected() -> None:
    with pytest.raises(OccParseError):
        format_occ("", date(2026, 1, 5), "C", Decimal("100"))


def test_lowercase_root_normalized_to_upper() -> None:
    assert format_occ("spy", date(2026, 1, 5), "C", Decimal("400")).startswith("SPY")
    assert parse_occ("spy   260105C00400000").underlying == "SPY"


def test_bad_right_rejected() -> None:
    with pytest.raises(OccParseError):
        format_occ("SPY", date(2026, 1, 5), "X", Decimal("400"))  # type: ignore[arg-type]


def test_non_positive_strike_rejected() -> None:
    with pytest.raises(OccParseError):
        format_occ("SPY", date(2026, 1, 5), "C", Decimal("0"))
    with pytest.raises(OccParseError):
        format_occ("SPY", date(2026, 1, 5), "C", Decimal("-5"))


def test_float_strike_rejected() -> None:
    """Floats round wrong at *1000; the API requires Decimal."""
    with pytest.raises(OccParseError):
        format_occ("SPY", date(2026, 1, 5), "C", 145.00)  # type: ignore[arg-type]


def test_malformed_length_rejected() -> None:
    with pytest.raises(OccParseError):
        parse_occ("SHORT")
    with pytest.raises(OccParseError):
        parse_occ("NVDA260526X00145000")  # bad right
    with pytest.raises(OccParseError):
        parse_occ("NVDA2605260014500A")  # non-digit strike


def test_parse_bad_date_rejected() -> None:
    with pytest.raises(OccParseError):
        parse_occ("NVDA261332C00145000")  # month 13, day 32
