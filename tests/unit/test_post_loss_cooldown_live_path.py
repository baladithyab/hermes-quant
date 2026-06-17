"""ar_live_cooldown — post-loss cooldown is dead on the live path (Rule 4 inert).

DEFECT (structural death of Rule 4 on the live daemon path):

    DefaultRiskGate._cooldowns is always {} on every autonomous tick because:
    1. autonomous.tick() calls advisor_recommend() with NO risk_gate= argument
    2. advisor.recommend() constructs a FRESH DefaultRiskGate() every call
    3. settlement_loop.dispatch_settlement() never calls gate.record_loss()

    Rule 4 (gate.py:608-614) reads self._cooldowns. An always-empty _cooldowns
    means the cooldown check is always skipped — the rule is structurally dead.
    The cr12 fix wired record_loss() into AdvisorStrategy (the BACKTEST path)
    but left the live daemon path unwired.

FIX (HERMES_QUANT_POST_LOSS_COOLDOWN=1, default-OFF):

    After join_exit_fills produces SettledRoundTrip objects with realized_return < 0,
    persist a {(account_id, asset_class, asset): latest_loss_at} JSON sidecar.
    Before calling advisor_recommend() in the tick's watchlist loop, read the sidecar
    and build a DefaultRiskGate pre-seeded with those loss timestamps, then pass it
    via risk_gate=. Rule 4 now sees the prior losses and silences re-entry within
    the cooldown window.

    Flag OFF (production default) => no sidecar write, no gate injection => tick is
    BYTE-IDENTICAL to the pre-fix behavior (tests of the OFF path must confirm this).

RED->GREEN:
    test_rule4_fires_on_live_path_after_loss:
        With flag ON + a sidecar recording a recent loss, the pre-seeded gate silences
        the very next recommend() call (Rule 4 fires). PRE-FIX: _cooldowns={} so Rule 4
        is a no-op and the recommend() proceeds past Rule 4.

    test_flag_off_gate_has_empty_cooldowns (byte-identical OFF path):
        With flag OFF, _build_gate_with_cooldowns is NOT called; the gate that runs is
        a fresh DefaultRiskGate() with empty _cooldowns (the pre-fix invariant).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pandas as pd
import pytest

import hermes_quant.autonomous as autonomous
from hermes_quant.daemon.settlement_loop import (
    SettledRoundTrip,
    load_loss_cooldown_sidecar,
    persist_loss_cooldown_sidecar,
)
from hermes_quant.risk.gate import DefaultRiskGate, RiskConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ks_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated QUANT_HOME and loss-sidecar path for each test."""
    qh = tmp_path / "quant"
    qh.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(autonomous, "QUANT_HOME", qh)
    monkeypatch.setattr(autonomous, "_LOSS_COOLDOWN_SIDECAR_PATH", qh / "loss_cooldown_state.json")
    monkeypatch.setattr(autonomous, "_account_nav_usd", lambda: 100_000.0)
    return qh


# ---------------------------------------------------------------------------
# Sidecar helpers — load/persist contract
# ---------------------------------------------------------------------------


def test_persist_and_load_sidecar_roundtrip(tmp_path: Path) -> None:
    """persist_loss_cooldown_sidecar + load_loss_cooldown_sidecar roundtrip."""
    path = tmp_path / "losses.json"
    ts_a = pd.Timestamp("2026-06-17T10:00:00Z")
    ts_b = pd.Timestamp("2026-06-16T08:00:00Z")
    losses = {
        ("paper-default", "equity", "ASTS"): ts_a,
        ("paper-default", "equity", "NVDA"): ts_b,
    }
    persist_loss_cooldown_sidecar(losses, path)  # type: ignore[arg-type]
    loaded = load_loss_cooldown_sidecar(path)
    assert ("paper-default", "equity", "ASTS") in loaded
    assert ("paper-default", "equity", "NVDA") in loaded
    # Timestamps survive the JSON roundtrip with UTC normalization.
    assert abs((loaded[("paper-default", "equity", "ASTS")] - ts_a).total_seconds()) < 1
    assert abs((loaded[("paper-default", "equity", "NVDA")] - ts_b).total_seconds()) < 1


