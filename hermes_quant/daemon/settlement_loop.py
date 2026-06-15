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
import os
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
        # No-lookahead source guard: a signal row with a missing/None/empty/NaN
        # 'asof' must NOT become a NaT-stamped EpisodeOutcome. Downstream c96e
        # learning stamps observability = asof + horizon; a NaT asof would
        # poison the recency refit's no-lookahead filter (NaT >= x is always
        # False, silently admitting the sample). Coerce-and-skip mirrors the
        # _coerce_asof discipline already used by the exit-fill join.
        sig_asof = _coerce_asof(sig.get("asof"))
        if sig_asof is None:
            continue
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
            asof=sig_asof,
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
            asof=sig_asof,
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
# the slippage_only calibration gate above. It consumes the SAME executions.jsonl
# bus the rest of the loop reads — i.e. the ExecutionRecord schema serialized by
# hermes_quant.react.paper._record_to_dict (fields: asset, asset_class,
# asof_execution, target_position_pct, fill_price, fill_size_pct, signal_id,
# proposal_id, …). It does NOT add new bus columns: a bus->lot adapter
# (_normalize_exec_record) derives the lot side from the sign of the signed
# fill_size_pct, the lot qty from its magnitude (NAV-fraction units, matching
# portfolio_state's pos_delta convention), and the lot asof from asof_execution
# (the fill time). It then joins an EXIT fill to its ENTRY fill so a realized
# holding-period return can be computed and written to a settlement record. The
# reflector + calibrator may then read realized alpha.
#
# NOTE on history: an earlier draft of this block claimed it "reads the SAME
# records the rest of the loop reads" and then keyed off rec['side'] / ['qty'] /
# ['asof'] / ['exec_id'] / ['fees'] — keys that DO NOT EXIST on the real bus
# (review-team-3 defect 1). It does now, via the adapter above. There is NO
# production caller yet (daemon/main.py wires construct_realized_outcomes +
# dispatch_settlement, not join_exit_fills), so this stays additive; the
# adapter makes the instrument correct before a future seed wires it.
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
    """Parse an asof value to a UTC tz-aware Timestamp, or None.

    Review-team-3 defect (4): the bus mixes tz-aware ISO strings (the real
    PaperReactor stamps ``"...Z"`` / offset suffixes) with the occasional
    naive value carried in from older records or carry-in lot state. Comparing
    a tz-aware Timestamp against a naive one raises ``TypeError`` at the
    asof-honesty check. We therefore normalize EVERY asof to UTC tz-aware:
    naive inputs are localized to UTC; offset/Z inputs are converted to UTC.
    Comparisons across carry-in calls are then always well-formed.
    """
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    # Normalize to UTC tz-aware so naive/aware never mix at the comparison.
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _normalize_exec_record(rec: dict) -> dict | None:
    """Adapt a real executions.jsonl record to the canonical lot shape.

    Review-team-3 defect (1): the rest of the daemon (and the real
    PaperReactor / live reactors) write the ExecutionRecord schema serialized
    by ``hermes_quant.react.paper._record_to_dict`` — fields are ``asset``,
    ``asset_class``, ``asof_execution`` (ISO, the FILL time), ``fill_price``,
    ``fill_size_pct`` (SIGNED actual fill as a NAV fraction), ``signal_id``,
    ``proposal_id``, ``target_position_pct`` … There is NO ``side``, ``qty``,
    ``exec_id``, ``fees``, ``asof``, or ``account_id`` key on a real record.
    The original join_exit_fills read those non-existent keys and silently
    dropped every real fill.

    This adapter derives the lot fields from the real schema, while staying
    backward-compatible with records that already carry explicit
    ``side``/``qty``/``asof``/``exec_id``/``fees`` (older synthetic / future
    broker shapes): an explicit field always wins, the real-bus field is the
    fallback. Derivation from the real schema:

      - side: sign of the signed fill (``fill_size_pct``, else
        ``target_position_pct``). delta > 0 -> "buy" (long), < 0 -> "sell".
        This matches the position-quantity convention in
        ``portfolio_state._apply_execution_unsafe`` (``pos_delta =
        fill_size_pct``; ``new_qty = old_qty + fill_size_pct``), so a lot's
        direction is exactly the sign of the fraction it added.
      - qty: the MAGNITUDE of that signed fill (``abs(fill_size_pct)``), i.e.
        the NAV-fraction the fill moved the position by. The existing lot math
        treats notional as ``price * qty``, so qty in NAV-fraction units keeps
        the realized-return formula scale-free (gross is price-ratio based;
        fee drag is ``fees / (price * qty)``).
      - asof: ``asof_execution`` (the actual fill time) — the asof-honest
        ordering anchor. NOT ``asof_decision`` (advisor time).
      - asset: ``asset`` (the real field; there is no ``symbol`` key).
      - account_id: explicit ``account_id`` else ``reactor_metadata.account_id``
        else the bus default ``"paper-default"`` (mirrors PaperReactor's
        PortfolioState injection).
      - exec_id: explicit ``exec_id`` else ``proposal_id`` (the stable
        per-fill audit id on the real schema).
      - fees: explicit ``fees`` else ``reactor_metadata.fees`` else 0.0.

    Returns a dict with canonical keys (side, qty, fill_price, asof, asset,
    asset_class, account_id, exec_id, signal_id, fees) or None if the record
    is not a well-formed fill (no signed size, non-positive price, …). None
    records are skipped by the caller — never fabricated.
    """
    rmeta = rec.get("reactor_metadata")
    if not isinstance(rmeta, dict):
        rmeta = {}

    # ── side + qty ────────────────────────────────────────────────────────
    side = rec.get("side")
    qty = rec.get("qty")
    if side not in ("buy", "sell") or qty is None:
        # Derive from the signed NAV-fraction fill (real-bus path).
        signed = rec.get("fill_size_pct")
        if signed is None:
            signed = rec.get("target_position_pct")
        try:
            signed_f = float(signed)
        except (TypeError, ValueError):
            return None
        if signed_f == 0.0:
            # A zero-size fill (e.g. an admissibility REJECT record stamped
            # fill_size_pct=0.0) is not a position-moving lot. Skip it.
            return None
        derived_side = "buy" if signed_f > 0 else "sell"
        derived_qty = abs(signed_f)
        if side not in ("buy", "sell"):
            side = derived_side
        if qty is None:
            qty = derived_qty

    try:
        qty_f = float(qty)
        fill_price = float(rec["fill_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if qty_f <= 0 or fill_price <= 0:
        return None

    # ── asof: explicit `asof` else the real `asof_execution` fill time ─────
    asof = _coerce_asof(rec.get("asof") if rec.get("asof") is not None
                        else rec.get("asof_execution"))
    if asof is None:
        return None

    # ── fees: explicit else reactor_metadata.fees else 0.0 ─────────────────
    fees_raw = rec.get("fees")
    if fees_raw is None:
        fees_raw = rmeta.get("fees")
    try:
        fees = float(fees_raw) if fees_raw is not None else 0.0
    except (TypeError, ValueError):
        fees = 0.0

    account_id = (
        rec.get("account_id")
        or rmeta.get("account_id")
        or "paper-default"
    )
    exec_id = rec.get("exec_id") or rec.get("proposal_id")

    return {
        "side": side,
        "qty": qty_f,
        "fill_price": fill_price,
        "asof": asof,
        "asset": rec.get("asset"),
        "asset_class": rec.get("asset_class"),
        "account_id": account_id,
        "exec_id": exec_id,
        "signal_id": rec.get("signal_id"),
        "fees": fees,
        "_idx": rec.get("_idx", 0),
    }


def _exec_sort_key(rec: dict) -> tuple:
    """Stable, asof-honest ordering key for canonical lot records.

    Orders by asof ascending so FIFO entries open before exits close them.

    Review-team-3 defect (2): ties (same-asof fills) MUST break by the
    record's positional BUS INDEX (``_idx``) BEFORE any id. The bus order is
    the authoritative open-before-close lineage — an opening fill is appended
    before the closing fill that settles it. The original key tie-broke by
    ``exec_id`` lexically first, so a same-asof exit whose id sorts before its
    entry's id was processed first and treated as the OPENING lot, inverting
    both side and magnitude (a long opened +10% would record as short +9.09%).
    Never tie-break by lexical id before positional order. ``exec_id`` is kept
    only as a final, fully-deterministic last resort after ``_idx``.
    """
    asof = _coerce_asof(rec.get("asof"))
    # NaT-asof records sort last but stay deterministic via index/exec_id.
    asof_key = asof.value if asof is not None else 1 << 62
    return (asof_key, rec.get("_idx", 0), str(rec.get("exec_id") or ""))


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
    # Materialize the read-only input once (it may be a one-shot iterable).
    raw_records = list(execution_records)

    # i0c (ADR-0091 Option E, flag-gated): the production ExecutionRecord schema
    # writes `fill_size_pct` as the ABSOLUTE post-fill target, but this FIFO reads
    # it as a traded DELTA. Under HERMES_QUANT_DELTA_NORMALIZER==1 we run the ONE
    # shared FillDeltaNormalizer as a pre-pass so the FIFO sees the true increment
    # (re-affirm -> delta 0 -> skipped; flatten-to-0 -> the negative close delta),
    # exactly mirroring the position fold (portfolio_state.py:621-633,662). Flag
    # OFF (production default) => no pre-pass => the FIFO reads the raw field =>
    # byte-identical to legacy. The normalizer's carry-forward delta = target -
    # running_net is ORDER-DEPENDENT, so it must see records in asof order: we
    # stable-sort a COPY by asof_execution (mirror portfolio_state.py:633) and
    # override `fill_size_pct` on a SHALLOW COPY of each record (records are
    # read-only per the docstring) BEFORE _normalize_exec_record runs.
    if os.environ.get("HERMES_QUANT_DELTA_NORMALIZER", "0") == "1":
        from hermes_quant.state.fill_delta_normalizer import FillDeltaNormalizer

        _normalizer = FillDeltaNormalizer()
        _ordered_for_norm = sorted(
            raw_records, key=lambda r: r.get("asof_execution") or ""
        )
        normalized_records: list[dict] = []
        for rec in _ordered_for_norm:
            rec_copy = dict(rec)
            rec_copy["fill_size_pct"] = _normalizer.delta_for(rec)
            normalized_records.append(rec_copy)
        raw_records = normalized_records

    # bucket_key -> list of open lots (FIFO; each lot is a mutable dict).
    lots: dict[tuple, list[dict]] = {}
    # 335e: lots (deferred exits AND the real position lots that share a bucket
    # with a deferred exit) re-expressed as records and re-fed into THIS call's
    # matching stream. A deferred exit was previously copied forward verbatim and
    # NEVER re-matched, so it could never settle even when a valid earlier opening
    # lot arrived later. Re-feeding both the deferred exit and its bucket's real
    # lots through the asof-sorted matcher lets the earlier opener settle the
    # deferred exit; a deferred exit still without an honest opener RE-DEFERS.
    redrain_records: list[dict] = []
    # Which real buckets must be re-fed as records (because a deferred exit shares
    # them) instead of pre-loaded into the FIFO queue — pre-loading would pin the
    # carry-in lots at the FRONT of the FIFO and block an earlier-arriving opener.
    deferred_real_buckets: set[tuple] = set()
    if open_lots:
        for k in open_lots:
            if k and k[0] == "_deferred":
                deferred_real_buckets.add(tuple(k[1:]))

    if open_lots:
        # Deep-ish copy so we never mutate the caller's carry-in structure.
        # Review-team-3 defect (4): re-coerce each carried-in lot's asof to UTC
        # tz-aware so a naive carry-in value (from an older record) never mixes
        # with this call's tz-aware exit asof at the asof-honesty comparison.
        for k, v in open_lots.items():
            is_deferred = bool(k) and k[0] == "_deferred"
            real_bucket = tuple(k[1:]) if is_deferred else tuple(k)
            if is_deferred or real_bucket in deferred_real_buckets:
                # 335e: re-express each lot (deferred exit, OR a real position lot
                # in a bucket that ALSO has a deferred exit) as a signed record so
                # it rejoins the asof-sorted matching loop below. Side -> sign:
                # buy -> +qty (opener), sell -> -qty (exit). The lot keeps its
                # original asof, so the global re-sort orders the now-earlier
                # opener before the deferred exit and the round-trip settles.
                for lot in v:
                    side = lot.get("side")
                    qty = abs(float(lot.get("qty", 0.0)))
                    if qty <= 0.0:
                        continue
                    signed = -qty if side == "sell" else qty
                    asof = lot.get("asof")
                    fee_per_unit = lot.get("fee_per_unit", 0.0) or 0.0
                    redrain_records.append(
                        {
                            "asset": lot.get("asset"),
                            "asset_class": lot.get("asset_class"),
                            "account_id": lot.get("account_id"),
                            "fill_size_pct": signed,
                            "fill_price": lot.get("price"),
                            "asof": asof,
                            "asof_execution": asof,
                            "exec_id": lot.get("exec_id"),
                            "proposal_id": lot.get("exec_id"),
                            "signal_id": lot.get("signal_id"),
                            "fees": fee_per_unit * qty,
                            # A re-drained DEFERRED EXIT must never OPEN a fresh
                            # position lot — it was a closing intent. If the asof-
                            # sorted matcher gives it no honest earlier opener to
                            # close (empty / same-direction queue), it must RE-DEFER,
                            # not become a phantom opener (which would invent a
                            # lookahead round-trip). A re-fed REAL position lot has
                            # no such tag — it is a genuine opener and opens freely.
                            "_is_deferred_exit": is_deferred,
                        }
                    )
                continue
            copied = []
            for lot in v:
                lot_copy = dict(lot)
                if "asof" in lot_copy:
                    coerced = _coerce_asof(lot_copy["asof"])
                    if coerced is not None:
                        lot_copy["asof"] = coerced
                copied.append(lot_copy)
            lots[k] = copied

    # Review-team-3 defect (1): normalize every record from the REAL bus
    # schema (ExecutionRecord / _record_to_dict) — there is no side/qty/asof
    # key on a real fill. The adapter derives them; non-fills return None and
    # are dropped (never fabricated). It also stamps the positional bus index
    # so defect (2)'s deterministic, open-before-close tie-break holds.
    #
    # 335e: the re-drained carry-in lots (deferred exits + their bucket's real
    # lots, re-expressed as records above) are merged with this call's new records
    # into ONE stream, then re-sorted by the asof-honest key. Each re-drained lot
    # keeps its original asof, so a NEW opening lot whose asof is earlier than a
    # deferred exit sorts first and the deferred exit settles against it; the
    # existing matching loop, defer branch, and one-direction invariant are all
    # unchanged. When there are no deferred buckets, redrain_records is empty and
    # this is byte-identical to the legacy carry-in-pre-loaded path.
    indexed = []
    next_idx = 0
    for rec in redrain_records:
        norm = _normalize_exec_record({**rec, "_idx": next_idx})
        if norm is not None:
            # _normalize_exec_record returns a fresh canonical dict and drops
            # unknown keys; carry the deferred-exit marker forward so the matching
            # loop can re-defer (not open) an unmatched deferred exit.
            norm["_is_deferred_exit"] = bool(rec.get("_is_deferred_exit"))
            indexed.append(norm)
        next_idx += 1
    for rec in raw_records:
        norm = _normalize_exec_record({**rec, "_idx": next_idx})
        if norm is not None:
            indexed.append(norm)
        next_idx += 1
    ordered = sorted(indexed, key=_exec_sort_key)

    round_trips: list[SettledRoundTrip] = []
    # Review-team-3 defect (3): deferred exits — fills whose closing quantity
    # could not honestly match the open queue because the only opposing lots
    # opened LATER (carry-in lookahead). Held here, keyed by bucket, so they
    # are returned still-pending instead of fabricating an opposing residual
    # lot in a bucket that already holds the other direction. Stored under a
    # distinct namespaced key in the returned open_lots so the one-direction
    # invariant per real bucket is never violated.
    deferred: dict[tuple, list[dict]] = {}

    for rec in ordered:
        side = rec["side"]
        qty = rec["qty"]
        fill_price = rec["fill_price"]
        asof = rec["asof"]
        fees = rec["fees"]

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
            # 335e: a re-drained DEFERRED EXIT that reaches the opening branch has
            # no honest opposing opener earlier in the asof-sorted stream (the
            # queue is empty or holds its OWN direction). It must NOT open a fresh
            # position lot — that would invent a phantom opener (a lookahead round-
            # trip when a later opener closes it). RE-DEFER it idempotently: hold
            # it still-pending under the namespaced bucket exactly as the original
            # deferral did, so the next incremental call can retry it.
            if rec.get("_is_deferred_exit"):
                deferred.setdefault(bucket, []).append(
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
        asof_honest_break = False  # True if we stopped on a lookahead lot
        while remaining > 1e-12 and queue:
            lot = queue[0]
            # asof-honest: an entry must not be later than the exit closing it.
            if lot["asof"] > asof:
                # Out-of-order record (lot opened "after" this exit). Skip the
                # match rather than fabricate a lookahead pairing; leave the
                # lot open. Deterministic + fail-closed. Defect (3): mark so we
                # DEFER the unmatched exit instead of opening an opposing
                # residual lot in this (now-mixed-direction) bucket.
                asof_honest_break = True
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

        # Residual quantity beyond what the open queue could honestly settle.
        if remaining > 1e-12:
            residual_lot = {
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
            if asof_honest_break:
                # Defect (3): the queue still holds opposing lots that opened
                # AFTER this exit (carry-in lookahead). We must NOT enqueue an
                # opposing residual into that bucket — that would leave the
                # bucket holding BOTH directions, violating the one-direction
                # queue invariant. DEFER the exit: hold it still-pending in a
                # namespaced bucket so it is returned to the caller (honestly
                # unsettled) rather than fabricating a phantom opposing lot.
                deferred.setdefault(bucket, []).append(residual_lot)
            else:
                # Genuine direction flip: the queue fully drained (every
                # opposing lot was older and is now closed), so the leftover
                # opens a fresh lot in the new direction. Single-direction
                # invariant holds because `queue` is empty here.
                queue.append(residual_lot)

        # Drop emptied buckets so open_lots only carries truly-open quantity.
        if not queue:
            lots.pop(bucket, None)

    # ── invariant assertion: each real bucket holds exactly one direction ──
    # Review-team-3 defect (3): a real position queue must never mix buy +
    # sell lots. Namespaced ("_deferred", ...) keys are NOT position queues —
    # they hold still-pending exits (possibly of differing sides across defer
    # events), so they are exempt from the one-direction invariant.
    for bkt, q in lots.items():
        if bkt and bkt[0] == "_deferred":
            continue
        sides = {lot["side"] for lot in q}
        assert len(sides) <= 1, (
            f"one-direction-per-queue invariant violated in bucket {bkt}: {sides}"
        )

    # Merge deferred (still-pending, honestly-unsettled) exits into the
    # returned open_lots under a namespaced key so they survive to the next
    # incremental call without colliding with the real position bucket. Append
    # (not overwrite) so deferred exits carried in from a prior call are kept.
    for bkt, dlots in deferred.items():
        lots.setdefault(("_deferred", *bkt), []).extend(dlots)

    return round_trips, lots


def compute_horizon_return(
    entry_price: float,
    exit_price: float,
    side: str,
    *,
    fees: float = 0.0,
    qty: float = 1.0,
) -> float | None:
    """Realized holding-period return for one entry->exit pair, net of fees.

    Pure helper exposed for the calibrator/reflector and for direct testing.

    Args:
        entry_price: lot entry fill price (> 0).
        exit_price: lot exit fill price (> 0).
        side: ENTRY side — "buy" (long) or "sell" (short).
        fees: total fees on the round trip, in quote currency, to net out.
        qty: lot quantity the ``fees`` were charged on (default 1.0).
            Review-team-3 defect (5): fee drag is the fee burden divided by the
            ENTRY NOTIONAL, which is ``entry_price * qty`` — exactly the
            convention ``join_exit_fills`` uses (``prorated_fees /
            (entry_price * matched)``). The original helper divided by
            ``entry_price`` alone (an implicit qty=1), so the two disagreed
            whenever qty != 1. With the explicit qty param they now agree, and
            the default 1.0 keeps existing single-unit callers/tests identical.

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
        q = float(qty)
    except (ValueError, TypeError):
        return None
    if ep <= 0 or xp <= 0:
        return None
    if q <= 0:
        return None
    if side == "buy":
        gross = (xp - ep) / ep
    else:
        gross = (ep - xp) / ep
    # Net of fees: divide the total fee burden by the entry NOTIONAL
    # (entry_price * qty), agreeing with join_exit_fills' prorated fee drag.
    entry_notional = ep * q
    fee_drag = (fees / entry_notional) if (fees and entry_notional > 0) else 0.0
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
