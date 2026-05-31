"""hermes_quant.reconcile.pmcc_shadow — PMCC counterfactual validator (ADR-0029 §2.7).

For each recorded PMCC shadow position (``shadow.pmcc.load_pmcc_positions``), mark it
to model (``mark_pmcc``) at today's spot and compare the MODEL
``net_value``/``net_delta``/``net_theta_day`` against the reactor's REAL per-leg marks,
joined on ``note == multi_leg_id``. Returns per-position divergence rows for the
ADR-0029 D7 60-day evidence window.

The ``net_theta_day`` SIGN is the structural sanity check: a 'pmcc' marking
net-NEGATIVE theta from the model is a build bug (a real PMCC collects net theta), so it
is surfaced as a divergence with ``severity='build_bug_suspected'``.

Pure read/compare; writes nothing to executions/state. Consumes the
``note=multi_leg_id`` join the reactor stamped at fill time (research §4.2). This is
the daily counterfactual that "activates implicitly once the multi-leg reactor lands".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from hermes_quant.shadow.pmcc import load_pmcc_positions, mark_pmcc


@dataclass(frozen=True)
class PMCCShadowDivergence:
    """One PMCC shadow position marked-to-model and joined to the real per-leg mark."""

    multi_leg_id: str
    symbol: str
    asof: str
    model_net_value: float
    model_net_delta: float
    model_net_theta_day: float
    real_net_value: float | None  # None when no real mark was supplied for this id
    net_value_divergence: float | None  # model - real (None when real is missing)
    severity: str  # 'ok' | 'build_bug_suspected' | 'real_mark_missing'


def reconcile_pmcc_shadow(
    *,
    asof: date,
    spot_by_symbol: dict[str, float],
    real_marks_by_mleg_id: dict[str, float],
    path: Path | None = None,
) -> list[PMCCShadowDivergence]:
    """Mark each recorded PMCC shadow and compare to the reactor's real per-leg marks.

    Args:
        asof: the date to mark at.
        spot_by_symbol: today's spot per underlying.
        real_marks_by_mleg_id: the reactor's REAL summed per-leg ``market_value``,
            keyed by ``multi_leg_id`` (== the shadow position's ``note``).
        path: override the shadow store path (tests / non-default home).

    Returns:
        One ``PMCCShadowDivergence`` per recorded PMCC shadow position (those whose
        ``note`` is a multi_leg_id — i.e. reactor-stamped). Positions without a spot
        for their symbol are skipped (cannot mark without a spot).
    """
    out: list[PMCCShadowDivergence] = []
    for pos in load_pmcc_positions(path=path):
        mleg_id = pos.note
        if not mleg_id:
            continue  # not reactor-stamped (e.g. a Phase-1 prose note)
        spot = spot_by_symbol.get(pos.symbol)
        if spot is None or spot <= 0:
            continue  # cannot mark without a spot
        model = mark_pmcc(pos, spot=spot, asof=asof)
        real = real_marks_by_mleg_id.get(mleg_id)

        # Structural sanity: a real PMCC collects net theta (>0). A model marking
        # net-NEGATIVE theta is a build bug (wrong leg sign / inverted structure).
        if model.net_theta_day < 0:
            severity = "build_bug_suspected"
        elif real is None:
            severity = "real_mark_missing"
        else:
            severity = "ok"

        divergence = None if real is None else (model.net_value - real)
        out.append(
            PMCCShadowDivergence(
                multi_leg_id=mleg_id,
                symbol=pos.symbol,
                asof=model.asof,
                model_net_value=model.net_value,
                model_net_delta=model.net_delta,
                model_net_theta_day=model.net_theta_day,
                real_net_value=real,
                net_value_divergence=divergence,
                severity=severity,
            )
        )
    return out
