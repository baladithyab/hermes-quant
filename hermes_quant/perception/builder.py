"""hermes_quant.perception.builder — the ONE ``build_perception_frame`` loader (PDR-1).

Does exactly what ``recommend``'s ``None`` branch does **up to ctx-build**
(``advisor.py:741-881``), then returns a ``PerceptionFrame``. It is the single
producer of the ``{semantic_packets, decision_asof}`` slice (absorbs
``catalyst/wiring.py:semantic_market_extras``), so the catalyst flag/wiring
decoupling (GAP-D) and the tool-vs-cron decoupling (M17) cannot recur — there is
ONE producer reached by both the tool and the cron paths.

**Byte-identity safeguard (recon §3.3 / plan §3.3):** the builder does NOT
re-resolve provider / recipe / as_of / lookback. The advisor already did all of
that *before* Step 4; it passes the *already-resolved* inputs in so the frame path
and the ``None`` path share the identical pre-fetch state. On any empty / no-data
outcome the builder returns ``None`` so the advisor's frame-branch falls back to
the ``None`` branch and produces the *same* ``_gated_no_data`` result.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from hermes_quant.perception.frame import PerceptionFrame
from hermes_quant.protocol import DataProviderError, DataQualityError, RateLimitError

logger = logging.getLogger(__name__)


def _tf_minutes(timeframe: str) -> int:
    """Mirror of ``advisor._tf_minutes`` (advisor.py:1034)."""
    return {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 24 * 60,
    }.get(timeframe, 60)


def build_perception_frame(
    symbol: str,
    *,
    timeframe: str,
    asset_class: str,
    provider: Any,  # already-resolved provider (advisor resolves it; builder does NOT re-resolve)
    asof_ts: pd.Timestamp,  # the normalized as_of (wall-clock-now or replay cutoff) — advisor passes it in
    lookback_bars: int,
    decision_asof: datetime | None = None,  # wall-clock now for live; explicit for backtests (ADR-0068/0074)
    base_extras: Mapping[str, Any] | None = None,
) -> PerceptionFrame | None:
    """Build the single ``PerceptionFrame`` for ``symbol`` at ``asof_ts``.

    Returns ``None`` when there are no usable bars (no data, empty after as_of
    filter, or all-dropped by the still-forming guard) — the caller then passes
    ``perception_frame=None`` so the advisor's ``None`` branch produces the
    identical ``_gated_no_data`` result (byte-identity on the degenerate paths).

    Mirrors ``advisor.py:749-881`` step-for-step (each step cites the verbatim
    advisor line). The semantic slice absorbs ``semantic_market_extras``
    (silence-by-default: OFF / no packets / any error → empty).
    """
    # ---- Step 1: fetch bars (advisor.py:750-801) ----
    # Lookback window (provider's start/end semantics; conservative — extra bars
    # are fine). Mirrors advisor.py:753-760 exactly.
    end = asof_ts
    if timeframe == "1d":
        start = end - pd.Timedelta(days=lookback_bars * 2)
    elif timeframe == "1h":
        start = end - pd.Timedelta(hours=lookback_bars * 3)
    else:
        start = end - pd.Timedelta(minutes=lookback_bars * 2 * _tf_minutes(timeframe))

    def _fetch_with_as_of():
        # Mirrors advisor.py:766-777 (the TypeError backwards-compat fallback).
        try:
            return provider.fetch_bars(symbol, timeframe, start, end, as_of=asof_ts)
        except TypeError as exc:
            if "as_of" in str(exc) or "unexpected keyword" in str(exc):
                return provider.fetch_bars(symbol, timeframe, start, end)
            raise

    try:
        bars = _fetch_with_as_of()
    except (RateLimitError, DataProviderError, DataQualityError) as exc:
        # The advisor's None branch surfaces these as distinct gated reasons. The
        # builder cannot construct a frame, so it returns None and lets the
        # advisor's None branch re-fetch and emit the correct gated result.
        logger.debug("build_perception_frame(%s): provider error: %s", symbol, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, like the advisor
        logger.warning(
            "build_perception_frame: unexpected provider failure for %s: %s",
            symbol,
            exc,
            exc_info=True,
        )
        return None

    # ---- Step 2: as_of filter (lookahead enforcement; advisor.py:803-821) ----
    if len(bars) == 0:
        return None

    if asof_ts is not None and "timestamp" in bars.columns:
        bar_ts = bars["timestamp"]
        if bar_ts.dt.tz is None:
            cutoff = asof_ts.tz_convert(None) if asof_ts.tzinfo else asof_ts
        else:
            cutoff = asof_ts
        bars = bars[bar_ts <= cutoff].copy()

    if len(bars) == 0:
        return None

    # ADR-0069: drop the still-forming last bar (advisor.py:823-834).
    from hermes_quant.data.bar_alignment import drop_still_forming_bar

    bars, _bar_alignment_info = drop_still_forming_bar(bars, timeframe, asset_class)
    if _bar_alignment_info["still_forming_dropped"] and len(bars) == 0:
        return None

    # ---- Step 3: data anchor (advisor.py:836-847) ----
    last_bar_ts = bars["timestamp"].iloc[-1]
    last_bar_ts = pd.Timestamp(last_bar_ts)
    if last_bar_ts.tzinfo is None:
        last_bar_ts_utc = last_bar_ts.tz_localize("UTC")
    else:
        last_bar_ts_utc = last_bar_ts.tz_convert("UTC")

    # ---- Step 4: build extras (advisor.py:849-869), but split regime out ----
    frame_extras: dict[str, Any] = dict(base_extras or {})
    # ADR-0069: surface dropped still-forming bar values (advisor.py:854-858).
    if _bar_alignment_info["still_forming_dropped"]:
        frame_extras["still_forming_close"] = _bar_alignment_info["still_forming_close"]
        frame_extras["still_forming_high"] = _bar_alignment_info["still_forming_high"]
        frame_extras["still_forming_low"] = _bar_alignment_info["still_forming_low"]
        frame_extras["still_forming_volume"] = _bar_alignment_info["still_forming_volume"]

    # Per ADR-0063: regime is canonical (advisor.py:859-869). Pull `regime` (the
    # RegimePacket object) into frame.regime; keep regime_failure /
    # regime_classifier_kind in frame.extras. The adapter re-expands `regime`.
    frame_regime: Any | None = None
    try:
        from hermes_quant.regime.extras_builder import build_regime_extras

        regime_extras = build_regime_extras(symbol, bars)
        frame_regime = regime_extras.get("regime")
        if "regime_failure" in regime_extras:
            frame_extras["regime_failure"] = regime_extras["regime_failure"]
        if "regime_classifier_kind" in regime_extras:
            frame_extras["regime_classifier_kind"] = regime_extras["regime_classifier_kind"]
    except Exception as exc:  # noqa: BLE001 — mirrors advisor.py:864-869 fallback
        logger.warning(
            "build_perception_frame: regime extras build failed for %s: %s",
            symbol,
            exc,
            exc_info=True,
        )
        frame_regime = None
        frame_extras.setdefault("regime_failure", f"extras_builder_error: {exc}")
        frame_extras.setdefault("regime_classifier_kind", "unavailable")

    # ---- Step 5: semantic slice (absorbs catalyst/wiring.py:semantic_market_extras) ----
    # Default ON (FLAGS.md Tier A); set HERMES_QUANT_SEMANTIC_ENABLED=0 to opt out.
    # Silence-by-default: explicitly-OFF / no packets / any error -> empty.
    # decision_asof defaults to wall-clock now (live path); pass explicit for
    # backtests (ADR-0068/0074). Mirrors wiring.semantic_market_extras exactly.
    semantic_packets: tuple[Any, ...] = ()
    import os

    if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "1") == "1":
        try:
            from hermes_quant.catalyst.synthesize import load_packets_for

            sem_asof = decision_asof or datetime.now(UTC)
            packets = load_packets_for(symbol, sem_asof, horizon=timeframe)
            if packets:
                semantic_packets = tuple(packets)
                frame_extras["decision_asof"] = sem_asof.isoformat()
        except Exception as exc:  # noqa: BLE001 — never block on packet loading
            # RR13: this branch is only reached when HERMES_QUANT_SEMANTIC_ENABLED=1,
            # i.e. the feature is ENABLED. A silent debug log would hide an
            # always-failing enabled feature; warn so it is visible in ops logs.
            # (The happy path emits nothing, so flag-OFF / no-error stays byte-identical.)
            logger.warning(
                "build_perception_frame(%s): semantic load failed (feature ENABLED): %s",
                symbol,
                exc,
            )

    # ---- Step 5b: PDR-2 TrendVelocity (GAP-A) — flag-gated, default-OFF ----
    # Mirrors the Step-5 flag idiom (builder.py:175). OFF -> stays None -> adapter
    # writes nothing (adapter.py:51) -> synthesize keeps severity -> byte-identical.
    # `os` is bound function-locally in Step 5 (builder.py:173, unconditionally
    # before this block); `datetime`/`UTC` are imported at module head.
    frame_trend_velocity: Mapping[str, Any] | None = None
    if os.environ.get("HERMES_QUANT_TREND_VELOCITY", "0") == "1":
        try:
            from hermes_quant.perception.velocity import (
                compute_trend_velocity,
                counts_per_period,
            )
            from hermes_quant.perception.velocity_source import (
                interest_timestamps_by_symbol,
            )

            vel_asof = decision_asof or datetime.now(UTC)
            ts_by_symbol = interest_timestamps_by_symbol(
                symbol, vel_asof, horizon=timeframe
            )
            scores: dict[str, Any] = {}
            for sym, tss in ts_by_symbol.items():
                counts = counts_per_period(tss, asof=vel_asof, freq="W")
                sc = compute_trend_velocity(counts, asof=vel_asof)
                if sc is not None:
                    scores[sym] = sc.to_mapping()
            if scores:
                frame_trend_velocity = scores
        except Exception as exc:  # noqa: BLE001 — never block frame build on velocity
            # RR13: only reached with HERMES_QUANT_TREND_VELOCITY=1 (feature ENABLED)
            # -> warn (not debug) so an always-failing enabled feature is visible.
            logger.warning(
                "build_perception_frame(%s): velocity build failed (feature ENABLED): %s",
                symbol,
                exc,
            )

    # ---- Step 5c: PDR-3 convergence evidence (HERMES_QUANT_CONVERGENCE) ----
    # Container-only: stamps frame.convergence for provenance/audit. The EMISSION
    # gate lives in synthesize.py (single-source packets were already dropped at
    # write time when the flag is ON); here we just record what the loaded packets
    # converged on. Silence-by-default: OFF / no packets / any error -> None.
    # Packets are DICTs (load_packets_for returns dicts), so use .get().
    frame_convergence: Mapping[str, Any] | None = None
    if semantic_packets and os.environ.get("HERMES_QUANT_CONVERGENCE", "0") == "1":
        try:
            from hermes_quant.perception.convergence import (
                CONVERGENCE_MIN_FAMILIES,
                source_family,
            )

            # packets carry their feed family in metadata.feed_source (synthesize.py)
            fams = sorted(
                {
                    source_family((p.get("metadata") or {}).get("feed_source", ""))
                    for p in semantic_packets
                }
                - {"unknown"}
            )
            frame_convergence = {
                "families": fams,
                "n_independent": len(fams),
                "validated": len(fams) >= CONVERGENCE_MIN_FAMILIES,
            }
        except Exception as exc:  # noqa: BLE001 — never block frame build
            # RR13: only reached with HERMES_QUANT_CONVERGENCE=1 + packets present
            # (feature ENABLED) -> warn so an always-failing enabled feature is visible.
            logger.warning(
                "build_perception_frame(%s): convergence stamp failed (feature ENABLED): %s",
                symbol,
                exc,
            )

    # ---- Step 6b: PDR-4 SaturationScore (ADR-0079 GAP-C) -- default-OFF ----
    # Flag read at CALL time (mirrors wiring.py:40). OFF -> saturation stays None
    # -> adapter writes NOTHING -> semantic view byte-identical (flag-OFF safety).
    frame_saturation: Mapping[str, Any] | None = None
    if os.environ.get("HERMES_QUANT_SATURATION", "0") == "1" and semantic_packets:
        try:
            from hermes_quant.perception.saturation import compute_saturation

            # Score the freshest packet (the one the analyst selects: semantic.py:194).
            # CRITICAL: frame.semantic_packets holds packet DICTS, not objects --
            # load_packets_for returns list[dict] (synthesize.py:166,198) and the
            # builder does `tuple(packets)` (builder.py:182). Use dict .get(), NOT
            # getattr(): getattr on a dict returns the default every time, which would
            # make saturation a SILENT no-op even with the flag ON (the basis would
            # always be "no_basis"). Verified against HEAD 2026-05-31.
            # ar-time-ordering: select the SAME freshest packet the analyst does
            # (HermesSemanticAnalyst._select_packet) — by PARSED asof, not a lexical
            # string compare. packet["asof"] is a producer-dependent string (synthesize
            # +00:00 vs model/human 'Z' or non-UTC offset); a string max() could pick a
            # STALE packet and score saturation off the wrong basis. Single-format input
            # stays byte-identical.
            from hermes_quant.semantic import packet_asof_key
            _pkt = max(semantic_packets, key=lambda p: packet_asof_key(p.get("asof", "")))
            _md = _pkt.get("metadata") or {}
            _cd = _md.get("confirm_date") if isinstance(_md, Mapping) else None
            # RR10: wire THIS symbol's velocity score (PDR-2) through when present, so
            # the asof-honest "velocity_peak" basis can engage. frame_trend_velocity is
            # keyed by symbol (scores[sym] = sc.to_mapping(), Step 5b); .get(symbol)
            # yields None when velocity is OFF / absent -> compute_saturation falls back
            # to packet_age, exactly as before. Still flag-gated + default-OFF.
            _tv = (
                frame_trend_velocity.get(symbol)
                if frame_trend_velocity is not None
                else None
            )
            frame_saturation = compute_saturation(
                packet_asof=_pkt.get("asof"),
                asof=last_bar_ts_utc,            # the bar-asof replay anchor (== frame.asof)
                trend_velocity=_tv,              # PDR-2 velocity score for `symbol`, or None
                confirm_date=_cd,
            )
        except Exception as exc:  # noqa: BLE001 -- never block frame build on saturation
            # RR13: only reached with HERMES_QUANT_SATURATION=1 + packets present
            # (feature ENABLED) -> warn so an always-failing enabled feature is visible.
            logger.warning(
                "build_perception_frame(%s): saturation failed (feature ENABLED): %s",
                symbol,
                exc,
            )
            frame_saturation = None

    # ---- Step 5d: ADR-0084 event_risk (HERMES_QUANT_CALENDAR_ENABLED) — default-OFF ----
    # Flag read at CALL time (mirrors Step 5/5b/5c/6b). OFF -> event_risk stays None
    # -> adapter writes NOTHING (adapter.py) -> default extras key-set preserved ->
    # byte-identical. The seam is asof-honest (a future-announced event is excluded)
    # and outcome-free (only scheduled_for/kind/impact ride). decision_asof defaults
    # to wall-clock now (live); explicit for backtests (ADR-0068/0074).
    frame_event_risk: Mapping[str, Any] | None = None
    if os.environ.get("HERMES_QUANT_CALENDAR_ENABLED", "0") == "1":
        try:
            from hermes_quant.catalyst.wiring import calendar_market_extras

            cal_asof = decision_asof or datetime.now(UTC)
            cal_extras = calendar_market_extras(symbol, decision_asof=cal_asof)
            if cal_extras is not None and cal_extras.get("event_risk") is not None:
                frame_event_risk = cal_extras["event_risk"]
        except Exception as exc:  # noqa: BLE001 — never block frame build on calendar
            # RR13: only reached with HERMES_QUANT_CALENDAR_ENABLED=1 (feature ENABLED)
            # -> warn so an always-failing enabled feature is visible in ops logs.
            logger.warning(
                "build_perception_frame(%s): event_risk build failed (feature ENABLED): %s",
                symbol,
                exc,
            )

    # ---- Step 6: last_close (advisor.py:877) ----
    last_close = float(bars["close"].iloc[-1])

    # ---- Step 7: PDR-1 leaves the future scores + provenance empty ----
    return PerceptionFrame(
        symbol=symbol,
        asof=last_bar_ts_utc,
        bars=bars,
        last_close=last_close,
        regime=frame_regime,
        semantic_packets=semantic_packets,
        trend_velocity=frame_trend_velocity,
        convergence=frame_convergence,
        saturation=frame_saturation,
        event_risk=frame_event_risk,
        provenance=(),
        extras=frame_extras,
    )


def build_perception_frame_live(
    symbol: str,
    *,
    asset_class: str = "equity",
    timeframe: str | None = None,
    lookback_bars: int | None = None,
    decision_asof: datetime | None = None,
    provider: Any = None,
    base_extras: Mapping[str, Any] | None = None,
) -> PerceptionFrame | None:
    """Resolve the live-path inputs (provider / asof / lookback) the SAME way the
    advisor's ``None`` branch does, then call :func:`build_perception_frame`.

    This is the ONE entry point the three live decision crons (daily-interim,
    autonomous-tick, playbook-tick) call so a single producer fills the frame
    (M17: tool + cron perceive the same frame). It NEVER raises — any failure
    returns ``None`` so the caller forwards ``perception_frame=None`` and the
    advisor's ``None`` branch produces today's exact result (byte-identical).

    Defaults mirror ``advisor.py:713-724,742-744``: timeframe per asset_class,
    lookback per timeframe, default provider per asset_class, asof = wall-clock
    now (UTC). ``decision_asof`` defaults to wall-clock now inside the builder
    (live semantics, ADR-0068/0074).
    """
    try:
        from hermes_quant.advisor import (
            _DEFAULT_LOOKBACK_BY_TF,
            _DEFAULT_TF_BY_ASSET_CLASS,
            _get_default_provider,
        )

        tf = timeframe or _DEFAULT_TF_BY_ASSET_CLASS.get(asset_class, "1d")
        lb = lookback_bars or _DEFAULT_LOOKBACK_BY_TF.get(tf, 200)
        if provider is None:
            provider = _get_default_provider(asset_class)
        asof_ts = pd.Timestamp.now(tz="UTC")
        return build_perception_frame(
            symbol,
            timeframe=tf,
            asset_class=asset_class,
            provider=provider,
            asof_ts=asof_ts,
            lookback_bars=lb,
            decision_asof=decision_asof,
            base_extras=base_extras,
        )
    except Exception as exc:  # noqa: BLE001 — never block a tick on frame building
        logger.debug("build_perception_frame_live(%s): %s", symbol, exc)
        return None


__all__ = ["build_perception_frame", "build_perception_frame_live"]
