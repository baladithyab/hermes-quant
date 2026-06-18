"""agperc2 builder-level proof: HERMES_QUANT_OPTIONS_PERCEIVE + options_eligible
gate frame.options_chain / frame.iv_rank (Step 5e of build_perception_frame).

The contract (both REQUIRED):
  * flag absent OR options_eligible=False -> Step 5e is SKIPPED -> options_chain
    is None, iv_rank is None, and the projected ctx.extras carries no `iv_rank`
    key (byte-identical default path). The same synthetic chain parquet that
    populates the BOTH-ON case is on disk here, so the None is genuinely the gate,
    not a missing chain (non-vacuous).
  * flag=1 AND options_eligible=True -> the builder reads the recorded chain via
    ChainSnapshotReader.replay_chain and the as-of IV-rank via compute_iv_rank_asof
    (agperc1), so options_chain is populated, iv_rank is set, and the adapter
    surfaces extras['iv_rank'].
  * a ChainQualityError in Step 5e (missing / thin recorded chain) is CAUGHT and
    logged at WARNING — the frame is still built with options_chain=None (RR13:
    a missing chain must NOT abort frame building).
  * NO-LOOKAHEAD preserved end-to-end: the chain + iv_rank reflect ONLY rows
    visible at the bar-asof anchor (fetched_at <= asof / asof <= asof); a
    future-dated row never contributes.

Offline/deterministic: a RecordingProvider serves in-memory bars; the recorded
option chain is synthetic parquet under tmp_path (the chains dir is monkeypatched).
NO network, NO credentials.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import hermes_quant.options.data as options_data
from hermes_quant.options.data import ChainSnapshotReader, OptionChain
from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.builder import build_perception_frame

# The bar-asof replay anchor: the builder drops the still-forming last bar, so
# frame.asof == the LAST kept bar's timestamp. We build bars so the last kept bar
# is at this date and write the option chain parquet under the SAME date.
ASOF_DAY = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Synthetic bars (mirror tests/perception/test_event_risk_builder.py)
# ---------------------------------------------------------------------------


def _make_bars(n: int = 60, *, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed=seed)
    # Daily bars ending exactly at ASOF_DAY (a complete midnight-UTC daily bar, NOT
    # dropped by the still-forming guard) so the last KEPT bar -> frame.asof == ASOF_DAY.
    # The option chain parquet is written under the SAME date so Step 5e finds it.
    ts = pd.date_range(end=ASOF_DAY, periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * 0.3 + rng.normal(0, 0.4, n)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.3,
            "low": closes - 0.3,
            "close": closes,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


class _RecordingProvider:
    name = "recording"
    asset_classes = ["equity"]
    timeframes = ["1d"]
    requires_credentials = False

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._bars.copy()
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


# ---------------------------------------------------------------------------
# Synthetic chain parquet (mirror tests/options/test_iv_rank.py)
# ---------------------------------------------------------------------------


def _write_day_parquet(path: Path, rows: list[dict]) -> None:
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
    anchor: datetime,
    daily_ivs: list[float],
    *,
    fetched_offset: timedelta | None = None,
) -> None:
    """Write one per-day parquet per IV in `daily_ivs`, oldest first; current at
    `anchor.date()`. Each day gets two contracts at the same IV (median == IV).

    `fetched_offset` (if given) is added to fetched_at for the CURRENT day's rows
    so a future-dated row can be planted to exercise the no-lookahead filter."""
    n = len(daily_ivs)
    for i, iv in enumerate(daily_ivs):
        day_dt = anchor - timedelta(days=(n - 1 - i))
        path = reader._path_for(underlying, day_dt.date())
        fetched = day_dt
        if fetched_offset is not None and i == n - 1:
            fetched = day_dt + fetched_offset
        _write_day_parquet(
            path,
            [
                _row(f"NVDA260612C0014000{i}", asof=day_dt, fetched_at=fetched, iv=iv),
                _row(f"NVDA260612C0015000{i}", asof=day_dt, fetched_at=fetched, iv=iv),
            ],
        )


@pytest.fixture()
def _chains_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the DEFAULT ChainSnapshotReader dir (the one Step 5e instantiates) at
    tmp_path, so the builder's internal `ChainSnapshotReader()` reads our synthetic
    parquet without any network / real ~/.hermes dir."""
    d = tmp_path / "option_chains"
    monkeypatch.setattr(options_data, "_DEFAULT_CHAINS_DIR", d)
    return d


