"""cs55 — MultiLegPaperReactor must inherit the HERMES_QUANT_PORTFOLIO_CAPS gross
-exposure cap, like the other three reactor classes.

Cap-policy asymmetry (verified by the concurrent review team): of the four reactor
classes ``select_reactor`` dispatches —

  * ``PaperReactor``          -> ``_portfolio_cap_clip`` (HERMES_QUANT_PORTFOLIO_CAPS)
  * ``AlpacaPaperReactor``    -> broker BP / margin
  * ``DeterministicEquityReactor`` -> deterministic buying-power
  * ``MultiLegPaperReactor``  -> NOTHING (no aggregate gross-exposure cap)

The deterministic backend DOES enforce per-order buying power (a net-debit must
fit the account's cash BP), so a giant equity buy is already rejected. But the
AGGREGATE gross-exposure cap (``max_gross_exposure_pct``) is a DIFFERENT control:
it binds on the SUM of |position notional| across the whole book. A net-CREDIT
multi-leg family (CSP / covered call / credit spread) is NOT BP-blocked at all,
and even a BP-affordable family can push the BOOK past the gross cap when existing
positions already consume the headroom. That is the cs55 hole these tests target.

  RED  (pre-fix):  with PORTFOLIO_CAPS=1 and a book already near the 300% gross
                   cap, a new CSP family (BP-clean, net credit) opens UNCLIPPED —
                   bus gets a parent + child fill row(s), state.db is mutated.
  GREEN (post-fix): the same family is silenced (no-fill / silenced parent, bus
                   untouched, state.db untouched), like the equity path's
                   full-silence outcome.
  Flag OFF (default): byte-identical to today — the family fills unclipped and the
                   cap primitive is never even consulted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
)
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.react.multileg import MultiLegPaperReactor
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket
from hermes_quant.state.portfolio_state import PortfolioState


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)


def _near_cap_state_db(tmp_path, monkeypatch, *, gross_fraction: float):
    """A DB-backed PortfolioState whose get_positions reports a book at
    ``gross_fraction`` of NAV. The standard cap is 200% gross
    (``PortfolioCaps.standard().max_gross_exposure_pct == 2.0``), so
    ``gross_fraction=2.05`` is a book already past the gross cap (no remaining
    gross headroom) and ``gross_fraction=0.50`` leaves ample room.

    Mirrors tests/integration/test_all_layers_inherit_cap.py: positions are stored
    as NAV-fraction quantities (ADR-0041), so a single line at ``gross_fraction``
    is a ``gross_fraction``-of-NAV book. apply_execution is left REAL so a fill that
    is NOT silenced actually mutates the DB (proving the RED leak).
    """
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)

    class DummyPos:
        def __init__(self, quantity: float) -> None:
            self.quantity = quantity

    real_get_positions = ps.get_positions

    def _get_positions(account_id):
        # The seeded near-cap line PLUS whatever real fills have landed (so a
        # silenced fill leaves the book at gross_fraction; a leaked fill adds to it).
        out = dict(real_get_positions(account_id))
        out[("equity", "SPY")] = DummyPos(gross_fraction)
        return out

    ps.get_positions = _get_positions  # type: ignore[method-assign]

    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    return ps


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=-0.02, vega=0.10, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


def _admitted_gate(*, bucket, net_greeks, bpr_estimate=0.0) -> OptionsGateResult:
    return OptionsGateResult(
        admitted=True,
        bucket=bucket,
        reason=None,
        net_greeks=net_greeks,
        bpr_estimate=bpr_estimate,
        max_loss=None,
        contracts=1,
        warnings=(),
    )


def _csp(*, outer_qty: int = 1) -> MultiLegProposal:
    """A cash-secured-put family. Net CREDIT (not BP-blocked by the backend). Its
    gross notional is the option premium notional: 3.10 premium × outer_qty × 100.
    At outer_qty=1 that is $310 (~0.3% of the $100k default NAV); a large outer_qty
    is a family whose OWN gross dwarfs the cap."""
    put = OptionLeg(
        symbol="NVDA260626P00130000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(delta=-0.25),
        fill_price=3.10,
    )
    return MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(
            bucket=StructureBucket.CASH_SECURED_PUT,
            net_greeks=NetGreeks(delta=25.0),
            bpr_estimate=13000.0,
        ),
        proposal_id=f"prop_20260530T180000_NVDA_capcsp{outer_qty}",
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="cash_secured_put",
        underlying="NVDA",
        option_legs=(put,),
        stock_leg=None,
        outer_qty=outer_qty,
        net_debit_credit=Decimal("-3.10"),
        max_gain=Decimal("310"),
        breakeven_underlying=(Decimal("126.90"),),
        rationale="csp",
        source_recipe_id="r_csp",
    )


def _read_family(bus):
    if not bus.exists():
        return []
    return [json.loads(ln) for ln in bus.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# RED -> GREEN: a BP-clean over-cap family is silenced when PORTFOLIO_CAPS=1
# --------------------------------------------------------------------------- #
def test_over_cap_family_silenced_when_flag_on(
    enabled, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    # Book already past the 200% gross cap (205% gross => no remaining gross
    # headroom). The CSP family is net-credit so the backend's BP check does NOT
    # stop it; only the aggregate gross cap can. Pre-fix, the family opens
    # regardless; post-fix it is silenced (fail-closed on a breached book).
    ps = _near_cap_state_db(tmp_path, monkeypatch, gross_fraction=2.05)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    silenced = meta.get("no_fill") is True or meta.get("silenced") is True
    assert silenced, (
        "over-cap multi-leg family was NOT silenced by the portfolio cap with "
        "PORTFOLIO_CAPS=1 (cs55 cap-policy asymmetry: multileg has no gross cap)"
    )
    assert parent.fill_size_pct == pytest.approx(0.0)
    reason = str(meta.get("silence_reason", "")) + str(meta.get("broker_status", ""))
    assert "portfolio_cap_" in reason

    # No position-moving family was written to the bus; state.db unchanged (still
    # only the seeded near-cap line, no NVDA option position).
    assert _read_family(bus) == []
    assert ("us_option", "NVDA260626P00130000") not in ps.get_positions(
        "paper-default"
    )


def test_in_cap_family_fills_when_flag_on(enabled, tmp_path, monkeypatch) -> None:
    """A family with ample gross headroom fills normally with the flag ON."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    # Book at 50% gross; CSP adds ~13% => 63% << 300% cap. No false silence.
    ps = _near_cap_state_db(tmp_path, monkeypatch, gross_fraction=0.50)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    assert not meta.get("no_fill")
    assert not meta.get("silenced")
    family = _read_family(bus)
    assert len(family) == 2  # parent + single option child
    assert ps.get_positions("paper-default")[
        ("us_option", "NVDA260626P00130000")
    ].quantity == -1