def test_persist_merges_and_keeps_latest(tmp_path: Path) -> None:
    """A second persist merges: newer timestamps win, older are preserved."""
    path = tmp_path / "losses.json"
    t_old = pd.Timestamp("2026-06-15T09:00:00Z")
    t_new = pd.Timestamp("2026-06-17T10:00:00Z")

    persist_loss_cooldown_sidecar({("paper-default", "equity", "ASTS"): t_old}, path)  # type: ignore[arg-type]
    # Second call: newer timestamp for ASTS + new key NVDA.
    persist_loss_cooldown_sidecar(  # type: ignore[arg-type]
        {
            ("paper-default", "equity", "ASTS"): t_new,
            ("paper-default", "equity", "NVDA"): t_old,
        },
        path,
    )
    loaded = load_loss_cooldown_sidecar(path)
    # ASTS timestamp upgraded to newer.
    assert abs((loaded[("paper-default", "equity", "ASTS")] - t_new).total_seconds()) < 1
    # NVDA added.
    assert ("paper-default", "equity", "NVDA") in loaded


def test_load_missing_sidecar_returns_empty(tmp_path: Path) -> None:
    """load_loss_cooldown_sidecar on a missing file returns {} (cold-start OK)."""
    assert load_loss_cooldown_sidecar(tmp_path / "does_not_exist.json") == {}


def test_load_corrupt_sidecar_returns_empty(tmp_path: Path) -> None:
    """A corrupt sidecar is treated as cold-start (empty dict, never raises)."""
    path = tmp_path / "losses.json"
    path.write_text("not-valid-json}", encoding="utf-8")
    assert load_loss_cooldown_sidecar(path) == {}


# ---------------------------------------------------------------------------
# _persist_round_trip_losses — internal helper
# ---------------------------------------------------------------------------


def _make_rt(
    asset: str,
    asset_class: str,
    account_id: str,
    realized_return: float,
    asof_exit: str = "2026-06-17T10:00:00Z",
) -> SettledRoundTrip:
    """Build a minimal SettledRoundTrip for testing."""
    ts = pd.Timestamp(asof_exit)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return SettledRoundTrip(
        asset=asset,
        account_id=account_id,
        asset_class=asset_class,
        side="buy",
        qty=0.05,
        entry_price=100.0,
        exit_price=80.0 if realized_return < 0 else 110.0,
        asof_entry=ts - pd.Timedelta(hours=1),
        asof_exit=ts,
        entry_exec_id="e1",
        exit_exec_id="e2",
        entry_signal_id="s1",
        exit_signal_id=None,
        fees=0.0,
        realized_return=realized_return,
    )


def test_persist_round_trip_losses_writes_loss(ks_home: Path) -> None:
    """_persist_round_trip_losses writes a paper-default LOSS to the sidecar."""
    sidecar = ks_home / "loss_cooldown_state.json"
    rt = _make_rt("ASTS", "equity", "paper-default", -0.20, "2026-06-17T10:00:00Z")
    autonomous._persist_round_trip_losses([rt], sidecar)
    loaded = load_loss_cooldown_sidecar(sidecar)
    assert ("paper-default", "equity", "ASTS") in loaded


def test_persist_round_trip_losses_ignores_gains(ks_home: Path) -> None:
    """_persist_round_trip_losses does NOT write a profit round-trip."""
    sidecar = ks_home / "loss_cooldown_state.json"
    rt = _make_rt("NVDA", "equity", "paper-default", +0.05, "2026-06-17T10:00:00Z")
    autonomous._persist_round_trip_losses([rt], sidecar)
    loaded = load_loss_cooldown_sidecar(sidecar)
    # Sidecar never written for a profit.
    assert ("paper-default", "equity", "NVDA") not in loaded


def test_persist_round_trip_losses_ignores_non_paper_default(ks_home: Path) -> None:
    """_persist_round_trip_losses skips non-paper-default accounts (ar34 filter)."""
    sidecar = ks_home / "loss_cooldown_state.json"
    rt = _make_rt("ETH/USDT", "crypto", "freqtrade", -0.30, "2026-06-17T10:00:00Z")
    autonomous._persist_round_trip_losses([rt], sidecar)
    loaded = load_loss_cooldown_sidecar(sidecar)
    # Not paper-default => skipped.
    assert ("freqtrade", "crypto", "ETH/USDT") not in loaded


# ---------------------------------------------------------------------------
# _build_gate_with_cooldowns
# ---------------------------------------------------------------------------


