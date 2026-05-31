"""W3 hypothesis novelty/dedup gate tests (plan §6).

Gate condition 2: candidate hypotheses must pass novelty/dedup against the registry.
Also confirms the AST-purity guarantee carries through for meta-retro-authored criteria.
"""

from __future__ import annotations

from hermes_quant.research.hypothesis import Hypothesis
from hermes_quant.research.hypothesis_novelty import (
    check_novelty,
    token_jaccard,
)


def test_empty_library_passes() -> None:
    r = check_novelty("a brand new falsifiable claim about momentum", [])
    assert r.passes is True
    assert r.max_sim == 0.0
    assert r.nearest_claim is None
    assert r.reason == "library_empty"


def test_near_duplicate_rejected() -> None:
    existing = ["The regime_shift_invalidation setup carries persistent positive alpha and warrants a factor"]
    candidate = "The regime_shift_invalidation setup carries persistent positive alpha and warrants a factor"
    r = check_novelty(candidate, existing, threshold=0.85)
    assert r.max_sim >= 0.85
    assert r.passes is False
    assert r.nearest_claim == existing[0]


def test_distinct_claim_passes() -> None:
    existing = ["Momentum factor on semiconductors decays after earnings"]
    candidate = "Mean reversion in banking equities accelerates during rate hikes"
    r = check_novelty(candidate, existing, threshold=0.85)
    assert r.passes is True
    assert r.max_sim < 0.85


def test_threshold_env_override(monkeypatch) -> None:
    existing = ["alpha persists in the morning session for large cap technology names"]
    # Two claims share ~half their tokens -> mid Jaccard.
    candidate = "alpha persists in the morning session for small cap energy names"
    sim = token_jaccard(candidate, existing[0])
    assert 0.0 < sim < 1.0
    # With a high threshold the candidate passes; with a threshold below sim it is rejected.
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", "0.99")
    assert check_novelty(candidate, existing).passes is True
    monkeypatch.setenv("HERMES_QUANT_HYPOTHESIS_NOVELTY_THRESHOLD", str(sim - 0.01))
    assert check_novelty(candidate, existing).passes is False


def test_jaccard_symmetry_and_bounds() -> None:
    a = "the quick brown fox"
    b = "the quick brown fox jumps"
    assert token_jaccard(a, b) == token_jaccard(b, a)
    assert 0.0 <= token_jaccard(a, b) <= 1.0
    assert token_jaccard("", "anything") == 0.0


def test_candidate_criteria_pass_ast_purity() -> None:
    """The meta-retro registers candidates with numeric success/falsification criteria; the
    registry's existing AST-purity gate accepts them and rejects a malformed one (defense-
    in-depth, no new code needed)."""
    # The criteria the meta-retro uses must pass.
    hyp = Hypothesis(
        author="quant-monthly-meta-retro",
        claim="A test claim",
        null_hypothesis="The null",
        success_criteria=["vs_buyhold_alpha > 0.0", "sharpe >= 0.3"],
        falsification_criteria=["vs_buyhold_alpha <= 0.0"],
        duration_target_days=90,
    )
    assert hyp.success_criteria == ["vs_buyhold_alpha > 0.0", "sharpe >= 0.3"]

    # A malformed (sandbox-escape) criterion is rejected at construction.
    import pytest
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        Hypothesis(
            author="quant-monthly-meta-retro",
            claim="A test claim",
            null_hypothesis="The null",
            success_criteria=["__import__('os').system('rm -rf /')"],
            duration_target_days=90,
        )
