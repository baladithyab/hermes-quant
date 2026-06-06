"""Unit tests for the ADR-0077 / ADR-0079 admissibility PRECONDITION in PaperReactor.

Meta-review M05: admissibility previously gated ONLY the autonomous-tick decision seam.
The HITL `quant_approve` path and PaperReactor itself were NOT admissibility-aware, so with
`HERMES_QUANT_ADMISSIBILITY=1` an inadmissible short still EXECUTED on paper through
`PaperReactor.execute()`. ADR-0079 places admissibility at the REACTION layer (the reactor)
as a precondition. This suite pins that seam:

  * flag OFF  => the reactor executes EXACTLY as today (bit-identical record + bus line + ONE
                 line written), the oracle / NAV lookup is never touched.
  * flag ON + inadmissible short => NO fill written to the bus + a no-fill audit record whose
                 reactor_metadata carries the verdict reason.
  * flag ON + admissible short   => executes normally (a real fill is written).
  * flag ON + LONG (or non-equity) => executes normally (admissibility constrains shorts only).

Deterministic: the executions bus is a tmp file, the oracle is injected, NAV is monkeypatched.
No network, no ~/.hermes writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import hermes_quant.admissibility.gate_order as gate_order
import hermes_quant.react.paper as paper_mod
from hermes_quant.admissibility import (
    AdmissibilityContext,
    AdmissibilityState,
    ShortabilityVerdict,
)
from hermes_quant.proposals import Proposal
from hermes_quant.react.paper import PaperReactor

# --------------------------------------------------------------------------- #
# Fakes / builders
# --------------------------------------------------------------------------- #


class _RejectingOracle:
    """Always REJECTs a short (NOT_ETB); ACCEPTs longs. Used to prove the inadmissible
    short produces a no-fill regardless of ctx."""

    def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
        if str(side).lower() in {"short", "sell_short", "ss"}:
            return ShortabilityVerdict(AdmissibilityState.REJECTED, "NOT_ETB", 0.0)
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)


class _AcceptingOracle:
    """Always ACCEPTs (ETB whole-share short). Records the qty it saw."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
        self.calls.append({"symbol": symbol, "side": side, "qty": qty, "ctx": ctx})
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0030)


def _proposal(
    *,
    symbol: str = "GME",
    asset_class: str = "equity",
    decision_price: float = 200.0,
) -> Proposal:
    return Proposal(
        proposal_id=f"prop_2026-05-30T00:00:00_{symbol}_abc123",
        state="pending",
        symbol=symbol,
        asset_class=asset_class,
        timeframe="1d",
        created_at="2026-05-30T00:00:00Z",
        expires_at="2026-05-30T01:00:00Z",
        advisor_result={
            "as_of": "2026-05-30T00:00:00Z",
            "decision_price": decision_price,
            "signal_id": "sig-1",
        },
    )


@pytest.fixture()
def reactor(tmp_path: Path) -> PaperReactor:
    return PaperReactor(executions_path=tmp_path / "executions.jsonl")


