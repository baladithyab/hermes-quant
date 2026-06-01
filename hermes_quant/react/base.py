"""hermes_quant.react.base — Reactor Protocol + ExecutionRecord (ADR-0015)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecutionRecord:
    """Per-execution audit record. Matches the executions.jsonl shape the
    daemon's settlement loop expects (per ADR-0015 §D6).

    Fields populated by the reactor:
      - fill_price: decision_price for paper, broker-reported for live
      - fill_size_pct: actual filled size as % NAV (may diverge from
        target on partial fills in live mode; equals target for paper)
      - reactor_metadata: free-form dict for adapter-specific receipt info
    """

    proposal_id: str
    signal_id: str | None  # links back to the AggregatedSignal that drove the proposal
    asset: str
    asset_class: str
    timeframe: str
    asof_decision: str  # ISO UTC: when the advisor view was computed (wall-clock per ADR-0068)
    asof_execution: str  # ISO UTC: when React fired
    target_position_pct: float  # signed, e.g. +0.05 = 5% NAV long
    decision_price: float  # last_close at advisor time
    fill_price: float  # paper=decision_price; live=broker-reported
    fill_size_pct: float  # actual filled (paper=target; live may diverge)
    reactor_name: str
    human_in_the_loop: bool
    approver_user_id: str | None = None
    reactor_metadata: dict[str, Any] | None = None
    # ADR-0068: bar-boundary anchor for replay equality. Optional for backward
    # compatibility — old execution records persisted before ADR-0068 lack it
    # and read back as None. New records carry it explicitly so consumers can
    # distinguish "when did the model run" (asof_decision) from "what bar did
    # the model see" (bar_ts).
    bar_ts: str | None = None
    # B13: source/play_tag of the fire so the retro/settlement loop can tell
    # advisor (HITL approve) vs playbook vs autonomous-tick fills apart. Before
    # this field every fill read as "advisor" by default; the three writers now
    # stamp their own source. Backward-compatible: records persisted before B13
    # lack the key and read back as the safe default "advisor", so existing
    # readers that ignore it are bit-for-bit unaffected.
    play_tag: str = "advisor"


@runtime_checkable
class Reactor(Protocol):
    """Protocol contract for React adapters.

    All reactors are stateless w.r.t. the proposal lifecycle — they accept
    a proposal + an effective fill_size_pct (size_override_pct from the
    operator if present, else the advisor-recommended Kelly fraction) and
    return an ExecutionRecord. The proposal_store and journal layers
    handle the persistence side-effects.

    Live reactors MUST raise NotImplementedError or a clear error if
    invoked without `--live` opt-in. The fail-closed default protects
    against a config drift accidentally promoting paper-mode to live.
    """

    name: str
    requires_credentials: bool

    def execute(
        self,
        proposal: Any,  # hermes_quant.proposals.Proposal
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
        play_tag: str = "advisor",  # B13: source of the fire (advisor/playbook/autonomous)
    ) -> ExecutionRecord: ...
