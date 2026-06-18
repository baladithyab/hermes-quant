"""hermes_quant.options.iv_rank — as-of-honest IV-rank from recorded chain history.

The PERCEIVE-layer seam the options-origination wiring needs. Today
``autonomous._originate_mleg_proposal`` passes ``iv_rank=None`` (abstain), so the
options structure-selection is INERT but WIRED (autonomous.py:2324). This module
makes the IV-rank REAL: it reads the recorded implied-vol history from the SAME
per-day chain parquets ``ChainSnapshotReader.replay_chain`` reads
(``~/.hermes/quant/option_chains/<u>/<YYYY-MM-DD>.parquet``, ADR-0028 D7) and
returns a percentile rank in ``[0, 100]`` — the convention the consumer
(``options/structure_select.classify_iv_regime``) reads (0..100, validated there).

NO-LOOKAHEAD (cardinal, ADR-0028 D5): the IV history is filtered with the SAME
``fetched_at <= asof`` AND ``asof <= asof`` guard ``replay_chain`` applies at
``data.py:360``. An IV-rank computed from a future-dated row is a look-ahead leak
— the percentile would know tomorrow's vol today. Every per-day parquet is run
through that filter before its IV contributes to the trailing window.

FAIL-CLOSED / ABSTAIN (money-software posture): this never fabricates a rank.
  * < ``_MIN_WINDOW_POINTS`` (30) day-points in the window -> ``None`` (insufficient
    history; abstain rather than rank against a thin sample).
  * missing parquet / ``ChainQualityError`` for the current day -> ``None`` (NOT a
    raise; the caller treats None as "no IV regime" and abstains).
  * a NaN / inf IV is DROPPED, never ranked (a non-finite value defeats every
    ``<=`` comparison; finite-guard before it enters the series).

The compute is PURE (no flag read here): the ``HERMES_QUANT_OPTIONS_PERCEIVE``
flag gates the CALLER (agperc2), not the computation. ``options_perceive_enabled``
exposes the flag read so the default-OFF rail is registered in the flag inventory
and the caller can gate on it without re-implementing the fail-closed convention.
"""

from __future__ import annotations

import logging
import math
import os
import statistics
from datetime import date, datetime, timedelta

from .data import ChainQualityError, ChainSnapshotReader

logger = logging.getLogger(__name__)

# Minimum trailing day-points required to rank honestly. Below this the sample is
# too thin to be a meaningful percentile; abstain (return None) rather than emit a
# fabricated rank from a handful of observations.
_MIN_WINDOW_POINTS = 30

# Flag that gates the PERCEIVE-layer CALLER (agperc2). Default-OFF. compute_iv_rank_asof
# itself is pure and does NOT read this; the flag lives here so the caller gates on one
# canonical helper and the rail is documented in the flag inventory (default '0').
OPTIONS_PERCEIVE_FLAG = "HERMES_QUANT_OPTIONS_PERCEIVE"


def options_perceive_enabled() -> bool:
    """True iff the PERCEIVE-layer (IV-rank sourcing) is enabled (``=1``). Default-OFF.

    Fail-closed: any value other than the literal ``"1"`` is OFF, so a typo / partial
    config never silently enables a money-path perception seam. The compute itself is
    pure; this gates the CALLER that feeds the rank into structure selection."""
    return os.environ.get(OPTIONS_PERCEIVE_FLAG, "0") == "1"


