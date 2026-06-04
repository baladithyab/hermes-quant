"""57f6 — bounded, asof-honest, lesson-driven confidence haircut.

Today reflections/lessons are produced but only ever reach an OPTIONAL
LLM-committee prompt (HERMES_QUANT_MEMORY_INJECT). No deterministic default-path
decision is constrained by them — the learning loop is open. This module closes
it: a recent same-symbol same-direction LOSS lesson applies a documented,
bounded multiplicative haircut to a decision's confidence at the BMA aggregation
seam.

Invariants (the reviewer's job is to break these):

  - **Same ticker AND same direction.** A loss going long AAPL only cautions a
    new long-AAPL call, not a short, and not a different symbol.
  - **asof-HONEST.** A lesson whose outcome became observable at or after the
    decision asof is ignored. Using it would mean a past decision learned from a
    future outcome — lookahead, which corrupts every backtest. The guard is the
    same ``tau_observable < asof`` rule the retriever uses.
  - **Bounded.** No matter how many matching losses exist, confidence is never
    cut below ``floor_fraction`` of its original value. The loop can dampen, not
    silence.
  - **De-duplicated.** Each distinct ``lesson_id`` counts at most once.
  - **No-op when nothing matches.** Returns the input confidence unchanged.

This is a pure function over an explicit ``lessons`` list. The list is supplied
by an injected provider (see ``LossLessonProvider``) so the aggregator stays
decoupled from the reflections/decisions JSONL stack; in production the provider
joins ``reflections.jsonl`` (alpha_return < 0) with ``decisions.jsonl`` (the
direction). Pure-Python, deterministic, offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class LossLesson:
    """A settled losing trade, distilled to what a haircut needs.

    ``tau_observable`` is when the loss became KNOWABLE (the position close /
    settlement), NOT when the original decision was made — this is the field the
    no-lookahead guard keys on, mirroring the reflector's ``tau_observable``.
    """

    lesson_id: str
    ticker: str
    direction: int
    tau_observable: pd.Timestamp
    alpha_return: float


@runtime_checkable
class LossLessonProvider(Protocol):
    """Supplies recent loss lessons for a (ticker, asof). Implementations must
    themselves be asof-honest, but ``apply_lesson_haircut`` re-applies the guard
    defensively so a sloppy provider cannot introduce lookahead."""

    def recent_loss_lessons(
        self, ticker: str, asof: pd.Timestamp
    ) -> list[LossLesson]: ...


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def apply_lesson_haircut(
    confidence: float,
    ticker: str,
    direction: int,
    decision_asof: pd.Timestamp,
    lessons: list[LossLesson],
    per_lesson_haircut: float,
    floor_fraction: float,
) -> float:
    """Return ``confidence`` reduced by matching same-ticker/-direction losses.

    Parameters
    ----------
    confidence:
        The pre-haircut decision confidence.
    ticker, direction:
        The current decision's symbol and direction. Only lessons matching BOTH
        (ticker case-insensitive) are considered.
    decision_asof:
        The decision timestamp. Lessons with ``tau_observable >= decision_asof``
        are excluded (no lookahead).
    lessons:
        Candidate loss lessons (any tickers/directions/times — filtered here).
    per_lesson_haircut:
        Fractional reduction per distinct matching lesson, in [0, 1].
    floor_fraction:
        Lower bound as a fraction of the original confidence, in [0, 1]. The
        result is never below ``confidence * floor_fraction``.

    Returns the haircut confidence, clipped to [0, 1]. A strict no-op (returns
    ``confidence`` unchanged) when no lesson matches.
    """
    # A NaT decision asof is uninterpretable as a "now" — apply no haircut.
    if pd.isna(decision_asof):
        return confidence
    decision_asof = _as_utc(decision_asof)
    ticker_u = ticker.upper()

    seen: set[str] = set()
    n_matching = 0
    for lesson in lessons:
        if lesson.lesson_id in seen:
            continue  # de-dup by id
        if lesson.ticker.upper() != ticker_u:
            continue
        if lesson.direction != direction:
            continue
        # NO-LOOKAHEAD GUARD: the loss must have been observable strictly before
        # the decision. Equality counts as not-yet-knowable (conservative,
        # matching the retriever's `tau >= asof: continue`). A NaT tau_observable
        # is "unknown when this became knowable" — excluded, because `NaT >= asof`
        # is always False and would otherwise silently admit the lesson.
        tau = _as_utc(lesson.tau_observable)
        if pd.isna(tau) or tau >= decision_asof:
            continue
        seen.add(lesson.lesson_id)
        n_matching += 1

    if n_matching == 0:
        return confidence  # strict no-op

    # Compounding multiplicative haircut, clamped at the floor so a pile of
    # losses dampens but never silences.
    factor = (1.0 - per_lesson_haircut) ** n_matching
    factor = max(factor, floor_fraction)
    out = confidence * factor
    return float(min(max(out, 0.0), 1.0))
