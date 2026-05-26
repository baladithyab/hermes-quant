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

## v0.1.1 limitation — calibrator updates gated off (Phase-8 P0-A.3)

Phase-8 cross-family review (synthesis 2026-05-13 §P0-A) caught that the
single-fill realized_return formula:

    buy fill : (fill_price - decision_price) / decision_price
    sell fill: (decision_price - fill_price) / decision_price

is the SLIPPAGE per fill, not the directional return over signal.horizon.
A correct calibrator update needs the entry+exit pair joined together to
compute "did the price move in the signal's predicted direction over the
signal's horizon." v0.1.1 does not have exit-fill joining (executions
arrive one at a time without entry/exit metadata).

Until v0.1.2 lands proper join logic, calibrator updates are GATED OFF:
- construct_realized_outcomes still produces RealizedOutcome objects with
  the slippage value as `realized_return` (legacy field name; see v0.1.2
  for the rename).
- direction_correct is computed but flagged as PRELIMINARY by
  `_calibration_quality = "slippage_only"` on the outcome's view.metadata.
- dispatch_settlement reads `_calibration_quality` and SKIPS analyst.update()
  + aggregator.update() when it equals "slippage_only".

This means v0.1.1 settles fills (logs them, computes slippage) but does
NOT corrupt analyst Beta posteriors with noisy single-fill data. The
RollingSlippageEstimator can still be wired up correctly because it
explicitly wants the per-fill adverse-bps value, not the directional
return.
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

# Phase-8 P0-A.3 (2026-05-13): until v0.1.2 ships entry+exit fill joining,
# tag outcomes computed from a single fill's slippage as low-quality so
# downstream calibrator updates skip them.
CALIBRATION_QUALITY_SLIPPAGE_ONLY = "slippage_only"
CALIBRATION_QUALITY_HORIZON_RETURN = "horizon_return"  # v0.1.2+


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
    needed_ids = {rec["signal_id"] for rec in execution_records if rec.get("signal_id")}
    if not needed_ids:
        return {}

    signals = read_jsonl_tail(signal_bus_path, n=n_signal_records)
    return {s["id"]: s for s in signals if s.get("id") in needed_ids}


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

    Per Phase-8 P0-A.3: the value computed and stored as `realized_return`
    on each outcome is actually the per-fill SLIPPAGE, not the directional
    return over the signal's horizon. We tag every outcome's view metadata
    with `_calibration_quality = CALIBRATION_QUALITY_SLIPPAGE_ONLY` so
    `dispatch_settlement` skips analyst/aggregator update calls until
    v0.1.2 ships entry+exit fill joining.
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
        # Per Phase-8 P0-A.3: this is per-fill SLIPPAGE, not horizon return.
        # Stored on `realized_return` for legacy field-name compatibility;
        # downstream consumers MUST check view.metadata['_calibration_quality']
        # before treating this as a directional outcome.
        if side == "buy":
            realized_return = (fill_price - decision_price) / decision_price
        elif side == "sell":
            realized_return = (decision_price - fill_price) / decision_price
        else:
            continue

        sig_direction = sig.get("direction", 0)
        # Direction-correct as a heuristic ONLY (since realized_return is
        # slippage, not horizon return). Preserved for v0.1.2 schema
        # continuity but consumers must gate on _calibration_quality.
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
                    metadata={
                        "_calibration_quality": CALIBRATION_QUALITY_SLIPPAGE_ONLY,
                    },
                )
            except (KeyError, ValueError, TypeError):
                continue
            comp_direction_correct = (comp["direction"] > 0 and realized_return > 0) or (
                comp["direction"] < 0 and realized_return < 0
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
                comp["direction"] > 0 and realized_return > 0
            ) or (comp["direction"] < 0 and realized_return < 0)

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
            # Phase-8 P0-A.3: tag this AggregatedSignal as derived from
            # single-fill slippage so dispatch_settlement skips its
            # aggregator.update() call. v0.1.2 will lift the gate when
            # entry+exit fill joining lands.
            metadata={
                "_calibration_quality": CALIBRATION_QUALITY_SLIPPAGE_ONLY,
            },
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

    Per Phase-8 P0-A.3: outcomes tagged
    `view.metadata['_calibration_quality'] == CALIBRATION_QUALITY_SLIPPAGE_ONLY`
    are SKIPPED (not dispatched to analyst.update / aggregator.update). The
    underlying realized_return value on these outcomes is per-fill slippage,
    not horizon return — feeding it into Beta posteriors would corrupt
    calibration. v0.1.2 will lift this gate when entry+exit fill joining
    lands.

    Returns a stats dict for logging:
      {n_realized, n_episodes, n_analyst_updates, n_aggregator_updates,
       n_skipped_slippage_only}
    """
    stats = {
        "n_realized": len(realized_outcomes),
        "n_episodes": len(episode_outcomes),
        "n_analyst_updates": 0,
        "n_aggregator_updates": 0,
        "n_skipped_slippage_only": 0,
    }

    for outcome in realized_outcomes:
        # Phase-8 P0-A.3 gate: skip slippage-only outcomes
        meta = outcome.view.metadata or {}
        if meta.get("_calibration_quality") == CALIBRATION_QUALITY_SLIPPAGE_ONLY:
            stats["n_skipped_slippage_only"] += 1
            continue

        analyst = analysts_by_name.get(outcome.view.analyst)
        if analyst is None:
            continue
        if hasattr(analyst, "update"):
            try:
                analyst.update(outcome)
                stats["n_analyst_updates"] += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("analyst %s update failed: %s", outcome.view.analyst, e)

    # Episode outcomes: also skip if their aggregated_signal carries the
    # slippage-only quality tag on its components (which they will in v0.1.1
    # because construct_episode_outcomes inherits the single-fill slippage
    # formula).
    for sig_id, episode in episode_outcomes:
        sig_meta = episode.aggregated_signal.metadata or {}
        if sig_meta.get("_calibration_quality") == CALIBRATION_QUALITY_SLIPPAGE_ONLY:
            stats["n_skipped_slippage_only"] += 1
            continue
        try:
            aggregator.update(episode)
            stats["n_aggregator_updates"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("aggregator update failed for %s: %s", sig_id, e)

    return stats
