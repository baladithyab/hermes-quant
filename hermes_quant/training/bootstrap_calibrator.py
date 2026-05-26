"""hermes_quant.training.bootstrap_calibrator — fit IsotonicCalibrator from history.

Replays the live advisor pipeline (ClassicalTAAnalyst + MicrostructureLite,
optionally KronosAnalyst) against ~1y of historical Alpaca paper bars,
pairs each emitted view's raw_confidence with the realized direction-correct
outcome at t+horizon_bars, and fits an IsotonicCalibrator over the aggregated
(raw, correct) pairs. The fitted calibrator is pickled to disk where
BMAAggregator can load it on next init.

Posture:
- READ-ONLY against Alpaca. paper=True. NO order paths anywhere.
- Silence-by-default: any analyst exception → log + skip the bar (not the run).
- Reproducibility: deterministic numpy seed; bar windows are time-ordered.

Why this exists (the deadlock it breaks):
- ColdStartCalibrator (Beta(2,5)) caps calibrated confidence at 0.375.
- The risk gate's signed-edge formula `prob*log(1+mag) + (1-prob)*log(1-mag)`
  is negative at prob=0.375 even at mag=0.025, so every cold-start signal
  silences via cost_gate_edge_sign.
- Without trading, settled outcomes never accrue, so the IsotonicCalibrator
  never reaches its N>=200 threshold.
- This module breaks the deadlock by replaying historical bars to synthesize
  the (raw_confidence, realized_correct) pairs the calibrator needs.

ADR refs: ADR-0009 §P0-2 (calibration), ADR-0019 (evaluation discipline),
docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md.
"""

from __future__ import annotations

import logging
import os
import pickle
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.calibrators import IsotonicCalibrator
from hermes_quant.protocol import AnalystView, MarketContext

logger = logging.getLogger(__name__)


# Default canonical persistence location. BMAAggregator reads from the same
# path on init.
DEFAULT_CALIBRATOR_PATH = Path.home() / ".hermes" / "quant" / "calibrators" / "isotonic.pkl"

# Minimum bars an analyst needs before it will emit. ClassicalTAAnalyst's
# slowest indicator is the SMA50 (50 bars); we use 200 to give all analysts
# headroom and keep the warm-up consistent with live-mode behavior.
_MIN_CONTEXT_BARS = 200


def _get_credentials() -> tuple[str, str]:
    """Return (key, secret) from env. Mirrors universe.alpaca_scanner pattern."""
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET (or ..._KEY_ID/..._SECRET_KEY) required"
        )
    return key, secret


def _build_data_client():
    """Lazy-import alpaca StockHistoricalDataClient (paper-compatible IEX feed)."""
    from alpaca.data.historical.stock import StockHistoricalDataClient

    key, secret = _get_credentials()
    return StockHistoricalDataClient(api_key=key, secret_key=secret)


