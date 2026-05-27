"""hermes_quant.schemas.bar_snapshot — Per-bar pipeline state Pydantic model.

Per ADR-0038 §D.2 (P5). This is the internal state model carried through
the daemon pipeline (fetch → analyze → aggregate → gate → emit). It is
NOT (yet) the canonical JSONL row shape — `to_jsonl_row()` produces the
existing, bit-identical legacy dict shape under
`HERMES_QUANT_SNAPSHOT_V2=0` (default), and an opt-in typed shape under
`HERMES_QUANT_SNAPSHOT_V2=1` (planned default-on in v0.5).

Constraints
-----------
* `model_config = ConfigDict(frozen=True, extra="forbid")` everywhere.
* Slots are `None` when their pipeline stage hasn't run.
* `meta` is REQUIRED — missing meta is an error (catches misuse early).
* No mutation of existing Protocol types: every helper takes a Protocol
  type by reference and stores a typed view over it.

Anchor: docs/adr/ADR-0038-tradingagents-pattern-backfill.md §D.2
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from hermes_quant.protocol import (
        Action,
        AggregatedSignal,
        AnalystView,
        MarketContext,
    )


# ---------------------------------------------------------------------------
# Slot models — small typed views over Protocol dataclasses
# ---------------------------------------------------------------------------


class MetaSlot(BaseModel):
    """Pipeline metadata: schema version, signal id, runtime tags."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    signal_id: str
    """`sig-<UTC_ISO_seconds>-<asset>-<random6>` per legacy emit."""
    exchange: str | None = None
    timeframe: str
    asset_class: str


class OHLCVSlot(BaseModel):
    """Last-bar OHLCV summary (not the full bars frame; that's in MarketContext)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_close: float
    last_volume: float
    n_bars: int = Field(ge=0)
    """Number of bars used to compute analyst views."""


class IndicatorsSlot(BaseModel):
    """Optional indicator snapshot (placeholder for v0.5 typed surface).

    Kept tiny for Wave D — most indicators currently flow through analyst
    metadata. The slot exists so per-bar replay can bind a typed view.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: Mapping[str, float] | None = None
    """Indicator name → scalar value at decision asof."""


class AnalystViewSlot(BaseModel):
    """Typed view over an `AnalystView` dataclass for snapshot serialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst: str
    direction: int
    magnitude: float
    confidence: float
    confidence_raw: float
    horizon: str
    metadata: Mapping[str, Any] | None = None


class AggregatedSignalSlot(BaseModel):
    """Typed view over an `AggregatedSignal` dataclass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aggregator: str
    direction: int
    magnitude: float
    confidence: float
    confidence_raw: float
    horizon: str
    metadata: Mapping[str, Any] | None = None
    components: tuple[AnalystViewSlot, ...] = ()


class RiskCheckSlot(BaseModel):
    """Risk-gate decision context (target size + reason).

    Independent of `FinalDecisionSlot` so a recipe can emit a "would-have"
    risk read without committing to a final action.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_position_pct: float
    reason: str
    halt: bool = False
    halt_scope: tuple[str, str, str | None] | None = None
    halt_until: datetime | None = None


class FinalDecisionSlot(BaseModel):
    """The final emit-shape decision (currently identical content to RiskCheckSlot,
    kept distinct so future recipes can split them).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_position_pct: float
    reason: str
    halt: bool = False
    halt_scope: tuple[str, str, str | None] | None = None
    halt_until: datetime | None = None


# ---------------------------------------------------------------------------
# DaemonState helper models (P6 mirror in tools.py reads these)
# ---------------------------------------------------------------------------


class SymbolStatus(BaseModel):
    """Per-symbol pipeline status reconstructed from the JSONL bus tail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_bar_ts: datetime | None = None
    stages_seen: tuple[str, ...] = ()
    """Subset of {ohlcv,indicators,analysts,aggregated,risk,final}, ordered."""
    last_action_dir: int | None = None
    last_action_conf: float | None = None


class HaltSummary(BaseModel):
    """Compact halt registry view (read-only mirror)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str | None
    asset_class: str | None
    asset: str | None
    reason: str
    halted_at: datetime
    halted_until: datetime | None = None


# ---------------------------------------------------------------------------
# BarSnapshot — the per-bar pipeline state model
# ---------------------------------------------------------------------------


_LEGACY_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
"""Legacy bus emit format. Matches `tick_loop._build_signal_record`."""


def _format_legacy_ts(ts: datetime) -> str:
    """Format a datetime in the legacy bus format (microseconds + Z)."""
    return ts.strftime(_LEGACY_TS_FMT)


