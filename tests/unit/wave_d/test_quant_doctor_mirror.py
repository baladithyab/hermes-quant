"""Wave D tests — quant_doctor content-presence DaemonState mirror (ADR-0038 §D.3 / P6).

Coverage:
  * Empty bus (no signals.jsonl).
  * Partial-stage progression (rows with only ohlcv → only views → full).
  * Multi-symbol independence (each symbol's stage status is independent).
  * Dedup correctness (`_seen_event_ids` keyed on (symbol, bar_ts)).
  * Stale-bar timeout / heartbeat age computation.
  * Halt mirror (active_halts surfaced through HaltStateSQLite).
  * Journal pending count (proposal store).
  * Augmentation: existing quant_doctor checks (drift, optional_libs) still
    present after mirror added.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant import tools as hq_tools

# ---------------------------------------------------------------------------
# Helpers — write JSONL rows in legacy and V2 shapes
# ---------------------------------------------------------------------------


def _write_legacy_row(
    bus_path: Path,
    *,
    asset: str,
    asof: str,
    direction: int = 1,
    confidence: float = 0.55,
    has_components: bool = True,
    has_aggregator: bool = True,
    has_target: bool = True,
    decision_price: float | None = 100.0,
) -> dict:
    row: dict = {
        "schema_version": 1,
        "id": f"sig-{asof.replace(':', '-')}-{asset.replace('/', '-')}",
        "asof": asof,
        "asset": asset,
        "exchange": "test",
        "timeframe": "1h",
        "asset_class": "crypto",
        "horizon": "1h",
    }
    if decision_price is not None:
        row["decision_price"] = decision_price
        row["direction"] = direction
        row["magnitude"] = 0.012
        row["confidence"] = confidence
        row["confidence_raw"] = 0.7
    if has_components:
        row["components"] = [
            {
                "analyst": "ta",
                "direction": direction,
                "magnitude": 0.012,
                "confidence": confidence,
                "confidence_raw": 0.7,
                "horizon": "1h",
                "metadata": None,
            }
        ]
    if has_aggregator:
        row["aggregator"] = "bma"
        row["metadata"] = None
        row["semantic_packet_hashes"] = []
        row["committee_turns_hashes"] = []
    if has_target:
        row["target_position_pct"] = 0.10
        row["reason"] = "ok"
        row["halt"] = False
        row["halt_scope"] = None
        row["halt_until"] = None

    bus_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bus_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def _write_heartbeat(bus_path: Path, *, asof: str) -> None:
    row = {"type": "heartbeat", "asof": asof}
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bus_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _write_v2_row(
    bus_path: Path,
    *,
    symbol: str,
    bar_ts: str,
    has_aggregated: bool = True,
    has_final: bool = True,
) -> dict:
    row: dict = {
        "symbol": symbol,
        "bar_ts": bar_ts,
        "asof_decision": bar_ts,
        "meta": {
            "schema_version": 1,
            "signal_id": f"sig-{symbol}-{bar_ts}",
            "exchange": "test",
            "timeframe": "1h",
            "asset_class": "crypto",
        },
        "ohlcv": {"last_close": 100.0, "last_volume": 1000.0, "n_bars": 60},
        "indicators": None,
        "regime_label": None,
        "analyst_views": [
            {
                "analyst": "ta",
                "direction": 1,
                "magnitude": 0.01,
                "confidence": 0.5,
                "confidence_raw": 0.6,
                "horizon": "1h",
                "metadata": None,
            }
        ],
        "aggregated_signal": (
            {
                "aggregator": "bma",
                "direction": 1,
                "magnitude": 0.01,
                "confidence": 0.5,
                "confidence_raw": 0.6,
                "horizon": "1h",
                "metadata": None,
                "components": [],
            }
            if has_aggregated
            else None
        ),
        "risk_check": {
            "target_position_pct": 0.05,
            "reason": "ok",
            "halt": False,
            "halt_scope": None,
            "halt_until": None,
        }
        if has_final
        else None,
        "final_decision": {
            "target_position_pct": 0.05,
            "reason": "ok",
            "halt": False,
            "halt_scope": None,
            "halt_until": None,
        }
        if has_final
        else None,
    }
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bus_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


@pytest.fixture()
def isolated_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect tools module's path constants to a tmp signal bus."""
    bus = tmp_path / "signals.jsonl"
    monkeypatch.setattr(hq_tools, "SIGNAL_BUS_PATH", bus)
    monkeypatch.setattr(hq_tools, "EXECUTION_BUS_PATH", tmp_path / "executions.jsonl")
    monkeypatch.setattr(hq_tools, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hq_tools, "QUANT_HOME", tmp_path)
    return bus


# ---------------------------------------------------------------------------
# 1. Empty bus
# ---------------------------------------------------------------------------


def test_empty_bus_returns_empty_mirror(isolated_bus: Path) -> None:
    """No signals.jsonl → mirror has empty per_symbol/halts and pending=0."""
    out = json.loads(hq_tools.quant_doctor({}))
    ds = out["daemon_state"]
    assert ds is not None
    assert ds["per_symbol"] == {}
    assert ds["halts"] == []
    assert ds["journal_pending_count"] == 0
    assert ds["last_heartbeat_age_s"] is None