def _build(bars: pd.DataFrame, *, options_eligible: bool):
    return build_perception_frame(
        "NVDA",
        timeframe="1d",
        asset_class="equity",
        provider=_RecordingProvider(bars),
        asof_ts=pd.Timestamp(bars["timestamp"].iloc[-1]),
        lookback_bars=200,
        decision_asof=ASOF_DAY,
        options_eligible=options_eligible,
    )


def _seed_chain(chains_dir: Path, *, fetched_offset: timedelta | None = None) -> None:
    """Write a 35-day flat-0.30 IV history anchored at ASOF_DAY (>= the 30-point
    floor so compute_iv_rank_asof does NOT abstain; flat -> rank 100.0)."""
    reader = ChainSnapshotReader(chains_dir=chains_dir)
    _write_iv_history(reader, "NVDA", ASOF_DAY, [0.30] * 35, fetched_offset=fetched_offset)


# ---------------------------------------------------------------------------
# 1. DEFAULT-OFF byte-identical (flag unset OR options_eligible=False -> skip 5e).
# ---------------------------------------------------------------------------


def test_flag_unset_skips_step5e(monkeypatch, _chains_dir):
    """Flag absent, options_eligible=True: Step 5e SKIPPED -> None despite a valid
    chain on disk. RED-prove non-vacuous: the SAME chain populates the BOTH-ON case."""
    monkeypatch.delenv("HERMES_QUANT_OPTIONS_PERCEIVE", raising=False)
    _seed_chain(_chains_dir)
    frame = _build(_make_bars(), options_eligible=True)
    assert frame is not None
    assert frame.options_chain is None
    assert frame.iv_rank is None
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert "iv_rank" not in ctx.extras


def test_not_eligible_skips_step5e(monkeypatch, _chains_dir):
    """Flag=1 but options_eligible=False: Step 5e SKIPPED (BOTH required) -> None
    despite a valid chain on disk."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_PERCEIVE", "1")
    _seed_chain(_chains_dir)
    frame = _build(_make_bars(), options_eligible=False)
    assert frame is not None
    assert frame.options_chain is None
    assert frame.iv_rank is None
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert "iv_rank" not in ctx.extras


def test_explicit_zero_flag_skips_step5e(monkeypatch, _chains_dir):
    """An explicit '0' is identical to absent (default-OFF discipline)."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_PERCEIVE", "0")
    _seed_chain(_chains_dir)
    frame = _build(_make_bars(), options_eligible=True)
    assert frame is not None
    assert frame.options_chain is None
    assert frame.iv_rank is None


# ---------------------------------------------------------------------------
# 2. BOTH-ON: flag=1 AND options_eligible=True -> chain + iv_rank populated.
# ---------------------------------------------------------------------------


