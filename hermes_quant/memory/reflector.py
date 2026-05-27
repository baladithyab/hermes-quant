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

LLM caller (v0.1 — generic Callable):
  Pass ``llm_caller: Callable[[str], str]`` for real LLM reflections.
  When None (default), the stub formatter is used — deterministic, no API
  calls, safe in CI.

LLM caller (v0.2 — LLMCaller structured path, ADR-0057):
  Pass an ``LLMCaller`` instance as ``llm_caller``.  When
  HERMES_QUANT_REFLECTOR_LLM=1 and llm_caller.available() is True, a
  structured Pydantic schema (ReflectionLLMOutput) is sent to the LLM for
  reflection_text + lesson_category.  Falls back to v0.1 stub on any failure.

  Self-grade refusal invariant (ADR-0042 §anti-patterns / ADR-0057 §4):
    If decision['llm_committee_model_id'] == llm_caller.model_id, the v0.2
    path is REFUSED and v0.1 stub is used with a WARNING.  The PM model that
    made the decision MUST NOT reflect on its own outcome.

  Oracle Fallacy guard preserved (ADR-0057 §5):
    tau_observable is ALWAYS computed deterministically.  If the LLM response
    tries to set an earlier tau_observable, the Reflector overrides it.
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
from typing import Any, Callable, Optional

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
# Pydantic schema for v0.2 LLM structured output (I/O boundary only)
# ---------------------------------------------------------------------------

try:
    from pydantic import BaseModel, Field as PydanticField

    class ReflectionLLMOutput(BaseModel):
        """Pydantic model used ONLY as the LLM I/O boundary in v0.2.

        The LLM is asked to emit exactly two fields: a 2-4 sentence prose
        reflection_text and a lesson_category from the canonical enum.
        tau_observable is NEVER delegated to the LLM — it is always computed
        deterministically by the Reflector (Oracle Fallacy guard, ADR-0057 §5).
        """
        reflection_text: str = PydanticField(
            description=(
                "2-4 sentences of plain prose (no bullets, no headers, no markdown) "
                "covering: (1) was the directional call correct (cite alpha), "
                "(2) which part of thesis held or failed, (3) one concrete lesson."
            )
        )
        lesson_category: str = PydanticField(
            description=(
                "One of: thesis_invalidation_at_earnings, regime_shift_invalidation, "
                "position_sized_too_small, position_sized_too_large, "
                "correct_call_too_early, correct_call_too_late, "
                "noise_trade_no_lesson, unknown"
            )
        )

    _PYDANTIC_AVAILABLE = True

except ImportError:  # pragma: no cover
    _PYDANTIC_AVAILABLE = False
    ReflectionLLMOutput = None  # type: ignore[assignment,misc]


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


def _normalize_lesson_category(value: str) -> LessonCategory:
    """Coerce an LLM-returned string into a LessonCategory, defaulting to unknown.

    MoA review F5 (Claude I3): unknown values are coerced to LessonCategory.unknown
    but the caller logs `raw_lesson_category` separately so we have provenance
    for prompt-injection / model-misbehavior detection.
    """
    try:
        return LessonCategory(value)
    except (ValueError, KeyError, TypeError):
        return LessonCategory.unknown


def _normalize_model_id(model_id: str | None) -> str:
    """Normalize a model_id for self-grade-refusal comparison.

    MoA review F1 (Claude I1): exact-string equality is fragile against:
      - Provider-prefix asymmetry: "openai/gpt-4.1" vs "gpt-4.1"
      - Case differences: "OpenAI/..." vs "openai/..."
      - Dated-suffix variants: "openai/gpt-4.1-2025-04-14" vs "openai/gpt-4.1"

    Normalization steps:
      1. None / empty -> empty string
      2. Lowercase
      3. Strip provider prefix (everything before the last "/")
      4. Strip dated suffix matching -YYYY-MM-DD or -YYYYMMDD

    >>> _normalize_model_id("openai/gpt-4.1")
    'gpt-4.1'
    >>> _normalize_model_id("OpenAI/GPT-4.1-2025-04-14")
    'gpt-4.1'
    >>> _normalize_model_id("gpt-4.1")
    'gpt-4.1'
    >>> _normalize_model_id(None)
    ''
    """
    if not model_id:
        return ""
    import re as _re
    norm = model_id.strip().lower()
    # Drop provider prefix
    if "/" in norm:
        norm = norm.rsplit("/", 1)[-1]
    # Drop dated suffix
    norm = _re.sub(r"-\d{4}-\d{2}-\d{2}$", "", norm)
    norm = _re.sub(r"-\d{8}$", "", norm)
    return norm


# ---------------------------------------------------------------------------
# Audit-log helper for reflector_llm_call events
# ---------------------------------------------------------------------------