# ---------------------------------------------------------------------------
# 2. Partial-stage progression
# ---------------------------------------------------------------------------


def test_partial_stage_v2_only_ohlcv(isolated_bus: Path) -> None:
    """V2 row with only ohlcv populated reports stages_seen=['ohlcv']."""
    _write_v2_row(
        isolated_bus,
        symbol="BTC/USDT",
        bar_ts="2026-05-26T15:00:00",
        has_aggregated=False,
        has_final=False,
    )
    # Override: nullify analyst_views too. Easiest: write a row directly.
    bus_path = isolated_bus
    bus_path.unlink()  # rewrite cleanly
    minimal = {
        "symbol": "BTC/USDT",
        "bar_ts": "2026-05-26T15:00:00",
        "asof_decision": "2026-05-26T15:00:00",
        "meta": {
            "schema_version": 1,
            "signal_id": "sig-1",
            "timeframe": "1h",
            "asset_class": "crypto",
        },
        "ohlcv": {"last_close": 100.0, "last_volume": 1000.0, "n_bars": 60},
        "analyst_views": None,
        "aggregated_signal": None,
        "risk_check": None,
        "final_decision": None,
    }
    with open(bus_path, "w") as f:
        f.write(json.dumps(minimal) + "\n")

    out = json.loads(hq_tools.quant_doctor({}))
    sym = out["daemon_state"]["per_symbol"]["BTC/USDT"]
    assert sym["stages_seen"] == ["ohlcv"]


def test_partial_stage_legacy_full_pipeline(isolated_bus: Path) -> None:
    """Legacy row with all fields → all 6 stages seen."""
    _write_legacy_row(
        isolated_bus,
        asset="BTC/USDT",
        asof="2026-05-26T15:00:00.000000Z",
    )
    out = json.loads(hq_tools.quant_doctor({}))
    sym = out["daemon_state"]["per_symbol"]["BTC/USDT"]
    # ohlcv (decision_price), analysts (components), aggregated (aggregator),
    # risk + final (target_position_pct present)
    assert set(sym["stages_seen"]) == {"ohlcv", "analysts", "aggregated", "risk", "final"}


def test_partial_stage_legacy_missing_aggregator(isolated_bus: Path) -> None:
    """Legacy row missing aggregator → no 'aggregated' stage."""
    _write_legacy_row(
        isolated_bus,
        asset="ETH/USDT",
        asof="2026-05-26T15:00:00.000000Z",
        has_aggregator=False,
        has_target=False,
    )
    out = json.loads(hq_tools.quant_doctor({}))
    sym = out["daemon_state"]["per_symbol"]["ETH/USDT"]
    assert "aggregated" not in sym["stages_seen"]
    assert "risk" not in sym["stages_seen"]
    assert "analysts" in sym["stages_seen"]


# ---------------------------------------------------------------------------
# 3. Multi-symbol independence
# ---------------------------------------------------------------------------


def test_multi_symbol_independence(isolated_bus: Path) -> None:
    """Two symbols with different stage progress are tracked independently."""
    _write_legacy_row(
        isolated_bus, asset="BTC/USDT", asof="2026-05-26T15:00:00.000000Z"
    )
    _write_legacy_row(
        isolated_bus,
        asset="ETH/USDT",
        asof="2026-05-26T15:01:00.000000Z",
        has_aggregator=False,
        has_target=False,
    )
    _write_legacy_row(
        isolated_bus,
        asset="SOL/USDT",
        asof="2026-05-26T15:02:00.000000Z",
        direction=-1,
        confidence=0.7,
    )

    out = json.loads(hq_tools.quant_doctor({}))
    per = out["daemon_state"]["per_symbol"]
    assert set(per.keys()) == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
    assert "aggregated" in per["BTC/USDT"]["stages_seen"]
    assert "aggregated" not in per["ETH/USDT"]["stages_seen"]
    assert per["SOL/USDT"]["last_action_dir"] == -1
    assert per["SOL/USDT"]["last_action_conf"] == 0.7


# ---------------------------------------------------------------------------
# 4. Dedup correctness
# ---------------------------------------------------------------------------


def test_dedup_on_symbol_bar_ts(isolated_bus: Path) -> None:
    """Two rows with identical (symbol, asof) are deduped to 1 event."""
    asof = "2026-05-26T15:00:00.000000Z"
    _write_legacy_row(isolated_bus, asset="BTC/USDT", asof=asof)
    _write_legacy_row(isolated_bus, asset="BTC/USDT", asof=asof)
    _write_legacy_row(isolated_bus, asset="BTC/USDT", asof=asof)

    out = json.loads(hq_tools.quant_doctor({}))
    ds = out["daemon_state"]
    assert ds["n_dedup_events"] == 1
    assert "BTC/USDT" in ds["per_symbol"]


