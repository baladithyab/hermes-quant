"""Wave D tests — BarSnapshot Pydantic schema (ADR-0038 §D.2 / P5).

Coverage:
  * Schema round-trip (model_dump → reload → equality).
  * Slot independence (one stage populated, others remain None).
  * Frozen guarantee (mutation raises).
  * Missing-meta rejection (meta is REQUIRED).
  * `extra="forbid"` enforcement.
  * `from_market_context` constructor across pipeline stages.
  * JSONL parity with legacy `tick_loop._build_signal_record` shape under
    `HERMES_QUANT_SNAPSHOT_V2=0` (default) — bit-identical.
  * Opt-in V2 shape under `HERMES_QUANT_SNAPSHOT_V2=1`.
"""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from hermes_quant.daemon.tick_loop import AssetTask, _build_signal_record
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    AnalystView,
    MarketContext,
)
from hermes_quant.schemas import (
    AggregatedSignalSlot,
    AnalystViewSlot,
    BarSnapshot,
    FinalDecisionSlot,
    IndicatorsSlot,
    MetaSlot,
    OHLCVSlot,
    RiskCheckSlot,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bars(n: int = 60, start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-05-26", periods=n, freq="1h", tz=None)
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": [start_price + i * 0.1 for i in range(n)],
            "high": [start_price + i * 0.1 + 0.5 for i in range(n)],
            "low": [start_price + i * 0.1 - 0.5 for i in range(n)],
            "close": [start_price + i * 0.1 + 0.2 for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        }
    )


@pytest.fixture()
def ctx() -> MarketContext:
    bars = _make_bars()
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        last_volume=float(bars["volume"].iloc[-1]),
        asof=pd.Timestamp("2026-05-26T15:00:00.000000Z").tz_localize(None),
    )


@pytest.fixture()
def view() -> AnalystView:
    return AnalystView(
        analyst="ta_classic",
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.8,
        horizon="1h",
        rationale="bullish breakout",
        metadata={"rsi": 65.2, "packet_hash": "abc123"},
    )


@pytest.fixture()
def signal(view: AnalystView) -> AggregatedSignal:
    return AggregatedSignal(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=pd.Timestamp("2026-05-26T15:00:00.000000Z").tz_localize(None),
        direction=1,
        magnitude=0.011,
        confidence=0.55,
        confidence_raw=0.75,
        horizon="1h",
        components=(view,),
        aggregator="bma",
        metadata={"weights": {"ta_classic": 1.0}},
    )


@pytest.fixture()
def action() -> Action:
    return Action(
        target_position_pct=0.10,
        reason="kelly_sized",
        signal_id=None,
        halt=False,
    )


@pytest.fixture()
def task() -> AssetTask:
    return AssetTask(
        asset="BTC/USDT",
        asset_class="crypto",
        timeframe="1h",
        exchange="binance",
        horizon="4h",
    )


# ---------------------------------------------------------------------------
# 1. Schema round-trip
# ---------------------------------------------------------------------------


def test_bar_snapshot_round_trip_minimal() -> None:
    """Smallest valid BarSnapshot: only meta + identity."""
    asof = pd.Timestamp("2026-05-26T15:00:00.000000Z").tz_localize(None).to_pydatetime()
    snap = BarSnapshot(
        symbol="BTC/USDT",
        bar_ts=asof,
        asof_decision=asof,
        meta=MetaSlot(
            signal_id="sig-test-001",
            exchange="binance",
            timeframe="1h",
            asset_class="crypto",
        ),
    )
    dumped = snap.model_dump(mode="json")
    reloaded = BarSnapshot.model_validate(dumped)
    assert reloaded == snap


def test_bar_snapshot_round_trip_full(ctx, view, signal, action) -> None:
    """Full snapshot with every slot populated round-trips losslessly."""
    snap = BarSnapshot.from_market_context(
        ctx, [view], signal, action, signal_id="sig-test-full",
    )
    dumped = snap.model_dump(mode="json")
    reloaded = BarSnapshot.model_validate(dumped)
    assert reloaded == snap


# ---------------------------------------------------------------------------
# 2. Slot independence
# ---------------------------------------------------------------------------


def test_slots_default_to_none(ctx) -> None:
    """from_market_context with no views / no signal / no action leaves slots None."""
    snap = BarSnapshot.from_market_context(
        ctx, None, None, None, signal_id="sig-id-only"
    )
    assert snap.analyst_views is None
    assert snap.aggregated_signal is None
    assert snap.risk_check is None
    assert snap.final_decision is None
    # ohlcv is always populated from ctx
    assert snap.ohlcv is not None
    assert snap.ohlcv.last_close == ctx.last_close


def test_partial_pipeline_has_views_no_signal(ctx, view) -> None:
    """Analysts ran but aggregator/gate did not — slot independence holds."""
    snap = BarSnapshot.from_market_context(
        ctx, [view], None, None, signal_id="sig-views-only"
    )
    assert snap.analyst_views is not None
    assert len(snap.analyst_views) == 1
    assert snap.aggregated_signal is None
    assert snap.risk_check is None
    assert snap.final_decision is None


