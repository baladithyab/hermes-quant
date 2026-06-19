"""tests/playbook/test_horizons.py — the 5-rung horizon model (W2).

Single source of truth for the multi-horizon set 0D/1D/7D/14D/30D, each a
(timeframe, DTE-bucket) pair, attached to every profile-fit watchlist entry.
The decision layer picks which rung trades per tick.

These tests RED-prove the byte-identical-OFF guarantee and the no-lookahead
caveat documented in ``playbook/horizons.py``:

  * ``HERMES_QUANT_ZERO_DTE`` OFF (default) -> 0D is absent from
    ``default_horizon_set()`` and ``dte_bucket_for_horizon`` never yields
    the same-day (0, 0) bucket. The 30D rung's (25, 45) bucket is the SAME
    pair the options producer uses today (``recipes._DEFAULT_DTE_MIN/MAX``),
    so a flag-off / 30D-only path is byte-identical to the current fixed-DTE
    options path.
  * ``HERMES_QUANT_ZERO_DTE`` == "1" -> 0D is present and resolves to (0, 0).
  * Every rung's timeframe is either native ('1d') or in
    ``horizon_cache._RESAMPLE_RULES`` ('1w'/'1M'), so no new resample rule is
    needed for 1D-30D.
"""
from __future__ import annotations

import pytest

from hermes_quant.data.horizon_cache import _RESAMPLE_RULES
from hermes_quant.options import recipes
from hermes_quant.playbook import horizons as H

# The canonical 5 rungs, longest-first is NOT assumed; order is the model's own.
_ALL_RUNGS = ["0D", "1D", "7D", "14D", "30D"]
_NON_ZERO_RUNGS = ["1D", "7D", "14D", "30D"]


# ---------------------------------------------------------------------------
# HORIZONS mapping — the single source of truth
# ---------------------------------------------------------------------------


def test_horizons_has_exactly_the_five_rungs():
    """The mapping is exactly the 5 named rungs — no more, no fewer."""
    assert set(H.HORIZONS) == set(_ALL_RUNGS)


@pytest.mark.parametrize(
    ("rung", "timeframe", "dte_min", "dte_max"),
    [
        ("0D", "1d", 0, 0),
        ("1D", "1d", 1, 7),
        ("7D", "1w", 7, 14),
        ("14D", "1w", 14, 30),
        ("30D", "1M", 25, 45),
    ],
)
def test_horizon_spec_tuples(rung, timeframe, dte_min, dte_max):
    """Each rung maps to its agreed (timeframe, dte_min, dte_max)."""
    spec = H.HORIZONS[rung]
    assert spec.timeframe == timeframe
    assert spec.dte_min == dte_min
    assert spec.dte_max == dte_max


def test_zero_dte_rung_is_intraday_and_skips_still_forming_clip():
    """0D is the ONLY intraday rung; it must NOT apply the cs54 still-forming
    bar clip (the still-forming daily bar IS the same-session 0DTE input)."""
    assert H.HORIZONS["0D"].is_intraday is True
    assert H.HORIZONS["0D"].skip_still_forming_clip is True
    # Every non-0D rung is a settled/period-end read -> the clip applies.
    for rung in _NON_ZERO_RUNGS:
        assert H.HORIZONS[rung].is_intraday is False
        assert H.HORIZONS[rung].skip_still_forming_clip is False


def test_all_timeframes_are_native_or_resamplable():
    """No new resample rule is needed: every rung's timeframe is '1d' (native
    passthrough) or in horizon_cache._RESAMPLE_RULES ('1w'/'1M')."""
    for rung in _ALL_RUNGS:
        tf = H.HORIZONS[rung].timeframe
        assert tf == "1d" or tf in _RESAMPLE_RULES, (
            f"rung {rung} timeframe {tf!r} is neither native '1d' nor a "
            f"supported resample rule {sorted(_RESAMPLE_RULES)}"
        )


# ---------------------------------------------------------------------------
# Byte-identical-OFF anchor: 30D == today's fixed options DTE window
# ---------------------------------------------------------------------------


def test_30d_resolves_to_recipes_default_dte_window():
    """The 30D rung resolves to (25, 45) == recipes._DEFAULT_DTE_MIN/MAX so a
    flag-off / 30D-only options path is byte-identical to today."""
    assert H.dte_bucket_for_horizon("30D") == (
        recipes._DEFAULT_DTE_MIN,
        recipes._DEFAULT_DTE_MAX,
    )
    assert H.HORIZONS["30D"].dte_min == recipes._DEFAULT_DTE_MIN
    assert H.HORIZONS["30D"].dte_max == recipes._DEFAULT_DTE_MAX


# ---------------------------------------------------------------------------
# default_horizon_set() — ZERO_DTE flag gating (fail-closed == "1")
# ---------------------------------------------------------------------------


