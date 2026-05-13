"""hermes_quant.daemon.slippage — Side-aware adverse-bps slippage estimator.

Per synthesis-v2 §P1-ζ: the original adverse-bps formula was unsigned, which
double-counts cost on sells (where a higher fill price is GOOD, not bad).

The corrected sign-aware computation:

    buys:  adverse = (fill_price - decision_price) / decision_price
    sells: adverse = (decision_price - fill_price) / decision_price

In both cases:
    - Positive adverse = trade was worse than expected (we paid more on buys
      OR received less on sells)
    - Negative adverse = trade was better than expected (favorable slippage)

We persist only POSITIVE adverse values into the rolling estimator — favorable
slippage is opportunity, not a cost we should add to the trading-cost budget.

The rolling estimator returns a per-asset slippage estimate that bootstraps
from a constant (5-25 bps depending on asset class) until 30 days of fills
exist (per ADR-0004 implementation notes).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

Side = Literal["buy", "sell"]


def compute_adverse_bps_signed(
    *,
    decision_price: float,
    fill_price: float,
    side: Side,
) -> float:
    """Side-aware adverse-bps computation.

    Per synthesis-v2 §P1-ζ:
        buys:  (fill - decision) / decision
        sells: (decision - fill) / decision

    Args:
        decision_price: price at which the decision was made (signal asof).
        fill_price: actual fill price reported by the broker.
        side: 'buy' or 'sell'.

    Returns:
        Signed adverse fraction. Positive = bad (paid more / received less).
        Negative = good (favorable slippage).

    Raises:
        ValueError: if decision_price is not positive.
    """
    if decision_price <= 0:
        raise ValueError(f"decision_price must be > 0, got {decision_price}")
    if side == "buy":
        return (fill_price - decision_price) / decision_price
    elif side == "sell":
        return (decision_price - fill_price) / decision_price
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


# Bootstrap defaults per ADR-0004 implementation notes
# (5 bps liquid crypto, 2 bps liquid equities, 25 bps illiquid, etc.)
DEFAULT_BOOTSTRAP_SLIPPAGE = {
    "crypto": 0.0012,    # 12 bps round-trip
    "equity": 0.0005,    # 5 bps round-trip
    "etf": 0.0005,       # 5 bps
    "fx": 0.0008,        # 8 bps
    "illiquid": 0.0025,  # 25 bps
}


@dataclass
class RollingSlippageEstimator:
    """Per-asset rolling slippage estimator.

    Maintains a deque of recent ADVERSE-ONLY slippage observations
    (negative = favorable, dropped). Returns the mean of the deque or a
    bootstrap default if the deque is empty / too small.

    Per synthesis-v2 §P1-ζ: only POSITIVE adverse persisted. Favorable
    slippage is opportunity, not a cost.

    Per ADR-0004 notes: bootstraps from a constant until 30 days of fills
    exist. We use a sample-count threshold (`min_samples_for_estimate`) as
    the practical proxy.

    Args:
        asset_class: for bootstrap default lookup.
        max_samples: ring buffer size for rolling mean.
        min_samples_for_estimate: below this, return bootstrap default.
        bootstrap_default: explicit override; defaults from class table.

    Notes:
        - The rolling mean is a simple arithmetic mean. Median is more robust
          to outliers but requires sorting on every read; for v0.1 simplicity
          wins. v0.2 may switch to median or trimmed-mean.
        - Round-trip vs one-way: we record per-fill (one-way) and double on
          read to approximate round-trip. The risk gate uses round-trip.
    """

    asset_class: str = "crypto"
    max_samples: int = 1000
    min_samples_for_estimate: int = 30
    bootstrap_default: float | None = None

    _samples: deque = field(default_factory=deque)  # stores POSITIVE adverse only

    def __post_init__(self) -> None:
        if self.bootstrap_default is None:
            self.bootstrap_default = DEFAULT_BOOTSTRAP_SLIPPAGE.get(
                self.asset_class, 0.0012
            )
        # The deque may have been initialized with non-bounded length; bound it
        if self._samples.maxlen != self.max_samples:
            self._samples = deque(self._samples, maxlen=self.max_samples)

    def observe(
        self,
        *,
        decision_price: float,
        fill_price: float,
        side: Side,
    ) -> None:
        """Record a fill observation.

        Per synthesis-v2 §P1-ζ: only positive adverse is persisted.
        """
        adverse = compute_adverse_bps_signed(
            decision_price=decision_price,
            fill_price=fill_price,
            side=side,
        )
        if adverse > 0:
            self._samples.append(adverse)
        # Negative adverse (favorable slippage) is discarded — opportunity,
        # not cost.

    @property
    def n_samples(self) -> int:
        return len(self._samples)

    def estimate_one_way(self) -> float:
        """Mean adverse-bps one-way. Returns bootstrap_default until min_samples."""
        if self.n_samples < self.min_samples_for_estimate:
            # Bootstrap default is round-trip; halve for one-way
            assert self.bootstrap_default is not None
            return self.bootstrap_default / 2.0
        return sum(self._samples) / len(self._samples)

    def estimate_round_trip(self) -> float:
        """Mean adverse-bps round-trip (entry + exit cost)."""
        if self.n_samples < self.min_samples_for_estimate:
            assert self.bootstrap_default is not None
            return self.bootstrap_default
        return 2.0 * (sum(self._samples) / len(self._samples))

    def reset(self) -> None:
        self._samples.clear()
