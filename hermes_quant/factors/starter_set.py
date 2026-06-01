"""hermes_quant.factors.starter_set — Starter alpha factor registry.

Registers 15 WorldQuant-style alpha factors into an :class:`AlphaZoo`.
All factors are PURE pandas/numpy expressions that pass both the AST
purity gate and the lookahead sentinel.

These serve as:
  - Proof-of-concept that the safety infrastructure works end-to-end.
  - A baseline signal library for backtesting bootstraps.
  - A canonical reference for what a valid factor expression looks like.

Target: 452-factor catalog deferred to v0.2 (see ADR-0050 §Future Work).

References:
    HKUDS/Vibe-Trading — 452-factor Alpha Zoo (Wave 8c, ADR-0050)
    WorldQuant Alpha Catalog — selected starter signals
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from hermes_quant.factors.alpha_zoo import AlphaFactor, AlphaZoo

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Factor definitions — (name, description, source_code, tags)
# ---------------------------------------------------------------------------

_STARTER_FACTORS: list[dict] = [
    {
        "name": "alpha_close_minus_open",
        "description": (
            "Intraday price range proxy: difference between close and open. "
            "Positive = bullish intraday momentum."
        ),
        "source_code": 'bars["close"] - bars["open"]',
        "tags": ["momentum", "intraday", "price"],
    },
    {
        "name": "alpha_close_to_high_ratio",
        "description": (
            "Where did the close land relative to the daily high? "
            "Values near 1.0 indicate strong closing strength."
        ),
        "source_code": 'bars["close"] / bars["high"]',
        "tags": ["price", "strength", "intraday"],
    },
    {
        "name": "alpha_volume_zscore_20",
        "description": (
            "20-day rolling z-score of volume. Positive values = abnormally "
            "high volume relative to recent history (breakout signal)."
        ),
        "source_code": (
            "(bars[\"volume\"] - bars[\"volume\"].rolling(20).mean()) "
            "/ bars[\"volume\"].rolling(20).std()"
        ),
        "tags": ["volume", "zscore", "rolling"],
    },
    {
        "name": "alpha_log_return_5d",
        "description": "5-day log return: ln(close_t / close_{t-5}).",
        "source_code": 'np.log(bars["close"] / bars["close"].shift(5))',
        "tags": ["momentum", "return", "log"],
    },
    {
        "name": "alpha_high_low_range",
        "description": (
            "Normalised intraday range: (high - low) / close. "
            "Proxy for realised volatility and liquidity."
        ),
        "source_code": '(bars["high"] - bars["low"]) / bars["close"]',
        "tags": ["volatility", "range", "intraday"],
    },
    {
        "name": "alpha_close_above_ma20",
        "description": (
            "Binary signal: 1.0 when close > 20-day SMA, else 0.0. "
            "Trend-following regime filter."
        ),
        "source_code": (
            "(bars[\"close\"] > bars[\"close\"].rolling(20).mean()).astype(float)"
        ),
        "tags": ["trend", "binary", "ma"],
    },
    {
        "name": "alpha_rsi_14",
        "description": (
            "14-period Relative Strength Index computed from close returns. "
            "Values above 70 = overbought; below 30 = oversold."
        ),
        "source_code": (
            "pd.Series(\n"
            "    (lambda delta, gain, loss: "
            "100.0 - 100.0 / (1.0 + gain.rolling(14).mean() / loss.rolling(14).mean().replace(0, 1e-9)))(\n"
            "        bars[\"close\"].diff(),\n"
            "        bars[\"close\"].diff().clip(lower=0),\n"
            "        bars[\"close\"].diff().clip(upper=0).abs(),\n"
            "    ),\n"
            "    index=bars.index,\n"
            ")"
        ),
        "tags": ["oscillator", "rsi", "momentum"],
    },
    {
        "name": "alpha_atr_14_relative",
        "description": (
            "Average True Range (14 periods) divided by close price. "
            "Normalised volatility measure independent of price level."
        ),
        "source_code": (
            "(pd.concat([\n"
            "    (bars[\"high\"] - bars[\"low\"]).abs(),\n"
            "    (bars[\"high\"] - bars[\"close\"].shift(1)).abs(),\n"
            "    (bars[\"low\"] - bars[\"close\"].shift(1)).abs(),\n"
            "], axis=1).max(axis=1).rolling(14).mean()) / bars[\"close\"]"
        ),
        "tags": ["volatility", "atr", "normalised"],
    },
    {
        "name": "alpha_volume_price_corr_20",
        "description": (
            "20-day rolling Pearson correlation between close and volume. "
            "Positive = price moves with volume confirmation."
        ),
        "source_code": 'bars["close"].rolling(20).corr(bars["volume"])',
        "tags": ["correlation", "volume", "rolling"],
    },
    {
        "name": "alpha_momentum_60d",
        "description": "60-day price momentum: (close_t - close_{t-60}) / close_{t-60}.",
        "source_code": 'bars["close"].pct_change(60)',
        "tags": ["momentum", "return", "long"],
    },
    {
        "name": "alpha_obv_normalised",
        "description": (
            "On-Balance Volume proxy: running sum of signed volume where sign "
            "is determined by the daily close direction. Normalised by a 20-day "
            "rolling std to create a stationary signal."
        ),
        "source_code": (
            "(bars[\"volume\"] * np.sign(bars[\"close\"].diff())).cumsum() / "
            "((bars[\"volume\"] * np.sign(bars[\"close\"].diff())).cumsum().rolling(20).std() + 1e-9)"
        ),
        "tags": ["volume", "obv", "momentum"],
    },
    {
        "name": "alpha_price_acceleration",
        "description": (
            "Second derivative of close price: 1-day return minus 5-day return. "
            "Captures mean-reversion after momentum bursts."
        ),
        "source_code": (
            'bars["close"].pct_change(1) - bars["close"].pct_change(5)'
        ),
        "tags": ["momentum", "mean_reversion", "acceleration"],
    },
    {
        "name": "alpha_volume_weighted_return",
        "description": (
            "Close return weighted by normalised volume. "
            "Amplifies returns on high-conviction (high-volume) days."
        ),
        "source_code": (
            "bars[\"close\"].pct_change(1) * "
            "(bars[\"volume\"] / bars[\"volume\"].rolling(20).mean())"
        ),
        "tags": ["volume", "return", "composite"],
    },
    {
        "name": "alpha_bollinger_position",
        "description": (
            "Position of close within 20-day Bollinger Band (0 = lower band, "
            "1 = upper band). Mean-reversion indicator."
        ),
        "source_code": (
            "(bars[\"close\"] - (bars[\"close\"].rolling(20).mean() - 2 * bars[\"close\"].rolling(20).std())) / "
            "((bars[\"close\"].rolling(20).mean() + 2 * bars[\"close\"].rolling(20).std()) - "
            "(bars[\"close\"].rolling(20).mean() - 2 * bars[\"close\"].rolling(20).std()) + 1e-9)"
        ),
        "tags": ["bollinger", "mean_reversion", "normalised"],
    },
    {
        "name": "alpha_turnover_5d",
        "description": (
            "5-day average daily turnover ratio: volume / rolling_20d_avg_volume. "
            "Values > 1 signal unusually active trading."
        ),
        "source_code": (
            "bars[\"volume\"].rolling(5).mean() / "
            "(bars[\"volume\"].rolling(20).mean() + 1e-9)"
        ),
        "tags": ["volume", "turnover", "activity"],
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_starter_set(
    zoo: AlphaZoo,
    *,
    factor_returns: Mapping[str, np.ndarray] | None = None,
) -> list[str]:
    """Register the 15-factor starter set into *zoo*.

    All factors are validated through both the AST purity gate and the
    lookahead sentinel before being accepted.  Any unexpected rejection
    indicates a regression in the gate or a bug in the factor source.

    IC-dedup gate (B38, DEFAULT-OFF):
        When *factor_returns* is supplied AND the operator has set
        ``HERMES_QUANT_IC_DEDUP_AT_INGEST=1``, each factor's per-factor return
        series is forwarded to :meth:`AlphaZoo.register` so the IC-dedup gate
        can reject near-duplicates (ICmax >= threshold) at ingest. The mapping
        is keyed by factor *name* (``defn["name"]``); a factor with no entry in
        the mapping is registered with ``factor_returns=None`` (the gate is a
        no-op for that factor). When *factor_returns* is ``None`` (the default,
        and what every current call-site passes), behavior is byte-identical to
        the prior two-gate-only path -- the IC-dedup gate never runs.

    Args:
        zoo:            An :class:`AlphaZoo` instance to register factors into.
        factor_returns: Optional mapping ``{factor_name: return_series}`` used
                        by the IC-dedup gate. Default ``None`` (gate off).

    Returns:
        List of ``factor_id`` strings for all SUCCESSFULLY registered factors.
        When the IC-dedup gate is active a near-duplicate factor is rejected
        with :class:`~hermes_quant.factors.alpha_zoo.RedundantFactorError`,
        which propagates to the caller.

    Raises:
        PurityViolation:   If any factor fails the AST purity gate
                           (should never happen for this starter set).
        LookaheadDetected: If any factor fails the lookahead sentinel
                           (should never happen for this starter set).
        RedundantFactorError: If the IC-dedup gate (when active) rejects a
                           factor as a near-duplicate of an earlier one.
    """
    registered: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    for defn in _STARTER_FACTORS:
        factor = AlphaFactor(
            name=defn["name"],
            description=defn["description"],
            source_code=defn["source_code"],
            author="starter_set_v1",
            created_at=now,
            tags=defn.get("tags", []),
            params={},
            version=1,
        )
        rets = (
            factor_returns.get(defn["name"])
            if factor_returns is not None
            else None
        )
        fid = zoo.register(factor, factor_returns=rets)
        registered.append(fid)
        logger.debug("starter_set: registered %r as %s", factor.name, fid)

    logger.info("starter_set: registered %d factors", len(registered))
    return registered
