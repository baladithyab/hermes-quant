"""hermes_quant.advisor — Synchronous chat-mode advisor surface (ADR-0014).

This is the second of hermes-quant's two operator surfaces (per ADR-0013):

  Surface 1 — Autopilot:  daemon -> JSONL bus -> freqtrade. Long-running.
  Surface 2 — Advisor:    `quant_recommend(symbol)` synchronous tool. <- THIS FILE

The advisor is the on-ramp for "anyone using Hermes" — install the plugin,
`pip install -e .[yfinance]`, ask Hermes "what does the system say about
AAPL?" and get a structured analyst+aggregator+risk-gated answer with
journal lessons. No daemon, no broker API, no portfolio state.

Contract (per ADR-0014):
- READ-ONLY: must not write state.db, signals.jsonl, or update calibrators.
- SYNCHRONOUS: returns within ~10s on default lookback. No async.
- DETERMINISTIC: same (symbol, as_of, indicators) -> same dict.
- SAFE on no-data: returns gated dict, never raises to caller.
- as_of-aware: optional timestamp filter for replay-mode queries.
- Single-symbol in v0.1.2 (multi-symbol post-portfolio rewrite, ADR-0011).
- No LLM in the chain (ADR-0012; LLMAnalyst lands v0.3.0).

Importantly, the advisor builds a SYNTHETIC FLAT PORTFOLIO for the risk
gate's evaluation. It is NOT reading any real broker state. The Kelly
fraction returned is the position size daemon-mode would HAVE TARGETED
given a clean slate; consumers must not interpret it as an order.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    AnalystView,
    DataProviderError,
    DataQualityError,
    HaltRecord,
    HaltState,
    MarketContext,
    MarketState,
    Portfolio,
    RateLimitError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lookback defaults per timeframe (bars)
# Conservative — must be large enough for SMA(50) + warm-up + stable ATR.
# ---------------------------------------------------------------------------

_DEFAULT_LOOKBACK_BY_TF = {
    "1m": 240,  # 4h
    "5m": 288,  # 1d
    "15m": 192,  # 2d
    "30m": 168,  # ~3.5d
    "1h": 200,  # ~8d
    "1d": 200,  # ~10mo
}

_DEFAULT_TF_BY_ASSET_CLASS = {
    "equity": "1d",
    "etf": "1d",
    "crypto": "1h",
    "fx": "1h",
}


# ---------------------------------------------------------------------------
# Conservative MarketState for advisor (per ADR-0014 §D2 "must not diverge
# from daemon"). When we don't have rolling fill stats yet, fall back to
# ADR-0009 §P1-12 conservative defaults. These match what the daemon uses
# in cold-start mode.
# ---------------------------------------------------------------------------

_BOOTSTRAP_VOL_BY_ASSET_CLASS = {
    "equity": 0.012,  # ~1.2% per-day stdev on equities
    "etf": 0.008,
    "crypto": 0.030,
    "fx": 0.005,
}

_BOOTSTRAP_COSTS_BY_ASSET_CLASS = {
    # (commission, spread, slippage) per side; round-trip = 2x
    "equity": (0.0005, 0.0003, 0.0005),
    "etf": (0.0003, 0.0002, 0.0003),
    "crypto": (0.0010, 0.0005, 0.0012),
    "fx": (0.0001, 0.0001, 0.0002),
}


# ---------------------------------------------------------------------------
# Empty halt state for the advisor (no real halt registry consulted)
# ---------------------------------------------------------------------------


class _EmptyHaltState:
    """No-op halt state — advisor has no awareness of real halt registry.

    The advisor surface deliberately operates on "as if no halts exist"
    semantics; if the operator wants halt-aware advice, they should consult
    `quant_status` separately. This keeps the advisor a pure stateless
    function of (symbol, as_of, lookback).
    """

    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool:
        return False

    def active_halts(self) -> list[HaltRecord]:
        return []


# ---------------------------------------------------------------------------
# Synthetic flat portfolio for the risk gate
# ---------------------------------------------------------------------------


def _synthetic_portfolio(
    asset: str, asset_class: str, asof: pd.Timestamp, equity: float = 100_000.0
) -> Portfolio:
    """Build a fresh flat portfolio so the risk gate has something to evaluate.

    The advisor explicitly does NOT read real broker state. The gate's
    `target_position_pct` output is informational — the position size that
    daemon-mode would target on a clean slate.
    """
    return Portfolio(
        account_id="advisor-synthetic",
        asset_class=asset_class,
        asof=asof,
        positions={},
        cash=equity,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=equity,
        daily_open_equity=equity,
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


@dataclass
class _AdvisorResult:
    """Internal accumulator. Converted to dict at end of recommend()."""

    symbol: str
    asset_class: str
    timeframe: str
    as_of: pd.Timestamp | None = None
    bars_received: int = 0
    gaps: list[str] = field(default_factory=list)
    last_bar_age_minutes: float | None = None
    analyst_views: list[dict[str, Any]] = field(default_factory=list)
    aggregated_signal: dict[str, Any] | None = None
    risk_gate: dict[str, Any] | None = None
    lessons: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    analyst_errors: list[str] = field(default_factory=list)
    data_provider_alive: bool = True
    recipe_id: str | None = None
    recipe_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "recipe": {
                "id": self.recipe_id,
                "config_hash": self.recipe_hash,
            }
            if self.recipe_id
            else None,
            "data_quality": {
                "bars_received": self.bars_received,
                "gaps": self.gaps,
                "last_bar_age_minutes": self.last_bar_age_minutes,
            },
            "analyst_views": self.analyst_views,
            "aggregated_signal": self.aggregated_signal,
            "risk_gate": self.risk_gate,
            "lessons": self.lessons,
            "caveats": self.caveats,
            "doctor": {
                "data_provider_alive": self.data_provider_alive,
                "analyst_errors": self.analyst_errors,
            },
        }


def _gated_no_data(result: _AdvisorResult, reason: str) -> dict[str, Any]:
    """Return shape per ADR-0014 §D3.4 "Safe under no-data scenarios."""
    result.aggregated_signal = None
    result.risk_gate = {
        "pass": False,
        "gated_reason": reason,
        "kelly_fraction": 0.0,
        "recommended_action": "gated",
    }
    if "Insufficient data for recommendation" not in result.caveats:
        result.caveats.append("Insufficient data for recommendation")
    return result.to_dict()


