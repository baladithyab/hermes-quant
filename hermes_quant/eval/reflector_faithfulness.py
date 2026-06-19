"""hermes_quant.eval.reflector_faithfulness — B41-c Gate 3 (ADR-4665 §7.3).

ADVISORY-PLANE / EVAL-ONLY faithfulness gate for the Reflector — the LLM stage
closest to default-ON (``HERMES_QUANT_REFLECTOR_LLM``). The Reflector is
write-only and OFF the decision path (it writes reflections/lessons, it does NOT
size trades), but it had NO eval metric. This module is that metric: it produces
a pass/fail faithfulness verdict a HUMAN reads before flipping the flag
default-ON. It changes no decision path, flips no flag, places no order, and
touches no risk-gate / kill-switch / sizing surface.

It checks each reflection (the ``Reflection`` dataclass row in
``reflections.jsonl`` produced by ``hermes_quant.memory.reflector``) on three
axes:

  1. **Grounded in logged trade facts.** Every numerical claim in the
     reflection's prose must trace to a number RECOMPUTED from the logged trade
     record (entry/exit price, benchmark, derived raw/alpha return, holding
     days). Invented P&L / phantom segment figures fail. The extraction is the
     regression-tested ``grounding.verifier.extract_numerical_claims`` primitive
     (Wave-5 ClaimVerifier); the matching is fact-tolerance against the trade
     record, because a settled trade has no rendered OHLCV ``GroundTruthBlock``
     to substring-cite (so ``ClaimVerifier.verify`` itself does not fit — only
     its extractor does). An optional LLM-as-judge supplies the qualitative
     layer; in tests it is a golden-fixture replay (no live LLM).

  2. **No post-trade leakage into decision-feeding fields.** The Reflector is
     write-only, so writing a post-trade outcome is fine; LEAKAGE is writing
     future (post-trade) information into a field a FUTURE DECISION reads. The
     retriever (``memory/retriever.py``) and the BMA confidence haircut
     (``learning/lesson_haircut.py``) BOTH gate retrieval on the SAME rule:
     surface a reflection to a decision at ``asof`` only when
     ``tau_observable < asof``. That guard is only sound if (a)
     ``tau_observable`` is HONEST — at or after the deterministic floor
     ``max(asof_resolution, asof_decision + holding_days·86400 + 6h)`` (an
     understated tau admits the reflection too early), and (b) the only
     free-form decision-feeding field — ``reflection_text`` — embeds NO event
     dated after the observability horizon (that date would be future knowledge
     carried past tau). Both sub-checks are PURELY DETERMINISTIC (no LLM): this
     is the core no-look-ahead contract.

  3. **Stable lesson_category.** The categorical label the Reflector assigns must
     be stable: the same trade-class — ``(ticker, direction, sign(alpha))`` —
     must map to the same ``lesson_category`` and not drift run-to-run.

All times UTC. Deterministic + reproducible: same inputs → identical verdict.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Reuse the regression-tested numerical-claim extractor from the Wave-5
# ClaimVerifier (grounding/verifier.py) rather than re-implementing the regex.
from hermes_quant.grounding.verifier import _normalize_number, extract_numerical_claims

# Reuse the Reflector's deterministic tau_observable floor so the gate's
# no-look-ahead horizon can NEVER drift from the production Oracle-Fallacy guard.
from hermes_quant.memory.reflector import _compute_tau_observable, _parse_dt

# ---------------------------------------------------------------------------
# Numeric matching tolerance (deterministic; no RNG, no wall-clock)
# ---------------------------------------------------------------------------

_ABS_TOL = 0.1     # absolute floor — covers 1-decimal rounding of percentages
_REL_TOL = 0.01    # 1% relative — covers price rounding

# ISO calendar-date token in free-form prose (YYYY-MM-DD).
_DATE_TOKEN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# The single free-form decision-feeding field (the retriever injects it verbatim
# into the PM prompt). Write-only fields (reflector_prompt_hash, benchmark, ...)
# are never read by a decision, so they cannot leak.
_FREEFORM_DECISION_FEEDING_FIELD = "reflection_text"


# ---------------------------------------------------------------------------
# Trade facts — recomputed from the logged record (the GROUND TRUTH)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeFacts:
    """Numbers recomputed from the logged trade record, independent of what the
    reflection self-reports. Grounding traces reflection claims against THESE."""

    ticker: str
    direction: int
    entry_price: float
    exit_price: float
    benchmark_return: float
    raw_return: float
    alpha_return: float
    holding_days: int
    asof_decision: datetime
    asof_resolution: datetime
    tau_floor: datetime

    def allowed_magnitudes(self) -> list[float]:
        """Magnitudes any grounded claim may match (fraction AND percentage forms)."""
        ratios = (self.raw_return, self.alpha_return, self.benchmark_return)
        vals: list[float] = [self.entry_price, self.exit_price,
                             float(self.holding_days)]
        for r in ratios:
            vals.append(r)            # fraction form, e.g. 0.055
            vals.append(r * 100.0)    # percentage form, e.g. 5.5
        # absolute values too (a loss may be cited as a positive magnitude)
        return [abs(v) for v in vals]


def recompute_trade_facts(trade_record: Mapping[str, Any]) -> TradeFacts:
    """Recompute derived facts from a logged trade record.

    Mirrors ``Reflector.reflect_on_close`` math exactly (short returns inverted),
    so the gate measures the reflection against the SAME ground truth the
    Reflector saw — never against the reflection's own self-reported numbers.
    """
    entry = float(trade_record.get("entry_price", 0) or 0)
    exit_ = float(trade_record.get("exit_price", 0) or 0)
    benchmark_return = float(trade_record.get("benchmark_return", 0) or 0)
    direction = int(trade_record.get("direction", 0))

    raw_return = (exit_ - entry) / abs(entry) if entry else 0.0
    if direction < 0:
        raw_return = -raw_return
    alpha_return = raw_return - benchmark_return

    asof_dec = _parse_dt(trade_record.get("asof_decision"))
    asof_res = _parse_dt(trade_record.get("asof_resolution"))
    holding_days = max(0, int((asof_res - asof_dec).total_seconds() / 86400))
    tau_floor = _compute_tau_observable(asof_res, asof_dec, holding_days)

    return TradeFacts(
        ticker=str(trade_record.get("ticker", "")).upper(),
        direction=direction,
        entry_price=entry,
        exit_price=exit_,
        benchmark_return=benchmark_return,
        raw_return=round(raw_return, 8),
        alpha_return=round(alpha_return, 8),
        holding_days=holding_days,
        asof_decision=asof_dec,
        asof_resolution=asof_res,
        tau_floor=tau_floor,
    )


# ---------------------------------------------------------------------------
# Judge verdict (LLM-as-judge boundary; golden-fixtured in tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeVerdict:
    """A single LLM-as-judge verdict over one reflection.

    In production this is produced by an ``LLMCaller``-backed judge; in tests it
    is replayed from a recorded golden fixture (no live LLM, no network)."""

    grounded: bool
    lesson_category: str
    reason: str


# A judge is any callable (reflection_dict, TradeFacts) -> JudgeVerdict.
Judge = Callable[[Mapping[str, Any], TradeFacts], JudgeVerdict]


def golden_judge(verdicts_by_reflection_id: Mapping[str, Mapping[str, Any]]) -> Judge:
    """Build a deterministic judge that REPLAYS recorded golden verdicts.

    Keyed by ``reflection_id``. This is the test-path judge: it makes the
    qualitative LLM checks (#1 grounding, #3 stability) deterministic and
    network-free while leaving the production wiring (a real ``LLMCaller``)
    interchangeable behind the same ``Judge`` signature.
    """

    def _judge(reflection: Mapping[str, Any], facts: TradeFacts) -> JudgeVerdict:
        rid = str(reflection.get("reflection_id", ""))
        rec = verdicts_by_reflection_id.get(rid)
        if rec is None:
            # No recorded verdict → conservative abstain (treated as grounded so
            # the deterministic tracer remains the binding check).
            return JudgeVerdict(grounded=True,
                                lesson_category=str(reflection.get("lesson_category", "")),
                                reason=f"no golden verdict for {rid}; abstaining")
        return JudgeVerdict(
            grounded=bool(rec.get("grounded", True)),
            lesson_category=str(rec.get("lesson_category", "")),
            reason=str(rec.get("reason", "")),
        )

    return _judge


# ---------------------------------------------------------------------------
# Check + verdict result types (house style: frozen dataclass, reasons list)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One faithfulness check's outcome."""

    name: str                       # "grounding" | "no_leakage" | "lesson_stability"
    passed: bool
    reasons: list[str] = field(default_factory=list)   # one per failure
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateVerdict:
    """Aggregate faithfulness verdict a human reads before flipping the flag.

    ``passed`` is True iff EVERY check passed (advisory; the operator still
    makes the final default-ON call)."""

    passed: bool
    judge_used: bool
    checks: list[CheckResult]
    reasons: list[str] = field(default_factory=list)   # aggregated failing reasons


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _matches_a_fact(claim_value: float, allowed: list[float]) -> bool:
    target = abs(claim_value)
    for a in allowed:
        if abs(target - a) <= max(_ABS_TOL, abs(a) * _REL_TOL):
            return True
    return False


class ReflectorFaithfulnessGate:
    """Offline, deterministic faithfulness gate for reflector outputs.

    Advisory-plane / eval-only. No decision path, no flag flip, no order, no
    risk-gate / sizing reference. Reproducible: same inputs → identical verdict.
    """

    def __init__(self, *, grounding_floor: float = 1.0) -> None:
        # 1.0 = every numerical claim must trace (fail any invented number),
        # matching the Wave-5 ClaimVerifier strict posture for memory writes.
        if not (0.0 <= grounding_floor <= 1.0):
            raise ValueError(f"grounding_floor must be in [0, 1], got {grounding_floor}")
        self.grounding_floor = grounding_floor

    # ------------------------------------------------------------------
    # Check #1 — grounded in logged trade facts
    # ------------------------------------------------------------------

    def _check_grounding(
        self,
        reflection: Mapping[str, Any],
        facts: TradeFacts,
        judge: Judge | None,
    ) -> CheckResult:
        text = str(reflection.get("reflection_text", "") or "")
        claims = extract_numerical_claims(text)
        allowed = facts.allowed_magnitudes()

        ungrounded: list[str] = []
        for raw_claim in claims:
            try:
                value = float(_normalize_number(raw_claim))
            except ValueError:
                ungrounded.append(raw_claim)
                continue
            if not _matches_a_fact(value, allowed):
                ungrounded.append(raw_claim)

        n_claims = len(claims)
        n_ungrounded = len(ungrounded)
        coverage = 1.0 if n_claims == 0 else (n_claims - n_ungrounded) / n_claims
        tracer_ok = coverage >= self.grounding_floor

        judge_grounded: bool | None = None
        judge_reason = ""
        if judge is not None:
            jv = judge(reflection, facts)
            judge_grounded = jv.grounded
            judge_reason = jv.reason

        passed = tracer_ok and (judge_grounded is not False)

        reasons: list[str] = []
        if not tracer_ok:
            reasons.append(
                f"grounding coverage {coverage:.2f} < floor {self.grounding_floor:.2f}; "
                f"untraceable numerical claims (not in the logged trade record): {ungrounded[:5]}"
            )
        if judge_grounded is False:
            reasons.append(f"LLM-judge flagged ungrounded: {judge_reason}")

        return CheckResult(
            name="grounding",
            passed=passed,
            reasons=reasons,
            detail={
                "n_claims": n_claims,
                "n_ungrounded": n_ungrounded,
                "ungrounded_claims": ungrounded,
                "coverage": round(coverage, 4),
                "judge_grounded": judge_grounded,
            },
        )

    # ------------------------------------------------------------------
    # Check #2 — no post-trade leakage into decision-feeding fields
    # ------------------------------------------------------------------

    def _check_no_leakage(
        self,
        reflection: Mapping[str, Any],
        facts: TradeFacts,
    ) -> CheckResult:
        reasons: list[str] = []

        # (A) tau_observable must be HONEST: PRESENT, parseable, AND at or after the
        # deterministic floor.
        #
        # An ABSENT/None/blank tau is the most-dishonest case — there is NO
        # observability stamp at all. It is the literal initial persisted value
        # (a reflection written before its tau was computed, or via a torn/partial
        # write, carries None). The PRODUCTION retriever fail-CLOSES on None
        # (excludes the reflection from retrieval — retriever.py:366-367). This
        # gate exists to CERTIFY that SAME no-look-ahead rule, so it MUST also
        # fail-CLOSED here. We therefore distinguish absent/unparseable from
        # parseable BEFORE the floor comparison instead of routing None through
        # _parse_dt (which returns datetime.now(UTC) — always after the floor, so
        # it would silently PASS the dishonest case). Same _parse_dt(None)->now()
        # fail-open family fixed at the catalyst / semantic / freqtrade sites.
        #
        # An understated (present but too-early) tau admits the reflection to a
        # future decision too early — the retriever / haircut guard is `tau < asof`.
        raw_tau = reflection.get("tau_observable")
        tau_missing = raw_tau is None or (isinstance(raw_tau, str) and not raw_tau.strip())
        tau: datetime | None
        tau_unparseable = False
        if tau_missing:
            tau = None
            reasons.append(
                "tau_observable is absent/None — unverifiable observability stamp; "
                "fail-closed, matching the retriever's exclusion rule (retriever.py "
                "treats a None tau as not-yet-observable and excludes it)."
            )
        else:
            try:
                tau = _parse_dt(raw_tau)
            except (ValueError, TypeError):
                tau = None
                tau_unparseable = True
                reasons.append(
                    f"tau_observable {raw_tau!r} is unparseable as an ISO datetime — "
                    f"unverifiable observability stamp; fail-closed."
                )

        tau_below_floor = tau is not None and tau < facts.tau_floor
        if tau_below_floor:
            reasons.append(
                f"tau_observable {tau.isoformat()} is BEFORE the deterministic floor "
                f"{facts.tau_floor.isoformat()} — would surface this reflection to a "
                f"future decision before its outcome was knowable (look-ahead)."
            )

        # Observability horizon: nothing in a decision-feeding field may post-date
        # this. When tau is absent/unparseable, fall back to the deterministic
        # floor / resolution (the most-conservative horizon) for the date scan.
        horizon = max(facts.tau_floor, facts.asof_resolution)
        if tau is not None:
            horizon = max(horizon, tau)

        # (B) the only free-form decision-feeding field — reflection_text — must
        # embed no event dated after the horizon (future knowledge carried past tau).
        text = str(reflection.get(_FREEFORM_DECISION_FEEDING_FIELD, "") or "")
        future_dates: list[str] = []
        for y, m, d in _DATE_TOKEN.findall(text):
            try:
                dt = datetime(int(y), int(m), int(d), tzinfo=horizon.tzinfo)
            except ValueError:
                continue
            if dt.date() > horizon.date():
                future_dates.append(f"{y}-{m}-{d}")
        if future_dates:
            reasons.append(
                f"reflection_text references date(s) after the observability horizon "
                f"{horizon.date().isoformat()}: {future_dates} — post-trade knowledge "
                f"leaking into a decision-feeding field."
            )

        tau_absent = tau_missing or tau_unparseable
        passed = not tau_absent and not tau_below_floor and not future_dates
        return CheckResult(
            name="no_leakage",
            passed=passed,
            reasons=reasons,
            detail={
                "tau_observable": tau.isoformat() if tau is not None else None,
                "tau_floor": facts.tau_floor.isoformat(),
                "tau_below_floor": tau_below_floor,
                "tau_missing": tau_missing,
                "tau_unparseable": tau_unparseable,
                "observability_horizon": horizon.isoformat(),
                "future_dates_in_text": future_dates,
            },
        )

    # ------------------------------------------------------------------
    # Check #3 — stable lesson_category across identical trade-classes
    # ------------------------------------------------------------------

    @staticmethod
    def _trade_class_key(reflection: Mapping[str, Any], facts: TradeFacts) -> str:
        alpha = float(reflection.get("alpha_return", facts.alpha_return) or 0.0)
        sign = "+" if alpha > 0 else "-" if alpha < 0 else "0"
        return f"{facts.ticker}|{facts.direction}|{sign}"

    def _check_lesson_stability(
        self,
        reflections: list[Mapping[str, Any]],
        facts_by_decision: Mapping[str, TradeFacts],
    ) -> CheckResult:
        by_class: dict[str, set[str]] = {}
        for r in reflections:
            facts = facts_by_decision[str(r.get("decision_id", ""))]
            key = self._trade_class_key(r, facts)
            by_class.setdefault(key, set()).add(str(r.get("lesson_category", "")))

        drifting = {k: sorted(v) for k, v in by_class.items() if len(v) > 1}
        passed = not drifting

        reasons: list[str] = []
        for key, cats in drifting.items():
            reasons.append(
                f"lesson_category drifted for identical trade-class {key}: {cats} "
                f"(same trade-class must map to one stable category)."
            )

        return CheckResult(
            name="lesson_stability",
            passed=passed,
            reasons=reasons,
            detail={"by_class": {k: sorted(v) for k, v in by_class.items()},
                    "drifting": drifting},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_one(
        self,
        reflection: Mapping[str, Any],
        trade_record: Mapping[str, Any],
        *,
        judge: Judge | None = None,
    ) -> GateVerdict:
        """Evaluate a single reflection against its logged trade record.

        Runs grounding (#1) + no-leakage (#2). Stability (#3) needs >1 reflection
        of the same trade-class — see :meth:`evaluate_batch`.
        """
        facts = recompute_trade_facts(trade_record)
        checks = [
            self._check_grounding(reflection, facts, judge),
            self._check_no_leakage(reflection, facts),
        ]
        return self._finalize(checks, judge_used=judge is not None)

    def evaluate_batch(
        self,
        reflections: list[Mapping[str, Any]],
        trade_records: Mapping[str, Mapping[str, Any]],
        *,
        judge: Judge | None = None,
    ) -> GateVerdict:
        """Evaluate a batch: aggregate grounding + no-leakage per reflection, plus
        the cross-reflection lesson_category stability (#3).

        ``trade_records`` is keyed by ``decision_id``.

        An EMPTY reflection batch is insufficient data and fails CLOSED: the
        per-reflection grounding/leakage results would otherwise fold through
        ``all([]) == True`` (and an empty corpus has no lesson-category drift),
        so a zero-reflection run would certify PASS having certified NOTHING.
        The verdict a human reads before flipping the flag must not read as a
        clean PASS off zero evaluated reflections.
        """
        if not reflections:
            insufficient = CheckResult(
                name="batch_non_empty",
                passed=False,
                reasons=[
                    "empty reflection batch — insufficient data to certify "
                    "faithfulness; fail-closed (a zero-reflection run certifies "
                    "nothing and must not read as PASS)."
                ],
                detail={"n_reflections": 0},
            )
            return self._finalize([insufficient], judge_used=judge is not None)

        facts_by_decision: dict[str, TradeFacts] = {}
        per_grounding: list[CheckResult] = []
        per_leakage: list[CheckResult] = []
        for r in reflections:
            did = str(r.get("decision_id", ""))
            if did not in facts_by_decision:
                facts_by_decision[did] = recompute_trade_facts(trade_records[did])
            facts = facts_by_decision[did]
            per_grounding.append(self._check_grounding(r, facts, judge))
            per_leakage.append(self._check_no_leakage(r, facts))

        grounding_agg = _aggregate(per_grounding, "grounding")
        leakage_agg = _aggregate(per_leakage, "no_leakage")
        stability = self._check_lesson_stability(reflections, facts_by_decision)

        return self._finalize([grounding_agg, leakage_agg, stability],
                              judge_used=judge is not None)

    @staticmethod
    def _finalize(checks: list[CheckResult], *, judge_used: bool) -> GateVerdict:
        passed = all(c.passed for c in checks)
        reasons = [r for c in checks if not c.passed for r in c.reasons]
        return GateVerdict(passed=passed, judge_used=judge_used,
                           checks=checks, reasons=reasons)


def _aggregate(results: list[CheckResult], name: str) -> CheckResult:
    """Fold per-reflection check results into one batch-level check."""
    passed = all(r.passed for r in results)
    reasons = [r for res in results if not res.passed for r in res.reasons]
    return CheckResult(
        name=name,
        passed=passed,
        reasons=reasons,
        detail={"n_evaluated": len(results),
                "n_failed": sum(1 for r in results if not r.passed)},
    )
