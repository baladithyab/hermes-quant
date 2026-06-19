"""Unit tests for HermesQuantConsumer._latest_signal_for drop observability.

Defect (this lane): `_latest_signal_for` is the SOLE gate from signals.jsonl into
all four LIVE freqtrade consumer paths (populate_entry_trend, populate_exit_trend,
custom_stake_amount, order_filled). When the upstream daemon emits degraded
records — wrong schema_version, a stale asof, or an unparseable/garbage asof — the
gate fail-CLOSED drops the signal with a bare `return None` and emits ZERO log
lines. Meanwhile the heartbeat dead-man-switch is a DISTINCT rail (keyed on
type=='heartbeat' records with their own asof): a daemon emitting fresh heartbeats
but garbage per-asset signals keeps the dead-man-switch satisfied, so trading halts
unobservably — the operator sees zero new trades, zero logs, and fresh heartbeats,
indistinguishable from a deliberate quiet day.

The fail-CLOSED *direction* is correct money-software posture and must stay. The
defect is the absence of OBSERVABILITY of *why* trading halted (ar28 lesson:
unobservable silence on a money rail defeats the operator). Every sibling
trading-halt (safe-stop) logs; this gate alone does not.

Fix contract (asserted here):
  * the FIRST time a pair flips from "had/could-have a usable signal" to dropping
    for a given reason, emit exactly ONE logger.warning naming the pair + reason;
  * repeats of the same (pair, reason) are SUPPRESSED (the gate runs every candle
    per pair — naive per-call logging would spam);
  * a different reason, or recovery then re-drop, logs again (state-transition);
  * the silence-by-default behaviour is preserved: a dropped signal still emits NO
    trade (enter_long / exit_long stay 0).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.consumers.freqtrade.quant_consumer_strategy import HermesQuantConsumer


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch):
    """A HermesQuantConsumer with bus paths redirected to a hermetic tmp dir."""
    s = HermesQuantConsumer({})
    sig_path = tmp_path / "signals.jsonl"
    exec_path = tmp_path / "executions.jsonl"
    halt_path = tmp_path / "halt_state.json"
    monkeypatch.setattr(s, "SIGNAL_BUS_PATH", sig_path)
    monkeypatch.setattr(s, "EXECUTION_BUS_PATH", exec_path)
    monkeypatch.setattr(s, "HALT_STATE_MIRROR", halt_path)
    import hermes_quant.consumers.freqtrade.quant_consumer_strategy as mod

    monkeypatch.setattr(mod, "EXECUTION_BUS_PATH", exec_path)
    # Keep the dead-man-switch from firing during the test window: write a fresh
    # heartbeat so _refresh_state does NOT enter safe-stop. This isolates the
    # per-signal drop path (the rail under test) from the heartbeat rail.
    return s, sig_path


def _write_bus(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _fresh_heartbeat(asof: str) -> dict:
    return {"type": "heartbeat", "asof": asof, "schema_version": 1}


def _drop_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and "signal" in r.getMessage().lower() and "drop" in r.getMessage().lower()
    ]


def test_stale_signal_drop_logs_once_per_pair_reason(strategy, caplog):
    """A genuinely-stale cached signal (asof older than max_signal_age_minutes) must
    be dropped AND log exactly one warning naming the pair + a stale reason. Repeats
    of the same (pair, reason) are suppressed."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    heartbeat_asof = (now - pd.Timedelta(seconds=5)).isoformat()
    stale_asof = (now - pd.Timedelta(minutes=90)).isoformat()  # > 30 min cap
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat(heartbeat_asof),
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "ETH/USDT",
                "asof": stale_asof,
                "direction": 1,
                "target_position_pct": 0.1,
            },
        ],
    )

    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        sig1 = s._latest_signal_for("ETH/USDT", now)
        sig2 = s._latest_signal_for("ETH/USDT", now)  # repeat — must NOT log again

    assert sig1 is None  # fail-CLOSED preserved
    assert sig2 is None
    warns = _drop_warnings(caplog)
    assert len(warns) == 1, f"expected exactly one stale-drop warning, got {warns}"
    assert "ETH/USDT" in warns[0]
    assert "stale" in warns[0].lower()


