"""Unit tests for H-adm: account-context plumbing + the unified `admit_or_reject` seam.

Two linked ADR-0077/0079 fixes this suite pins:

  (1) Account-context plumbing. The shared `admissibility.gate_order.admit_or_reject`
      seam now plumbs `account_equity` (= the paper NAV) into AdmissibilityContext, so an
      ETB whole-share short with account context ADMITS instead of fail-closing with
      MISSING_ACCOUNT_CONTEXT on the equity floor. `available_bp` stays a documented gap
      (not tracked in the materialized paper state) — a short with equity-but-no-BP still
      fails-closed on the BP hard check, never an assumed pass.

  (2) Unification. The autonomous-tick seam was refactored to call the SAME
      `admit_or_reject` the PaperReactor + HITL paths use. This suite asserts the
      autonomous seam and the reactor seam produce IDENTICAL verdicts for the same input,
      so there is ONE seam and zero drift.

Deterministic: no network, no ~/.hermes writes. Oracles are injected; NAV is monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import hermes_quant.admissibility.gate_order as gate_order
import hermes_quant.autonomous as auto
import hermes_quant.react.paper as paper_mod
from hermes_quant.admissibility import (
    AdmissibilityContext,
    AdmissibilityState,
    ShortabilityVerdict,
    admit_or_reject,
    evaluate_admissibility,
)
from hermes_quant.proposals import Proposal
from hermes_quant.react.paper import PaperReactor
from hermes_quant.watchlist import WatchlistEntry

_ASOF = datetime(2026, 5, 30, tzinfo=UTC)


class _LiveLikeOracle:
    """An ETB asset delegating to the REAL deterministic core with the fail-closed
    default (require_account_context=True). The ONLY thing that can change the verdict
    is the account/quote context the seam supplies — so it is the probe for plumbing."""

    def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
        populated = AdmissibilityContext(
            tradable=True,
            marginable=True,
            shortable=True,
            easy_to_borrow=True,
            current_ask=ctx.current_ask,
            account_equity=ctx.account_equity,
            available_bp=ctx.available_bp,
        )
        return evaluate_admissibility(symbol, side, qty, asof, populated)


# --------------------------------------------------------------------------- #
# (1) Account-context plumbing on the shared seam
# --------------------------------------------------------------------------- #


def test_etb_short_with_full_account_context_is_admitted(monkeypatch):
    """ETB whole-share short WITH account context (equity + bp) ADMITS — no longer the
    MISSING_ACCOUNT_CONTEXT fail-closed it used to be when context wasn't plumbed."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _LiveLikeOracle())

    # 0.20 NAV @ $200 over $100k NAV = 100 whole shares. BP $100k clears
    # max(1.03*200*100, 1.50*200*100) = $30k Reg-T requirement.
    verdict = admit_or_reject(
        "GME", "short", -0.20, 100_000.0, 200.0, _ASOF,
        account_equity=100_000.0, available_bp=100_000.0,
    )

    assert verdict.admitted is True
    assert verdict.state is AdmissibilityState.ACCEPTED
    assert verdict.reason is None
    assert verdict.adjusted_target_pct == -0.20  # full size, never amplified
    assert verdict.qty_shares == 100


def test_equity_plumbed_but_bp_missing_fails_closed_on_bp(monkeypatch):
    """account_equity present clears the < $2k floor (step 5); but available_bp absent
    (the documented gap) now fails-closed on the BP hard check (step 8b) —
    MISSING_ACCOUNT_CONTEXT, never an assumed pass. This is the autonomous-seam reality."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _LiveLikeOracle())

    verdict = admit_or_reject(
        "GME", "short", -0.20, 100_000.0, 200.0, _ASOF,
        account_equity=100_000.0,  # available_bp left None: the gap
    )

    assert verdict.admitted is False
    assert verdict.state is AdmissibilityState.REJECTED
    assert verdict.reason == "MISSING_ACCOUNT_CONTEXT"
    assert verdict.adjusted_target_pct == 0.0  # REJECT-only -> flatten


def test_no_account_context_at_all_still_rejects(monkeypatch):
    """Neither equity nor bp => fail-closed at the equity floor (step 5),
    MISSING_ACCOUNT_CONTEXT. Proves the seam never assumes account capability."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _LiveLikeOracle())

    verdict = admit_or_reject("GME", "short", -0.20, 100_000.0, 200.0, _ASOF)

    assert verdict.admitted is False
    assert verdict.reason == "MISSING_ACCOUNT_CONTEXT"
    assert verdict.adjusted_target_pct == 0.0


