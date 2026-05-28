"""hermes_quant.react.slippage_model — paper-execution slippage envelope (ADR-0070).

Today PaperReactor writes `fill_price = decision_price` for 100% of fills (zero
modeled slippage). Live execution will be strictly worse: spread cross, market
impact for sized orders, queue-position drift over wall-clock latency, and an
auction premium for late-session orders that time into the close auction.

This module provides a deterministic-given-seed slippage envelope that PaperReactor
calls at fill time. Mean realized slippage runs roughly 5–15 bps per fill for liquid
US equities at 20% NAV, with realistic variance driven by ATR-implied σ_per_second.
The envelope is configurable per-asset-class via `PaperSlippageConfig`; defaults are
the "obviously safer than 0 bps" baseline.

The model is opt-in via env var `HERMES_QUANT_PAPER_SLIPPAGE_MODEL`:

  HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1  (or unset) — legacy passthrough,
                                                       fill_price = decision_price.
  HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2  — the envelope below applies.

Per ADR-0070 D70.4: default OFF for one trading day after merge so reflectors /
calibrators / Sharpe-eval consumers of `executions.jsonl` see a stable regime
during validation. Flip to default-on after side-by-side audit.

Determinism: a hash of (proposal_id, asof_execution) seeds the per-fill RNG, so
two replays of the same fill produce the same slippage. Replay equality (ADR-0009
§replay-equality) holds even with stochastic slippage.

Sign conventions:
  Long fill (target > 0):  pays POSITIVE slippage → fill_price > decision_price.
  Short fill (target < 0): pays POSITIVE slippage → fill_price < decision_price
                           (you receive fewer dollars per share you sold short).
  Either way, the trader is the price-taker; slippage is a cost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperSlippageConfig:
    """Per-asset-class slippage envelope. ADR-0070 §D70.1.

    All values are bps (1 bp = 0.01% = 0.0001) unless noted otherwise.

      * spread_cross_bps          — half-spread paid on entry. Multiply ×2 for
                                    round-trip on exit.
      * impact_bps_per_pct_nav    — bps cost per 1% of NAV traded (proxy for
                                    "size of order" without needing ADV info).
                                    For a 20% NAV pick at default 0.5 bps/%,
                                    impact = 10 bps. The default is calibrated
                                    so a 20% NAV equity fill totals ~13–18 bps
                                    (spread + impact + drift), in line with
                                    real-broker ITG / LiquidNet shortfall
                                    estimates for retail-sized institutional
                                    orders.
      * queue_latency_seconds     — wall-clock delay between decision tick and
                                    fill print. Drives σ_per_second drift.
      * vol_drift_bps_per_sqrt_sec — σ_per_second of price drift over latency,
                                    expressed as bps per √s (per stylized-fact
                                    diffusion). Liquid US equity ≈ 1.5 bps/√s.
      * auction_premium_bps       — extra paid for orders placed within
                                    auction_premium_minutes of close.
      * auction_premium_minutes   — how many minutes before close trigger the
                                    auction premium.
      * max_total_bps             — hard cap on total modeled slippage. Defaults
                                    to 75 bps so a single bad RNG draw can't
                                    produce 5%-of-NAV slippage that would never
                                    happen live.
      * default_target_pct        — for impact computation when target_pct is
                                    unknown. Conservative default 5%.
    """

    spread_cross_bps: float = 3.0
    impact_bps_per_pct_nav: float = 0.5
    queue_latency_seconds: float = 1.0
    vol_drift_bps_per_sqrt_sec: float = 1.5
    auction_premium_bps: float = 10.0
    auction_premium_minutes: int = 30
    max_total_bps: float = 75.0
    default_target_pct: float = 0.05

    @classmethod
    def equity_default(cls) -> PaperSlippageConfig:
        return cls()

    @classmethod
    def crypto_default(cls) -> PaperSlippageConfig:
        return cls(
            spread_cross_bps=8.0,
            impact_bps_per_pct_nav=1.5,
            queue_latency_seconds=0.5,
            vol_drift_bps_per_sqrt_sec=4.0,
            auction_premium_bps=0.0,    # no close auction
            auction_premium_minutes=0,
            max_total_bps=150.0,
        )


def config_for_asset_class(asset_class: str) -> PaperSlippageConfig:
    """Return per-asset-class default config."""
    if asset_class == "crypto":
        return PaperSlippageConfig.crypto_default()
    return PaperSlippageConfig.equity_default()


# ---------------------------------------------------------------------------
# Deterministic RNG seeding
# ---------------------------------------------------------------------------


def seed_for_fill(proposal_id: str, asof_execution: str) -> int:
    """Deterministic 64-bit seed from the fill identity.

    Two replays of the same (proposal_id, asof_execution) produce the same
    seed, hence the same RNG draws, hence the same modeled slippage. This
    preserves the ADR-0009 §replay-equality invariant.
    """
    raw = f"{proposal_id}|{asof_execution}".encode()
    digest = hashlib.sha256(raw).digest()
    # Take the low 8 bytes as a uint64 seed
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------


def apply_slippage(
    *,
    decision_price: float,
    target_pct: float,
    asof_execution: str,
    proposal_id: str,
    asset_class: str = "equity",
    config: PaperSlippageConfig | None = None,
    is_late_session: bool = False,
) -> tuple[float, dict[str, float]]:
    """Compute a slipped fill price.

    Args:
        decision_price: bar close at decision time. Must be > 0.
        target_pct: signed position target as fraction of NAV. Sign drives
            the slippage direction. Magnitude drives the impact term.
        asof_execution: ISO UTC timestamp of the fill, used as part of the
            deterministic RNG seed.
        proposal_id: the proposal ID that generated the fill, used as part
            of the deterministic RNG seed.
        asset_class: "equity" | "crypto". Selects per-class config.
        config: explicit config override; if None, uses the asset_class default.
        is_late_session: if True, the auction-premium term is added. The
            caller is responsible for determining whether the wall-clock
            time falls within auction_premium_minutes of close.

    Returns:
        (fill_price, breakdown):
          fill_price: decision_price adjusted by total modeled slippage
                       (always trader-adverse — paid as a cost regardless
                       of direction).
          breakdown: dict with the bps contribution of each term:
            spread_bps, impact_bps, latency_drift_bps, auction_bps,
            total_bps_pre_cap, total_bps (post-cap).

    Sign behavior:
        target_pct > 0 (long):  fill_price > decision_price (paid more)
        target_pct < 0 (short): fill_price < decision_price (received less)
        target_pct = 0:         fill_price = decision_price (no fill)
    """
    if decision_price <= 0:
        raise ValueError(f"decision_price must be > 0, got {decision_price}")

    cfg = config if config is not None else config_for_asset_class(asset_class)

    if target_pct == 0.0:
        return decision_price, {
            "spread_bps": 0.0,
            "impact_bps": 0.0,
            "latency_drift_bps": 0.0,
            "auction_bps": 0.0,
            "total_bps_pre_cap": 0.0,
            "total_bps": 0.0,
        }

    rng = np.random.default_rng(seed_for_fill(proposal_id, asof_execution))

    # Spread-cross: fixed half-spread, paid every fill.
    spread_bps = cfg.spread_cross_bps

    # Impact: linear in |target_pct| as a proxy for "% of NAV traded."
    # impact_bps_per_pct_nav is calibrated so a 20% NAV equity fill yields
    # ~10 bps impact at the default 0.5 bps/%. For more sophisticated
    # ADV-normalized impact, see ADR-0070 §D70.5 (deferred).
    size_pct = abs(target_pct) if target_pct != 0 else cfg.default_target_pct
    impact_bps = cfg.impact_bps_per_pct_nav * (size_pct * 100.0)
    # impact_bps_per_pct_nav is bps per 1% (=0.01); size_pct is a fraction
    # like 0.20 (=20%), so we multiply by 100 to get the 20× the per-1%
    # impact for a 20% position.

    # Latency drift: zero-mean Gaussian over √(latency seconds).
    # Drift can go either way, but we always realize it as adverse — the
    # trader is the price taker — so we take |draw|.
    sigma_bps = cfg.vol_drift_bps_per_sqrt_sec * np.sqrt(cfg.queue_latency_seconds)
    latency_drift_bps = float(abs(rng.normal(loc=0.0, scale=sigma_bps)))

    # Auction premium: late-session-only.
    auction_bps = cfg.auction_premium_bps if is_late_session else 0.0

    total_bps_pre_cap = spread_bps + impact_bps + latency_drift_bps + auction_bps
    total_bps = min(total_bps_pre_cap, cfg.max_total_bps)

    # Sign of slippage cost: trader adverse regardless of direction.
    # For a long, fill > decision; for a short, fill < decision.
    direction_sign = 1.0 if target_pct > 0 else -1.0
    fill_price = decision_price * (1.0 + direction_sign * (total_bps / 1e4))

    return fill_price, {
        "spread_bps": float(spread_bps),
        "impact_bps": float(impact_bps),
        "latency_drift_bps": float(latency_drift_bps),
        "auction_bps": float(auction_bps),
        "total_bps_pre_cap": float(total_bps_pre_cap),
        "total_bps": float(total_bps),
    }


# ---------------------------------------------------------------------------
# Helper: detect "late session" from asof_execution
# ---------------------------------------------------------------------------


def is_late_session_equity(asof_execution: str, *, minutes_before_close: int = 30) -> bool:
    """For US equities, detect if asof_execution falls within `minutes_before_close`
    of the 16:00 ET session close.

    asof_execution is an ISO-8601 UTC string. Returns True when wall-clock
    falls in `[16:00 - minutes_before_close, 16:00] ET` on the same date.
    Outside that window (including pre-open, post-close), returns False.

    Uses zoneinfo for DST correctness (matches the bar_alignment ADR-0069 helper).
    """
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    if not asof_execution:
        return False
    try:
        # Tolerate both "...Z" and "...+00:00" forms
        t = asof_execution.replace("Z", "+00:00")
        wall = datetime.fromisoformat(t)
    except ValueError:
        return False
    if wall.tzinfo is None:
        wall = wall.replace(tzinfo=timezone.utc)

    et = ZoneInfo("America/New_York")
    et_wall = wall.astimezone(et)
    close_t = time(16, 0)
    et_close = datetime.combine(et_wall.date(), close_t, tzinfo=et)
    et_late_window_start = et_close.replace(hour=15, minute=60 - minutes_before_close)
    # Above hack handles minutes_before_close=30 → 15:30 ET; for general
    # minutes_before_close, compute via timedelta.
    from datetime import timedelta
    et_late_window_start = et_close - timedelta(minutes=minutes_before_close)

    return et_late_window_start <= et_wall <= et_close
