"""Tests for hermes_quant.governance.invariants (ADR-0031 D6)."""
from __future__ import annotations

import pytest

from hermes_quant.governance import invariants
from hermes_quant.governance.invariants import (
    ACTION_SPACE,
    IMMUTABLE_INVARIANTS,
    InvariantAllowlistOverlap,
    RETRO_BLOCKLIST_PATHS,
    assert_disjoint_from,
    check,
)


def test_immutable_invariants_count_is_11() -> None:
    assert len(IMMUTABLE_INVARIANTS) == 11


def test_action_space_is_frozen() -> None:
    assert isinstance(ACTION_SPACE, frozenset)
    # Per AGENTS.md "Action space is discrete"
    expected = {0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20}
    assert ACTION_SPACE == frozenset(expected)


def test_action_space_immutable() -> None:
    """frozenset has no add/remove — assignment is the only attack vector,
    and this is by convention a module-level constant."""
    with pytest.raises(AttributeError):
        ACTION_SPACE.add(0.25)  # type: ignore[attr-defined]


def test_governance_module_blocked_from_retro_code_change() -> None:
    """ADR-0031 D7: governance/** must be on the retro blocklist."""
    assert "hermes_quant/governance/**" in RETRO_BLOCKLIST_PATHS


def test_invariants_list_disjoint_from_retro_allowlist() -> None:
    """Per ADR-0026 (patched 2026-05-24), code_change_allowlist names file
    paths, not invariant strings. The disjointness contract: no
    invariant *name* may appear in an allowlist path string.

    This test enforces the four documented allowlist scopes per the
    ADR-0026 patch in ADR-0031 D7.
    """
    retro_allowlist = (
        "hermes_quant/risk/**",
        "hermes_quant/proposals.py",
        "methodology/*.yaml",
    )
    overlap = set(IMMUTABLE_INVARIANTS) & set(retro_allowlist)
    assert overlap == set()
    # Sanity: the call form raises on overlap
    assert_disjoint_from(retro_allowlist)


def test_assert_disjoint_from_raises_on_overlap() -> None:
    bad = ("action_space_discrete", "something_else")
    with pytest.raises(InvariantAllowlistOverlap):
        assert_disjoint_from(bad)


def test_check_action_space_discrete_pass() -> None:
    assert check("action_space_discrete", {"position_size_pct_nav": 0.10}) is True


def test_check_action_space_discrete_fail() -> None:
    assert check("action_space_discrete", {"position_size_pct_nav": 0.13}) is False


def test_check_unknown_invariant_raises() -> None:
    with pytest.raises(ValueError):
        check("not_a_real_invariant", {})


def test_check_governance_module_immune_to_retro() -> None:
    assert (
        check(
            "governance_module_immune_to_retro",
            {"retro_target_path": "hermes_quant/risk/gate.py"},
        )
        is True
    )
    assert (
        check(
            "governance_module_immune_to_retro",
            {"retro_target_path": "hermes_quant/governance/audit_log.py"},
        )
        is False
    )


def test_check_live_broker_requires_approval() -> None:
    assert (
        check(
            "live_broker_requires_approval",
            {"broker_kind": "live", "approval_token_present": False},
        )
        is False
    )
    assert (
        check(
            "live_broker_requires_approval",
            {"broker_kind": "live", "approval_token_present": True},
        )
        is True
    )
    # Paper broker not subject to this invariant
    assert (
        check(
            "live_broker_requires_approval",
            {"broker_kind": "paper", "approval_token_present": False},
        )
        is True
    )