def test_both_on_populates_chain_and_iv_rank(monkeypatch, _chains_dir):
    """Flag=1 AND options_eligible=True with a recorded chain -> options_chain is an
    OptionChain, iv_rank is set (flat 0.30 series -> 100.0), and the adapter surfaces
    extras['iv_rank']. This proves test 1's None is the GATE, not a missing chain."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_PERCEIVE", "1")
    _seed_chain(_chains_dir)
    frame = _build(_make_bars(), options_eligible=True)
    assert frame is not None
    assert isinstance(frame.options_chain, OptionChain)
    assert frame.options_chain.underlying == "NVDA"
    assert len(frame.options_chain.snapshots) == 2  # current-day chain (two contracts)
    assert frame.iv_rank == pytest.approx(100.0)
    ctx = frame_to_context(frame, timeframe="1d", asset_class="equity")
    assert ctx.extras["iv_rank"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. ChainQualityError in Step 5e -> caught, WARNING, options_chain=None (not raised).
# ---------------------------------------------------------------------------


def test_chain_quality_error_caught_not_raised(monkeypatch, _chains_dir, caplog):
    """Both ON, but NO recorded chain at the asof day (and no IV history) -> the
    replay_chain raises ChainQualityError; Step 5e CATCHES it, logs WARNING, and the
    frame is still built with options_chain=None / iv_rank=None (RR13 — a missing
    chain must not abort frame building)."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_PERCEIVE", "1")
    # _chains_dir exists but is EMPTY -> replay_chain hits 'no recorded chain' ->
    # ChainQualityError. We must NOT seed a chain here.
    with caplog.at_level(logging.WARNING, logger="hermes_quant.perception.builder"):
        frame = _build(_make_bars(), options_eligible=True)
    assert frame is not None  # NOT raised
    assert frame.options_chain is None
    assert frame.iv_rank is None
    assert any(
        "options chain unavailable" in rec.getMessage() for rec in caplog.records
    ), "expected a WARNING that the options chain was unavailable"


# ---------------------------------------------------------------------------
# 4. NO-LOOKAHEAD preserved end-to-end (chain/iv_rank reflect only fetched_at<=asof).
# ---------------------------------------------------------------------------


def test_no_lookahead_future_row_excluded_end_to_end(monkeypatch, _chains_dir):
    """A future-dated (fetched_at > asof) high-IV row on the CURRENT day is EXCLUDED
    end-to-end, so both the chain snapshots AND the iv_rank reflect only rows visible
    at the bar-asof anchor.

    Setup: 34 prior flat-0.30 days + a CURRENT day whose two at-asof rows are 0.30
    BUT whose fetched_at is 2h in the FUTURE (past the bar-asof anchor).
      * filter ON (correct): the current day's future rows are DROPPED -> replay_chain
        for the current day raises ChainQualityError (<2 contracts) -> caught ->
        options_chain None; compute_iv_rank_asof has no current-day point -> ranks the
        34 prior flat-0.30 days -> 100.0 (still >= the 30-point floor).
      * filter OFF (LEAK): the future 0.30 rows survive -> the chain is populated.
    We assert options_chain is None (the future-dated current chain was filtered to
    <2 and abstained) while iv_rank still ranks the visible prior history."""
    monkeypatch.setenv("HERMES_QUANT_OPTIONS_PERCEIVE", "1")
    reader = ChainSnapshotReader(chains_dir=_chains_dir)
    # 34 prior days (offsets 1..34) at flat 0.30, all fetched at their own day.
    _write_iv_history(reader, "NVDA", ASOF_DAY - timedelta(days=1), [0.30] * 34)
    # CURRENT day (ASOF_DAY): two rows whose fetched_at is in the FUTURE (poison).
    future = ASOF_DAY + timedelta(hours=2)
    _write_day_parquet(
        reader._path_for("NVDA", ASOF_DAY.date()),
        [
            _row("NVDA260612C00300000", asof=ASOF_DAY, fetched_at=future, iv=0.30),
            _row("NVDA260612C00301000", asof=ASOF_DAY, fetched_at=future, iv=0.30),
        ],
    )
    frame = _build(_make_bars(), options_eligible=True)
    assert frame is not None
    # The current-day chain's only rows are future-dated -> filtered to <2 ->
    # ChainQualityError -> caught -> options_chain None (the leak would populate it).
    assert frame.options_chain is None, (
        "the future-dated (fetched_at > asof) current-day rows must be EXCLUDED; "
        "a populated chain here is a NO-LOOKAHEAD leak"
    )
    # iv_rank ranks ONLY the 34 visible prior flat-0.30 days (>= 30-point floor) -> 100.0.
    assert frame.iv_rank == pytest.approx(100.0)
