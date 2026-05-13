"""hermes_quant.react.paper — Paper-only reactor for HITL mode (ADR-0015 §D5).

PaperReactor writes to ~/.hermes/quant/executions.jsonl with fill_price
equal to decision_price. v0.1.2 deliberately does NOT simulate slippage
on paper fills — slippage modeling lives upstream in MarketState
(ADR-0009 §P1-12 cold-start defaults) and would conflict with the
daemon's executions.jsonl format that real broker fills (v0.2 live
reactors) write.

The executions bus is the SAME bus the daemon's freqtrade consumer would
fill. This is deliberate: HITL paper fills feed the same calibrator that
autonomous-mode fills will feed in v0.2, so the calibrator's training
data is consistent across modes.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

from .base import ExecutionRecord, Reactor

logger = logging.getLogger(__name__)


class PaperReactor:
    """Reactor that writes paper executions to executions.jsonl.

    Paper fills use fill_price=decision_price; v0.1.2 does not simulate
    slippage on the paper side. The daemon's settlement loop computes
    realized P&L from paired entry/exit fills, so the lack of slippage
    on entry is symmetric — both legs of a paper round-trip use
    decision_price.

    Per ADR-0015 §D5 + §D10: paper-only in v0.1.2. Live reactors gated
    by separate adapters and explicit --live opt-in.
    """

    name = "paper"
    requires_credentials = False

    def __init__(self, executions_path: Path | None = None) -> None:
        self.executions_path = executions_path or EXECUTION_BUS_PATH
        self.executions_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.executions_path.exists():
            self.executions_path.touch()

    def execute(
        self,
        proposal: Any,
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
    ) -> ExecutionRecord:
        """Append an execution record to the bus and return it.

        Args:
            proposal: hermes_quant.proposals.Proposal (state must be
                pending; caller is responsible for state-machine flow).
            fill_size_pct: signed fraction of NAV (e.g. +0.05 = 5% long).
                If the operator passed size_override on approve, that's
                what should land here; otherwise the advisor's
                kelly_fraction.
            approver_user_id: Hermes user id of approver, if available.
        """
        decision_price = self._extract_decision_price(proposal)
        signal_id = self._extract_signal_id(proposal)
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        asof_decision = (proposal.advisor_result or {}).get("as_of") or now

        record = ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=signal_id,
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision=asof_decision,
            asof_execution=now,
            target_position_pct=fill_size_pct,
            decision_price=decision_price,
            fill_price=decision_price,    # paper: no slippage
            fill_size_pct=fill_size_pct,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "paper": True,
                "advisor_caveats": (proposal.advisor_result or {}).get("caveats", []),
            },
        )

        # Append to the executions bus. Same flock pattern signal_bus uses.
        # The record format aligns with what the daemon's settlement loop
        # already consumes — see daemon/settlement_loop.py for the reader side.
        line = json.dumps(_record_to_dict(record), separators=(",", ":"),
                          sort_keys=True) + "\n"
        with append_locked(self.executions_path) as fd:
            os.write(fd, line.encode("utf-8"))

        logger.info(
            "paper-react: %s asset=%s size=%+.4f decision_price=%.4f",
            record.proposal_id, record.asset, record.fill_size_pct,
            record.decision_price,
        )
        return record

    @staticmethod
    def _extract_decision_price(proposal: Any) -> float:
        """Pull the decision-time price from the embedded advisor_result.

        Advisor stores the last close as `aggregated_signal` doesn't have it,
        but data_quality.last_bar_age_minutes confirms the bar; we fall back
        to advisor's metadata if available. If everything's missing (e.g.
        gated proposal that somehow got approved), use 0.0 as a sentinel —
        the calibrator will gate on data_quality anyway.
        """
        ar = proposal.advisor_result or {}
        # Prefer explicit decision_price if the advisor populates it
        # (placeholder for v0.1.2 — currently advisor doesn't expose this
        # at top level; we extract from the analyst views' metadata if
        # ClassicalTA included it).
        for view in (ar.get("analyst_views") or []):
            md = view.get("metadata") or {}
            if "last_close" in md:
                try:
                    return float(md["last_close"])
                except (TypeError, ValueError):
                    pass
        # Fall back to a price encoded by the advisor at as_of time, if any.
        # (v0.1.3 should add `decision_price` as a top-level advisor field;
        # ADR-0014 amendment can lock this.)
        return 0.0

    @staticmethod
    def _extract_signal_id(proposal: Any) -> str | None:
        ar = proposal.advisor_result or {}
        sig = ar.get("aggregated_signal") or {}
        return sig.get("signal_id")  # advisor doesn't currently emit this; will when daemon-mode is integrated


def _record_to_dict(record: ExecutionRecord) -> dict[str, Any]:
    """Serialize an ExecutionRecord to a JSONL-safe dict."""
    return {
        "proposal_id": record.proposal_id,
        "signal_id": record.signal_id,
        "asset": record.asset,
        "asset_class": record.asset_class,
        "timeframe": record.timeframe,
        "asof_decision": record.asof_decision,
        "asof_execution": record.asof_execution,
        "target_position_pct": record.target_position_pct,
        "decision_price": record.decision_price,
        "fill_price": record.fill_price,
        "fill_size_pct": record.fill_size_pct,
        "reactor_name": record.reactor_name,
        "human_in_the_loop": record.human_in_the_loop,
        "approver_user_id": record.approver_user_id,
        "reactor_metadata": record.reactor_metadata or {},
    }
