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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

from .admissibility_precondition import admissibility_reject_equity
from .base import ExecutionRecord

logger = logging.getLogger(__name__)


def _account_nav_usd() -> float | None:
    """Best-available paper-account NAV (USD), or None on any failure (fail-closed).

    Mirrors `hermes_quant.autonomous._account_nav_usd` so the reactor's admissibility
    share-conversion uses the SAME NAV source as the autonomous-tick seam, without
    importing from autonomous.py (kept decoupled). Source priority:
      1. state.db cash.equity_total (materialized NAV after fills) — the truth.
      2. paper bootstrap initial cash (no fills yet).
    Returns None on any failure so the caller fails-closed (no fabricated NAV).
    """
    try:
        from hermes_quant.state.portfolio_state import (
            _default_initial_cash,
            get_portfolio_state,
        )

        cash = get_portfolio_state().get_cash("paper-default")
        if cash is not None and cash.equity_total > 0:
            return float(cash.equity_total)
        boot = _default_initial_cash()
        return float(boot) if boot > 0 else None
    except Exception as exc:  # noqa: BLE001 — fail-closed: unknown NAV => None.
        logger.warning("paper-react: NAV lookup failed (admissibility fail-closed): %s", exc)
        return None


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

        Wave 4 (ADR-0042) reflection hook:
            When env var HERMES_QUANT_REFLECTION=1 is set AND the fill
            brings the position quantity to zero (i.e., a close), the
            Reflector is triggered asynchronously.  Default OFF — behavior
            is bit-identical to pre-Wave-4 when the env var is absent.
        """
        decision_price = self._extract_decision_price(proposal)
        signal_id = self._extract_signal_id(proposal)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ADR-0077 / ADR-0079: pre-trade admissibility as a REACTION-layer PRECONDITION.
        # DEFAULT-OFF behind HERMES_QUANT_ADMISSIBILITY; with the flag absent this block is a
        # bit-for-bit no-op (the gate is never consulted and the fill proceeds exactly as today).
        # When ON, an inadmissible SHORT equity order is REJECTED here — the reactor (the actual
        # Reaction layer) is now admissibility-aware, not just the autonomous-tick decision seam.
        # REJECT-only / fail-closed: it can only refuse to fill, never widen, force, or flip a side.
        admissibility_reject = self._admissibility_reject(proposal, fill_size_pct, now)
        if admissibility_reject is not None:
            return admissibility_reject
        # ADR-0068: prefer the wall-clock decision time emitted by the advisor.
        # Fall back to `as_of` (= bar boundary) for advisor_result dicts produced
        # before the ADR-0068 split, then to `now` if neither is available.
        # The previous behavior (asof_decision = as_of) silently labeled every
        # fill with the bar-boundary midnight, hiding the true decision-vs-fill
        # latency.
        adv = proposal.advisor_result or {}
        asof_decision = (
            adv.get("decision_wall_clock")
            or adv.get("as_of")
            or now
        )
        bar_ts = adv.get("bar_ts") or adv.get("as_of")  # = old as_of for v1 records

        # ADR-0070: opt-in slippage model. Default OFF (legacy passthrough,
        # fill_price = decision_price) unless HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2.
        # When enabled, we model spread + impact + latency drift + auction premium
        # with a deterministic per-fill RNG seed so replays of the same fill
        # produce the same slipped fill price.
        slippage_mode = os.environ.get("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
        slippage_breakdown: dict[str, float] | None = None
        if slippage_mode == "v0.2":
            from hermes_quant.react.slippage_model import (
                apply_slippage,
                is_late_session_equity,
            )
            is_late = (
                is_late_session_equity(now)
                if proposal.asset_class == "equity"
                else False
            )
            try:
                fill_price, slippage_breakdown = apply_slippage(
                    decision_price=decision_price,
                    target_pct=fill_size_pct,
                    asof_execution=now,
                    proposal_id=proposal.proposal_id,
                    asset_class=proposal.asset_class,
                    is_late_session=is_late,
                )
            except ValueError as exc:
                # Bad input (e.g. decision_price <= 0) — degrade to passthrough
                # rather than fail the fill. Surface in metadata for audit.
                logger.warning(
                    "paper-react: slippage_model rejected fill for %s: %s; "
                    "degraded to passthrough",
                    proposal.proposal_id,
                    exc,
                )
                fill_price = decision_price
                slippage_breakdown = {"error": str(exc)}
        else:
            fill_price = decision_price  # legacy passthrough

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
            fill_price=fill_price,  # ADR-0070: slipped when v0.2 enabled, else decision_price
            fill_size_pct=fill_size_pct,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "paper": True,
                "advisor_caveats": (proposal.advisor_result or {}).get("caveats", []),
                "slippage_model": slippage_mode,
                "slippage_breakdown": slippage_breakdown,
            },
            bar_ts=bar_ts,
        )

        # Append to the executions bus. Same flock pattern signal_bus uses.
        # The record format aligns with what the daemon's settlement loop
        # already consumes — see daemon/settlement_loop.py for the reader side.
        line = json.dumps(_record_to_dict(record), separators=(",", ":"), sort_keys=True) + "\n"
        with append_locked(self.executions_path) as fd:
            os.write(fd, line.encode("utf-8"))

        logger.info(
            "paper-react: %s asset=%s size=%+.4f decision_price=%.4f "
            "fill_price=%.4f slippage_model=%s",
            record.proposal_id,
            record.asset,
            record.fill_size_pct,
            record.decision_price,
            record.fill_price,
            slippage_mode,
        )

        # Wave 1c (ADR-0041): update PortfolioState incrementally.
        # Failure must NOT block execution — silence-by-default per ADR-0031.
        try:
            from hermes_quant.state.portfolio_state import get_portfolio_state

            _record_dict = _record_to_dict(record)
            # Inject account_id so PortfolioState can partition by account.
            # PaperReactor doesn't carry an account_id field today — use the
            # execution bus default sentinel "paper-default" unless reactor_metadata
            # carries an override (forward-compat for v0.2 named-account support).
            if "account_id" not in _record_dict or not _record_dict.get("account_id"):
                _record_dict["account_id"] = (
                    (record.reactor_metadata or {}).get("account_id") or "paper-default"
                )
            get_portfolio_state().apply_execution(_record_dict)
        except Exception as _e:  # pragma: no cover — defensive
            logger.warning("PortfolioState.apply_execution failed (non-blocking): %s", _e)

        # -----------------------------------------------------------------------
        # Wave 4 (ADR-0042): trigger Reflector on position close.
        # Gated by HERMES_QUANT_REFLECTION=1 — default OFF.
        # "Position close" heuristic: fill_size_pct has the opposite sign to
        # the existing open position (detected via PortfolioState), OR the
        # resulting net exposure rounds to zero. We use a simple sign-flip
        # heuristic here; the full settlement-loop path is the authoritative
        # close detector (daemon/settlement_loop.py).
        # -----------------------------------------------------------------------
        import os as _os
        if _os.environ.get("HERMES_QUANT_REFLECTION", "0") == "1":
            try:
                from hermes_quant.memory._paper_reflection_hook import (
                    maybe_reflect_on_close,
                )
                maybe_reflect_on_close(record, proposal)
            except Exception as _re:  # pragma: no cover — non-blocking
                logger.warning("Wave4 reflection hook failed (non-blocking): %s", _re)

        return record

    def _admissibility_reject(
        self, proposal: Any, fill_size_pct: float, now: str
    ) -> ExecutionRecord | None:
        """Pre-trade admissibility precondition for SHORT equity paper fills (ADR-0077/0079).

        Delegates to the shared ``admissibility_reject_equity`` seam so the
        multi-leg reactor's equity-leg precondition is BIT-IDENTICAL to this one
        (plan §2.6). Behavior is unchanged from the inline version:

            None  => proceed with the fill (flag OFF, long order, non-equity, or ADMITTED).
                     With HERMES_QUANT_ADMISSIBILITY unset this ALWAYS returns None and never
                     touches the oracle / NAV lookup — bit-for-bit the pre-ADR-0077 behavior.
            ExecutionRecord (fill_size_pct=0.0, NOT written to the bus) => the short was found
                     inadmissible; the reactor records the rejection in the audit trail.
        """
        # asof: prefer the advisor's wall-clock decision time, else the bar boundary, else now.
        adv = proposal.advisor_result or {}
        asof_str = adv.get("decision_wall_clock") or adv.get("as_of") or now
        bar_ts = adv.get("bar_ts") or adv.get("as_of")
        return admissibility_reject_equity(
            symbol=proposal.symbol,
            asset_class=proposal.asset_class,
            fill_size_pct=fill_size_pct,
            decision_price=self._extract_decision_price(proposal),
            nav_provider=_account_nav_usd,
            asof_decision=asof_str,
            asof_execution=now,
            reactor_name=self.name,
            proposal_id=proposal.proposal_id,
            signal_id=self._extract_signal_id(proposal),
            timeframe=proposal.timeframe,
            bar_ts=bar_ts,
        )

    @staticmethod
    def _extract_decision_price(proposal: Any) -> float:
        """Pull the decision-time price from the embedded advisor_result.

        Per ADR-0014 amendment Wave B.1 (2026-05-13): advisor exposes
        `decision_price` as a top-level field. Older proposals (pre-fix)
        may have it buried in analyst_views[0].metadata.last_close — we
        fall back through that for forward-compat with already-stored
        proposals.
        """
        ar = proposal.advisor_result or {}
        # Preferred: top-level decision_price (advisor Wave B.1+)
        top_dp = ar.get("decision_price")
        if top_dp is not None:
            try:
                return float(top_dp)
            except (TypeError, ValueError):
                pass
        # Fallback for pre-Wave-B.1 advisor_results stored before the fix:
        # ClassicalTA's metadata happens to carry last_close.
        for view in ar.get("analyst_views") or []:
            md = view.get("metadata") or {}
            if "last_close" in md:
                try:
                    return float(md["last_close"])
                except (TypeError, ValueError):
                    pass
        # Worst case: gated proposals approved-anyway (operator override)
        # land here. 0.0 is the sentinel; the daemon's settlement loop
        # gates on data_quality at calibration time.
        return 0.0

    @staticmethod
    def _extract_signal_id(proposal: Any) -> str | None:
        """Pull signal_id from advisor_result. None for advisor-only proposals
        (the advisor surface doesn't emit signals; daemon-integration will)."""
        ar = proposal.advisor_result or {}
        # Top-level (Wave B.1+ advisor)
        sid = ar.get("signal_id")
        if sid:
            return sid
        # Fallback: aggregated_signal sub-dict
        sig = ar.get("aggregated_signal") or {}
        return sig.get("signal_id")


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
        "bar_ts": record.bar_ts,  # ADR-0068: explicit bar-boundary anchor
    }
