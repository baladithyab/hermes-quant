"""aegis-agmon1 (iter-5 REBUILD): options/combo per-position stop-loss sweep.

NON-VACUOUS by construction — every test runs against the REAL CompositePlaysStore
(``open_composite`` with real {symbol, side, position_intent} leg dicts) and a REAL
FIXTURE parquet chain that the REAL ``ChainSnapshotReader.replay_chain`` reads. NO
``_Store`` / ``_Mleg`` / ``_Leg`` doubles, NO injected mark callable — the discarded
build's vacuity (doubles that injected a ``ratio_qty`` attr the dict legs lack + a
negative ``net_entry_price`` the real store never stores) is structurally impossible
here: the sweep reads ``store.list_open()[].option_legs`` (the ml00b dicts) and marks
them off the fixture parquet via ``_resolve_options_leg_mark``.

RED-PROOF of non-vacuity: delete the fixture parquet => the sweep can resolve NO mark
=> HOLD (no fire). Restore it => the breach fires. The mark MUST flow through the real
parquet reader; a sweep that could not see the real legs/marks would HOLD and the
fire-test would FAIL.

POSTURE: silence-by-default + fail-CLOSED. A missing parquet / missing OCC row / NaN
mid HOLDS the whole composite — never a partial mark, never a fabricated close.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.state.composite_plays import CompositePlaysStore

# --------------------------------------------------------------------------- #
# REAL fixtures: a tmp QUANT_HOME with a real state.db composite store + a real
# parquet option chain the real ChainSnapshotReader reads.
# --------------------------------------------------------------------------- #

ASOF = datetime(2026, 6, 18, 16, 0, tzinfo=UTC)

# A bull-put-spread on AAPL: SOLD the 195P (sell_to_open), BOUGHT the 190P
# (buy_to_open). Net CREDIT 1.50/spread => signed net_debit_credit = -1.50.
_P195 = "AAPL260717P00195000"
_P190 = "AAPL260717P00190000"


def _chain_row(symbol: str, *, bid: float, ask: float) -> dict:
    """One parquet row in the EXACT schema ChainSnapshotReader._row_to_snapshot reads
    (mirrors tests/options/test_iv_rank.py::_row). The mark the sweep uses is mid =
    (bid + ask) / 2."""
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
    """Write a real AAPL chain parquet to qhome/option_chains/AAPL/<asof-date>.parquet."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = qhome / "option_chains" / "AAPL" / f"{ASOF.date():%Y-%m-%d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), path)
    return path


