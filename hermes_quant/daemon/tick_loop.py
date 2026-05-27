"""hermes_quant.daemon.tick_loop — Per-tick fetch → analyze → aggregate → gate → emit.

The tick loop is the daemon's hot path. Each invocation:
  1. Auto-clear expired halts.
  2. For each (asset, timeframe) the daemon is responsible for:
     a. Fetch latest bars via the data provider chain.
     b. Construct MarketContext.
     c. Run all enabled analysts → AnalystView list.
     d. Run aggregator → AggregatedSignal.
     e. Construct MarketState (volatility, costs).
     f. Run risk gate → Action | None.
     g. If Action: emit signal record to bus.
  3. Update last_tick_at for the heartbeat emitter.

Errors anywhere in steps 2a-2g are caught and logged WITHOUT halting other
asset processing — defense in depth. The daemon's silence-by-default
posture means a fetch failure for one asset doesn't taint signals for
other assets.

This is a SYNCHRONOUS loop. v0.2 may add asyncio.gather over assets, but
v0.1.1's bottleneck is yfinance rate limiting, not loop concurrency.
"""

from __future__ import annotations

import hashlib
import logging
import os
<<<<<<< Updated upstream
=======
import uuid
>>>>>>> Stashed changes
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.signal_bus import (
    SIGNAL_BUS_PATH,
    emit_signal_record,
)
from hermes_quant.daemon.watermark import Watermark, WatermarkStore
from hermes_quant.data.base import fetch_with_chain
from hermes_quant.protocol import (
    Action,
    Aggregator,
    Analyst,
    DataProviderError,
    DataQualityError,
    HaltState,
    MarketContext,
    MarketState,
    Portfolio,
    RiskGate,
)

logger = logging.getLogger(__name__)


# ADR-0038 §D.1 — watermark integration is opt-in. When unset/`0`, this
# module's behaviour is bit-identical to legacy: no watermark reads, no
# writes, and `WatermarkStore` is never constructed.
_WATERMARK_ENV = "HERMES_QUANT_WATERMARK_ENABLED"


def _watermark_enabled() -> bool:
    return os.environ.get(_WATERMARK_ENV, "0").strip() == "1"


