"""hermes_quant.react.deterministic_equity — DeterministicEquityReactor (ADR-0088 follow-up).

Routes EQUITY paper fills through the BP-enforcing ``DeterministicBackend`` instead
of the legacy synthetic append-only book (``PaperReactor``). The deterministic
backend is the source of buying-power truth on this path: it ENFORCES BP before
admitting a BUY and tracks TRUE signed shares, exactly the way Alpaca paper does for
the ``AlpacaPaperReactor``. This closes the 880%-gross root cause on the EQUITY path
(not just multileg): an over-BP fire is REJECTED at the reactor and surfaced as a
no-fill, instead of silently appended to an unbounded book.

Design rails (money-software):
  * ADDITIVE + DEFAULT-OFF. Selected ONLY when ``HERMES_QUANT_DETERMINISTIC_EQUITY=1``
    AND ``resolve_backend_choice() == 'deterministic'`` (see ``react.dispatch``).
    With the flag unset, the equity path is bit-for-bit the legacy ``PaperReactor``.
    Note: ``resolve_backend_choice()`` returns ``'deterministic'`` by DEFAULT, so
    gating on it alone would silently change everyone's equity path — that is why
    the explicit opt-in flag is REQUIRED.
  * Precondition chain MIRRORS ``PaperReactor`` ORDER (so flag-ON keeps every existing
    guard a flag-OFF run had):
      1. ``_enforce_fill_size_invariant`` (reused from paper.py).
      2. short-equity admissibility precondition (reused shared seam, default-OFF
         behind HERMES_QUANT_ADMISSIBILITY).
      3. portfolio-cap clip (reused from PaperReactor; HONORED when
         HERMES_QUANT_PORTFOLIO_CAPS=1). The BP-enforcing backend makes the cap
         redundant, but we still apply it so flag-ON does not surprise an operator who
         already set the cap flag. BP-enforcement + cap can BOTH apply: the cap clips
         the NAV fraction first, then the backend enforces BP on the (clipped) order.
      4. slippage (ADR-0070 v0.2 when HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2). The
         SLIPPED price is what we hand the backend as ``decision_price`` so the
         recorded fill_price + the BP notional both reflect slippage.
  * FAIL-CLOSED. Unknown NAV (account_equity None / <= 0) -> no-fill record (NEVER a
    fabricated fill). An over-BP BUY (InsufficientBuyingPowerError) or unusable
    backend (BackendUnavailableError) -> no-fill record carrying the rejection reason
    (``bp_rejected`` / ``backend_unavailable``). The reactor NEVER crashes and NEVER
    fabricates.
  * TRUE SHARES. On a successful fill the ExecutionRecord carries BOTH
    ``reactor_metadata['quantity']`` = signed TRUE shares AND ``fill_size_pct`` =
    the (possibly cap-clipped) NAV fraction. The dual-book reconcile in
    ``portfolio_state.apply_execution`` keys on ``quantity`` to account cash by REAL
    notional (signed_shares × price), the same true-unit path the Alpaca reactor uses.
  * account_id: ``paper-default``. The reactor shares the SAME book the autonomous
    tick + the legacy PaperReactor read/write, matching legacy semantics so the
    enforced fills and rejections appear in the book the rest of the system already
    consumes (NOT a separate shadow partition — that is the Alpaca reactor's job).
  * DETERMINISTIC. No RNG (slippage's per-fill seed is deterministic); asof uses
    ``datetime.now(UTC)``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

from .backend import (
    BackendUnavailableError,
    InsufficientBuyingPowerError,
    select_backend,
)
from .base import ExecutionRecord
from .multileg import _dict_to_record
from .paper import (
    PaperReactor,
    _account_nav_usd,
    _enforce_fill_size_invariant,
    _record_to_dict,
)

logger = logging.getLogger(__name__)

# The deterministic-equity book shares the SAME partition the legacy PaperReactor +
# autonomous tick read/write, so enforced fills + rejections land in the book the
# rest of the system already consumes (matching legacy semantics; NOT a shadow book).
DETERMINISTIC_EQUITY_ACCOUNT_ID = "paper-default"


class DeterministicEquityReactor:
    """Reactor that routes equity paper fills through the BP-enforcing DeterministicBackend.

    The backend enforces buying power on a BUY and tracks TRUE signed shares; the
    ExecutionRecord reflects what ACTUALLY filled (true shares + slipped price), and an
    over-BP fire is surfaced as a no-fill rather than an unbounded append. Reconciliation
    writes to the ``paper-default`` PortfolioState partition (shared with the legacy book).
    """

    name = "deterministic-equity"
    # No creds: the deterministic backend is a LOCAL simulator (no network).
    requires_credentials = False

    def __init__(self, executions_path: Path | None = None) -> None:
        self.executions_path = executions_path or EXECUTION_BUS_PATH
        self.executions_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.executions_path.exists():
            self.executions_path.touch()
        # Reuse PaperReactor's precondition machinery (admissibility + portfolio-cap
        # clip + extractors) verbatim so the chain is BIT-IDENTICAL to the legacy
        # reactor's, WITHOUT re-implementing it. The helper instance writes nothing
        # itself (we only call its pure precondition methods).
        self._pre = PaperReactor(executions_path=self.executions_path)

    # ------------------------------------------------------------------
    # Reactor.execute
    # ------------------------------------------------------------------
    def execute(
        self,
        proposal: Any,
        *,
        fill_size_pct: float,
        approver_user_id: str | None = None,
        play_tag: str = "advisor",
    ) -> ExecutionRecord:
        """Route a signed NAV-fraction equity fill through the deterministic backend.

        Mirrors ``PaperReactor.execute`` precondition ORDER (invariant ->
        admissibility -> portfolio-cap clip -> slippage), then converts the NAV
        fraction to signed TRUE shares EXACTLY like ``AlpacaPaperReactor`` and submits
        to the BP-enforcing backend. An over-BP / unavailable / unknown-NAV outcome is
        a no-fill record (never a crash, never a fabricated fill).
        """
        # ── F-1: bus-scan idempotency (no-op re-fire) ───────────────────────────
        # The deterministic backend has no server-side dedup (unlike Alpaca, which
        # rejects a duplicate client_order_id), and the state-layer idempotency key
        # is only stable within a 1-second asof window. So a retry of the SAME
        # already-filled proposal a second later would double-book cash + position.
        # Mirror MultiLegPaperReactor._existing_parent: scan the bus for a prior
        # ACTUAL fill (fill_price > 0) from THIS reactor for this proposal_id and
        # return it unchanged. A prior NO-FILL audit record (BP reject etc.) does
        # NOT block a legitimate later retry once capital frees up.
        existing = self._existing_fill(proposal.proposal_id)
        if existing is not None:
            logger.info(
                "det-equity-react: idempotency hit on proposal_id=%s; returning "
                "existing fill (no-op re-fire, no double-book)",
                proposal.proposal_id,
            )
            return existing

        fill_size_pct = _enforce_fill_size_invariant(proposal, fill_size_pct)
        decision_price = self._pre._extract_decision_price(proposal)
        signal_id = self._pre._extract_signal_id(proposal)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── precondition 2: short-equity admissibility (ADR-0077/0079) ──────────
        # DEFAULT-OFF behind HERMES_QUANT_ADMISSIBILITY; bit-for-bit no-op when the
        # flag is absent. REJECT-only / fail-closed.
        admissibility_reject = self._pre._admissibility_reject(
            proposal, fill_size_pct, now, play_tag=play_tag
        )
        if admissibility_reject is not None:
            return admissibility_reject

        # ── precondition 3: portfolio-cap clip (ADR-0087) ───────────────────────
        # HONORED when HERMES_QUANT_PORTFOLIO_CAPS=1 (the backend's BP enforcement
        # makes it redundant, but we keep it so flag-ON does not silently drop a
        # guard an operator already enabled). BP-enforcement + cap can BOTH apply:
        # the cap clips the NAV fraction here, then the backend enforces BP on the
        # resulting (clipped) order. With the flag unset this is a bit-identical
        # no-op: (None, fill_size_pct, None).
        cap_silence, fill_size_pct, cap_metadata = self._pre._portfolio_cap_clip(
            proposal, fill_size_pct, now, play_tag=play_tag
        )
        if cap_silence is not None:
            return cap_silence

        adv = proposal.advisor_result or {}
        asof_decision = adv.get("decision_wall_clock") or adv.get("as_of") or now
        bar_ts = adv.get("bar_ts") or adv.get("as_of")

        # ── precondition 4: slippage (ADR-0070) ─────────────────────────────────
        # The SLIPPED price is handed to the backend as decision_price so the
        # recorded fill_price AND the BP notional both reflect slippage. Default-OFF
        # passthrough (fill_price = decision_price) unless v0.2 is enabled.
        fill_price, slippage_mode, slippage_breakdown = self._apply_slippage(
            proposal, decision_price, fill_size_pct, now
        )

        # ── NAV-fraction -> signed TRUE shares (MIRRORS AlpacaPaperReactor) ─────
        # equity = backend.account_equity(); notional = abs(fill_size_pct) * equity;
        # shares = notional / decision_price; signed_qty = +shares if long else -shares.
        backend = select_backend()
        account_equity = backend.account_equity()
        if account_equity is None or account_equity <= 0:
            # Fail-closed: unknown / non-positive NAV. Do NOT size off a fabricated
            # NAV — return a no-fill record (never a fabricated fill).
            logger.warning(
                "det-equity-react: account equity unknown/non-positive (%r) for %s; "
                "fail-closed no-fill",
                account_equity,
                proposal.symbol,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason="account_equity_unknown",
                reason_key="equity_unknown",
                cap_metadata=cap_metadata,
            )

        price_for_qty = fill_price if fill_price and fill_price > 0 else decision_price
        if price_for_qty is None or price_for_qty <= 0:
            # No usable price to convert NAV-fraction -> shares. Fail-closed no-fill.
            logger.warning(
                "det-equity-react: non-positive price (%r) for %s; fail-closed no-fill",
                price_for_qty,
                proposal.symbol,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason="non_positive_price",
                reason_key="price_unknown",
                cap_metadata=cap_metadata,
            )

        notional_usd = abs(fill_size_pct) * account_equity
        shares = notional_usd / price_for_qty
        signed_qty = shares if fill_size_pct > 0 else -shares

        # ── submit to the BP-enforcing backend ──────────────────────────────────
        # The backend fills AT the price we pass; we pass the SLIPPED price so the
        # recorded fill_price includes slippage. A BUY over BP raises
        # InsufficientBuyingPowerError; an unusable backend raises
        # BackendUnavailableError. BOTH become a surfaced no-fill (the headline fix).
        try:
            fill = backend.submit_equity(
                symbol=proposal.symbol,
                signed_qty=signed_qty,
                decision_price=price_for_qty,
                client_order_id=proposal.proposal_id,
            )
        except InsufficientBuyingPowerError as exc:
            logger.warning(
                "det-equity-react: BP REJECT %s asset=%s target=%+.4f qty=%+.4f — NO FILL: %s",
                proposal.proposal_id,
                proposal.symbol,
                fill_size_pct,
                signed_qty,
                exc,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason=str(exc),
                reason_key="bp_rejected",
                cap_metadata=cap_metadata,
                extra_metadata={
                    "account_equity": account_equity,
                    "requested_qty": signed_qty,
                    "requested_notional": notional_usd,
                },
            )
        except BackendUnavailableError as exc:
            logger.warning(
                "det-equity-react: backend UNAVAILABLE %s asset=%s — NO FILL: %s",
                proposal.proposal_id,
                proposal.symbol,
                exc,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason=str(exc),
                reason_key="backend_unavailable",
                cap_metadata=cap_metadata,
                extra_metadata={"account_equity": account_equity},
            )
        except Exception as exc:  # noqa: BLE001 — F-2: honor the never-crashes contract
            # Any OTHER backend failure (an unexpected RuntimeError/ValueError/etc.
            # from a future or alternate BrokerBackend impl) must become a clean
            # no-fill, NOT propagate out of the money seam. The module contract is
            # "the reactor NEVER crashes"; the labeled handlers above cover the
            # expected rejections, this catch-all covers everything else.
            logger.warning(
                "det-equity-react: backend submit_equity raised %s for %s asset=%s "
                "— NO FILL (unexpected): %s",
                type(exc).__name__,
                proposal.proposal_id,
                proposal.symbol,
                exc,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason=f"{type(exc).__name__}: {exc}",
                reason_key="backend_error",
                cap_metadata=cap_metadata,
                extra_metadata={"account_equity": account_equity},
            )

        if not fill.is_fill:
            # A backend that returned a terminal non-fill (defensive — the
            # deterministic backend always fills on a BP pass, but a future backend
            # might not). Surface a no-fill, never fabricate.
            logger.warning(
                "det-equity-react: backend returned non-fill status=%s for %s — NO FILL",
                fill.status,
                proposal.proposal_id,
            )
            return self._nofill_record(
                proposal,
                signal_id=signal_id,
                asof_decision=asof_decision,
                asof_execution=now,
                decision_price=decision_price,
                requested_pct=fill_size_pct,
                bar_ts=bar_ts,
                play_tag=play_tag,
                approver_user_id=approver_user_id,
                reason=f"backend_status_{fill.status}",
                reason_key="backend_nofill",
                cap_metadata=cap_metadata,
            )

        # ── successful fill: build the record (mirror AlpacaPaperReactor) ───────
        filled_avg_price = float(fill.filled_avg_price)
        filled_qty = float(fill.filled_qty)  # signed TRUE shares (backend truth)
        filled_notional = abs(filled_qty) * filled_avg_price
        # Realized NAV fraction = what ACTUALLY filled, signed to match the side.
        realized_fill_pct = (filled_notional / account_equity) if account_equity > 0 else 0.0
        realized_fill_pct = realized_fill_pct if fill_size_pct > 0 else -realized_fill_pct

        record = ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=signal_id,
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision=asof_decision,
            asof_execution=now,
            target_position_pct=fill_size_pct,  # the (cap-clipped) requested NAV fraction
            decision_price=decision_price,
            fill_price=filled_avg_price,  # backend fill (= slipped price we passed)
            fill_size_pct=realized_fill_pct,  # actual filled fraction (true-notional)
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "paper": True,
                "deterministic_backend": True,
                "account_id": DETERMINISTIC_EQUITY_ACCOUNT_ID,
                "backend": backend.name,
                "backend_order_id": fill.order_id,
                "backend_status": fill.status,
                # signed TRUE shares -> apply_execution accounts cash by real notional.
                "quantity": filled_qty,
                "filled_avg_price": filled_avg_price,
                "filled_notional": filled_notional,
                "account_equity": account_equity,
                "requested_target_pct": fill_size_pct,
                "slippage_model": slippage_mode,
                "slippage_breakdown": slippage_breakdown,
                "advisor_caveats": (proposal.advisor_result or {}).get("caveats", []),
                # ADR-0087 cap audit trail (None on full-pass / cap-flag-OFF).
                **(cap_metadata or {}),
            },
            bar_ts=bar_ts,
            play_tag=play_tag,
        )

        self._append_bus(record)
        logger.info(
            "det-equity-react: %s asset=%s target=%+.4f realized=%+.4f "
            "fill_price=%.4f qty=%+.4f status=%s order=%s",
            record.proposal_id,
            record.asset,
            fill_size_pct,
            realized_fill_pct,
            filled_avg_price,
            filled_qty,
            fill.status,
            fill.order_id,
        )
        self._reconcile_state(record)
        self._maybe_reflect(record, proposal)
        return record

    # ------------------------------------------------------------------
    # Slippage (ADR-0070) — same logic as PaperReactor.execute inline block
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_slippage(
        proposal: Any, decision_price: float, fill_size_pct: float, now: str
    ) -> tuple[float, str, dict[str, float] | None]:
        """Return (fill_price, slippage_mode, slippage_breakdown).

        Bit-identical to PaperReactor.execute's slippage block: the deterministic
        per-fill envelope is applied by DEFAULT (v0.2, per FLAGS.md Tier-A promotion);
        set HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.1 to opt OUT to the legacy passthrough
        (fill_price = decision_price). A bad input degrades to passthrough with an
        error breakdown (never fails the fill).
        """
        slippage_mode = os.environ.get("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        slippage_breakdown: dict[str, float] | None = None
        if slippage_mode != "v0.2":
            return decision_price, slippage_mode, None

        from hermes_quant.react.slippage_model import (
            apply_slippage,
            is_late_session_equity,
        )

        is_late = (
            is_late_session_equity(now) if proposal.asset_class == "equity" else False
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
            logger.warning(
                "det-equity-react: slippage_model rejected fill for %s: %s; "
                "degraded to passthrough",
                proposal.proposal_id,
                exc,
            )
            fill_price = decision_price
            slippage_breakdown = {"error": str(exc)}  # type: ignore[dict-item]
        return fill_price, slippage_mode, slippage_breakdown

    # ------------------------------------------------------------------
    # No-fill record builder (fail-closed; NEVER a fabricated fill price)
    # ------------------------------------------------------------------
    def _nofill_record(
        self,
        proposal: Any,
        *,
        signal_id: str | None,
        asof_decision: str,
        asof_execution: str,
        decision_price: float,
        requested_pct: float,
        bar_ts: str | None,
        play_tag: str,
        approver_user_id: str | None,
        reason: str,
        reason_key: str,
        cap_metadata: dict[str, Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        """Build a NO-FILL ExecutionRecord (fill_price=0.0, fill_size_pct=0.0).

        Appended to the bus (so the autonomous tick / audit trail SEES the rejection),
        but it moves no position so state.db is NOT reconciled. ``reason_key`` is set
        to ``bp_rejected`` / ``backend_unavailable`` / etc. as a True flag so a reader
        can branch on the outcome.
        """
        metadata: dict[str, Any] = {
            "paper": True,
            "deterministic_backend": True,
            "account_id": DETERMINISTIC_EQUITY_ACCOUNT_ID,
            "no_fill": True,
            reason_key: True,
            "no_fill_reason": reason,
            "requested_target_pct": requested_pct,
            **(cap_metadata or {}),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        record = ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=signal_id,
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision=asof_decision,
            asof_execution=asof_execution,
            target_position_pct=requested_pct,
            decision_price=decision_price,
            fill_price=0.0,  # NEVER fabricated — nothing filled
            fill_size_pct=0.0,
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata=metadata,
            bar_ts=bar_ts,
            play_tag=play_tag,
        )
        # A no-fill moves no position — append for audit, but do NOT reconcile state.db.
        self._append_bus(record)
        return record

    # ------------------------------------------------------------------
    # Bus + state reconciliation (same patterns as Paper/Alpaca reactors)
    # ------------------------------------------------------------------
    def _existing_fill(self, proposal_id: str) -> ExecutionRecord | None:
        """Scan the bus for a prior ACTUAL fill from this reactor for proposal_id.

        F-1 idempotency: returns the reconstructed ExecutionRecord of a prior
        fill (reactor_name == self.name, fill_price > 0) so a re-fire of an
        already-filled proposal is a no-op rather than a double-book. A prior
        no-fill audit record (fill_price == 0.0 — BP reject, unknown NAV, etc.)
        is IGNORED so a legitimate retry after capital frees up is not blocked.
        Tolerant of a missing/partial bus (same posture as the multileg reactor).
        """
        if not self.executions_path.exists():
            return None
        try:
            raw = self.executions_path.read_bytes()
        except OSError:
            return None
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if (
                rec.get("proposal_id") == proposal_id
                and rec.get("reactor_name") == self.name
                and float(rec.get("fill_price") or 0.0) > 0.0
            ):
                return _dict_to_record(rec)
        return None

    def _append_bus(self, record: ExecutionRecord) -> None:
        line = json.dumps(_record_to_dict(record), separators=(",", ":"), sort_keys=True) + "\n"
        with append_locked(self.executions_path) as fd:
            os.write(fd, line.encode("utf-8"))

    def _reconcile_state(self, record: ExecutionRecord) -> None:
        """Mirror the backend fill into state.db under the ``paper-default`` partition.

        Same shape as PaperReactor's reconcile (inject account_id). The signed-shares
        ``quantity`` in reactor_metadata makes apply_execution track the position in
        TRUE share units and account cash by real notional. Non-blocking: a state
        write error must never block the (already-recorded) fill.
        """
        try:
            from hermes_quant.state.portfolio_state import get_portfolio_state

            record_dict = _record_to_dict(record)
            record_dict["account_id"] = DETERMINISTIC_EQUITY_ACCOUNT_ID
            get_portfolio_state().apply_execution(record_dict)
        except Exception as exc:  # noqa: BLE001 — defensive, non-blocking
            logger.warning(
                "det-equity-react: PortfolioState.apply_execution failed (non-blocking): %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Reflection hook (HERMES_QUANT_REFLECTION) — parity with PaperReactor
    # ------------------------------------------------------------------
    @staticmethod
    def _maybe_reflect(record: ExecutionRecord, proposal: Any) -> None:
        """Trigger the Wave-4 reflection hooks on a fill (default ON).

        Set HERMES_QUANT_REFLECTION=0 to opt out. Promoted to default-ON
        2026-06-05 (FLAGS.md Tier A) — the hook is deterministic (Reflector
        stub formatter unless REFLECTOR_LLM=1), cheap, and a no-op when no
        pending decision matches the fill.

        Bit-identical gating to PaperReactor: only runs when
        HERMES_QUANT_REFLECTION=1; non-blocking on any failure.
        """
        if os.environ.get("HERMES_QUANT_REFLECTION", "1") != "1":
            return
        try:
            from hermes_quant.memory._paper_reflection_hook import (
                maybe_record_decision_on_open,
                maybe_reflect_on_close,
            )

            maybe_record_decision_on_open(record, proposal)
            maybe_reflect_on_close(record, proposal)
        except Exception as exc:  # noqa: BLE001 — non-blocking
            logger.warning("det-equity-react: reflection hook failed (non-blocking): %s", exc)


# Re-export the shared NAV provider so callers / tests can reference the SAME source
# the admissibility seam uses (paper-default equity_total). Kept as a module symbol
# for parity with paper.py / alpaca_paper.py.
__all__ = ["DeterministicEquityReactor", "DETERMINISTIC_EQUITY_ACCOUNT_ID", "_account_nav_usd"]
