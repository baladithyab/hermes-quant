"""hermes_quant.exits — autonomous position-management (loss-cut / take-profit) pass.

This is a SEPARATE function from `autonomous.tick()` by deliberate design
(ULTRACODE-REVIEW Q2, the blocking finding): entries and exits have OPPOSITE
desired behavior under a tripped kill-switch. The kill-switch trips precisely
when cumulative realized P&L has breached ``-kill_switch_pct`` — i.e. exactly
when the book is bleeding and you most want stop-losses to fire. ``tick()``
early-returns on a tripped switch (autonomous.py:427, 465) BEFORE any symbol
work, so any exit logic placed inside it would freeze losers open. Therefore:

    manage_open_positions() is callable independently and NEVER reads the
    kill-switch. Entries halt under a tripped switch; exits must still fire.

Master flag: ``quant.autonomous.manage_positions`` (default False => byte-
identical no-op: read nothing, append nothing). Thresholds live on
``quant.autonomous``:

    stop_loss_pct        0.10   loss band that triggers a close
    take_profit_pct      None   gain band (None = take-profit OFF)
    anomaly_breaker_pct  0.50   >this fraction of the book breaching => feed event
    max_exits_per_tick   3      rate limit; exit the N worst, alert the rest
    mark_jump_max        0.25   per-symbol finite-but-wrong mark guard

Per-position override: an open fill whose ``reactor_metadata.trader_stop_loss``
carries a PRICE overrides the default pct band when the mark has crossed it.

Fail-closed rails (all mandatory — every one prevents a mass-liquidation mode):
  * market-clock gate: unknown / error => CLOSED => exit nothing.
  * valid-mark gate (NaN-safe): ``mark is None or not isfinite(mark) or mark<=0``.
  * per-symbol sanity clamp: ``|mark/last_bus_price - 1| > mark_jump_max`` => skip.
  * cross-sectional anomaly breaker: a whole book going red in one snapshot is a
    DATA fault, not a market move => alert, exit nothing.
  * exit leg replicates the FillSizeInvariant finiteness check it bypasses by
    NOT going through ``PaperReactor.execute()`` (execute hardcodes
    ``target_position_pct=fill_size_pct`` at paper.py:266 and so can never emit a
    flat record); ``|held| > HARD_FILL_CEILING`` => ALERT, never silent skip.

The exit mechanism is the PROVEN direct-append-with-``target_position_pct=0.0``
pattern from ops/scripts/quant-flatten-paper-default.py:107-137.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hermes_quant.daemon.signal_bus import append_locked
from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import (
    HARD_FILL_CEILING,
    FillSizeInvariantError,
    _enforce_fill_size_invariant,
    _record_to_dict,
)

logger = logging.getLogger(__name__)

QUANT_HOME = Path.home() / ".hermes" / "quant"

# The anomaly breaker requires BOTH a fraction breach AND a minimum absolute
# count, so a 2-name book where both names legitimately gap down is not
# misclassified as a feed fault (review Q3 "N >= some floor").
ANOMALY_BREAKER_MIN_COUNT = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ExitResult:
    """Structured outcome of one exit pass (review §Component-1 step 9).

    exited_symbols   : positions actually closed this pass (empty on dry_run).
    would_exit       : positions that WOULD close (dry_run report; also the
                       cap-selected set before any append).
    skipped_bad_mark : positions skipped by the valid-mark gate / sanity clamp /
                       bad entry — never acted on.
    anomaly_tripped  : the cross-sectional breaker fired => exited NOTHING.
    alerts           : human-readable lines for the rate-limited, unrepresentable
                       (|held|>ceiling), and anomaly-breaker cases.
    """

    exited_symbols: list[str] = field(default_factory=list)
    would_exit: list[str] = field(default_factory=list)
    skipped_bad_mark: list[str] = field(default_factory=list)
    anomaly_tripped: bool = False
    alerts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "exited_symbols": self.exited_symbols,
            "would_exit": self.would_exit,
            "skipped_bad_mark": self.skipped_bad_mark,
            "anomaly_tripped": self.anomaly_tripped,
            "alerts": self.alerts,
        }


@dataclass
class _PosMeta:
    """Per-symbol data recovered by walking executions.jsonl."""

    entry_price: float | None = None
    last_bus_price: float | None = None
    play_tag: str = "advisor"
    trader_stop_loss: float | None = None
    asset_class: str = "equity"
    timeframe: str = "1d"
    # FIX-A (Codex P1): the TRUE cumulative open quantity = sum of signed
    # fill_size_pct across this symbol's paper fills. The reconstruct view is
    # latest-target-supersedes (every add stamps target=fill, so two +0.1 adds
    # read as held=0.1), but settlement FIFO sums the signed fills (=+0.2). The
    # close must offset the CUMULATIVE, not the latest target snapshot, or it
    # leaves a residual lot hidden from settlement + future reconstruct passes.
    # None => no paper fill seen for the symbol (defensive; falls back to held).
    cumulative_fill: float | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _read_autonomous_config() -> dict:
    """Read ``quant.autonomous`` from the active (profile-aware) config.yaml.

    Routed through watchlist.get_config_path so the same monkeypatch the rest
    of the suite uses isolates this read too.
    """
    from hermes_quant.watchlist import get_config_path

    path = get_config_path()
    if not path.exists():
        return {}
    try:
        import yaml

        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — fail-soft: no config => no-op
        logger.warning("exits: config read failed: %s", exc)
        return {}
    return (cfg.get("quant") or {}).get("autonomous") or {}


def _exit_thresholds(auto: dict) -> dict:
    take_raw = auto.get("take_profit_pct", None)
    return {
        "manage_positions": bool(auto.get("manage_positions", False)),
        "stop_loss_pct": float(auto.get("stop_loss_pct", 0.10)),
        "take_profit_pct": (None if take_raw is None else float(take_raw)),
        "anomaly_breaker_pct": float(auto.get("anomaly_breaker_pct", 0.50)),
        "max_exits_per_tick": int(auto.get("max_exits_per_tick", 3)),
        "mark_jump_max": float(auto.get("mark_jump_max", 0.25)),
    }


# ---------------------------------------------------------------------------
# Default providers (live path) — ALWAYS fail-closed
# ---------------------------------------------------------------------------


def _default_clock_provider() -> bool | None:
    """Live market-open check via the Alpaca trading clock.

    Returns True/False on a clean read, None on ANY failure (missing creds,
    network, import) so the caller treats unknown as CLOSED. Never raises.
    """
    try:
        from hermes_quant.universe.alpaca_scanner import (  # type: ignore
            _build_trading_client,
            _get_credentials,
        )

        key, secret = _get_credentials()
        client = _build_trading_client(key, secret)
        clock = client.get_clock()
        return bool(getattr(clock, "is_open"))
    except Exception as exc:  # noqa: BLE001 — fail-closed: unknown => None => CLOSED
        logger.warning("exits: live clock read failed (fail-closed CLOSED): %s", exc)
        return None


def _default_marks_provider(symbols: list[str]) -> dict[str, float]:
    """Live last-trade marks via Alpaca. Returns {} on any failure (=> every
    symbol skipped by the valid-mark gate). Never raises, never returns the bus
    decision_price (that is stale entry-price data, not a live mark)."""
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        from hermes_quant.universe.alpaca_scanner import _get_credentials  # type: ignore

        key, secret = _get_credentials()
        client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        req = StockLatestTradeRequest(symbol_or_symbols=list(symbols))
        trades = client.get_stock_latest_trade(req)
        out: dict[str, float] = {}
        for sym, trade in (trades or {}).items():
            price = getattr(trade, "price", None)
            if price is not None:
                out[sym] = float(price)
        return out
    except Exception as exc:  # noqa: BLE001 — fail-closed: no marks => skip all
        logger.warning("exits: live marks fetch failed (skip all): %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Bus recovery
# ---------------------------------------------------------------------------


def _recover_position_meta(executions_path: Path) -> dict[str, _PosMeta]:
    """Walk executions.jsonl once and recover, per symbol:

      * entry_price      : latest NON-ZERO-target fill's entry (pnl basis). FIX-B:
                           the actual fill_price (positive) wins; decision_price is
                           the fallback only for older records lacking a fill_price.
      * play_tag         : latest non-zero-target fill's play_tag (attribution)
      * trader_stop_loss : that fill's reactor_metadata.trader_stop_loss (price)
      * last_bus_price   : latest fill's decision_price overall (sanity-clamp basis)
      * cumulative_fill  : FIX-A — sum of signed fill_size_pct across ALL paper
                           fills (the settlement-FIFO basis the close must offset)
      * asset_class/timeframe : carried onto the close record

    Fail-soft: a malformed line is skipped (one bad append must not black-hole
    the whole recovery).
    """
    meta: dict[str, _PosMeta] = {}
    # symbol -> ts of the latest non-zero-target fill we've recorded entry for
    _entry_ts: dict[str, str] = {}
    _bus_ts: dict[str, str] = {}

    if not executions_path.exists():
        return meta
    try:
        text = executions_path.read_text(encoding="utf-8")
    except OSError:
        return meta

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("reactor_name") != "paper":
            continue
        asset = rec.get("asset")
        ts = rec.get("asof_execution")
        if asset is None or ts is None:
            continue
        m = meta.setdefault(asset, _PosMeta())

        dp = rec.get("decision_price")
        # last_bus_price: latest fill of ANY kind (matches flatten's latest_bus_prices)
        if dp is not None and ts >= _bus_ts.get(asset, ""):
            try:
                m.last_bus_price = float(dp)
                _bus_ts[asset] = ts
            except (TypeError, ValueError):
                pass

        # FIX-A (Codex P1): accumulate the SIGNED fill for EVERY paper fill (open
        # legs, adds, and prior partial closes) so cumulative_fill == the net open
        # quantity the settlement FIFO matcher tracks. This is the offset the
        # close must use, not the latest target snapshot.
        fsp = rec.get("fill_size_pct")
        if fsp is not None:
            try:
                m.cumulative_fill = (m.cumulative_fill or 0.0) + float(fsp)
            except (TypeError, ValueError):
                pass

        # entry / play_tag / stop: latest NON-ZERO-target fill (an opening leg)
        target = rec.get("target_position_pct")
        try:
            is_open_leg = target is not None and float(target) != 0.0
        except (TypeError, ValueError):
            is_open_leg = False
        if is_open_leg and ts >= _entry_ts.get(asset, ""):
            _entry_ts[asset] = ts
            # FIX-B (Codex P2): the threshold/pnl basis is the ACTUAL entry the
            # v0.2 slippage model filled at and settlement realizes against —
            # i.e. fill_price. decision_price (the pre-slippage quote) is only a
            # fallback for older records that never recorded a fill_price.
            entry_basis: float | None = None
            fp = rec.get("fill_price")
            if fp is not None:
                try:
                    fp_f = float(fp)
                    if math.isfinite(fp_f) and fp_f > 0:
                        entry_basis = fp_f
                except (TypeError, ValueError):
                    entry_basis = None
            if entry_basis is None and dp is not None:
                try:
                    entry_basis = float(dp)
                except (TypeError, ValueError):
                    entry_basis = None
            m.entry_price = entry_basis
            m.play_tag = str(rec.get("play_tag") or "advisor")
            m.asset_class = str(rec.get("asset_class") or "equity")
            m.timeframe = str(rec.get("timeframe") or "1d")
            rmeta = rec.get("reactor_metadata") or {}
            if isinstance(rmeta, dict):
                stop = rmeta.get("trader_stop_loss")
                try:
                    m.trader_stop_loss = (
                        None if stop is None else float(stop)
                    )
                except (TypeError, ValueError):
                    m.trader_stop_loss = None
    return meta


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def manage_open_positions(
    *,
    dry_run: bool = True,
    marks_provider: Callable[[list[str]], dict[str, float]] | None = None,
    clock_provider: Callable[[], bool | None] | None = None,
    quant_home: Path = QUANT_HOME,
) -> ExitResult:
    """Run one autonomous exit pass over the open paper book.

    Args:
        dry_run: when True, compute + report (``would_exit``) but append NOTHING.
        marks_provider: ``(symbols) -> {symbol: live_price}``. Default = live
            Alpaca last-trade, fail-closed. NEVER the bus decision_price (stale).
        clock_provider: ``() -> is_open``. Default = live Alpaca clock,
            fail-closed (unknown/error => CLOSED => exit nothing).
        quant_home: the ~/.hermes/quant home; ``quant_home/"executions.jsonl"``
            is passed explicitly to every bus reader/writer (test-isolation).

    Returns:
        ExitResult. Flag-OFF => empty result, bus byte-identical.
    """
    result = ExitResult()

    auto = _read_autonomous_config()
    cfg = _exit_thresholds(auto)

    # MASTER FLAG. Default OFF => byte-identical no-op: read nothing, append
    # nothing (do not even touch the bus or the clock).
    if not cfg["manage_positions"]:
        return result

    if marks_provider is None:
        marks_provider = _default_marks_provider
    if clock_provider is None:
        clock_provider = _default_clock_provider

    # MARKET-CLOCK GATE (fail-closed). Unknown / error / not-exactly-True => the
    # market is treated as CLOSED and we exit nothing. An after-hours / holiday
    # exit on a stale mark is a (future) live-money hazard (review Q3/Q7).
    try:
        is_open = clock_provider()
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning("exits: clock provider raised (fail-closed CLOSED): %s", exc)
        is_open = None
    if is_open is not True:
        logger.info("exits: market not open (is_open=%r) — exiting nothing", is_open)
        return result

    executions_path = quant_home / "executions.jsonl"

    # Authoritative open book (NEVER state.db). reconstruct returns
    # {symbol: target_pct} for non-zero (open) positions only.
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    open_book = reconstruct_portfolio_state(
        executions_path, reactor_filter="paper"
    ).positions
    if not open_book:
        return result

    symbols = sorted(open_book)
    meta = _recover_position_meta(executions_path)

    # Live marks (injected dict in tests).
    try:
        marks = marks_provider(symbols) or {}
    except Exception as exc:  # noqa: BLE001 — a marks failure => skip all, never crash
        logger.warning("exits: marks provider raised — skipping all: %s", exc)
        marks = {}

    stop = abs(cfg["stop_loss_pct"])
    take = cfg["take_profit_pct"]
    mark_jump_max = cfg["mark_jump_max"]

    # ----- Per-symbol classification -----
    # A "breach" is a candidate exit (stop / take / per-position-stop cross).
    # The third tuple element is the TRUE cumulative held quantity (FIX-A), the
    # offset the close leg must use — not the reconstruct latest-target snapshot.
    breaches: list[tuple[str, float, float]] = []  # (symbol, pnl_pct, true_held)
    valid_marked = 0

    for sym in symbols:
        held = open_book[sym]
        m = meta.get(sym, _PosMeta())
        # FIX-A (Codex P1): close the cumulative signed fill, not the latest
        # target snapshot. reconstruct's `held` is latest-target-supersedes; the
        # settlement FIFO matcher nets cumulative fill_size_pct. Offsetting only
        # the latest target leaves a residual lot hidden from settlement + future
        # exit passes. Fall back to `held` only if the cumulative is somehow
        # unrecoverable (defensive; the symbol IS in the reconstruct open book).
        true_held = m.cumulative_fill if m.cumulative_fill is not None else held
        mark = marks.get(sym)

        # VALID-MARK GATE (NaN-safe, mandatory exact form). NaN <= 0 is False, so
        # an isfinite() test is required — never simplify to ``mark <= 0``.
        if mark is None or not math.isfinite(mark) or mark <= 0:
            result.skipped_bad_mark.append(sym)
            continue

        # Entry must be usable, else pnl is undefined (0.0 entry is the
        # decision_price sentinel for gated-anyway proposals).
        entry = m.entry_price
        if entry is None or not math.isfinite(entry) or entry <= 0:
            result.skipped_bad_mark.append(sym)
            continue

        # PER-SYMBOL SANITY CLAMP: a finite, positive mark that jumped more than
        # mark_jump_max from the last known bus price is a finite-but-wrong feed
        # glitch (stale pre-split cache, bid/ask inversion) => skip.
        clamp_basis = m.last_bus_price
        if clamp_basis is None or not math.isfinite(clamp_basis) or clamp_basis <= 0:
            clamp_basis = entry
        if abs(mark / clamp_basis - 1.0) > mark_jump_max:
            result.skipped_bad_mark.append(sym)
            continue

        valid_marked += 1

        # Direction comes from the TRUE cumulative position (FIX-A): a sequence
        # of fills can net long while the latest target snapshot's sign differs,
        # so the pnl sign must follow the quantity we will actually flatten.
        qty_sign = 1.0 if true_held >= 0 else -1.0
        pnl_pct = qty_sign * (mark / entry - 1.0)

        # Per-position stop-loss override (a PRICE): fires on a cross even when
        # the default pct band is not breached.
        stop_price_cross = False
        if m.trader_stop_loss is not None and math.isfinite(m.trader_stop_loss):
            if true_held >= 0:
                stop_price_cross = mark <= m.trader_stop_loss
            else:
                stop_price_cross = mark >= m.trader_stop_loss

        hit_stop = pnl_pct <= -stop or stop_price_cross
        hit_take = take is not None and pnl_pct >= abs(take)
        if hit_stop or hit_take:
            breaches.append((sym, pnl_pct, true_held))

    # ----- CROSS-SECTIONAL ANOMALY BREAKER -----
    # The per-symbol rails are all independent; that independence is exactly the
    # vulnerability a correlated feed fault exploits (review Q3). If more than
    # anomaly_breaker_pct of the MARKED book breaches at once AND the count
    # clears the floor, it is a DATA fault, not a market move: alert, exit
    # NOTHING. (max_exits_per_tick only rate-limits; this prevents the
    # cumulative full-book liquidation over multiple ticks.)
    breach_count = len(breaches)
    if valid_marked > 0:
        breach_fraction = breach_count / valid_marked
        if (
            breach_fraction > cfg["anomaly_breaker_pct"]
            and breach_count >= ANOMALY_BREAKER_MIN_COUNT
        ):
            result.anomaly_tripped = True
            result.alerts.append(
                f"ANOMALY BREAKER: {breach_count}/{valid_marked} "
                f"({breach_fraction:.0%}) of the marked book breached in one "
                f"snapshot — treating as a FEED EVENT, exiting NOTHING. "
                f"Symbols: {', '.join(s for s, _, _ in breaches)}"
            )
            logger.warning("exits: %s", result.alerts[-1])
            return result

    if not breaches:
        return result

    # ----- RATE LIMIT: exit the N worst, alert the rest -----
    # Worst = most negative pnl_pct first (stops rank ahead of take-profits).
    breaches.sort(key=lambda t: t[1])
    cap = cfg["max_exits_per_tick"]
    to_exit = breaches[:cap]
    rate_limited = breaches[cap:]
    for sym, pnl_pct, _held in rate_limited:
        result.alerts.append(
            f"RATE LIMIT: {sym} breached ({pnl_pct:+.2%}) but max_exits_per_tick="
            f"{cap} reached this tick — alerted, not exited."
        )

    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for sym, pnl_pct, held in to_exit:
        offset = -float(held)  # offsetting signed size realizes the P&L

        # REPLICATE the FillSizeInvariant finiteness check that the direct-append
        # path bypasses by NOT going through execute(). |held| > HARD_FILL_CEILING
        # is a position the system cannot represent — and one it cannot represent
        # is one it cannot auto-close. That needs an ALERT, never a silent skip.
        try:
            _enforce_fill_size_invariant(None, offset)
        except FillSizeInvariantError as exc:
            result.alerts.append(
                f"UNREPRESENTABLE: {sym} held={held:+.4f} cannot be auto-closed "
                f"(|fill|>{HARD_FILL_CEILING}) — {exc}. NOT exited; manual flatten "
                f"required."
            )
            logger.warning("exits: %s", result.alerts[-1])
            continue

        if dry_run:
            result.would_exit.append(sym)
            continue

        m = meta.get(sym, _PosMeta())
        mark = float(marks[sym])
        record = ExecutionRecord(
            proposal_id=f"prop_{now}_{sym}_AUTOEXIT",
            signal_id=None,
            asset=sym,
            asset_class=m.asset_class,
            timeframe=m.timeframe,
            asof_decision=now,
            asof_execution=now,
            target_position_pct=0.0,  # <-- closes it in the reconstruct/cap view
            decision_price=mark,
            fill_price=mark,  # paper: fill_price = the live exit mark
            fill_size_pct=offset,  # <-- offsetting leg realizes the P&L
            reactor_name="paper",
            human_in_the_loop=False,
            approver_user_id="autonomous-exit",
            reactor_metadata={
                "paper": True,
                "autonomous_exit": True,
                "exit_reason": ("take_profit" if pnl_pct > 0 else "stop_loss"),
                "exit_pnl_pct": pnl_pct,
            },
            bar_ts=now,
            play_tag=m.play_tag,  # recovered originating tag (clean attribution)
        )
        line = (
            json.dumps(_record_to_dict(record), separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        with append_locked(executions_path) as fd:
            os.write(fd, line.encode("utf-8"))
        result.exited_symbols.append(sym)
        logger.info(
            "exits: CLOSED %s held=%+.4f pnl=%+.4f at mark=%.4f (play_tag=%s)",
            sym,
            held,
            pnl_pct,
            mark,
            m.play_tag,
        )

    # FIX-C (Codex P2): the direct append bypasses PaperReactor.execute(), which
    # is what normally rebuilds state.db incrementally. Without this, after a stop
    # closes a symbol the bus is flat but state.db (+ derived cash / NAV) still
    # shows the stale open => the NAV kill-switch, status, sizing, and
    # admissibility all run on stale state. Rebuild state.db from the bus — the
    # SAME reconstruct_from() call ops/scripts/quant-flatten-paper-default.py uses
    # (@149-154) — so the source-of-truth bus and the derived cache agree.
    #
    # Guarded: only on a REAL exit (dry-run appended nothing, so nothing to
    # reconcile), and NEVER let a reconcile failure crash the exit — the bus is
    # the source of truth and the close already landed there; a stale cache is
    # recoverable, a crashed exit is not (log + continue).
    if not dry_run and result.exited_symbols:
        try:
            from hermes_quant.state.portfolio_state import PortfolioState

            state_db = quant_home / "state.db"
            res = PortfolioState(state_db_path=state_db).reconstruct_from(
                executions_path
            )
            logger.info(
                "exits: state.db reconciled after %d exit(s) "
                "(processed=%d accounts=%s errors=%d)",
                len(result.exited_symbols),
                res.executions_processed,
                sorted(res.accounts_seen),
                len(res.errors),
            )
        except Exception as exc:  # noqa: BLE001 — bus is source of truth; never crash
            result.alerts.append(
                f"STATE.DB RECONCILE FAILED after exit ({exc}); bus is flat and is "
                f"the source of truth — run quant-flatten-paper-default to rebuild "
                f"the cache."
            )
            logger.warning("exits: %s", result.alerts[-1])

    return result
