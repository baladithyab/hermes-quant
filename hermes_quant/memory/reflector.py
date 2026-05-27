"""hermes_quant.memory.reflector — Layer 2: post-trade reflection (ADR-0042).

Gated by env var HERMES_QUANT_REFLECTION=1. Default OFF — bit-identical
pre-Wave-4 behavior when the env var is absent.

The Reflector produces a Reflection dataclass from a closed decision + exit
record. It computes:

  - raw_return, alpha_return  (exit / entry price math)
  - holding_days
  - outcome_quality (1-5) via percentile against a stub alpha distribution
  - tau_observable = max(asof_resolution, asof_decision + holding_days*86400
                         + 6*3600)   # post-close adj-data publication ~6h

Oracle Fallacy note (arxiv:2605.19337 §4.2):
  tau_observable is the wall-clock timestamp at which this episode's outcome
  became knowable. The retriever (Layer 3) MUST NOT surface this reflection
  for any decision whose asof < tau_observable.  The canonical regression test
  for this invariant lives in tests/memory/test_oracle_fallacy.py.

LLM caller:
  Pass ``llm_caller: Callable[[str], str]`` for real LLM reflections.
  When None (default), the stub formatter is used — deterministic, no API
  calls, safe in CI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from hermes_quant.memory.decisions import MEMORY_HOME, _to_utc_iso

logger = logging.getLogger(__name__)

REFLECTIONS_PATH = MEMORY_HOME / "reflections.jsonl"

CURRENT_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Stub historical alpha distribution (v0.1 — hardcoded; replace in v0.2)
# ---------------------------------------------------------------------------

_ALPHA_MEAN: float = 0.0
_ALPHA_STD: float = 0.02  # ≈ 2% per-trade alpha std

# ---------------------------------------------------------------------------
# Lesson category enum
# ---------------------------------------------------------------------------


class LessonCategory(str, Enum):
    thesis_invalidation_at_earnings = "thesis_invalidation_at_earnings"
    regime_shift_invalidation = "regime_shift_invalidation"
    position_sized_too_small = "position_sized_too_small"
    position_sized_too_large = "position_sized_too_large"
    correct_call_too_early = "correct_call_too_early"
    correct_call_too_late = "correct_call_too_late"
    noise_trade_no_lesson = "noise_trade_no_lesson"
    unknown = "unknown"


# ---------------------------------------------------------------------------
# Reflection dataclass
# ---------------------------------------------------------------------------


@dataclass
class Reflection:
    """A single post-trade reflection entry (one row in reflections.jsonl)."""

    schema_version: int
    reflection_id: str
    decision_id: str
    asof_resolution: str       # ISO-8601 UTC — when position closed
    tau_observable: str        # ISO-8601 UTC — when outcome became knowable
    ticker: str
    raw_return: float
    alpha_return: float
    benchmark: str
    holding_days: int
    outcome_quality: int       # 1–5
    reflection_text: str
    lesson_category: str
    reflector_model: str
    reflector_prompt_hash: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _make_reflection_id(asof_resolution: str | datetime, ticker: str, decision_id: str) -> str:
    if isinstance(asof_resolution, datetime):
        ts = asof_resolution.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    else:
        ts = (
            asof_resolution
            .replace(":", "")
            .replace("-", "")
            .replace("Z", "")
            .replace("+00:00", "")[:15]
        )
    suffix = decision_id[-6:] if len(decision_id) >= 6 else decision_id
    return f"ref_{ts}_{ticker.upper()}_{suffix}"


def _compute_outcome_quality(alpha_return: float) -> int:
    """Map alpha return to 1-5 ordinal.

    Percentile-based against stub historical alpha distribution
    (mean=0, std=0.02, v0.1 hardcoded). Bands:

      <= -2σ → 1,  <= -1σ → 2,  <= +0.5σ → 3,
      <= +1σ → 4,  > +1σ  → 5
    """
    z = (alpha_return - _ALPHA_MEAN) / _ALPHA_STD if _ALPHA_STD > 0 else 0.0
    if z <= -2.0:
        return 1
    if z <= -1.0:
        return 2
    if z <= 0.5:
        return 3
    if z <= 1.0:
        return 4
    return 5


def _classify_lesson(alpha_return: float, direction: int, holding_days: int) -> LessonCategory:
    """Heuristic lesson-category assignment (v0.1 stub)."""
    correct = (alpha_return > 0 and direction != 0) or (alpha_return == 0)
    if not correct and holding_days <= 1:
        return LessonCategory.noise_trade_no_lesson
    if not correct and holding_days > 30:
        return LessonCategory.regime_shift_invalidation
    if not correct:
        return LessonCategory.thesis_invalidation_at_earnings
    if correct and holding_days <= 1:
        return LessonCategory.correct_call_too_early
    return LessonCategory.unknown


def _stub_reflection_text(
    direction: int,
    rating: str,
    alpha_return: float,
    outcome_quality: int,
    lesson_category: LessonCategory,
) -> str:
    """Deterministic stub reflection text — no LLM call required."""
    call_correct = "correct" if (alpha_return >= 0) == (direction >= 0) else "wrong"
    direction_label = "long" if direction > 0 else "short" if direction < 0 else "flat"
    return (
        f"Direction call ({direction_label}, {rating}) was {call_correct}; "
        f"alpha {alpha_return:+.1%}. "
        f"Outcome quality {outcome_quality}/5. "
        f"Lesson: {lesson_category.value}."
    )


def _prompt_hash_for_stub(decision_id: str) -> str:
    return "stub:" + hashlib.sha256(f"stub-v0.1:{decision_id}".encode()).hexdigest()[:12]


def _compute_tau_observable(
    asof_resolution: datetime,
    asof_decision: datetime,
    holding_days: int,
) -> datetime:
    """tau_observable = max(asof_resolution,
                           asof_decision + holding_days*86400s + 6h)

    The +6h offset reflects the typical adj-close data publication lag.
    """
    natural = asof_decision + timedelta(seconds=holding_days * 86400 + 6 * 3600)
    return max(asof_resolution, natural)


def _parse_dt(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Reflector class
# ---------------------------------------------------------------------------


class Reflector:
    """Post-trade reflection engine (ADR-0042 Layer 2).

    Parameters
    ----------
    reflections_path:
        Override the default path (for tests).
    llm_caller:
        Optional ``Callable[[prompt_str], reflection_text_str]``.
        When None, the deterministic stub formatter is used — safe in CI,
        no API calls, fully testable.
    model_name:
        Model identifier stored in the reflection row (for audit trail).
    """

    def __init__(
        self,
        reflections_path: Path | None = None,
        llm_caller: Callable[[str], str] | None = None,
        model_name: str = "stub-v0.1",
    ) -> None:
        self._path = reflections_path or REFLECTIONS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()
        self._llm_caller = llm_caller
        self._model_name = model_name

    def reflect_on_close(
        self,
        decision: dict[str, Any],
        exit_record: dict[str, Any],
        benchmark: str = "SPY",
    ) -> Reflection:
        """Produce and persist a Reflection for a closed position.

        Parameters
        ----------
        decision:
            A decision-log row (kind="decision") as returned by DecisionLog.
        exit_record:
            Dict with keys:
              - asof_resolution (str|datetime) : when position closed
              - exit_price      (float)         : close price
              - entry_price     (float)         : open price
              - benchmark_return (float)        : benchmark return over holding period
        benchmark:
            Benchmark ticker label (stored in the reflection row).

        Returns
        -------
        Reflection
            The persisted reflection. Also written to reflections.jsonl.
        """
        asof_dec = _parse_dt(decision.get("asof_decision"))
        asof_res = _parse_dt(exit_record.get("asof_resolution"))

        entry_price = float(exit_record.get("entry_price", 0) or 0)
        exit_price = float(exit_record.get("exit_price", 0) or 0)
        benchmark_return = float(exit_record.get("benchmark_return", 0) or 0)
        direction = int(decision.get("direction", 0))

        # --- compute returns ---
        if entry_price and entry_price != 0:
            raw_return = (exit_price - entry_price) / abs(entry_price)
        else:
            raw_return = 0.0

        # For shorts, returns are inverted relative to price movement
        if direction < 0:
            raw_return = -raw_return

        alpha_return = raw_return - benchmark_return

        # --- holding period ---
        delta = asof_res - asof_dec
        holding_days = max(0, int(delta.total_seconds() / 86400))

        # --- outcome quality ---
        outcome_quality = _compute_outcome_quality(alpha_return)

        # --- lesson category ---
        rating = str(decision.get("rating", ""))
        lesson_category = _classify_lesson(alpha_return, direction, holding_days)

        # --- tau_observable (Oracle Fallacy critical field) ---
        tau_obs = _compute_tau_observable(asof_res, asof_dec, holding_days)

        # --- decision ID ---
        decision_id = str(decision.get("decision_id", "unknown"))
        ticker = str(decision.get("ticker", "UNKNOWN")).upper()

        # --- reflection text ---
        if self._llm_caller is not None:
            prompt = _build_llm_prompt(
                decision=decision,
                raw_return=raw_return,
                alpha_return=alpha_return,
                benchmark=benchmark,
                holding_days=holding_days,
                outcome_quality=outcome_quality,
            )
            try:
                reflection_text = self._llm_caller(prompt)
            except Exception:
                logger.exception("LLM reflector call failed; falling back to stub")
                reflection_text = _stub_reflection_text(
                    direction, rating, alpha_return, outcome_quality, lesson_category
                )
            phash = "llm:" + hashlib.sha256(prompt.encode()).hexdigest()[:12]
        else:
            reflection_text = _stub_reflection_text(
                direction, rating, alpha_return, outcome_quality, lesson_category
            )
            phash = _prompt_hash_for_stub(decision_id)

        # --- build Reflection ---
        reflection_id = _make_reflection_id(asof_res, ticker, decision_id)
        reflection = Reflection(
            schema_version=CURRENT_SCHEMA_VERSION,
            reflection_id=reflection_id,
            decision_id=decision_id,
            asof_resolution=asof_res.isoformat(),
            tau_observable=tau_obs.isoformat(),
            ticker=ticker,
            raw_return=round(raw_return, 8),
            alpha_return=round(alpha_return, 8),
            benchmark=benchmark,
            holding_days=holding_days,
            outcome_quality=outcome_quality,
            reflection_text=reflection_text,
            lesson_category=lesson_category.value,
            reflector_model=self._model_name,
            reflector_prompt_hash=phash,
        )

        self._persist(reflection)
        return reflection

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _persist(self, reflection: Reflection) -> None:
        row = {**asdict(reflection)}
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        with _write_lock:
            with open(self._path, "a", buffering=1) as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        logger.info(
            "reflector: persisted %s decision=%s tau_observable=%s",
            reflection.reflection_id,
            reflection.decision_id,
            reflection.tau_observable,
        )


# ---------------------------------------------------------------------------
# LLM prompt builder (used when llm_caller is provided)
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are reflecting on a closed trade. The decision log entry follows.
Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).

Cover in order:
1. Was the directional call correct? (cite the alpha figure)
2. Which part of the investment thesis held or failed?
3. One concrete lesson to apply to the next similar analysis.

Be specific and terse. Your output will be stored verbatim in a decision log
and re-read by future analysts, so every word must earn its place.

DECISION:
{decision_log_entry}

OUTCOME:
- raw_return: {raw_return:+.2%}
- alpha_return: {alpha_return:+.2%}
- benchmark: {benchmark}
- holding_days: {holding_days}
- outcome_quality: {outcome_quality}/5
"""


def _build_llm_prompt(
    *,
    decision: dict[str, Any],
    raw_return: float,
    alpha_return: float,
    benchmark: str,
    holding_days: int,
    outcome_quality: int,
) -> str:
    safe_decision = {
        k: v for k, v in decision.items()
        if k not in ("schema_version", "kind")
    }
    return _PROMPT_TEMPLATE.format(
        decision_log_entry=json.dumps(safe_decision, indent=2, default=str),
        raw_return=raw_return,
        alpha_return=alpha_return,
        benchmark=benchmark,
        holding_days=holding_days,
        outcome_quality=outcome_quality,
    )
