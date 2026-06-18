"""quantcore.aggregate — deterministic committee aggregation (backlog B-05).

Replaces the in-prompt arithmetic of skills/analysts/SKILL.md ("Committee
aggregation") with deterministic code: a lean port of the Beta-binomial BMA
in hermes_quant/aggregators/bma.py (ADR-0003), driven by the settlement
calibration tallies that quantcore.settle maintains in calibration.json:

    {analyst: {bucket: {"n": int, "n_correct": int}}}

Silence-by-default discipline:
- an analyst at or below 50% directional accuracy gets ZERO weight — never
  negative (a bad analyst is silenced, not bet against);
- a near-split committee (margin < min_margin) yields direction 0;
- cold start (EVERY analyst weightless) falls back to an UNWEIGHTED vote so
  a fresh install cannot brick the committee, and signals that by emitting
  null (None) weight values in the output;
- flat views (direction 0) add to the committee's total weight but to neither
  side, so they dilute both the margin and the confidence — abstention is a
  first-class vote against action.

Calibration-quality shrinkage: callers that want ECE-aware damping apply
shrink_confidence() PER VIEW, BEFORE calling aggregate() — the aggregator
itself never re-shrinks (see shrink_confidence docstring).
"""

from __future__ import annotations

from quantcore.schemas import AnalystView

__all__ = ["analyst_weight", "aggregate", "shrink_confidence"]

#: CommitteeSignal.dissent caps at 2000 chars; the dict output honors the same cap.
_DISSENT_MAX_LEN = 2000


def analyst_weight(
    calibration: dict,
    analyst: str,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
) -> float:
    """Beta-binomial posterior weight for one analyst from calibration tallies.

    Tallies (n, n_correct) are summed across ALL confidence buckets:

        alpha          = prior_alpha + n_correct
        beta           = prior_beta + (n - n_correct)
        posterior_mean = alpha / (alpha + beta)

    Cold start (no data for this analyst) leaves the posterior mean at the
    prior mean — 0.5 for the default uniform Beta(1, 1) prior.

    Weight = max(0, 2 * posterior_mean - 1): an analyst at 50% directional
    accuracy (a coin flip) gets ZERO weight, and below 50% is also zero, not
    negative — silence-by-default; we never invert a bad analyst's view.
    """
    n = 0
    n_correct = 0
    for rec in (calibration.get(analyst) or {}).values():
        n += int(rec.get("n", 0))
        n_correct += int(rec.get("n_correct", 0))
    posterior_mean = (prior_alpha + n_correct) / (prior_alpha + prior_beta + n)
    return max(0.0, 2.0 * posterior_mean - 1.0)


def aggregate(
    views: list[AnalystView],
    calibration: dict,
    *,
    agreement_bonus: float = 0.03,
    max_confidence: float = 0.75,
    min_margin: float = 0.10,
) -> dict:
    """Combine AnalystViews into one committee verdict (weighted vote).

    Vote: each view contributes weight(analyst) x view.confidence, signed by
    view.direction. Flat views (direction 0) contribute to the total but to
    neither side. Then:

    - direction:  sign of the signed sum ONLY if |sum| / total >= min_margin,
      else 0 (near-split committees are silenced).
    - confidence: weighted mean confidence of the winning side over the TOTAL
      committee weight (losing and flat views dilute toward silence), plus
      agreement_bonus when ALL views agree on the winning direction; capped
      at max_confidence. 0.0 when direction == 0.
    - magnitude:  weighted mean of the winning side's magnitudes. 0.0 when
      direction == 0.
    - dissent:    verbatim "analyst: rationale" for every losing-side view
      (recorded even when the margin rule forces direction 0).
    - weights:    {analyst: weight}. When EVERY weight is zero (all analysts
      cold or at/below 50% accuracy) the vote falls back to UNWEIGHTED
      (weight 1.0 per view) so cold start cannot brick the committee, and
      every value in `weights` is None to signal the fallback.

    Deterministic: views are processed in a stable sort by analyst name (then
    direction/confidence/magnitude/rationale), so input order never changes
    any output bit.
    """
    ordered = sorted(
        views,
        key=lambda v: (v.analyst, v.direction, v.confidence, v.magnitude, v.rationale),
    )
    out = {
        "direction": 0,
        "magnitude": 0.0,
        "confidence": 0.0,
        "dissent": "",
        "n_distinct_analysts": len({v.analyst for v in ordered}),
        "weights": {},
    }
    if not ordered:
        return out

    raw_weights = {
        a: analyst_weight(calibration, a) for a in sorted({v.analyst for v in ordered})
    }
    cold_start = all(w <= 0.0 for w in raw_weights.values())
    out["weights"] = {a: (None if cold_start else w) for a, w in raw_weights.items()}

    def _w(analyst: str) -> float:
        return 1.0 if cold_start else raw_weights[analyst]

    vote = sum(_w(v.analyst) * v.confidence * v.direction for v in ordered)
    total = sum(_w(v.analyst) * v.confidence for v in ordered)  # flat views included
    raw_sign = 1 if vote > 1e-12 else (-1 if vote < -1e-12 else 0)
    margin = abs(vote) / total if total > 1e-12 else 0.0

    # Dissent is judged against the raw vote sign and recorded VERBATIM,
    # whether or not the margin rule lets the direction stand.
    if raw_sign != 0:
        losers = [v for v in ordered if v.direction == -raw_sign]
        out["dissent"] = "; ".join(f"{v.analyst}: {v.rationale}" for v in losers)[
            :_DISSENT_MAX_LEN
        ]

    if raw_sign == 0 or margin + 1e-12 < min_margin:
        return out  # silence: direction 0, confidence/magnitude stay 0.0

    direction = raw_sign
    winning = [v for v in ordered if v.direction == direction]
    total_weight_all = sum(_w(v.analyst) for v in ordered)
    winning_weight = sum(_w(v.analyst) for v in winning)
    # Winning-side weighted confidence over the TOTAL committee weight:
    # losing AND flat views dilute confidence (correct behavior — abstention
    # and dissent both argue for smaller size downstream).
    confidence = (
        sum(_w(v.analyst) * v.confidence for v in winning) / total_weight_all
    )
    if all(v.direction == direction for v in ordered):
        confidence += agreement_bonus
    confidence = min(confidence, max_confidence)
    magnitude = sum(_w(v.analyst) * v.magnitude for v in winning) / winning_weight

    out["direction"] = direction
    out["confidence"] = confidence
    out["magnitude"] = magnitude
    return out


def shrink_confidence(confidence: float, ece: float, threshold: float = 0.10) -> float:
    """Shrink a single analyst's confidence toward 0.5 when its ECE is bad.

    CALLERS APPLY THIS PER-VIEW, BEFORE aggregate() — feed each view's
    confidence through shrink_confidence(view.confidence, ece_for(analyst))
    using the per-analyst ECE from quantcore.settle.calibration_report(),
    then aggregate the shrunk views. The aggregator never re-shrinks.

    When ece > threshold:  shrunk = 0.5 + (confidence - 0.5) * (1 - min(ece, 0.5))
    When ece <= threshold: confidence is returned unchanged.

    The factor floor at min(ece, 0.5) means even a maximally miscalibrated
    analyst keeps half its distance from 0.5 — shrinkage dampens, the
    zero-weight floor in analyst_weight() is what silences.
    """
    if ece <= threshold:
        return confidence
    factor = 1.0 - min(ece, 0.5)
    return 0.5 + (confidence - 0.5) * factor
