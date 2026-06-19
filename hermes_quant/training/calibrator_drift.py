"""hermes_quant.training.calibrator_drift — calibrator drift detection (B11).

Detects when the live ``IsotonicCalibrator`` has drifted from realized
outcomes:
  (a) emit an alert when the mean ``|raw → calibrated|`` vs realized-hit-rate
      gap exceeds a threshold (default 5%), and
  (b) OPTIONALLY auto-refit the live calibrator — gated behind
      ``HERMES_QUANT_CALIBRATOR_AUTO_REFIT=1`` (read at the cron layer).

Posture (AGENTS.md):
  * Detection/alert is ALWAYS safe — read-only, never touches the live pickle.
  * Silence-by-default: zero samples ⇒ ``drift=0.0``, ``should_alert=False``,
    ``reason='no_samples'`` (no data ⇒ no alarm).
  * The refit that swaps the live calibrator pickle (what the risk gate then
    sees) is the ONLY behavior-changing path and is default-OFF. With
    ``auto_refit=False`` the live pickle is NEVER mutated.

The drift metric is the population-level ECE-style gap the ``calibrators.py``
module docstring already prescribes: ``E[direction_correct | calibrated]`` vs
the calibrated probability. Bucketed-ECE is a stretch goal, not implemented.

ADR refs: ADR-0009 §P0-2 (calibration drift surfaced in quant_doctor).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)

DRIFT_LOG_PATH = (
    _resolve_quant_home() / "calibrators" / "drift-log.jsonl"
)

_DEFAULT_DRIFT_THRESHOLD = 0.05  # 5% predicted-vs-realized gap
_DRIFT_LOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DriftResult:
    """Outcome of one drift computation."""

    drift: float                 # abs(predicted_mean - realized_mean)
    predicted_mean: float
    realized_mean: float
    n_samples: int
    threshold: float
    should_alert: bool
    refit_recommended: bool      # should_alert AND n_samples >= min_refit_samples
    reason: str


def compute_drift(
    calibrator: Any,
    raw_scores: Sequence[float],
    direction_correct: Sequence[bool],
    *,
    threshold: float = _DEFAULT_DRIFT_THRESHOLD,
    min_refit_samples: int = 200,
) -> DriftResult:
    """Pure drift metric over paired ``(raw_scores, direction_correct)``.

    For each sample: ``calibrated_i = calibrator.calibrate(raw_i)``.
      * ``predicted = mean(calibrated_i)`` — what the calibrator claims.
      * ``realized  = mean(direction_correct)`` — the empirical hit rate.
      * ``drift     = abs(predicted - realized)``.
      * ``should_alert = drift > threshold``.
      * ``refit_recommended = should_alert and n_samples >= min_refit_samples``.

    Silence-by-default: ``n_samples == 0`` ⇒ ``drift=0.0``,
    ``should_alert=False``, ``reason='no_samples'``.
    """
    raw_list = list(raw_scores)
    correct_list = list(direction_correct)
    n = min(len(raw_list), len(correct_list))

    if n == 0:
        return DriftResult(
            drift=0.0,
            predicted_mean=0.0,
            realized_mean=0.0,
            n_samples=0,
            threshold=threshold,
            should_alert=False,
            refit_recommended=False,
            reason="no_samples",
        )

    calibrated = [float(calibrator.calibrate(float(raw_list[i]))) for i in range(n)]
    predicted_mean = sum(calibrated) / n
    realized_mean = sum(1.0 if bool(correct_list[i]) else 0.0 for i in range(n)) / n
    drift = abs(predicted_mean - realized_mean)
    should_alert = drift > threshold
    refit_recommended = should_alert and n >= min_refit_samples

    if should_alert:
        reason = (
            f"drift={drift:.4f} > threshold={threshold:.4f} "
            f"(predicted={predicted_mean:.4f}, realized={realized_mean:.4f}, n={n})"
        )
    else:
        reason = (
            f"drift={drift:.4f} <= threshold={threshold:.4f} "
            f"(predicted={predicted_mean:.4f}, realized={realized_mean:.4f}, n={n})"
        )

    return DriftResult(
        drift=drift,
        predicted_mean=predicted_mean,
        realized_mean=realized_mean,
        n_samples=n,
        threshold=threshold,
        should_alert=should_alert,
        refit_recommended=refit_recommended,
        reason=reason,
    )


def append_drift_log(
    result: DriftResult,
    *,
    path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON row to the drift log. NEVER raises (logs at WARNING).

    Row: ``schema_version=1``, ``asof`` (UTC now ISO-8601), all DriftResult
    fields, plus any ``extra`` keys (e.g. ``refit=True``).
    """
    target = path or DRIFT_LOG_PATH
    try:
        row: dict[str, Any] = {
            "schema_version": _DRIFT_LOG_SCHEMA_VERSION,
            "asof": datetime.now(UTC).isoformat(),
        }
        row.update(asdict(result))
        if extra:
            row.update(extra)
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:  # noqa: BLE001
        logger.warning("append_drift_log failed (%s); continuing.", exc)


