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
from dataclasses import dataclass
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


# ===========================================================================
# Settlement v0.1.2 — exit-fill joining + horizon-return math (ADR-0083 Phase 0b)
# ===========================================================================
#
# Everything below is PURELY ADDITIVE measurement infrastructure. It does NOT
# touch the deterministic risk gate, the sizing ladder, the kill-switch, or
# the slippage_only calibration gate above. It reads the SAME executions.jsonl
# records the rest of the loop reads and joins an EXIT fill to its ENTRY fill
# so a realized holding-period return can be computed and written to a
# settlement record. The reflector + calibrator may then read realized alpha.
#
# Why this is the keystone (ADR-0083): settlement v0.1.1 computes per-fill
# SLIPPAGE only — it never joins an exit to its entry, so a multi-period hold
# cannot be scored, which blocks BMA Beta auto-learning (O6) and any
# horizon-mode measurement. Joining the pair and computing entry->exit return
# net of cost is the measurement instrument every eval depends on.
#
# Honesty rails:
#   - asof-honest: an exit fill is only matched against entries whose asof is
#     <= the exit's asof (no lookahead — you cannot close a lot before you
#     opened it).
#   - no fabrication: an unpaired / still-open lot yields realized_return=None,
#     NOT 0.0. A fabricated 0 would silently feed the calibrator a "flat"
#     outcome that never happened.
#   - net of the existing cost model: the realized return is computed on the
#     actual fill prices (which already embed the slippage envelope applied at
#     fill time, ADR-0070) and is further netted of the explicit `fees`
#     recorded on both legs. No new cost assumption is introduced.
#   - deterministic: FIFO lot matching over records in bus order; no RNG, no
#     clock reads.


@dataclass(frozen=True)
class SettledRoundTrip:
    """A joined entry+exit (or partial-exit) lot with its realized return.

    A single exit fill may settle quantity drawn from multiple FIFO entry
    lots; each (entry_lot, exit_fill) pairing of matched quantity produces one
    SettledRoundTrip. Realized return is the holding-period return on the
    matched quantity, entry_price -> exit_price, net of the prorated fees on
    both legs.

    asof_entry <= asof_exit is guaranteed by the matcher (asof-honest).
    """

    asset: str
    account_id: str
    asset_class: str
    side: str  # the ENTRY side: "buy" (long lot) or "sell" (short lot)
    qty: float  # matched quantity settled by this pairing (always > 0)
    entry_price: float
    exit_price: float
    asof_entry: pd.Timestamp
    asof_exit: pd.Timestamp
    entry_exec_id: str | None
    exit_exec_id: str | None
    entry_signal_id: str | None
    exit_signal_id: str | None
    fees: float  # prorated entry + exit fees attributed to the matched qty
    realized_return: float
    """Holding-period return on the matched qty, net of prorated fees.

    For a long lot (entry side == "buy"):
        gross = (exit_price - entry_price) / entry_price
    For a short lot (entry side == "sell"):
        gross = (entry_price - exit_price) / entry_price
    The fee drag (prorated fees / entry notional) is subtracted from gross so
    the sign convention is "positive return == the lot made money".
    """


def _coerce_asof(value) -> pd.Timestamp | None:
    """Parse an asof value to a tz-naive-comparable Timestamp, or None."""
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    return ts


def _exec_sort_key(rec: dict) -> tuple:
    """Stable, asof-honest ordering key for execution records.

    Orders by asof ascending so FIFO entries open before exits close them.
    Ties broken by exec_id then the record's positional index (added by the
    caller) so ordering is fully deterministic even for same-asof fills.
    """
    asof = _coerce_asof(rec.get("asof"))
    # NaT-asof records sort last but stay deterministic via exec_id/index.
    asof_key = asof.value if asof is not None else 1 << 62
    return (asof_key, str(rec.get("exec_id") or ""), rec.get("_idx", 0))


