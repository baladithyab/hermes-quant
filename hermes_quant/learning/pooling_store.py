"""d97e — atomic, asof-stamped persistence of the ag03 hierarchical pooler state.

The ag03 :class:`~hermes_quant.learning.hierarchical_pooling.HierarchicalPooler`
keeps per-(analyst, regime, epoch) correctness cells — the effective-n / warm-up
state that drives the headline weighting honesty band — ONLY in memory. A
cron-mode restart therefore loses the whole warm-up band and the operator-visible
"headline still warming up" signal silently resets to cold. This module persists a
snapshot of the pooler cells + per-analyst active-epoch map to a single JSON
artifact and loads it back, so the warm-up state is durable.

This mirrors :mod:`hermes_quant.learning.posterior_store` exactly (atomic
tmp+rename via ``atomic_write_json``, fail-safe loads that degrade to cold-start
on any missing/corrupt file, asof stamping, a module-level default dir tests can
redirect into ``tmp_path``). Consulted ONLY when the EXISTING default-OFF
HERMES_QUANT_HIERARCHICAL_POOLING flag is set (the flag gate lives in the BMA
aggregator), so its mere presence never changes the default-OFF path.

Design notes
------------
- **Atomic writes** reuse ``hermes_quant.artifacts.atomic_write_json`` (tmp +
  rename), so a crash mid-write can never leave a half-written artifact that
  would silently corrupt the warm-up band.
- **Fail-safe loads.** A missing or corrupt artifact leaves the pooler untouched
  (cold-start) and logs at WARNING — it never raises. Money-software must not
  crash the decision path on a bad cache file.
- **Poisoned-row rejection.** A cell with a non-finite or negative alpha/beta is
  skipped (that cell cold-starts) rather than poisoning the warm-up math, exactly
  as ``posterior_store`` rejects bad Beta params.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from hermes_quant.artifacts import QUANT_HOME, atomic_write_json, safe_asset_path
from hermes_quant.learning.hierarchical_pooling import HierarchicalPooler, PoolingCell

logger = logging.getLogger(__name__)

# Default location for the persisted pooler. Overridable by monkeypatching this
# constant in tests, or by passing an explicit ``path`` to save/load.
POOLING_DIR = QUANT_HOME / "ag03_hierarchical_pooling"

SCHEMA_VERSION = 1

# Corrupt state should cold-start that cell, not dominate the warm-up math. This
# bound is far above any realistic offline horizon but rejects poisoned artifacts.
_MAX_COUNT = 10_000_000.0


def _default_path(recipe_key: str | None) -> Path:
    key = safe_asset_path(recipe_key) if recipe_key else "default"
    return POOLING_DIR / f"{key}.json"


def save_pooler_state(
    pooler: HierarchicalPooler,
    *,
    path: Path | None = None,
    asof: pd.Timestamp,
    recipe_key: str | None = None,
) -> Path:
    """Atomically persist the pooler's cells + per-analyst active-epoch map.

    Parameters
    ----------
    pooler:
        The :class:`HierarchicalPooler` whose accumulation state to snapshot.
    path:
        Explicit artifact path. When None, ``POOLING_DIR / <recipe_key>.json``.
    asof:
        The decision/settlement asof at which this snapshot is current.
    recipe_key:
        Optional key disambiguating poolers per recipe/universe when using the
        default directory.

    Returns the path written.
    """
    target = path if path is not None else _default_path(recipe_key)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "ag03_hierarchical_pooling",
        "asof": _as_utc(asof).isoformat(),
        "epoch_of": dict(pooler.epoch_state()),
        "cells": pooler.cells_state(),
    }
    atomic_write_json(target, payload)
    return target


def load_pooler_state(
    pooler: HierarchicalPooler,
    *,
    path: Path | None = None,
    recipe_key: str | None = None,
) -> None:
    """Hydrate ``pooler`` from a persisted snapshot, in place (fail-safe).

    On any missing/corrupt file the pooler is left untouched (cold-start) and a
    WARNING is logged — never raises. Individual poisoned cell rows are skipped
    (that cell cold-starts); the valid cells still load.
    """
    target = path if path is not None else _default_path(recipe_key)
    try:
        if not target.exists():
            return
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — bad cache must not crash decisions
        logger.warning(
            "pooling_store: failed to load %s (%s); leaving pooler cold-start",
            target,
            exc,
        )
        return

    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, list):
        logger.warning(
            "pooling_store: %s has no 'cells' list; leaving pooler cold-start",
            target,
        )
        return

    cells: list[PoolingCell] = []
    for rec in raw_cells:
        try:
            cells.append(_parse_cell(rec))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "pooling_store: skipping corrupt pooling cell %r in %s (%s)",
                rec,
                target,
                exc,
            )
            continue

    raw_epoch = payload.get("epoch_of")
    epoch_of: dict[str, str] = {}
    if isinstance(raw_epoch, dict):
        for analyst, epoch in raw_epoch.items():
            if isinstance(analyst, str) and isinstance(epoch, str):
                epoch_of[analyst] = epoch

    pooler.load_state(cells=cells, epoch_of=epoch_of)


def _parse_cell(rec: Any) -> PoolingCell:
    if not isinstance(rec, dict):
        raise TypeError("cell is not an object")
    analyst = rec["analyst"]
    regime = rec["regime"]
    epoch = rec.get("epoch", "")
    if not isinstance(analyst, str) or not isinstance(regime, str) or not isinstance(epoch, str):
        raise ValueError("analyst/regime/epoch must be strings")
    alpha = _coerce_count("alpha", rec["alpha"])
    beta = _coerce_count("beta", rec["beta"])
    return PoolingCell(analyst=analyst, regime=regime, epoch=epoch, alpha=alpha, beta=beta)


def _coerce_count(field: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be numeric")
    value = float(raw)
    if not math.isfinite(value) or value < 0.0 or value > _MAX_COUNT:
        raise ValueError(f"{field} out of bounds")
    return value


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")