class BarSnapshot(BaseModel):
    """Per-bar pipeline state. Each pipeline stage populates its slot.

    Used as the in-memory state passed between analysts/aggregator/risk-gate
    and as the future JSONL row schema for `tick_loop` emit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    symbol: str
    bar_ts: datetime
    """tz-naive UTC. The bar's close timestamp."""
    asof_decision: datetime
    """When the gate decided. tz-naive UTC."""

    # Slots populated in pipeline order. None = stage didn't run.
    ohlcv: OHLCVSlot | None = None
    indicators: IndicatorsSlot | None = None
    regime_label: str | None = None
    analyst_views: tuple[AnalystViewSlot, ...] | None = None
    aggregated_signal: AggregatedSignalSlot | None = None
    risk_check: RiskCheckSlot | None = None
    final_decision: FinalDecisionSlot | None = None
    meta: MetaSlot

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_market_context(
        cls,
        ctx: MarketContext,
        views: list[AnalystView] | None,
        signal: AggregatedSignal | None,
        action: Action | None,
        *,
        signal_id: str,
        bar_ts: datetime | None = None,
        asof_decision: datetime | None = None,
        regime_label: str | None = None,
        indicators: Mapping[str, float] | None = None,
    ) -> BarSnapshot:
        """Build a BarSnapshot from existing Protocol types.

        None-valued stages map to None slots; any populated stage takes a
        typed view of its source object. Does NOT mutate the inputs.

        ``signal_id`` is required because BarSnapshot.meta requires it
        (catches misuse — every emit needs an id).
        """
        # Resolve timestamps. `bar_ts` defaults to the LAST bar's timestamp
        # in `ctx.bars` — the upper bound of what's been processed by the
        # advisor. This is the correct identity for per-bar dedup (replay
        # diagnostics, V2 row dedup): re-running the same tick on the same
        # market bar must produce the same `bar_ts`. `asof_decision` defaults
        # to `ctx.asof` (the decision/tick wall-clock time), which can be
        # AFTER the last bar (e.g. tick at 10:05 processing the 10:00 bar).
        # See ADR-0038 §D.1 — `bar_ts` is the watermark/dedup identity,
        # `asof_decision` is the decision-clock anchor.
        ad = asof_decision if asof_decision is not None else ctx.asof
        ad_dt = ad.to_pydatetime() if hasattr(ad, "to_pydatetime") else ad

        if bar_ts is not None:
            ts = bar_ts
        else:
            # Try last bar timestamp from ctx.bars; fall back to asof if
            # bars is empty or column missing (defensive — advisor inputs
            # have always carried bars+timestamp at this stage).
            ts = ctx.asof
            try:
                bars_attr = getattr(ctx, "bars", None)
                if bars_attr is not None and len(bars_attr) > 0:
                    ts_col = bars_attr["timestamp"]
                    last_bar_ts = ts_col.iloc[-1]
                    # Normalize to tz-naive UTC to match watermark.py
                    if hasattr(last_bar_ts, "tzinfo") and last_bar_ts.tzinfo is not None:
                        last_bar_ts = last_bar_ts.tz_convert("UTC").tz_localize(None)
                    ts = last_bar_ts
            except (KeyError, AttributeError, IndexError):
                # Bars structure missing or malformed — preserve legacy fallback
                ts = ctx.asof
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts  # type: ignore[union-attr]

        meta = MetaSlot(
            signal_id=signal_id,
            exchange=ctx.exchange,
            timeframe=ctx.timeframe,
            asset_class=ctx.asset_class,
        )

        # Always populate ohlcv when ctx is given (cheap; deterministic)
        n_bars = int(len(ctx.bars)) if hasattr(ctx, "bars") else 0
        ohlcv = OHLCVSlot(
            last_close=float(ctx.last_close),
            last_volume=float(ctx.last_volume),
            n_bars=n_bars,
        )

        ind_slot: IndicatorsSlot | None = None
        if indicators is not None:
            ind_slot = IndicatorsSlot(snapshot=dict(indicators))

        view_slots: tuple[AnalystViewSlot, ...] | None = None
        if views is not None:
            view_slots = tuple(
                AnalystViewSlot(
                    analyst=v.analyst,
                    direction=int(v.direction),
                    magnitude=float(v.magnitude),
                    confidence=float(v.confidence),
                    confidence_raw=float(v.confidence_raw),
                    horizon=v.horizon,
                    metadata=dict(v.metadata) if v.metadata else None,
                )
                for v in views
            )

        signal_slot: AggregatedSignalSlot | None = None
        if signal is not None:
            comp_slots = tuple(
                AnalystViewSlot(
                    analyst=v.analyst,
                    direction=int(v.direction),
                    magnitude=float(v.magnitude),
                    confidence=float(v.confidence),
                    confidence_raw=float(v.confidence_raw),
                    horizon=v.horizon,
                    metadata=dict(v.metadata) if v.metadata else None,
                )
                for v in signal.components
            )
            signal_slot = AggregatedSignalSlot(
                aggregator=signal.aggregator,
                direction=int(signal.direction),
                magnitude=float(signal.magnitude),
                confidence=float(signal.confidence),
                confidence_raw=float(signal.confidence_raw),
                horizon=signal.horizon,
                metadata=dict(signal.metadata) if signal.metadata else None,
                components=comp_slots,
            )

        risk_slot: RiskCheckSlot | None = None
        final_slot: FinalDecisionSlot | None = None
        if action is not None:
            halt_until_dt = None
            if action.halt_until is not None:
                halt_until_dt = (
                    action.halt_until.to_pydatetime()
                    if hasattr(action.halt_until, "to_pydatetime")
                    else action.halt_until
                )
            risk_slot = RiskCheckSlot(
                target_position_pct=float(action.target_position_pct),
                reason=action.reason,
                halt=bool(action.halt),
                halt_scope=tuple(action.halt_scope) if action.halt_scope else None,
                halt_until=halt_until_dt,
            )
            final_slot = FinalDecisionSlot(
                target_position_pct=float(action.target_position_pct),
                reason=action.reason,
                halt=bool(action.halt),
                halt_scope=tuple(action.halt_scope) if action.halt_scope else None,
                halt_until=halt_until_dt,
            )

        return cls(
            symbol=ctx.asset,
            bar_ts=ts_dt,
            asof_decision=ad_dt,
            ohlcv=ohlcv,
            indicators=ind_slot,
            regime_label=regime_label,
            analyst_views=view_slots,
            aggregated_signal=signal_slot,
            risk_check=risk_slot,
            final_decision=final_slot,
            meta=meta,
        )

    # ------------------------------------------------------------------
    # JSONL row adapter
    # ------------------------------------------------------------------

    def to_jsonl_row(self) -> dict[str, Any]:
        """Serialize this snapshot to a JSONL-emit dict.

        Under `HERMES_QUANT_SNAPSHOT_V2=0` (default), the dict is
        bit-identical to the legacy `_build_signal_record` shape
        (see `hermes_quant/daemon/tick_loop.py`).

        Under `HERMES_QUANT_SNAPSHOT_V2=1`, emit a typed
        BarSnapshot.model_dump() with ISO timestamps.

        Raises:
            ValueError: if required slots for emit are missing
                (final_decision, aggregated_signal) under V2=0.
        """
        v2 = os.environ.get("HERMES_QUANT_SNAPSHOT_V2", "0") == "1"
        if v2:
            return self._to_jsonl_row_v2()
        return self._to_jsonl_row_legacy()

    # -- legacy emit shape (matches tick_loop._build_signal_record) --
    def _to_jsonl_row_legacy(self) -> dict[str, Any]:
        if self.aggregated_signal is None or self.final_decision is None:
            raise ValueError(
                "BarSnapshot.to_jsonl_row(legacy): aggregated_signal and "
                "final_decision are required to emit a legacy bus row."
            )

        sig = self.aggregated_signal
        dec = self.final_decision
        meta_md = dict(sig.metadata) if sig.metadata else None

        # Components — exactly matches tick_loop._build_signal_record
        components = [
            {
                "analyst": v.analyst,
                "direction": v.direction,
                "magnitude": float(v.magnitude),
                "confidence": float(v.confidence),
                "confidence_raw": float(v.confidence_raw),
                "horizon": v.horizon,
                "metadata": dict(v.metadata) if v.metadata else None,
            }
            for v in sig.components
        ]

        # decision_price = ohlcv.last_close (P0-A.1 in tick_loop)
        if self.ohlcv is None:
            raise ValueError(
                "BarSnapshot.to_jsonl_row(legacy): ohlcv slot required "
                "to populate decision_price."
            )
        decision_price = float(self.ohlcv.last_close)

        semantic_packet_hashes = [
            (dict(v.metadata).get("packet_hash") if v.metadata else None)
            for v in sig.components
            if v.metadata and dict(v.metadata).get("packet_hash")
        ]
        committee_turns_hashes = [
            turn.get("input_hash")
            for turn in (
                (dict(sig.metadata).get("committee") or {}).get("model_backed_turns", [])
                if sig.metadata
                else []
            )
            if turn.get("input_hash")
        ]

        return {
            "schema_version": self.meta.schema_version,
            "id": self.meta.signal_id,
            "asof": _format_legacy_ts(self.asof_decision),
            "asset": self.symbol,
            "exchange": self.meta.exchange,
            "timeframe": self.meta.timeframe,
            "asset_class": self.meta.asset_class,
            "direction": sig.direction,
            "magnitude": float(sig.magnitude),
            "confidence": float(sig.confidence),
            "confidence_raw": float(sig.confidence_raw),
            "horizon": sig.horizon,
            "decision_price": decision_price,
            "target_position_pct": float(dec.target_position_pct),
            "reason": dec.reason,
            "halt": bool(dec.halt),
            "halt_scope": list(dec.halt_scope) if dec.halt_scope else None,
            "halt_until": (
                _format_legacy_ts(dec.halt_until) if dec.halt_until is not None else None
            ),
            "components": components,
            "aggregator": sig.aggregator,
            "metadata": meta_md,
            "semantic_packet_hashes": semantic_packet_hashes,
            "committee_turns_hashes": committee_turns_hashes,
        }

    # -- V2 typed shape --
    def _to_jsonl_row_v2(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
