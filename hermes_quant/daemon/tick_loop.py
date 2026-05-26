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

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.signal_bus import (
    SIGNAL_BUS_PATH,
    emit_signal_record,
)
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

    Returns:
        Number of non-silent Actions emitted to the bus.
    """
    asof = asof if asof is not None else pd.Timestamp.utcnow()
    state.n_ticks += 1
    state.last_tick_at = asof
    state.active_assets = [t.asset for t in tasks]

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

            # Emit signal record
            record = _build_signal_record(signal, action, task, asof, ctx)
            emit_signal_record(record, path=bus_path)
            n_emitted += 1
            logger.info(
                "emitted signal: asset=%s dir=%d tgt=%.3f reason=%s",
                task.asset,
                signal.direction,
                action.target_position_pct,
                action.reason,
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
) -> dict:
    """Construct the JSONL record per ADR-0008 schema.

    Per Phase-8 synthesis P0-A.1 (2026-05-13): persist `decision_price` so
    consumers can correctly attribute slippage and the settlement loop can
    compute the realized return formula correctly. `decision_price` is the
    signal's bar-close at signal.asof — that is, `ctx.last_close`.
    """
    sig_id = f"sig-{asof.strftime('%Y%m%dT%H%M%SZ')}-{task.asset.replace('/', '-')}-{uuid.uuid4().hex[:6]}"
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
