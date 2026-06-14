"""hermes_quant.react.paper — PaperReactor execution bus writer.

Per ADR-0015 and ADR-0041, PaperReactor is the canonical source of
executions.jsonl entries for paper trading.  It is intentionally dumb
and side-effect free beyond appending JSONL records and updating the
materialized PortfolioState view; all higher-level orchestration lives
in callers.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime
from numbers import Real
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked
from hermes_quant.daemon.tick_lock import account_tick_lock, symbol_tick_lock

from .admissibility_precondition import admissibility_reject_equity
from .base import ExecutionRecord

logger = logging.getLogger(__name__)

HARD_FILL_CEILING = 1.0


def _resolve_account_id(proposal: Any) -> str:
    """Resolve the account partition for the tick-lock key, the cap-read, and the
    state.db write — kept as ONE helper so they cannot disagree on the account.

    v0.1 invariant (cs10, 2026-06-13): the paper reactor is SINGLE-ACCOUNT. The
    canonical ``Proposal`` dataclass (proposals.py) has NO ``reactor_metadata``
    field, and neither the ``StoredMultiLegProposal`` read-wrapper nor any current
    producer sets one — so the partition is always the ``"paper-default"``
    sentinel. The previous body read ``getattr(proposal, "reactor_metadata", ...)``
    and claimed an account-override safety property the data model does not
    provide: the getattr ALWAYS missed and the function ALWAYS returned
    "paper-default". This is byte-identical to that behavior; it just removes the
    dead override path and the false comment. When v0.2 introduces real named
    accounts, this is the single seam to teach about the account field — and the
    bus-append path at _execute_fired() injects the same "paper-default" sentinel,
    so the two agree by construction.
    """
    return "paper-default"


class FillSizeInvariantError(ValueError):
    """fill_size_pct violated the last-seam invariant. Fail closed."""


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
        "play_tag": record.play_tag,  # B13: source of the fire
        # ADR-0091 Option E: tag how the fold interprets the per-fill size field.
        # None (legacy) reads as absolute-target; serialized verbatim so a new
        # record can stamp SCHEMA_ABSOLUTE_TARGET while old records stay None.
        "schema_version": record.schema_version,
    }


def _enforce_fill_size_invariant(_proposal: Any, fill_size_pct: float) -> float:
    """Validate the signed NAV fraction before any money-moving side effect.

    This is the final sanity invariant at the paper execution seam. It rejects
    non-finite values and obviously insane magnitudes, but it deliberately does
    not enforce the normal position cap. The portfolio-cap seam owns clipping
    ordinary oversized fills.
    """
    try:
        fill_size = float(fill_size_pct)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FillSizeInvariantError(
            f"fill_size_pct={fill_size_pct!r} is non-finite; refusing to execute"
        ) from exc
    if not isinstance(fill_size_pct, Real) or not math.isfinite(fill_size):
        raise FillSizeInvariantError(
            f"fill_size_pct={fill_size_pct!r} is non-finite; refusing to execute"
        )

    if abs(fill_size) > HARD_FILL_CEILING:
        raise FillSizeInvariantError(
            f"|fill_size_pct|={abs(fill_size):.4f} exceeds hard ceiling "
            f"{HARD_FILL_CEILING:.4f}; refusing to execute"
        )
    return fill_size_pct


def _account_nav_usd() -> float | None:
    """Best-available paper-account NAV (USD), or None on any failure (fail-closed).

    Mirrors `hermes_quant.autonomous._account_nav_usd` so the reactor's admissibility
    share-conversion uses the SAME NAV source as the autonomous-tick seam, without
    importing from autonomous.py (kept decoupled). Source priority:
      1. state.db cash.equity_total (materialized NAV after fills) — the truth.
      2. paper bootstrap initial cash (no fills yet).
    Returns None on any failure so the caller fails-closed (no fabricated NAV).

    NOTE (ADR-0086): this uses the cost-basis `equity_total` deliberately — the
    admissibility share-conversion needs a NAV figure, and it must match the
    autonomous seam's source for the two seams to agree. Read-time MTM
    (get_marked_equity) is a SEPARATE reporting concern and must NOT be wired here.
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
        play_tag: str = "advisor",
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
            play_tag: B13 source/play_tag of the fire — "advisor" (HITL
                approve, the default), "playbook", or "autonomous". Carried
            onto the ExecutionRecord so the retro/settlement loop can
                attribute fills by source. Default "advisor" keeps existing
                callers bit-for-bit (every fill read as advisor before B13).

        Wave 4 (ADR-0042) reflection hook:
            When env var HERMES_QUANT_REFLECTION=1 is set AND the fill
            brings the position quantity to zero (i.e., a close), the
            Reflector is triggered asynchronously.  Default OFF — behavior
            is bit-identical to pre-Wave-4 when the env var is absent.
        """
        fill_size_pct = _enforce_fill_size_invariant(proposal, fill_size_pct)
        decision_price = self._extract_decision_price(proposal)
        signal_id = self._extract_signal_id(proposal)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # cr05 (2026-06-14): REJECT a non-finite / zero / negative decision_price.
        # _extract_decision_price() returns the 0.0 SENTINEL when no usable price is
        # present (gated proposal approved-anyway / missing advisor field). A fill at
        # price 0.0 is NOT a recoverable degradation — it silently corrupts the P&L
        # ledger (zero-division in horizon-return math, a $0 cost basis). Fail closed:
        # return a SILENCE record (fill_size_pct=0.0, NOT appended, no state.db write),
        # the silence-by-default posture. This pre-empts BOTH the v0.1 passthrough and
        # the v0.2 apply_slippage call so neither can ever build a fill_price=0.0 record.
        # We return a record (not raise) because the live fire loop (autonomous.py:948)
        # calls execute() with no try/except — a new raise would crash the tick.
        if not math.isfinite(decision_price) or decision_price <= 0.0:
            logger.warning(
                "paper-react: %s asset=%s REJECTED — decision_price=%r is non-finite or "
                "<= 0 (no usable price); refusing to book a corrupt zero-price fill",
                proposal.proposal_id,
                proposal.symbol,
                decision_price,
            )
            return self._silence_record(
                proposal,
                fill_size_pct=fill_size_pct,
                decision_price=decision_price,
                signal_id=signal_id,
                now=now,
                approver_user_id=approver_user_id,
                play_tag=play_tag,
                silence_reason="zero_decision_price",
            )

        # ADR-0077 / ADR-0079: pre-trade admissibility as a REACTION-layer PRECONDITION.
        # DEFAULT-OFF behind HERMES_QUANT_ADMISSIBILITY; with the flag absent this block is a
        # bit-for-bit no-op (the gate is never consulted and the fill proceeds exactly as today).
        # When ON, an inadmissible SHORT equity order is REJECTED here — the reactor (the actual
        # Reaction layer) is now admissibility-aware, not just the autonomous-tick decision seam.
        # REJECT-only / fail-closed: it can only refuse to fill, never widen, force, or flip a side.
        admissibility_reject = self._admissibility_reject(
            proposal, fill_size_pct, now, play_tag=play_tag
        )
        if admissibility_reject is not None:
            return admissibility_reject

        # ADR-0078 / ra10: single-writer per-symbol TICK LOCK across the
        # read-decide-fire-store window. The cap-read below RECONSTRUCTS the book,
        # then we append to executions.jsonl and update state.db — a read-modify-
        # write that two armed crons (autonomous + playbook) can interleave on the
        # SAME symbol (the 880%-gross mechanism). The per-write flock on the bus
        # and BEGIN IMMEDIATE on state.db each serialize ONE write but NOT the
        # read-decide that precedes them, so they cannot close this. We hold an
        # exclusive per-(account, asset_class, symbol) advisory lock from BEFORE
        # the cap-read through the state.db update; different symbols use different
        # lock files and never block each other.
        #
        # FAIL-OPEN-SAFE (a deadlocking lock is worse than the race):
        #   * acquired           -> the whole sequence runs serialized under the lock.
        #   * contended (timeout) -> another writer holds the symbol THIS tick; we
        #                            SKIP it (silenced audit record, NOT appended,
        #                            no double-fire, no block).
        #   * fail-open          -> the lock file is unopenable / flock unsupported;
        #                            we proceed exactly as today (race re-opens) with
        #                            a WARNING. Never hang, never crash.
        # DEFAULT-ON with a kill-switch: set HERMES_QUANT_TICK_LOCK=0 to bypass the
        # lock entirely (byte-identical to the pre-ADR-0078 path).
        account_id = _resolve_account_id(proposal)
        if os.environ.get("HERMES_QUANT_TICK_LOCK", "1") != "1":
            return self._execute_fired(
                proposal,
                fill_size_pct=fill_size_pct,
                approver_user_id=approver_user_id,
                play_tag=play_tag,
                decision_price=decision_price,
                signal_id=signal_id,
                now=now,
            )

        def _fire() -> ExecutionRecord:
            """The per-SYMBOL tick-lock body. Extracted VERBATIM from the inline
            ADR-0078 block so behavior under HERMES_QUANT_TICK_LOCK is unchanged.
            cr04 wraps THIS closure in the optional per-account lock below."""
            with symbol_tick_lock(account_id, proposal.asset_class, proposal.symbol) as lock:
                if not lock.acquired and lock.contended:
                    # Another writer holds this symbol this tick. SKIP (silence-by-
                    # default) — do NOT append, do NOT block, do NOT double-fire.
                    logger.warning(
                        "paper-react: %s asset=%s SKIPPED — symbol tick-lock contended "
                        "(%s); another writer is firing this symbol this tick",
                        proposal.proposal_id,
                        proposal.symbol,
                        lock.reason,
                    )
                    return ExecutionRecord(
                        proposal_id=proposal.proposal_id,
                        signal_id=signal_id,
                        asset=proposal.symbol,
                        asset_class=proposal.asset_class,
                        timeframe=proposal.timeframe,
                        asof_decision=now,
                        asof_execution=now,
                        target_position_pct=fill_size_pct,
                        decision_price=decision_price,
                        fill_price=decision_price,
                        fill_size_pct=0.0,
                        reactor_name=self.name,
                        human_in_the_loop=True,
                        approver_user_id=approver_user_id,
                        reactor_metadata={
                            "paper": True,
                            "silenced": True,
                            "silence_reason": "tick_lock_contended",
                            "tick_lock": lock.reason,
                        },
                        bar_ts=(proposal.advisor_result or {}).get("bar_ts"),
                        play_tag=play_tag,
                    )
                # acquired OR fail-open: run the fire sequence. On fail-open the lock
                # is not held (degraded to today's behavior) but the tick proceeds.
                return self._execute_fired(
                    proposal,
                    fill_size_pct=fill_size_pct,
                    approver_user_id=approver_user_id,
                    play_tag=play_tag,
                    decision_price=decision_price,
                    signal_id=signal_id,
                    now=now,
                )

        # cr04 (2026-06-14): DEFAULT-OFF per-ACCOUNT lock around the cross-symbol cap
        # TOCTOU race. The per-symbol lock above serializes the SAME symbol, but the
        # portfolio-cap seam reads the WHOLE-account book, so two DIFFERENT symbols
        # racing both see the same pre-fire headroom and both pass the cap (ADR-0091
        # named this non-atomicity). When HERMES_QUANT_ACCOUNT_LOCK=1, hold a per-
        # account lock OUTSIDE the per-symbol lock (account-outer/symbol-inner =>
        # fixed acquire order, no deadlock) spanning the cap-read through the state.db
        # write so the loser sees the winner's consumed headroom. DEFAULT-OFF: with the
        # flag absent this branch is never taken and execute() is byte-identical to the
        # pre-cr04 path (the per-symbol _fire() runs directly). Contended -> SKIP via a
        # silence record; acquired/fail-open -> _fire().
        if os.environ.get("HERMES_QUANT_ACCOUNT_LOCK", "0") != "1":
            return _fire()

        with account_tick_lock(account_id) as acct_lock:
            if not acct_lock.acquired and acct_lock.contended:
                logger.warning(
                    "paper-react: %s asset=%s SKIPPED — account tick-lock contended "
                    "(%s); another writer is firing on this account this tick",
                    proposal.proposal_id,
                    proposal.symbol,
                    acct_lock.reason,
                )
                return self._silence_record(
                    proposal,
                    fill_size_pct=fill_size_pct,
                    decision_price=decision_price,
                    signal_id=signal_id,
                    now=now,
                    approver_user_id=approver_user_id,
                    play_tag=play_tag,
                    silence_reason="account_lock_contended",
                    extra_metadata={"account_lock": acct_lock.reason},
                )
            # acquired OR fail-open: run the per-symbol fire sequence under the
            # account lock. On fail-open the account lock is not held (degraded to
            # today's cross-symbol race) but the tick proceeds.
            return _fire()

    def _execute_fired(
        self,
        proposal: Any,
        *,
        fill_size_pct: float,
        approver_user_id: str | None,
        play_tag: str,
        decision_price: float,
        signal_id: str | None,
        now: str,
    ) -> ExecutionRecord:
        """The read-decide-fire-store body of execute(), run under the tick lock.

        Extracted verbatim from execute() (ADR-0078): the per-symbol tick lock
        wraps THIS method so the cap-read (which reconstructs the book), the bus
        append, and the state.db update are one serialized critical section per
        symbol. Behavior is otherwise unchanged from the pre-lock inline body.
        """
        # ADR-0087: portfolio-cap seam at the REACTION layer (DEFAULT-OFF).
        # When HERMES_QUANT_PORTFOLIO_CAPS=1, this precondition reads current
        # portfolio headroom and either SILENCES an over-cap fire (clipped to
        # ~0) or SCALES it down to the remaining headroom. With the flag unset
        # this helper is a bit-identical no-op: it returns (None, fill_size_pct,
        # None) without touching state or the cap module.
        #
        # Three outcomes:
        #   * full silence  -> returns a silenced ExecutionRecord (early return).
        #   * partial scale -> rewrites fill_size_pct to the clipped value and
        #                      returns cap_metadata (cap_scaled_from/to/factor)
        #                      that is merged into the fired record below.
        #   * full pass      -> fill_size_pct unchanged, cap_metadata is None.
        cap_silence, fill_size_pct, cap_metadata = self._portfolio_cap_clip(
            proposal, fill_size_pct, now, play_tag=play_tag
        )
        if cap_silence is not None:
            return cap_silence

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

        # ADR-0070: slippage model. Default ON (v0.2 envelope) per FLAGS.md Tier-A
        # promotion — set HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1 to opt OUT and get
        # the legacy passthrough (fill_price = decision_price). When enabled, we model
        # spread + impact + latency drift + auction premium with a deterministic
        # per-fill RNG seed so replays of the same fill produce the same slipped price.
        slippage_mode = os.environ.get("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
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
                # cr05 (2026-06-14): FAIL CLOSED. The previous body DEGRADED to a
                # passthrough fill (fill_price = decision_price) and APPENDED it —
                # but apply_slippage only raises ValueError when decision_price is
                # non-finite or <= 0 (slippage_model.py: "must be finite and > 0"),
                # so the degrade booked a corrupt zero/garbage-price fill. The A1
                # guard in execute() already rejects a non-finite/<= 0 decision_price
                # upstream, so reaching here means a finite price > 0 that the model
                # STILL rejected — we must NOT book it. Return a SILENCE record
                # (NOT appended, no state.db write) instead of degrading. We return a
                # record rather than re-raise so the no-try/except live fire loop
                # (autonomous.py:948) is not crashed by a new raising path.
                logger.warning(
                    "paper-react: %s asset=%s REJECTED — slippage_model refused the "
                    "fill (%s); failing closed (no degraded zero/garbage-price booking)",
                    proposal.proposal_id,
                    proposal.symbol,
                    exc,
                )
                return self._silence_record(
                    proposal,
                    fill_size_pct=fill_size_pct,
                    decision_price=decision_price,
                    signal_id=signal_id,
                    now=now,
                    approver_user_id=approver_user_id,
                    play_tag=play_tag,
                    silence_reason="slippage_rejected",
                    extra_metadata={"slippage_breakdown": {"error": str(exc)}},
                )
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
                # ADR-0087: when the portfolio-cap seam scaled this fire down to
                # remaining headroom, surface the audit trail (cap_scaled_from/to/
                # factor). None on the full-pass and flag-OFF paths.
                **(cap_metadata or {}),
            },
            bar_ts=bar_ts,
            play_tag=play_tag,  # B13: source of the fire (advisor/playbook/autonomous)
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
        # Gated by HERMES_QUANT_REFLECTION — default ON (code default "1"; set =0 to opt out).
        # "Position close" heuristic: fill_size_pct has the opposite sign to
        # the existing open position (detected via PortfolioState), OR the
        # resulting net exposure rounds to zero. We use a simple sign-flip
        # heuristic here; the full settlement-loop path is the authoritative
        # close detector (daemon/settlement_loop.py).
        # -----------------------------------------------------------------------
        import os as _os
        if _os.environ.get("HERMES_QUANT_REFLECTION", "1") == "1":
            try:
                from hermes_quant.memory._paper_reflection_hook import (
                    maybe_record_decision_on_open,
                    maybe_reflect_on_close,
                )
                # W1 (capability-map O1): record the pending decision on an OPENING
                # fill so the reflection loop has source-water. Symmetric with the
                # close hook; the open recorder defers to the close hook for fills
                # that close an existing position. This ignites the one closed-in-code
                # feedback edge (reflection→retriever→PM prompt) that was dark because
                # record_decision() had zero production callers.
                maybe_record_decision_on_open(record, proposal)
                maybe_reflect_on_close(record, proposal)
            except Exception as _re:  # pragma: no cover — non-blocking
                logger.warning("Wave4 reflection hook failed (non-blocking): %s", _re)

        return record

    def _silence_record(
        self,
        proposal: Any,
        *,
        fill_size_pct: float,
        decision_price: float,
        signal_id: str | None,
        now: str,
        approver_user_id: str | None,
        play_tag: str,
        silence_reason: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        """Build an audit-only SILENCE ExecutionRecord (fill_size_pct=0.0, NOT appended).

        Mirrors the tick-lock-contended (execute) and portfolio-cap (_portfolio_cap_clip)
        silence shapes so every reactor refusal looks the same on the audit trail:
        a returned ExecutionRecord that is NEVER written to executions.jsonl and NEVER
        updates state.db. The caller returns it directly without appending.

        cr05 (2026-06-14): the zero/negative/non-finite decision_price guard and the
        slippage-rejection fail-closed branch both produce this record, so the refusal
        is byte-identical regardless of which seam caught the bad price.
        """
        metadata: dict[str, Any] = {
            "paper": True,
            "silenced": True,
            "silence_reason": silence_reason,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=signal_id,
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision=now,
            asof_execution=now,
            target_position_pct=fill_size_pct,
            decision_price=decision_price,
            fill_price=decision_price,
            fill_size_pct=0.0,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata=metadata,
            bar_ts=(proposal.advisor_result or {}).get("bar_ts"),
            play_tag=play_tag,
        )

    def _admissibility_reject(
        self, proposal: Any, fill_size_pct: float, now: str, *, play_tag: str = "advisor"
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
            play_tag=play_tag,
        )

    def _portfolio_cap_clip(
        self,
        proposal: Any,
        fill_size_pct: float,
        now: str,
        *,
        play_tag: str = "advisor",
    ) -> tuple[ExecutionRecord | None, float, dict[str, Any] | None]:
        """Portfolio-cap precondition for paper fills (ADR-0087).

        DEFAULT-OFF behind HERMES_QUANT_PORTFOLIO_CAPS. With the flag unset
        this helper is a bit-identical no-op and does not import or touch
        portfolio-normalize or PortfolioState.

        Returns a ``(silence_record, effective_fill_size_pct, cap_metadata)``
        triad so the seam can express all three ADR-0087 outcomes:

          * **Full silence** - the clip leaves no usable headroom (not fired,
            or clipped to ~0). Returns ``(ExecutionRecord, 0.0, None)`` where
            the record is an audit-only silenced record that is NOT appended to
            executions.jsonl and does NOT update PortfolioState. The caller
            early-returns it.
          * **Partial scale** - the clip fits the fire only partially
            (``fired=True`` with ``scale_factor < 1.0``). Returns
            ``(None, clipped.portfolio_target_pct, cap_metadata)`` where
            ``cap_metadata`` carries ``cap_scaled_from`` (original
            fill_size_pct), ``cap_scaled_to`` (clipped value), and
            ``cap_scale_factor``. The caller executes a real fill at the
            CLIPPED size and merges ``cap_metadata`` into the record.
          * **Full pass** - the fire fits entirely (``scale_factor == 1.0``).
            Returns ``(None, fill_size_pct, None)``: the fill proceeds at the
            original size, no cap metadata, behavior unchanged.

        With HERMES_QUANT_PORTFOLIO_CAPS unset the return is always
        ``(None, fill_size_pct, None)`` - bit-for-bit the pre-ADR behavior.
        """

        if os.environ.get("HERMES_QUANT_PORTFOLIO_CAPS") != "1":
            return None, fill_size_pct, None

        # Flag ON path only: import the cap machinery lazily so the OFF path
        # stays import- and IO-free.
        from hermes_quant.risk.portfolio_normalize import (
            PortfolioCaps,
            clip_one_to_remaining_headroom,
        )
        from hermes_quant.risk.portfolio_normalize import (
            PortfolioState as RiskPortfolioState,
        )
        from hermes_quant.state.portfolio_state import get_portfolio_state

        ps = get_portfolio_state()
        # Resolve the account through the SAME helper the tick-lock key and the
        # bus-append path use, so the cap-read reconstructs the SAME book that gets
        # written (cs10, 2026-06-13). v0.1 is single-account: this resolves to the
        # "paper-default" sentinel. The previous inline getattr block read a
        # reactor_metadata.account_id override the Proposal dataclass does not
        # carry — it always missed and always returned "paper-default" — so this is
        # byte-identical; it just routes through the one helper instead of dead
        # duplicated logic.
        account_id = _resolve_account_id(proposal)
        positions = ps.get_positions(account_id)
        # cs60: key pos_map on the CANONICAL (asset_class, symbol) position key,
        # not the bare symbol. The same underlying can hold two distinct
        # positions in different asset classes (e.g. an equity AAPL AND a
        # us_option AAPL on the same name); a bare-symbol key would collapse them
        # into one bucket and mis-sum the gross the cap clips against (it under-
        # or over-counts a same-symbol cross-asset-class book). RiskPortfolioState
        # only iterates positions.values() for gross/net, so the tuple key is a
        # pure uniqueness device — for a single-asset-class book (the common
        # case, no same-symbol collision) the gross/net sums are byte-identical
        # whether keyed by bare symbol or (asset_class, symbol).
        pos_map: dict[tuple[str, str], float] = {}
        for key, position in positions.items():
            # Positions are stored as NAV-fraction quantities in v0.1 (ADR-0041).
            # The cap seam reads them as target_position_pct.
            pos_map[key] = position.quantity

        state = RiskPortfolioState(positions=pos_map)
        caps = PortfolioCaps.standard()

        # De-risking guard (P1 trade-correctness fix).
        #
        # Positions are stored as the latest signed target_position_pct per
        # symbol (ADR-0041 / portfolio_normalize.PortfolioState semantics), and
        # fill_size_pct here is the new signed target for proposal.symbol. A
        # symbol's contribution to gross exposure is abs(target). If this fill
        # lowers or preserves abs(existing), it frees or preserves headroom and
        # must not be clipped by remaining-headroom logic.
        # cs60: look up THIS proposal's own existing position by its canonical
        # (asset_class, symbol) key, so an equity de-risk compares against the
        # equity line and not a same-symbol option line (and vice versa).
        existing = pos_map.get((proposal.asset_class, proposal.symbol), 0.0)
        if abs(fill_size_pct) <= abs(existing) + 1e-9:
            return None, fill_size_pct, None

        clipped = clip_one_to_remaining_headroom(
            asset=proposal.symbol,
            per_symbol_target_pct=fill_size_pct,
            state=state,
            caps=caps,
        )

        if not clipped.fired or abs(clipped.portfolio_target_pct) < 1e-9:
            # Full silence: record an audit-only ExecutionRecord that is NOT
            # appended to executions.jsonl and does NOT update PortfolioState.
            silence_reason = clipped.silence_reason or "no_headroom"
            silence_record = ExecutionRecord(
                proposal_id=proposal.proposal_id,
                signal_id=self._extract_signal_id(proposal),
                asset=proposal.symbol,
                asset_class=proposal.asset_class,
                timeframe=proposal.timeframe,
                asof_decision=now,
                asof_execution=now,
                target_position_pct=fill_size_pct,
                decision_price=self._extract_decision_price(proposal),
                fill_price=self._extract_decision_price(proposal),
                fill_size_pct=0.0,
                reactor_name=self.name,
                human_in_the_loop=True,
                approver_user_id=None,
                reactor_metadata={
                    "paper": True,
                    "silenced": True,
                    "silence_reason": f"portfolio_cap_{silence_reason}",
                },
                bar_ts=(proposal.advisor_result or {}).get("bar_ts"),
                play_tag=play_tag,
            )
            return silence_record, 0.0, None

        # The clip fired with usable headroom. Two sub-cases:
        #   scale_factor ~= 1.0  -> full pass: nothing changes, no cap metadata.
        #   scale_factor  < 1.0  -> partial scale: rewrite fill to the clipped
        #                           NAV-fraction and surface the audit trail.
        if clipped.scale_factor >= 1.0 - 1e-9:
            return None, fill_size_pct, None

        cap_metadata: dict[str, Any] = {
            "cap_scaled_from": fill_size_pct,
            "cap_scaled_to": clipped.portfolio_target_pct,
            "cap_scale_factor": clipped.scale_factor,
        }
        return None, clipped.portfolio_target_pct, cap_metadata

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
        """Best-effort extractor for the upstream advisor's signal id.

        Older proposals may not carry this field; the settlement loop
        tolerates None and falls back to proposal_id-only joins.
        """
        ar = proposal.advisor_result or {}
        sid = ar.get("signal_id")
        if sid is not None:
            return str(sid)
        return None
