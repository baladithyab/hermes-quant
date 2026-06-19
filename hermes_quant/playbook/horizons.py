"""hermes_quant.playbook.horizons — the 5-rung multi-horizon model (W2).

Single source of truth for the horizon set ``0D / 1D / 7D / 14D / 30D``. Each
rung is a ``(timeframe, DTE-bucket)`` pair. ONE profile-fit watchlist entry
carries a ``horizon_set`` (a list of these rung labels); the DECISION layer
(``structure_select`` + the gate) picks WHICH rung trades per tick — the
watchlist never pre-picks a strategy or a horizon.

Why this module exists
----------------------
Today the autonomous tick calls ``advisor_recommend(timeframe=entry.timeframe)``
once, and the multi-leg options producer uses a single fixed DTE window
(``recipes._DEFAULT_DTE_MIN/MAX`` == 25/45). The rearchitecture replaces the
single timeframe with a multi-horizon fan-out and a horizon-aware DTE resolver.
This module is the seam both consumers read from, so the rung-to-timeframe and
rung-to-DTE mappings live in exactly one place.

The mapping (agreed design)
----------------------------
=====  ==========  =============  ==========================================
Rung   timeframe   DTE bucket     note
=====  ==========  =============  ==========================================
0D     1d (intra)  (0, 0)         0DTE same-session; GATED (see below)
1D     1d          (1, 7)         nearest weekly
7D     1w          (7, 14)        the existing W-FRI resample path
14D    1w          (14, 30)       W-FRI resample
30D    1M          (25, 45)       BME resample; == recipes._DEFAULT_DTE_*
=====  ==========  =============  ==========================================

The timeframes ``1d`` / ``1w`` / ``1M`` are ALL already supported:
``1d`` is the native passthrough in ``horizon_cache.resample_to_horizon`` and
``1w`` / ``1M`` are keys of ``horizon_cache._RESAMPLE_RULES``. No new resample
rule is needed for the 1D-30D rungs; only 0D needs its own representation.

Byte-identical-OFF guarantee
----------------------------
The 30D rung resolves to ``(25, 45)`` == ``recipes._DEFAULT_DTE_MIN/MAX``, so a
flag-off / 30D-only options path selects the SAME DTE window the producer uses
today. With ``HERMES_QUANT_ZERO_DTE`` unset, ``default_horizon_set()`` returns
``["1D", "7D", "14D", "30D"]`` (the operator sees 1D-30D) and
``dte_bucket_for_horizon`` can never yield the same-day ``(0, 0)`` window — so
nothing in the existing fixed-DTE path changes behavior.

0DTE gating + the no-lookahead caveat
-------------------------------------
0D is its own rung with DTE bucket ``(0, 0)`` and is OMITTED from a ticker's
horizon set unless ``HERMES_QUANT_ZERO_DTE == "1"`` (its own default-OFF flag,
fail-closed: only the literal ``"1"`` enables it; any other value -> OFF, so a
typo never silently turns on the 0DTE scan/decision path).

NO-LOOKAHEAD CAVEAT (load-bearing): at the 0D rung the decision input is the
INTRADAY, still-forming daily bar (a same-session 0DTE read is correct only if
it reads the live intraday tick). This is the OPPOSITE of every other rung,
where the still-forming trailing bar is a lookahead leak and is dropped by the
cs54 / ADR-0069 clip (``advisor.drop_still_forming_bar``). 0D therefore carries
``is_intraday=True`` and ``skip_still_forming_clip=True`` so its consumer routes
it through a separate intraday read path and does NOT apply the period-end clip
(applying it at 0D would drop the ONLY bar, silencing the rung). Every other
rung is a settled / period-end read: ``is_intraday=False`` and the clip applies
exactly as today.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# ADR-0029 producer DTE default — the 30D rung resolves to this exact window so
# a flag-off / 30D-only options path is byte-identical to today. Imported so the
# equality is asserted in tests against the actual source-of-truth constant
# rather than a hand-copied literal that could drift.
from hermes_quant.options.recipes import _DEFAULT_DTE_MAX, _DEFAULT_DTE_MIN

# ---------------------------------------------------------------------------
# Default-OFF flag (W2 §flag-3): gates 0D/0DTE membership in horizon_set AND the
# (0, 0) DTE bucket reaching structure_select. Module constant in the canonical
# ``_FLAG = "HERMES_QUANT_..."`` form with a quoted-literal default and a
# fail-closed ``== "1"`` check, so the flag-inventory scanner's _CONST / _VIA_CONST
# regexes pick it up and a typo never silently enables a money/scan path.
# OFF -> horizon_set excludes 0D; dte_bucket_for_horizon never returns (0, 0); the
# 30D rung's (25, 45) default stands. OFF == byte-identical to today's fixed-DTE
# options path.
# ---------------------------------------------------------------------------
_ZERO_DTE_FLAG = "HERMES_QUANT_ZERO_DTE"

# The 0DTE rung label (a distinct same-session representation, never folded into 1D).
HORIZON_0DTE = "0D"


def _zero_dte_enabled() -> bool:
    """Fail-closed read of the 0DTE flag: ONLY the literal ``"1"`` enables it.

    Read at call time (never cached at import) so a test / operator env flip
    takes effect without a re-import — mirrors the recipes / options_gate /
    gate idiom.
    """
    return os.environ.get(_ZERO_DTE_FLAG, "0") == "1"


@dataclass(frozen=True)
class HorizonSpec:
    """One rung of the horizon model: a (timeframe, DTE-bucket) pair + the
    intraday / no-lookahead routing flags the consumer needs.

    Attributes:
        rung: the rung label ("0D" | "1D" | "7D" | "14D" | "30D").
        timeframe: the OHLCV timeframe to fan out to recommend_multi_horizon
            ("1d" native passthrough, "1w" / "1M" resample rules).
        dte_min: inclusive lower DTE bound for option-leg selection.
        dte_max: inclusive upper DTE bound for option-leg selection.
        is_intraday: True ONLY for 0D — the decision input is the live,
            still-forming daily bar (same-session 0DTE read).
        skip_still_forming_clip: True ONLY for 0D — the consumer must NOT apply
            the cs54 / ADR-0069 still-forming-bar drop (it would remove the
            only bar). Every settled / period-end rung keeps the clip.
    """

    rung: str
    timeframe: str
    dte_min: int
    dte_max: int
    is_intraday: bool
    skip_still_forming_clip: bool


# ---------------------------------------------------------------------------
# HORIZONS — the single source of truth.
#
# 30D's (25, 45) is intentionally == recipes._DEFAULT_DTE_MIN/MAX (asserted in
# tests). 0D is intraday with the still-forming clip SKIPPED; every other rung
# is a settled / period-end read with the clip applied (is_intraday=False).
# ---------------------------------------------------------------------------
HORIZONS: dict[str, HorizonSpec] = {
    HORIZON_0DTE: HorizonSpec(
        rung=HORIZON_0DTE,
        timeframe="1d",  # intraday read of the still-forming daily bar
        dte_min=0,
        dte_max=0,
        is_intraday=True,
        skip_still_forming_clip=True,
    ),
    "1D": HorizonSpec(
        rung="1D",
        timeframe="1d",
        dte_min=1,
        dte_max=7,
        is_intraday=False,
        skip_still_forming_clip=False,
    ),
    "7D": HorizonSpec(
        rung="7D",
        timeframe="1w",  # the existing W-FRI resample path
        dte_min=7,
        dte_max=14,
        is_intraday=False,
        skip_still_forming_clip=False,
    ),
    "14D": HorizonSpec(
        rung="14D",
        timeframe="1w",
        dte_min=14,
        dte_max=30,
        is_intraday=False,
        skip_still_forming_clip=False,
    ),
    "30D": HorizonSpec(
        rung="30D",
        timeframe="1M",  # the existing BME resample path
        dte_min=_DEFAULT_DTE_MIN,  # == 25, so flag-off / 30D-only is byte-identical
        dte_max=_DEFAULT_DTE_MAX,  # == 45
        is_intraday=False,
        skip_still_forming_clip=False,
    ),
}

# Deterministic rung order: 0D first (when present), then ascending horizon.
_RUNG_ORDER: tuple[str, ...] = ("0D", "1D", "7D", "14D", "30D")


def default_horizon_set() -> list[str]:
    """The default multi-horizon set attached to every profile-fit watchlist entry.

    Returns ``["1D", "7D", "14D", "30D"]`` by default (the operator SEES 1D-30D).
    When ``HERMES_QUANT_ZERO_DTE == "1"`` it becomes ``["0D", "1D", "7D", "14D",
    "30D"]`` (0D leads). Deterministic order (``_RUNG_ORDER``).

    Read at call time so the flag flip takes effect without a re-import.
    """
    include_zero = _zero_dte_enabled()
    out: list[str] = []
    for rung in _RUNG_ORDER:
        if rung == HORIZON_0DTE and not include_zero:
            continue
        out.append(rung)
    return out


def dte_bucket_for_horizon(rung: str) -> tuple[int, int]:
    """Resolve a rung to its ``(dte_min, dte_max)`` window for structure_select.

    Wired into ``build_and_persist_multi_leg(dte_min=, dte_max=)`` so the chosen
    rung picks the option-leg DTE window. The 30D rung resolves to ``(25, 45)``
    == ``recipes._DEFAULT_DTE_MIN/MAX``, so flag-off / 30D-only is byte-identical.

    0D resolves to ``(0, 0)`` (same-day expiry) — but ONLY when
    ``HERMES_QUANT_ZERO_DTE == "1"``. With the flag OFF, asking for the 0D bucket
    raises ``ValueError`` (0D is not a reachable rung), so the same-day window can
    never reach structure_select.

    Raises:
        ValueError: if ``rung`` is unknown, or if it is "0D" while the ZERO_DTE
            flag is OFF.
    """
    if rung == HORIZON_0DTE and not _zero_dte_enabled():
        raise ValueError(
            f"{rung!r} is gated behind {_ZERO_DTE_FLAG}=1 and is not a reachable "
            "rung; the (0, 0) same-day DTE bucket must not reach structure_select"
        )
    spec = HORIZONS.get(rung)
    if spec is None:
        raise ValueError(
            f"unknown horizon rung {rung!r}; supported: {sorted(HORIZONS)}"
        )
    return (spec.dte_min, spec.dte_max)


def timeframes_for_set(horizon_set: list[str]) -> list[str]:
    """Map a horizon set to its de-duplicated, order-preserving timeframes.

    Threads ``entry.horizon_set`` into ``recommend_multi_horizon(symbol,
    horizons=...)``: the default set ``["1D", "7D", "14D", "30D"]`` maps to
    ``["1d", "1w", "1M"]`` (7D and 14D both resolve to "1w" -> de-duplicated).
    ``recommend_multi_horizon`` itself also dedupes, but we dedupe here so the
    threaded list is already canonical.

    Raises:
        ValueError: if any rung in ``horizon_set`` is unknown.
    """
    out: list[str] = []
    for rung in horizon_set:
        spec = HORIZONS.get(rung)
        if spec is None:
            raise ValueError(
                f"unknown horizon rung {rung!r}; supported: {sorted(HORIZONS)}"
            )
        if spec.timeframe not in out:
            out.append(spec.timeframe)
    return out


__all__ = [
    "HORIZONS",
    "HORIZON_0DTE",
    "HorizonSpec",
    "default_horizon_set",
    "dte_bucket_for_horizon",
    "timeframes_for_set",
]