def _fetch_bars(client: Any, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Pull daily bars for a single symbol over [start, end).

    Returns a DataFrame with columns ['timestamp','open','high','low','close','volume'],
    timestamp in UTC, ascending, deduplicated. Empty DataFrame on any error
    (silence-by-default).
    """
    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        resp = client.get_stock_bars(req)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alpaca fetch failed for %s: %s", symbol, exc)
        return pd.DataFrame()

    data = getattr(resp, "data", None)
    bars = (data or {}).get(symbol) if data is not None else None
    if not bars:
        # Some SDK versions support indexing
        try:
            bars = list(resp[symbol])  # type: ignore[index]
        except Exception:
            return pd.DataFrame()

    rows = []
    for b in bars:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        rows.append(
            {
                "timestamp": pd.Timestamp(ts).tz_convert("UTC")
                if pd.Timestamp(ts).tzinfo
                else pd.Timestamp(ts).tz_localize("UTC"),
                "open": float(getattr(b, "open", float("nan")) or float("nan")),
                "high": float(getattr(b, "high", float("nan")) or float("nan")),
                "low": float(getattr(b, "low", float("nan")) or float("nan")),
                "close": float(getattr(b, "close", float("nan")) or float("nan")),
                "volume": float(getattr(b, "volume", 0.0) or 0.0),
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["volume"] > 0]
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def _build_analysts(include_kronos: bool = False) -> list[Any]:
    """Build the analyst committee for replay.

    Mirrors the live advisor pipeline: ClassicalTAAnalyst + MicrostructureLite
    are always available; KronosAnalyst is opt-in (it is slow ~75s/symbol on
    CPU, so off by default in the bootstrap).
    """
    analysts: list[Any] = []
    try:
        from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst

        analysts.append(ClassicalTAAnalyst())
    except ImportError:
        logger.warning("ClassicalTAAnalyst unavailable; skipping")
    try:
        from hermes_quant.analysts.microstructure import MicrostructureLite

        analysts.append(MicrostructureLite())
    except ImportError:
        logger.warning("MicrostructureLite unavailable; skipping")
    if include_kronos:
        try:
            from hermes_quant.analysts.kronos import KronosAnalyst

            analysts.append(KronosAnalyst())
        except ImportError:
            logger.warning("KronosAnalyst not installed; skipping")
    return analysts


def _walk_bars_for_symbol(
    bars: pd.DataFrame,
    symbol: str,
    analysts: list[Any],
    horizon_bars: int,
    min_context_bars: int,
) -> dict[str, list[tuple[float, bool]]]:
    """Walk forward through `bars`, run each analyst at each t, pair with t+H outcome.

    Returns {analyst_name: [(raw_conf, direction_correct), ...]}.
    """
    out: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    n = len(bars)
    if n <= min_context_bars + horizon_bars:
        return out

    closes = bars["close"].to_numpy()
    timestamps = bars["timestamp"]

    # Walk t from min_context_bars to n-horizon_bars-1 (inclusive) so we
    # always have a future bar to settle against. Pre-slice once per t.
    for t in range(min_context_bars, n - horizon_bars):
        window = bars.iloc[: t + 1]
        asof_ts = pd.Timestamp(timestamps.iloc[t])
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
        ctx = MarketContext(
            asset=symbol,
            timeframe="1d",
            asset_class="equity",
            exchange=None,
            bars=window,
            last_close=float(closes[t]),
            last_volume=float(bars["volume"].iloc[t]),
            asof=asof_ts,
            extras={},
        )

        future_close = float(closes[t + horizon_bars])
        spot_close = float(closes[t])
        future_up = future_close > spot_close

        for analyst in analysts:
            analyst_name = getattr(analyst, "name", type(analyst).__name__)
            try:
                view: AnalystView | None = analyst.analyze(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "analyst %s raised on %s @ %s: %s — skipping bar",
                    analyst_name,
                    symbol,
                    asof_ts,
                    exc,
                )
                continue
            if view is None or view.direction == 0:
                continue
            # direction_correct: did the realized N-bar move match view.direction sign?
            direction_correct = (view.direction > 0) == future_up
            out[analyst_name].append((float(view.confidence_raw), bool(direction_correct)))
    return out


def _atomic_pickle(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def bootstrap_calibrator(
    symbols: Iterable[str],
    days: int = 365,
    timeframe: str = "1d",
    horizon_bars: int = 4,
    output_path: Path = DEFAULT_CALIBRATOR_PATH,
    min_samples: int = 200,
    include_kronos: bool = False,
    data_client: Any | None = None,
    seed: int = 1729,
) -> dict:
    """Replay the live advisor pipeline against historical Alpaca bars; fit + persist.

    Args:
        symbols: tickers to replay. Bootstrap is read-only against Alpaca.
        days: lookback window for bar history (default ~1y).
        timeframe: bar timeframe; only "1d" is wired here. (Kept for API parity.)
        horizon_bars: forward-return window in bars used to settle each emitted
            view. 4 ≈ 4 trading days for daily bars.
        output_path: pickle target. Atomic write.
        min_samples: aggregated (raw, correct) pairs required to actually fit
            the IsotonicCalibrator. Below this, the function still returns the
            collected pair count but `fitted=False` and no pickle is written.
        include_kronos: include KronosAnalyst. Off by default (slow).
        data_client: test seam — pass a mock to skip the real Alpaca client.
        seed: numpy RNG seed for reproducibility (currently affects nothing
            inside the analysts but pins any future stochastic step).

    Returns:
        dict with keys:
            n_samples (int): total aggregated (raw, correct) pairs across analysts
            fitted (bool): whether IsotonicCalibrator was fitted + persisted
            output_path (str): path string (always returned, even if not written)
            symbols_processed (int): symbols that yielded ≥1 walked bar
            analyst_breakdown (dict[str, int]): per-analyst pair count
    """
    if timeframe != "1d":
        raise ValueError(f"only timeframe='1d' is supported, got {timeframe!r}")

    np.random.seed(seed)

    if data_client is None:
        data_client = _build_data_client()

    end = datetime.now(UTC)
    # Pad lookback by ~30d to ensure we have ≥days of trading bars after
    # weekends/holidays drop out. The walk requires
    # min_context_bars (200) + horizon_bars (4) bars to emit anything, so we
    # explicitly need ≥204 trading days; days=365 calendar-days yields
    # ~252 trading days which is sufficient.
    start = end - timedelta(days=days + 30)

    analysts = _build_analysts(include_kronos=include_kronos)
    if not analysts:
        logger.warning("no analysts available; nothing to bootstrap")
        return {
            "n_samples": 0,
            "fitted": False,
            "output_path": str(output_path),
            "symbols_processed": 0,
            "analyst_breakdown": {},
        }

    pairs_by_analyst: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    symbols_processed = 0
    symbols_list = list(symbols)
    logger.info(
        "bootstrap: %d symbols, %d days, horizon=%d bars, analysts=%s",
        len(symbols_list),
        days,
        horizon_bars,
        [getattr(a, "name", type(a).__name__) for a in analysts],
    )

    for i, symbol in enumerate(symbols_list, 1):
        bars = _fetch_bars(data_client, symbol, start, end)
        if bars.empty or len(bars) <= _MIN_CONTEXT_BARS + horizon_bars:
            logger.info(
                "bootstrap: %s skipped (only %d bars; need >%d)",
                symbol,
                len(bars),
                _MIN_CONTEXT_BARS + horizon_bars,
            )
            continue
        per_symbol = _walk_bars_for_symbol(
            bars,
            symbol,
            analysts,
            horizon_bars=horizon_bars,
            min_context_bars=_MIN_CONTEXT_BARS,
        )
        n_views_this_symbol = sum(len(v) for v in per_symbol.values())
        if n_views_this_symbol > 0:
            symbols_processed += 1
        for k, v in per_symbol.items():
            pairs_by_analyst[k].extend(v)
        logger.info(
            "bootstrap: %s [%d/%d] yielded %d view-outcome pairs (cum=%d)",
            symbol,
            i,
            len(symbols_list),
            n_views_this_symbol,
            sum(len(v) for v in pairs_by_analyst.values()),
        )

    # Aggregate across analysts (ADR-0009 uses a SHARED calibrator at the
    # aggregator level; per-analyst calibrators are out of scope for this
    # bootstrap).
    all_raw: list[float] = []
    all_correct: list[bool] = []
    breakdown: dict[str, int] = {}
    for name, pairs in pairs_by_analyst.items():
        breakdown[name] = len(pairs)
        for raw, correct in pairs:
            all_raw.append(raw)
            all_correct.append(correct)

    n_samples = len(all_raw)
    result: dict[str, Any] = {
        "n_samples": n_samples,
        "fitted": False,
        "output_path": str(output_path),
        "symbols_processed": symbols_processed,
        "analyst_breakdown": breakdown,
    }

    if n_samples < min_samples:
        logger.warning(
            "bootstrap: only %d samples (need >=%d); calibrator NOT fitted",
            n_samples,
            min_samples,
        )
        return result

    cal = IsotonicCalibrator()
    try:
        cal.fit(np.asarray(all_raw), np.asarray(all_correct, dtype=float))
    except Exception as exc:  # noqa: BLE001
        logger.error("IsotonicCalibrator.fit failed: %s", exc)
        result["fit_error"] = repr(exc)
        return result

    _atomic_pickle(Path(output_path), cal)
    result["fitted"] = True
    logger.info(
        "bootstrap: fitted IsotonicCalibrator (n=%d) -> %s", cal.n_samples, output_path
    )
    return result


__all__ = ["bootstrap_calibrator", "DEFAULT_CALIBRATOR_PATH"]