def _open_credit_spread(store: CompositePlaysStore, *, net_entry_price: float = -1.50) -> None:
    """Open a REAL composite row carrying real leg dicts (the ml00b shape)."""
    store.open_composite(
        multi_leg_id="ml_1",
        underlying="AAPL",
        strategy_kind="bull_put_spread",
        outer_qty=1,
        net_entry_price=net_entry_price,  # SIGNED: -1.50 = a credit structure
        fill_size_pct=0.0,
        expected_leg_count=2,
        max_loss=500.0,
        option_legs=[
            {"symbol": _P195, "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": _P190, "side": "buy", "position_intent": "buy_to_open"},
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
# 1. BREACH FIRES — a deep-loss credit spread fires a close, marked off the REAL
#    fixture parquet via the REAL replay-chain reader (the close routes through
#    the real armed reactor).
# --------------------------------------------------------------------------- #
def test_options_stop_fires_close_when_deep_loss(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")  # arm the close reactor
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)  # received 1.50 credit

    # Marks that blow the spread out: short 195P now mid 5.00, long 190P mid 0.50.
    # net cost-to-close = +5.00 (pay to buy back short) - 0.50 (sell long) = +4.50.
    # credit 1.50 - 4.50 = -3.00 pnl => loss_pct = 3.00 / 1.50 = 200% >> stop.
    _write_chain(
        qhome,
        [
            _chain_row(_P195, bid=4.95, ask=5.05),  # mid 5.00
            _chain_row(_P190, bid=0.45, ask=0.55),  # mid 0.50
        ],
    )

    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store,
        stop_pct=0.50,
        mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF,
        result=result,
    )
    assert "ml_1" in closed, "a deep-loss credit spread must fire a close"
    fired = [d for d in result.decisions if d.gate == "OPTIONS_PER_POSITION_STOP_FIRED"]
    assert len(fired) == 1
    assert fired[0].asset_class == "multi_leg"
    assert fired[0].details["loss_pct"] == pytest.approx(2.0)
    assert result.fires == 1


# --------------------------------------------------------------------------- #
# 2. NON-VACUITY RED-PROOF — DELETE the fixture parquet => the SAME breaching
#    composite HOLDs (no mark resolvable). This is the test that would FAIL if the
#    sweep could fabricate a mark / could not see the real chain.
# --------------------------------------------------------------------------- #
def test_missing_chain_holds_even_at_breach(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # NO parquet written -> replay_chain raises ChainQualityError -> mark source None.

    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store,
        stop_pct=0.50,
        mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF,
        result=result,
    )
    assert closed == set(), "no replayable chain => HOLD (never fabricate a close)"
    assert result.fires == 0
    assert not [d for d in result.decisions if d.gate == "OPTIONS_PER_POSITION_STOP_FIRED"]


# --------------------------------------------------------------------------- #
# 3. WINNER HELD — a credit spread that has DECAYED (cheapened) is a winner, not a
#    stop candidate. Marked off the REAL fixture.
# --------------------------------------------------------------------------- #
def test_options_winner_not_stopped(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # Legs decayed: short 195P mid 0.20, long 190P mid 0.05. cost-to-close =
    # +0.20 - 0.05 = +0.15. pnl = 1.50 - 0.15 = +1.35 (a winner) -> loss_pct = 0.0.
    _write_chain(
        qhome,
        [
            _chain_row(_P195, bid=0.18, ask=0.22),
            _chain_row(_P190, bid=0.03, ask=0.07),
        ],
    )
    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store, stop_pct=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result,
    )
    assert closed == set(), "a decayed (winning) credit spread must NOT fire the stop"
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 4. NaN MID HOLDS — a leg with a missing (NaN-decoded) mid makes the whole
#    composite unmarkable => HOLD (fail-CLOSED, never partial-mark).
# --------------------------------------------------------------------------- #
def test_nan_leg_mark_holds(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # The long leg has NO bid/ask (mid -> None). The short leg is deep ITM (would
    # breach if marked alone) — but one unmarkable leg HOLDS the whole composite.
    short = _chain_row(_P195, bid=4.95, ask=5.05)
    long_missing = _chain_row(_P190, bid=0.45, ask=0.55)
    long_missing["bid"] = float("nan")
    long_missing["ask"] = float("nan")
    _write_chain(qhome, [short, long_missing])

    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store, stop_pct=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result,
    )
    assert closed == set(), "a NaN/missing leg mark must HOLD the whole composite"
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 5. MISSING OCC ROW HOLDS — a chain that does not contain one of the legs' OCC
#    symbols makes that leg unmarkable => HOLD.
# --------------------------------------------------------------------------- #
def test_missing_occ_row_holds(qhome: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    # The chain has the short leg + an UNRELATED contract (>=2 rows so replay_chain
    # does not raise), but NOT the long leg the composite needs.
    _write_chain(
        qhome,
        [
            _chain_row(_P195, bid=4.95, ask=5.05),
            _chain_row("AAPL260717P00180000", bid=0.10, ask=0.20),  # unrelated
        ],
    )
    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store, stop_pct=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result,
    )
    assert closed == set(), "a leg whose OCC is absent from the chain must HOLD"
    assert result.fires == 0


# --------------------------------------------------------------------------- #
# 6. FLAG GATES the wired path — _maybe_run_options_stop_sweep is INERT when
#    HERMES_QUANT_OPTIONS_MONITOR is unset (no store read, byte-identical no-op),
#    even with a breaching composite + a real chain on disk.
# --------------------------------------------------------------------------- #
def test_options_monitor_flag_gates_wired_sweep(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_MONITOR", raising=False)
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=4.95, ask=5.05), _chain_row(_P190, bid=0.45, ask=0.55)],
    )
    result = _result()
    closed = auto._maybe_run_options_stop_sweep(stop_pct=0.50, asof=ASOF, result=result)
    assert closed == set(), "flag OFF => inert (no composite store read, no fire)"
    assert result.fires == 0

    # Flag ON => the wired path reads the real store + real chain and fires.
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_MONITOR", "1")
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    result2 = _result()
    closed2 = auto._maybe_run_options_stop_sweep(stop_pct=0.50, asof=ASOF, result=result2)
    assert "ml_1" in closed2, "flag ON => the wired sweep fires the close"
    assert result2.fires == 1


# --------------------------------------------------------------------------- #
# 7. DISABLED REACTOR HOLDS — a real breach, marked off the real chain, but the
#    multi-leg reactor is OFF (production default) => the close is a no-fill =>
#    the composite stays open (HOLD), recorded as a silence (fail-CLOSED).
# --------------------------------------------------------------------------- #
def test_disabled_reactor_is_nofill_hold(
    qhome: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)  # reactor OFF
    store = CompositePlaysStore(db_path=qhome / "state.db")
    _open_credit_spread(store, net_entry_price=-1.50)
    _write_chain(
        qhome,
        [_chain_row(_P195, bid=4.95, ask=5.05), _chain_row(_P190, bid=0.45, ask=0.55)],
    )
    result = _result()
    closed = auto._run_options_position_stop_sweep(
        store=store, stop_pct=0.50, mark_leg_for=auto._resolve_options_leg_mark,
        asof=ASOF, result=result,
    )
    assert closed == set(), "reactor OFF => the close is a no-fill => composite stays open"
    assert result.fires == 0
    nofill = [d for d in result.decisions if d.gate == "OPTIONS_PER_POSITION_STOP_NO_FILL"]
    assert len(nofill) == 1
    assert result.silences == 1