def _bus_lines(reactor: PaperReactor) -> list[str]:
    return [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Flag OFF: bit-for-bit no-op
# --------------------------------------------------------------------------- #


def test_flag_off_short_executes_bit_identical(reactor, monkeypatch):
    """Flag OFF: a SHORT equity fill executes EXACTLY as today. The gate is never
    consulted (oracle/NAV untouched) and a normal fill lands on the bus."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    # Slippage now DEFAULTS to v0.2 (FLAGS.md Tier-A); pin v0.1 so this test keeps
    # asserting the legacy passthrough fill (fill_price == decision_price). The
    # subject here is the admissibility seam, not slippage.
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")

    def _boom_select():
        raise AssertionError("select_oracle must NOT be called when the flag is OFF")

    def _boom_nav():
        raise AssertionError("NAV lookup must NOT run when the flag is OFF")

    monkeypatch.setattr(gate_order, "select_oracle", _boom_select)
    monkeypatch.setattr(paper_mod, "_account_nav_usd", _boom_nav)

    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)

    # A real fill: full size, fill_price == decision_price (legacy passthrough), on the bus.
    assert rec.fill_size_pct == -0.20
    assert rec.fill_price == 200.0
    assert rec.decision_price == 200.0
    assert rec.reactor_metadata.get("admissibility_rejected") is None
    lines = _bus_lines(reactor)
    assert len(lines) == 1  # exactly one fill written


def test_flag_off_matches_pre_gate_record(reactor, monkeypatch):
    """The flag-OFF record is byte-identical to a reactor whose admissibility code never
    runs: every persisted field except the wall-clock asof_execution matches."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    # Pin the v0.1 off-switch: this test asserts the persisted record is byte-identical
    # to a no-slippage passthrough (fill_price == decision_price, slippage_model v0.1).
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    rec = reactor.execute(_proposal(), fill_size_pct=-0.20, approver_user_id="u1")

    from hermes_quant.react.paper import _record_to_dict

    d = _record_to_dict(rec)
    # The fields the slippage/admissibility seams could perturb are all at their legacy values.
    assert d["fill_size_pct"] == -0.20
    assert d["fill_price"] == 200.0
    assert d["target_position_pct"] == -0.20
    assert d["reactor_metadata"]["slippage_model"] == "v0.1"
    assert "admissibility_rejected" not in d["reactor_metadata"]


# --------------------------------------------------------------------------- #
# Flag ON: inadmissible short => NO fill + audit reason
# --------------------------------------------------------------------------- #


def test_flag_on_inadmissible_short_no_fill_and_audit(reactor, monkeypatch):
    """Flag ON + REJECT: NO fill is written to the bus, and the returned record is a
    no-fill (fill_size_pct=0.0) carrying the verdict reason in reactor_metadata."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RejectingOracle())

    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)

    # No-fill record.
    assert rec.fill_size_pct == 0.0
    assert rec.fill_price == 0.0
    assert rec.target_position_pct == -0.20  # what was requested, for the audit
    # Audit reason present.
    assert rec.reactor_metadata["admissibility_rejected"] is True
    assert rec.reactor_metadata["admissibility_state"] == "REJECTED"
    assert rec.reactor_metadata["admissibility_reason"] == "NOT_ETB"
    assert rec.reactor_metadata["requested_target_pct"] == -0.20

    # NOTHING written to the executions bus — the inadmissible short did not execute.
    assert _bus_lines(reactor) == []


def test_flag_on_fail_closed_when_nav_missing(reactor, monkeypatch):
    """Flag ON + missing NAV => 0 shares => the live core REJECTs (FRACTIONAL/UNKNOWN);
    fail-closed: no fill, never an assumed-admissible execution. Uses the REAL core oracle."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: None)  # NAV unknown

    from hermes_quant.admissibility import evaluate_admissibility

    class _RealCoreOracle:
        def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
            populated = AdmissibilityContext(
                tradable=True,
                marginable=True,
                shortable=True,
                easy_to_borrow=True,
                current_ask=ctx.current_ask,
            )
            return evaluate_admissibility(symbol, side, qty, asof, populated)

    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RealCoreOracle())

    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)

    assert rec.fill_size_pct == 0.0
    assert rec.reactor_metadata["admissibility_rejected"] is True
    assert rec.reactor_metadata["admissibility_qty_shares"] == 0  # no shares valued
    assert _bus_lines(reactor) == []


# --------------------------------------------------------------------------- #
# Flag ON: admissible short / long => executes normally
# --------------------------------------------------------------------------- #


def test_flag_on_admissible_short_executes(reactor, monkeypatch):
    """Flag ON + ACCEPTED short: executes normally; the oracle saw a WHOLE-SHARE qty
    (0.20*100k/200 = 100 shares), not the NAV fraction; a real fill lands on the bus."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    # Pin v0.1: this test asserts the admissible short fills at the unslipped
    # decision price (fill_price == 200.0). Slippage is not its subject.
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)
    oracle = _AcceptingOracle()
    monkeypatch.setattr(gate_order, "select_oracle", lambda: oracle)

    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)

    assert rec.fill_size_pct == -0.20  # full fill
    assert rec.fill_price == 200.0
    assert "admissibility_rejected" not in (rec.reactor_metadata or {})
    assert len(_bus_lines(reactor)) == 1  # the fill was written

    # The oracle received a whole-share integer, not the 0.20 fraction.
    assert len(oracle.calls) == 1
    assert oracle.calls[0]["qty"] == 100
    assert isinstance(oracle.calls[0]["qty"], int)
    assert oracle.calls[0]["side"] == "short"


def test_flag_on_long_executes_without_gate(reactor, monkeypatch):
    """Flag ON + LONG: admissibility constrains shorts only. A long fill executes
    normally and the oracle/NAV lookup is never consulted (long path is a no-op too)."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")

    def _boom_select():
        raise AssertionError("select_oracle must NOT be called for a LONG order")

    def _boom_nav():
        raise AssertionError("NAV lookup must NOT run for a LONG order")

    monkeypatch.setattr(gate_order, "select_oracle", _boom_select)
    monkeypatch.setattr(paper_mod, "_account_nav_usd", _boom_nav)

    rec = reactor.execute(_proposal(), fill_size_pct=+0.10)

    assert rec.fill_size_pct == +0.10
    assert len(_bus_lines(reactor)) == 1


def test_flag_on_non_equity_short_executes_without_gate(reactor, monkeypatch):
    """Flag ON + non-equity (crypto) short: the equity short-admissibility predicate
    does not apply; the fill executes and the oracle/NAV lookup is never consulted."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")

    def _boom_select():
        raise AssertionError("select_oracle must NOT be called for a non-equity order")

    monkeypatch.setattr(gate_order, "select_oracle", _boom_select)
    monkeypatch.setattr(
        paper_mod,
        "_account_nav_usd",
        lambda: (_ for _ in ()).throw(AssertionError("NAV must not run for non-equity")),
    )

    rec = reactor.execute(
        _proposal(symbol="BTC/USDT", asset_class="crypto"), fill_size_pct=-0.20
    )

    assert rec.fill_size_pct == -0.20
    assert len(_bus_lines(reactor)) == 1
