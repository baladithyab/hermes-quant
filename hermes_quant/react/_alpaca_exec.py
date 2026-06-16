"""hermes_quant.react._alpaca_exec — shared Alpaca submit/poll/cancel mechanics.

Factored out of ``alpaca_paper.py`` (PR #69) so BOTH the equity ``AlpacaPaperReactor``
and the protocol-shaped ``backends.alpaca_backend.AlpacaBackend`` reuse the SAME,
hard-won fail-closed poll/cancel/extract-fill semantics rather than duplicating them.

The behavior here is intentionally bit-identical to the original in-class methods on
``AlpacaPaperReactor`` (all P1/P2/P3 fixes preserved):

  * ``partially_filled`` is NON-terminal for a working DAY order (P1-D): keep polling.
  * on poll-budget timeout, CANCEL the still-working order then re-read once so a
    cancel-vs-fill race still records the realized partial (P1-C / P3-B).
  * a terminal reject/close with NO fill RAISES (fail-closed; never fabricated).
  * ``done_for_day`` / ``canceled`` carrying a partial is recorded (P3-B).
  * an empty order id is the caller's guard (P2-B); credentials/build guard (P2-A/C)
    live with the caller too.

These are free functions (taking ``client``, ``logger``, and the poll cadence as
explicit params) so they are trivially shared and unit-testable without a class.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

# Terminal reject/close statuses (mirrors alpaca_paper._REJECT_STATUSES). NOTE
# 'partially_filled' is intentionally NOT here: for a working DAY order it is
# NON-terminal (the remainder can still fill), so the poll loop keeps polling
# through it (P1-D) and only records it once the order is genuinely terminal.
REJECT_STATUSES = frozenset(
    {"rejected", "canceled", "cancelled", "expired", "done_for_day"}
)

# Default poll budget for an order to reach a terminal/fill status.
POLL_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 1.0


class AlpacaSubmitError(RuntimeError):
    """Order submission to Alpaca failed (creds/build/submit/poll). Fail closed."""


def build_paper_trading_client() -> Any:
    """Build a paper ``TradingClient`` from the EXACT existing env-var pattern.

    Mirrors ``hermes_quant.admissibility.oracle._resolve_client`` — does NOT invent
    new env vars. Raises ``AlpacaSubmitError`` (fail-closed) if creds are absent or
    the import/build fails, so a flag-on run without creds fails LOUD.
    """
    try:
        from alpaca.trading.client import TradingClient
    except Exception as exc:  # noqa: BLE001 — alpaca-py optional at import time
        raise AlpacaSubmitError(
            f"alpaca-py is required but failed to import: {exc}"
        ) from exc

    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get(
        "ALPACA_API_SECRET_KEY"
    )
    if not key or not secret:
        raise AlpacaSubmitError(
            "ALPACA_API_KEY and ALPACA_API_SECRET (or *_ID/*_KEY aliases) are "
            "required; refusing to route a paper fill without creds"
        )
    try:
        return TradingClient(api_key=key, secret_key=secret, paper=True)
    except Exception as exc:  # noqa: BLE001 — client build can fail on bad creds
        raise AlpacaSubmitError(f"failed to build paper TradingClient: {exc}") from exc


def to_float(value: Any) -> float | None:
    """Best-effort float coercion (Alpaca returns Decimals/strs); None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def order_id_of(order: Any) -> str:
    """Extract a usable order id (``.id``, falling back to ``.client_order_id``)."""
    return str(getattr(order, "id", "") or getattr(order, "client_order_id", ""))


def extract_fill(order: Any) -> tuple[float, float] | None:
    """Return (filled_avg_price, filled_qty) iff both are positive, else None."""
    price = to_float(getattr(order, "filled_avg_price", None))
    qty = to_float(getattr(order, "filled_qty", None))
    if price is not None and price > 0 and qty is not None and qty > 0:
        return price, qty
    return None


