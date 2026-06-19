"""hermes_quant.react.alpaca_paper — AlpacaPaperReactor (ADR-00xx, additive).

Routes equity paper fills through Alpaca's *paper-trading* broker instead of the
home-grown synthetic append-only book (``PaperReactor``). The broker is the
source of truth: it enforces buying power, margin, and shorting rules natively
and reports REAL fills (avg fill price + filled qty). Reconciling Alpaca truth
into ``state.db`` is the whole point — so this reactor writes the materialized
PortfolioState view under a SEPARATE account partition (``alpaca-paper``) from
the synthetic ``paper-default`` book, which is what enables shadow comparison.

Design rails (money-software):
  * ADDITIVE + DEFAULT-OFF. Selected only when ``HERMES_QUANT_ALPACA_PAPER=1``
    AND the proposal is an equity proposal (see ``react.dispatch.select_reactor``).
    With the flag unset, the equity path is bit-for-bit the legacy ``PaperReactor``.
  * INJECTABLE client. The constructor takes ``client=None`` and lazily builds a
    paper ``TradingClient`` from the EXACT existing env-var pattern (no new env
    vars). Unit tests inject a fake client — no network.
  * Precondition chain MIRRORS ``PaperReactor`` ORDER:
      1. ``_enforce_fill_size_invariant`` (reused from paper.py).
      2. ``_admissibility_reject`` short-equity precondition (reused seam,
         default-OFF behind HERMES_QUANT_ADMISSIBILITY).
      3. portfolio-cap clip is INTENTIONALLY SKIPPED — Alpaca enforces buying
         power / gross-exposure natively, so the band-aid cap (ADR-0087) is
         obsolete on this path. (See note in execute().)
  * FAIL-CLOSED. Missing creds / client build failure / submit rejection (e.g.
    insufficient buying power, 422) RAISE a clear error — a buying-power
    rejection is a LEGITIMATE outcome that must surface, never be masked with a
    fabricated fill. An order that is submitted but never fills within the poll
    timeout returns an ExecutionRecord with ``fill_size_pct=0.0`` and metadata
    noting ``unfilled_timeout`` + the alpaca order id (so it can be reconciled
    later) — never a fabricated fill price.
  * DETERMINISTIC. No RNG; asof uses ``datetime.now(UTC)``.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

from . import _alpaca_exec
from ._alpaca_exec import (
    AlpacaSubmitError,
)
from ._alpaca_exec import (
    build_paper_trading_client as _build_paper_trading_client,
)
from ._alpaca_exec import (
    to_float as _to_float,
)
from .admissibility_precondition import admissibility_reject_equity
from .base import ExecutionRecord
from .paper import (
    _enforce_fill_size_invariant,
    _record_to_dict,
)

logger = logging.getLogger(__name__)

# The Alpaca-reconciled book lives in its OWN partition so it is a SEPARATE book
# from the synthetic "paper-default" one. This is what enables shadow comparison.
ALPACA_ACCOUNT_ID = "alpaca-paper"

# Terminal/non-terminal order statuses + poll cadence now live in ._alpaca_exec
# (shared with backends.alpaca_backend). Re-exported here as the historical names
# so existing imports / behavior stay bit-identical.
_REJECT_STATUSES = _alpaca_exec.REJECT_STATUSES
_POLL_TIMEOUT_S = _alpaca_exec.POLL_TIMEOUT_S
_POLL_INTERVAL_S = _alpaca_exec.POLL_INTERVAL_S


class AlpacaPaperReactor:
    """Reactor that routes equity paper fills through Alpaca's paper broker.

    The broker enforces buying power / margin / shorting and reports the REAL
    avg fill price + filled qty. The ExecutionRecord reflects what ACTUALLY
    filled (broker truth), not what was requested. Reconciliation writes to the
    ``alpaca-paper`` PortfolioState partition.
    """

    name = "alpaca_paper"
    # The reactor needs Alpaca creds — but the client is lazily built so a
    # flag-off / test-injected path never touches the network.
    requires_credentials = True

    def __init__(
        self,
        *,
        client: Any | None = None,
        executions_path: Path | None = None,
        poll_timeout_s: float = _POLL_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        # INJECTABLE client: tests pass a fake; production passes None and the
        # client is lazily built on first execute() from the existing env vars.
        self._client = client
        self.executions_path = executions_path or EXECUTION_BUS_PATH
        self.executions_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.executions_path.exists():
            self.executions_path.touch()
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = _build_paper_trading_client()
        return self._client

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
        """Route a signed NAV-fraction fill through Alpaca paper, return the record.

        Mirrors ``PaperReactor.execute`` precondition ORDER:
          1. fill_size invariant (reused helper).
          2. short-equity admissibility precondition (reused seam, default-OFF).
          3. portfolio-cap clip — INTENTIONALLY NOT APPLIED here: Alpaca enforces
             buying power / gross exposure natively (it will REJECT an over-BP
             order, which we surface), so the ADR-0087 band-aid cap is obsolete on
             this path. We deliberately omit it.
        """
        fill_size_pct = _enforce_fill_size_invariant(proposal, fill_size_pct)
        decision_price = self._extract_decision_price(proposal)
        signal_id = self._extract_signal_id(proposal)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        adv = proposal.advisor_result or {}
        asof_decision = adv.get("decision_wall_clock") or adv.get("as_of") or now
        bar_ts = adv.get("bar_ts") or adv.get("as_of")

        # ── precondition 1b: usable decision_price (cr05/ar32 precondition parity) ──
        # MIRRORS the sibling reactors' fail-closed guard: PaperReactor and
        # DeterministicEquityReactor refuse to size a fill off a non-finite / <=0
        # decision_price (det_equity returns a no-fill record at the <=0 price
        # check). ``_extract_decision_price`` returns the 0.0 sentinel when neither
        # an advisor ``decision_price`` nor an analyst_views ``last_close`` is
        # present, and a non-finite top-level value coerces straight through.
        # A corrupt/missing entry-basis must NOT be submitted to the broker or
        # recorded verbatim into ExecutionRecord.decision_price — fail closed with a
        # silence record (no order, no position, no state.db reconcile). We RETURN a
        # record (never raise) because the live fire loop calls execute() without a
        # try/except (same rationale as the unfilled-timeout path below).
        if not math.isfinite(decision_price) or decision_price <= 0.0:
            logger.warning(
                "alpaca-react: %s asset=%s has no usable decision_price (%r); "
                "fail-closed silence (no order submitted)",
                proposal.proposal_id,
                proposal.symbol,
                decision_price,
            )
            record = ExecutionRecord(
                proposal_id=proposal.proposal_id,
                signal_id=signal_id,
                asset=proposal.symbol,
                asset_class=proposal.asset_class,
                timeframe=proposal.timeframe,
                asof_decision=asof_decision,
                asof_execution=now,
                target_position_pct=fill_size_pct,
                decision_price=0.0,  # NEVER record a non-finite basis
                fill_price=0.0,  # NEVER fabricated — nothing was filled
                fill_size_pct=0.0,
                reactor_name=self.name,
                human_in_the_loop=True,
                approver_user_id=approver_user_id,
                reactor_metadata={
                    "alpaca_paper": True,
                    "account_id": ALPACA_ACCOUNT_ID,
                    "silenced": True,
                    "silence_reason": "zero_decision_price",
                    "requested_target_pct": fill_size_pct,
                },
                bar_ts=bar_ts,
                play_tag=play_tag,
            )
            # A silence moves no position — append for audit, do NOT reconcile state.db.
            self._append_bus(record)
            return record

        # ── precondition 2: short-equity admissibility (ADR-0077/0079) ──────────
        # DEFAULT-OFF behind HERMES_QUANT_ADMISSIBILITY; bit-for-bit no-op when
        # the flag is absent. REJECT-only / fail-closed (can only refuse to fill).
        admissibility_reject = self._admissibility_reject(
            proposal, fill_size_pct, now, play_tag=play_tag
        )
        if admissibility_reject is not None:
            return admissibility_reject

        # ── precondition 3: portfolio-cap clip — INTENTIONALLY SKIPPED ──────────
        # Alpaca's paper broker enforces buying power / margin / gross exposure
        # natively and will REJECT an over-buying-power order (surfaced below as
        # AlpacaSubmitError). The home-grown HERMES_QUANT_PORTFOLIO_CAPS band-aid
        # is therefore obsolete on this path and deliberately NOT applied.
        # (asof_decision / bar_ts were resolved above for the decision_price guard.)

        # ── submit to Alpaca + poll for the real fill ───────────────────────────
        client = self._resolve_client()  # raises AlpacaSubmitError if creds absent

        account_equity = self._fetch_account_equity(client)
        # Convert the signed NAV fraction to a NOTIONAL USD order (fractional-safe).
        notional_usd = abs(fill_size_pct) * account_equity
        side = self._order_side(fill_size_pct)

        order = self._submit_order(
            client,
            symbol=proposal.symbol,
            notional_usd=notional_usd,
            side=side,
            client_order_id=proposal.proposal_id,
        )
        order_id = self._order_id(order)
        if not order_id:
            # A submit that returns no usable id cannot be polled or reconciled.
            # Fail closed rather than poll a blank id (P2-B).
            raise AlpacaSubmitError(
                f"submit_order for {proposal.symbol} returned no order id; "
                "refusing to proceed (cannot poll/reconcile a blank id)"
            )

        filled = self._poll_until_filled(client, order, order_id)

        if filled is None:
            # Submitted but never reached a fill within the poll budget. Return a
            # 0.0-fill record with metadata so it can be reconciled later. We do
            # NOT fabricate a fill price.
            logger.warning(
                "alpaca-react: order %s for %s NOT filled within %.1fs poll budget "
                "(target=%+.4f) — returning unfilled_timeout record",
                order_id,
                proposal.symbol,
                self._poll_timeout_s,
                fill_size_pct,
            )
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
                fill_price=0.0,  # NEVER fabricated — order is unfilled
                fill_size_pct=0.0,
                reactor_name=self.name,
                human_in_the_loop=True,
                approver_user_id=approver_user_id,
                reactor_metadata={
                    "alpaca_paper": True,
                    "account_id": ALPACA_ACCOUNT_ID,
                    "unfilled_timeout": True,
                    "alpaca_order_id": order_id,
                    "requested_target_pct": fill_size_pct,
                    "account_equity": account_equity,
                    "notional_usd": notional_usd,
                },
                bar_ts=bar_ts,
                play_tag=play_tag,
            )
            # An unfilled order moves no position — do NOT reconcile state.db.
            self._append_bus(record)
            return record

        filled_avg_price, filled_qty, status = filled
        # signed shares: BUY -> +, SELL -> -
        signed_qty = filled_qty if fill_size_pct > 0 else -filled_qty
        filled_notional = filled_avg_price * filled_qty
        # Realized NAV fraction = what ACTUALLY filled (may differ from target on
        # a partial fill). Signed to match the requested side.
        realized_fill_pct = (
            (filled_notional / account_equity) if account_equity > 0 else 0.0
        )
        realized_fill_pct = realized_fill_pct if fill_size_pct > 0 else -realized_fill_pct

        record = ExecutionRecord(
            proposal_id=proposal.proposal_id,
            signal_id=signal_id,
            asset=proposal.symbol,
            asset_class=proposal.asset_class,
            timeframe=proposal.timeframe,
            asof_decision=asof_decision,
            asof_execution=now,
            target_position_pct=fill_size_pct,  # the REQUESTED NAV fraction
            decision_price=decision_price,
            fill_price=filled_avg_price,  # BROKER-REPORTED avg fill price
            fill_size_pct=realized_fill_pct,  # ACTUAL filled fraction (partial-aware)
            reactor_name=self.name,
            human_in_the_loop=True,
            approver_user_id=approver_user_id,
            reactor_metadata={
                "alpaca_paper": True,
                "account_id": ALPACA_ACCOUNT_ID,
                "alpaca_order_id": order_id,
                "alpaca_status": status,
                "filled_qty": signed_qty,
                "filled_avg_price": filled_avg_price,
                "filled_notional": filled_notional,
                "account_equity": account_equity,
                "requested_target_pct": fill_size_pct,
                # signed-shares for state.db true-unit position tracking (the
                # apply_execution path keys on reactor_metadata.quantity).
                "quantity": signed_qty,
                "advisor_caveats": (proposal.advisor_result or {}).get("caveats", []),
            },
            bar_ts=bar_ts,
            play_tag=play_tag,
        )

        self._append_bus(record)
        logger.info(
            "alpaca-react: %s asset=%s target=%+.4f realized=%+.4f "
            "fill_price=%.4f qty=%+.4f status=%s order=%s",
            record.proposal_id,
            record.asset,
            fill_size_pct,
            realized_fill_pct,
            filled_avg_price,
            signed_qty,
            status,
            order_id,
        )
        self._reconcile_state(record)
        return record

    # ------------------------------------------------------------------
    # Alpaca order mechanics (all take the injected client)
    # ------------------------------------------------------------------
    def _fetch_account_equity(self, client: Any) -> float:
        """Read account equity (USD) from the broker. Fail-closed on bad value."""
        try:
            account = client.get_account()
        except Exception as exc:  # noqa: BLE001 — surface broker/account errors
            raise AlpacaSubmitError(f"get_account() failed: {exc}") from exc
        equity = _to_float(getattr(account, "equity", None))
        # NaN/inf MUST be rejected: ``_to_float`` catches only (TypeError,
        # ValueError), so float(Decimal('NaN'))/float('inf')/float('1e400')
        # succeed and return nan/inf. A non-finite NAV defeats BOTH the
        # ``<= 0`` check here (nan<=0 / inf<=0 are False) AND the downstream
        # zero-notional guard (nan<1.0 / +inf<1.0 are False), so it would size
        # a NaN/inf-notional order. Finite-guard the NAV numerator (mirrors the
        # math.isfinite price guards elsewhere on this path).
        if equity is None or not math.isfinite(equity) or equity <= 0:
            raise AlpacaSubmitError(
                f"account equity unavailable or non-positive ({equity!r}); "
                "refusing to size an order off a bad NAV"
            )
        return equity

    @staticmethod
    def _order_side(fill_size_pct: float) -> Any:
        """Map a signed NAV fraction to an Alpaca OrderSide (BUY if >0 else SELL)."""
        from alpaca.trading.enums import OrderSide

        return OrderSide.BUY if fill_size_pct > 0 else OrderSide.SELL

    def _submit_order(
        self,
        client: Any,
        *,
        symbol: str,
        notional_usd: float,
        side: Any,
        client_order_id: str | None = None,
    ) -> Any:
        """Submit a NOTIONAL market order (fractional-safe). Fail-closed on reject.

        A buying-power / 422 rejection raises ``AlpacaSubmitError`` — a legitimate
        outcome that MUST surface, never be masked by a fabricated fill.

        Idempotency (P2-A): a ``client_order_id`` derived from the proposal_id is
        attached so a RETRY after a submit-then-poll-error cannot double-submit —
        Alpaca rejects a duplicate client_order_id, which surfaces here as an
        AlpacaSubmitError (fail-closed) rather than opening a second position.

        Zero-notional guard (P2-C): a notional that rounds to <= 0 (or below
        Alpaca's $1 minimum) is refused rather than submitting a meaningless
        $0.00 order.
        """
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        rounded = round(notional_usd, 2)
        if rounded < 1.0:
            # Alpaca's minimum notional is $1; a sub-$1 (or rounded-to-zero)
            # order is meaningless. Fail closed — do not submit.
            raise AlpacaSubmitError(
                f"notional ${rounded:.2f} for {symbol} is below the $1 minimum "
                "after rounding; refusing to submit a meaningless order"
            )

        req_kwargs: dict[str, Any] = {
            "symbol": symbol,
            "notional": rounded,
            "side": side,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            # Deterministic per-proposal id so a retry collides (Alpaca rejects
            # duplicate client_order_id) instead of double-submitting.
            req_kwargs["client_order_id"] = str(client_order_id)
        req = MarketOrderRequest(**req_kwargs)
        try:
            return client.submit_order(req)
        except Exception as exc:  # noqa: BLE001 — insufficient BP / 422 / dup id / network
            raise AlpacaSubmitError(
                f"submit_order rejected for {symbol} notional=${rounded:.2f}: {exc}"
            ) from exc

    @staticmethod
    def _order_id(order: Any) -> str:
        return _alpaca_exec.order_id_of(order)

    def _poll_until_filled(
        self, client: Any, order: Any, order_id: str
    ) -> tuple[float, float, str] | None:
        """Poll get_order_by_id until TERMINAL, then report the fill.

        Delegates to the shared ``_alpaca_exec.poll_until_filled`` (factored out so
        the equity reactor and ``AlpacaBackend`` reuse the SAME P1/P2/P3 semantics):
        partial is non-terminal (P1-D), cancel-on-timeout + re-read (P1-C), a
        terminal reject with no fill RAISES (fail-closed), done_for_day/canceled
        partials are recorded (P3-B).
        """
        return _alpaca_exec.poll_until_filled(
            client,
            order,
            order_id,
            poll_timeout_s=self._poll_timeout_s,
            poll_interval_s=self._poll_interval_s,
            logger=logger,
        )

    @staticmethod
    def _extract_fill(order: Any) -> tuple[float, float] | None:
        """Return (filled_avg_price, filled_qty) iff both are positive, else None."""
        return _alpaca_exec.extract_fill(order)

    def _cancel_and_settle(
        self, client: Any, order_id: str
    ) -> tuple[float, float, str] | None:
        """Cancel a still-working order on timeout; record any realized partial.

        Delegates to the shared ``_alpaca_exec.cancel_and_settle`` (P1-C/P3-B).
        """
        return _alpaca_exec.cancel_and_settle(
            client,
            order_id,
            poll_interval_s=self._poll_interval_s,
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Bus + state reconciliation
    # ------------------------------------------------------------------
    def _append_bus(self, record: ExecutionRecord) -> None:
        line = json.dumps(_record_to_dict(record), separators=(",", ":"), sort_keys=True) + "\n"
        with append_locked(self.executions_path) as fd:
            os.write(fd, line.encode("utf-8"))

    def _reconcile_state(self, record: ExecutionRecord) -> None:
        """Mirror the broker fill into state.db under the ``alpaca-paper`` partition.

        Same shape as PaperReactor's reconcile (inject account_id), but the
        account is ``alpaca-paper`` so the Alpaca-reconciled book is a SEPARATE
        partition from the synthetic ``paper-default`` book. The signed-shares
        ``quantity`` in reactor_metadata makes apply_execution track the position
        in TRUE share units (broker truth), not the NAV-fraction proxy.

        Non-blocking: a state write error must never block the (already-real) fill.
        """
        try:
            from hermes_quant.state.portfolio_state import get_portfolio_state

            record_dict = _record_to_dict(record)
            record_dict["account_id"] = ALPACA_ACCOUNT_ID
            get_portfolio_state().apply_execution(record_dict)
        except Exception as exc:  # noqa: BLE001 — defensive, non-blocking
            logger.warning(
                "alpaca-react: PortfolioState.apply_execution failed (non-blocking): %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Preconditions / extractors (reuse PaperReactor's logic)
    # ------------------------------------------------------------------
    def _admissibility_reject(
        self, proposal: Any, fill_size_pct: float, now: str, *, play_tag: str = "advisor"
    ) -> ExecutionRecord | None:
        """Short-equity admissibility precondition — delegates to the shared seam.

        Bit-identical to PaperReactor's: default-OFF behind HERMES_QUANT_ADMISSIBILITY,
        REJECT-only / fail-closed. Uses the SAME NAV provider as the synthetic book
        (the materialized paper-default NAV) so the admissibility share-conversion
        agrees with the autonomous seam.
        """
        from .paper import _account_nav_usd

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

    @staticmethod
    def _extract_decision_price(proposal: Any) -> float:
        """Identical logic to PaperReactor._extract_decision_price."""
        ar = proposal.advisor_result or {}
        top_dp = ar.get("decision_price")
        if top_dp is not None:
            try:
                return float(top_dp)
            except (TypeError, ValueError):
                pass
        for view in ar.get("analyst_views") or []:
            md = view.get("metadata") or {}
            if "last_close" in md:
                try:
                    return float(md["last_close"])
                except (TypeError, ValueError):
                    pass
        return 0.0

    @staticmethod
    def _extract_signal_id(proposal: Any) -> str | None:
        """Identical logic to PaperReactor._extract_signal_id."""
        ar = proposal.advisor_result or {}
        sid = ar.get("signal_id")
        if sid is not None:
            return str(sid)
        return None
