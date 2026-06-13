"""hermes_quant.pdr_core.contracts — the frozen contract TRIAD (ADR-0092 Increment-1).

Three immutable dataclasses define the entire seam between a host SHELL and the
host-agnostic CORE:

  - :class:`AnalystView`  — the host-blind / modality-blind PERCEPTION seam. A shell
    produces this from whatever pipeline it runs (LLM, numerical, social-arb, Kronos,
    catalyst). It carries NO host types and NO infra references — only the directional
    view + calibrated confidence + provenance. This is a faithful lift of
    ``hermes_quant.protocol.AnalystView`` (same field names/semantics) widened with the
    asset/asset_class/asof/bar_ts fields the core needs to size and settle host-blind.
  - :class:`Proposal`     — the authorized, SIZED DECISION the core returns. Its
    ``target_position_pct`` lives on the discrete ladder {0, +-0.05, +-0.10, +-0.15,
    +-0.20} (mirrors ``governance.invariants.ACTION_SPACE``; the core keeps its OWN
    copy so the extraction stays mechanical and the core imports no governance/host
    module). Construction REJECTS any off-ladder size — the anti-leverage-gambling
    invariant is arithmetic, enforced at the boundary.
  - :class:`Fill`         — the REACTION feedback the shell pushes back after executing
    a Proposal. ``fill_size_pct`` is the ABSOLUTE post-fill target (Option E, per
    ADR-0091): downstream folds derive the traded delta via the shared
    ``FillDeltaNormalizer``, so re-affirming an unchanged target is a no-op.

Why frozen dataclasses (not Pydantic): ``hermes_quant.protocol`` — the existing money
contract layer — uses frozen dataclasses; the core mirrors that, depends only on the
stdlib, and stays trivially movable to a standalone repo. (Pydantic is used in the
``agents`` shell layer, which the core must never reach.)

Versioning rule (inherited from protocol.py): fields are added only, never renamed or
removed before a major version bump. New fields get sensible defaults; consumers ignore
unknown fields. ``Fill.schema_version`` carries the explicit feedback-contract version.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Type aliases — lifted from hermes_quant.protocol, kept host-blind.
# ---------------------------------------------------------------------------

Direction = Literal[-1, 0, 1]
"""-1 = short, 0 = flat, +1 = long. (Mirrors protocol.Direction.)"""

AssetClass = Literal["crypto", "equity", "etf", "fx", "option"]
"""Asset class. (Mirrors protocol.AssetClass.)"""


# ---------------------------------------------------------------------------
# Discrete position ladder — the core's OWN copy of the action space.
# ---------------------------------------------------------------------------

POSITION_LADDER: frozenset[float] = frozenset(
    {0.0, 0.05, -0.05, 0.10, -0.10, 0.15, -0.15, 0.20, -0.20}
)
"""The discrete target-position ladder (fraction of NAV), signed.

This is a byte-for-byte mirror of ``hermes_quant.governance.invariants.ACTION_SPACE``.
The core deliberately keeps its OWN copy rather than importing governance: the purity
contract (ADR-0092) forbids the core from reaching into host/governance modules, and a
duplicated immutable constant is the price of a mechanical extraction. A drift test in
the host repo can assert ``POSITION_LADDER == ACTION_SPACE`` to catch divergence.

Discreteness is the anti-leverage-gambling invariant: a Proposal may only target a rung
on this ladder, enforced arithmetically at construction time.
"""

# Float-equality tolerance for ladder membership. The ladder rungs are exact
# in the sense the producers use, but a value arrived at by arithmetic (e.g.
# 0.05 + 0.05) can land a few ULPs off; snap within tolerance before rejecting.
_LADDER_ATOL: float = 1e-9


def _on_ladder(value: float) -> bool:
    """True iff ``value`` is (within tolerance) a rung on POSITION_LADDER."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(v):
        return False
    return any(math.isclose(v, rung, abs_tol=_LADDER_ATOL) for rung in POSITION_LADDER)


