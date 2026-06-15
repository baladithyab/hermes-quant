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

AssetClass = Literal["crypto", "equity", "etf", "fx", "option", "us_option"]
"""Asset class. (Mirrors protocol.AssetClass.)

``us_option`` is the token the live host money-state stamps on real US
equity-option fills (``react.multileg`` -> ``state.portfolio_state``), where the
×100 contract multiplier gates EXACTLY on the literal string ``"us_option"``.
``option`` is the generic/legacy family token. BOTH are members so the contract
vocabulary RECOGNIZES the host's live token: the seam that will own
sizing+settlement (ADR-0092) must key the contract multiplier on the option
FAMILY (:data:`OPTION_ASSET_CLASSES` / :func:`is_option_asset_class`), not on a
single string — otherwise a ``Fill.asset_class == "us_option"`` settled by a core
that only checks ``== "option"`` silently misses the ×100 (ac1). Adding the member
is purely additive: every ``asset_class`` field is typed ``str`` (no Literal
validation at construction), so the live money-state behavior is byte-identical.
"""

# The option FAMILY — the set of asset_class tokens that denote an options
# contract and therefore carry the ×100 share-per-contract multiplier in
# settlement. The host money-state (``state.portfolio_state``) gates the live
# multiplier on the literal ``"us_option"``; the host-agnostic seam that will own
# settlement (ADR-0092) MUST recognize the whole family so it never misses the
# multiplier on a token the host already uses. ``"option"`` is the generic Literal
# member; ``"us_option"`` is the live host stamp. Keep this in sync with the
# option members of :data:`AssetClass`.
OPTION_ASSET_CLASSES: frozenset[str] = frozenset({"option", "us_option"})
"""The asset_class tokens that denote an options contract (carry the ×100 multiplier)."""


def is_option_asset_class(asset_class: str | None) -> bool:
    """True iff ``asset_class`` denotes an options contract (the ×100 family).

    The host-agnostic settlement seam (ADR-0092) keys the contract multiplier on
    THIS family recognizer — not on a single string — so a ``us_option`` fill (the
    live host stamp) and a generic ``option`` fill are both recognized. Keying on a
    bare ``== "option"`` would silently miss the ×100 on the live token (ac1)."""
    return asset_class in OPTION_ASSET_CLASSES


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
        # av1: reject ``bool`` explicitly. In Python ``bool`` subclasses ``int``
        # (``True == 1``, ``False == 0``, ``float(True) == 1.0``), so a bool would
        # silently pass both the ``not in (-1,0,1)`` direction check and the
        # ``float(val)`` [0,1] range checks below — slipping into the typed
        # host-blind contract (Direction int / calibrated float) the core sizer
        # and gate read. A bool direction even arithmetic-multiplies undetected in
        # the vote (``v.direction * w * v.confidence``). Guard the bool type first.
        if isinstance(self.direction, bool) or self.direction not in (-1, 0, 1):
            raise ValueError(
                f"AnalystView.direction must be one of (-1, 0, 1), got {self.direction!r}"
            )
        for name in ("magnitude", "confidence", "confidence_raw"):
            val = getattr(self, name)
            if isinstance(val, bool):
                raise ValueError(
                    f"AnalystView.{name} must be a real number in [0, 1], got {val!r}"
                )
            # cs83: reject any non-(int|float) type, BEFORE the float() coercion
            # below. av1's ``float(val)`` validates a LOCAL copy then DISCARDS it,
            # so a str/Decimal/numpy value that coerces into [0, 1] is STORED RAW
            # on the frozen dataclass, off-type vs the declared float contract. A
            # str confidence/magnitude then breaks the perception-fusion path
            # (aggregate.py ``v.direction * w * v.confidence`` / ``v.magnitude * w``
            # -> sequence-multiply / TypeError). (``bool`` is an ``int`` subclass;
            # the bool guard above runs first so ``(int, float)`` won't re-admit it.)
            if not isinstance(val, (int, float)):
                raise ValueError(
                    f"AnalystView.{name} must be a real number (int|float) in "
                    f"[0, 1], got {type(val).__name__} {val!r} (a str/Decimal/numpy "
                    "value is stored off-type and breaks the vote-fusion path)."
                )
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
        # cs82: reject ``bool`` explicitly, BEFORE the ladder check. In Python
        # ``bool`` subclasses ``int`` (``False == 0.0``, ``True == 1.0``), so a
        # bool ``target_position_pct`` silently passes ``_on_ladder`` —
        # ``False`` snaps to rung ``0.0`` (a flat verdict) and would slip into
        # the SIZED DECISION the shell executes against, typed as a float. Mirror
        # av1's AnalystView guard: reject the bool type at the boundary so the
        # core stays the trustworthy seam.
        if isinstance(self.target_position_pct, bool):
            raise ValueError(
                "Proposal.target_position_pct must be a real number on the "
                f"discrete ladder, got bool {self.target_position_pct!r} "
                "(a bool snaps to a ladder rung and corrupts the sized decision)."
            )
        # cs83: reject any non-(int|float) type, BEFORE the ladder check. The
        # field is declared ``float`` but the FROZEN dataclass STORES the
        # constructor argument unchanged, and ``_on_ladder`` only validates a
        # local ``float(value)`` copy (line ~113) — so a str/Decimal/numpy value
        # that happens to coerce onto a rung is STORED RAW, off-type. A str then
        # crashes the money path (``p.target_position_pct < 0`` -> TypeError;
        # ``p.target_position_pct * nav`` -> str sequence-multiply garbage); a
        # Decimal/numpy is a silent type mismatch against the declared float
        # contract. The sole in-core producer (quarter_kelly_size) returns a
        # genuine python float, so reject (strict typed contract) rather than
        # coerce. (``bool`` is an ``int`` subclass, so the bool guard MUST run
        # first — it does — or ``(int, float)`` would re-admit it.)
        if not isinstance(self.target_position_pct, (int, float)):
            raise ValueError(
                "Proposal.target_position_pct must be a real number (int|float) "
                f"on the discrete ladder, got {type(self.target_position_pct).__name__} "
                f"{self.target_position_pct!r} (a str/Decimal/numpy value is stored "
                "off-type and breaks NAV sizing)."
            )
        if not _on_ladder(self.target_position_pct):
            raise ValueError(
                "Proposal.target_position_pct must be on the discrete ladder "
                f"{sorted(POSITION_LADDER)}, got {self.target_position_pct!r}. "
                "Off-ladder sizes are rejected (anti-leverage-gambling invariant)."
            )


