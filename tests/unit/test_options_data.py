"""Unit tests for hermes_quant.options.data (Wave B2).

Deterministic, no network. Per plan §2.2 + ADR-0027 amendment golden cases.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    ChainQualityError,
    ChainSnapshotReader,
    GreekComputationError,
    LiveChainDisabled,
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
    _complete_greeks_or_fail,
    aggregate_net_greeks,
)


def _opt(
    symbol: str,
    side: str,
    *,
    delta: float | None,
    gamma: float = 0.0,
    theta: float = 0.0,
    vega: float = 0.0,
    rho: float = 0.0,
    ratio_qty: int = 1,
    intent: str | None = None,
) -> OptionLeg:
    if intent is None:
        intent = "buy_to_open" if side == "buy" else "sell_to_open"
    return OptionLeg(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        position_intent=intent,  # type: ignore[arg-type]
        ratio_qty=ratio_qty,
        greeks_at_decision=OptionGreeksSnapshot(
            delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho
        ),
    )


# ---------------------------------------------------------------------------
# aggregate_net_greeks golden cases (ADR-0027 amendments)
# ---------------------------------------------------------------------------


def test_aggregate_covered_call_includes_stock_delta() -> None:
    """100 long shares + 1 short 0.30Δ call -> net.delta == 70."""
    legs = [
        StockLeg(underlying="NVDA", qty=100),
        _opt("NVDA260612C00150000", "sell", delta=0.30),
    ]
    net = aggregate_net_greeks(legs)
    assert net.delta == pytest.approx(70.0)


def test_aggregate_csp_no_stock_leg() -> None:
    """1 short 0.30Δ put -> net.delta == 30 (sign: short of a -0.30Δ put).

    A put delta is negative; here greeks_at_decision.delta is provided as the
    put's own (negative) delta is modeled by passing delta=-0.30. Plan golden:
    CSP only (1 short 0.30Δ put) -> net.delta == 30."""
    legs = [_opt("NVDA260612P00140000", "sell", delta=-0.30)]
    net = aggregate_net_greeks(legs)
    assert net.delta == pytest.approx(30.0)


def test_aggregate_short_stock_negative_delta() -> None:
    """-100 short shares + 1 long 0.30Δ call -> net.delta == -70."""
    legs = [
        StockLeg(underlying="NVDA", qty=-100),
        _opt("NVDA260612C00150000", "buy", delta=0.30),
    ]
    net = aggregate_net_greeks(legs)
    assert net.delta == pytest.approx(-70.0)


def test_aggregate_debit_call_vertical() -> None:
    """Buy 0.30Δ / sell 0.18Δ same expiry -> net.delta == +12 (research §2.3)."""
    legs = [
        _opt("NVDA260612C00140000", "buy", delta=0.30),
        _opt("NVDA260612C00150000", "sell", delta=0.18),
    ]
    net = aggregate_net_greeks(legs)
    assert net.delta == pytest.approx(12.0)


def test_aggregate_zero_shares_no_contribution() -> None:
    base = [_opt("NVDA260612P00140000", "sell", delta=-0.30)]
    with_zero = [*base, StockLeg(underlying="NVDA", qty=0)]
    assert aggregate_net_greeks(base).delta == aggregate_net_greeks(with_zero).delta


def test_aggregate_unknown_leg_type_raises() -> None:
    class Bogus:
        pass

    with pytest.raises(TypeError):
        aggregate_net_greeks([Bogus()])  # type: ignore[list-item]


def test_aggregate_missing_greeks_fail_closed() -> None:
    leg = OptionLeg(
        symbol="NVDA260612C00150000",
        side="sell",
        position_intent="sell_to_open",
        greeks_at_decision=None,
    )
    with pytest.raises(GreekComputationError):
        aggregate_net_greeks([leg])


def test_aggregate_ratio_qty_scales() -> None:
    legs = [_opt("NVDA260612C00150000", "buy", delta=0.50, ratio_qty=2)]
    assert aggregate_net_greeks(legs).delta == pytest.approx(100.0)


def test_aggregate_order_qty_default_is_one_lot() -> None:
    """Default order_qty=1 keeps the single-lot footprint (bit-for-bit prior)."""
    legs = [_opt("NVDA260612C00150000", "buy", delta=0.50)]
    assert aggregate_net_greeks(legs).delta == pytest.approx(50.0)
    assert aggregate_net_greeks(legs, order_qty=1).delta == pytest.approx(50.0)


def test_aggregate_order_qty_scales_multi_contract() -> None:
    """Bug 6: a multi-contract order's greeks scale by ratio_qty*order_qty*100,
    NOT a single lot. A 10-contract 0.50Δ call is 500 delta, not 50 — scaling by
    one lot would let a 10-lot order slip past per-lot greek caps."""
    legs = [_opt("NVDA260612C00150000", "buy", delta=0.50)]
    assert aggregate_net_greeks(legs, order_qty=10).delta == pytest.approx(500.0)


def test_aggregate_order_qty_compounds_with_ratio_qty() -> None:
    """units = ratio_qty * order_qty * 100: ratio_qty=2, order_qty=3 -> 600Δ."""
    legs = [_opt("NVDA260612C00150000", "buy", delta=0.50, ratio_qty=2)]
    assert aggregate_net_greeks(legs, order_qty=3).delta == pytest.approx(300.0)


def test_aggregate_order_qty_does_not_scale_stock_qty() -> None:
    """Stock qty is an absolute share count -> NOT multiplied by order_qty."""
    legs = [StockLeg(underlying="NVDA", qty=100)]
    assert aggregate_net_greeks(legs, order_qty=5).delta == pytest.approx(100.0)


def test_aggregate_full_greek_vector() -> None:
    legs = [
        _opt(
            "NVDA260612C00150000",
            "sell",
            delta=0.30,
            gamma=0.01,
            theta=-0.05,
            vega=0.10,
            rho=0.02,
        )
    ]
    net = aggregate_net_greeks(legs)
    assert net.delta == pytest.approx(-30.0)
    assert net.gamma == pytest.approx(-1.0)
    assert net.theta == pytest.approx(5.0)  # short collects decay -> +theta
    assert net.vega == pytest.approx(-10.0)
    assert net.rho == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# NetGreeks vector add
# ---------------------------------------------------------------------------


def test_netgreeks_add_is_vector_add() -> None:
    a = NetGreeks(delta=70.0, gamma=1.0, theta=2.0, vega=3.0, rho=4.0)
    b = NetGreeks(delta=-30.0, gamma=-0.5, theta=1.0, vega=-1.0, rho=0.0)
    s = a + b
    assert s == NetGreeks(delta=40.0, gamma=0.5, theta=3.0, vega=2.0, rho=4.0)


def test_netgreeks_zero() -> None:
    assert NetGreeks.zero() == NetGreeks(0.0, 0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# OptionLeg derived properties via parse_occ
# ---------------------------------------------------------------------------


def test_option_leg_derived_properties() -> None:
    leg = _opt("NVDA260612C00150000", "buy", delta=0.50)
    assert leg.underlying == "NVDA"
    assert leg.right == "C"
    assert leg.strike == Decimal("150")
    assert leg.expiry.isoformat() == "2026-06-12"


# ---------------------------------------------------------------------------
# replay_chain: look-ahead drop + ChainQualityError
# ---------------------------------------------------------------------------


def _write_parquet_chain(path, rows) -> None:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df), path)


def _row(symbol: str, *, asof, fetched_at, delta=0.30) -> dict:
    return {
        "contract_symbol": symbol,
        "asof": asof,
        "fetched_at": fetched_at,
        "underlying_spot": 150.0,
        "risk_free_rate": 0.05,
        "bid": 2.40,
        "ask": 2.60,
        "last": 2.50,
        "volume": 100,
        "open_interest": 500,
        "delta": delta,
        "gamma": 0.01,
        "theta": -0.05,
        "vega": 0.10,
        "rho": 0.02,
        "iv": 0.45,
        "iv_source": "provider",
    }


def test_replay_drops_lookahead_rows(tmp_path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    path = reader._path_for("NVDA", asof.date())
    rows = [
        _row("NVDA260612C00140000", asof=asof, fetched_at=asof),
        _row("NVDA260612C00150000", asof=asof, fetched_at=asof),
        # look-ahead row: fetched 1h AFTER asof -> must be dropped
        _row(
            "NVDA260612C00160000",
            asof=asof,
            fetched_at=datetime(2026, 6, 1, 17, 0, tzinfo=UTC),
        ),
    ]
    _write_parquet_chain(path, rows)

    chain = reader.replay_chain("NVDA", asof)
    assert reader.last_lookahead_drops == 1
    symbols = {s.symbol for s in chain.snapshots}
    assert "NVDA260612C00160000" not in symbols
    assert len(chain.snapshots) == 2


def test_replay_raises_chain_quality_error_when_too_few(tmp_path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    path = reader._path_for("NVDA", asof.date())
    rows = [
        _row("NVDA260612C00140000", asof=asof, fetched_at=asof),
        # only 1 valid, the other is look-ahead -> <2 remain
        _row(
            "NVDA260612C00150000",
            asof=asof,
            fetched_at=datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
        ),
    ]
    _write_parquet_chain(path, rows)
    with pytest.raises(ChainQualityError):
        reader.replay_chain("NVDA", asof)


def test_replay_missing_file_raises_chain_quality_error(tmp_path) -> None:
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    with pytest.raises(ChainQualityError):
        reader.replay_chain("NVDA", datetime(2026, 6, 1, 16, 0, tzinfo=UTC))


def test_replay_chain_find_and_dte(tmp_path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    path = reader._path_for("NVDA", asof.date())
    _write_parquet_chain(
        path,
        [
            _row("NVDA260612C00140000", asof=asof, fetched_at=asof),
            _row("NVDA260612C00150000", asof=asof, fetched_at=asof),
        ],
    )
    chain = reader.replay_chain("NVDA", asof)
    found = chain.find(expiry=date(2026, 6, 12), strike=Decimal("150"), right="C")
    assert found is not None
    assert found.dte == (date(2026, 6, 12) - asof.date()).days
    assert found.mid == pytest.approx(2.50)


# ---------------------------------------------------------------------------
# fetch_chain_live: inert by default
# ---------------------------------------------------------------------------


def test_fetch_chain_live_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_LIVE_CHAIN", raising=False)
    reader = ChainSnapshotReader()
    with pytest.raises(LiveChainDisabled):
        reader.fetch_chain_live("NVDA")


def test_fetch_chain_live_requires_credentials(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_LIVE_CHAIN", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    reader = ChainSnapshotReader()
    with pytest.raises(LiveChainDisabled):
        reader.fetch_chain_live("NVDA")


# ---------------------------------------------------------------------------
# greek-completion fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mid,dte,spot",
    [(0.0, 30, 150.0), (-1.0, 30, 150.0), (2.5, 0, 150.0), (2.5, -3, 150.0), (2.5, 30, 0.0), (2.5, 30, -5.0)],
)
def test_greek_completion_fail_closed(mid, dte, spot) -> None:
    with pytest.raises(GreekComputationError):
        _complete_greeks_or_fail(mid=mid, dte_days=dte, spot=spot)


def test_greek_completion_passes_on_valid_inputs() -> None:
    # No exception on a valid set.
    _complete_greeks_or_fail(mid=2.5, dte_days=30, spot=150.0)
