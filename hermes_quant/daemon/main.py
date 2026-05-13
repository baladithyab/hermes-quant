"""hermes_quant.daemon.main — Daemon entry point.

Wires the lock + heartbeat emitter + tick loop + settlement loop into a
single long-lived process. Invoked via:

    hermes-quant-daemon [--account ACCOUNT] [--config PATH]

Or via the Hermes CLI:

    hermes quant start [--account ACCOUNT]

The daemon does NOT spawn freqtrade — that's a separate process the user
runs alongside (per ADR-0008 sidecar architecture).

Lifecycle:
  1. Acquire DaemonLock (singleton enforcement).
  2. Discover analysts / aggregator / data providers from entry points.
  3. Bootstrap halt registry from SQLite.
  4. Start HeartbeatEmitter thread.
  5. Loop: tick → settle → sleep(tick_interval).
  6. On SIGINT/SIGTERM: stop heartbeat, release lock, exit cleanly.

For v0.1.1 the config is hardcoded to a sensible default (BTC/USDT 1h
crypto with conservative profile). v0.1.2 will read from
~/.hermes/config.yaml::quant.

Per synthesis-v2: NO unilateral spending of compute. The daemon's tick
interval default (60s) is conservative; a user can lower it via config
once they're paper-trading happily.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from hermes_quant.daemon.discovery import (
    instantiate_aggregator,
    instantiate_analysts,
    instantiate_data_provider,
)
from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.heartbeat import HeartbeatEmitter
from hermes_quant.daemon.lock import DaemonLock
from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.daemon.settlement_loop import (
    construct_episode_outcomes,
    construct_realized_outcomes,
    dispatch_settlement,
    find_signals_for_executions,
)
from hermes_quant.daemon.signal_bus import (
    EXECUTION_BUS_PATH,
    read_jsonl_tail,
)
from hermes_quant.daemon.tick_loop import (
    AssetTask,
    TickLoopState,
    run_one_tick,
)
from hermes_quant.protocol import DaemonAlreadyRunning
from hermes_quant.risk.gate import PROFILES, DefaultRiskGate

logger = logging.getLogger(__name__)


_SHUTDOWN = False


def _sigterm_handler(_signum, _frame):
    global _SHUTDOWN
    _SHUTDOWN = True
    logger.info("shutdown signal received")


def _build_default_tasks() -> list[AssetTask]:
    """v0.1.1 default tasks. v0.1.2 will read from config."""
    return [
        AssetTask(asset="BTC/USDT", asset_class="crypto", timeframe="1h",
                  exchange="binance", horizon="4h"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-quant-daemon")
    parser.add_argument("--account", default="default",
                        help="Account ID (used for lock file and partitions)")
    parser.add_argument("--profile", default="conservative",
                        choices=list(PROFILES.keys()),
                        help="Risk config profile")
    parser.add_argument("--tick-interval", type=int, default=60,
                        help="Seconds between ticks (default 60)")
    parser.add_argument("--data-provider", default="yfinance",
                        help="Primary data provider (entry-point name)")
    parser.add_argument("--aggregator", default="bma",
                        help="Aggregator (entry-point name)")
    parser.add_argument("--analysts", default="classical-ta",
                        help="Comma-separated analyst names (entry-point keys)")
    parser.add_argument("--max-ticks", type=int, default=0,
                        help="Stop after N ticks (0 = forever)")
    parser.add_argument("--initial-cash", type=float, default=100_000.0,
                        help="Starting cash (paper-trade)")
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Acquire singleton lock
    lock = DaemonLock(account_id=args.account)
    try:
        lock.acquire()
    except DaemonAlreadyRunning as e:
        logger.error("%s", e)
        return 2

    # Signal handlers
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    try:
        # Bootstrap components
        halt_state = HaltStateSQLite()

        provider = instantiate_data_provider(args.data_provider)
        if provider is None:
            logger.error("data provider %s not found", args.data_provider)
            return 3
        data_providers = [provider]

        analyst_names = [n.strip() for n in args.analysts.split(",") if n.strip()]
        # Map dashes to underscores for entry-point lookup if needed
        analysts = instantiate_analysts(enabled_names=analyst_names)
        if not analysts:
            # Try with underscore conversion (e.g. classical-ta → classical_ta)
            alt = [n.replace("-", "_") for n in analyst_names]
            analysts = instantiate_analysts(enabled_names=alt)
        if not analysts:
            logger.error("no analysts loaded; check entry points")
            return 4

        aggregator = instantiate_aggregator(args.aggregator)
        if aggregator is None:
            logger.error("aggregator %s not found", args.aggregator)
            return 5

        risk_gate = DefaultRiskGate(PROFILES[args.profile]())

        # Heartbeat emitter
        state = TickLoopState()

        def heartbeat_state() -> dict:
            return {
                "last_tick_at": state.last_tick_at,
                "active_assets": state.active_assets,
                "n_ticks": state.n_ticks,
                "n_errors": state.n_errors,
            }

        heartbeat = HeartbeatEmitter(
            get_state=heartbeat_state,
            interval_seconds=10.0,
        )
        heartbeat.start()

        # Portfolio loader factory
        def portfolio_for(account_id: str, asset_class: str):
            return reconstruct_portfolio(
                account_id, asset_class,
                initial_cash=args.initial_cash,
                bus_path=EXECUTION_BUS_PATH,
            )

        # Settlement state — track which exec records we've already processed
        last_settled_record_count = 0
        analysts_by_name = {a.name: a for a in analysts}

        # Main loop
        tasks = _build_default_tasks()
        logger.info(
            "daemon started: account=%s profile=%s tick=%ds analysts=%s",
            args.account, args.profile, args.tick_interval,
            [a.name for a in analysts],
        )

        tick_count = 0
        while not _SHUTDOWN:
            tick_count += 1

            # Tick: fetch → analyze → aggregate → gate → emit
            try:
                n_emitted = run_one_tick(
                    tasks=tasks,
                    data_providers=data_providers,
                    analysts=analysts,
                    aggregator=aggregator,
                    risk_gate=risk_gate,
                    halt_state=halt_state,
                    portfolio_for=portfolio_for,
                    state=state,
                )
                if n_emitted > 0:
                    logger.info("tick #%d: emitted %d signals",
                                tick_count, n_emitted)
            except Exception as e:  # noqa: BLE001
                logger.exception("tick failed: %s", e)
                state.n_errors += 1

            # Settlement: tail executions.jsonl, dispatch updates
            try:
                exec_records = read_jsonl_tail(EXECUTION_BUS_PATH, n=1000)
                new_records = exec_records[last_settled_record_count:]
                if new_records:
                    signals = find_signals_for_executions(new_records)
                    realized = construct_realized_outcomes(new_records, signals)
                    episodes = construct_episode_outcomes(new_records, signals)
                    stats = dispatch_settlement(
                        realized, episodes,
                        analysts_by_name=analysts_by_name,
                        aggregator=aggregator,
                    )
                    if stats["n_realized"] + stats["n_episodes"] > 0:
                        logger.info("settlement: %s", stats)
                    last_settled_record_count = len(exec_records)
            except Exception as e:  # noqa: BLE001
                logger.warning("settlement loop error: %s", e)

            if args.max_ticks > 0 and tick_count >= args.max_ticks:
                logger.info("max_ticks reached; stopping")
                break

            # Sleep with shutdown awareness
            for _ in range(args.tick_interval):
                if _SHUTDOWN:
                    break
                time.sleep(1)

        heartbeat.stop()
        logger.info("daemon shutdown complete: %d ticks, %d errors",
                    state.n_ticks, state.n_errors)
        return 0

    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
