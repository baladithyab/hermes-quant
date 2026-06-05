"""Unit tests for MultiLegPaperReactor -> pluggable BrokerBackend wiring (ADR-0088).

The reactor no longer hardcodes ``PaperBroker``; ``_fill`` routes through the
backend that ``select_backend()`` returns — ``DeterministicBackend`` by default
(no Alpaca flag/creds, the CI default), ``AlpacaBackend`` when configured. These
tests verify the wiring on the DEFAULT (deterministic) path, plus the fail-closed
no-fill behavior when a backend raises.

Reuses the proposal-construction fixtures (``_cc`` / ``_csp`` / ``_pmcc`` minted
via the blessed ``MultiLegProposal.from_gate_result`` seam) and the ``enabled`` /
``state_db`` fixtures from ``test_multileg_reactor_fill`` — risk_gate_pass=True is
unrepresentable by direct construction, so we never hand-set it.

Deterministic, no network. ``state_db`` gives a fresh empty PortfolioState whose
buying power falls back to the bootstrap $100k, so the CC equity leg ($16k) and the
PMCC net debit fit BP — preserving the OLD PaperBroker behavior (which did not
enforce BP at all) for these in-bounds plays.
"""

from __future__ import annotations

import json

import pytest

import hermes_quant.react.multileg as mleg_mod
from hermes_quant.react.backend import (
    FillResult,
    InsufficientBuyingPowerError,
)
from hermes_quant.react.backends.deterministic_backend import DeterministicBackend
from hermes_quant.react.multileg import MultiLegPaperReactor
from hermes_quant.state.portfolio_state import PortfolioState