# --------------------------------------------------------------------------- #
# Flag OFF (default) — byte-identical to today: fills unclipped, cap never consulted
# --------------------------------------------------------------------------- #
def test_over_cap_family_fills_when_flag_off(enabled, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    ps = _near_cap_state_db(tmp_path, monkeypatch, gross_fraction=2.05)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    # Guardrail: the cap primitive must NOT be consulted with the flag OFF.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("cap primitive consulted with PORTFOLIO_CAPS OFF")
            ),
        )
        parent = reactor.execute(_csp(), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    assert not meta.get("no_fill")
    assert not meta.get("silenced")
    family = _read_family(bus)
    assert len(family) == 2  # parent + option child — unclipped fill
    assert ps.get_positions("paper-default")[
        ("us_option", "NVDA260626P00130000")
    ].quantity == -1


# --------------------------------------------------------------------------- #
# A family whose OWN gross dwarfs the cap is silenced even on an EMPTY book.
# --------------------------------------------------------------------------- #
def test_self_oversized_family_silenced_on_empty_book(
    enabled, tmp_path, monkeypatch
) -> None:
    """The most direct cs55 case: a large net-credit CSP family whose own gross
    notional (3.10 × 1600 × 100 = $496k = 496% of $100k NAV) blows past the 200%
    cap by itself, on an otherwise EMPTY book. The backend does NOT BP-block a net
    credit, so pre-fix this opens unclipped; post-fix it is silenced."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(outer_qty=1600), fill_size_pct=0.05)

    meta = parent.reactor_metadata or {}
    assert meta.get("no_fill") is True or meta.get("silenced") is True
    assert parent.fill_size_pct == pytest.approx(0.0)
    assert "portfolio_cap_" in str(meta.get("silence_reason", ""))
    assert _read_family(bus) == []
    assert ps.get_positions("paper-default") == {}


# --------------------------------------------------------------------------- #
# Flag ON but cap NOT breached by an empty book — the common case, no regression.
# --------------------------------------------------------------------------- #
def test_flag_on_empty_book_fills(enabled, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)

    parent = reactor.execute(_csp(), fill_size_pct=0.05)
    meta = parent.reactor_metadata or {}
    assert not meta.get("no_fill")
    assert not meta.get("silenced")
    assert ps.get_positions("paper-default")[
        ("us_option", "NVDA260626P00130000")
    ].quantity == -1