def _audit_reflector_call(path_kind: str, extra: dict[str, Any]) -> None:
    """Append a reflector_llm_call event to the audit log. Never raises."""
    try:
        import uuid as _uuid
        from hermes_quant.governance.audit_log import (
            AUDIT_LOG_PATH,
            CURRENT_SCHEMA_VERSION as _AV,
            _write_lock as _awl,
        )
        import os as _os

        row = {
            "event_id": str(_uuid.uuid4()),
            "kind": "reflector_llm_call",
            "schema_version": _AV,
            "asof": datetime.now(UTC).isoformat(),
            "source": "hermes_quant.memory.reflector",
            "payload": {"path_kind": path_kind, **extra},
        }
        line = json.dumps(row, sort_keys=True, default=str)
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _awl:
            with open(AUDIT_LOG_PATH, "a", buffering=1) as f:
                f.write(line + "\n")
                f.flush()
                _os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reflector: audit_append failed (%s); continuing.", exc)


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
        v0.1 path: Optional ``Callable[[prompt_str], reflection_text_str]``.
        v0.2 path: Optional ``LLMCaller`` instance (from
        hermes_quant.agents.llm_caller).  The v0.2 structured-output path
        is activated when HERMES_QUANT_REFLECTOR_LLM=1 and
        llm_caller.available() returns True.
        When None (or either gate is closed), the deterministic stub
        formatter is used — safe in CI, no API calls, fully testable.
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

        # --- tau_observable (Oracle Fallacy critical field — always deterministic) ---
        tau_obs = _compute_tau_observable(asof_res, asof_dec, holding_days)

        # --- decision ID ---
        decision_id = str(decision.get("decision_id", "unknown"))
        ticker = str(decision.get("ticker", "UNKNOWN")).upper()

        # --- reflection text + lesson_category (v0.2 path or v0.1 stub) ---
        llm_result = self._reflect_with_llm(
            decision=decision,
            exit_record=exit_record,
            benchmark=benchmark,
            raw_return=raw_return,
            alpha_return=alpha_return,
            holding_days=holding_days,
            outcome_quality=outcome_quality,
            asof_res=asof_res,
            asof_dec=asof_dec,
            tau_obs_deterministic=tau_obs,
            decision_id=decision_id,
        )

        if llm_result is not None:
            reflection_text, lesson_category_out, phash = llm_result
        else:
            # v0.1 legacy path: check for old-style plain Callable llm_caller
            if self._llm_caller is not None and not _is_llm_caller_instance(self._llm_caller):
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
                lesson_category_out = lesson_category
            else:
                reflection_text = _stub_reflection_text(
                    direction, rating, alpha_return, outcome_quality, lesson_category
                )
                phash = _prompt_hash_for_stub(decision_id)
                lesson_category_out = lesson_category

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
            lesson_category=lesson_category_out.value if isinstance(lesson_category_out, LessonCategory) else str(lesson_category_out),
            reflector_model=self._model_name,
            reflector_prompt_hash=phash,
        )

        self._persist(reflection)
        return reflection

    # ------------------------------------------------------------------
    # v0.2 LLM path
    # ------------------------------------------------------------------

    def _reflect_with_llm(
        self,
        *,
        decision: dict[str, Any],
        exit_record: dict[str, Any],
        benchmark: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        outcome_quality: int,
        asof_res: datetime,
        asof_dec: datetime,
        tau_obs_deterministic: datetime,
        decision_id: str,
    ) -> Optional[tuple[str, LessonCategory, str]]:
        """Attempt the v0.2 LLM-structured-output reflection path.

        Returns
        -------
        (reflection_text, lesson_category, prompt_hash) on success, else None.

        Gates (all must be True):
          (a) self._llm_caller is a LLMCaller instance (not a plain Callable)
          (b) HERMES_QUANT_REFLECTOR_LLM=1
          (c) self._llm_caller.available() returns True

        Self-grade refusal invariant (ADR-0042 / ADR-0057):
          If decision['llm_committee_model_id'] == self._llm_caller.model_id
          → refuse, fall back to v0.1, emit WARNING + audit record
          'v02_self_grade_refused'.

        Oracle Fallacy guard (ADR-0057 §5):
          tau_observable is ALWAYS taken from the deterministic helper.
          The LLM result does NOT carry tau_observable — it only provides
          reflection_text + lesson_category.  This prevents any LLM from
          embedding future knowledge via a crafted timestamp.
        """
        # Gate (a): must be a LLMCaller-compatible instance
        if not _is_llm_caller_instance(self._llm_caller):
            return None

        # Gate (b): feature flag
        if os.environ.get("HERMES_QUANT_REFLECTOR_LLM", "0") != "1":
            _audit_reflector_call(
                "v01_stub_text",
                {"decision_id": decision_id, "reason": "feature_flag_off"},
            )
            return None

        # Gate (c): API key available
        if not self._llm_caller.available():
            _audit_reflector_call(
                "v01_stub_text",
                {"decision_id": decision_id, "reason": "llm_caller_not_available"},
            )
            return None

        # Self-grade refusal invariant ─────────────────────────────────────
        # MoA review F1 (Claude I1): exact-string equality fails on:
        #   - provider-prefix asymmetry ("openai/gpt-4.1" vs "gpt-4.1")
        #   - case differences ("OpenAI/..." vs "openai/...")
        #   - dated-suffix variants ("openai/gpt-4.1-2025-04-14" vs "openai/gpt-4.1")
        # Normalize both sides via _normalize_model_id() before comparison.
        pm_model = decision.get("llm_committee_model_id", None)
        reflector_model = getattr(self._llm_caller, "model_id", None)
        if pm_model and reflector_model and _normalize_model_id(pm_model) == _normalize_model_id(reflector_model):
            logger.warning(
                "Reflector v0.2: SELF-GRADE REFUSED — decision was made by %s "
                "which normalizes to the same model as the reflector (%s). "
                "Falling back to v0.1 stub. "
                "(ADR-0042 anti-patterns / ADR-0057 §4)",
                pm_model,
                reflector_model,
            )
            _audit_reflector_call(
                "v02_self_grade_refused",
                {
                    "decision_id": decision_id,
                    "pm_model": pm_model,
                    "pm_model_normalized": _normalize_model_id(pm_model),
                    "reflector_model": reflector_model,
                    "reflector_model_normalized": _normalize_model_id(reflector_model),
                },
            )
            return None

        # Build prompts ────────────────────────────────────────────────────
        direction = int(decision.get("direction", 0))
        lesson_values = ", ".join(lc.value for lc in LessonCategory)

        system_prompt = (
            "You are a post-trade reviewer with deep expertise in quantitative "
            "portfolio analysis.  The decision below was made by a portfolio "
            "manager and has now resolved with the outcome shown.  Your task:\n\n"
            "1. Write exactly 2-4 sentences of plain prose (no bullets, no "
            "headers, no markdown) covering: was the directional call correct "
            "(cite the alpha figure), which part of the investment thesis held or "
            "failed, and one concrete lesson to apply to the next similar analysis.\n"
            "2. Classify the trade into exactly one lesson category.\n\n"
            f"Valid lesson categories: {lesson_values}\n\n"
            "Be specific and terse.  Do NOT speculate about future trades.  "
            "Return only the structured JSON — no surrounding commentary."
        )

        safe_decision = {
            k: v for k, v in decision.items()
            if k not in ("schema_version", "kind")
        }
        user_prompt = (
            "DECISION:\n"
            + json.dumps(safe_decision, indent=2, default=str)
            + "\n\nOUTCOME:\n"
            + json.dumps(
                {
                    "raw_return": f"{raw_return:+.4f}",
                    "alpha_return": f"{alpha_return:+.4f}",
                    "benchmark": benchmark,
                    "holding_days": holding_days,
                    "outcome_quality": f"{outcome_quality}/5",
                },
                indent=2,
            )
        )

        prompt_hash = "llm-v02:" + hashlib.sha256(
            (system_prompt + user_prompt).encode()
        ).hexdigest()[:12]

        # LLM call ─────────────────────────────────────────────────────────
        try:
            if ReflectionLLMOutput is None:
                raise ImportError("pydantic not available; cannot use v0.2 structured path")

            parsed_obj, _raw = self._llm_caller.call(
                system_prompt,
                user_prompt,
                schema=ReflectionLLMOutput,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Reflector v0.2: LLM call raised %s; falling back to v0.1 stub.",
                exc,
            )
            _audit_reflector_call(
                "v02_llm_fallback_to_v01",
                {"decision_id": decision_id, "error": str(exc)},
            )
            return None

        if parsed_obj is None:
            _audit_reflector_call(
                "v02_llm_fallback_to_v01",
                {"decision_id": decision_id, "error": "llm_returned_none"},
            )
            return None

        # Extract fields from Pydantic model ───────────────────────────────
        reflection_text = str(parsed_obj.reflection_text).strip()
        lesson_category = _normalize_lesson_category(str(parsed_obj.lesson_category))

        if not reflection_text:
            _audit_reflector_call(
                "v02_llm_fallback_to_v01",
                {"decision_id": decision_id, "error": "empty_reflection_text"},
            )
            return None

        # Oracle Fallacy guard verification ────────────────────────────────
        # tau_observable is ALWAYS the deterministic value; the LLM cannot
        # alter it.  (The LLM output schema intentionally does not include
        # tau_observable — this is the canonical guard documented in ADR-0057.)

        _audit_reflector_call(
            "v02_llm_succeeded",
            {
                "decision_id": decision_id,
                "reflector_model": reflector_model,
                "lesson_category": lesson_category.value,
                "prompt_hash": prompt_hash,
            },
        )

        return reflection_text, lesson_category, prompt_hash

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
# v0.2 helper: detect LLMCaller instance without hard import dependency
# ---------------------------------------------------------------------------


def _is_llm_caller_instance(obj: Any) -> bool:
    """Return True iff *obj* is an LLMCaller (duck-typed: has .call + .available + .model_id)."""
    return (
        obj is not None
        and callable(getattr(obj, "call", None))
        and callable(getattr(obj, "available", None))
        and hasattr(obj, "model_id")
    )


# ---------------------------------------------------------------------------
# LLM prompt builder (used when llm_caller is a plain Callable — v0.1 compat)
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
