"""Unit tests for the ADR-0077 admissibility UNIT bridge in the autonomous seam.

The bug (Codex Facets 1+3, build-review — triple-confirmed): autonomous.py passed
`abs(effective_size)` — a NAV FRACTION like 0.20 — as the SHARE `qty` to
`oracle.verdict(...)`. Under the live AlpacaShortabilityOracle the whole-share check
then rejected EVERY short as FRACTIONAL_SHORT, so flipping HERMES_QUANT_ADMISSIBILITY=1
would have silenced ALL shorts.

These tests drive the REAL `hermes_quant.autonomous.tick()` through the FIRE -> admissibility
seam with an INJECTED fake oracle, asserting:
  - flag ON + ETB whole-share short: oracle receives a WHOLE-SHARE integer qty (not the
    fraction) and the short is NOT silenced as FRACTIONAL_SHORT (it FIREs).
  - fail-closed: missing NAV (or price) yields 0 shares + REJECT, never an assumed pass.
  - flag OFF: the whole block is skipped — bit-for-bit no-op (oracle/NAV never touched).

Deterministic; no network, no ~/.hermes writes (dry_run=True, mocked NAV/mode).
"""

from __future__ import annotations

from typing import Any

import pytest

import hermes_quant.admissibility.gate_order as gate_order
import hermes_quant.autonomous as auto
from hermes_quant.admissibility import (
    AdmissibilityContext,
    AdmissibilityState,
    ShortabilityVerdict,
    evaluate_admissibility,
)
from hermes_quant.watchlist import WatchlistEntry

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _RecordingOracle:
    """Records the qty passed to verdict() and always ACCEPTs. The recorded qty
    is the regression probe: it MUST be a whole-share integer, never a fraction."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def verdict(self, symbol, side, qty, asof, ctx):  # noqa: ANN001
        self.calls.append({"symbol": symbol, "side": side, "qty": qty, "ctx": ctx})
        return ShortabilityVerdict(AdmissibilityState.ACCEPTED, None, 0.0030)


class _RealCoreOracle:
    """Delegates to the shared deterministic core with a fully-ETB asset, so the
    ONLY thing that can reject is the qty/ctx the seam supplies. Used to prove
    fail-closed behavior on missing NAV/price."""

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


def _short_advisor(*, kelly: float = -0.20, decision_price: float = 200.0):
    """An advisor_recommend stub that yields a high-conviction SHORT which clears
    the silence-bias gate (conf 0.9, 2 voices, urgency >> 0.5) and carries a
    top-level decision_price for the share conversion."""

    def _recommend(**kwargs: Any) -> dict[str, Any]:
        return {
            "as_of": "2026-05-30T00:00:00Z",
            "decision_price": decision_price,
            "aggregated_signal": {
                "direction": -1,
                "confidence": 0.9,
                "magnitude": 0.02,
            },
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


@pytest.fixture
def autonomous_env(monkeypatch, tmp_path):
    """Put the orchestrator into a state where a FIRE can reach the admissibility
    seam: PDR mode autonomous, kill-switch clear, conservative-but-passable gate."""
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(
        auto,
        "_read_kill_switch",
        lambda: auto.KillSwitchState(
            tripped=False, tripped_at=None, cumulative_pnl_pct=0.0, threshold_pct=0.10, reason=None
        ),
    )
    # Disable portfolio caps so the kelly short reaches the admissibility block intact.
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
    # Isolate QUANT_HOME to an EMPTY tmp book so the always-on concurrent-cap rail
    # (dea6d27, ADR-0016 §D9) counts 0 open positions and does NOT fire
    # SILENCE_CONCURRENT_CAP. The rail reads QUANT_HOME/executions.jsonl at tick start;
    # without this isolation it reads the operator's REAL book and silences the FIRE
    # under test. Matches this module's "no ~/.hermes writes" isolation contract — the
    # rail was wired live AFTER these tests were authored, so the fixture had a gap.
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path)
    # Isolate from the operator's live ~/.hermes/config.yaml. _read_safety_rails()
    # reads it, and an operator who enables quant.autonomous.require_stop_loss=true
    # would activate the stop-loss backstop (3863-3896), which size-DOWNS this test's
    # stopless -0.20 short to stopless_max_size_pct=0.05 — making the size assertions
    # fail on that machine while passing in clean CI. A unit test must not depend on
    # the developer's live config; force all safety rails to their code defaults
    # (require_stop_loss=False = byte-identical legacy behavior). Same class of
    # live-wiring gap as the concurrent-cap isolation above.
    monkeypatch.setattr(auto, "_read_config", lambda: {})
    return monkeypatch


_WL = [WatchlistEntry(symbol="GME", asset_class="equity", timeframe="1d")]


def test_etb_whole_share_short_not_rejected_as_fractional(autonomous_env, monkeypatch):
    """Flag ON: the oracle must receive a WHOLE-SHARE integer qty (0.20 NAV @ 200 over
    100k NAV = 100 shares), NOT the 0.20 fraction. With an ETB-accepting oracle the
    short FIREs — proving it is no longer silenced as FRACTIONAL_SHORT."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    # H-adm #1 (commit 72e3d8b): the autonomous short branch now fetches a LIVE
    # paper-account buying-power via oracle.live_buying_power(). Mock it for a
    # deterministic unit test — a generous BP so the BP hard-check passes and the
    # ETB whole-share short FIREs (the property under test).
    import hermes_quant.admissibility.oracle as _oracle_mod
    monkeypatch.setattr(_oracle_mod, "live_buying_power", lambda: 200_000.0)
    oracle = _RecordingOracle()
    monkeypatch.setattr(gate_order, "select_oracle", lambda: oracle)

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())

    # The oracle saw a whole-share integer, not the NAV fraction.
    assert len(oracle.calls) == 1
    qty = oracle.calls[0]["qty"]
    assert qty == 100, f"expected 100 shares (0.20*100k/200), got {qty!r}"
    assert isinstance(qty, int)
    assert qty != pytest.approx(0.20)  # NOT the fraction the bug passed
    # The decision-price quote we DO have is plumbed into ctx, as is account_equity
    # (= the paper NAV) — the H-adm #1 fix. available_bp is now LIVE-plumbed
    # (commit 72e3d8b) — it is the mocked buying-power, no longer None.
    assert oracle.calls[0]["ctx"].current_ask == 200.0
    assert oracle.calls[0]["ctx"].account_equity == 100_000.0
    assert oracle.calls[0]["ctx"].available_bp == 200_000.0

    # And the ETB whole-share short FIREd — not silenced as FRACTIONAL_SHORT.
    gme = [d for d in result.decisions if d.symbol == "GME"]
    assert len(gme) == 1
    assert gme[0].gate == "FIRE"
    assert gme[0].action is not None
    assert gme[0].action["target_position_pct"] == -0.20
    assert result.fires == 1


