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
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, append_locked

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

# Terminal/non-terminal order statuses (mirrors mleg_fill._TERMINAL).
# 'partially_filled' is intentionally NOT here: for a working DAY market order it
# is NON-terminal (the remainder can still fill), so the poll loop keeps polling
# through it rather than treating it as final (P1-D). It is only recorded once the
# order reaches a genuinely terminal status.
_REJECT_STATUSES = frozenset({"rejected", "canceled", "cancelled", "expired", "done_for_day"})

# Poll budget for the order to reach a terminal/fill status. Bounded so the seam
# never blocks indefinitely; an unfilled order returns a 0.0 fill (reconcile later).
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 1.0


class AlpacaSubmitError(RuntimeError):
    """Order submission to Alpaca failed (creds/build/submit). Fail closed."""


def _build_paper_trading_client() -> Any:
    """Build a paper ``TradingClient`` from the EXACT existing env-var pattern.

    Mirrors ``hermes_quant.admissibility.oracle._resolve_client`` and
    ``training.bootstrap_calibrator`` — does NOT invent new env vars. Raises
    ``AlpacaSubmitError`` (fail-closed) if creds are absent or the import/build
    fails, so a flag-on run without creds fails LOUD rather than silently no-ops.
    """
    try:
        from alpaca.trading.client import TradingClient
    except Exception as exc:  # noqa: BLE001 — alpaca-py optional at import time
        raise AlpacaSubmitError(
            f"alpaca-py is required for AlpacaPaperReactor but failed to import: {exc}"
        ) from exc

    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise AlpacaSubmitError(
            "ALPACA_API_KEY and ALPACA_API_SECRET (or *_ID/*_KEY aliases) are required "
            "for AlpacaPaperReactor; refusing to route a paper fill without creds"
        )
    try:
        return TradingClient(api_key=key, secret_key=secret, paper=True)
    except Exception as exc:  # noqa: BLE001 — client build can fail on bad creds
        raise AlpacaSubmitError(f"failed to build paper TradingClient: {exc}") from exc


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion (Alpaca returns Decimals/strs); None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

        adv = proposal.advisor_result or {}
        asof_decision = adv.get("decision_wall_clock") or adv.get("as_of") or now
        bar_ts = adv.get("bar_ts") or adv.get("as_of")

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
        if equity is None or equity <= 0:
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
        return str(getattr(order, "id", "") or getattr(order, "client_order_id", ""))

    def _poll_until_filled(
        self, client: Any, order: Any, order_id: str
    ) -> tuple[float, float, str] | None:
        """Poll get_order_by_id until the order is TERMINAL, then report the fill.

        Returns ``(filled_avg_price, filled_qty, status)`` when the order reaches
        a terminal state with a positive fill (fully ``filled``, or
        ``canceled``/``expired``/``done_for_day`` carrying a partial), or ``None``
        when the poll budget elapses with no fill recorded. Raises
        ``AlpacaSubmitError`` on a terminal REJECT with zero fill.

        P1-D fix: ``partially_filled`` is a NON-terminal status for a working DAY
        market order — the remainder can still fill milliseconds later. We do NOT
        return on the first partial snapshot (that under-accounts the position).
        We keep polling until the order is terminal.

        P1-C fix: on timeout the order is still WORKING at the broker (DAY TIF) —
        leaving it open orphans a position the book doesn't record. We CANCEL the
        working order, then re-read it: if the cancel raced a fill we record the
        realized fill (terminal), otherwise we return None (a clean 0-fill the
        caller records as unfilled, with no working order left behind).
        """
        deadline = time.monotonic() + self._poll_timeout_s

        current = order
        while True:
            status = str(getattr(current, "status", "") or "").lower()

            # Fully filled — terminal, record it.
            if status == "filled":
                fill = self._extract_fill(current)
                if fill is not None:
                    return (*fill, status)
                # 'filled' but fields not yet populated — keep polling for them.
            # Terminal reject/close statuses: record a partial if one exists
            # (P3-B: done_for_day / canceled can carry a partial fill), else
            # surface the rejection (fail-closed, never fabricate).
            elif status in _REJECT_STATUSES:
                fill = self._extract_fill(current)
                if fill is not None:
                    return (*fill, status)
                raise AlpacaSubmitError(
                    f"alpaca order {order_id} reached terminal status {status!r} "
                    "with no fill — surfacing rejection, not fabricating a fill"
                )
            # 'partially_filled', 'new', 'accepted', 'pending_*' — NON-terminal.
            # Do NOT return yet (P1-D): the order is still working.

            if time.monotonic() >= deadline:
                # P1-C: cancel the still-working order so it cannot fill later and
                # orphan a position the book never recorded. Then re-read once: a
                # cancel can race a fill at the broker.
                return self._cancel_and_settle(client, order_id)

            time.sleep(self._poll_interval_s)
            try:
                current = client.get_order_by_id(order_id)
            except Exception as exc:  # noqa: BLE001 — poll error, surface it
                raise AlpacaSubmitError(
                    f"get_order_by_id({order_id}) failed during poll: {exc}"
                ) from exc

    @staticmethod
    def _extract_fill(order: Any) -> tuple[float, float] | None:
        """Return (filled_avg_price, filled_qty) iff both are positive, else None."""
        price = _to_float(getattr(order, "filled_avg_price", None))
        qty = _to_float(getattr(order, "filled_qty", None))
        if price is not None and price > 0 and qty is not None and qty > 0:
            return price, qty
        return None

    def _cancel_and_settle(
        self, client: Any, order_id: str
    ) -> tuple[float, float, str] | None:
        """Cancel a still-working order on timeout; record any realized partial.

        P1-C: a DAY market order left working after our poll budget would fill at
        the broker and orphan an unrecorded position. We cancel it, then re-read:
          * if the order had (or raced into) a partial/full fill -> record it
            (terminal — the cancel only removes the UNfilled remainder);
          * otherwise -> None (a clean unfilled; no working order remains).
        Cancel failures are non-fatal here: we still re-read and report whatever
        actually filled. Worst case we return None and the caller records a
        0-fill — but we never leave a silently-working order with a recorded
        0-fill (the orphan failure mode).
        """
        try:
            client.cancel_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001 — cancel best-effort; still re-read
            logger.warning(
                "alpaca-react: cancel_order_by_id(%s) failed on timeout: %s "
                "(re-reading to settle any partial)",
                order_id,
                exc,
            )
        # Give the cancel a moment to settle, then read the final state.
        time.sleep(self._poll_interval_s)
        try:
            final = client.get_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alpaca-react: post-cancel get_order_by_id(%s) failed: %s",
                order_id,
                exc,
            )
            return None
        fill = self._extract_fill(final)
        if fill is not None:
            status = str(getattr(final, "status", "") or "").lower() or "canceled"
            logger.info(
                "alpaca-react: order %s canceled on timeout but had a realized "
                "partial fill — recording it (status=%s)",
                order_id,
                status,
            )
            return (*fill, status)
        return None

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