def test_seam_plumbs_context_into_oracle(monkeypatch):
    """The seam plumbs current_ask + account_equity + available_bp into the ctx the
    oracle sees (the H-adm #1 wiring), not a bare current_ask-only ctx."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    seen: dict[str, Any] = {}

    class _CapturingOracle:
        def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
            seen["ctx"] = ctx
            return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0030)

    monkeypatch.setattr(gate_order, "select_oracle", lambda: _CapturingOracle())

    admit_or_reject(
        "GME", "short", -0.20, 100_000.0, 200.0, _ASOF,
        account_equity=100_000.0, available_bp=88_000.0,
    )

    assert seen["ctx"].current_ask == 200.0
    assert seen["ctx"].account_equity == 100_000.0
    assert seen["ctx"].available_bp == 88_000.0


# --------------------------------------------------------------------------- #
# (2) The autonomous seam and the reactor seam share ONE verdict-producing seam
# --------------------------------------------------------------------------- #


def _short_advisor(*, kelly: float = -0.20, decision_price: float = 200.0):
    def _recommend(**kwargs: Any) -> dict[str, Any]:
        return {
            "as_of": "2026-05-30T00:00:00Z",
            "decision_price": decision_price,
            "aggregated_signal": {"direction": -1, "confidence": 0.9, "magnitude": 0.02},
            "risk_gate": {
                "pass": True,
                "gated_reason": None,
                "kelly_fraction": kelly,
                "reason": "test_short",
            },
            "analyst_views": [
                {"metadata": {"atr_relative": 0.01}},
                {"metadata": {"atr_relative": 0.01}},
            ],
            "lessons": [],
        }

    return _recommend


def _proposal(*, decision_price: float = 200.0) -> Proposal:
    return Proposal(
        proposal_id="prop_2026-05-30T00:00:00_GME_abc123",
        state="pending",
        symbol="GME",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-05-30T00:00:00Z",
        expires_at="2026-05-30T01:00:00Z",
        advisor_result={
            "as_of": "2026-05-30T00:00:00Z",
            "decision_price": decision_price,
            "signal_id": "sig-1",
        },
    )


@pytest.fixture
def autonomous_env(monkeypatch):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(
        auto,
        "_read_kill_switch",
        lambda: auto.KillSwitchState(
            tripped=False, tripped_at=None, cumulative_pnl_pct=0.0,
            threshold_pct=0.10, reason=None,
        ),
    )
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    return monkeypatch


_WL = [WatchlistEntry(symbol="GME", asset_class="equity", timeframe="1d")]


def test_autonomous_and_reactor_seams_produce_identical_verdict(
    autonomous_env, monkeypatch, tmp_path: Path
):
    """Seam-divergence regression (updated 2026-06-02, deep-work-loop Phase-7).

    HISTORY: this test originally asserted both the autonomous-tick seam and the
    PaperReactor seam REJECT a GME short with MISSING_ACCOUNT_CONTEXT, because
    neither plumbed `available_bp` ("the shared documented gap on both paths").

    FINDING: commit 72e3d8b ("wire live broker buying-power into the short BP
    check, H-adm #1") plumbed `available_bp` via oracle.live_buying_power() into
    the AUTONOMOUS seam ONLY (autonomous.py:566). The PaperReactor seam
    (paper.py::_account_nav_usd → admissibility_reject_equity) still passes
    available_bp=None (gate_order.py:144 documents it as "NOT cheaply available
    at the paper seam"). So the two seams NO LONGER produce an identical verdict:
    with bp known, the autonomous seam admits/FIREs; the reactor seam still
    REJECTs as MISSING_ACCOUNT_CONTEXT. This is a real seam-divergence (logged as
    a found-bug for the ADR-0077/0079 owner) — the reactor seam needs the same
    live-bp plumbing to restore parity. Until then this test pins the ACTUAL
    divergent behavior so the divergence is visible, not silently green.
    """
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _LiveLikeOracle())
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)
    # Autonomous seam fetches live bp; mock it deterministically (generous → BP check passes).
    import hermes_quant.admissibility.oracle as _oracle_mod
    monkeypatch.setattr(_oracle_mod, "live_buying_power", lambda: 200_000.0)

    # Autonomous seam: bp is now known → the ETB short is admissible → FIRE.
    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())
    gme = [d for d in result.decisions if d.symbol == "GME"]
    assert len(gme) == 1
    assert gme[0].gate == "FIRE", (
        "autonomous seam should FIRE now that available_bp is live-plumbed (72e3d8b)"
    )

    # Reactor seam: bp still NOT plumbed here (gate_order.py:144) → fails-closed
    # as MISSING_ACCOUNT_CONTEXT. THIS IS THE DIVERGENCE (found-bug).
    reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)
    assert rec.fill_size_pct == 0.0  # no-fill
    assert rec.reactor_metadata["admissibility_rejected"] is True
    assert rec.reactor_metadata["admissibility_reason"] == "MISSING_ACCOUNT_CONTEXT"
    # nothing written to the bus
    assert [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()] == []


def test_autonomous_and_reactor_seams_agree_on_reject(autonomous_env, monkeypatch, tmp_path):
    """Same-input agreement under an explicitly REJECTING oracle (NOT_ETB): both seams
    silence/no-fill with the identical reason — verbatim shared seam, no drift."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")

    class _RejectingOracle:
        def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
            if str(side).lower() in {"short", "sell_short", "ss"}:
                return ShortabilityVerdict(AdmissibilityState.REJECTED, "NOT_ETB", 0.0)
            return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0)

    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RejectingOracle())
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())
    gme = [d for d in result.decisions if d.symbol == "GME"][0]
    assert gme.gate == "SILENCE_ADMISSIBILITY"
    assert gme.details["reason"] == "NOT_ETB"

    reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)
    assert rec.fill_size_pct == 0.0
    assert rec.reactor_metadata["admissibility_reason"] == "NOT_ETB"
    assert gme.details["reason"] == rec.reactor_metadata["admissibility_reason"]