# ---------------------------------------------------------------------------
# 3. Frozen + extra=forbid guarantees
# ---------------------------------------------------------------------------


def test_frozen_guarantee_top_level(ctx) -> None:
    """Top-level field mutation raises (frozen=True)."""
    snap = BarSnapshot.from_market_context(ctx, None, None, None, signal_id="s")
    with pytest.raises(ValidationError):
        snap.symbol = "ETH/USDT"  # type: ignore[misc]


def test_frozen_guarantee_slot(ctx) -> None:
    """Nested slot mutation raises."""
    snap = BarSnapshot.from_market_context(ctx, None, None, None, signal_id="s")
    assert snap.ohlcv is not None
    with pytest.raises(ValidationError):
        snap.ohlcv.last_close = 999.0  # type: ignore[misc]


def test_extra_forbid_on_meta() -> None:
    """Unknown fields on MetaSlot are rejected."""
    with pytest.raises(ValidationError):
        MetaSlot(
            signal_id="s",
            timeframe="1h",
            asset_class="crypto",
            unknown_key="x",  # type: ignore[call-arg]
        )


def test_extra_forbid_on_bar_snapshot() -> None:
    """Unknown fields on BarSnapshot itself are rejected."""
    asof = pd.Timestamp("2026-05-26T15:00:00.000000Z").tz_localize(None).to_pydatetime()
    with pytest.raises(ValidationError):
        BarSnapshot(
            symbol="X",
            bar_ts=asof,
            asof_decision=asof,
            meta=MetaSlot(signal_id="s", timeframe="1h", asset_class="crypto"),
            mystery_slot="boom",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# 4. Missing-meta rejection
# ---------------------------------------------------------------------------


def test_meta_is_required() -> None:
    """BarSnapshot without meta raises ValidationError."""
    asof = pd.Timestamp("2026-05-26T15:00:00.000000Z").tz_localize(None).to_pydatetime()
    with pytest.raises(ValidationError):
        BarSnapshot(  # type: ignore[call-arg]
            symbol="BTC/USDT",
            bar_ts=asof,
            asof_decision=asof,
        )


# ---------------------------------------------------------------------------
# 5. JSONL parity with legacy tick_loop._build_signal_record
# ---------------------------------------------------------------------------


def _legacy_id(asof: pd.Timestamp, asset: str) -> str:
    """Replicate tick_loop's id format (deterministic for parity tests)."""
    return f"sig-{asof.strftime('%Y%m%dT%H%M%SZ')}-{asset.replace('/', '-')}-aaaaaa"


@pytest.mark.parametrize("env_value", ["0", "", None])
def test_jsonl_parity_default(monkeypatch, ctx, view, signal, action, task, env_value) -> None:
    """Under default env (V2 unset/0/empty), to_jsonl_row matches legacy bit-identical."""
    if env_value is None:
        monkeypatch.delenv("HERMES_QUANT_SNAPSHOT_V2", raising=False)
    else:
        monkeypatch.setenv("HERMES_QUANT_SNAPSHOT_V2", env_value)

    sig_id = _legacy_id(ctx.asof, ctx.asset)
    snap = BarSnapshot.from_market_context(
        ctx, [view], signal, action, signal_id=sig_id
    )

    # Build the legacy record the way tick_loop does it (bar_ts arg added
    # post-91d4796 to make the id deterministic; the test overrides the
    # id below so the actual value doesn't affect parity).
    legacy = _build_signal_record(signal, action, task, ctx.asof, ctx, ctx.asof)
    # Replace legacy's randomly-suffixed id with our deterministic one for comparison
    legacy["id"] = sig_id

    new = snap.to_jsonl_row()

    assert set(new.keys()) == set(legacy.keys()), (
        f"JSONL key sets differ.\nNEW only: {set(new) - set(legacy)}\n"
        f"LEGACY only: {set(legacy) - set(new)}"
    )
    for k in legacy:
        assert new[k] == legacy[k], f"Key {k!r} differs: new={new[k]!r} legacy={legacy[k]!r}"


def test_jsonl_parity_with_halt(monkeypatch, ctx, view, signal, task) -> None:
    """Halt-action path also bit-identical."""
    monkeypatch.delenv("HERMES_QUANT_SNAPSHOT_V2", raising=False)

    halt_until = pd.Timestamp("2026-05-26T22:00:00.000000Z").tz_localize(None)
    halt_action = Action(
        target_position_pct=0.0,
        reason="daily_loss_breaker",
        halt=True,
        halt_scope=("default", "crypto", "BTC/USDT"),
        halt_until=halt_until,
    )

    sig_id = _legacy_id(ctx.asof, ctx.asset)
    snap = BarSnapshot.from_market_context(
        ctx, [view], signal, halt_action, signal_id=sig_id
    )

    legacy = _build_signal_record(signal, halt_action, task, ctx.asof, ctx, ctx.asof)
    legacy["id"] = sig_id
    new = snap.to_jsonl_row()

    assert new == legacy


def test_jsonl_parity_packet_hash_extraction(monkeypatch, ctx, signal, action, task) -> None:
    """semantic_packet_hashes derives from view.metadata['packet_hash'] same as legacy."""
    monkeypatch.delenv("HERMES_QUANT_SNAPSHOT_V2", raising=False)

    sig_id = _legacy_id(ctx.asof, ctx.asset)
    snap = BarSnapshot.from_market_context(
        ctx, list(signal.components), signal, action, signal_id=sig_id
    )

    legacy = _build_signal_record(signal, action, task, ctx.asof, ctx, ctx.asof)
    legacy["id"] = sig_id
    new = snap.to_jsonl_row()
    assert new["semantic_packet_hashes"] == legacy["semantic_packet_hashes"]
    assert "abc123" in new["semantic_packet_hashes"]


def test_jsonl_parity_committee_turns(monkeypatch, ctx, view, action, task) -> None:
    """committee_turns_hashes path mirrors legacy walk through metadata.committee."""
    monkeypatch.delenv("HERMES_QUANT_SNAPSHOT_V2", raising=False)

    sig_with_committee = AggregatedSignal(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        asof=ctx.asof,
        direction=1,
        magnitude=0.011,
        confidence=0.55,
        confidence_raw=0.75,
        horizon="1h",
        components=(view,),
        aggregator="bma",
        metadata={
            "committee": {
                "model_backed_turns": [
                    {"input_hash": "h1", "role": "bull"},
                    {"input_hash": "h2", "role": "bear"},
                    {"role": "judge"},  # no input_hash → skipped
                ]
            }
        },
    )

    sig_id = _legacy_id(ctx.asof, ctx.asset)
    snap = BarSnapshot.from_market_context(
        ctx, [view], sig_with_committee, action, signal_id=sig_id
    )

    legacy = _build_signal_record(sig_with_committee, action, task, ctx.asof, ctx, ctx.asof)
    legacy["id"] = sig_id
    new = snap.to_jsonl_row()
    assert new["committee_turns_hashes"] == ["h1", "h2"]
    assert new["committee_turns_hashes"] == legacy["committee_turns_hashes"]


# ---------------------------------------------------------------------------
# 6. Opt-in V2 shape
# ---------------------------------------------------------------------------


def test_jsonl_v2_opt_in(monkeypatch, ctx, view, signal, action) -> None:
    """Under HERMES_QUANT_SNAPSHOT_V2=1, emit the typed BarSnapshot model_dump shape."""
    monkeypatch.setenv("HERMES_QUANT_SNAPSHOT_V2", "1")

    snap = BarSnapshot.from_market_context(
        ctx, [view], signal, action, signal_id="sig-v2"
    )
    new = snap.to_jsonl_row()

    # New shape: typed slots, not flattened. Top-level keys reflect BarSnapshot fields.
    assert "symbol" in new
    assert "meta" in new and isinstance(new["meta"], dict)
    assert "aggregated_signal" in new
    assert "final_decision" in new
    assert new["meta"]["signal_id"] == "sig-v2"
    # And critically, the legacy flat keys are NOT present at top level
    assert "schema_version" not in new
    assert "components" not in new
    assert "decision_price" not in new


def test_jsonl_legacy_requires_aggregated_signal(monkeypatch, ctx) -> None:
    """Legacy emit raises if aggregated_signal is missing."""
    monkeypatch.delenv("HERMES_QUANT_SNAPSHOT_V2", raising=False)

    snap = BarSnapshot.from_market_context(ctx, None, None, None, signal_id="s")
    with pytest.raises(ValueError, match="aggregated_signal"):
        snap.to_jsonl_row()


# ---------------------------------------------------------------------------
# 7. Slot helpers — small typed views work standalone
# ---------------------------------------------------------------------------


def test_slot_helpers_construct_independently() -> None:
    """Each slot model is independently constructable (helps replay)."""
    ohlcv = OHLCVSlot(last_close=100.0, last_volume=1000.0, n_bars=60)
    ind = IndicatorsSlot(snapshot={"rsi": 65.0})
    av = AnalystViewSlot(
        analyst="x", direction=1, magnitude=0.01, confidence=0.6,
        confidence_raw=0.7, horizon="1h",
    )
    agg = AggregatedSignalSlot(
        aggregator="bma", direction=1, magnitude=0.01, confidence=0.5,
        confidence_raw=0.6, horizon="1h", components=(av,),
    )
    risk = RiskCheckSlot(target_position_pct=0.05, reason="ok")
    final = FinalDecisionSlot(target_position_pct=0.05, reason="ok")

    assert ohlcv.last_close == 100.0
    assert ind.snapshot == {"rsi": 65.0}
    assert agg.components[0].analyst == "x"
    assert risk.target_position_pct == 0.05
    assert final.target_position_pct == 0.05


def test_n_bars_must_be_non_negative() -> None:
    """OHLCVSlot.n_bars must be >= 0."""
    with pytest.raises(ValidationError):
        OHLCVSlot(last_close=100.0, last_volume=1.0, n_bars=-1)