def join_exit_fills(
    execution_records: Iterable[dict],
    *,
    open_lots: dict | None = None,
) -> tuple[list[SettledRoundTrip], dict]:
    """Join exit fills to their entry fills via FIFO lot matching.

    Walks executions in asof-honest (asof-ascending, then exec_id, then bus
    order) sequence. Each fill either OPENS/ADDS to the open-lot queue for its
    (account, asset_class, asset) bucket (when it is the same direction as the
    current net position) or CLOSES against the oldest opposing lots first
    (FIFO). Each matched (entry_lot, exit_fill) pairing emits one
    SettledRoundTrip carrying the realized holding-period return on the matched
    quantity, net of prorated fees on both legs.

    A direction flip (e.g. selling more than the open long) closes the entire
    opposing queue and opens a fresh lot with the residual quantity, so the
    join never fabricates a phantom return for the residual.

    Args:
        execution_records: executions to settle. Read-only; not mutated.
        open_lots: optional carry-in lot state from a prior call (the returned
            `open_lots` of an earlier invocation). Lets the daemon settle
            incrementally without re-reading the whole bus. Defaults to empty.

    Returns:
        (round_trips, open_lots):
          - round_trips: SettledRoundTrip per matched entry/exit pairing,
            in the order exits were processed (deterministic).
          - open_lots: residual open-lot state ({bucket_key: [lot, ...]}) for
            unmatched (still-open) quantity. Carry this into the next call.
            Quantity still in open_lots has realized_return = None semantics
            (it is simply absent from round_trips — never a fabricated 0).
    """
    # bucket_key -> list of open lots (FIFO; each lot is a mutable dict).
    lots: dict[tuple, list[dict]] = {}
    if open_lots:
        # Deep-ish copy so we never mutate the caller's carry-in structure.
        for k, v in open_lots.items():
            lots[k] = [dict(lot) for lot in v]

    indexed = [{**rec, "_idx": i} for i, rec in enumerate(execution_records)]
    ordered = sorted(indexed, key=_exec_sort_key)

    round_trips: list[SettledRoundTrip] = []

    for rec in ordered:
        side = rec.get("side")
        if side not in ("buy", "sell"):
            continue
        try:
            qty = float(rec["qty"])
            fill_price = float(rec["fill_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if qty <= 0 or fill_price <= 0:
            continue
        asof = _coerce_asof(rec.get("asof"))
        if asof is None:
            continue
        fees = float(rec.get("fees", 0.0) or 0.0)

        bucket = (
            rec.get("account_id"),
            rec.get("asset_class"),
            rec.get("asset"),
        )
        queue = lots.setdefault(bucket, [])

        # Current net direction of the open queue: "buy" lots are long, "sell"
        # lots are short. A queue holds only one direction at a time (a flip
        # fully closes the queue before opening the residual).
        queue_side = queue[0]["side"] if queue else None

        # fee-per-unit on this fill, used to prorate the matched-qty fee share.
        fee_per_unit = (fees / qty) if qty > 0 else 0.0

        if queue_side is None or queue_side == side:
            # Opening or adding to a same-direction lot — just enqueue.
            queue.append(
                {
                    "asset": rec.get("asset"),
                    "account_id": rec.get("account_id"),
                    "asset_class": rec.get("asset_class"),
                    "side": side,
                    "qty": qty,
                    "price": fill_price,
                    "asof": asof,
                    "exec_id": rec.get("exec_id"),
                    "signal_id": rec.get("signal_id"),
                    "fee_per_unit": fee_per_unit,
                }
            )
            continue

        # Opposing fill — close FIFO against the open queue.
        remaining = qty
        exit_fee_per_unit = fee_per_unit
        while remaining > 1e-12 and queue:
            lot = queue[0]
            # asof-honest: an entry must not be later than the exit closing it.
            if lot["asof"] > asof:
                # Out-of-order record (lot opened "after" this exit). Skip the
                # match rather than fabricate a lookahead pairing; leave the
                # lot open. Deterministic + fail-closed.
                break
            matched = min(lot["qty"], remaining)
            entry_price = lot["price"]
            exit_price = fill_price

            if lot["side"] == "buy":
                gross = (exit_price - entry_price) / entry_price
            else:  # short lot
                gross = (entry_price - exit_price) / entry_price

            entry_notional = entry_price * matched
            prorated_fees = lot["fee_per_unit"] * matched + exit_fee_per_unit * matched
            fee_drag = (prorated_fees / entry_notional) if entry_notional > 0 else 0.0
            realized_return = gross - fee_drag

            round_trips.append(
                SettledRoundTrip(
                    asset=lot["asset"],
                    account_id=lot["account_id"],
                    asset_class=lot["asset_class"],
                    side=lot["side"],
                    qty=matched,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    asof_entry=lot["asof"],
                    asof_exit=asof,
                    entry_exec_id=lot["exec_id"],
                    exit_exec_id=rec.get("exec_id"),
                    entry_signal_id=lot["signal_id"],
                    exit_signal_id=rec.get("signal_id"),
                    fees=prorated_fees,
                    realized_return=realized_return,
                )
            )

            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-12:
                queue.pop(0)

        # Residual opposing quantity beyond the open queue = a direction flip.
        # Open a fresh lot in the new direction with the leftover (its fee
        # share is the un-consumed remainder of this fill's fees).
        if remaining > 1e-12:
            queue.append(
                {
                    "asset": rec.get("asset"),
                    "account_id": rec.get("account_id"),
                    "asset_class": rec.get("asset_class"),
                    "side": side,
                    "qty": remaining,
                    "price": fill_price,
                    "asof": asof,
                    "exec_id": rec.get("exec_id"),
                    "signal_id": rec.get("signal_id"),
                    "fee_per_unit": exit_fee_per_unit,
                }
            )

        # Drop emptied buckets so open_lots only carries truly-open quantity.
        if not queue:
            lots.pop(bucket, None)

    return round_trips, lots


def compute_horizon_return(
    entry_price: float,
    exit_price: float,
    side: str,
    *,
    fees: float = 0.0,
) -> float | None:
    """Realized holding-period return for one entry->exit pair, net of fees.

    Pure helper exposed for the calibrator/reflector and for direct testing.

    Args:
        entry_price: lot entry fill price (> 0).
        exit_price: lot exit fill price (> 0).
        side: ENTRY side — "buy" (long) or "sell" (short).
        fees: total fees on the round trip, in quote currency, to net out.

    Returns:
        Net holding-period return (positive == the lot made money), or None if
        the inputs are not a well-formed pair (e.g. missing/non-positive
        price). None is the honest "cannot measure" value — never a
        fabricated 0.0.
    """
    if side not in ("buy", "sell"):
        return None
    try:
        ep = float(entry_price)
        xp = float(exit_price)
    except (ValueError, TypeError):
        return None
    if ep <= 0 or xp <= 0:
        return None
    if side == "buy":
        gross = (xp - ep) / ep
    else:
        gross = (ep - xp) / ep
    # Net of fees: prorate against entry notional (qty cancels, so pass total
    # fees / (entry_price) per unit — caller supplies total fees and we treat
    # one notional unit). For a per-unit-agnostic helper we express fee drag
    # relative to entry price; callers with qty should use join_exit_fills.
    fee_drag = (fees / ep) if (fees and ep > 0) else 0.0
    return gross - fee_drag


def realized_returns_by_signal(
    round_trips: Iterable[SettledRoundTrip],
) -> dict[str, float]:
    """Aggregate settled round trips into a {entry_signal_id: realized_return}.

    For signals settled by multiple exits (partial closes), the per-lot
    returns are combined as a notional-weighted average over the matched
    quantity so the result is the realized return of the whole entry signal.

    Returns only signals that have at least one settled (closed) lot; an
    entry signal whose position is still fully open does NOT appear (its
    realized return is None — absence, never a fabricated 0).
    """
    weighted_sum: dict[str, float] = defaultdict(float)
    weight: dict[str, float] = defaultdict(float)
    for rt in round_trips:
        if not rt.entry_signal_id:
            continue
        w = rt.entry_price * rt.qty  # entry notional
        if w <= 0:
            continue
        weighted_sum[rt.entry_signal_id] += rt.realized_return * w
        weight[rt.entry_signal_id] += w
    return {
        sig_id: weighted_sum[sig_id] / weight[sig_id]
        for sig_id in weighted_sum
        if weight[sig_id] > 0
    }