def _view_to_dict(view: AnalystView) -> dict[str, Any]:
    return {
        "analyst": view.analyst,
        "direction": int(view.direction),
        "magnitude": float(view.magnitude),
        "confidence": float(view.confidence),
        "confidence_raw": float(view.confidence_raw),
        "horizon": view.horizon,
        "rationale": view.rationale,
        "metadata": dict(view.metadata) if view.metadata else None,
    }


def _signal_to_dict(sig: AggregatedSignal) -> dict[str, Any]:
    return {
        "asset": sig.asset,
        "timeframe": sig.timeframe,
        "direction": int(sig.direction),
        "magnitude": float(sig.magnitude),
        "confidence": float(sig.confidence),
        "confidence_raw": float(sig.confidence_raw),
        "horizon": sig.horizon,
        "aggregator": sig.aggregator,
        "n_components": len(sig.components),
        "metadata": dict(sig.metadata) if sig.metadata else None,
    }


def _action_to_gate_dict(action: Action | None, signal: AggregatedSignal) -> dict[str, Any]:
    """Convert an Action (or silence) to the advisor's risk_gate sub-dict.

    Per ADR-0014 §D1: pass=True iff the gate emitted an Action with a
    non-zero target_position_pct (i.e., the signal would actually trade
    in daemon mode). Silence (None) and zero-target Actions both gate.
    """
    if action is None:
        return {
            "pass": False,
            "gated_reason": "silenced_by_gate",
            "kelly_fraction": 0.0,
            "recommended_action": "gated",
        }
    if action.target_position_pct == 0.0:
        # halt or flatten emit; treat as gated for advisor purposes
        return {
            "pass": False,
            "gated_reason": action.reason,
            "kelly_fraction": 0.0,
            "recommended_action": "gated_flatten",
        }
    direction_word = "long" if signal.direction > 0 else "short"
    return {
        "pass": True,
        "gated_reason": None,
        "kelly_fraction": float(action.target_position_pct),
        "recommended_action": f"{direction_word}_with_stop",
        "reason": action.reason,
    }


# ---------------------------------------------------------------------------
# Lazy provider construction
# ---------------------------------------------------------------------------


def _get_default_provider(asset_class: str):
    """Return a DataProvider for the asset class.

    v0.1.2: only yfinance for equity/etf is wired. Crypto/fx will arrive
    when ccxt provider lands (ADR-0005).
    """
    if asset_class in ("equity", "etf"):
        from hermes_quant.data.yfinance_provider import YFinanceProvider

        return YFinanceProvider()
    raise NotImplementedError(
        f"asset_class={asset_class!r} not supported in v0.1.2 advisor "
        f"(crypto/fx require ccxt provider — ADR-0005)"
    )


# ---------------------------------------------------------------------------
# Lazy lessons retrieval (ADR-0010 settlement journal — read path)
# ---------------------------------------------------------------------------


def _get_recent_lessons(symbol: str, n_same: int, n_cross: int) -> list[dict[str, Any]]:
    """Read recent journal lessons.

    v0.1.2 stub: ADR-0010 journal writer + reader are scheduled for the
    same release as this advisor. Until journal/reader.py lands, returns
    an empty list (advisor stays functional, lessons just unavailable).

    When journal.reader.get_recent_lessons exists, this delegates to it.
    """
    try:
        from hermes_quant.journal.reader import (  # type: ignore[import-not-found]
            get_recent_lessons,
        )
    except ImportError:
        return []
    try:
        return get_recent_lessons(symbol, n_same=n_same, n_cross=n_cross)
    except Exception as exc:
        logger.warning(
            "advisor: journal lesson retrieval failed (%s); returning empty lessons",
            exc,
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_default_analysts() -> list[Any]:
    """Return the canonical advisor analyst loadout (ADR-0018).

    Each optional dependency is wrapped so a missing dep degrades gracefully
    rather than crashing the advisor.
    """
    from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst

    analysts: list[Any] = [ClassicalTAAnalyst()]
    try:
        from hermes_quant.analysts.microstructure import MicrostructureLite

        analysts.append(MicrostructureLite())
    except ImportError:
        pass
    try:
        from hermes_quant.analysts.kronos import KronosAnalyst

        analysts.append(KronosAnalyst())
    except ImportError:
        pass
    # ADR-0064 (v0.6.1): equity FundamentalsAnalyst — default OFF until
    # the FundamentalsProvider parquet cache is prewarmed in production
    # (scripts/quant-fundamentals-prewarm-daily.py). Gate is read at
    # call time so tests / cron flips take effect immediately.
    if os.environ.get("HERMES_QUANT_FUNDAMENTALS_ENABLED", "0") == "1":
        try:
            from hermes_quant.analysts.fundamentals import FundamentalsAnalyst

            analysts.append(FundamentalsAnalyst())
        except ImportError:
            pass
    # ADR-0074: Catalyst Sense semantic analyst — default ON; set
    # HERMES_QUANT_SEMANTIC_ENABLED=0 to opt out. Promoted to default-ON
    # 2026-06-05 (FLAGS.md Tier A) after weeks live in .env=1 with no crash.
    # The negative-control + precision eval (hermes_quant.catalyst.eval) has
    # cleared. When enabled, it consumes SemanticPackets the advisor loads into
    # ctx.extras["semantic_packets"] (via catalyst.synthesize.load_packets_for)
    # and emits a PEER AnalystView into BMA — never an override. No-ops to an
    # abstain when no packet is present, so it is safe ON-by-default even before
    # full packet coverage. Gate read at call time (tests / cron flips take
    # effect immediately).
    if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "1") == "1":
        try:
            from hermes_quant.analysts.semantic import HermesSemanticAnalyst

            analysts.append(HermesSemanticAnalyst())
        except ImportError:
            pass

    # ADR-0089: OvernightDriftAnalyst — a zero-turnover conviction modulator on
    # hold-through-close daily positions. DEFAULT-OFF behind
    # HERMES_QUANT_OVERNIGHT_DRIFT (read at call time, mirroring the flags above).
    # With the flag absent the analyst is never constructed and the roster is
    # byte-identical to today. When enabled it emits a PEER AnalystView (the
    # trailing overnight-minus-intraday spread nudges the daily long thesis); it
    # never sizes, never proposes a round-trip, and is subject to the same BMA
    # dissent-aware capping as every analyst. Stays default-OFF until a real-data
    # flag-ablation clears the promote bar (ADR-0089 acceptance gate; the C2
    # EVENT_RISK HOLD is the precedent).
    if os.environ.get("HERMES_QUANT_OVERNIGHT_DRIFT", "0") == "1":
        try:
            from hermes_quant.analysts.overnight_drift import OvernightDriftAnalyst

            analysts.append(OvernightDriftAnalyst())
        except ImportError:
            pass

    # ADR-0080 / seed 908e (anti-overfit lane L3): DSR/walk-forward OOS ADMISSION
    # gate for analysts joining the committee. Factors clear this eval-gate before
    # any live weight; analysts previously joined with NO overfit check. DEFAULT-OFF
    # behind HERMES_QUANT_ANALYST_ADMISSION (read at call time, mirroring the flags
    # above + the factor proposer's boundary-only flag). With the flag OFF the
    # roster is byte-identical to today; with it ON, an analyst whose persisted
    # admission decision is not `admitted` (or that has no decision — fail-closed) is
    # dropped before it can vote. The gate module reads no env; the flag lives here.
    from hermes_quant.governance.analyst_admission import apply_admission_gate

    analysts = apply_admission_gate(
        analysts,
        enabled=os.environ.get("HERMES_QUANT_ANALYST_ADMISSION", "0") == "1",
    )
    return analysts