def _compute_indicator_snapshot_hash(ctx: MarketContext) -> str:
    """Compute the 16-hex-char snapshot hash per ADR-0038 §D.1.

    Hash projection: a deterministic tuple of MarketContext identity +
    bar-summary fields — `(asset, timeframe, asset_class, exchange,
    last_close, last_volume, asof_iso)`. We deliberately do NOT hash the
    full bars DataFrame: bars include a rolling backfill window that
    shifts each tick, so its hash would never match across runs even for
    the same `bar_ts`. The projected fields uniquely identify the bar
    that was processed; mismatch on replay is a strong signal of an
    upstream data revision.
    """
    asof = ctx.asof
    if asof.tzinfo is not None:
        asof = asof.tz_convert("UTC").tz_localize(None)
    payload = "|".join(
        [
            ctx.asset,
            ctx.timeframe,
            ctx.asset_class,
            ctx.exchange or "",
            f"{float(ctx.last_close):.10g}",
            f"{float(ctx.last_volume):.10g}",
            asof.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        ]
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass
class AssetTask:
    """One unit of work for the tick loop."""

    asset: str
    asset_class: str
    timeframe: str
    exchange: str | None = None
    horizon: str = "4h"  # for the signal record
    lookback_bars: int = 500


@dataclass
class TickLoopState:
    """Mutable state passed to each tick."""

    last_tick_at: pd.Timestamp | None = None
    last_signals_emitted: int = 0
    n_ticks: int = 0
    n_errors: int = 0
    n_skipped_watermark: int = 0  # ADR-0038 §D.1: replays skipped via watermark.
    active_assets: list[str] = field(default_factory=list)


def compute_market_state(
    bars: pd.DataFrame,
    *,
    asset: str,
    asof: pd.Timestamp,
    commission: float = 0.001,
    spread: float = 0.0008,
    slippage_estimate: float = 0.0012,
    volatility_window: int = 30,
    tz: str = "UTC",
) -> MarketState:
    """Compute MarketState from recent bars.

    Volatility is the rolling stdev of log returns over `volatility_window`
    bars. Per ADR-0009 §P0-1: volatility is STDEV (not variance); the gate
    squares it for σ².

    Slippage is the caller-provided estimate; the daemon's settlement loop
    updates this from real fills via RollingSlippageEstimator (Wave 1.5).
    """
    if len(bars) < volatility_window:
        # Bootstrap default — 1% per period stdev
        vol = 0.01
    else:
        log_returns = np.log(bars["close"]).diff().dropna()
        vol = float(log_returns.tail(volatility_window).std())
        if np.isnan(vol) or vol <= 0:
            vol = 0.01

    return MarketState(
        asset=asset,
        asof=asof,
        volatility=vol,
        commission=commission,
        spread=spread,
        slippage_estimate=slippage_estimate,
        tz=tz,
    )


def run_one_tick(
    *,
    tasks: list[AssetTask],
    data_providers: list,
    analysts: list[Analyst],
    aggregator: Aggregator,
    risk_gate: RiskGate,
    halt_state: HaltState,
    portfolio_for: Callable[[str, str], Portfolio],
    state: TickLoopState,
    bus_path=SIGNAL_BUS_PATH,
    asof: pd.Timestamp | None = None,
    watermark_store: WatermarkStore | None = None,
) -> int:
    """Run one tick. Returns number of non-silent signals emitted.

    Args:
        tasks: list of AssetTask to process.
        data_providers: ordered DataProvider chain.
        analysts: enabled analysts.
        aggregator: the (only) aggregator.
        risk_gate: the (only) risk gate.
        halt_state: HaltState (auto_clear_expired called at start).
        portfolio_for: callable (account_id, asset_class) → Portfolio.
        state: mutable TickLoopState updated in place.
        bus_path: signal bus.
        asof: tick timestamp; default now.
        watermark_store: optional injected WatermarkStore. When None and
            `HERMES_QUANT_WATERMARK_ENABLED=1`, a default-path store is
            constructed. When None and the flag is unset, the watermark
            path is not exercised at all (legacy bit-identical behaviour
            per ADR-0038 §D.1).

    Returns:
        Number of non-silent Actions emitted to the bus.
    """
    asof = asof if asof is not None else pd.Timestamp.utcnow()
    state.n_ticks += 1
    state.last_tick_at = asof
    state.active_assets = [t.asset for t in tasks]

    # ADR-0038 §D.1: lazily resolve watermark store when the env flag is
    # set and no caller-injected store was provided. Resolve once per tick
    # so all assets share one connection-pool / cache.
    if watermark_store is None and _watermark_enabled():
        try:
            watermark_store = WatermarkStore()
        except Exception as e:  # noqa: BLE001
            logger.warning("watermark store init failed; disabling for this tick: %s", e)
            watermark_store = None

    # Auto-clear expired halts
    try:
        if isinstance(halt_state, HaltStateSQLite):
            halt_state.auto_clear_expired()
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_clear_expired failed: %s", e)

    n_emitted = 0
    for task in tasks:
        try:
            # Fetch bars
            end = asof
            tf_secs = {
                "1m": 60,
                "5m": 300,
                "15m": 900,
                "30m": 1800,
                "1h": 3600,
                "4h": 14400,
                "1d": 86400,
            }.get(task.timeframe, 3600)
            start = end - pd.Timedelta(seconds=tf_secs * task.lookback_bars * 2)
            bars = fetch_with_chain(
                data_providers,
                task.asset,
                task.timeframe,
                start,
                end,
            )

            # Build context
            last_close = float(bars["close"].iloc[-1])
            last_vol = float(bars["volume"].iloc[-1])
            ctx = MarketContext(
                asset=task.asset,
                timeframe=task.timeframe,
                asset_class=task.asset_class,
                exchange=task.exchange,
                bars=bars,
                last_close=last_close,
                last_volume=last_vol,
                asof=asof,
            )

            # ADR-0038 §D.1 — watermark short-circuit. We use the LAST bar's
            # timestamp as `bar_ts` (the upper bound of what's been
            # processed). If the watermark says we've already processed a
            # bar at-or-after this `bar_ts`, this is a replay (same tick
            # already journaled); skip it. The optional snapshot-hash
            # mismatch is logged at WARNING level but never raised — an
            # upstream data revision should not crash the daemon.
            bar_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
            if bar_ts.tzinfo is not None:
                bar_ts = bar_ts.tz_convert("UTC").tz_localize(None)
            if watermark_store is not None:
                try:
<<<<<<< Updated upstream
                    wm = watermark_store.get(
                        task.asset,
                        task.exchange or "",
                        task.timeframe,
                    )
                except ValueError as e:
                    logger.warning(
                        "corrupt watermark for %s/%s/%s; treating as missing: %s",
                        task.asset,
                        task.exchange or "",
                        task.timeframe,
=======
                    wm = watermark_store.get(task.asset)
                except ValueError as e:
                    logger.warning(
                        "corrupt watermark for %s; treating as missing: %s",
                        task.asset,
>>>>>>> Stashed changes
                        e,
                    )
                    wm = None
                if wm is not None and wm.last_processed_bar_ts >= bar_ts:
                    new_hash = _compute_indicator_snapshot_hash(ctx)
                    if new_hash != wm.indicator_snapshot_hash:
                        logger.warning(
                            "watermark hash mismatch for %s at bar_ts=%s "
                            "(stored=%s, recomputed=%s); skipping replay anyway",
                            task.asset,
                            bar_ts,
                            wm.indicator_snapshot_hash,
                            new_hash,
                        )
                    logger.info(
                        "skipping replay for %s: bar_ts=%s <= watermark=%s",
                        task.asset,
                        bar_ts,
                        wm.last_processed_bar_ts,
                    )
                    state.n_skipped_watermark += 1
                    continue

            # Run analysts
            views = []
            for a in analysts:
                try:
                    v = a.analyze(ctx)
                    if v is not None:
                        views.append(v)
                except Exception as e:  # noqa: BLE001
                    logger.warning("analyst %s failed on %s: %s", a.name, task.asset, e)
                    continue

            if not views:
                continue  # silent

            # Aggregate
            signal = aggregator.aggregate(views, ctx)
            if signal.direction == 0:
                continue  # silent

            # Compute market state
            market = compute_market_state(
                bars,
                asset=task.asset,
                asof=asof,
                tz="UTC" if task.asset_class == "crypto" else "America/New_York",
            )

            # Get portfolio
            portfolio = portfolio_for("default", task.asset_class)

            # Run risk gate
            action = risk_gate.gate(signal, market, portfolio, halt_state)
            if action is None:
                continue  # silent

            # Phase-8 P0-C (synthesis 2026-05-13): when the gate's circuit
            # breakers (drawdown / daily-loss) emit a halt action, the tick
            # loop MUST install the durable halt in the SQLite registry
            # BEFORE emitting the bus signal. Without this, the halt action
            # is announced to the bus but not committed to durable storage —
            # on next tick the same circuit-breaker reading would re-fire,
            # on daemon restart the halt history is lost, and other assets
            # in the same scope wouldn't observe the halt. This violates
            # synthesis-v2 §P0-D ordering ("durable halt FIRST, then any
            # other action").
            if action.halt and action.halt_scope is not None:
                try:
                    scope_account, scope_class, scope_asset = action.halt_scope
                    halt_state.add_halt(
                        account_id=scope_account if scope_account != "*" else None,
                        asset_class=scope_class if scope_class != "*" else None,
                        asset=scope_asset if scope_asset != "*" else None,
                        reason=action.reason,
                        halted_until=action.halt_until,
                    )
                except ValueError:
                    # Active halt already exists for this scope — fine; the
                    # gate's halt action is idempotent in that case.
                    pass
                except Exception as e:  # noqa: BLE001
                    # Don't let halt-install failure crash the tick;
                    # the bus emission below still tells consumers, and
                    # the gate will re-emit on the next tick.
                    logger.exception(
                        "halt installation failed for scope=%s: %s",
                        action.halt_scope,
                        e,
                    )

            # Emit signal record. Pass bar_ts so the signal_id is
            # deterministic across replays — without this, a crash
            # between emit and watermark.set() produces duplicate rows
            # with different UUIDs that downstream consumers can't
            # collapse on signal_id (codex P2 finding 2026-05-26).
            record = _build_signal_record(signal, action, task, asof, ctx, bar_ts)  # type: ignore[arg-type]
            emit_signal_record(record, path=bus_path)
            n_emitted += 1
            logger.info(
                "emitted signal: asset=%s dir=%d tgt=%.3f reason=%s",
                task.asset,
                signal.direction,
                action.target_position_pct,
                action.reason,
            )

            # ADR-0038 §D.1 — watermark write happens AFTER the emit
            # returns, so on crash mid-tick we may re-emit the same
            # `(symbol, bar_ts)` exactly once on next start; downstream
            # consumers idempotency-key on `signal_id`.
            if watermark_store is not None:
                try:
                    now = pd.Timestamp.utcnow()
                    if now.tzinfo is not None:
                        now = now.tz_convert("UTC").tz_localize(None)
                    watermark_store.set(
                        Watermark(
                            symbol=task.asset,
<<<<<<< Updated upstream
                            exchange=task.exchange or "",
                            timeframe=task.timeframe,
                            last_processed_bar_ts=bar_ts,  # type: ignore[arg-type]
=======
                            last_processed_bar_ts=bar_ts,
>>>>>>> Stashed changes
                            indicator_snapshot_hash=_compute_indicator_snapshot_hash(ctx),
                            updated_at=now,
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    # Watermark failure is non-fatal — the bar was already
                    # journaled; worst case we re-process it next tick.
                    logger.warning(
                        "watermark write failed for %s at bar_ts=%s: %s",
                        task.asset,
                        bar_ts,
                        e,
                    )

        except DataQualityError as e:
            logger.warning("data quality issue for %s: %s", task.asset, e)
            state.n_errors += 1
            continue
        except DataProviderError as e:
            logger.warning("data provider failed for %s: %s", task.asset, e)
            state.n_errors += 1
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected error processing %s: %s", task.asset, e)
            state.n_errors += 1
            continue

    state.last_signals_emitted = n_emitted
    return n_emitted


def _build_signal_record(
    signal,
    action: Action,
    task: AssetTask,
    asof: pd.Timestamp,
    ctx: MarketContext,
    bar_ts: pd.Timestamp,
) -> dict:
    """Construct the JSONL record per ADR-0008 schema.

    Per Phase-8 synthesis P0-A.1 (2026-05-13): persist `decision_price` so
    consumers can correctly attribute slippage and the settlement loop can
    compute the realized return formula correctly. `decision_price` is the
    signal's bar-close at signal.asof — that is, `ctx.last_close`.

    The `id` field is a CONTENT-DETERMINISTIC hash of the canonical replay
    identity `(asset, exchange, timeframe, bar_ts)`. Replaying the same
    market bar produces the same `id`, so downstream consumers can dedupe
    on signal_id and the watermark idempotency loop closes properly: a
    crash between `emit_signal_record()` and `watermark_store.set()` no
    longer leaks a fresh-UUID duplicate on restart.

    The `asof` decision-time prefix is preserved for human-debug
    readability ("which tick emitted this") but the disambiguating tail
    is now a sha1[:12] of the dedup tuple, not a random hex.
    """
    bar_ts_iso = (
        bar_ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if hasattr(bar_ts, "strftime")
        else str(bar_ts)
    )
    dedup_payload = "|".join(
        [
            task.asset,
            task.exchange or "",
            task.timeframe,
            bar_ts_iso,
        ]
    ).encode()
    dedup_tail = hashlib.sha1(dedup_payload).hexdigest()[:12]
    sig_id = (
        f"sig-{asof.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{task.asset.replace('/', '-')}-{dedup_tail}"
    )
    return {
        "schema_version": 1,
        "id": sig_id,
        "asof": asof.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "asset": task.asset,
        "exchange": task.exchange,
        "timeframe": task.timeframe,
        "asset_class": task.asset_class,
        "direction": signal.direction,
        "magnitude": float(signal.magnitude),
        "confidence": float(signal.confidence),
        "confidence_raw": float(signal.confidence_raw),
        "horizon": signal.horizon,
        "decision_price": float(ctx.last_close),  # P0-A.1
        "target_position_pct": float(action.target_position_pct),
        "reason": action.reason,
        "halt": bool(action.halt),
        "halt_scope": list(action.halt_scope) if action.halt_scope else None,
        "halt_until": (
            action.halt_until.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if action.halt_until is not None
            else None
        ),
        "components": [
            {
                "analyst": v.analyst,
                "direction": v.direction,
                "magnitude": float(v.magnitude),
                "confidence": float(v.confidence),
                "confidence_raw": float(v.confidence_raw),
                "horizon": v.horizon,
                "metadata": dict(v.metadata) if v.metadata else None,
            }
            for v in signal.components
        ],
        "aggregator": signal.aggregator,
        "metadata": dict(signal.metadata) if signal.metadata else None,
        "semantic_packet_hashes": [
            (dict(v.metadata).get("packet_hash") if v.metadata else None)
            for v in signal.components
            if v.metadata and dict(v.metadata).get("packet_hash")
        ],
        "committee_turns_hashes": [
            turn.get("input_hash")
            for turn in (
                (dict(signal.metadata).get("committee") or {}).get("model_backed_turns", [])
                if signal.metadata
                else []
            )
            if turn.get("input_hash")
        ],
    }