def test_bad_schema_drop_logs(strategy, caplog):
    """A cached signal with schema_version != 1 must be dropped AND log a warning."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 2,  # wrong schema
                "asset": "BTC/USDT",
                "asof": now.isoformat(),
                "direction": 1,
            },
        ],
    )

    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        sig = s._latest_signal_for("BTC/USDT", now)

    assert sig is None
    warns = _drop_warnings(caplog)
    assert len(warns) == 1, f"expected one bad-schema warning, got {warns}"
    assert "BTC/USDT" in warns[0]


def test_garbage_asof_parse_error_drop_logs(strategy, caplog):
    """A cached signal whose asof is unparseable garbage (pd.Timestamp raises
    DateParseError, a ValueError subclass -> the except branch) must be dropped AND
    log a parse-error warning. This is the documented ar43 daemon failure class."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "SOL/USDT",
                "asof": "not-a-timestamp",  # garbage -> DateParseError in the guard
                "direction": 1,
            },
        ],
    )

    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        sig = s._latest_signal_for("SOL/USDT", now)

    assert sig is None
    warns = _drop_warnings(caplog)
    assert len(warns) == 1, f"expected one parse-error warning, got {warns}"
    assert "SOL/USDT" in warns[0]


def test_distinct_reasons_each_log_once(strategy, caplog):
    """Two different pairs dropping for two different reasons each log once — the
    suppression key is (pair, reason), not a single global flag."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 2,  # bad schema
                "asset": "ETH/USDT",
                "asof": now.isoformat(),
                "direction": 1,
            },
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "BTC/USDT",
                "asof": (now - pd.Timedelta(minutes=90)).isoformat(),  # stale
                "direction": 1,
            },
        ],
    )

    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        s._latest_signal_for("ETH/USDT", now)
        s._latest_signal_for("ETH/USDT", now)  # repeat — suppressed
        s._latest_signal_for("BTC/USDT", now)
        s._latest_signal_for("BTC/USDT", now)  # repeat — suppressed

    warns = _drop_warnings(caplog)
    assert len(warns) == 2, f"expected exactly two distinct (pair,reason) warnings, got {warns}"
    joined = " | ".join(warns)
    assert "ETH/USDT" in joined and "BTC/USDT" in joined


def test_recovery_then_redrop_logs_again(strategy, caplog):
    """After a pair recovers (a usable signal), a subsequent drop logs AGAIN — the
    state-transition is edge-triggered, not latched forever."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")

    # Phase 1: a stale signal -> drop (logs once)
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "ETH/USDT",
                "asof": (now - pd.Timedelta(minutes=90)).isoformat(),
                "direction": 1,
            },
        ],
    )
    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        assert s._latest_signal_for("ETH/USDT", now) is None
    assert len(_drop_warnings(caplog)) == 1

    # Phase 2: a fresh usable signal arrives -> recovery (clears the latch)
    fresh_now = now + pd.Timedelta(minutes=5)
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((fresh_now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "ETH/USDT",
                "asof": fresh_now.isoformat(),
                "direction": 1,
            },
        ],
    )
    assert s._latest_signal_for("ETH/USDT", fresh_now) is not None

    # Phase 3: degrade AGAIN (stale) -> must log a SECOND time
    caplog.clear()
    later = fresh_now + pd.Timedelta(minutes=90)
    with caplog.at_level("WARNING", logger="hermes_quant.consumers.freqtrade.quant_consumer_strategy"):
        assert s._latest_signal_for("ETH/USDT", later) is None
    assert len(_drop_warnings(caplog)) == 1, "re-drop after recovery must log again"


def test_dropped_signal_emits_no_trade(strategy):
    """Silence-by-default is preserved: a dropped (stale) signal yields enter_long=0
    and exit_long=0 — the fix adds observability without emitting any trade."""
    s, sig_path = strategy
    now = pd.Timestamp("2026-05-31T12:00:00Z")
    _write_bus(
        sig_path,
        [
            _fresh_heartbeat((now - pd.Timedelta(seconds=5)).isoformat()),
            {
                "type": "signal",
                "schema_version": 1,
                "asset": "ETH/USDT",
                "asof": (now - pd.Timedelta(minutes=90)).isoformat(),
                "direction": 1,
            },
        ],
    )
    df = pd.DataFrame({"date": [now], "close": [100.0]})
    entry = s.populate_entry_trend(df.copy(), {"pair": "ETH/USDT"})
    exit_ = s.populate_exit_trend(df.copy(), {"pair": "ETH/USDT"})
    assert int(entry["enter_long"].iloc[-1]) == 0
    assert int(exit_["exit_long"].iloc[-1]) == 0
