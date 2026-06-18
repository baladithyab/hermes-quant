"""Tests for hermes_quant.options.iv_rank (agperc1 — the PERCEIVE-layer IV-rank seam).

Covers the cardinal NO-LOOKAHEAD invariant (a future-dated IV row is excluded),
the fail-closed abstain paths (<30 day-points, missing parquet / ChainQualityError),
a known-percentile correctness check, and the WatchlistEntry.options_eligible
add-only flag round-tripping through the dict loader.

Synthetic parquet via tmp_path; NO network, NO credentials, NO flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.options.data import ChainSnapshotReader
from hermes_quant.options.iv_rank import compute_iv_rank_asof
from hermes_quant.watchlist import WatchlistEntry, list_watchlist


# ---------------------------------------------------------------------------
# Synthetic parquet helpers (mirror tests/unit/test_options_data.py)
# ---------------------------------------------------------------------------


def _write_day_parquet(path: Path, rows: list[dict]) -> None:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df), path)


def _row(symbol: str, *, asof: datetime, fetched_at: datetime, iv: float) -> dict:
    return {
        "contract_symbol": symbol,
        "asof": asof,
        "fetched_at": fetched_at,
        "underlying_spot": 150.0,
        "risk_free_rate": 0.05,
        "bid": 2.40,
        "ask": 2.60,
        "last": 2.50,
        "volume": 100,
        "open_interest": 500,
        "delta": 0.30,
        "gamma": 0.01,
        "theta": -0.05,
        "vega": 0.10,
        "rho": 0.02,
        "iv": iv,
        "iv_source": "provider",
    }


def _write_iv_history(
    reader: ChainSnapshotReader,
    underlying: str,
    asof: datetime,
    daily_ivs: list[float],
) -> None:
    """Write one per-day parquet for each IV in `daily_ivs`, oldest first.

    daily_ivs[-1] is the CURRENT day (== asof.date()); daily_ivs[-2] is the day
    before, etc. Each day gets two contracts at the same IV so the median == that IV.
    """
    n = len(daily_ivs)
    for i, iv in enumerate(daily_ivs):
        # oldest is at offset n-1, current (asof.date()) at offset 0
        day_dt = asof - timedelta(days=(n - 1 - i))
        path = reader._path_for(underlying, day_dt.date())
        _write_day_parquet(
            path,
            [
                _row(f"NVDA260612C0014000{i}", asof=day_dt, fetched_at=day_dt, iv=iv),
                _row(f"NVDA260612C0015000{i}", asof=day_dt, fetched_at=day_dt, iv=iv),
            ],
        )


# ---------------------------------------------------------------------------
# 1. NO-LOOKAHEAD: a future-dated (fetched_at > asof) high-IV row is EXCLUDED.
# ---------------------------------------------------------------------------


def test_no_lookahead_future_row_changes_rank(tmp_path: Path) -> None:
    """RED-prove the cardinal no-look-ahead invariant: a future-dated (fetched_at > asof)
    high-IV row, if NOT filtered, lifts the CURRENT-day representative IV and flips the
    rank. The fetched_at<=asof filter (mirrors data.py:360) must EXCLUDE it.

    Setup: 39 distinct PRIOR days span 0.11..0.49 (all below 0.50). The CURRENT day has
    two at-asof rows at IV 0.30 PLUS two future-dated rows at 0.90.
      * filtered (correct):  current repr = median(0.30, 0.30) = 0.30.
        le_count = #{iv <= 0.30} = 20 prior (0.11..0.30) + the current 0.30 = 21.
        rank = 21/40 = 52.5.
      * unfiltered (LEAK):   current repr = median(0.30, 0.30, 0.90, 0.90) = 0.60.
        le_count = #{iv <= 0.60} = all 40. rank = 100.0.
    52.5 != 100.0 -> the test fails if the future rows are not dropped."""
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)

    # 39 PRIOR days (offsets 1..39), each a distinct IV in 0.11..0.49 (all < 0.50).
    prior = [round(0.11 + 0.01 * k, 2) for k in range(39)]
    for k, iv in enumerate(prior):
        day = asof - timedelta(days=(39 - k))  # offset 39 (oldest) .. 1 (yesterday)
        path = reader._path_for("NVDA", day.date())
        _write_day_parquet(
            path,
            [
                _row(f"NVDA260612C001{k:02d}000", asof=day, fetched_at=day, iv=iv),
                _row(f"NVDA260612C002{k:02d}000", asof=day, fetched_at=day, iv=iv),
            ],
        )

    # CURRENT day: two at-asof rows at 0.30 + two FUTURE-dated rows at 0.90 (the poison).
    current_path = reader._path_for("NVDA", asof.date())
    future_dt = asof + timedelta(hours=2)
    _write_day_parquet(
        current_path,
        [
            _row("NVDA260612C00300000", asof=asof, fetched_at=asof, iv=0.30),
            _row("NVDA260612C00301000", asof=asof, fetched_at=asof, iv=0.30),
            _row("NVDA260612C00900000", asof=asof, fetched_at=future_dt, iv=0.90),
            _row("NVDA260612C00901000", asof=asof, fetched_at=future_dt, iv=0.90),
        ],
    )

    rank = compute_iv_rank_asof("NVDA", asof, reader=reader, window_days=252)
    assert rank == pytest.approx(52.5), (
        f"expected the future 0.90 rows to be EXCLUDED (current IV 0.30 -> rank 52.5); got {rank}. "
        "Without the fetched_at<=asof filter the future rows lift the current repr to 0.60 -> rank 100.0."
    )


# ---------------------------------------------------------------------------
# 2. < 30 day-points -> None (abstain).
# ---------------------------------------------------------------------------


def test_insufficient_history_returns_none(tmp_path: Path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # Only 29 day-points -> below the 30-point floor -> abstain.
    _write_iv_history(reader, "NVDA", asof, [0.30] * 29)
    assert compute_iv_rank_asof("NVDA", asof, reader=reader) is None


def test_exactly_30_points_ranks(tmp_path: Path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # Exactly 30 -> at the floor -> ranks (not None). Flat series -> current==median==100.
    _write_iv_history(reader, "NVDA", asof, [0.30] * 30)
    rank = compute_iv_rank_asof("NVDA", asof, reader=reader)
    assert rank is not None
    assert rank == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. missing parquet / ChainQualityError -> None (NOT raises).
# ---------------------------------------------------------------------------


def test_missing_parquet_returns_none(tmp_path: Path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # No parquet written anywhere -> no day-points -> abstain (None), never raises.
    result = compute_iv_rank_asof("NVDA", asof, reader=reader)
    assert result is None


# ---------------------------------------------------------------------------
# 4. known IV series -> known percentile.
# ---------------------------------------------------------------------------


def test_known_series_median_is_50(tmp_path: Path) -> None:
    """Current IV at the median of the window -> ~50 percentile.

    Window of 40 days: IVs 0.01..0.40 (one per day), CURRENT day at 0.20 (the 20th
    value). le_count = #{iv <= 0.20} = 20 (0.01..0.20). 20/40 = 50.0. A broken
    denominator (39) -> 51.3, off-by-one (21 or 19) -> 52.5/47.5 — all diverge from 50."""
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # daily_ivs[-1] is CURRENT. Build so current == 0.20 and the rest span 0.01..0.40.
    others = [round(0.01 * k, 2) for k in range(1, 41) if round(0.01 * k, 2) != 0.20]
    daily = others + [0.20]  # 39 others + current 0.20 = 40 points
    _write_iv_history(reader, "NVDA", asof, daily)
    rank = compute_iv_rank_asof("NVDA", asof, reader=reader)
    assert rank == pytest.approx(50.0), f"expected median->50.0, got {rank}"


def test_current_at_top_is_100(tmp_path: Path) -> None:
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    # current is the MAX -> every historical <= current -> 100.
    daily = [round(0.01 * k, 2) for k in range(1, 40)] + [0.95]
    _write_iv_history(reader, "NVDA", asof, daily)
    rank = compute_iv_rank_asof("NVDA", asof, reader=reader)
    assert rank == pytest.approx(100.0)


def test_nan_iv_dropped_not_ranked(tmp_path: Path) -> None:
    """A NaN IV in the CURRENT day's parquet is DROPPED before the day's median.

    RED-prove: the current day has exactly ONE finite row (0.30) and ONE NaN row.
      * finite-guard ON (correct): NaN dropped -> current-day median = median([0.30]) = 0.30.
        flat 0.30 series -> rank 100.0.
      * finite-guard OFF (leak): median([0.30, nan]) = mean of two = NaN -> current_iv NaN ->
        NO ``iv <= NaN`` is True -> le_count 0 -> rank 0.0.
    100.0 != 0.0 -> a missing finite-guard would corrupt the rank to 0."""
    asof = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    reader = ChainSnapshotReader(chains_dir=tmp_path)
    _write_iv_history(reader, "NVDA", asof, [0.30] * 35)
    # REPLACE the current day with exactly one finite (0.30) + one NaN row, both at-asof,
    # so median([finite, nan]) = NaN unless the NaN is dropped first.
    current_path = reader._path_for("NVDA", asof.date())
    _write_day_parquet(
        current_path,
        [
            _row("NVDA260612C00160000", asof=asof, fetched_at=asof, iv=0.30),
            _row("NVDA260612C00170000", asof=asof, fetched_at=asof, iv=float("nan")),
        ],
    )
    rank = compute_iv_rank_asof("NVDA", asof, reader=reader)
    # NaN dropped -> current-day median is 0.30 -> flat series -> 100.
    assert rank == pytest.approx(100.0), (
        f"expected the NaN to be DROPPED (rank 100.0); got {rank}. "
        "Without the finite-guard median([0.30, nan]) = NaN -> current_iv NaN -> rank 0.0."
    )


# ---------------------------------------------------------------------------
# 5. WatchlistEntry.options_eligible defaults False + round-trips through dict loader.
# ---------------------------------------------------------------------------


def test_options_eligible_defaults_false() -> None:
    e = WatchlistEntry(symbol="NVDA", asset_class="equity", timeframe="1d")
    assert e.options_eligible is False
    # to_dict round-trips the field.
    assert e.to_dict()["options_eligible"] is False


def test_options_eligible_roundtrips_through_dict_loader(tmp_path: Path) -> None:
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg = {
        "quant": {
            "autonomous": {
                "watchlist": [
                    {"symbol": "AAPL", "asset_class": "equity", "timeframe": "1d"},
                    {
                        "symbol": "NVDA",
                        "asset_class": "equity",
                        "timeframe": "1d",
                        "options_eligible": True,
                    },
                ]
            }
        }
    }
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    entries = {e.symbol: e for e in list_watchlist(path=cfg_path)}
    # AAPL has no key -> defaults False (byte-identical to a pre-agperc1 entry).
    assert entries["AAPL"].options_eligible is False
    # NVDA opted in -> threaded through as True.
    assert entries["NVDA"].options_eligible is True