def _load_calibrator(calibrator_path: Path) -> Any:
    """Load a pickled calibrator. Lazy import; returns None on any failure.

    Trust note: the calibrator pickle is written ONLY by
    ``bootstrap_calibrator._atomic_pickle`` to a local, operator-owned path
    (``~/.hermes/quant/calibrators/isotonic.pkl``). It is never sourced from a
    network or untrusted location, so the pickle load below is safe and matches
    the existing read path in ``aggregators.bma`` / the bootstrap counterpart.
    """
    import pickle  # nosec B403 — local, operator-owned artifact (see docstring)

    if not calibrator_path.exists():
        logger.warning("calibrator pickle not found at %s", calibrator_path)
        return None
    try:
        with open(calibrator_path, "rb") as fh:
            return pickle.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to load calibrator from %s: %s", calibrator_path, exc)
        return None


def run_drift_check(
    *,
    calibrator_path: Path | None = None,
    pairs: tuple[Sequence[float], Sequence[bool]] | None = None,
    auto_refit: bool = False,
    threshold: float = _DEFAULT_DRIFT_THRESHOLD,
    min_refit_samples: int = 200,
    refit_kwargs: dict[str, Any] | None = None,
    drift_log_path: Path | None = None,
) -> DriftResult:
    """Load the live calibrator, compute drift over ``pairs``, append the log.

    If ``auto_refit`` AND ``result.refit_recommended``: call
    ``bootstrap_calibrator(**refit_kwargs)`` to re-fit + atomic-replace the
    pickle, and record ``refit=True`` in the drift log. When ``auto_refit`` is
    False the live pickle is NEVER touched (default-OFF behavior, identical to
    today).

    ``pairs`` is the test seam: pass ``(raw_scores, direction_correct)``
    directly to avoid any network. When ``pairs is None`` and no upstream
    collection is wired, the check degrades to ``no_samples`` (silence).
    """
    from hermes_quant.training.bootstrap_calibrator import (
        DEFAULT_CALIBRATOR_PATH,
    )

    cal_path = calibrator_path or DEFAULT_CALIBRATOR_PATH
    calibrator = _load_calibrator(cal_path)

    if calibrator is None or pairs is None:
        result = DriftResult(
            drift=0.0,
            predicted_mean=0.0,
            realized_mean=0.0,
            n_samples=0,
            threshold=threshold,
            should_alert=False,
            refit_recommended=False,
            reason="no_calibrator" if calibrator is None else "no_samples",
        )
        append_drift_log(result, path=drift_log_path)
        return result

    raw_scores, direction_correct = pairs
    result = compute_drift(
        calibrator,
        raw_scores,
        direction_correct,
        threshold=threshold,
        min_refit_samples=min_refit_samples,
    )

    refit_done = False
    if auto_refit and result.refit_recommended:
        from hermes_quant.training.bootstrap_calibrator import bootstrap_calibrator

        kwargs = dict(refit_kwargs or {})
        kwargs.setdefault("output_path", cal_path)
        try:
            refit_result = bootstrap_calibrator(**kwargs)
            refit_done = bool(refit_result.get("fitted", False))
            logger.info("calibrator auto-refit completed: %s", refit_result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("calibrator auto-refit failed (%s); leaving live pickle.", exc)

    append_drift_log(result, path=drift_log_path, extra={"refit": refit_done})
    return result


# Re-exported for test convenience / atomic-write reuse parity.
def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic text write (POSIX rename). Used by tooling; not on the hot path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = [
    "DRIFT_LOG_PATH",
    "DriftResult",
    "append_drift_log",
    "compute_drift",
    "run_drift_check",
]
