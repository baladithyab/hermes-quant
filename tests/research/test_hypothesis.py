"""tests/research/test_hypothesis.py — Hypothesis Registry tests (ADR-0048).

Coverage:
 - Hypothesis model validation (field constraints, extra-forbid).
 - HypothesisRegistry.register() round-trip.
 - Auto-ID generation when hypothesis_id is empty.
 - ID collision raises HypothesisIDCollision.
 - Status transitions: open→running→validated.
 - Status transitions: open→running→falsified.
 - Status transitions: open→abandoned.
 - Status transitions: open→running→abandoned.
 - Invalid transition raises InvalidStatusTransition.
 - Terminal state refuses further transitions.
 - Append-only enforcement: truncate() + update() raise AppendOnlyViolation.
 - read_all_open / read_all_running / read_all_resolved iterators.
 - Multi-hypothesis registry round-trip.
 - scope key limit validation.
 - success_criteria + falsification_criteria length limits.
"""

from __future__ import annotations

import pytest

from hermes_quant.research.hypothesis import (
    AppendOnlyViolation,
    Hypothesis,
    HypothesisIDCollision,
    HypothesisNotFound,
    HypothesisRegistry,
    InvalidStatusTransition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_hypothesis(**overrides) -> Hypothesis:
    defaults = dict(
        author="test-agent",
        claim="Sentiment analyst increases Sharpe by >=0.10",
        null_hypothesis="Sentiment makes no difference (alpha <= 0)",
        success_criteria=["sharpe >= 0.10"],
        falsification_criteria=["sharpe < 0.0"],
        experiment_design="Walk-forward backtest over 90 days",
        duration_target_days=90,
        scope={"universe": ["AAPL"], "env": "paper"},
    )
    defaults.update(overrides)
    return Hypothesis(**defaults)


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(path=tmp_path / "hypotheses.jsonl")


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


def test_hypothesis_model_defaults():
    h = _minimal_hypothesis()
    assert h.status == "open"
    assert h.hypothesis_id == ""  # empty → registry fills it
    assert h.created_at == ""


def test_hypothesis_extra_forbid():
    with pytest.raises(Exception):
        _minimal_hypothesis(unknown_field="oops")


def test_hypothesis_claim_max_length():
    with pytest.raises(Exception):
        _minimal_hypothesis(claim="x" * 513)


def test_hypothesis_null_hypothesis_max_length():
    with pytest.raises(Exception):
        _minimal_hypothesis(null_hypothesis="y" * 513)


def test_hypothesis_success_criteria_too_many():
    with pytest.raises(Exception):
        _minimal_hypothesis(success_criteria=["a", "b", "c", "d", "e", "f"])


def test_hypothesis_success_criteria_item_too_long():
    with pytest.raises(Exception):
        _minimal_hypothesis(success_criteria=["x" * 257])


def test_hypothesis_falsification_criteria_too_many():
    with pytest.raises(Exception):
        _minimal_hypothesis(falsification_criteria=["a"] * 6)


def test_hypothesis_scope_too_many_keys():
    with pytest.raises(Exception):
        _minimal_hypothesis(scope={str(i): i for i in range(11)})


def test_hypothesis_duration_bounds():
    with pytest.raises(Exception):
        _minimal_hypothesis(duration_target_days=0)
    with pytest.raises(Exception):
        _minimal_hypothesis(duration_target_days=731)


# ---------------------------------------------------------------------------
# Registry round-trip tests
# ---------------------------------------------------------------------------


def test_register_and_read(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    assert hyp_id.startswith("hyp_AAPL_")
    recovered = registry.read(hyp_id)
    assert recovered is not None
    assert recovered.hypothesis_id == hyp_id
    assert recovered.claim == h.claim
    assert recovered.status == "open"


def test_auto_id_generation_uses_ticker_from_scope(registry):
    h = _minimal_hypothesis(scope={"universe": ["MSFT"], "env": "test"})
    hyp_id = registry.register(h)
    assert "MSFT" in hyp_id


def test_explicit_id_is_preserved(registry):
    h = _minimal_hypothesis(hypothesis_id="hyp_CUSTOM_20250101_abc123")
    hyp_id = registry.register(h)
    assert hyp_id == "hyp_CUSTOM_20250101_abc123"
    recovered = registry.read(hyp_id)
    assert recovered.hypothesis_id == hyp_id


def test_read_unknown_returns_none(registry):
    assert registry.read("hyp_NONEXISTENT_20250101_000000") is None


def test_id_collision_raises(registry):
    h = _minimal_hypothesis(hypothesis_id="hyp_AAPL_20250101_aabbcc")
    registry.register(h)
    with pytest.raises(HypothesisIDCollision):
        registry.register(h)


# ---------------------------------------------------------------------------
# Status transition tests
# ---------------------------------------------------------------------------


def test_transition_open_to_running(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    recovered = registry.read(hyp_id)
    assert recovered.status == "running"


def test_transition_running_to_validated(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "validated", evidence={"run_id": "run_xyz"})
    recovered = registry.read(hyp_id)
    assert recovered.status == "validated"


def test_transition_running_to_falsified(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "falsified")
    assert registry.read(hyp_id).status == "falsified"


def test_transition_open_to_abandoned(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "abandoned")
    assert registry.read(hyp_id).status == "abandoned"


def test_transition_running_to_abandoned(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "abandoned")
    assert registry.read(hyp_id).status == "abandoned"


def test_invalid_transition_raises(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    with pytest.raises(InvalidStatusTransition):
        registry.update_status(hyp_id, "validated")  # open → validated is invalid


def test_invalid_transition_from_open_to_falsified(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    with pytest.raises(InvalidStatusTransition):
        registry.update_status(hyp_id, "falsified")  # must go through running


def test_terminal_state_blocks_further_transitions(registry):
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "validated")
    with pytest.raises(InvalidStatusTransition):
        registry.update_status(hyp_id, "falsified")


def test_update_status_unknown_hypothesis_raises(registry):
    with pytest.raises(HypothesisNotFound):
        registry.update_status("hyp_GHOST_20250101_000000", "running")


# ---------------------------------------------------------------------------
# Append-only enforcement tests
# ---------------------------------------------------------------------------


def test_truncate_raises_append_only_violation(registry):
    with pytest.raises(AppendOnlyViolation):
        registry.truncate()


def test_update_raises_append_only_violation(registry):
    with pytest.raises(AppendOnlyViolation):
        registry.update()


# ---------------------------------------------------------------------------
# Iterator tests
# ---------------------------------------------------------------------------


def test_read_all_open(registry):
    ids = set()
    for i in range(3):
        h = _minimal_hypothesis(claim=f"claim {i}")
        ids.add(registry.register(h))
    open_ids = {h.hypothesis_id for h in registry.read_all_open()}
    assert ids == open_ids


def test_read_all_running(registry):
    h1 = _minimal_hypothesis(claim="claim A")
    h2 = _minimal_hypothesis(claim="claim B")
    id1 = registry.register(h1)
    id2 = registry.register(h2)
    registry.update_status(id1, "running")
    running_ids = {h.hypothesis_id for h in registry.read_all_running()}
    assert id1 in running_ids
    assert id2 not in running_ids


def test_read_all_resolved(registry):
    h1 = _minimal_hypothesis(claim="terminal claim")
    id1 = registry.register(h1)
    registry.update_status(id1, "running")
    registry.update_status(id1, "validated")
    resolved_ids = {h.hypothesis_id for h in registry.read_all_resolved()}
    assert id1 in resolved_ids


def test_multi_hypothesis_isolation(registry):
    """Multiple hypotheses coexist without cross-contamination."""
    ids = []
    for i in range(5):
        h = _minimal_hypothesis(claim=f"claim {i}", duration_target_days=30 + i)
        ids.append(registry.register(h))
    # Transition only the first two
    registry.update_status(ids[0], "running")
    registry.update_status(ids[1], "abandoned")
    # Verify status isolation
    assert registry.read(ids[0]).status == "running"
    assert registry.read(ids[1]).status == "abandoned"
    assert registry.read(ids[2]).status == "open"
    assert registry.read(ids[3]).status == "open"
    assert registry.read(ids[4]).status == "open"


# ---------------------------------------------------------------------------
# B25 — 'monitoring' status + run-card linkage
# ---------------------------------------------------------------------------


def test_transition_running_to_monitoring(registry):
    """B25: running → monitoring is a valid non-terminal transition."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "monitoring", evidence={"run_id": "run_abc"})
    assert registry.read(hyp_id).status == "monitoring"


def test_transition_monitoring_to_validated(registry):
    """B25: monitoring → validated reaches a terminal verdict."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "monitoring")
    registry.update_status(hyp_id, "validated")
    assert registry.read(hyp_id).status == "validated"


def test_transition_monitoring_to_falsified(registry):
    """B25: monitoring → falsified reaches a terminal verdict."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "monitoring")
    registry.update_status(hyp_id, "falsified")
    assert registry.read(hyp_id).status == "falsified"


def test_transition_monitoring_to_abandoned(registry):
    """B25: monitoring → abandoned is allowed (escape hatch)."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "monitoring")
    registry.update_status(hyp_id, "abandoned")
    assert registry.read(hyp_id).status == "abandoned"


def test_invalid_transition_open_to_monitoring(registry):
    """B25: open → monitoring must go through running first."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    with pytest.raises(InvalidStatusTransition):
        registry.update_status(hyp_id, "monitoring")


def test_legacy_running_to_validated_still_allowed(registry):
    """B25 is additive: the legacy running → validated edge is preserved."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    registry.update_status(hyp_id, "running")
    registry.update_status(hyp_id, "validated")  # no monitoring hop required
    assert registry.read(hyp_id).status == "validated"


def test_read_all_monitoring(registry):
    """B25: read_all_monitoring isolates monitoring-status hypotheses."""
    id1 = registry.register(_minimal_hypothesis(claim="claim A"))
    id2 = registry.register(_minimal_hypothesis(claim="claim B"))
    registry.update_status(id1, "running")
    registry.update_status(id1, "monitoring")
    registry.update_status(id2, "running")
    monitoring_ids = {h.hypothesis_id for h in registry.read_all_monitoring()}
    assert id1 in monitoring_ids
    assert id2 not in monitoring_ids


# --- run-card linkage (B25) ---


def test_run_card_ids_defaults_empty(registry):
    """A freshly registered hypothesis has an empty run-card linkage."""
    hyp_id = registry.register(_minimal_hypothesis())
    assert registry.read(hyp_id).run_card_ids == []


def test_link_run_card_materialises_in_order(registry):
    """B25: link_run_card rows are replayed into run_card_ids in order."""
    hyp_id = registry.register(_minimal_hypothesis())
    registry.link_run_card(hyp_id, "run_001")
    registry.link_run_card(hyp_id, "run_002")
    assert registry.read(hyp_id).run_card_ids == ["run_001", "run_002"]


def test_link_run_card_is_idempotent(registry):
    """Re-linking an already-linked run_id is a no-op (deduplicated)."""
    hyp_id = registry.register(_minimal_hypothesis())
    registry.link_run_card(hyp_id, "run_001")
    registry.link_run_card(hyp_id, "run_001")
    assert registry.read(hyp_id).run_card_ids == ["run_001"]


def test_link_run_card_unknown_hypothesis_raises(registry):
    with pytest.raises(HypothesisNotFound):
        registry.link_run_card("hyp_GHOST_20250101_000000", "run_001")


def test_linkage_survives_status_changes(registry):
    """Linkage and status are materialised independently from one event log."""
    hyp_id = registry.register(_minimal_hypothesis())
    registry.update_status(hyp_id, "running")
    registry.link_run_card(hyp_id, "run_001")
    registry.update_status(hyp_id, "monitoring")
    registry.link_run_card(hyp_id, "run_002")
    recovered = registry.read(hyp_id)
    assert recovered.status == "monitoring"
    assert recovered.run_card_ids == ["run_001", "run_002"]


def test_legacy_row_without_run_card_ids_parses(registry):
    """Backward-compat: a registration row predating B25 (no run_card_ids
    field) reads back fine with run_card_ids defaulting to []."""
    import json

    legacy_row = {
        "schema_version": 1,
        "kind": "hypothesis",
        "hypothesis_id": "hyp_LEGACY_20250101_aaaaaa",
        "created_at": "2025-01-01T00:00:00+00:00",
        "author": "legacy-agent",
        "claim": "legacy claim",
        "null_hypothesis": "legacy null",
        "success_criteria": ["sharpe >= 0.10"],
        "falsification_criteria": ["sharpe < 0.0"],
        "experiment_design": "legacy design",
        "duration_target_days": 90,
        "scope": {"universe": ["AAPL"]},
        "related_adrs": [],
        "status": "open",
        # NOTE: no "run_card_ids" key — predates B25.
    }
    # Append the legacy row directly, bypassing register().
    with open(registry._path, "a") as fh:  # noqa: SLF001
        fh.write(json.dumps(legacy_row) + "\n")

    recovered = registry.read("hyp_LEGACY_20250101_aaaaaa")
    assert recovered is not None
    assert recovered.run_card_ids == []
    assert recovered.status == "open"
