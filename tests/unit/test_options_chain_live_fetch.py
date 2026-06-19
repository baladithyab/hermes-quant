"""Unit tests for OptionChainReader.fetch_chain_live body (AG-PERC-3).

Deterministic, no network, no credentials hitting the wire: the Alpaca client
is INJECTED as a mock returning a fake chain payload, so the parse ->
OptionChain -> greek-completion -> atomic parquet-write path is proven end to
end without touching the real sandbox.

The live-sandbox probe (>=2 contracts for a real symbol) is operator/credential
-gated and lives in tests/integration/test_options_chain_live.py (SKIPPED unless
APCA_API_KEY_ID + flag + alpaca extra are present).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from hermes_quant.options.data import (
    ChainSnapshotReader,
    GreekComputationError,
    LiveChainDisabled,
    OptionChain,
)

# ---------------------------------------------------------------------------
# Fake Alpaca SDK shapes (match alpaca.data.models.snapshots.OptionsSnapshot)
# ---------------------------------------------------------------------------


@dataclass
class _FakeGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


@dataclass
class _FakeQuote:
    bid_price: float
    ask_price: float
    timestamp: datetime


@dataclass
class _FakeTrade:
    price: float


@dataclass
class _FakeSnapshot:
    symbol: str
    latest_trade: _FakeTrade | None
    latest_quote: _FakeQuote | None
    implied_volatility: float | None
    greeks: _FakeGreeks | None


class _FakeOptionClient:
    """Stand-in for OptionHistoricalDataClient: records the request, returns a
    dict keyed by OCC symbol (the real get_option_chain return shape)."""

    def __init__(self, chain: dict[str, _FakeSnapshot]) -> None:
        self._chain = chain
        self.last_request = None

    def get_option_chain(self, request_params):  # noqa: ANN001 - test double
        self.last_request = request_params
        return dict(self._chain)


class _FakeStockClient:
    """Stand-in for the equity spot source (StockHistoricalDataClient)."""

    def __init__(self, spot: float) -> None:
        self._spot = spot
        self.last_request = None

    def get_stock_latest_trade(self, request_params):  # noqa: ANN001 - test double
        self.last_request = request_params
        # Mirror the real Dict[symbol, Trade] return shape.
        sym = request_params.symbol_or_symbols
        if isinstance(sym, list):
            sym = sym[0]
        return {sym: _FakeTrade(price=self._spot)}


_ASOF = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)


def _snap(
    symbol: str,
    *,
    bid: float = 2.40,
    ask: float = 2.60,
    iv: float | None = 0.45,
    greeks: _FakeGreeks | None = None,
) -> _FakeSnapshot:
    return _FakeSnapshot(
        symbol=symbol,
        latest_trade=_FakeTrade(price=(bid + ask) / 2.0),
        latest_quote=_FakeQuote(bid_price=bid, ask_price=ask, timestamp=_ASOF),
        implied_volatility=iv,
        greeks=greeks,
    )


def _provider_greeks() -> _FakeGreeks:
    return _FakeGreeks(delta=0.30, gamma=0.01, theta=-0.05, vega=0.10, rho=0.02)


def _enable_live(monkeypatch, tmp_path) -> ChainSnapshotReader:
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_LIVE_CHAIN", "1")
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    return ChainSnapshotReader(chains_dir=tmp_path)


# ---------------------------------------------------------------------------
# Inert-by-default rails (UNCHANGED — byte-identical guard)
# ---------------------------------------------------------------------------


def test_live_disabled_by_default_even_with_client(monkeypatch, tmp_path) -> None:
    """An injected client does NOT bypass the default-OFF flag gate."""
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_LIVE_CHAIN", raising=False)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    client = _FakeOptionClient({"NVDA260612C00150000": _snap("NVDA260612C00150000")})
    with pytest.raises(LiveChainDisabled):
        reader.fetch_chain_live("NVDA", client=client)


def test_live_requires_credentials_even_with_client(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_LIVE_CHAIN", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    client = _FakeOptionClient({"NVDA260612C00150000": _snap("NVDA260612C00150000")})
    with pytest.raises(LiveChainDisabled):
        reader.fetch_chain_live("NVDA", client=client)


# ---------------------------------------------------------------------------
# Happy path: mock client -> OptionChain -> parquet write
# ---------------------------------------------------------------------------


def test_live_builds_chain_from_provider_greeks(monkeypatch, tmp_path) -> None:
    reader = _enable_live(monkeypatch, tmp_path)
    chain_payload = {
        "NVDA260612C00140000": _snap("NVDA260612C00140000", greeks=_provider_greeks()),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", greeks=_provider_greeks()),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=150.0)

    chain = reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=_ASOF)

    assert isinstance(chain, OptionChain)
    assert chain.underlying == "NVDA"
    assert chain.underlying_spot == pytest.approx(150.0)
    assert len(chain.snapshots) == 2
    for s in chain.snapshots:
        # mid from the NBBO quote
        assert s.mid == pytest.approx(2.50)
        # provider greeks carried through, never zero-greeks
        assert s.greeks.delta == pytest.approx(0.30)
        assert s.greeks.iv_source == "provider"
        assert s.fetched_at is not None
        assert s.underlying_spot == pytest.approx(150.0)


def test_live_writes_atomic_parquet_that_replays(monkeypatch, tmp_path) -> None:
    """The live writer must emit the exact parquet schema replay_chain reads, so
    a write -> replay round-trips.

    The live convention is asof == fetch wall-clock (a snapshot fetched at T can
    only be replayed for decisions at asof >= T — the no-lookahead invariant). We
    fetch with asof=now so fetched_at (now) <= asof holds and the rows survive the
    replay-time look-ahead filter. Replaying at a slightly later asof is the real
    usage; the OCC expiry (2026-06-12) is in the past relative to a real today but
    DTE/quality is replay_chain's concern, not the schema round-trip we assert."""
    reader = _enable_live(monkeypatch, tmp_path)
    asof = datetime.now(UTC)
    chain_payload = {
        "NVDA260612C00140000": _snap("NVDA260612C00140000", greeks=_provider_greeks()),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", greeks=_provider_greeks()),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=150.0)

    reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=asof)

    path = reader._path_for("NVDA", asof.date())
    assert path.exists(), "live fetch must persist a parquet snapshot"
    # No leftover temp file in the directory (atomic replace cleaned up).
    leftovers = [p.name for p in path.parent.iterdir() if p.suffix != ".parquet"]
    assert leftovers == [], f"unexpected non-parquet leftovers: {leftovers}"

    # Round-trip: replay at an asof at/after the fetch wall-clock (so fetched_at
    # <= asof and the rows survive the look-ahead filter).
    replay_asof = datetime.now(UTC) + timedelta(seconds=1)
    # Same calendar day -> same parquet path.
    if replay_asof.date() != asof.date():  # pragma: no cover - midnight boundary
        replay_asof = asof
    replayed = reader.replay_chain("NVDA", replay_asof)
    assert {s.symbol for s in replayed.snapshots} == set(chain_payload)
    for s in replayed.snapshots:
        assert s.greeks.delta == pytest.approx(0.30)
        assert s.mid == pytest.approx(2.50)


