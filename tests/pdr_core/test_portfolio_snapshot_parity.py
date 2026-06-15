"""ADR-0092 ra06: parity between the canonical pdr_core NAV-fraction read-projection
(``CorePortfolioSnapshot``) and the live sizer snapshot
(``risk.portfolio_normalize.PortfolioState``).

ra06 collapses the SIZER snapshot — ``risk/portfolio_normalize.py:101
PortfolioState`` — onto a frozen, host-blind canonical type in pdr_core. Unlike the
gate read-view (already lifted to ``gate_types.CorePortfolio``) this is the type the
Stage-2 sizer CONSUMES, so the slice must be byte-identical: a NEW frozen
``CorePortfolioSnapshot`` in pdr_core + ``PortfolioState`` made a THIN SUBCLASS of it.

Three contracts proven here:

  1. STRUCTURAL IDENTITY — ``CorePortfolioSnapshot`` exposes EXACTLY the one field
     (``positions``) and the THREE properties (``gross_exposure_pct`` /
     ``net_exposure_pct`` / ``cash_pct``) that the sizer reads, with the SAME
     arithmetic, on the SAME fixtures — incl. the AAPL/BA fixture the ra06 task
     pins and the 860%-gross over-leveraged forensic case.

  2. SHIM — ``risk.portfolio_normalize.PortfolioState`` IS a subclass of
     ``CorePortfolioSnapshot`` (the migration handle), still frozen, and its inner
     ``positions`` dict stays IN-PLACE MUTABLE (the autonomous.py:880 tick invariant
     — the dataclass is frozen at the FIELD binding, not the dict contents).

  3. KEY-OPACITY — the projection sums over ``.values()`` only, so it accepts the
     heterogeneous key types the live consumers use (bare ``str`` from
     portfolio/state, ``tuple[str, str]`` from react/paper cs60, ``\\x1f``-joined
     ``str`` from react/multileg cs65) with NO key validation.

RED-first: with ``CorePortfolioSnapshot`` absent the import below fails at
collection -> every test errors. Creating the module turns GREEN.
"""

from __future__ import annotations

import dataclasses

import pytest

# --- the canonical core type under construction (new home) -----------------
from hermes_quant.pdr_core.portfolio_snapshot import CorePortfolioSnapshot

# --- the live sizer snapshot (parity oracle) -------------------------------
from hermes_quant.risk.portfolio_normalize import PortfolioState

# ---------------------------------------------------------------------------
# Fixture matrix — the SAME books fed through both types.
# ---------------------------------------------------------------------------

# The AAPL/BA fixture the ra06 task pins (the true paper-default book from
# test_reader_partition_cross_account_pooling.py:110).
AAPL_BA = {"AAPL": 0.05, "BA": -0.20}

_FIXTURES = [
    pytest.param({}, id="empty"),
    pytest.param(AAPL_BA, id="aapl_ba"),
    pytest.param({"AAPL": 0.20, "MSFT": -0.10, "GOOG": 0.05}, id="mixed_long_short"),
    pytest.param({"EXIST": 0.75}, id="single_long"),
    pytest.param({"EXIST_SHORT": -0.95}, id="single_short"),
    # the 5/28 forensic 860%-gross over-leveraged case (negative cash).
    pytest.param(
        {**{f"SYM{i}": -0.20 for i in range(38)}, **{f"LONG{i}": 0.20 for i in range(5)}},
        id="overleveraged_860pct",
    ),
]


@pytest.mark.parametrize("positions", _FIXTURES)
def test_gross_net_cash_parity(positions: dict[str, float]) -> None:
    """The core projection and the live sizer snapshot agree EXACTLY on the three
    derived reads over the whole fixture matrix (incl. AAPL/BA + over-leveraged)."""
    core = CorePortfolioSnapshot(positions=dict(positions))
    live = PortfolioState(positions=dict(positions))

    assert core.gross_exposure_pct == live.gross_exposure_pct
    assert core.net_exposure_pct == live.net_exposure_pct
    assert core.cash_pct == live.cash_pct
    # and the underlying positions round-trip unchanged.
    assert core.positions == live.positions == positions