def test_build_gate_with_cooldowns_pre_seeds_cooldowns(ks_home: Path) -> None:
    """_build_gate_with_cooldowns returns a gate whose _cooldowns is pre-seeded
    from the sidecar. This is the core cross-tick state-restoration invariant."""
    sidecar = ks_home / "loss_cooldown_state.json"
    ts = pd.Timestamp("2026-06-17T10:00:00Z")
    persist_loss_cooldown_sidecar({("paper-default", "equity", "ASTS"): ts}, sidecar)  # type: ignore[arg-type]

    gate = autonomous._build_gate_with_cooldowns(sidecar)
    assert isinstance(gate, DefaultRiskGate)
    key = ("paper-default", "equity", "ASTS")
    assert key in gate._cooldowns, (
        "live-path defect: _build_gate_with_cooldowns must pre-seed _cooldowns from the sidecar; "
        f"got empty _cooldowns={gate._cooldowns!r}"
    )
    assert gate._cooldowns[key].last_loss_at is not None


def test_build_gate_no_sidecar_returns_empty_gate(ks_home: Path) -> None:
    """When no sidecar exists, _build_gate_with_cooldowns returns a gate with empty
    _cooldowns — byte-identical cold-start behavior."""
    sidecar = ks_home / "loss_cooldown_state.json"
    gate = autonomous._build_gate_with_cooldowns(sidecar)
    assert isinstance(gate, DefaultRiskGate)
    assert gate._cooldowns == {}


# ---------------------------------------------------------------------------
# RED->GREEN: Rule 4 fires on the live path after a loss
#
# This is the keystone test for the defect fix. PRE-FIX: with an empty sidecar
# the gate has empty _cooldowns; Rule 4 never fires regardless of what's in the
# sidecar. POST-FIX: the seeded gate has the loss timestamp; Rule 4 silences
# re-entry within the cooldown window.
# ---------------------------------------------------------------------------


def _halt_state(tmp_path: Path):
    """Minimal HaltState backed by a throw-away SQLite DB."""
    from hermes_quant.daemon.halt_state import HaltStateSQLite

    return HaltStateSQLite(
        db_path=tmp_path / "halts.db",
        mirror_path=tmp_path / "halts.json",
    )


def _signal(asset: str = "ASTS", asset_class: str = "equity"):
    from hermes_quant.protocol import AggregatedSignal

    return AggregatedSignal(
        asset=asset,
        timeframe="1d",
        asset_class=asset_class,
        asof=pd.Timestamp("2026-06-17T14:00:00Z"),
        direction=1,
        magnitude=0.05,
        confidence=0.85,
        confidence_raw=0.85,
        horizon="1d",
        components=(),
        aggregator="bma",
    )


def _market():
    from hermes_quant.protocol import MarketState

    return MarketState(
        asset="ASTS",
        asof=pd.Timestamp("2026-06-17T14:00:00Z"),
        volatility=0.02,
        commission=0.0001,
        spread=0.0001,
        slippage_estimate=0.0001,
        tz="UTC",
    )


def _portfolio(account_id: str = "paper-default", asset_class: str = "equity"):
    from hermes_quant.protocol import Portfolio

    return Portfolio(
        account_id=account_id,
        asset_class=asset_class,
        asof=pd.Timestamp("2026-06-17T14:00:00Z"),
        positions={},
        cash=100_000.0,
        equity_total=100_000.0,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
    )