# ---------------------------------------------------------------------------
# Fill — the REACTION feedback the shell pushes back after execution.
# ---------------------------------------------------------------------------

# The canonical Option-E schema sentinel lives HERE (the contract layer) so the
# host fold classifier (react.base.is_absolute_target_record) and this contract
# agree by construction — react.base imports THIS, removing the duplicate string.
# It is a string (not an int) because that is the wire value already persisted in
# every ExecutionRecord and the value the fold classifier matches. sv1: an int here
# silently classified a Fill-driven record as true-delta and re-inflated positions.
SCHEMA_ABSOLUTE_TARGET: str = "absolute-target-v1"
FILL_SCHEMA_VERSION: str = SCHEMA_ABSOLUTE_TARGET
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
    schema_version: str = FILL_SCHEMA_VERSION
    metadata: Mapping[str, Any] | None = field(default=None)

    def __post_init__(self) -> None:
        # cs82: reject ``bool`` explicitly, BEFORE the finiteness/>0 checks. In
        # Python ``bool`` subclasses ``int`` (``True`` is finite & > 0, ``False``
        # is finite), so a bool ``fill_price``/``fill_size_pct`` silently passes
        # the fl1 isfinite/>0 guards below — a bool ``fill_price`` (``True`` ->
        # ``$1.00``) lands in the cash basis and a bool ``fill_size_pct`` lands
        # in FillDeltaNormalizer.running_net as an absolute target. Mirror av1's
        # AnalystView guard: reject the bool type at the boundary first.
        for _name in ("fill_price", "fill_size_pct"):
            _val = getattr(self, _name)
            if isinstance(_val, bool):
                raise ValueError(
                    f"Fill.{_name} must be a real number, got bool "
                    f"{_val!r} (a bool slips through the "
                    "isfinite/>0 checks and corrupts the cash basis / running_net)."
                )
            # cs83: reject any non-(int|float) type, BEFORE the isfinite/>0
            # checks. The field is declared ``float`` but the FROZEN dataclass
            # STORES the constructor argument unchanged. A str is already
            # rejected here only by accident (``math.isfinite('1.5')`` raises);
            # make that explicit and also catch the SILENT cases —
            # ``math.isfinite(Decimal('1.5'))`` is True and numpy floats pass, so
            # a Decimal/numpy value is STORED RAW into the cash basis /
            # FillDeltaNormalizer.running_net, off-type vs the declared float
            # contract. The shell pushes back a genuine float, so reject (strict
            # typed contract). (``bool`` is an ``int`` subclass; the bool guard
            # above runs first so ``(int, float)`` does not re-admit it.)
            if not isinstance(_val, (int, float)):
                raise ValueError(
                    f"Fill.{_name} must be a real number (int|float), got "
                    f"{type(_val).__name__} {_val!r} (a str/Decimal/numpy value "
                    "is stored off-type and corrupts the cash basis / running_net)."
                )
        if not math.isfinite(self.fill_size_pct):
            raise ValueError(
                f"Fill.fill_size_pct must be finite, got {self.fill_size_pct!r} "
                "(a NaN/inf target poisons the carry-forward running_net)."
            )
        if not math.isfinite(self.fill_price) or self.fill_price <= 0.0:
            raise ValueError(
                f"Fill.fill_price must be finite and > 0, got {self.fill_price!r} "
                "(a 0/negative/NaN price corrupts the cash basis)."
            )
