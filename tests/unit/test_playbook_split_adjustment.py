"""Split-adjustment regression for ``compute_play_snapshot`` (ar85).

Defect: ``compute_play_snapshot`` fetched ``tk.history(period="1y",
auto_adjust=False)`` and fed the RAW (split-UN-adjusted) Close/High/Low columns
into the statistical features (realized_vol_30d, rsi_14, atr, the 52-week-high
window, five_d_return) AND into ``build_regime_extras``. A routine corporate
action (e.g. a 4:1 split) injects a price discontinuity that:

  (a) within the trailing 30 sessions injects a single log-return of
      ln(0.25) = -1.386 into ``_realized_vol``, annualizing to a spurious
      ~22x ``realized_vol_30d``; and
  (b) anywhere in the 1y window inflates the raw 52-week high ~Nx so a stock
      sitting at its true post-split high reads ~-75% below it.

Those corrupted features are DETERMINISTIC GATE inputs in ``profiles.py``:
  * ``realized_vol_30d`` is a swing HARD rule (between 0.30 and 1.50) AND a
    ``vol_runaway`` eviction (gt_field 2.0) → a held name is WRONGLY EVICTED.
  * ``distance_from_52w_high_pct`` is a soft rule on covered_call / leaps.

The fix: the playbook is the ANALYSIS side, where back-adjustment IS wanted
(cf. the comment at ``data/yfinance_provider.py:211`` — raw OHLC is for the
*decision-price* path only). ``compute_play_snapshot`` must fetch with
``auto_adjust=True`` so the statistical features see split-consistent prices.
The most-recent bar is the adjustment anchor, so ``last_close`` (a price-band
gate input) is byte-identical between the two modes.

These tests exercise the REAL ``compute_play_snapshot`` body via a faithful
``yfinance.Ticker`` mock whose ``history()`` honors the ``auto_adjust`` kwarg
exactly as real yfinance does (raw OHLC when False, back-adjusted OHLC when
True). They run offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

import hermes_quant.playbook.scorers as scorers_module
from hermes_quant.playbook.profiles import PROFILES

# --------------------------------------------------------------------------- #
# Synthetic 1y daily history with a 4:1 split at t-15
# --------------------------------------------------------------------------- #

_N_BARS = 252
_SPLIT_RATIO = 4.0  # 4:1 forward split
_SPLIT_AT_FROM_END = 15  # split takes effect 15 sessions before the last bar
_PRE_SPLIT_PRICE = 400.0  # flat as-traded price BEFORE the split
_POST_SPLIT_PRICE = 100.0  # == pre / 4 — a totally calm, flat name (true vol ~0)


def _build_history(*, auto_adjust: bool) -> pd.DataFrame:
    """Build a faithful yfinance-style 1y daily OHLCV frame.

    The underlying is dead-calm: as-traded price is a flat 400 then (after a
    routine 4:1 split) a flat 100. True realized vol is ~0 and the name sits
    AT its post-split 52-week high.

    * ``auto_adjust=False`` → RAW as-traded OHLC (what real yfinance returns;
      only the separate 'Adj Close' column reflects adjustment). This is what
      the buggy code consumed.
    * ``auto_adjust=True`` → back-adjusted OHLC anchored on the most-recent
      bar (pre-split bars divided by the 4x split factor → flat 100 throughout).
    """
    end = datetime(2026, 6, 15, tzinfo=UTC)
    idx = pd.DatetimeIndex(
        [end - timedelta(days=(_N_BARS - 1 - i)) for i in range(_N_BARS)]
    )

    split_idx = _N_BARS - _SPLIT_AT_FROM_END  # first POST-split bar
    raw_close = [
        _PRE_SPLIT_PRICE if i < split_idx else _POST_SPLIT_PRICE
        for i in range(_N_BARS)
    ]

    if auto_adjust:
        # Back-adjust pre-split bars by 1/ratio (anchor = most recent bar).
        close = [
            c / _SPLIT_RATIO if i < split_idx else c
            for i, c in enumerate(raw_close)
        ]
    else:
        close = list(raw_close)

    # Tight intraday range so ATR is well-defined but small; high/low track
    # close on the same adjustment basis.
    high = [c * 1.01 for c in close]
    low = [c * 0.99 for c in close]
    openp = list(close)
    volume = [1_000_000.0] * _N_BARS

    return pd.DataFrame(
        {
            "Open": openp,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


class _FakeTicker:
    """Minimal yfinance.Ticker stand-in.

    ``history`` honors ``auto_adjust`` exactly as the real client does so the
    test is sensitive to the precise kwarg the production code passes.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, *, period=None, auto_adjust=False, **_kwargs):  # noqa: D401
        return _build_history(auto_adjust=auto_adjust)

    @property
    def info(self):
        # Enough fundamentals to land the name on an EQUITY profile; no network.
        return {
            "marketCap": 5e10,
            "quoteType": "EQUITY",
            "beta": 1.0,
        }