def poll_until_filled(
    client: Any,
    order: Any,
    order_id: str,
    *,
    poll_timeout_s: float = POLL_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    logger: logging.Logger | None = None,
) -> tuple[float, float, str] | None:
    """Poll ``get_order_by_id`` until the order is TERMINAL, then report the fill.

    Returns ``(filled_avg_price, filled_qty, status)`` when the order reaches a
    terminal state with a positive fill (fully ``filled``, or
    ``canceled``/``expired``/``done_for_day`` carrying a partial), or ``None`` when
    the poll budget elapses with no fill recorded. Raises ``AlpacaSubmitError`` on a
    terminal REJECT with zero fill.

    P1-D: ``partially_filled`` is NON-terminal for a working DAY order — we do NOT
    return on the first partial snapshot (that under-accounts the position); we keep
    polling until the order is terminal.

    P1-C: on timeout the order is still WORKING at the broker (DAY TIF) — we CANCEL
    it then re-read once: if the cancel raced a fill we record it (terminal), else
    return None ONLY when the order is provably no longer working. An UNCONFIRMABLE
    settlement (cancel did not confirm AND the re-read raised) fails CLOSED — see
    ``cancel_and_settle``.
    """
    log = logger or logging.getLogger(__name__)
    deadline = time.monotonic() + poll_timeout_s

    current = order
    while True:
        status = str(getattr(current, "status", "") or "").lower()

        # Fully filled — terminal, record it.
        if status == "filled":
            fill = extract_fill(current)
            if fill is not None:
                return (*fill, status)
            # 'filled' but fields not yet populated — keep polling for them.
        # Terminal reject/close statuses: record a partial if one exists
        # (P3-B: done_for_day / canceled can carry a partial fill), else
        # surface the rejection (fail-closed, never fabricate).
        elif status in REJECT_STATUSES:
            fill = extract_fill(current)
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
            # orphan a position the book never recorded. Then re-read once.
            return cancel_and_settle(
                client, order_id, poll_interval_s=poll_interval_s, logger=log
            )

        time.sleep(poll_interval_s)
        try:
            current = client.get_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001 — poll error, surface it
            raise AlpacaSubmitError(
                f"get_order_by_id({order_id}) failed during poll: {exc}"
            ) from exc


def cancel_and_settle(
    client: Any,
    order_id: str,
    *,
    poll_interval_s: float = POLL_INTERVAL_S,
    logger: logging.Logger | None = None,
) -> tuple[float, float, str] | None:
    """Cancel a still-working order on timeout; record any realized partial.

    P1-C: a DAY order left working after our poll budget would fill at the broker
    and orphan an unrecorded position. We cancel it, then re-read:
      * if the order had (or raced into) a partial/full fill -> record it (terminal
        — the cancel only removes the UNfilled remainder);
      * if the order is provably no longer working (cancel confirmed) and the
        re-read shows no fill -> None (a clean unfilled; no working order remains).

    Settlement-UNKNOWN fails CLOSED (P1-C2): if the cancel was NOT confirmed (the
    ``cancel_order_by_id`` call raised) AND the post-cancel re-read ALSO raises, we
    cannot prove the order stopped working — it may still be working at the broker
    and may yet FILL, creating a real LIVE position. Returning None here would have
    the caller write a fill_size_pct=0.0 / ``unfilled_timeout`` no-fill that never
    reconciles state.db and is treated terminally by every consumer, orphaning that
    position PERMANENTLY. So we RAISE ``AlpacaSubmitError`` — byte-identical to the
    active-poll re-read (which raises on the SAME broker condition), surfacing the
    unknown rather than collapsing it into a clean no-fill. A re-read that raises
    after a CONFIRMED cancel still degrades to None (the order is provably gone).
    """
    log = logger or logging.getLogger(__name__)
    cancel_confirmed = True
    try:
        client.cancel_order_by_id(order_id)
    except Exception as exc:  # noqa: BLE001 — cancel best-effort; still re-read
        cancel_confirmed = False
        log.warning(
            "alpaca-exec: cancel_order_by_id(%s) failed on timeout: %s "
            "(re-reading to settle any partial)",
            order_id,
            exc,
        )
    # Give the cancel a moment to settle, then read the final state.
    time.sleep(poll_interval_s)
    try:
        final = client.get_order_by_id(order_id)
    except Exception as exc:  # noqa: BLE001
        if not cancel_confirmed:
            # Settlement UNKNOWN: cancel UNCONFIRMED + re-read raised. The order may
            # still be WORKING and may yet fill -> a 0.0 no-fill would orphan a real
            # LIVE position. Fail CLOSED (matches the active-poll re-read at L162).
            raise AlpacaSubmitError(
                f"alpaca order {order_id}: cancel was NOT confirmed and the "
                f"post-cancel re-read also failed ({exc}) — settlement UNKNOWN, the "
                "order may still be working; refusing to record a clean no-fill"
            ) from exc
        # Cancel CONFIRMED: the order is provably no longer working. A transient
        # re-read failure is safe to degrade to a clean unfilled (no orphan risk).
        log.warning(
            "alpaca-exec: post-cancel get_order_by_id(%s) failed after a CONFIRMED "
            "cancel: %s (order provably no longer working — clean unfilled)",
            order_id,
            exc,
        )
        return None
    fill = extract_fill(final)
    if fill is not None:
        status = str(getattr(final, "status", "") or "").lower() or "canceled"
        log.info(
            "alpaca-exec: order %s canceled on timeout but had a realized "
            "partial fill — recording it (status=%s)",
            order_id,
            status,
        )
        return (*fill, status)
    return None
