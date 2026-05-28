"""hermes_quant.risk.portfolio_normalize — portfolio-aware Kelly stage-2 sizing (ADR-0071).

Per-symbol Kelly (existing `quarter_kelly_size` in `risk/kelly.py`) is *individually*
prudent and *jointly* reckless for a portfolio of correlated bets at full Kelly. With
43 picks each sized at ±20% NAV, the gate produces 860% gross exposure — no cash
reserve, no correlated-day blow-up bound, no parity with live broker margin.

This module is the portfolio-layer second stage: it reads current portfolio state +
proposed new targets and either scales them (uniform λ ∈ (0, 1]) or clips them
(greedy first-come-first-served) to fit within three caps:

  * `max_gross_exposure_pct` — sum of |target_pct| ≤ this. Default 200% NAV.
  * `max_net_exposure_pct`   — abs(sum of target_pct) ≤ this. Default 100% NAV.
  * `min_cash_reserve_pct`   — at least this NAV always free. Default 20% NAV.

Two policies:

  * `scale_to_fit` (default)  — preserves relative Kelly ranking by uniform λ.
                                For a batch of pending picks, every pick is scaled
                                by the same λ. Relative ordering preserved.
  * `priority_rank`           — sorts by abs(target_pct) descending and accepts in
                                that order until any cap binds; remaining picks dropped.

Use `normalize_targets()` for batch normalization (the canonical batch pattern).
Use `clip_one_to_remaining_headroom()` for greedy online single-symbol fires
(the autonomous-tick pattern, where decisions stream in one at a time).

Existing positions are read-only here — Stage-2 does NOT retroactively re-size
them. That's the rebalancer's job (ADR-0035 wave-4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioCaps:
    """Per-account portfolio-level caps. ADR-0071 §D71.2.

    All values are fractions of NAV. Defaults are operator-tunable via
    RiskConfig but the defaults below were chosen as the "obviously safer
    than the status quo" baseline:
      * 200% gross   ≈ "2× leveraged" common bound
      * 100% net     ≈ no leveraged net direction without explicit operator override
      * 20% cash     ≈ always-room-for-one-more-conviction headroom
    """

    max_gross_exposure_pct: float = 2.0
    max_net_exposure_pct: float = 1.0
    min_cash_reserve_pct: float = 0.20
    normalization: str = "scale_to_fit"  # "scale_to_fit" | "priority_rank"

    def __post_init__(self) -> None:
        if self.max_gross_exposure_pct <= 0:
            raise ValueError(
                f"max_gross_exposure_pct must be > 0, got {self.max_gross_exposure_pct}"
            )
        if self.max_net_exposure_pct < 0:
            raise ValueError(
                f"max_net_exposure_pct must be >= 0, got {self.max_net_exposure_pct}"
            )
        if not 0.0 <= self.min_cash_reserve_pct < 1.0:
            raise ValueError(
                f"min_cash_reserve_pct must be in [0, 1), got {self.min_cash_reserve_pct}"
            )
        if self.normalization not in ("scale_to_fit", "priority_rank"):
            raise ValueError(
                f"normalization must be 'scale_to_fit' or 'priority_rank', "
                f"got {self.normalization!r}"
            )

    # Profile constructors mirror RiskConfig.profile_* shape.

    @classmethod
    def conservative(cls) -> PortfolioCaps:
        return cls(
            max_gross_exposure_pct=1.0,
            max_net_exposure_pct=0.5,
            min_cash_reserve_pct=0.40,
        )

    @classmethod
    def standard(cls) -> PortfolioCaps:
        return cls()

    @classmethod
    def aggressive(cls) -> PortfolioCaps:
        return cls(
            max_gross_exposure_pct=3.0,
            max_net_exposure_pct=1.5,
            min_cash_reserve_pct=0.10,
        )


@dataclass(frozen=True)
class PortfolioState:
    """Read-only snapshot of current portfolio used by Stage-2 normalization.

    `positions` maps symbol -> signed target_position_pct of NAV (the LATEST
    `target_position_pct` per symbol from `executions.jsonl`, not delta-summed —
    PaperReactor semantics treat each fill as the new target, not an addition).

    `cash_pct` is implied: 1 - sum(abs(positions)). May be negative if the
    book is over-leveraged (which is exactly the case ADR-0071 was written to
    catch). Stage-2 fails closed in that case (silences all new picks).
    """

    positions: dict[str, float] = field(default_factory=dict)

    @property
    def gross_exposure_pct(self) -> float:
        return sum(abs(p) for p in self.positions.values())

    @property
    def net_exposure_pct(self) -> float:
        return sum(self.positions.values())

    @property
    def cash_pct(self) -> float:
        return 1.0 - self.gross_exposure_pct


# ---------------------------------------------------------------------------
# Helpers — headroom computation
# ---------------------------------------------------------------------------


def _headroom(state: PortfolioState, caps: PortfolioCaps) -> tuple[float, float, float]:
    """Return (gross_headroom, net_headroom_signed, cash_headroom).

    All three are NAV-fractions of additional |delta| we can absorb before
    the corresponding cap binds. Negative values mean the corresponding cap
    is already breached.

    `net_headroom_signed` is the symmetric one-sided remaining net exposure
    on each side: if current net is +0.3 and cap is 1.0, you have 0.7 long-side
    room and 1.3 short-side room. We return min of those for conservative
    sizing.
    """
    gross = state.gross_exposure_pct
    net = state.net_exposure_pct
    gross_headroom = caps.max_gross_exposure_pct - gross
    cash_headroom = (1.0 - caps.min_cash_reserve_pct) - gross
    long_room = caps.max_net_exposure_pct - net
    short_room = caps.max_net_exposure_pct + net
    net_headroom = min(long_room, short_room)
    return gross_headroom, net_headroom, cash_headroom


def headroom_summary(state: PortfolioState, caps: PortfolioCaps) -> dict[str, float]:
    """Operator-readable view of remaining headroom under each cap."""
    g, n, c = _headroom(state, caps)
    return {
        "gross_exposure_pct": state.gross_exposure_pct,
        "net_exposure_pct": state.net_exposure_pct,
        "cash_pct": state.cash_pct,
        "gross_headroom": g,
        "net_headroom_min_side": n,
        "cash_headroom": c,
    }


# ---------------------------------------------------------------------------
# Batch normalization — Policy A (scale_to_fit) / Policy B (priority_rank)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedTarget:
    """Per-symbol Stage-2 output."""

    asset: str
    per_symbol_target_pct: float       # input from Stage-1 Kelly
    portfolio_target_pct: float         # post-normalization
    scale_factor: float                 # 0.0 ≤ s ≤ 1.0
    fired: bool                         # False = silenced (cap bound elsewhere)
    silence_reason: str | None = None   # set when fired=False


def normalize_targets(
    per_symbol_targets: list[tuple[str, float]],
    state: PortfolioState,
    caps: PortfolioCaps,
) -> list[NormalizedTarget]:
    """Stage-2 portfolio-aware sizing for a batch of new picks.

    Args:
        per_symbol_targets: list of (asset, target_pct). target_pct is the
            output of Stage-1 (per-symbol quarter-Kelly), already clipped to
            the per-symbol cap.
        state: current portfolio snapshot. Existing positions are NOT
            re-sized — they consume headroom.
        caps: portfolio-level caps + normalization policy.

    Returns:
        List of NormalizedTarget, one per input, in input order.

    Behavior:
        * Empty input: empty output.
        * State already breaches a cap: every target silenced with reason
          "headroom_breached".
        * Demand fits within all caps: pass-through (scale=1.0).
        * Demand exceeds a cap under "scale_to_fit": every target scaled by
          uniform λ ∈ (0, 1].
        * Demand exceeds a cap under "priority_rank": targets sorted by
          |target_pct| descending; accept in order until any cap binds, drop
          the rest.

    The function is deterministic given (per_symbol_targets, state, caps).
    """
    out: list[NormalizedTarget] = []
    if not per_symbol_targets:
        return out

    g_room, n_room, c_room = _headroom(state, caps)

    # If the existing book has already breached any cap, fail closed —
    # silence every new pick. The rebalancer is responsible for bringing
    # the book back inside caps before new fires resume.
    if g_room <= 0 or c_room <= 0:
        reason = (
            f"headroom_breached gross={state.gross_exposure_pct:.3f} "
            f"vs max_gross={caps.max_gross_exposure_pct:.3f} "
            f"min_cash={caps.min_cash_reserve_pct:.3f}"
        )
        return [
            NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=t,
                portfolio_target_pct=0.0,
                scale_factor=0.0,
                fired=False,
                silence_reason=reason,
            )
            for asset, t in per_symbol_targets
        ]

    # The most-restrictive headroom against the demand from the new batch.
    # `gross_demand` is the total absolute size requested.
    # `net_demand_signed` is the signed sum requested.
    gross_demand = sum(abs(t) for _, t in per_symbol_targets)
    net_demand = sum(t for _, t in per_symbol_targets)

    if caps.normalization == "priority_rank":
        return _normalize_priority_rank(
            per_symbol_targets, state, caps, g_room, c_room
        )

    # Default policy: scale_to_fit
    return _normalize_scale_to_fit(
        per_symbol_targets, gross_demand, net_demand, g_room, n_room, c_room
    )


def _normalize_scale_to_fit(
    per_symbol_targets: list[tuple[str, float]],
    gross_demand: float,
    net_demand: float,
    g_room: float,
    n_room: float,
    c_room: float,
) -> list[NormalizedTarget]:
    """Policy A — preserves relative Kelly ranking by uniform λ."""
    # λ_gross: gross can absorb at most g_room more |delta|.
    # λ_cash:  cash sleeve allows at most c_room more |delta|.
    # λ_net:   the *signed* sum must end up in [-net_cap, +net_cap].
    #          Existing net + λ * net_demand ∈ [-net_cap, +net_cap]
    #          → λ * net_demand ∈ [-n_room_lower, +n_room_upper]; we use
    #          the symmetric n_room (min side) for simplicity / safety.
    lambdas: list[float] = [1.0]
    if gross_demand > 0:
        lambdas.append(g_room / gross_demand)
        lambdas.append(c_room / gross_demand)
    if abs(net_demand) > 0 and n_room > 0:
        lambdas.append(n_room / abs(net_demand))
    elif abs(net_demand) > 0 and n_room <= 0:
        # net cap already at its bound on the side we'd push into.
        # Conservative: silence net-additive picks entirely.
        lambdas.append(0.0)

    lam = max(0.0, min(lambdas))
    out: list[NormalizedTarget] = []
    for asset, t in per_symbol_targets:
        normalized = lam * t
        out.append(
            NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=t,
                portfolio_target_pct=normalized,
                scale_factor=lam,
                fired=normalized != 0.0,
                silence_reason=None if normalized != 0.0 else "scale_to_fit_lambda_zero",
            )
        )
    return out


def _normalize_priority_rank(
    per_symbol_targets: list[tuple[str, float]],
    state: PortfolioState,
    caps: PortfolioCaps,
    g_room_init: float,
    c_room_init: float,
) -> list[NormalizedTarget]:
    """Policy B — sort by |target| desc, accept until any cap binds."""
    # Build (original_index, asset, target) tuples, sort by |target| desc.
    indexed = list(enumerate(per_symbol_targets))
    indexed.sort(key=lambda iat: -abs(iat[1][1]))

    decisions: dict[int, NormalizedTarget] = {}
    g_remaining = g_room_init
    c_remaining = c_room_init
    running_net = state.net_exposure_pct

    for orig_idx, (asset, t) in indexed:
        size = abs(t)
        if size <= 0:
            decisions[orig_idx] = NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=t,
                portfolio_target_pct=0.0,
                scale_factor=0.0,
                fired=False,
                silence_reason="zero_target",
            )
            continue
        # Net-side remaining check
        prospective_net = running_net + t
        if abs(prospective_net) > caps.max_net_exposure_pct:
            decisions[orig_idx] = NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=t,
                portfolio_target_pct=0.0,
                scale_factor=0.0,
                fired=False,
                silence_reason=(
                    f"priority_rank_net_cap_bound "
                    f"prospective_net={prospective_net:.4f} "
                    f"vs max_net={caps.max_net_exposure_pct:.4f}"
                ),
            )
            continue
        if size > g_remaining or size > c_remaining:
            decisions[orig_idx] = NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=t,
                portfolio_target_pct=0.0,
                scale_factor=0.0,
                fired=False,
                silence_reason=(
                    f"priority_rank_gross_or_cash_bound "
                    f"size={size:.4f} g_room={g_remaining:.4f} "
                    f"c_room={c_remaining:.4f}"
                ),
            )
            continue
        # Accept full size
        decisions[orig_idx] = NormalizedTarget(
            asset=asset,
            per_symbol_target_pct=t,
            portfolio_target_pct=t,
            scale_factor=1.0,
            fired=True,
            silence_reason=None,
        )
        g_remaining -= size
        c_remaining -= size
        running_net = prospective_net

    # Restore original input order
    return [decisions[i] for i in range(len(per_symbol_targets))]


# ---------------------------------------------------------------------------
# Online (greedy) clip — for streaming one-at-a-time decisions
# ---------------------------------------------------------------------------


def clip_one_to_remaining_headroom(
    asset: str,
    per_symbol_target_pct: float,
    state: PortfolioState,
    caps: PortfolioCaps,
) -> NormalizedTarget:
    """Greedy first-come-first-served Stage-2 for a single new pick.

    Use this when decisions stream in one at a time (autonomous-tick fire
    loop) and you'd rather size each pick to fit within remaining headroom
    than batch them up. Compared to `normalize_targets`, this:

      * Does NOT preserve the per-symbol Kelly relative ranking — late
        picks may be silenced or shrunk if early picks consumed the budget.
      * Always fires at least up to the remaining headroom (greedy).
      * Is order-dependent: shuffling the input order changes which picks
        fire and at what size.

    Returns a NormalizedTarget. The `state` argument is treated as
    read-only — the caller is responsible for updating their running state
    (or refreshing from `executions.jsonl`) between calls.
    """
    if per_symbol_target_pct == 0.0:
        return NormalizedTarget(
            asset=asset,
            per_symbol_target_pct=0.0,
            portfolio_target_pct=0.0,
            scale_factor=0.0,
            fired=False,
            silence_reason="zero_target",
        )

    g_room, n_room, c_room = _headroom(state, caps)

    if g_room <= 0 or c_room <= 0:
        return NormalizedTarget(
            asset=asset,
            per_symbol_target_pct=per_symbol_target_pct,
            portfolio_target_pct=0.0,
            scale_factor=0.0,
            fired=False,
            silence_reason=(
                f"headroom_breached gross={state.gross_exposure_pct:.3f} "
                f"cash={state.cash_pct:.3f}"
            ),
        )

    requested = abs(per_symbol_target_pct)
    sign = 1.0 if per_symbol_target_pct > 0 else -1.0

    # Net cap: prospective net after this pick must stay in [-net_cap, +net_cap]
    prospective_net = state.net_exposure_pct + per_symbol_target_pct
    if abs(prospective_net) > caps.max_net_exposure_pct:
        # Shrink to land at the cap on this side.
        # Solve: existing_net + sign * x = sign * net_cap  →  x = sign*net_cap - existing_net
        net_cap_signed = sign * caps.max_net_exposure_pct
        max_net_signed_addition = net_cap_signed - state.net_exposure_pct
        # max_net_signed_addition has the same sign as `sign` only if there's
        # genuine room; otherwise it's zero or wrong-signed and we silence.
        if max_net_signed_addition * sign <= 0:
            return NormalizedTarget(
                asset=asset,
                per_symbol_target_pct=per_symbol_target_pct,
                portfolio_target_pct=0.0,
                scale_factor=0.0,
                fired=False,
                silence_reason=(
                    f"net_cap_bound prospective_net={prospective_net:.4f} "
                    f"vs max_net={caps.max_net_exposure_pct:.4f}"
                ),
            )
        requested = min(requested, abs(max_net_signed_addition))

    # Gross + cash caps: take the more restrictive
    headroom = min(g_room, c_room)
    accepted = min(requested, headroom)
    accepted = max(0.0, accepted)
    portfolio_target = sign * accepted
    scale = accepted / abs(per_symbol_target_pct) if per_symbol_target_pct != 0 else 0.0

    if portfolio_target == 0.0:
        return NormalizedTarget(
            asset=asset,
            per_symbol_target_pct=per_symbol_target_pct,
            portfolio_target_pct=0.0,
            scale_factor=0.0,
            fired=False,
            silence_reason="clip_to_zero (no headroom)",
        )

    return NormalizedTarget(
        asset=asset,
        per_symbol_target_pct=per_symbol_target_pct,
        portfolio_target_pct=portfolio_target,
        scale_factor=scale,
        fired=True,
        silence_reason=None,
    )