@pytest.fixture
def _patch_yfinance(monkeypatch):
    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", _FakeTicker)
    yield


# --------------------------------------------------------------------------- #
# RED: split corrupts the deterministic gate inputs
# --------------------------------------------------------------------------- #


def test_split_does_not_inject_phantom_realized_vol(_patch_yfinance):
    """A 4:1 split in the trailing 30 sessions must NOT inflate realized_vol_30d.

    On the buggy raw-price path the single ln(0.25) log-return annualizes to a
    ~22x ``realized_vol_30d`` (~4.0), which:
      * blows past the swing HARD rule upper band (between 0.30, 1.50), AND
      * trips the ``vol_runaway`` eviction (gt_field 2.0).
    The true underlying is dead-calm so adjusted vol must be ~0.
    """
    snap = scorers_module.compute_play_snapshot("CALM", asof=datetime(2026, 6, 15, tzinfo=UTC))

    rv = snap["realized_vol_30d"]
    assert rv is not None, "realized_vol_30d should be computed for a 1y series"

    # The vol_runaway eviction threshold (profiles.py swing: gt_field 2.0).
    vol_runaway_threshold = PROFILES["swing"].eviction_rules["vol_runaway"][2]
    assert rv < vol_runaway_threshold, (
        f"realized_vol_30d={rv!r} would trip the vol_runaway eviction "
        f"(>{vol_runaway_threshold}) on a dead-calm name — phantom vol from "
        f"the unadjusted 4:1 split log-return."
    )

    # And it must remain inside the swing HARD-rule admit band's upper bound
    # (a calm name reading 22x vol is also wrongly rejected from admission).
    swing_hi = PROFILES["swing"].hard_rules["realized_vol_30d"][2]
    assert rv <= swing_hi, (
        f"realized_vol_30d={rv!r} exceeds the swing admit upper band {swing_hi}."
    )


def test_split_does_not_distort_distance_from_52w_high(_patch_yfinance):
    """A name AT its true post-split high must read ~0% below the 52w high.

    On the raw path the pre-split 400 dominates ``max(window_52)`` so the
    post-split last price of 100 reads ~-75% below the (phantom) high — which
    fails the covered_call/leaps soft rule (distance >= -0.15 / -0.20).
    """
    snap = scorers_module.compute_play_snapshot("CALM", asof=datetime(2026, 6, 15, tzinfo=UTC))

    dist = snap["distance_from_52w_high_pct"]
    assert dist is not None
    assert dist == pytest.approx(0.0, abs=0.02), (
        f"distance_from_52w_high_pct={dist!r}: a name at its true post-split "
        f"high should read ~0, not deeply negative (raw 52w-high inflation)."
    )


def test_last_close_is_unchanged_by_adjustment(_patch_yfinance):
    """last_close (a price-band gate input) must stay the RAW as-traded price.

    auto_adjust anchors on the most-recent bar, so the adjusted last close
    equals the raw last close. The price-band hard rule (between 10, 500) and
    price_too_low eviction must be unaffected by the fix.
    """
    snap = scorers_module.compute_play_snapshot("CALM", asof=datetime(2026, 6, 15, tzinfo=UTC))
    assert snap["last_close"] == pytest.approx(_POST_SPLIT_PRICE), (
        "last_close must remain the as-traded post-split price (adjustment is "
        "anchored on the most recent bar)."
    )