# Reuse the blessed proposal builders from the fill-heart test module (minted via
# MultiLegProposal.from_gate_result — risk_gate_pass=True is never hand-set). The
# enabled/state_db fixtures are defined locally to keep this module's fixtures
# self-contained (avoids the cross-module fixture-import F811 noise).
from tests.unit.test_multileg_reactor_fill import _cc, _csp, _pmcc


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)
    monkeypatch.delenv("HERMES_QUANT_BROKER_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    return ps


def _read_family(bus):
    return [json.loads(ln) for ln in bus.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Default backend is the deterministic simulator (behavior-preserving)
# --------------------------------------------------------------------------- #
def test_default_backend_is_deterministic(enabled, monkeypatch) -> None:
    """With no Alpaca flag/creds (the CI default), select_backend() routes to the
    DeterministicBackend — the local simulator whose fill math mirrors the OLD
    PaperBroker deterministic path (behavior-preserving by default)."""
    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)
    monkeypatch.delenv("HERMES_QUANT_BROKER_BACKEND", raising=False)
    from hermes_quant.react.backend import select_backend

    backend = select_backend()
    assert isinstance(backend, DeterministicBackend)
    assert backend.name == "deterministic"


# --------------------------------------------------------------------------- #
# (a) mleg fill produces the right per-leg children through the backend
# --------------------------------------------------------------------------- #
def test_mleg_fills_through_backend_per_leg_children(
    enabled, state_db, tmp_path
) -> None:
    """A PMCC (>=2 option legs -> mleg path) routed through the deterministic
    backend produces ONE child per leg with the correct signed contracts and
    OCC symbols (the FillResult.legs -> LegFill expansion)."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _pmcc()
    parent = reactor.execute(p, fill_size_pct=0.05)

    assert parent.asset_class == "multi_leg"
    # PMCC is a net DEBIT (positive) — the parent fill_price preserves the sign.
    assert parent.fill_price == pytest.approx(44.50)

    family = _read_family(bus)
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    assert len(children) == 2  # one child per option leg
    assert all(c["asset_class"] == "us_option" for c in children)

    by_sym = {c["asset"]: c["reactor_metadata"]["quantity"] for c in children}
    # LEAPS long call +1, near-dated short call -1 (signed TRUE contracts).
    assert by_sym["NVDA271217C00120000"] == pytest.approx(1.0)
    assert by_sym["NVDA260703C00180000"] == pytest.approx(-1.0)

    # State reconciles the per-leg children (parent is an audit rollup).
    positions = state_db.get_positions("paper-default")
    assert positions[("us_option", "NVDA271217C00120000")].quantity == 1
    assert positions[("us_option", "NVDA260703C00180000")].quantity == -1


# --------------------------------------------------------------------------- #
# (b) CC (single option + stock leg) fills both legs through the backend
# --------------------------------------------------------------------------- #
def test_cc_fills_option_and_equity_through_backend(
    enabled, state_db, tmp_path
) -> None:
    """A covered call routes the short call through submit_option_single and the
    +100 equity leg through submit_equity (a SEPARATE order) — both fill via the
    deterministic backend and produce two children."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    p = _cc()
    parent = reactor.execute(p, fill_size_pct=0.05)

    assert parent.fill_price < 0  # net credit (short call premium)

    family = _read_family(bus)
    children = [r for r in family if r["reactor_metadata"]["role"] != "parent"]
    assert len(children) == 2
    eq = next(c for c in children if c["asset_class"] == "equity")
    opt = next(c for c in children if c["asset_class"] == "us_option")
    assert eq["reactor_metadata"]["quantity"] == 100  # +100 shares (long)
    assert eq["fill_price"] == pytest.approx(160.0)  # equity decision basis
    assert opt["reactor_metadata"]["quantity"] == -1  # short 1 call
    assert opt["fill_price"] == pytest.approx(4.50)  # option passthrough mid

    positions = state_db.get_positions("paper-default")
    assert positions[("equity", "NVDA")].quantity == 100
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1


# --------------------------------------------------------------------------- #
# (c) a backend BP-rejection yields a no-fill parent, NOT a crash
# --------------------------------------------------------------------------- #
def test_backend_bp_rejection_yields_nofill_not_crash(
    enabled, state_db, tmp_path, monkeypatch
) -> None:
    """A backend that RAISES InsufficientBuyingPowerError (the deterministic
    backend's fail-closed BP refusal, or a real broker BP reject) must be
    converted to a no-fill parent record — the reactor NEVER crashes and NEVER
    fabricates a fill, and writes nothing to the bus / state."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    # Monkeypatch select_backend to return a backend whose single-leg submit raises
    # InsufficientBuyingPowerError (a CSP is the single-leg path).
    class _BpRejectBackend:
        name = "deterministic"

        def submit_option_single(self, leg, *, qty, limit_price, client_order_id):
            raise InsufficientBuyingPowerError(
                "simulated BP refusal — not enough cash for the option"
            )

        def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id):
            raise AssertionError("equity submit should not be reached on a CSP")

        def submit_option_mleg(
            self, option_legs, *, outer_qty, net_limit_price, client_order_id
        ):
            raise AssertionError("mleg submit should not be reached on a CSP")

    monkeypatch.setattr(mleg_mod, "select_backend", lambda: _BpRejectBackend())

    # Must NOT raise — the BP exception is converted to a no-fill parent.
    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.fill_price == 0.0
    assert parent.fill_size_pct == 0.0
    # Fail-closed: nothing written, no positions (never fabricate a fill).
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


def test_backend_bp_rejection_on_mleg_yields_nofill(
    enabled, state_db, tmp_path, monkeypatch
) -> None:
    """The same fail-closed conversion applies on the mleg path: a BP raise from
    submit_option_mleg becomes a no-fill parent, not a crash."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    class _BpRejectMlegBackend:
        name = "deterministic"

        def submit_option_mleg(
            self, option_legs, *, outer_qty, net_limit_price, client_order_id
        ):
            raise InsufficientBuyingPowerError("simulated mleg BP refusal")

        def submit_option_single(self, leg, *, qty, limit_price, client_order_id):
            raise AssertionError("single submit should not be reached on a PMCC")

        def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id):
            raise AssertionError("equity submit should not be reached on a PMCC")

    monkeypatch.setattr(mleg_mod, "select_backend", lambda: _BpRejectMlegBackend())

    parent = reactor.execute(_pmcc(), fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


# --------------------------------------------------------------------------- #
# A backend that returns a reject STATUS (not a raise) also yields a no-fill
# --------------------------------------------------------------------------- #
def test_backend_reject_status_yields_nofill(
    enabled, state_db, tmp_path, monkeypatch
) -> None:
    """A backend FillResult with a reject status (rejected/expired) -> no-fill
    parent (the _guard_result reject semantics, mirroring the old _guard_fill)."""
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    def _expired(self, leg, *, qty, limit_price, client_order_id):
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=0.0,
            filled_qty=0.0,
            status="expired",
            position_intent=leg.position_intent,
            source="deterministic",
        )

    monkeypatch.setattr(DeterministicBackend, "submit_option_single", _expired)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}