def test_live_completes_missing_greeks_via_optlib(monkeypatch, tmp_path) -> None:
    """A no-greeks-tier contract (greeks=None) gets greeks synthesized via the
    fail-closed completion path — never left as None / zero-greeks."""
    reader = _enable_live(monkeypatch, tmp_path)
    chain_payload = {
        # one provider-greeks contract, one no-greeks-tier contract
        "NVDA260612C00140000": _snap("NVDA260612C00140000", greeks=_provider_greeks()),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", iv=0.45, greeks=None),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=150.0)

    chain = reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=_ASOF)

    synth = next(
        (s for s in chain.snapshots if s.symbol == "NVDA260612C00150000"), None
    )
    assert synth is not None
    # Greeks completed (non-None) and tagged as a synthesized European approx.
    assert synth.greeks.delta is not None
    assert synth.greeks.gamma is not None
    assert synth.greeks.theta is not None
    assert synth.greeks.vega is not None
    assert synth.greeks.iv_source == "py_vollib_european_approximation"
    # A call ~7% OTM with 11 DTE has a positive but sub-0.5 delta.
    assert 0.0 < synth.greeks.delta < 1.0


# ---------------------------------------------------------------------------
# Fail-closed: never zero-greeks
# ---------------------------------------------------------------------------


def test_live_fail_closed_on_nonpositive_mid(monkeypatch, tmp_path) -> None:
    """A no-greeks contract with mid<=0 cannot have honest greeks synthesized; it
    is dropped (fail-closed) — never admitted with zero-greeks. With only one
    other valid contract remaining (>=2 needed downstream is replay's concern),
    the live path still returns the surviving valid contract(s)."""
    reader = _enable_live(monkeypatch, tmp_path)
    chain_payload = {
        "NVDA260612C00140000": _snap("NVDA260612C00140000", greeks=_provider_greeks()),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", greeks=_provider_greeks()),
        # crossed/zero quote -> mid<=0 -> cannot complete greeks -> dropped
        "NVDA260612C00160000": _snap(
            "NVDA260612C00160000", bid=0.0, ask=0.0, greeks=None
        ),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=150.0)

    chain = reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=_ASOF)

    symbols = {s.symbol for s in chain.snapshots}
    assert "NVDA260612C00160000" not in symbols, "mid<=0 no-greeks contract must be dropped"
    # Survivors carry honest greeks.
    for s in chain.snapshots:
        assert s.greeks.delta is not None


def test_live_fail_closed_on_nonpositive_spot(monkeypatch, tmp_path) -> None:
    """spot<=0 defeats greek completion for the whole chain — fail-closed: the
    completion validator raises GreekComputationError rather than fabricating
    zero-greeks for the no-greeks tier."""
    reader = _enable_live(monkeypatch, tmp_path)
    chain_payload = {
        # no-greeks tier so spot is actually consulted for completion
        "NVDA260612C00140000": _snap("NVDA260612C00140000", iv=0.45, greeks=None),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", iv=0.45, greeks=None),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=0.0)  # bad spot

    with pytest.raises(GreekComputationError):
        reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=_ASOF)


def test_live_fetched_at_is_not_a_lookahead_into_decision(monkeypatch, tmp_path) -> None:
    """fetched_at is a provider-return wall-clock stamp (ADR-0028 D7), distinct
    from asof (the decision time). It must be >= asof here (snapshot fetched now,
    decided as-of the passed asof) and is never fed as a scored feature."""
    reader = _enable_live(monkeypatch, tmp_path)
    chain_payload = {
        "NVDA260612C00140000": _snap("NVDA260612C00140000", greeks=_provider_greeks()),
        "NVDA260612C00150000": _snap("NVDA260612C00150000", greeks=_provider_greeks()),
    }
    client = _FakeOptionClient(chain_payload)
    stock = _FakeStockClient(spot=150.0)

    chain = reader.fetch_chain_live("NVDA", client=client, stock_client=stock, asof=_ASOF)
    for s in chain.snapshots:
        assert s.asof == _ASOF
        assert s.fetched_at >= _ASOF
