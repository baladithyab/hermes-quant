"""aegis-agmon2 (iter-5 REBUILD): options-aware TAKE-PROFIT sweep.

NON-VACUOUS by construction — every test runs against the REAL CompositePlaysStore
(``open_composite`` with real {symbol, side, position_intent} leg dicts) and a REAL
FIXTURE parquet chain the REAL ``ChainSnapshotReader.replay_chain`` reads. NO doubles,
NO injected mark callable.

For a CREDIT structure the max gain is the full credit; the sweep fires a structure-aware
CLOSE (BUY_TO_CLOSE the short legs FIRST) when >= tp_fraction (0.50) of max gain is
captured. STOP PRECEDENCE: a composite the stop sweep already closed this tick is SKIPPED.
A DEBIT structure (no bounded max-gain source) returns None gain -> HOLD (deferred).

RED-PROOF of non-vacuity: delete the fixture parquet => HOLD even when the structure has
fully decayed (the gain can't be computed without the real marks). Restore => fire.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.state.composite_plays import CompositePlaysStore

ASOF = datetime(2026, 6, 18, 16, 0, tzinfo=UTC)

_P195 = "AAPL260717P00195000"
_P190 = "AAPL260717P00190000"


def _chain_row(symbol: str, *, bid: float, ask: float) -> dict:
    return {
        "contract_symbol": symbol,
        "asof": ASOF,
        "fetched_at": ASOF,
        "underlying_spot": 150.0,
        "risk_free_rate": 0.05,
        "bid": bid,
        "ask": ask,
        "last": (bid + ask) / 2.0,
        "volume": 100,
        "open_interest": 500,
        "delta": -0.30,
        "gamma": 0.01,
        "theta": -0.05,
        "vega": 0.10,
        "rho": 0.02,
        "iv": 0.45,
        "iv_source": "provider",
    }


def _write_chain(qhome: Path, rows: list[dict]) -> Path:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = qhome / "option_chains" / "AAPL" / f"{ASOF.date():%Y-%m-%d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), path)
    return path


def _open_credit_spread(store: CompositePlaysStore, *, net_entry_price: float = -1.50) -> None:
    store.open_composite(
        multi_leg_id="ml_1",
        underlying="AAPL",
        strategy_kind="bull_put_spread",
        outer_qty=1,
        net_entry_price=net_entry_price,
        fill_size_pct=0.0,
        expected_leg_count=2,
        max_loss=500.0,
        option_legs=[
            {"symbol": _P195, "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": _P190, "side": "buy", "position_intent": "buy_to_open"},
        ],
    )


def _open_debit_spread(store: CompositePlaysStore) -> None:
    """A DEBIT vertical: BOUGHT the 195C, SOLD the 190C; net DEBIT +1.50 (paid)."""
    store.open_composite(
        multi_leg_id="ml_debit",
        underlying="AAPL",
        strategy_kind="vertical_spread",
        outer_qty=1,
        net_entry_price=1.50,  # SIGNED +1.50 = a debit structure
        fill_size_pct=0.0,
        expected_leg_count=2,
        max_loss=150.0,
        option_legs=[
            {"symbol": "AAPL260717C00190000", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": "AAPL260717C00195000", "side": "sell", "position_intent": "sell_to_open"},
        ],
    )


@pytest.fixture
def qhome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "quant"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auto, "QUANT_HOME", home)
    return home


def _result() -> auto.TickResult:
    return auto.TickResult(asof="x", mode="autonomous", dry_run=False, watchlist_size=0)


# --------------------------------------------------------------------------- #
# 1. FIRES AT >= 50% OF MAX GAIN — a credit spread that decayed to half its credit.
# --------------------------------------------------------------------------- #
def test_tp_fires_at_half_max_gain(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)  # credit 1.50, max gain 1.50
    # Decayed: short 195P mid 0.70, long 190P mid 0.05 => cost-to-close = +0.70 - 0.05 = 0.65.
    # pnl = 1.50 - 0.65 = 0.85; gain fraction = 0.85 / 1.50 = 0.567 >= 0.50 -> TP fires.
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=0.68, ask=0.72), _chain_row(_P190, bid=0.03, ask=0.07)],
    )
    result = _result()
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed=set(),
    )
    assert "ml_1" in closed
    fired = [d for d in result.decisions if d.gate == "OPTIONS_PER_POSITION_TAKE_PROFIT_FIRED"]
    assert len(fired) == 1
    assert fired[0].details["gain_pct"] == pytest.approx(0.85 / 1.50)
    # structure-aware: the SHORT leg (195P) is closed FIRST.
    assert fired[0].details["close_order"][0] == _P195
    assert result.fires == 1


# --------------------------------------------------------------------------- #
# 2. NON-VACUITY RED-PROOF — delete the parquet => HOLD even when decayed.
# --------------------------------------------------------------------------- #
def test_missing_chain_holds_tp(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # NO parquet -> no mark -> HOLD.
    result = _result()
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed=set(),
    )
    assert closed == set()
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 3. BELOW THRESHOLD HELD — a credit spread that has only decayed a little.
# --------------------------------------------------------------------------- #
def test_below_threshold_held(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # cost-to-close = +1.20 - 0.05 = 1.15. pnl = 1.50 - 1.15 = 0.35; gain = 0.233 < 0.50.
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=1.18, ask=1.22), _chain_row(_P190, bid=0.03, ask=0.07)],
    )
    result = _result()
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed=set(),
    )
    assert closed == set(), "below the TP threshold must HOLD"
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 4. STOP PRECEDENCE — a composite the stop sweep closed this tick is SKIPPED.
# --------------------------------------------------------------------------- #
def test_stop_precedence_skips_already_closed(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # A TP-qualifying mark (would fire) ...
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=0.68, ask=0.72), _chain_row(_P190, bid=0.03, ask=0.07)],
    )
    result = _result()
    # ... but the stop already closed ml_1 THIS tick -> the TP sweep must skip it.
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed={"ml_1"},
    )
    assert closed == set(), "STOP PRECEDENCE: an already-closed composite is never double-acted"
    assert result.fires == 0
    assert not [d for d in result.decisions if d.gate.startswith("OPTIONS_PER_POSITION_TAKE_PROFIT")]


# --------------------------------------------------------------------------- #
# 5. NaN MARK HOLDS — a missing leg mid HOLDs the whole composite on the TP side too.
# --------------------------------------------------------------------------- #
def test_nan_mark_holds_tp(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    short = _chain_row(_P195, bid=0.68, ask=0.72)
    long_missing = _chain_row(_P190, bid=float("nan"), ask=float("nan"))
    _write_chain(qhome, [short, long_missing])
    result = _result()
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed=set(),
    )
    assert closed == set()
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 6. DEBIT STRUCTURE DEFERRED — a debit spread (net_entry_price > 0) has no bounded
#    max-gain source on this path, so _options_position_gain_pct returns None -> HOLD.
# --------------------------------------------------------------------------- #
def test_debit_structure_returns_none_holds(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_debit_spread(store)
    # Even a hugely-profitable mark must HOLD (no bounded max-gain source).
    _write_chain(
        qhome,
        [
            _chain_row("AAPL260717C00190000", bid=9.95, ask=10.05),  # long way ITM
            _chain_row("AAPL260717C00195000", bid=5.95, ask=6.05),
        ],
    )
    result = _result()
    closed = auto._run_options_position_tp_sweep(
        store=store, tp_fraction=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result, already_closed=set(),
    )
    assert closed == set(), "a debit structure with no max-gain source is HELD (deferred)"
    assert result.fires == 0
    # The gain-pct helper itself returns None for a debit structure.
    assert auto._options_position_gain_pct(net_entry_price=1.50, net_close_cost=-4.0) is None


# --------------------------------------------------------------------------- #
# 7. WIRED PATH — _maybe_run_options_stop_sweep runs the TP sweep ONLY when
#    HERMES_QUANT_TAKE_PROFIT_SWEEP=1 (with OPTIONS_MONITOR=1). Real store + chain.
# --------------------------------------------------------------------------- #
def test_take_profit_sweep_flag_gates_tp(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_MONITOR", "1")
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", raising=False)
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # A TP-qualifying mark but NOT a stop breach (a winner): cost-to-close 0.65 < credit.
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=0.68, ask=0.72), _chain_row(_P190, bid=0.03, ask=0.07)],
    )
    # TAKE_PROFIT_SWEEP OFF: the stop sweep runs (no breach -> nothing) and NO TP fires.
    result = _result()
    closed = auto._maybe_run_options_stop_sweep(stop_pct=0.50, asof=ASOF, result=result)
    assert closed == set(), "TAKE_PROFIT_SWEEP OFF => no options TP fire"
    assert not [d for d in result.decisions if d.gate.startswith("OPTIONS_PER_POSITION_TAKE_PROFIT")]

    # TAKE_PROFIT_SWEEP ON: the TP sweep fires the decayed winner.
    monkeypatch.setenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", "1")
    result2 = _result()
    closed2 = auto._maybe_run_options_stop_sweep(stop_pct=0.50, asof=ASOF, result=result2)
    assert "ml_1" in closed2, "TAKE_PROFIT_SWEEP ON => the TP sweep fires the close"
    assert result2.fires == 1