def _normalize_asof(as_of: str | pd.Timestamp | None) -> pd.Timestamp:
    if as_of is None:
        return pd.Timestamp.now(tz="UTC")
    if isinstance(as_of, str):
        ts = pd.Timestamp(as_of)
    else:
        ts = as_of
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def _fetch_bars_for_horizon(
    provider: Any,
    symbol: str,
    horizon: str,
    asof_ts: pd.Timestamp,
    horizons_in_set: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV bars for ``symbol`` at ``horizon``.

    For native provider timeframes ('1d', '1h', '5m', ...) this delegates
    directly to ``provider.fetch_bars``. For resampled multi-horizon timeframes
    ('1w', '1M', '1Q') this delegates to
    ``hermes_quant.data.horizon_cache.get_resampled_history`` which fetches
    daily bars once and resamples in-memory (ADR-0036).
    """
    # Resampled horizons go through the horizon cache so the daily fetch is
    # amortized across the full set.
    if horizon in ("1w", "1M", "1Q"):
        from hermes_quant.data.horizon_cache import get_resampled_history

        return get_resampled_history(
            symbol,
            horizon,
            asof=asof_ts,
            horizons_in_set=horizons_in_set,
            provider=provider,
        )

    # Native provider timeframe — same fetch path as single-horizon `recommend`
    lookback_bars = _DEFAULT_LOOKBACK_BY_TF.get(horizon, 200)
    end = asof_ts
    if horizon == "1d":
        start = end - pd.Timedelta(days=lookback_bars * 2)
    elif horizon == "1h":
        start = end - pd.Timedelta(hours=lookback_bars * 3)
    else:
        start = end - pd.Timedelta(minutes=lookback_bars * 2 * _tf_minutes(horizon))

    try:
        return provider.fetch_bars(symbol, horizon, start, end, as_of=asof_ts)
    except TypeError as exc:
        # Older provider without as_of kwarg
        if "as_of" in str(exc) or "unexpected keyword" in str(exc):
            return provider.fetch_bars(symbol, horizon, start, end)
        raise


def recommend_multi_horizon(
    symbol: str,
    *,
    horizons: Iterable[str] = ("1d", "1w"),
    asset_class: str = "equity",
    as_of: str | pd.Timestamp | None = None,
    provider: Any = None,
    analysts: list[Any] | None = None,
    market_extras: Mapping[str, Any] | None = None,
) -> list[AnalystView]:
    """Multi-timeframe analyst fan-out (ADR-0036).

    For each horizon in ``horizons``, build a MarketContext with that
    timeframe, run all registered analysts for ``asset_class``, and return
    the union of their AnalystView outputs. Each returned view's ``horizon``
    field is retagged to the horizon under which it was produced (the wrapper
    overrides the analyst's intrinsic horizon so downstream consumers — most
    importantly the BMA aggregator's cross-horizon weighting — see one view
    per (analyst, horizon) pair).

    Silence-by-default invariants (ADR-0036 §"Silence-by-default invariants
    preserved"):
      - If a horizon's analyst returns None, the view is skipped, NOT
        penalized.
      - If a horizon yields zero bars (provider failure / empty history),
        that horizon contributes zero views — no exception propagates.
      - If zero horizons produce views, the returned list is empty (the
        downstream aggregator will emit silence).

    Args:
        symbol: ticker / pair.
        horizons: ordered set of horizon labels (default ``("1d", "1w")``
            per ADR-0036). Duplicates are deduplicated while preserving order.
        asset_class: ``"equity"`` | ``"etf"`` | ``"crypto"`` | ``"fx"``.
        as_of: optional anchor timestamp for replay-mode queries.
        provider: dependency-injected DataProvider (defaults to yfinance for
            equity/etf).
        analysts: dependency-injected analyst list (defaults to the canonical
            advisor loadout).
        market_extras: optional provider-specific extras forwarded into
            MarketContext.extras.

    Returns:
        Flat list of AnalystView with up to N_analysts × N_horizons entries.

    Note:
        This is a wrapper-level fan-out — every existing analyst already
        consumes ``MarketContext.timeframe`` (per ADR-0002), so no analyst
        code changes are required.
    """
    # Dedupe horizons while preserving order
    horizons_list: list[str] = []
    for h in horizons:
        if h not in horizons_list:
            horizons_list.append(h)
    if not horizons_list:
        horizons_list = ["1d"]

    if provider is None:
        try:
            provider = _get_default_provider(asset_class)
        except NotImplementedError as exc:
            logger.warning(
                "recommend_multi_horizon: %s; returning empty view list", exc
            )
            return []

    if analysts is None:
        analysts = _build_default_analysts()

    asof_ts = _normalize_asof(as_of)
    all_views: list[AnalystView] = []

    for h in horizons_list:
        try:
            bars = _fetch_bars_for_horizon(
                provider, symbol, h, asof_ts, horizons_in_set=horizons_list
            )
        except (DataProviderError, DataQualityError, RateLimitError) as exc:
            logger.info(
                "recommend_multi_horizon: skipping %s @ %s — provider error: %s",
                symbol,
                h,
                exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — fail-soft per ADR-0036
            logger.warning(
                "recommend_multi_horizon: unexpected provider error for %s @ %s: %s",
                symbol,
                h,
                exc,
                exc_info=True,
            )
            continue

        if bars is None or len(bars) == 0:
            # Silence-by-default: no bars at this horizon → no views
            continue

        # Filter to as_of (defense-in-depth)
        if "timestamp" in bars.columns:
            bar_ts = bars["timestamp"]
            if hasattr(bar_ts, "dt"):
                if bar_ts.dt.tz is None:
                    cutoff = (
                        asof_ts.tz_convert(None) if asof_ts.tzinfo else asof_ts
                    )
                else:
                    cutoff = asof_ts
                bars = bars[bar_ts <= cutoff].copy()

        if len(bars) == 0:
            continue

        last_bar_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
        if last_bar_ts.tzinfo is None:
            last_bar_ts_utc = last_bar_ts.tz_localize("UTC")
        else:
            last_bar_ts_utc = last_bar_ts.tz_convert("UTC")

        ctx_extras_base = dict(market_extras or {})
        # Per ADR-0063: build canonical regime extras and merge OVER caller-supplied
        # values so callers cannot shadow the regime key. build_regime_extras never
        # raises (silence-by-default per ADR-0036); on classifier failure regime is
        # None with regime_failure populated.
        try:
            from hermes_quant.regime.extras_builder import build_regime_extras
            regime_extras = build_regime_extras(symbol, bars)
            ctx_extras_base.update(regime_extras)
        except Exception as exc:  # noqa: BLE001 — never block analyst loop
            logger.warning("recommend_multi_horizon: regime extras build failed for %s: %s",
                           symbol, exc, exc_info=True)
            ctx_extras_base.setdefault("regime", None)
            ctx_extras_base.setdefault("regime_failure", f"extras_builder_error: {exc}")
            ctx_extras_base.setdefault("regime_classifier_kind", "unavailable")

        ctx = MarketContext(
            asset=symbol,
            timeframe=h,
            asset_class=asset_class,
            exchange=None,
            bars=bars,
            last_close=float(bars["close"].iloc[-1]),
            last_volume=float(bars["volume"].iloc[-1]),
            asof=last_bar_ts_utc,
            extras=ctx_extras_base,
        )

        horizon_views: list[AnalystView] = []
        for analyst in analysts:
            analyst_name = getattr(analyst, "name", type(analyst).__name__)
            try:
                if hasattr(analyst, "analyze"):
                    view = analyst.analyze(ctx)
                elif hasattr(analyst, "observe"):
                    view = analyst.observe(ctx)
                else:
                    logger.warning(
                        "recommend_multi_horizon: %s has no analyze/observe", analyst_name
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 — one bad analyst can't kill fan-out
                logger.warning(
                    "recommend_multi_horizon: analyst %s raised at horizon %s: %s",
                    analyst_name,
                    h,
                    exc,
                    exc_info=True,
                )
                continue

            if view is None:
                continue

            # Retag horizon to the fan-out's horizon (ADR-0036). The analyst
            # may have hardcoded its own horizon in the view; for the BMA
            # cross-horizon weighting to apply correctly, the view must
            # carry the horizon under which it was produced.
            if view.horizon != h:
                view = dataclasses.replace(view, horizon=h)
            horizon_views.append(view)

        # Grounding enforcement (seed 24ba) — the SECOND advisor entry point.
        # recommend() enforces at its Step 5.5 seam; this fan-out is an equally
        # valid path into a downstream aggregator (ops/scripts/quant-playbook-tick.py),
        # so it must drop ungrounded grounded-views too — fail-CLOSED. Enforce
        # per-horizon against THIS horizon's ctx (carries the same extras incl.
        # ground_truth_block). ADDITIVE: no block / no grounding marker / flag-OFF
        # -> identity passthrough, byte-identical to today.
        from hermes_quant.grounding.enforcement import enforce_grounding

        horizon_views, _dropped = enforce_grounding(horizon_views, ctx)
        if _dropped:
            logger.info(
                "recommend_multi_horizon: grounding enforcement dropped %d "
                "ungrounded view(s) at horizon %s: %s",
                len(_dropped),
                h,
                ", ".join(sorted({r["analyst"] for r in _dropped})),
            )
        all_views.extend(horizon_views)

    return all_views


def recommend(
    symbol: str,
    *,
    asset_class: str | None = None,
    timeframe: str | None = None,
    lookback_bars: int | None = None,
    include_lessons: bool = True,
    n_lessons_same: int = 3,
    n_lessons_cross: int = 2,
    as_of: str | pd.Timestamp | None = None,
    provider: Any = None,
    analysts: list[Any] | None = None,
    aggregator: Any = None,
    risk_gate: Any = None,
    recipe: Any = None,
    recipe_id: str | None = None,
    market_extras: Mapping[str, Any] | None = None,
    perception_frame: Any = None,
) -> dict[str, Any]:
    """Synchronous recommendation for a single symbol. Read-only (ADR-0014).

    .. note::
        This is the legacy single-horizon entry point and remains the
        canonical advisor surface. For multi-timeframe analysis, use
        :func:`recommend_multi_horizon` directly. ADR-0036 §"Daily-cadence
        implications" specifies that the daily playbook tick will eventually
        opt into multi-horizon fan-out via the ``HERMES_QUANT_HORIZONS``
        environment variable — that wire-up is **Wave C** and intentionally
        deferred from this commit. See ADR-0036 for the migration plan.

    Args:
        symbol: ticker / pair (e.g., "AAPL", "SPY").
        asset_class: one of {"equity","etf","crypto","fx"}. Default "equity".
        timeframe: bar timeframe; defaults per asset_class (equity/etf=1d,
            crypto/fx=1h).
        lookback_bars: how many bars of history to fetch; defaults per
            timeframe.
        include_lessons: pull recent journal entries for this symbol +
            cross-symbol context. Default True. ADR-0014 §D7 advisor MAY
            be invoked with lessons disabled to save tokens.
        n_lessons_same / n_lessons_cross: lesson budget if include_lessons.
        as_of: optional ISO timestamp or pd.Timestamp to anchor "now".
            When set, bars are filtered to `<= as_of` (lookahead enforcement
            per ADR-0005 amendment). For backtest-mode replay queries.
        provider / analysts / aggregator / risk_gate: dependency injection
            for tests. All default to canonical implementations when None.
        perception_frame: optional ADR-0079 PDR-1 PerceptionFrame built ONCE
            upstream by build_perception_frame(). When None (the default, and
            every backtest), the ctx is built internally — byte-identical to
            today. When provided, the frame is projected into MarketContext via
            the pure frame_to_context adapter (Steps 5-8 are identical on both
            branches; M06 ADMIT-before-GATE ordering preserved). When BOTH
            perception_frame and market_extras are passed, the frame wins (it
            already absorbed the semantic slice) and a caveat is appended.

    Returns:
        Structured dict per ADR-0014 §D1 return-shape table. Never raises
        for caller-visible errors — exceptions are caught and surfaced via
        the `doctor.analyst_errors` and `caveats` fields.

    Raises:
        Nothing user-visible. Internally, DataProviderError on hard
        provider failure becomes a gated dict with caveat; same for
        DataQualityError. Programming bugs (TypeError, etc.) propagate.
    """
    # Recipe selection happens before defaults so recipe timeframe/asset_class
    # can drive the hot path. Existing callers that pass explicit arguments keep
    # precedence. Dependency-injected analysts/aggregator/risk_gate also keep
    # precedence for tests and backtest seeded posteriors.
    active_recipe = recipe
    if active_recipe is None and recipe_id is not None:
        from hermes_quant.recipes import get_recipe

        active_recipe = get_recipe(recipe_id)
    if active_recipe is not None:
        active_recipe.validate()
        asset_class = asset_class or active_recipe.asset_class
        timeframe = timeframe or active_recipe.timeframe
        if analysts is None:
            from hermes_quant.recipes import instantiate_recipe_analysts

            analysts = instantiate_recipe_analysts(active_recipe)
        if aggregator is None:
            from hermes_quant.recipes import instantiate_recipe_aggregator

            aggregator = instantiate_recipe_aggregator(active_recipe)
        if risk_gate is None:
            from hermes_quant.recipes import instantiate_recipe_risk_gate

            risk_gate = instantiate_recipe_risk_gate(active_recipe)

    asset_class = asset_class or "equity"
    timeframe = timeframe or _DEFAULT_TF_BY_ASSET_CLASS.get(asset_class, "1d")
    lookback_bars = lookback_bars or _DEFAULT_LOOKBACK_BY_TF.get(timeframe, 200)

    # Normalize as_of
    asof_ts: pd.Timestamp | None
    if as_of is None:
        asof_ts = pd.Timestamp.now(tz="UTC")
    elif isinstance(as_of, str):
        asof_ts = pd.Timestamp(as_of)
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
    else:
        asof_ts = as_of
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")

    result = _AdvisorResult(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        recipe_id=getattr(active_recipe, "id", None),
        recipe_hash=getattr(active_recipe, "config_hash", None),
    )
    result.caveats.append("Snapshot-in-time view; not a guaranteed forecast")
    result.caveats.append("No portfolio risk context (single-symbol view, ADR-0014 v0.1.2)")
    result.caveats.append("Calibration not updated from this read")

    # ---- Steps 1-4: build the MarketContext ----
    # ADR-0079 PDR-1: when a PerceptionFrame is handed in (built ONCE upstream by
    # build_perception_frame), just PROJECT it — the frame already did the
    # fetch -> as_of filter -> still-forming drop -> regime/semantic build. When
    # no frame is passed (the default, and every backtest), build ctx internally
    # below — byte-identical to today. A None frame is identical to not passing
    # one (the simplest contract: crons always build a frame and forward the
    # possibly-None result without branching).
    if perception_frame is not None:
        # Frame-wins precedence (recon §3.3): the frame already absorbed the
        # semantic slice, so a separately-passed market_extras is ignored.
        # Silence-by-default posture — caveat, never raise.
        if market_extras is not None:
            result.caveats.append("market_extras ignored: perception_frame present")
        from hermes_quant.perception.adapter import frame_to_context

        ctx = frame_to_context(
            perception_frame, timeframe=timeframe, asset_class=asset_class
        )
        last_bar_ts_utc = perception_frame.asof  # bar-asof = replay anchor (== :856)
        result.as_of = last_bar_ts_utc
        result.bars_received = len(perception_frame.bars)
        # last_bar_age_minutes is wall-clock-derived and excluded from the
        # replay-compared keys; derive it identically to the None branch so any
        # consumer that reads it sees the same value.
        if asof_ts is not None:
            age_seconds = (asof_ts - last_bar_ts_utc).total_seconds()
            result.last_bar_age_minutes = max(0.0, age_seconds / 60.0)
    else:
        # ---- Step 1: fetch bars ----
        if provider is None:
            try:
                provider = _get_default_provider(asset_class)
            except NotImplementedError as exc:
                result.caveats.append(str(exc))
                result.data_provider_alive = False
                return _gated_no_data(result, "asset_class_unsupported")

        # Lookback window (provider's start/end semantics; the per-tf delta
        # is conservative — extra bars are fine, validate_bars trims to the
        # requested window if needed).
        end = asof_ts
        if timeframe == "1d":
            start = end - pd.Timedelta(days=lookback_bars * 2)
        elif timeframe == "1h":
            start = end - pd.Timedelta(hours=lookback_bars * 3)
        else:
            # intraday — yfinance has tight lookback windows; widen by 2x
            start = end - pd.Timedelta(minutes=lookback_bars * 2 * _tf_minutes(timeframe))

        # Pass `as_of` to the provider for leaf-level lookahead enforcement
        # (ADR-0005 amendment, Wave C.1). The provider filters bars before
        # returning; the redundant filter below is kept for fallback safety
        # in case a custom provider doesn't honor as_of.
        def _fetch_with_as_of():
            try:
                return provider.fetch_bars(symbol, timeframe, start, end, as_of=asof_ts)
            except TypeError as exc:
                # Backwards-compat: older providers (or test doubles) without
                # the as_of kwarg. Only swallow TypeError when the message
                # matches a kwarg-related signature mismatch — otherwise
                # propagate (a provider that genuinely raises TypeError from
                # its body should not be silently retried).
                if "as_of" in str(exc) or "unexpected keyword" in str(exc):
                    return provider.fetch_bars(symbol, timeframe, start, end)
                raise

        try:
            bars = _fetch_with_as_of()
        except RateLimitError as exc:
            result.caveats.append(f"Provider rate-limited: {exc}")
            result.data_provider_alive = False
            return _gated_no_data(result, "rate_limited")
        except DataProviderError as exc:
            result.caveats.append(f"Data provider error: {exc}")
            result.data_provider_alive = False
            return _gated_no_data(result, "data_provider_error")
        except DataQualityError as exc:
            result.caveats.append(f"Data quality error: {exc}")
            return _gated_no_data(result, "data_quality_error")
        except Exception as exc:  # noqa: BLE001 — advisor degrades gracefully
            result.caveats.append(f"Unexpected provider error: {exc}")
            result.data_provider_alive = False
            logger.warning(
                "advisor: unexpected provider failure for %s: %s",
                symbol,
                exc,
                exc_info=True,
            )
            return _gated_no_data(result, "unexpected_provider_error")

        # ---- Step 2: as_of filter (lookahead enforcement) ----
        # Cheap empty-check first — empty-bars DataFrame from a test fixture
        # may have no datetime dtype on the timestamp column, which would
        # crash the `.dt.tz` access below.
        if len(bars) == 0:
            return _gated_no_data(result, "no_bars_returned")

        if asof_ts is not None and "timestamp" in bars.columns:
            # Compare-safe even when bars timestamps are tz-naive
            bar_ts = bars["timestamp"]
            if bar_ts.dt.tz is None:
                cutoff = asof_ts.tz_convert(None) if asof_ts.tzinfo else asof_ts
            else:
                cutoff = asof_ts
            bars = bars[bar_ts <= cutoff].copy()

        result.bars_received = len(bars)
        if result.bars_received == 0:
            return _gated_no_data(result, "no_bars_returned")

        # ADR-0069: drop the still-forming last bar for daily-timeframe equity reads
        # mid-session. yfinance returns today's intraday-still-forming bar with a
        # close that's the latest tick — not a settled bar close. Reading that as
        # `last_close` breaks replay equality, calibration distributions, and
        # downstream slippage attribution. Surface the dropped values in extras
        # for analysts that explicitly opt in.
        from hermes_quant.data.bar_alignment import drop_still_forming_bar
        bars, _bar_alignment_info = drop_still_forming_bar(bars, timeframe, asset_class)
        if _bar_alignment_info["still_forming_dropped"]:
            result.bars_received = len(bars)
            if result.bars_received == 0:
                return _gated_no_data(result, "no_bars_after_still_forming_drop")

        # ---- Step 3: data-quality probe ----
        # validate_bars normalizes to tz-NAIVE UTC (per data/base.py L75). Convert
        # back to tz-aware UTC for arithmetic with asof_ts (which is tz-aware).
        last_bar_ts = bars["timestamp"].iloc[-1]
        last_bar_ts = pd.Timestamp(last_bar_ts)
        if last_bar_ts.tzinfo is None:
            last_bar_ts_utc = last_bar_ts.tz_localize("UTC")
        else:
            last_bar_ts_utc = last_bar_ts.tz_convert("UTC")
        age_seconds = (asof_ts - last_bar_ts_utc).total_seconds()
        result.last_bar_age_minutes = max(0.0, age_seconds / 60.0)
        result.as_of = last_bar_ts_utc  # actual data anchor, not wall clock

        # ---- Step 4: build MarketContext ----
        ctx_extras_base = dict(market_extras or {})
        # ADR-0069: surface dropped still-forming bar values so analysts that
        # genuinely want today's intraday tick can read them explicitly via extras.
        # Default consumers see clean settled-bar `last_close`.
        if _bar_alignment_info["still_forming_dropped"]:
            ctx_extras_base["still_forming_close"] = _bar_alignment_info["still_forming_close"]
            ctx_extras_base["still_forming_high"] = _bar_alignment_info["still_forming_high"]
            ctx_extras_base["still_forming_low"] = _bar_alignment_info["still_forming_low"]
            ctx_extras_base["still_forming_volume"] = _bar_alignment_info["still_forming_volume"]
        # Per ADR-0063: regime is canonical; merge OVER caller values
        try:
            from hermes_quant.regime.extras_builder import build_regime_extras
            regime_extras = build_regime_extras(symbol, bars)
            ctx_extras_base.update(regime_extras)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommend: regime extras build failed for %s: %s",
                           symbol, exc, exc_info=True)
            ctx_extras_base.setdefault("regime", None)
            ctx_extras_base.setdefault("regime_failure", f"extras_builder_error: {exc}")
            ctx_extras_base.setdefault("regime_classifier_kind", "unavailable")

        ctx = MarketContext(
            asset=symbol,
            timeframe=timeframe,
            asset_class=asset_class,
            exchange=None,  # yfinance equity has no notion of exchange
            bars=bars,
            last_close=float(bars["close"].iloc[-1]),
            last_volume=float(bars["volume"].iloc[-1]),
            asof=last_bar_ts_utc,
            extras=ctx_extras_base,
        )

    # ---- Step 5: run analysts ----
    if analysts is None:
        # Per charter §"What I'd build first" + ADR-0018:
        # The canonical hermes-quant committee is THREE analysts:
        #   1. ClassicalTAAnalyst (always available)
        #   2. MicrostructureLite (always available; OHLCV-only proxies)
        #   3. KronosAnalyst (lazy-load gated; abstains if `kronos`
        #      package not installed — BMA filters abstainers per
        #      ADR-0018 §D4)
        # Each import is wrapped so a missing optional dependency
        # degrades gracefully rather than crashing the advisor.
        from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst

        analysts = [ClassicalTAAnalyst()]
        try:
            from hermes_quant.analysts.microstructure import MicrostructureLite

            analysts.append(MicrostructureLite())
        except ImportError:
            pass
        try:
            from hermes_quant.analysts.kronos import KronosAnalyst

            analysts.append(KronosAnalyst())
        except ImportError:
            # KronosAnalyst class import failed (shouldn't happen since
            # the module is in our own package), but defensive fallback
            pass
        # ADR-0064 (v0.6.1): equity FundamentalsAnalyst — default OFF.
        # See _build_default_analysts() comment for context.
        if os.environ.get("HERMES_QUANT_FUNDAMENTALS_ENABLED", "0") == "1":
            try:
                from hermes_quant.analysts.fundamentals import FundamentalsAnalyst

                analysts.append(FundamentalsAnalyst())
            except ImportError:
                pass
        # ADR-0074: Catalyst Sense semantic analyst — default ON; set
        # HERMES_QUANT_SEMANTIC_ENABLED=0 to opt out.
        # See _build_default_analysts() comment for context.
        if os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "1") == "1":
            try:
                from hermes_quant.analysts.semantic import HermesSemanticAnalyst

                analysts.append(HermesSemanticAnalyst())
            except ImportError:
                pass

    views: list[AnalystView] = []
    for analyst in analysts:
        analyst_name = getattr(analyst, "name", type(analyst).__name__)
        try:
            # Per ADR-0002, the canonical method is `analyze`. Some
            # analysts may expose `observe` as an alias — try analyze first.
            if hasattr(analyst, "analyze"):
                view = analyst.analyze(ctx)
            elif hasattr(analyst, "observe"):
                view = analyst.observe(ctx)
            else:
                result.analyst_errors.append(f"{analyst_name}: no analyze/observe method")
                continue
        except Exception as exc:  # noqa: BLE001 — one bad analyst can't kill advisor
            result.analyst_errors.append(f"{analyst_name}: {exc}")
            logger.warning(
                "advisor: analyst %s raised: %s",
                analyst_name,
                exc,
                exc_info=True,
            )
            continue
        if view is None:
            continue
        views.append(view)
        result.analyst_views.append(_view_to_dict(view))

    # ---- Step 5.5: grounding enforcement (seed 24ba) ----
    # Wire ClaimVerifier into the decision path at the views -> aggregator seam.
    # A grounded analyst view (one that opted into the GroundTruthBlock via
    # ctx.extras['ground_truth_block']) whose numeric claims fail citation
    # verification is DROPPED from the vote — fail-CLOSED toward NOT trading on
    # ungrounded/hallucinated numerics. ADDITIVE: when no block is present (the
    # default advisor path), or enforcement is unset/off, this is identity
    # passthrough and byte-identical to today. The dropped views remain visible
    # in result.analyst_views (marked grounding_dropped) so the audit trail still
    # records WHY a view didn't reach the aggregator.
    from hermes_quant.grounding.enforcement import enforce_grounding

    _views_before = views
    views, _grounding_dropped = enforce_grounding(views, ctx)
    if _grounding_dropped:
        # Annotate the analyst_views entries for the views that were dropped so
        # the audit trail records the dropped contribution and WHY. result.
        # analyst_views was appended in lockstep with _views_before, so match by
        # POSITION using object identity (kept views are the SAME objects). This
        # is robust to two analysts sharing a name where only one view is dropped
        # (a name-keyed match would mis-stamp the kept same-named view).
        _kept_ids = {id(v) for v in views}
        _dropped_iter = iter(_grounding_dropped)
        for _orig_view, _vd in zip(_views_before, result.analyst_views):
            if id(_orig_view) not in _kept_ids:
                _rec = next(_dropped_iter, None)
                if _rec is not None:
                    _vd["grounding_dropped"] = True
                    _vd["grounding_reason"] = _rec.get("reason")
                    _vd["grounding_uncited_claims"] = _rec.get("uncited_claims")
        _dropped_names = ", ".join(sorted({r["analyst"] for r in _grounding_dropped}))
        result.caveats.append(
            f"Grounding enforcement dropped {len(_grounding_dropped)} ungrounded "
            f"analyst view(s) from the vote: {_dropped_names}"
        )

    if not views:
        # Either every analyst declined to emit (cold-start, insufficient
        # bars), every analyst raised, or every emitted view was dropped by
        # grounding enforcement (all numerics ungrounded). Either way, no
        # signal to gate — fail-closed.
        if _grounding_dropped:
            return _gated_no_data(result, "all_views_dropped_ungrounded")
        return _gated_no_data(
            result,
            "no_analyst_views" if not result.analyst_errors else "all_analysts_errored",
        )

    # ---- Step 6: aggregate ----
    if aggregator is None:
        from hermes_quant.aggregators.bma import BMAAggregator

        aggregator = BMAAggregator()

    try:
        agg_signal = aggregator.aggregate(views, ctx)
    except Exception as exc:  # noqa: BLE001
        result.analyst_errors.append(f"aggregator: {exc}")
        logger.warning("advisor: aggregator raised: %s", exc, exc_info=True)
        return _gated_no_data(result, "aggregator_error")

    # ---- Step 6.5: ADR-0084 event-risk carrier (DEFAULT-OFF, ADDITIVE) ----
    # The 743b pre-event guard (risk/gate.py Rule 3.5) reads the asof-honest,
    # outcome-free event-risk payload from `signal.metadata['event_risk']`, but
    # the calendar wiring stamps it onto `ctx.extras['event_risk']`. Nothing
    # copies it across the aggregator seam — so the guard never fired. This
    # one-shot copy bridges that gap.
    #
    # RAILS (ADR-0084 D-1/D-3): fully gated on HERMES_QUANT_EVENT_RISK (read at
    # call time). Flag absent => no metadata key is copied => the 743b guard
    # never fires => behavior is byte-identical to today. The copy only ever
    # ADDS the read-only advisory key; it never touches the ladder, sizing, or
    # the gate logic itself. The payload was already filtered upstream to
    # `announced_at <= decision_asof` (asof-honest by construction).
    agg_signal = _carry_event_risk(agg_signal, ctx)

    result.aggregated_signal = _signal_to_dict(agg_signal)

    # ---- Step 7: risk gate ----
    if risk_gate is None:
        from hermes_quant.risk.gate import DefaultRiskGate

        risk_gate = DefaultRiskGate()

    market = _bootstrap_market_state(symbol, asset_class, last_bar_ts_utc)
    portfolio = _synthetic_portfolio(symbol, asset_class, last_bar_ts_utc)
    halt_state: HaltState = _EmptyHaltState()  # type: ignore[assignment]

    try:
        action = risk_gate.gate(agg_signal, market, portfolio, halt_state)
    except Exception as exc:  # noqa: BLE001
        result.analyst_errors.append(f"risk_gate: {exc}")
        logger.warning("advisor: risk gate raised: %s", exc, exc_info=True)
        return _gated_no_data(result, "risk_gate_error")

    result.risk_gate = _action_to_gate_dict(action, agg_signal)

    # ---- Step 8: lessons (optional) ----
    if include_lessons:
        try:
            result.lessons = _get_recent_lessons(symbol, n_lessons_same, n_lessons_cross)
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisor: lesson retrieval failed: %s", exc, exc_info=True)

    # Top-level decision_price + signal_id for downstream consumers
    # (Reactor adapters need decision_price; settlement loop needs signal_id).
    # Per ADR-0014 amendment 2026-05-13 (Wave B.1), advisor exposes these
    # as top-level fields rather than burying them in analyst_views[0].metadata.
    final = result.to_dict()
    final["decision_price"] = float(ctx.last_close)
    # Per ADR-0068: split bar-time (replay anchor) from decision wall-clock.
    # `as_of` continues to mean the bar boundary (preserves replay equality).
    # `bar_ts` is the new explicit alias (same value, named-honestly).
    # `decision_wall_clock` is the wall-clock UTC at signal-emit time —
    # what an outside observer would write down as "the model decided X at
    # this moment." Downstream consumers that need decision-time honesty
    # (audit trail, slippage attribution, holding-period math) should
    # prefer `decision_wall_clock`; consumers that need replay-stable bar
    # identity should prefer `bar_ts`. Schema bump tracked separately.
    final["bar_ts"] = result.as_of.isoformat() if result.as_of is not None else None
    final["decision_wall_clock"] = datetime.now(timezone.utc).isoformat()
    # signal_id is the proposal_id-equivalent for the daemon side; advisor
    # itself doesn't emit one, so this stays None until daemon-mode integrates.
    final["signal_id"] = None
    return final


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ADR-0084 event-risk master flag (mirrors risk/gate.py + options_gate.py:
# read at call time, never cached at import). Absent/"0" => no carry => the
# pre-event guard never fires => byte-identical to today.
_EVENT_RISK_FLAG = "HERMES_QUANT_EVENT_RISK"


def _carry_event_risk(signal: AggregatedSignal, ctx: MarketContext) -> AggregatedSignal:
    """One-shot copy of ``ctx.extras['event_risk']`` onto the aggregated
    signal's ``metadata['event_risk']`` (ADR-0084 carrier).

    The 743b pre-event guard (risk/gate.py) reads its asof-honest, outcome-free
    event-risk payload from ``signal.metadata['event_risk']``, but the calendar
    wiring stamps it onto ``ctx.extras['event_risk']``. This bridges that seam.

    DEFAULT-OFF + ADDITIVE: returns ``signal`` UNCHANGED unless
    ``HERMES_QUANT_EVENT_RISK=1`` (read at call time) AND ``ctx.extras`` carries
    a non-None ``event_risk`` value. When OFF (or the carrier value is absent)
    NO metadata key is added — the returned object is the same signal, so the
    persisted dict and the gated signal are byte-identical to today. The copy
    only ADDS the read-only advisory key; it never touches the ladder, sizing,
    or the gate logic (ADR-0084 D-1/D-3). Pure; never raises.
    """
    if os.environ.get(_EVENT_RISK_FLAG, "0") != "1":
        return signal
    extras = getattr(ctx, "extras", None) or {}
    event_risk = extras.get("event_risk")
    if event_risk is None:
        return signal
    new_metadata = {**(signal.metadata or {}), "event_risk": event_risk}
    return dataclasses.replace(signal, metadata=new_metadata)


def _tf_minutes(timeframe: str) -> int:
    return {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 24 * 60,
    }.get(timeframe, 60)


def _bootstrap_market_state(symbol: str, asset_class: str, asof: pd.Timestamp) -> MarketState:
    """Conservative MarketState for advisor mode (no rolling fill stats yet).

    Per ADR-0009 §P1-12 cold-start: use safe defaults until the daemon has
    accumulated 30 days of real fills. The advisor never accumulates fills,
    so it always uses bootstrap.
    """
    vol = _BOOTSTRAP_VOL_BY_ASSET_CLASS.get(asset_class, 0.015)
    commission, spread, slippage = _BOOTSTRAP_COSTS_BY_ASSET_CLASS.get(
        asset_class, (0.0005, 0.0003, 0.0007)
    )
    return MarketState(
        asset=symbol,
        asof=asof,
        volatility=vol,
        commission=commission * 2,  # round-trip
        spread=spread * 2,
        slippage_estimate=slippage * 2,
        funding_cost=0.0,
        borrow_cost=0.0,
        tz="UTC",
    )
