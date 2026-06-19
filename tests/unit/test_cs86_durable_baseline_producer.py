"""cs86 — producer-level wiring of cs01's durable drawdown baseline seam.

cs01 added a default-OFF `baseline_store=` seam to `DefaultRiskGate` (gate.py:414)
and a durable `DrawdownBaselineStore` (a monotonic HWM peak + session-anchored
daily-open in state.db). But the LIVE producer left cs01 INERT twice over:

1. `advisor.recommend()` constructs `DefaultRiskGate()` with NO store
   (advisor.py:1168 / recipes.py:286), AND
2. it feeds a SYNTHETIC flat 100k portfolio (`_synthetic_portfolio`,
   account_id "advisor-synthetic", eq=peak=open=100k -> drawdown always 0).

So a profitable-from-inception account that draws down past `max_drawdown_pct`
peak-to-trough does NOT trip Rule-1 through the producer today. cs86 wires it
behind the NEW default-OFF flag HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE:

- Flag OFF (production default) => producer is BYTE-IDENTICAL to today
  (DefaultRiskGate(), synthetic portfolio, NO store import/construct/write).
- Flag ON + a real (account, asset_class, equity) => producer builds the store
  (pointed at the live state.db dir), a REAL-equity portfolio, and a store-backed
  gate, so the durable HWM accumulates and the breaker trips end-to-end.
- Fail-CLOSED under flag-ON: a None/non-finite/<=0 NAV or a store-construct error
  returns a GATED result (never a silent synthetic-100k fall-open).

These tests drive the FULL producer (`advisor.recommend`) via DI stubs and the
tick forwarding, all against a tmp_path state.db (the live ~/.hermes is NEVER
touched).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.protocol import AggregatedSignal, AnalystView

FLAG = "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE"


# ---------------------------------------------------------------------------
# Producer DI stubs — drive recommend() to Step 7 with a fireable signal.
# ---------------------------------------------------------------------------


def _bars(asof: pd.Timestamp, n: int = 60) -> pd.DataFrame:
    """Minimal valid ascending UTC OHLCV frame ending at `asof`."""
    idx = pd.date_range(end=asof, periods=n, freq="1h", tz="UTC")
    close = pd.Series(range(100, 100 + n), dtype=float).values
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [1_000.0] * n,
        }
    )


class _StubProvider:
    def fetch_bars(self, symbol, timeframe, start, end, as_of=None):
        anchor = as_of if as_of is not None else pd.Timestamp("2026-05-13T12:00:00Z")
        return _bars(anchor)


class _StubAnalyst:
    name = "stub-bull"

    def analyze(self, ctx) -> AnalystView:
        return AnalystView(
            analyst="stub-bull",
            direction=1,
            magnitude=0.05,
            confidence=0.9,
            confidence_raw=0.95,
            horizon="1h",
            rationale="stub bullish",
        )


class _StubAggregator:
    """Returns a fireable (direction=1) AggregatedSignal regardless of inputs."""

    def aggregate(self, views, ctx) -> AggregatedSignal:
        return AggregatedSignal(
            asset=ctx.asset,
            timeframe=ctx.timeframe,
            asset_class=ctx.asset_class,
            asof=ctx.asof,
            direction=1,
            magnitude=0.05,
            confidence=0.9,
            confidence_raw=0.95,
            horizon="1h",
            components=tuple(views),
            aggregator="stub",
        )


def _recommend(durable_equity_account, *, asof="2026-05-13T12:00:00Z"):
    from hermes_quant.advisor import recommend

    return recommend(
        "BTC/USDT",
        asset_class="crypto",
        timeframe="1h",
        as_of=asof,
        include_lessons=False,
        provider=_StubProvider(),
        analysts=[_StubAnalyst()],
        aggregator=_StubAggregator(),
        durable_equity_account=durable_equity_account,
    )


@pytest.fixture
def tmp_store(monkeypatch, tmp_path: Path):
    """Redirect the no-arg DrawdownBaselineStore() default paths to tmp_path.

    The producer constructs `DrawdownBaselineStore()` (no args); its default-arg
    paths are bound at class-definition time, so we wrap the class to inject the
    tmp_path db/mirror. Returns the (db_path, mirror_path) the producer will use.
    """
    import hermes_quant.risk.baseline_store as bs

    db_path = tmp_path / "state.db"
    mirror_path = tmp_path / "drawdown_baselines.json"
    real_cls = bs.DrawdownBaselineStore

    def _factory(db_path=db_path, mirror_path=mirror_path):
        return real_cls(db_path=db_path, mirror_path=mirror_path)

    monkeypatch.setattr(bs, "DrawdownBaselineStore", _factory)
    return db_path, mirror_path


def _row(db_path: Path, account_id: str, asset_class: str):
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT peak_equity, daily_open_equity FROM drawdown_baselines "
            "WHERE account_id=? AND asset_class=?",
            (account_id, asset_class),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. GREEN end-to-end — flag ON, the producer trips Rule-1.
# ---------------------------------------------------------------------------


def test_green_producer_trips_drawdown_when_flag_on(monkeypatch, tmp_store):
    """Flag ON: a profitable-from-inception account (peak 130k) that falls to 104k
    (~20% peak-to-trough) trips Rule-1 END-TO-END through recommend() — and the
    PRODUCER itself built the HWM (proving observation, not just gate)."""
    monkeypatch.setenv(FLAG, "1")
    db_path, _mirror = tmp_store

    # Tick 1: observe equity 130k (the producer reconciles it -> durable peak 130k).
    r1 = _recommend(("paper-default", "crypto", 130_000.0))
    assert r1["risk_gate"]["gated_reason"] is None or "drawdown" not in str(
        r1["risk_gate"].get("gated_reason", "")
    )

    # Tick 2: equity falls to 104k same partition -> drawdown vs durable peak 130k.
    r2 = _recommend(("paper-default", "crypto", 104_000.0))
    rg = r2["risk_gate"]
    # A drawdown halt is a zero-target flatten action -> "gated_flatten".
    assert rg["recommended_action"] == "gated_flatten"
    assert rg["pass"] is False
    assert "drawdown_circuit_breaker" in rg["gated_reason"]
    # (130000 - 104000) / 130000 = 0.2000 > 0.15
    assert "0.2000" in rg["gated_reason"]

    # The PRODUCER persisted the durable HWM for the REAL partition.
    row = _row(db_path, "paper-default", "crypto")
    assert row is not None
    assert row["peak_equity"] == pytest.approx(130_000.0)


# ---------------------------------------------------------------------------
# 2. RED today / flag OFF — byte-identical: no trip, no store, no write.
# ---------------------------------------------------------------------------


def test_flag_off_byte_identical_no_trip_no_write(monkeypatch, tmp_store):
    """Flag UNSET (default OFF): even with a drawn-down NAV param leaked in, the
    producer ignores it -> synthetic flat portfolio + no store -> NO drawdown trip
    and ZERO state.db writes. This is the byte-identical safety guarantee."""
    monkeypatch.delenv(FLAG, raising=False)
    db_path, mirror_path = tmp_store

    # Pre-build the HWM history would be irrelevant: with the flag OFF nothing is
    # ever observed. Pass a drawn-down NAV to prove the param is inert without flag.
    r = _recommend(("paper-default", "crypto", 104_000.0))
    rg = r["risk_gate"]

    # No durable drawdown trip: the synthetic flat 100k reports drawdown 0, so the
    # only gate outcome is a non-drawdown one (flat-silence or a normal pass/size).
    assert "drawdown_circuit_breaker" not in str(rg)

    # ZERO new state.db rows + no JSON mirror written.
    assert _row(db_path, "paper-default", "crypto") is None
    assert _row(db_path, "advisor-synthetic", "crypto") is None
    assert not mirror_path.exists()


def test_flag_off_uses_synthetic_portfolio_and_no_store(monkeypatch):
    """White-box: flag OFF, the gate the producer hands to .gate() has
    baseline_store None and the portfolio is the synthetic 'advisor-synthetic'
    100k one — byte-identical to advisor.py:1165-1171 today."""
    monkeypatch.delenv(FLAG, raising=False)

    captured = {}

    from hermes_quant.risk.gate import DefaultRiskGate as _RealGate

    orig_gate = _RealGate.gate

    def _spy_gate(self, signal, market, portfolio, halt_state):
        captured["baseline_store"] = self.baseline_store
        captured["account_id"] = portfolio.account_id
        captured["equity_total"] = portfolio.equity_total
        return orig_gate(self, signal, market, portfolio, halt_state)

    monkeypatch.setattr(_RealGate, "gate", _spy_gate)
    _recommend(("paper-default", "crypto", 104_000.0))

    assert captured["baseline_store"] is None
    assert captured["account_id"] == "advisor-synthetic"
    assert captured["equity_total"] == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# 3. Fail-CLOSED under flag-ON — NAV/store failure never re-opens the breaker.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_nav", [None, float("nan"), float("inf"), 0.0, -5.0])
def test_flag_on_bad_nav_fails_closed(monkeypatch, tmp_store, bad_nav):
    """Flag ON + a None/non-finite/<=0 NAV => GATED (durable_baseline_nav_unavailable),
    never a silent synthetic-100k pass."""
    monkeypatch.setenv(FLAG, "1")
    r = _recommend(("paper-default", "crypto", bad_nav))
    rg = r["risk_gate"]
    assert rg["recommended_action"] == "gated"
    assert rg["pass"] is False
    assert rg["gated_reason"] == "durable_baseline_nav_unavailable"


def test_flag_on_store_construct_error_fails_closed(monkeypatch):
    """Flag ON + DrawdownBaselineStore construction raises => GATED
    (durable_baseline_store_error), never a fall-open to the synthetic 100k."""
    monkeypatch.setenv(FLAG, "1")
    import hermes_quant.risk.baseline_store as bs

    def _boom(*a, **k):
        raise OSError("simulated state.db open failure")

    monkeypatch.setattr(bs, "DrawdownBaselineStore", _boom)
    r = _recommend(("paper-default", "crypto", 104_000.0))
    rg = r["risk_gate"]
    assert rg["recommended_action"] == "gated"
    assert rg["pass"] is False
    assert rg["gated_reason"] == "durable_baseline_store_error"


# ---------------------------------------------------------------------------
# 4. Tick forwarding smoke — tick threads the NAV into recommend() ON the flag,
#    and passes None (byte-identical call shape) OFF the flag.
# ---------------------------------------------------------------------------


def _watch_entry(symbol="BTC/USDT", asset_class="crypto", timeframe="1h"):
    from hermes_quant.watchlist import WatchlistEntry

    return WatchlistEntry(symbol=symbol, asset_class=asset_class, timeframe=timeframe)


def test_tick_forwards_durable_equity_account_when_flag_on(monkeypatch):
    """Flag ON: tick resolves the real NAV once and threads
    durable_equity_account=('paper-default', asset_class, nav) into recommend()."""
    monkeypatch.setenv(FLAG, "1")
    import hermes_quant.autonomous as auto

    # Pin autonomous mode (the tick's mode-gate would otherwise return early on a
    # config-less / cold home where _read_pdr_mode() correctly defaults to "advise").
    # This is the established idiom for every other autonomous tick test
    # (test_autonomous_admissibility_units.py / test_autonomous_tick_direction_bias.py);
    # omitting it made these tests silently ride the live ~/.hermes/config.yaml leak,
    # which the ADR-0092 home-decouple (4aafaf3) closed.
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 123_456.0)

    seen = {}

    def _spy_recommend(*, symbol, asset_class, timeframe, include_lessons,
                       perception_frame=None, durable_equity_account=None, **kw):
        seen["durable_equity_account"] = durable_equity_account
        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "aggregated_signal": None,
            "risk_gate": {"pass": False, "recommended_action": "gated",
                          "gated_reason": "x", "kelly_fraction": 0.0},
        }

    # No semantic frame fetch noise.
    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    auto.tick(dry_run=True, symbols=[_watch_entry()], advisor_recommend=_spy_recommend)

    assert seen["durable_equity_account"] == ("paper-default", "crypto", 123_456.0)


def test_tick_passes_none_when_flag_off(monkeypatch):
    """Flag OFF: tick passes durable_equity_account=None (default) -> byte-identical
    call shape, no NAV resolved for the durable path."""
    monkeypatch.delenv(FLAG, raising=False)
    import hermes_quant.autonomous as auto

    # Pin autonomous mode (see the flag-ON sibling above) — the mode-gate would
    # otherwise short-circuit the tick on a config-less / cold home.
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")

    seen = {"called": False, "value": "sentinel"}

    def _spy_recommend(*, symbol, asset_class, timeframe, include_lessons,
                       perception_frame=None, durable_equity_account=None, **kw):
        seen["called"] = True
        seen["value"] = durable_equity_account
        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "aggregated_signal": None,
            "risk_gate": {"pass": False, "recommended_action": "gated",
                          "gated_reason": "x", "kelly_fraction": 0.0},
        }

    monkeypatch.setenv("HERMES_QUANT_SEMANTIC_ENABLED", "0")
    auto.tick(dry_run=True, symbols=[_watch_entry()], advisor_recommend=_spy_recommend)

    assert seen["called"] is True
    assert seen["value"] is None