def test_dedup_distinct_bar_ts_kept(isolated_bus: Path) -> None:
    """Different bar_ts on same symbol are NOT deduped."""
    _write_legacy_row(
        isolated_bus, asset="BTC/USDT", asof="2026-05-26T15:00:00.000000Z"
    )
    _write_legacy_row(
        isolated_bus, asset="BTC/USDT", asof="2026-05-26T16:00:00.000000Z"
    )
    out = json.loads(hq_tools.quant_doctor({}))
    assert out["daemon_state"]["n_dedup_events"] == 2


# ---------------------------------------------------------------------------
# 5. Heartbeat / stale-bar timeout
# ---------------------------------------------------------------------------


def test_heartbeat_age_computed(isolated_bus: Path) -> None:
    """A recent heartbeat populates last_heartbeat_age_s."""
    now = pd.Timestamp.utcnow().tz_localize(None)
    hb_ts = (now - pd.Timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_heartbeat(isolated_bus, asof=hb_ts)
    # Need at least one signal too so the bus has a non-empty event set
    _write_legacy_row(isolated_bus, asset="BTC/USDT", asof="2026-05-26T15:00:00.000000Z")

    out = json.loads(hq_tools.quant_doctor({}))
    age = out["daemon_state"]["last_heartbeat_age_s"]
    assert age is not None
    assert 25 <= age <= 60  # ~30s plus a little jitter


def test_no_heartbeat_yields_none_age(isolated_bus: Path) -> None:
    """No heartbeat in bus → last_heartbeat_age_s is None."""
    _write_legacy_row(isolated_bus, asset="BTC/USDT", asof="2026-05-26T15:00:00.000000Z")
    out = json.loads(hq_tools.quant_doctor({}))
    assert out["daemon_state"]["last_heartbeat_age_s"] is None


# ---------------------------------------------------------------------------
# 6. Halt mirror
# ---------------------------------------------------------------------------


def test_halt_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Active halts surface through the daemon_state mirror."""
    from hermes_quant.daemon import halt_state as hs_mod

    state_db = tmp_path / "state.db"
    halt_json = tmp_path / "halts.json"
    monkeypatch.setattr(hs_mod, "DEFAULT_STATE_DB", state_db)
    monkeypatch.setattr(hs_mod, "DEFAULT_HALT_JSON_MIRROR", halt_json)

    # Also redirect the bus to tmp
    bus = tmp_path / "signals.jsonl"
    monkeypatch.setattr(hq_tools, "SIGNAL_BUS_PATH", bus)
    monkeypatch.setattr(hq_tools, "EXECUTION_BUS_PATH", tmp_path / "executions.jsonl")
    monkeypatch.setattr(hq_tools, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(hq_tools, "QUANT_HOME", tmp_path)
    bus.parent.mkdir(parents=True, exist_ok=True)
    bus.write_text("")  # empty

    # Add a halt
    h = hs_mod.HaltStateSQLite(db_path=state_db, mirror_path=halt_json)
    h.add_halt(
        account_id="default",
        asset_class="crypto",
        asset="BTC/USDT",
        reason="daily_loss_breaker",
    )

    out = json.loads(hq_tools.quant_doctor({}))
    halts = out["daemon_state"]["halts"]
    assert len(halts) == 1
    assert halts[0]["account_id"] == "default"
    assert halts[0]["reason"] == "daily_loss_breaker"


# ---------------------------------------------------------------------------
# 7. Journal pending count
# ---------------------------------------------------------------------------


def test_journal_pending_count_zero_when_no_proposals(isolated_bus: Path) -> None:
    """Default: no pending proposals → journal_pending_count == 0."""
    out = json.loads(hq_tools.quant_doctor({}))
    assert out["daemon_state"]["journal_pending_count"] == 0


# ---------------------------------------------------------------------------
# 8. Augmentation does not break existing quant_doctor checks
# ---------------------------------------------------------------------------


def test_augmentation_preserves_existing_blocks(isolated_bus: Path) -> None:
    """Top-level quant_doctor response keeps all legacy keys after augmentation."""
    out = json.loads(hq_tools.quant_doctor({}))
    # legacy / pre-existing surface
    assert "checks" in out
    assert "optional_libs" in out
    assert "drift" in out
    assert "next_step" in out
    assert "include_calibration" in out
    # new surface
    assert "daemon_state" in out


def test_daemon_state_can_be_disabled(isolated_bus: Path) -> None:
    """`daemon_state=False` arg → block is None (back-compat)."""
    out = json.loads(hq_tools.quant_doctor({"daemon_state": False}))
    assert out["daemon_state"] is None
    # Other surfaces still fine
    assert "checks" in out


def test_v2_row_action_dir_extracted(isolated_bus: Path) -> None:
    """V2 row: last_action_dir comes from aggregated_signal.direction."""
    _write_v2_row(isolated_bus, symbol="BTC/USDT", bar_ts="2026-05-26T15:00:00")
    out = json.loads(hq_tools.quant_doctor({}))
    sym = out["daemon_state"]["per_symbol"]["BTC/USDT"]
    assert sym["last_action_dir"] == 1
    assert sym["last_action_conf"] == 0.5
