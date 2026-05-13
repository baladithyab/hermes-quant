"""hermes_quant.daemon.settlement_loop — Tail executions.jsonl, build outcomes.

Per ADR-0009 §P0-3 + §P1-9 + §P1-10: when a fill closes a position (or
partially settles), construct RealizedOutcome (per-analyst) and
EpisodeOutcome (cross-sectional), feed them to:
  - StatefulAnalyst.update(RealizedOutcome) — for per-analyst calibration
  - Aggregator.update(EpisodeOutcome) — for posterior weight evolution
  - DefaultRiskGate.record_loss() — for cooldown tracking
  - RollingSlippageEstimator.observe() — for cost-budget refinement

The settlement loop reads executions.jsonl, joins each execution back to
the originating signal record (via signal_id), and computes:
  - direction_correct: did the realized return have the same sign as the signal?
  - realized_return: actual log return over signal.horizon
  - realized_pnl_net: (close_price - entry_price) * qty - fees

For v0.1.1 we ship the SHELL of the settlement loop with the join logic
and outcome construction; the actual aggregator/analyst.update() dispatch
happens but ablation/refit logic is deferred to v0.1.2.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from hermes_quant.daemon.signal_bus import (
    EXECUTION_BUS_PATH,
    SIGNAL_BUS_PATH,
    read_jsonl_tail,
)
from hermes_quant.protocol import (
    AnalystView,
    EpisodeOutcome,
    RealizedOutcome,
    StatefulAnalyst,
)

logger = logging.getLogger(__name__)


def find_signals_for_executions(
    execution_records: list[dict],
    *,
    signal_bus_path: Path = SIGNAL_BUS_PATH,
    n_signal_records: int = 10_000,
) -> dict[str, dict]:
    """Build {signal_id: signal_record} map for the given executions.

    Args:
        execution_records: list of executions to find signals for.
        signal_bus_path: signal bus path.
        n_signal_records: max signals to scan from tail.

    Returns:
        {signal_id: signal_record} for matched signals.
    """
    needed_ids = {
        rec["signal_id"]
        for rec in execution_records
        if rec.get("signal_id")
    }
    if not needed_ids:
        return {}

    signals = read_jsonl_tail(signal_bus_path, n=n_signal_records)
    return {
        s["id"]: s for s in signals
        if s.get("id") in needed_ids
    }


def construct_realized_outcomes(
    execution_records: list[dict],
    signals: dict[str, dict],
) -> list[RealizedOutcome]:
    """Build RealizedOutcome list for analyst.update() dispatches.

    For each fill that has a matching signal:
      - For each analyst component in the signal, build a RealizedOutcome
        comparing that component's direction to the realized return sign.

    Note: the signal record stores `components` as a list of dicts (post
    JSONL roundtrip), not AnalystView dataclasses. We reconstruct the
    minimal AnalystView for the outcome dataclass.
    """
    outcomes: list[RealizedOutcome] = []
    for exec_rec in execution_records:
        sig_id = exec_rec.get("signal_id")
        if not sig_id or sig_id not in signals:
            continue
        sig = signals[sig_id]
        try:
            decision_price = float(exec_rec.get("decision_price", 0.0))
            fill_price = float(exec_rec["fill_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if decision_price <= 0 or fill_price <= 0:
            continue

        side = exec_rec.get("side")
        if side == "buy":
            realized_return = (fill_price - decision_price) / decision_price
        elif side == "sell":
            realized_return = (decision_price - fill_price) / decision_price
        else:
            continue

        sig_direction = sig.get("direction", 0)
        direction_correct = (sig_direction > 0 and realized_return > 0) or (
            sig_direction < 0 and realized_return < 0
        )

        for comp in sig.get("components", []):
            try:
                view = AnalystView(
                    analyst=comp["analyst"],
                    direction=comp["direction"],
                    magnitude=float(comp.get("magnitude", 0.0)),
                    confidence=float(comp.get("confidence", 0.0)),
                    confidence_raw=float(comp.get("confidence_raw", comp.get("confidence", 0.0))),
                    horizon=comp.get("horizon", sig.get("horizon", "1h")),
                )
            except (KeyError, ValueError, TypeError):
                continue
            comp_direction_correct = (
                (comp["direction"] > 0 and realized_return > 0)
                or (comp["direction"] < 0 and realized_return < 0)
            )
            outcomes.append(
                RealizedOutcome(
                    view=view,
                    asof_view=pd.Timestamp(sig["asof"]),
                    asof_settlement=pd.Timestamp(exec_rec["asof"]),
                    realized_return=realized_return,
                    direction_correct=comp_direction_correct,
                )
            )
    return outcomes


def construct_episode_outcomes(
    execution_records: list[dict],
    signals: dict[str, dict],
) -> list[tuple[str, EpisodeOutcome]]:
    """Build (signal_id, EpisodeOutcome) pairs for aggregator.update().

    Per ADR-0009 §P1-10: EpisodeOutcome is cross-sectional — contains the
    AggregatedSignal + per-analyst direction_correct map at the same
    timestamp.

    For v0.1.1 we synthesize the AggregatedSignal from the bus record's
    fields; in v0.1.2 we may also persist the original AggregatedSignal
    object (or its serialization) so the EpisodeOutcome can include the
    exact components list.
    """
    from hermes_quant.protocol import AggregatedSignal

    out: list[tuple[str, EpisodeOutcome]] = []
    for exec_rec in execution_records:
        sig_id = exec_rec.get("signal_id")
        if not sig_id or sig_id not in signals:
            continue
        sig = signals[sig_id]
        try:
            decision_price = float(exec_rec.get("decision_price", 0.0))
            fill_price = float(exec_rec["fill_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if decision_price <= 0:
            continue

        side = exec_rec.get("side")
        if side == "buy":
            realized_return = (fill_price - decision_price) / decision_price
        elif side == "sell":
            realized_return = (decision_price - fill_price) / decision_price
        else:
            continue

        # Reconstruct components
        components = []
        direction_correct: dict[str, bool] = {}
        for comp in sig.get("components", []):
            try:
                view = AnalystView(
                    analyst=comp["analyst"],
                    direction=comp["direction"],
                    magnitude=float(comp.get("magnitude", 0.0)),
                    confidence=float(comp.get("confidence", 0.0)),
                    confidence_raw=float(comp.get("confidence_raw", comp.get("confidence", 0.0))),
                    horizon=comp.get("horizon", sig.get("horizon", "1h")),
                )
                components.append(view)
            except (KeyError, ValueError, TypeError):
                continue
            direction_correct[comp["analyst"]] = (
                (comp["direction"] > 0 and realized_return > 0)
                or (comp["direction"] < 0 and realized_return < 0)
            )

        agg_signal = AggregatedSignal(
            asset=sig["asset"],
            timeframe=sig["timeframe"],
            asset_class=sig.get("asset_class", "crypto"),
            asof=pd.Timestamp(sig["asof"]),
            direction=sig["direction"],
            magnitude=float(sig.get("magnitude", 0.0)),
            confidence=float(sig.get("confidence", 0.0)),
            confidence_raw=float(sig.get("confidence_raw", sig.get("confidence", 0.0))),
            horizon=sig.get("horizon", "1h"),
            components=tuple(components),
            aggregator=sig.get("aggregator", "bma"),
        )

        episode = EpisodeOutcome(
            asset=sig["asset"],
            timeframe=sig["timeframe"],
            asof=pd.Timestamp(sig["asof"]),
            aggregated_signal=agg_signal,
            realized_returns={agg_signal.horizon: realized_return},
            direction_correct=direction_correct,
            realized_net_pnl=None,  # populated by portfolio_loader join in v0.1.2
        )
        out.append((sig_id, episode))
    return out


def dispatch_settlement(
    realized_outcomes: list[RealizedOutcome],
    episode_outcomes: list[tuple[str, EpisodeOutcome]],
    *,
    analysts_by_name: dict[str, StatefulAnalyst],
    aggregator,
) -> dict:
    """Dispatch outcomes to analyst.update() and aggregator.update().

    Returns a stats dict for logging:
      {n_realized, n_episodes, n_analyst_updates, n_aggregator_updates}
    """
    stats = {
        "n_realized": len(realized_outcomes),
        "n_episodes": len(episode_outcomes),
        "n_analyst_updates": 0,
        "n_aggregator_updates": 0,
    }

    for outcome in realized_outcomes:
        analyst = analysts_by_name.get(outcome.view.analyst)
        if analyst is None:
            continue
        if hasattr(analyst, "update"):
            try:
                analyst.update(outcome)
                stats["n_analyst_updates"] += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("analyst %s update failed: %s",
                               outcome.view.analyst, e)

    for sig_id, episode in episode_outcomes:
        try:
            aggregator.update(episode)
            stats["n_aggregator_updates"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("aggregator update failed for %s: %s", sig_id, e)

    return stats