def _finite_iv(value: object) -> float | None:
    """Coerce a recorded IV cell to a finite positive float, else None.

    A NaN/inf IV (an IV-overflow / degenerate-mid solve artifact) is inadmissible:
    it defeats every ``<=`` comparison in the percentile and would silently corrupt
    the rank (NaN-fail-open). A non-positive IV is also dropped (vol must be > 0)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0.0:
        return None
    return v


def _representative_iv_for_day(
    reader: ChainSnapshotReader, underlying: str, day: date, asof: datetime
) -> float | None:
    """Read one day's chain parquet and return that day's representative IV, or None.

    Applies the SAME no-look-ahead filter ``replay_chain`` applies (``fetched_at <= asof``
    AND ``asof <= asof``) so a future-dated row never contributes. The day's representative
    IV is the MEDIAN of the finite per-contract IVs visible at ``asof`` for that day (median
    is robust to a single mispriced wing). Returns None when the day has no parquet or no
    admissible finite IV (missing day -> the day simply does not contribute a point)."""
    import pyarrow.parquet as pq  # core dep; lazy to keep imports light (mirrors replay_chain)

    path = reader._path_for(underlying, day)
    if not path.exists():
        return None
    # FAIL-CLOSED (review DEFECT fix): a corrupt/unreadable parquet, or one missing an
    # expected column, must ABSTAIN (return None) — NEVER raise. This helper is on the
    # PERCEIVE money-path; a single bad day's stored chain must not crash the tick nor
    # break compute_iv_rank_asof's documented "never raises" contract. Read defensively
    # and guard EVERY required column before indexing it.
    try:
        df = pq.read_table(path).to_pandas()
    except Exception:  # noqa: BLE001 — ArrowInvalid (corrupt file) / OSError / etc.
        logger.warning(
            "iv_rank: unreadable chain parquet for %s %s — abstaining this day",
            underlying, day,
        )
        return None
    # A malformed parquet may lack the no-look-ahead / iv columns. Guard presence BEFORE
    # indexing so a missing 'fetched_at'/'asof'/'iv' column abstains instead of KeyError.
    required = {"fetched_at", "asof", "iv"}
    if not required.issubset(set(df.columns)):
        logger.warning(
            "iv_rank: chain parquet for %s %s missing columns %s — abstaining this day",
            underlying, day, sorted(required - set(df.columns)),
        )
        return None
    # Same belt-and-suspenders no-look-ahead filter as data.py:360. The asof<=asof filter
    # is defense-in-depth against a recorder that ever stamps a row's own decision asof in
    # the future relative to the query asof (mirrors replay_chain's dual filter).
    df = df[df["fetched_at"] <= asof]
    df = df[df["asof"] <= asof]
    if len(df) == 0:
        return None
    ivs = [iv for iv in (_finite_iv(v) for v in df["iv"].tolist()) if iv is not None]
    if not ivs:
        return None
    return float(statistics.median(ivs))


def compute_iv_rank_asof(
    symbol: str,
    asof: datetime,
    reader: ChainSnapshotReader | None = None,
    window_days: int = 252,
) -> float | None:
    """As-of-honest IV-rank for ``symbol`` at ``asof`` in ``[0, 100]``, or None (abstain).

    Builds a trailing series of per-day representative IVs over the ``window_days``
    calendar days ending at ``asof`` (inclusive), reading the SAME recorded chain
    parquets ``ChainSnapshotReader.replay_chain`` reads and applying the SAME
    ``fetched_at <= asof`` no-look-ahead filter at every day (a future row is
    excluded — an IV-rank that saw a future high-IV row would be a look-ahead leak).

    IV-rank = ``100 * (count of historical IVs <= current IV) / total`` where the
    *current* IV is the most recent day's representative IV (latest day <= asof with
    data) and the *historical* series is every admissible day-point in the window
    INCLUDING the current day (so a current IV at the series median ranks ~50).

    Returns None (abstain, NEVER raises) when:
      * fewer than ``_MIN_WINDOW_POINTS`` (30) admissible day-points in the window;
      * the current day's parquet is missing / raises ``ChainQualityError``;
      * no admissible finite IV exists.
    """
    reader = reader or ChainSnapshotReader()
    if window_days < 1:
        return None

    end_day = asof.date()
    series: list[tuple[date, float]] = []
    try:
        for offset in range(window_days):
            day = end_day - timedelta(days=offset)
            iv = _representative_iv_for_day(reader, symbol, day, asof)
            if iv is not None:
                series.append((day, iv))
    except ChainQualityError:
        # Fail-closed to abstain: a quality breach on any day -> no honest rank.
        return None
    except FileNotFoundError:
        return None

    if len(series) < _MIN_WINDOW_POINTS:
        return None

    # Current IV = the most recent day's representative IV (largest date <= asof).
    series.sort(key=lambda t: t[0])
    current_iv = series[-1][1]
    ivs = [iv for _, iv in series]

    le_count = sum(1 for iv in ivs if iv <= current_iv)
    rank = 100.0 * le_count / len(ivs)
    # Defensive clamp: arithmetic keeps this in [0, 100] already, but the consumer
    # rejects out-of-range (classify_iv_regime returns None outside 0..100), so a
    # clamp here keeps a float-edge from silently abstaining the regime.
    if rank < 0.0:
        return 0.0
    if rank > 100.0:
        return 100.0
    return rank
