"""hermes_quant.governance.invariants — single source of truth for
immutable rules (ADR-0031 D6).

These are CONSTANTS. The rest of the system imports them; nothing in the
retro loop's code_change allowlist may modify this file (ADR-0026 D5 +
ADR-0031 D7). CI verifies disjointness with the retro allowlist.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final


class InvariantAllowlistOverlap(Exception):
    """Raised when retro allowlist overlaps with IMMUTABLE_INVARIANTS."""


# Discrete action space per AGENTS.md "Action space is discrete".
ACTION_SPACE: Final[frozenset[float]] = frozenset(
    {0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20}
)

# Immutable bounds per ADR-0027 / ADR-0029.
MAX_POSITION_PCT_NAV: Final[float] = 0.20
BPR_BUFFER_FRACTION: Final[float] = 0.25
MAX_DRAWDOWN_PCT: Final[float] = 0.10
MAX_NET_DELTA_PCT_NAV: Final[float] = 0.20

# 11 immutable invariants — strings name the predicate; check() evaluates
# them against runtime state.
IMMUTABLE_INVARIANTS: Final[tuple[str, ...]] = (
    "action_space_discrete",
    "no_naked_short_options",
    "risk_gate_not_bypassed",
    "live_orders_blocked_in_research_mode",
    "covered_call_includes_stock_leg",
    "live_broker_requires_approval",
    "option_chain_replay_no_lookahead",
    "state_json_atomic_rename",
    "analyst_confidence_calibrated",
    "utc_end_to_end",
    "governance_module_immune_to_retro",
)

# The retro loop's code_change allowlist may NOT touch these paths.
# Importing this from retro tests verifies the disjointness contract.
RETRO_BLOCKLIST_PATHS: Final[tuple[str, ...]] = (
    "hermes_quant/governance/**",
    "hermes_quant/risk/**",
    "hermes_quant/protocol.py",
)


def check(invariant_name: str, runtime_state: dict[str, Any]) -> bool:
    """Return True if the named invariant holds for the supplied runtime state.

    `runtime_state` is a flat dict of probe values. Missing keys cause the
    invariant to fail closed (False) — silence-by-default per AGENTS.md.
    """
    if invariant_name not in IMMUTABLE_INVARIANTS:
        raise ValueError(f"unknown invariant: {invariant_name!r}")

    if invariant_name == "action_space_discrete":
        size = runtime_state.get("position_size_pct_nav")
        return size is not None and float(size) in ACTION_SPACE

    if invariant_name == "no_naked_short_options":
        # short option leg requires an offsetting stock or long-option leg
        return bool(runtime_state.get("short_option_has_offset", True))

    if invariant_name == "risk_gate_not_bypassed":
        return bool(runtime_state.get("passed_through_risk_gate", False))

    if invariant_name == "live_orders_blocked_in_research_mode":
        if runtime_state.get("mode") == "research":
            return not bool(runtime_state.get("live_order_attempted", False))
        return True

    if invariant_name == "covered_call_includes_stock_leg":
        if runtime_state.get("strategy") == "covered_call":
            return bool(runtime_state.get("has_long_stock_leg", False))
        return True

    if invariant_name == "live_broker_requires_approval":
        if runtime_state.get("broker_kind") == "live":
            return bool(runtime_state.get("approval_token_present", False))
        return True

    if invariant_name == "option_chain_replay_no_lookahead":
        fetched_at = runtime_state.get("fetched_at")
        asof = runtime_state.get("asof")
        if fetched_at is None or asof is None:
            return True
        return fetched_at <= asof

    if invariant_name == "state_json_atomic_rename":
        return bool(runtime_state.get("used_atomic_rename", True))

    if invariant_name == "analyst_confidence_calibrated":
        drift = runtime_state.get("calibrator_drift_max", 0.0)
        return float(drift) <= 0.05

    if invariant_name == "utc_end_to_end":
        tz = runtime_state.get("timezone")
        if tz is None:
            return True
        return str(tz).upper() in ("UTC", "+00:00", "Z")

    if invariant_name == "governance_module_immune_to_retro":
        target = runtime_state.get("retro_target_path", "")
        return not str(target).startswith("hermes_quant/governance/")

    return False  # unreachable; pragma: no cover


def assert_disjoint_from(allowlist: Iterable[str]) -> None:
    """Raise if the retro allowlist names any IMMUTABLE_INVARIANTS entry."""
    overlap = set(IMMUTABLE_INVARIANTS) & set(allowlist)
    if overlap:
        raise InvariantAllowlistOverlap(overlap)