def test_aapl_ba_exact_values() -> None:
    """Pin the AAPL/BA fixture's exact arithmetic on the CORE type (the ra06 gate)."""
    core = CorePortfolioSnapshot(positions=dict(AAPL_BA))
    assert abs(core.gross_exposure_pct - 0.25) < 1e-12  # 0.05 + 0.20
    assert abs(core.net_exposure_pct - (-0.15)) < 1e-12  # 0.05 - 0.20
    assert abs(core.cash_pct - 0.75) < 1e-12  # 1 - 0.25


def test_empty_defaults_to_full_cash() -> None:
    """No-arg construction is an empty book = 100% cash (the reconstruct empty path)."""
    core = CorePortfolioSnapshot()
    assert core.gross_exposure_pct == 0.0
    assert core.net_exposure_pct == 0.0
    assert core.cash_pct == 1.0


# ---------------------------------------------------------------------------
# Contract 2 — the SHIM: subclass identity, frozen, in-place-mutable dict.
# ---------------------------------------------------------------------------


def test_portfolio_state_is_core_subclass() -> None:
    """The live ``PortfolioState`` IS a ``CorePortfolioSnapshot`` (the migration
    handle the consumer-migration sequence keys on)."""
    assert issubclass(PortfolioState, CorePortfolioSnapshot)
    inst = PortfolioState(positions=dict(AAPL_BA))
    assert isinstance(inst, CorePortfolioSnapshot)
    # name + repr preserved (a bare alias would change repr to the core name).
    assert type(inst).__name__ == "PortfolioState"
    assert repr(inst).startswith("PortfolioState(")


def test_both_types_are_frozen_at_field_binding() -> None:
    """Both are frozen dataclasses: rebinding ``.positions`` raises (the field
    binding is frozen)."""
    for cls in (CorePortfolioSnapshot, PortfolioState):
        inst = cls(positions={"A": 0.1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.positions = {}  # type: ignore[misc]


def test_positions_dict_is_in_place_mutable() -> None:
    """The autonomous.py:880 tick invariant: the inner ``positions`` dict is
    mutated IN PLACE between picks (frozen at the binding, not the contents). Both
    types must preserve this or the autonomous caps gate breaks."""
    for cls in (CorePortfolioSnapshot, PortfolioState):
        inst = cls(positions={"AAPL": 0.05})
        inst.positions["BA"] = -0.20  # the exact autonomous mutation pattern
        assert abs(inst.gross_exposure_pct - 0.25) < 1e-12
        assert abs(inst.net_exposure_pct - (-0.15)) < 1e-12


# ---------------------------------------------------------------------------
# Contract 3 — KEY-OPACITY: heterogeneous key types the live consumers use.
# ---------------------------------------------------------------------------


def test_accepts_tuple_keys_react_paper_cs60() -> None:
    """react/paper.py keys pos_map on ``(asset_class, symbol)`` tuples (cs60). The
    projection sums over ``.values()`` only, so tuple keys are accepted and the
    gross/net are identical to the bare-symbol equivalent."""
    tuple_book = {("equity", "AAPL"): 0.05, ("us_option", "AAPL"): -0.20}
    core = CorePortfolioSnapshot(positions=tuple_book)
    assert abs(core.gross_exposure_pct - 0.25) < 1e-12
    assert abs(core.net_exposure_pct - (-0.15)) < 1e-12


def test_accepts_separator_joined_keys_react_multileg_cs65() -> None:
    """react/multileg.py keys pos_map on ``f'{asset_class}\\x1f{symbol}'`` (cs65)."""
    sep_book = {"equity\x1fAAPL": 0.05, "us_option\x1fAAPL": -0.20}
    core = CorePortfolioSnapshot(positions=sep_book)
    assert abs(core.gross_exposure_pct - 0.25) < 1e-12


# ---------------------------------------------------------------------------
# Contract 4 — the canonical type's SHAPE matches the live one exactly.
# ---------------------------------------------------------------------------


def test_field_and_property_shape_identical() -> None:
    """The core type has the SAME single field and the SAME three properties as
    the live sizer snapshot — no wider, no narrower."""
    core_fields = {f.name for f in dataclasses.fields(CorePortfolioSnapshot)}
    live_fields = {f.name for f in dataclasses.fields(PortfolioState)}
    assert core_fields == live_fields == {"positions"}

    for prop in ("gross_exposure_pct", "net_exposure_pct", "cash_pct"):
        assert isinstance(getattr(CorePortfolioSnapshot, prop), property)
        # the subclass inherits the SAME property object (no override).
        assert getattr(PortfolioState, prop) is getattr(CorePortfolioSnapshot, prop)
