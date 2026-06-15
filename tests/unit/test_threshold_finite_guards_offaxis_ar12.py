"""ar12 — research-plane threshold env coercions must reject non-finite (the ar09/ar10 family, off-axis tail).

The codebase-clean convergence review (wf w4q451umg) closed the MONEY-rail threshold family
(autonomous.py kill-switch / silence-bias / safety-rails via ar08/ar09; portfolio_state NAV,
tick_lock timeout, llm_budget ceilings via ar10) but flagged TWO research/advisory-plane siblings
with the identical fail-open gap — `float(os.environ.get(...))` catching only ValueError:

  - factors/ic_dedup.py  (HERMES_QUANT_IC_DEDUP_THRESHOLD, default 0.99):
        passes = max_corr < thr. A `1e400` typo -> thr=inf -> `max_corr < inf` is ALWAYS True ->
        EVERY candidate factor is admitted -> the "Correlation Red Sea" (F4) the gate exists to
        prevent floods the library. nan -> always False -> silently bricks the gate (reject-all).
  - research/hypothesis_novelty.py (HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD, default 0.85):
        passes = max_sim < thr. thr=inf -> every claim reads as "novel" -> re-propose near-duplicate
        hypotheses forever (defeats the dedup). nan -> nothing ever novel.

These are PROPOSE-ONLY / advisory-plane (never touch a limit, size, or the risk gate), so P3 — but
they are the same bug family, and family-completion discipline closes the whole family.

FIX: reject non-finite at the env coercion, fall back to the documented default + warn.
Byte-identical for any finite configured value.
"""

from __future__ import annotations

import math


# --------------------------------------------------------------------------- #
# ic_dedup
# --------------------------------------------------------------------------- #
def _ic_default(monkeypatch, raw: str) -> float:
    """Re-resolve ic_dedup's env-derived default under a patched env var."""
    import hermes_quant.factors.ic_dedup as icd
    monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_THRESHOLD", raw)
    return icd._finite_threshold(
        raw, icd._IC_DEDUP_DEFAULT, "HERMES_QUANT_IC_DEDUP_THRESHOLD"
    )


def test_ic_dedup_inf_typo_falls_back_to_default(monkeypatch) -> None:
    """`1e400` overflows to inf without ValueError -> admit-all Red Sea. Must fall back."""
    import hermes_quant.factors.ic_dedup as icd
    val = _ic_default(monkeypatch, "1e400")
    assert math.isfinite(val), "a non-finite IC-dedup threshold must not propagate (admit-all)"
    assert val == icd._IC_DEDUP_DEFAULT


def test_ic_dedup_inf_literal_falls_back(monkeypatch) -> None:
    import hermes_quant.factors.ic_dedup as icd
    assert _ic_default(monkeypatch, "inf") == icd._IC_DEDUP_DEFAULT


def test_ic_dedup_nan_falls_back(monkeypatch) -> None:
    import hermes_quant.factors.ic_dedup as icd
    assert _ic_default(monkeypatch, "nan") == icd._IC_DEDUP_DEFAULT


def test_ic_dedup_nonnumeric_falls_back(monkeypatch) -> None:
    import hermes_quant.factors.ic_dedup as icd
    assert _ic_default(monkeypatch, "not-a-float") == icd._IC_DEDUP_DEFAULT


def test_ic_dedup_finite_byte_identical(monkeypatch) -> None:
    assert _ic_default(monkeypatch, "0.95") == 0.95


# --------------------------------------------------------------------------- #
# hypothesis_novelty
# --------------------------------------------------------------------------- #
def test_novelty_inf_typo_falls_back(monkeypatch) -> None:
    import hermes_quant.research.hypothesis_novelty as hn
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "1e400")
    val = hn._default_threshold()
    assert math.isfinite(val), "a non-finite novelty threshold must not propagate (everything-novel)"
    assert val == hn._NOVELTY_DEFAULT


def test_novelty_nan_falls_back(monkeypatch) -> None:
    import hermes_quant.research.hypothesis_novelty as hn
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "nan")
    assert hn._default_threshold() == hn._NOVELTY_DEFAULT


def test_novelty_nonnumeric_falls_back(monkeypatch) -> None:
    import hermes_quant.research.hypothesis_novelty as hn
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "garbage")
    assert hn._default_threshold() == hn._NOVELTY_DEFAULT


def test_novelty_finite_byte_identical(monkeypatch) -> None:
    import hermes_quant.research.hypothesis_novelty as hn
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "0.75")
    assert hn._default_threshold() == 0.75


def test_novelty_end_to_end_inf_does_not_admit_everything(monkeypatch) -> None:
    """Integration: with an inf-typo threshold, a near-duplicate claim must STILL be
    rejected (passes=False) because the guard clamps to the finite default 0.85."""
    import hermes_quant.research.hypothesis_novelty as hn
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "1e400")
    existing = ["momentum reversal predicts next-day equity returns"]
    # An identical claim has Jaccard 1.0 — must be rejected under the clamped 0.85 default.
    res = hn.check_novelty(existing[0], existing)
    assert res.passes is False, "an inf-typo threshold must not turn the novelty gate into admit-all"
