"""c96e — atomic, asof-stamped persistence of per-analyst Beta posteriors.

The BMA aggregator keeps per-analyst Beta(alpha, beta) skill posteriors in
memory. Today they are rebuilt from the prior every time ``advisor.recommend()``
constructs a fresh aggregator, so learned skill never survives a restart. This
module persists a snapshot of those posteriors to a single JSON artifact and
loads it back, so skill estimates are durable.

Design notes
------------
- **Atomic writes** reuse ``hermes_quant.artifacts.atomic_write_json`` (tmp +
  rename), so a crash mid-write can never leave a half-written artifact that
  would silently corrupt skill estimates.
- **Fail-safe loads.** A missing or corrupt artifact yields ``{}`` (treated as
  cold-start by the caller) and is logged at WARNING — it never raises. This
  mirrors how ``BMAAggregator._load_calibrator`` degrades to ColdStart on a bad
  pickle: money-software must not crash the decision path on a bad cache file.
- **asof stamping.** Every artifact records the asof at which it was written so
  operators (and future staleness logic) can reason about how current the
  skill estimates are.
- The default artifact directory is a module-level constant so tests can
  redirect it into ``tmp_path`` (see the autouse fixture in
  ``tests/learning/conftest.py``) and never touch the real ``~/.hermes``.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from hermes_quant.artifacts import QUANT_HOME, atomic_write_json, safe_asset_path

logger = logging.getLogger(__name__)

# Default location for persisted posteriors. Overridable by monkeypatching this
# constant in tests, or by passing an explicit ``path`` to save/load.
POSTERIOR_DIR = QUANT_HOME / "l2_learning_posteriors"

SCHEMA_VERSION = 1

# Corrupt state should cold-start, not dominate BMA weights. These bounds are
# far above any realistic offline training horizon but reject poisoned artifacts.
_MAX_BETA_PARAM = 10_000_000.0
_MAX_N_OBSERVATIONS = 10_000_000


@dataclass(frozen=True)
class PersistedPosterior:
    """One analyst's persisted Beta posterior plus recency metadata.

    ``last_observable_asof`` is the observability timestamp of the most recent
    sample folded into this posterior — kept so a recency-decay refit can reason
    about staleness without re-reading the full sample history.

    ``decay_samples`` is the bounded ``(observable_asof, correct)`` ring the
    recency refit consumes. It MUST be persisted when the decay flag is in use:
    the refit ignores the summary (alpha, beta) and recomputes from this ring, so
    a reloaded aggregator with an empty ring would silently collapse a skilled
    analyst's weight to the prior mean. Default empty for the persistence-only
    (decay-off) case, where the ring is unused.
    """

    alpha: float
    beta: float
    n_observations: int
    last_observable_asof: pd.Timestamp | None = None
    decay_samples: tuple[tuple[pd.Timestamp, bool], ...] = ()


def _default_path(recipe_key: str | None) -> Path:
    key = safe_asset_path(recipe_key) if recipe_key else "default"
    return POSTERIOR_DIR / f"{key}.json"


def save_posteriors(
    posteriors: dict[str, PersistedPosterior],
    *,
    path: Path | None = None,
    asof: pd.Timestamp,
    recipe_key: str | None = None,
) -> Path:
    """Atomically persist a per-analyst posterior snapshot.

    Parameters
    ----------
    posteriors:
        ``{analyst_name: PersistedPosterior}``.
    path:
        Explicit artifact path. When None, ``POSTERIOR_DIR / <recipe_key>.json``
        is used.
    asof:
        The decision/settlement asof at which this snapshot is current. Stamped
        into the payload.
    recipe_key:
        Optional key disambiguating posteriors per recipe/universe when using
        the default directory.

    Returns the path written.
    """
    target = path if path is not None else _default_path(recipe_key)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "l2_learning_posteriors",
        "asof": _as_utc(asof).isoformat(),
        "posteriors": {
            name: {
                "alpha": float(p.alpha),
                "beta": float(p.beta),
                "n_observations": int(p.n_observations),
                "last_observable_asof": (
                    _as_utc(p.last_observable_asof).isoformat()
                    if p.last_observable_asof is not None
                    else None
                ),
                # Recency-refit sample ring: [[observable_asof_iso, correct], ...]
                "decay_samples": [
                    [_as_utc(ts).isoformat(), bool(correct)]
                    for ts, correct in p.decay_samples
                ],
            }
            for name, p in posteriors.items()
        },
    }
    atomic_write_json(target, payload)
    return target


def load_posteriors(
    *,
    path: Path | None = None,
    recipe_key: str | None = None,
) -> dict[str, PersistedPosterior]:
    """Load a posterior snapshot, or return ``{}`` on any missing/corrupt file.

    Never raises — a bad cache file degrades to cold-start, logged at WARNING.
    """
    target = path if path is not None else _default_path(recipe_key)
    try:
        if not target.exists():
            return {}
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — bad cache must not crash decisions
        logger.warning(
            "posterior_store: failed to load %s (%s); treating as cold-start",
            target,
            exc,
        )
        return {}

    raw = payload.get("posteriors")
    if not isinstance(raw, dict):
        logger.warning(
            "posterior_store: %s has no 'posteriors' map; treating as cold-start",
            target,
        )
        return {}

    out: dict[str, PersistedPosterior] = {}
    for name, rec in raw.items():
        try:
            parsed = _parse_posterior_row(rec)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "posterior_store: skipping corrupt posterior row for %r in %s (%s)",
                name,
                target,
                exc,
            )
            continue
        out[name] = parsed
    return out


def _parse_posterior_row(rec: Any) -> PersistedPosterior:
    if not isinstance(rec, dict):
        raise TypeError("row is not an object")

    alpha = _coerce_beta_param("alpha", rec["alpha"])
    beta = _coerce_beta_param("beta", rec["beta"])
    n_observations = _coerce_observation_count(rec.get("n_observations", 0))
    last = _coerce_optional_timestamp("last_observable_asof", rec.get("last_observable_asof"))
    decay_samples = _coerce_decay_samples(rec.get("decay_samples") or [])
    return PersistedPosterior(
        alpha=alpha,
        beta=beta,
        n_observations=n_observations,
        last_observable_asof=last,
        decay_samples=decay_samples,
    )


def _coerce_beta_param(field: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be numeric")
    value = float(raw)
    if not math.isfinite(value) or value <= 0.0 or value > _MAX_BETA_PARAM:
        raise ValueError(f"{field} out of bounds")
    return value


def _coerce_observation_count(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("n_observations must be an integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            raise ValueError("n_observations must be a finite integer")
        value = int(raw)
    elif isinstance(raw, str):
        as_float = float(raw)
        if not math.isfinite(as_float) or not as_float.is_integer():
            raise ValueError("n_observations must be a finite integer")
        value = int(as_float)
    else:
        raise ValueError("n_observations must be an integer")
    if value < 0 or value > _MAX_N_OBSERVATIONS:
        raise ValueError("n_observations out of bounds")
    return value


def _coerce_optional_timestamp(field: str, raw: Any) -> pd.Timestamp | None:
    if raw is None:
        return None
    ts = pd.Timestamp(raw)
    if pd.isna(ts):
        raise ValueError(f"{field} is not finite")
    return ts


def _coerce_decay_samples(raw: Any) -> tuple[tuple[pd.Timestamp, bool], ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("decay_samples must be a list")
    samples: list[tuple[pd.Timestamp, bool]] = []
    for sample in raw:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError("decay sample must be [timestamp, correct]")
        ts_raw, correct = sample
        if not isinstance(correct, bool):
            raise ValueError("decay sample correctness must be boolean")
        ts = _coerce_optional_timestamp("decay sample timestamp", ts_raw)
        if ts is None:
            raise ValueError("decay sample timestamp is missing")
        samples.append((ts, correct))
    return tuple(samples)


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")
