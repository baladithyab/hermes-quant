"""hermes_quant.react.multileg — multi-leg paper reactor SCAFFOLD (ADR-0029).

DEFAULT-OFF. Until HERMES_QUANT_MULTILEG_REACTOR=1 (which is NOT set anywhere in
this wave), every execute() call raises MultiLegReactorDisabled and NOTHING is
written to executions.jsonl. This is the B01 foundation; the go-live wave flips
the flag after the Wave B fidelity foundation + ADR-0029 D7's 60-day paper
evidence window.

The class mirrors PaperReactor's Reactor-Protocol surface so it drops into the
react dispatch without touching proposals/store. It does NOT touch a live
broker: any code path toward a live mleg order raises LiveMultiLegNotAuthorized
(ADR-0029 D7) — but no such path exists in this scaffold.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH

from .base import ExecutionRecord


class MultiLegReactorDisabled(RuntimeError):  # noqa: N818 — plan/ADR-0029-mandated name
    """Raised by execute() when HERMES_QUANT_MULTILEG_REACTOR != 1."""


class LiveMultiLegNotAuthorized(RuntimeError):  # noqa: N818 — ADR-0029-D7-mandated name
    """Hard refusal: live multi-leg is gated behind a future promotion ADR
    (ADR-0029 D7). Not a config flag."""


class MultiLegPaperReactor:
    """Paper-only multi-leg reactor scaffold. Interface-compatible with
    PaperReactor; inert unless HERMES_QUANT_MULTILEG_REACTOR=1."""

    name = "multileg-paper"
    requires_credentials = False

    def __init__(self, executions_path: Path | None = None) -> None:
        # Mirror PaperReactor.__init__ surface (default EXECUTION_BUS_PATH) but
        # DO NOT open/write/touch anything until the reactor is enabled. While
        # disabled, the scaffold must leave the executions bus untouched.
        self.executions_path = executions_path or EXECUTION_BUS_PATH

    @staticmethod
    def _enabled() -> bool:
        return os.environ.get("HERMES_QUANT_MULTILEG_REACTOR", "0") == "1"

    def execute(
        self,
        proposal: Any,  # MultiLegProposal once it lands; Any for now
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
    ) -> ExecutionRecord:
        """Default-OFF: raises MultiLegReactorDisabled. When enabled (NOT this
        wave), would: aggregate net greeks, build the mleg leg array
        (position_intent + ratio_qty per leg, outer qty/type/limit_price per
        ADR-0029 D2 amendment — research §1.3 shape), write ONE atomic
        ExecutionRecord per proposal to executions.jsonl (paper fill_price =
        net debit/credit). NEVER calls a live broker."""
        if not self._enabled():
            raise MultiLegReactorDisabled(
                "multi-leg reactor is default-OFF; set "
                "HERMES_QUANT_MULTILEG_REACTOR=1 to enable (gated by ADR-0029 D7)"
            )
        raise NotImplementedError(
            "multi-leg execution body is deferred to the B01 go-live wave"
        )