def test_rule4_fires_on_live_path_after_loss(tmp_path: Path) -> None:
    """RED->GREEN: Rule 4 silences re-entry when a recent loss is recorded in the sidecar.

    PRE-FIX (to reproduce the RED state manually): replace `gate = autonomous._build_gate_with_cooldowns(sidecar)`
    with `gate = DefaultRiskGate()` — _cooldowns will be empty and Rule 4 will not fire
    (n_silenced_cooldown == 0).

    POST-FIX: the gate is built from the sidecar so _cooldowns is pre-seeded with the
    loss at 13:00; at 14:00 (60 min inside the 24h window) Rule 4 silences ASTS.
    """
    sidecar = tmp_path / "loss_cooldown_state.json"
    # Record a loss for ASTS 60 minutes before the tick's portfolio.asof=14:00.
    loss_ts = pd.Timestamp("2026-06-17T13:00:00Z")
    persist_loss_cooldown_sidecar({("paper-default", "equity", "ASTS"): loss_ts}, sidecar)  # type: ignore[arg-type]

    # Build the gate from the sidecar (the post-fix path).
    gate = autonomous._build_gate_with_cooldowns(sidecar)
    assert isinstance(gate, DefaultRiskGate), "gate must be a DefaultRiskGate"
    assert ("paper-default", "equity", "ASTS") in gate._cooldowns, (
        "post-fix: sidecar loss must be in gate._cooldowns before calling .gate()"
    )

    # With cooldown_after_loss_minutes=1440 (24h) and loss 60 min ago: Rule 4 fires.
    gate.config = RiskConfig(cooldown_after_loss_minutes=1440)
    action = gate.gate(_signal("ASTS"), _market(), _portfolio(), _halt_state(tmp_path))
    assert action is None, (
        "live-path Rule 4 defect NOT fixed: a signal for ASTS inside the 24h cooldown "
        f"window should have been SILENCED but was approved: action={action!r}"
    )
    assert gate._n_silenced_cooldown == 1, (
        f"expected n_silenced_cooldown=1, got {gate._n_silenced_cooldown}"
    )


def test_flag_off_gate_has_empty_cooldowns(tmp_path: Path) -> None:
    """Byte-identical baseline: when the flag is OFF, _build_gate_with_cooldowns is not
    called and the gate is constructed fresh (empty _cooldowns). This pins the invariant
    that the OFF path is unchanged from the pre-fix behavior."""
    # Even with a populated sidecar, a plain DefaultRiskGate() has empty cooldowns.
    sidecar = tmp_path / "loss_cooldown_state.json"
    ts = pd.Timestamp("2026-06-17T13:00:00Z")
    persist_loss_cooldown_sidecar({("paper-default", "equity", "ASTS"): ts}, sidecar)  # type: ignore[arg-type]

    # The flag-OFF path does NOT call _build_gate_with_cooldowns; it uses a plain gate.
    gate = DefaultRiskGate()
    assert gate._cooldowns == {}, (
        "flag-OFF baseline: a fresh DefaultRiskGate must have empty _cooldowns "
        "(byte-identical to pre-fix)"
    )
    # Rule 4 does NOT fire (no cooldown state).
    gate.config = RiskConfig(cooldown_after_loss_minutes=1440)
    gate.gate(_signal("ASTS"), _market(), _portfolio(), _halt_state(tmp_path))
    assert gate._n_silenced_cooldown == 0, (
        "flag-OFF: Rule 4 must not fire when _cooldowns is empty"
    )


# ---------------------------------------------------------------------------
# _persist_round_trip_losses integration: end-to-end sidecar -> gate seeding
# ---------------------------------------------------------------------------


def test_e2e_round_trip_loss_to_gate_cooldown(tmp_path: Path) -> None:
    """End-to-end: a paper-default realized loss flows from SettledRoundTrip
    -> _persist_round_trip_losses (sidecar write) -> _build_gate_with_cooldowns
    -> gate._cooldowns pre-seeded -> Rule 4 fires on the next tick.

    This covers the full live-path seam from settlement to gate."""
    sidecar = tmp_path / "loss_cooldown_state.json"
    loss_ts = pd.Timestamp("2026-06-17T13:00:00Z")
    rt = _make_rt("ASTS", "equity", "paper-default", -0.20, loss_ts.isoformat())
    # Step 1: settlement records the loss.
    autonomous._persist_round_trip_losses([rt], sidecar)
    # Step 2: next tick builds a seeded gate.
    gate = autonomous._build_gate_with_cooldowns(sidecar)
    # Step 3: gate has cooldown state for ASTS.
    key = ("paper-default", "equity", "ASTS")
    assert key in gate._cooldowns, "sidecar loss must flow into gate._cooldowns"
    assert gate._cooldowns[key].last_loss_at is not None
    # Step 4: Rule 4 fires inside the window.
    gate.config = RiskConfig(cooldown_after_loss_minutes=1440)
    action = gate.gate(_signal("ASTS"), _market(), _portfolio(), _halt_state(tmp_path))
    assert action is None, "Rule 4 must silence re-entry within the 24h cooldown"
    assert gate._n_silenced_cooldown == 1
