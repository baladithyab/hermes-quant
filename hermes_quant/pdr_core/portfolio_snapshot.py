"""hermes_quant.pdr_core.portfolio_snapshot — the canonical host-agnostic
NAV-fraction portfolio read-projection (ADR-0092, ra06).

The Stage-2 portfolio-aware sizer (``risk.portfolio_normalize``) reads a tiny
read-only snapshot of the current book: a ``positions`` map of NAV fractions plus
three derived reads (gross / net / cash exposure). That snapshot carries NO host
types and NO infra — it is the host-blind PERCEPTION-of-the-book the core sizer
needs, and ADR-0092 wants it owned by the core (alongside the gate read-view
``gate_types.CorePortfolio``, which is the SEPARATE per-(account, asset_class)
drawdown/daily-loss view).

This module lifts that snapshot into the core as :class:`CorePortfolioSnapshot`.
``risk.portfolio_normalize.PortfolioState`` is then a thin SUBCLASS of it (the
host-named shim), so every existing consumer is byte-identical — the field, the
three properties, the frozen-at-binding semantics, and the in-place-mutable inner
dict are all inherited unchanged. A parity test
(``tests/pdr_core/test_portfolio_snapshot_parity.py``) proves the two agree on the
AAPL/BA fixture and the over-leveraged matrix.

Design notes (the byte-identical invariants the parity test pins):

  - ``positions`` is a PLAIN MUTABLE ``dict`` with ``field(default_factory=dict)``,
    NOT a frozen ``Mapping``. The autonomous tick MUTATES this dict IN PLACE
    between picks (``autonomous.py:880`` — ``portfolio_state.positions[sym] = ...``)
    so each subsequent pick sees consumed headroom. The dataclass is frozen at the
    FIELD BINDING (you cannot rebind ``.positions``), never at the dict contents.
    A "frozen dict" projection would break that consumer — so we deliberately do
    NOT freeze the contents.

  - ``positions`` keys are OPAQUE. The live consumers use heterogeneous key types:
    bare ``str`` symbols (``portfolio/state.py``), ``(asset_class, symbol)`` tuples
    (``react/paper.py`` cs60), and ``f"{asset_class}\\x1f{symbol}"`` joined strings
    (``react/multileg.py`` cs65). The three properties sum over ``.values()`` only,
    so this type performs NO key validation and accepts all of them identically.
    The field is annotated ``dict[str, float]`` only to mirror the historical
    annotation; the runtime is key-opaque.

  - ``cash_pct`` MAY be negative when the book is over-leveraged (the 860%-gross
    forensic case ADR-0071 was written to catch). The sizer fails closed on that.

PURITY (ADR-0092, ``tests/pdr_core/test_contract_purity.py``): stdlib only
(``dataclasses``). No host / infra / governance / state import.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CorePortfolioSnapshot:
    """Host-agnostic read-only NAV-fraction snapshot of the current book.

    ``positions`` maps a position key -> signed ``target_position_pct`` of NAV (the
    LATEST target per key, NOT delta-summed — re-affirming a target supersedes it).
    Keys are opaque (see module docstring). The three derived reads:

      - ``gross_exposure_pct`` — ``sum(|p|)``; total absolute size deployed.
      - ``net_exposure_pct``   — ``sum(p)``; signed directional exposure.
      - ``cash_pct``           — ``1 - gross``; implied free cash sleeve. MAY be
        negative when over-leveraged (the sizer fails closed in that case).

    Frozen at the field binding; the inner ``positions`` dict stays mutable in
    place so a streaming caller (the autonomous tick) can consume headroom between
    picks without reconstructing the wrapper.
    """

    positions: dict[str, float] = field(default_factory=dict)

    @property
    def gross_exposure_pct(self) -> float:
        return sum(abs(p) for p in self.positions.values())

    @property
    def net_exposure_pct(self) -> float:
        return sum(self.positions.values())

    @property
    def cash_pct(self) -> float:
        return 1.0 - self.gross_exposure_pct
