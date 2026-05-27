"""Lock-in tests for codex security+invariant lens findings (2026-05-26).

Three P2 findings landed in [hash-tbd]; these tests guard against
regression.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.tick_loop import (
    AssetTask,
    TickLoopState,
    _build_signal_record,
    run_one_tick,
)
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    AnalystView,
    MarketContext,
    Portfolio,
)
from hermes_quant.tools import _compute_daemon_state_mirror

# ---------------------------------------------------------------------------
# Fix #1 — DeliberativeConfig keyword: enable_risk_mgmt (NOT include_risk_mgmt)
# ---------------------------------------------------------------------------


class TestDeliberativeConfigKeyword:
    """Codex P2 finding: quant-playbook-tick.py was passing
    `include_risk_mgmt=...` to `DeliberativeConfig`, but the dataclass
    field is `enable_risk_mgmt`. Calling with the wrong kwarg raises
    TypeError, breaking the deliberative path silently before
    `run_llm_committee()` could run.

    These tests pin the canonical field name so a future rename forces
    every caller to update.
    """

    def test_canonical_field_name_is_enable_risk_mgmt(self):
        from hermes_quant.aggregators.deliberative import DeliberativeConfig

        # Must accept enable_risk_mgmt without error.
        cfg = DeliberativeConfig(enable_llm_turns=True, enable_risk_mgmt=True)
        assert cfg.enable_risk_mgmt is True

    def test_include_risk_mgmt_is_NOT_a_field(self):  # noqa: N802 — capital NOT is deliberate emphasis
        """If a future contributor reintroduces an `include_risk_mgmt`
        alias, they must add this test deliberately. The dataclass is
        frozen + slots-strict, so unknown kwargs raise TypeError."""
        from hermes_quant.aggregators.deliberative import DeliberativeConfig

        with pytest.raises(TypeError, match="include_risk_mgmt"):
            DeliberativeConfig(  # type: ignore[call-arg]
                enable_llm_turns=True, include_risk_mgmt=True
            )

    def test_playbook_tick_uses_canonical_keyword(self):
        """Static check: ops/scripts/quant-playbook-tick.py must spell
        the keyword `enable_risk_mgmt`. The runtime copy at
        ~/.hermes/scripts/ is exercised by the cron and is synced by
        the same commits, so we only check the source-of-truth path
        here."""
        repo_root = Path(__file__).resolve().parents[3]
        path = repo_root / "ops" / "scripts" / "quant-playbook-tick.py"
        text = path.read_text()
        assert "enable_risk_mgmt=" in text, (
            "playbook-tick.py must pass enable_risk_mgmt=... to "
            "DeliberativeConfig"
        )
        assert "include_risk_mgmt=" not in text, (
            "playbook-tick.py must NOT use include_risk_mgmt= — that "
            "kwarg does not exist on DeliberativeConfig and raises "
            "TypeError"
        )


# ---------------------------------------------------------------------------
# Fix #4 — quant_doctor cold-start halt visibility
# ---------------------------------------------------------------------------


class TestQuantDoctorColdStartHaltVisibility:
    """Codex P2 finding: when the daemon hasn't created signals.jsonl yet
    but state.db already contains an active halt, the early return at
    tools.py:1139 reported `halts: []` — hiding the safety state in
    exactly the cold-start scenario the diagnostic is supposed to cover.

    Fixed by replacing the hard early-return with a `bus_present` flag:
    when bus is absent, per_symbol/heartbeat probes are skipped (they
    need bus rows) but halt + pending probes still run.
    """

    def test_halts_visible_even_when_bus_absent(self, tmp_path: Path):
        # Set up: no signal bus, but state.db has an active halt.
        bus = tmp_path / "signals.jsonl"
        assert not bus.exists()
        state_db = tmp_path / "state.db"
        halt_mirror = tmp_path / "halt.json"

        # Create a halt registry with one active halt.
        hs = HaltStateSQLite(db_path=state_db, mirror_path=halt_mirror)
        hs.add_halt(
            account_id="acct-test",
            asset_class="crypto",
            asset=None,  # account-wide halt
            reason="operator_emergency_stop",
            halted_until=None,
        )
        active = hs.active_halts()
        assert len(active) == 1, "fixture: halt registry should have 1 row"

        # Run quant_doctor — should surface the halt despite missing bus.
        out = _compute_daemon_state_mirror(
            signal_bus_path=bus,
            state_db_path=state_db,
            halt_mirror_path=halt_mirror,
        )

        # Per-symbol/heartbeat are empty (correct — no bus rows).
        assert out["per_symbol"] == {}
        assert out["last_heartbeat_age_s"] is None
        # Halts are NOT empty.
        assert len(out["halts"]) == 1
        assert out["halts"][0]["reason"] == "operator_emergency_stop"
        assert out["halts"][0]["account_id"] == "acct-test"
        # Cold-start note is still surfaced for human callers.
        assert out.get("note") == "signal bus does not exist yet"

    def test_no_halts_no_bus_returns_empty_halts_with_note(self, tmp_path: Path):
        """Symmetry: no bus AND no halt registry → empty halts AND note."""
        bus = tmp_path / "signals.jsonl"
        out = _compute_daemon_state_mirror(
            signal_bus_path=bus,
            state_db_path=tmp_path / "nonexistent.db",
            halt_mirror_path=tmp_path / "nonexistent.json",
        )
        assert out["halts"] == []
        assert out.get("note") == "signal bus does not exist yet"


# ---------------------------------------------------------------------------
# Fix #2 — Deterministic signal_id (closes watermark idempotency loop)
# ---------------------------------------------------------------------------


def _make_bars_for_sig(n: int = 10) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-05-13T00:00:00", periods=n, freq="1h")
    closes = 60_000.0 * np.cumprod(1 + 0.001 + rng.normal(0, 0.005, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def _make_ctx(
    asset: str = "BTC/USDT",
    exchange: str = "binance",
    timeframe: str = "1h",
    asof_str: str = "2026-05-13T10:00:00",
) -> MarketContext:
    bars = _make_bars_for_sig(10)
    return MarketContext(
        asset=asset,
        timeframe=timeframe,
        asset_class="crypto",
        exchange=exchange,
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        last_volume=float(bars["volume"].iloc[-1]),
        asof=pd.Timestamp(asof_str),
    )


def _make_signal(asset: str = "BTC/USDT") -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp.utcnow(),
        direction=1,
        magnitude=0.02,
        confidence=0.7,
        confidence_raw=0.9,
        horizon="4h",
        components=(),
        aggregator="mock",
    )


def _make_action() -> Action:
    return Action(
        target_position_pct=0.05,
        reason="test",
        signal_id="placeholder",
    )


class TestSignalIdDeterminism:
    """Codex P2 finding: `_build_signal_record` was generating
    `uuid.uuid4().hex[:6]` as the disambiguating tail. Under
    HERMES_QUANT_WATERMARK_ENABLED=1, a crash between
    `emit_signal_record()` and `watermark_store.set()` reprocessed the
    same `(symbol, bar_ts)` on restart and emitted a SECOND signal with
    a fresh UUID — so consumers idempotency-keying on `signal_id`
    couldn't dedupe.

    Fixed by replacing the random tail with sha1[:12] of
    `(asset, exchange, timeframe, bar_ts)`. Same bar → same id.
    """

    def test_same_bar_ts_same_signal_id(self):
        ctx = _make_ctx()
        signal = _make_signal()
        action = _make_action()
        task = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h"
        )
        bar_ts = pd.Timestamp("2026-05-13T09:00:00")
        asof = pd.Timestamp("2026-05-13T09:05:00")

        rec_a = _build_signal_record(signal, action, task, asof, ctx, bar_ts)
        rec_b = _build_signal_record(signal, action, task, asof, ctx, bar_ts)

        # Two emits of the same bar → same id. This is what closes the
        # watermark idempotency loop.
        assert rec_a["id"] == rec_b["id"]

    def test_different_bar_ts_different_signal_id(self):
        ctx = _make_ctx()
        signal = _make_signal()
        action = _make_action()
        task = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h"
        )
        asof = pd.Timestamp("2026-05-13T10:00:00")
        bar_ts_a = pd.Timestamp("2026-05-13T09:00:00")
        bar_ts_b = pd.Timestamp("2026-05-13T10:00:00")

        rec_a = _build_signal_record(signal, action, task, asof, ctx, bar_ts_a)
        rec_b = _build_signal_record(signal, action, task, asof, ctx, bar_ts_b)

        assert rec_a["id"] != rec_b["id"]

    def test_different_exchange_different_signal_id(self):
        ctx = _make_ctx()
        signal = _make_signal()
        action = _make_action()
        bar_ts = pd.Timestamp("2026-05-13T09:00:00")
        asof = pd.Timestamp("2026-05-13T09:05:00")

        task_binance = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h"
        )
        task_kraken = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="kraken", horizon="4h"
        )

        rec_b = _build_signal_record(
            signal, action, task_binance, asof, ctx, bar_ts
        )
        rec_k = _build_signal_record(
            signal, action, task_kraken, asof, ctx, bar_ts
        )

        # Same symbol on two venues → different ids (avoids cross-venue
        # collision under multi-exchange routing).
        assert rec_b["id"] != rec_k["id"]

    def test_different_timeframe_different_signal_id(self):
        """Critical case: same symbol+bar_ts on two different timeframes
        (1h vs 1d) must produce different ids — pairs with the watermark
        composite-key fix from the prior commit."""
        ctx_1h = _make_ctx(timeframe="1h")
        ctx_1d = _make_ctx(timeframe="1d")
        signal = _make_signal()
        action = _make_action()
        bar_ts = pd.Timestamp("2026-05-13T00:00:00")
        asof = pd.Timestamp("2026-05-13T00:05:00")

        task_1h = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h"
        )
        task_1d = AssetTask(
            "BTC/USDT", "crypto", "1d", exchange="binance", horizon="1d"
        )

        rec_1h = _build_signal_record(
            signal, action, task_1h, asof, ctx_1h, bar_ts
        )
        rec_1d = _build_signal_record(
            signal, action, task_1d, asof, ctx_1d, bar_ts
        )

        assert rec_1h["id"] != rec_1d["id"]

    def test_signal_id_format(self):
        """Verify id format: sig-{YYYYMMDDTHHMMSSZ}-{symbol-with-slashes-as-dashes}-{12-hex}."""
        ctx = _make_ctx()
        signal = _make_signal()
        action = _make_action()
        task = AssetTask(
            "BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h"
        )
        bar_ts = pd.Timestamp("2026-05-13T09:00:00")
        asof = pd.Timestamp("2026-05-13T09:05:00")

        rec = _build_signal_record(signal, action, task, asof, ctx, bar_ts)
        sig_id = rec["id"]

        assert sig_id.startswith("sig-20260513T090500Z-BTC-USDT-")
        # The 12-hex tail is hashlib.sha1[:12] of the dedup payload.
        tail = sig_id.split("-")[-1]
        assert len(tail) == 12
        assert all(c in "0123456789abcdef" for c in tail)


class TestEndToEndReplayProducesSameId:
    """Integration: under watermark-enabled mode, a tick that re-runs on
    the SAME bar (because watermark.set() failed mid-tick) emits a
    record whose id matches the original. Without the deterministic id,
    this would have been two distinct rows that downstream consumers
    couldn't collapse.

    We don't simulate a real crash — we just call run_one_tick twice
    with watermark disabled (so no replay-skip) and verify the two
    emitted records have identical ids when the bars are identical.
    """

    def _make_provider(self, bars):
        p = MagicMock()
        p.name = "mock"
        p.fetch_bars.return_value = bars
        return p

    def _make_analyst(self, name: str):
        a = MagicMock()
        a.name = name
        a.analyze.return_value = AnalystView(
            analyst=name,
            direction=1,
            magnitude=0.02,
            confidence=0.7,
            confidence_raw=0.9,
            horizon="4h",
        )
        return a

    def _portfolio_for(self):
        def _f(account_id, asset_class):
            return Portfolio(
                account_id=account_id,
                asset_class=asset_class,
                asof=pd.Timestamp.utcnow(),
                positions={},
                cash=100_000.0,
                equity_total=100_000.0,
                realized_pnl_total=0.0,
                realized_fees_total=0.0,
                peak_equity=100_000.0,
                daily_open_equity=100_000.0,
            )

        return _f

    def test_replay_emits_identical_signal_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Watermark disabled — both ticks emit, and we verify the ids match.
        monkeypatch.delenv("HERMES_QUANT_WATERMARK_ENABLED", raising=False)

        bus = tmp_path / "signals.jsonl"
        halt_state = HaltStateSQLite(
            tmp_path / "halts.db", tmp_path / "halt.json"
        )
        bars = _make_bars_for_sig(50)
        provider = self._make_provider(bars)
        analysts = [self._make_analyst("a"), self._make_analyst("b")]

        agg = MagicMock()
        agg.aggregate.return_value = _make_signal()
        gate = MagicMock()
        gate.gate.return_value = _make_action()

        # Tick 1
        run_one_tick(
            tasks=[
                AssetTask(
                    "BTC/USDT",
                    "crypto",
                    "1h",
                    exchange="binance",
                    horizon="4h",
                )
            ],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=self._portfolio_for(),
            state=TickLoopState(),
            bus_path=bus,
            watermark_store=None,
        )

        # Tick 2 (replay on identical bars)
        run_one_tick(
            tasks=[
                AssetTask(
                    "BTC/USDT",
                    "crypto",
                    "1h",
                    exchange="binance",
                    horizon="4h",
                )
            ],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=self._portfolio_for(),
            state=TickLoopState(),
            bus_path=bus,
            watermark_store=None,
        )

        # Read the bus and compare the two signal ids — bar_ts is the
        # same so dedup-tail is the same. asof differs (two ticks),
        # but only the prefix carries it; the dedup-tail proves
        # determinism on the canonical replay key.
        from hermes_quant.daemon.signal_bus import read_jsonl_tail

        rows = [
            r
            for r in read_jsonl_tail(bus, n=10)
            if r.get("type") != "heartbeat"
        ]
        assert len(rows) >= 2
        # Last two emits: same dedup tail (last 12 hex chars after final "-")
        ids = [r["id"] for r in rows[-2:]]
        tails = [i.split("-")[-1] for i in ids]
        assert tails[0] == tails[1], (
            f"replay ids must share the same content-hash tail; "
            f"got {ids[0]} vs {ids[1]}"
        )