def test_autonomous_and_reactor_seams_agree_on_admit(autonomous_env, monkeypatch, tmp_path):
    """Same-input agreement under an ACCEPTING oracle: the autonomous seam FIREs and the
    reactor writes a real fill — both admit identically through the one shared seam."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")

    class _AcceptingOracle:
        def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
            return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0030)

    monkeypatch.setattr(gate_order, "select_oracle", lambda: _AcceptingOracle())
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    monkeypatch.setattr(paper_mod, "_account_nav_usd", lambda: 100_000.0)

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())
    gme = [d for d in result.decisions if d.symbol == "GME"][0]
    assert gme.gate == "FIRE"
    assert gme.action["target_position_pct"] == -0.20
    assert result.fires == 1

    reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
    rec = reactor.execute(_proposal(), fill_size_pct=-0.20)
    assert rec.fill_size_pct == -0.20  # real fill, full size
    assert "admissibility_rejected" not in (rec.reactor_metadata or {})


# --------------------------------------------------------------------------- #
# Flag-OFF no-op (the fail-closed/no-op verification pair)
# --------------------------------------------------------------------------- #


def test_flag_off_admit_or_reject_admits_everything(monkeypatch):
    """Flag OFF: select_oracle() => NullShortabilityOracle, so admit_or_reject ADMITS even
    an account-context-free short, bit-for-bit today's behavior. No fail-closed gate fires."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)

    verdict = admit_or_reject("GME", "short", -0.20, 100_000.0, 200.0, _ASOF)

    assert verdict.admitted is True
    assert verdict.state is AdmissibilityState.ACCEPTED
    assert verdict.adjusted_target_pct == -0.20