def test_default_set_excludes_zero_dte_when_flag_unset(monkeypatch):
    """OFF (flag absent) -> 0D absent; operator SEES 1D-30D only."""
    monkeypatch.delenv(H._ZERO_DTE_FLAG, raising=False)
    s = H.default_horizon_set()
    assert s == ["1D", "7D", "14D", "30D"]
    assert "0D" not in s


def test_default_set_excludes_zero_dte_when_flag_zero(monkeypatch):
    """OFF (flag == '0') -> 0D absent."""
    monkeypatch.setenv(H._ZERO_DTE_FLAG, "0")
    assert "0D" not in H.default_horizon_set()


@pytest.mark.parametrize("truthy_but_not_one", ["true", "TRUE", "yes", "2", "01", " 1", "1 "])
def test_default_set_fail_closed_only_literal_one_enables(monkeypatch, truthy_but_not_one):
    """Fail-closed: ONLY the literal '1' enables 0D; any other value -> OFF.
    (A typo never silently enables the 0DTE scan/decision path.)"""
    monkeypatch.setenv(H._ZERO_DTE_FLAG, truthy_but_not_one)
    assert "0D" not in H.default_horizon_set()


def test_default_set_includes_zero_dte_when_flag_one(monkeypatch):
    """ON (flag == '1') -> 0D present, ahead of 1D-30D."""
    monkeypatch.setenv(H._ZERO_DTE_FLAG, "1")
    s = H.default_horizon_set()
    assert s == ["0D", "1D", "7D", "14D", "30D"]
    assert s[0] == "0D"


def test_zero_dte_flag_is_canonical_name():
    """The flag constant is the canonical prefixed name so the flag-inventory
    scanner's _CONST/_VIA_CONST regexes pick it up."""
    assert H._ZERO_DTE_FLAG == "HERMES_QUANT_ZERO_DTE"


# ---------------------------------------------------------------------------
# dte_bucket_for_horizon — the DTE resolver wired into recipes kwargs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rung", "bucket"),
    [
        ("1D", (1, 7)),
        ("7D", (7, 14)),
        ("14D", (14, 30)),
        ("30D", (25, 45)),
    ],
)
def test_dte_bucket_for_non_zero_rungs(monkeypatch, rung, bucket):
    """Non-0D rungs resolve their DTE bucket regardless of the ZERO_DTE flag."""
    monkeypatch.delenv(H._ZERO_DTE_FLAG, raising=False)
    assert H.dte_bucket_for_horizon(rung) == bucket


def test_dte_bucket_never_zero_zero_when_flag_off(monkeypatch):
    """OFF -> asking for the 0D bucket raises (0D is not a reachable rung);
    the (0, 0) same-day window can never reach structure_select."""
    monkeypatch.delenv(H._ZERO_DTE_FLAG, raising=False)
    with pytest.raises(ValueError):
        H.dte_bucket_for_horizon("0D")
    # And no OTHER rung secretly yields (0, 0).
    for rung in _NON_ZERO_RUNGS:
        assert H.dte_bucket_for_horizon(rung) != (0, 0)


def test_dte_bucket_zero_zero_only_when_flag_on(monkeypatch):
    """ON -> 0D resolves to the same-day (0, 0) bucket (recipes.by_dte then
    selects only same-day-expiry contracts)."""
    monkeypatch.setenv(H._ZERO_DTE_FLAG, "1")
    assert H.dte_bucket_for_horizon("0D") == (0, 0)


def test_dte_bucket_rejects_unknown_rung():
    """An unknown rung is a hard error, not a silent default."""
    with pytest.raises(ValueError):
        H.dte_bucket_for_horizon("99D")


# ---------------------------------------------------------------------------
# timeframes_for_set — threading into recommend_multi_horizon
# ---------------------------------------------------------------------------


def test_timeframes_for_set_maps_rungs_to_timeframes(monkeypatch):
    """The default set threads to its timeframes for recommend_multi_horizon;
    duplicate timeframes (1D->1d, plus 7D/14D->1w) are de-duplicated in order."""
    monkeypatch.delenv(H._ZERO_DTE_FLAG, raising=False)
    tfs = H.timeframes_for_set(H.default_horizon_set())
    # 1D->1d, 7D->1w, 14D->1w, 30D->1M  => deduped, order-preserving
    assert tfs == ["1d", "1w", "1M"]


def test_timeframes_for_set_with_zero_dte(monkeypatch):
    """With 0D present, its timeframe ('1d') dedupes against the 1D rung's '1d'."""
    monkeypatch.setenv(H._ZERO_DTE_FLAG, "1")
    tfs = H.timeframes_for_set(H.default_horizon_set())
    assert tfs == ["1d", "1w", "1M"]