def test_fail_closed_when_nav_missing(autonomous_env, monkeypatch):
    """Missing NAV => 0 shares => the real core REJECTs (never an assumed-admissible
    short). The decision is SILENCE_ADMISSIBILITY, not FIRE — fail-closed."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: None)  # NAV unknown
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RealCoreOracle())

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())

    gme = [d for d in result.decisions if d.symbol == "GME"]
    assert len(gme) == 1
    assert gme[0].gate == "SILENCE_ADMISSIBILITY"
    assert gme[0].details["qty_shares"] == 0  # fail-closed: no shares valued
    assert result.fires == 0
    assert result.silences == 1


def test_fail_closed_when_price_missing(autonomous_env, monkeypatch):
    """Missing decision price => 0 shares + no quote => fail-closed REJECT, never FIRE."""
    monkeypatch.setenv("HERMES_QUANT_ADMISSIBILITY", "1")
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100_000.0)
    monkeypatch.setattr(gate_order, "select_oracle", lambda: _RealCoreOracle())

    # No decision_price and no analyst last_close -> price is None -> 0 shares.
    def _no_price_advisor(**kwargs: Any) -> dict[str, Any]:
        ar = _short_advisor()(**kwargs)
        ar.pop("decision_price")
        ar["analyst_views"] = [{"metadata": {"atr_relative": 0.01}}, {"metadata": {}}]
        return ar

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_no_price_advisor)

    gme = [d for d in result.decisions if d.symbol == "GME"]
    assert gme[0].gate == "SILENCE_ADMISSIBILITY"
    assert gme[0].details["qty_shares"] == 0
    assert result.fires == 0


def test_flag_off_is_bitwise_noop(autonomous_env, monkeypatch):
    """Flag OFF: the admissibility block is skipped entirely. The short FIREs exactly
    as today, and neither the oracle nor the NAV lookup is ever touched."""
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)

    nav_calls: list[int] = []
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: nav_calls.append(1) or 100_000.0)

    def _boom_select():
        raise AssertionError("select_oracle must NOT be called when flag is OFF")

    monkeypatch.setattr(gate_order, "select_oracle", _boom_select)

    result = auto.tick(dry_run=True, symbols=_WL, advisor_recommend=_short_advisor())

    gme = [d for d in result.decisions if d.symbol == "GME"]
    assert gme[0].gate == "FIRE"
    assert gme[0].action["target_position_pct"] == -0.20
    assert result.fires == 1
    assert nav_calls == []  # NAV lookup never ran (flag-OFF no-op)