# ---------------------------------------------------------------------------
# AnalystView — the host-blind / modality-blind PERCEPTION seam.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalystView:
    """A single analyst's view at a decision timestamp — the seam a host shell fills.

    Faithful lift of ``hermes_quant.protocol.AnalystView`` (same field names + semantics
    for ``analyst`` / ``direction`` / ``magnitude`` / ``confidence`` / ``confidence_raw``
    / ``horizon`` / ``rationale`` / ``evidence_ids``), widened with the asset identity and
    decision/bar timestamps the host-agnostic core needs to size and settle without
    knowing anything about the host.

    Semantics (per ADR-0002 + ADR-0009 §P0-2, inherited):
      - ``direction`` in {-1, 0, +1}.
      - ``magnitude`` in [0, 1]: normalized expected-move strength (the core sizer maps
        this onto the discrete ladder; it is host-blind so it carries a normalized
        magnitude rather than protocol.py's raw fractional return).
      - ``confidence`` is a CALIBRATED probability of directional correctness in [0, 1].
      - ``confidence_raw`` is the pre-calibration score (for debugging / calibrator
        training).
      - ``asof_decision`` is the decision timestamp; ``bar_ts`` is the timestamp of the
        bar the view was computed on (``bar_ts <= asof_decision`` — no-lookahead).
      - ``evidence_ids`` is provenance: a tuple of EvidenceRecord ids (ADR-0033) as
        strings, kept JSON-serializable.

    Timestamps are typed ``Any`` so a shell may pass an ISO-8601 string or a
    ``pandas.Timestamp`` without the core importing pandas. The core never parses them
    for arithmetic at this contract layer; downstream settlement does its own typing.
    """

    analyst: str
    asset: str
    asset_class: str
    direction: Direction
    magnitude: float  # normalized expected-move strength in [0, 1]
    confidence: float  # CALIBRATED probability in [0, 1]
    confidence_raw: float  # raw, uncalibrated score
    horizon: str  # e.g. "5m" | "1h" | "1d" — window over which the view holds
    asof_decision: Any  # decision timestamp (ISO str or pandas.Timestamp)
    bar_ts: Any  # bar the view was computed on; must be <= asof_decision
    rationale: str | None = None
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(
                f"AnalystView.direction must be one of (-1, 0, 1), got {self.direction!r}"
            )
        for name in ("magnitude", "confidence", "confidence_raw"):
            val = getattr(self, name)
            try:
                f = float(val)
            except (TypeError, ValueError):
                raise ValueError(
                    f"AnalystView.{name} must be a real number in [0, 1], got {val!r}"
                ) from None
            if not (math.isfinite(f) and 0.0 <= f <= 1.0):
                raise ValueError(
                    f"AnalystView.{name} must be a finite number in [0, 1], got {val!r}"
                )


# ---------------------------------------------------------------------------
# Proposal — the authorized, SIZED DECISION the core returns.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """The authorized, sized order intent the core returns to the shell.

    ``target_position_pct`` is a signed fraction of NAV that MUST sit on
    :data:`POSITION_LADDER`. Construction rejects any off-ladder size — discreteness
    is the anti-leverage-gambling invariant, enforced arithmetically at the boundary
    rather than trusted downstream. (Mirrors the ``ACTION_SPACE`` membership check in
    ``governance.invariants`` / the typed ``protocol.Proposal.target_size_pct_nav``.)

    Minimal by design: identity (``symbol`` / ``asset_class``), the sized target, the
    gate's human-readable reason, and the decision ``asof``. Richer linkage
    (proposal_id, evidence) lives on the shell-side ``protocol.Proposal``; the core's
    Proposal is the contract the shell executes against.
    """

    symbol: str
    asset_class: str
    target_position_pct: float  # signed NAV fraction; MUST be on POSITION_LADDER
    gate_reason: str
    asof: Any  # decision timestamp (ISO str or pandas.Timestamp)

    def __post_init__(self) -> None:
        if not _on_ladder(self.target_position_pct):
            raise ValueError(
                "Proposal.target_position_pct must be on the discrete ladder "
                f"{sorted(POSITION_LADDER)}, got {self.target_position_pct!r}. "
                "Off-ladder sizes are rejected (anti-leverage-gambling invariant)."
            )


# ---------------------------------------------------------------------------
# Fill — the REACTION feedback the shell pushes back after execution.
# ---------------------------------------------------------------------------

FILL_SCHEMA_VERSION: int = 1
"""Current Fill feedback-contract schema version (Option E absolute-target semantics)."""


@dataclass(frozen=True)
class Fill:
    """Execution feedback a shell pushes back to the core after acting on a Proposal.

    ``fill_size_pct`` is the ABSOLUTE post-fill target (NAV fraction), NOT an
    incremental traded delta — this is the Option E convention from ADR-0091. The core's
    settlement layer derives the traded delta from the absolute target via the shared
    ``FillDeltaNormalizer`` (``running_net``-carry), so re-affirming an unchanged target
    folds to a delta of 0. Producers stay simple (emit the target they targeted); the
    fold does the differencing exactly once, in one place.

    ``schema_version`` makes the feedback-contract version explicit so a shell on an
    older contract can be read-time upcast (Option E) rather than silently misfolded.
    """

    proposal_id: str
    asset: str
    asset_class: str
    fill_price: float
    fill_size_pct: float  # ABSOLUTE post-fill target (NAV fraction), per Option E
    asof_execution: Any  # execution timestamp (ISO str or pandas.Timestamp)
    schema_version: int = FILL_SCHEMA_VERSION
    metadata: Mapping[str, Any] | None = field(default=None)
