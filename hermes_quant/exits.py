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

    # FIX-1 (Codex round-2 P1): a SIZE-WEIGHTED BLENDED entry basis over the
    # symbol's opening legs — sum(fill_price_i * |fill_size_pct_i|) divided by
    # sum(|fill_size_pct_i|). FIX-A made the close QUANTITY cumulative but left
    # the price basis at the LATEST fill, so a position added at two prices
    # (+0.1@100, +0.1@200) was evaluated against 200 instead of the true blended
    # 150 — a mark of 170 read as -15% (false stop) instead of +13% (profit).
    # The basis must match the cumulative quantity it is closing. None => no
    # usable opening leg (fail-closed: the symbol is skipped, like a bad mark).
    blended_entry: float | None = None
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


def _fill_entry_basis(rec: dict) -> float | None:
    """The per-fill entry basis (FIX-B precedence): the actual ``fill_price``
    (positive, finite) the v0.2 slippage model filled at — which settlement
    realizes against — falling back to ``decision_price`` (the pre-slippage
    quote) only for older records that never recorded a fill_price. Returns None
    when neither yields a usable number (=> the leg cannot be weighted)."""
    fp = rec.get("fill_price")
    if fp is not None:
        try:
            fp_f = float(fp)
            if math.isfinite(fp_f) and fp_f > 0:
                return fp_f
        except (TypeError, ValueError):
            pass
    dp = rec.get("decision_price")
    if dp is not None:
        try:
            dp_f = float(dp)
            if math.isfinite(dp_f) and dp_f > 0:
                return dp_f
        except (TypeError, ValueError):
            return None
    return None


def _recover_position_meta(executions_path: Path) -> dict[str, _PosMeta]:
    """Walk executions.jsonl once and recover, per symbol:

      * blended_entry    : FIX-1 — the SIZE-WEIGHTED blended entry basis over ALL
                           opening legs (sum(basis_i * |fill_size_pct_i|) /
                           sum(|fill_size_pct_i|)). Each leg's basis is FIX-B's
                           precedence (positive fill_price first, decision_price
                           fallback). This matches the cumulative quantity the
                           close offsets — a position added at two prices is
                           evaluated against its true average cost, not the
                           latest add. None if any opening leg lacks a usable
                           price or the total weight is 0 (fail-closed => skip).
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
    # FIX-1 blended-basis accumulators over opening legs:
    #   _wsum = sum(basis_i * |fill_size_pct_i|), _wq = sum(|fill_size_pct_i|).
    # _poison marks a symbol whose blended basis is untrustworthy (an opening leg
    # with no usable price, or a leg whose |fill_size_pct| is unreadable) — such a
    # symbol is fail-closed to blended_entry=None (skipped, like a bad mark).
    _wsum: dict[str, float] = {}
    _wq: dict[str, float] = {}
    _poison: dict[str, bool] = {}
    # FIX-2 (Codex round-3 P1): the |net| below which a symbol is treated as FLAT
    # (a fully-closed round-trip). An exact offset (x + -x) is exactly 0.0 in
    # IEEE-754, so this only absorbs trivial accumulated float noise.
    flat_eps = 1e-9

    def _reset_symbol(asset: str, *, net: float, poison: bool) -> None:
        """Wipe a symbol's blended-basis + cumulative + carried entry state when
        it returns to flat (or flips sign) so a re-entry starts fresh. Mutates the
        enclosing accumulators in place (closure over _wsum/_wq/_poison/_entry_ts/
        meta). ``net`` is the residual signed position to carry (0.0 on a clean
        flat; the post-flip residual on a sign flip)."""
        _wsum[asset] = 0.0
        _wq[asset] = 0.0
        _poison[asset] = poison
        _entry_ts.pop(asset, None)
        mm = meta[asset]
        mm.cumulative_fill = net
        mm.play_tag = "advisor"
        mm.trader_stop_loss = None
        mm.asset_class = "equity"
        mm.timeframe = "1d"

    if not executions_path.exists():
        return meta
    try:
        text = executions_path.read_text(encoding="utf-8")
    except OSError:
        return meta

    # Collect the paper fills, then REPLAY them in ascending asof_execution order.
    # The file is normally append-ordered already; a STABLE sort by asof_execution
    # preserves file order on ties (matching the existing "latest fill on a tie =
    # last in file" semantics the `ts >=` checks below rely on) and defends against
    # any out-of-order append — which the round-trip reset (FIX-2) depends on to
    # see a close BEFORE the re-entry it precedes. Fail-soft: malformed / non-paper
    # / keyless / non-string-ts lines are skipped (one bad append must not crash
    # the whole recovery — a non-string ts would also break the `ts >=` compares).
    records: list[dict] = []
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
        if rec.get("asset") is None or not isinstance(rec.get("asof_execution"), str):
            continue
        records.append(rec)
    records.sort(key=lambda r: r["asof_execution"])  # stable: ties keep file order

    for rec in records:
        asset = rec["asset"]
        ts = rec["asof_execution"]
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
        # close must use, not the latest target snapshot. FIX-2 reads the running
        # NET off this accumulator (prev_net -> new_net) to detect a flat/flip.
        prev_net = m.cumulative_fill or 0.0
        fsp = rec.get("fill_size_pct")
        fsp_f: float | None = None
        if fsp is not None:
            try:
                fsp_f = float(fsp)
            except (TypeError, ValueError):
                fsp_f = None
        if fsp_f is not None:
            m.cumulative_fill = prev_net + fsp_f
        new_net = m.cumulative_fill or 0.0

        # entry / play_tag / stop: latest NON-ZERO-target fill (an opening leg)
        target = rec.get("target_position_pct")
        try:
            is_open_leg = target is not None and float(target) != 0.0
        except (TypeError, ValueError):
            is_open_leg = False
        if is_open_leg:
            # FIX-1: blend EVERY opening leg into the size-weighted basis (NOT just
            # the latest). Closing legs (target == 0) are excluded — their
            # fill_price is the EXIT mark, which would corrupt the entry basis.
            # The weight is |fill_size_pct| so the blend matches the cumulative
            # quantity; the sign comes from cumulative_fill at evaluation time.
            leg_basis = _fill_entry_basis(rec)
            w = abs(fsp_f) if fsp_f is not None else 0.0
            if leg_basis is None or not math.isfinite(w) or w <= 0:
                # An opening leg with no usable price (or no usable size) makes the
                # whole blend untrustworthy => fail-closed: skip this symbol.
                _poison[asset] = True
            else:
                _wsum[asset] = _wsum.get(asset, 0.0) + leg_basis * w
                _wq[asset] = _wq.get(asset, 0.0) + w

            if ts >= _entry_ts.get(asset, ""):
                _entry_ts[asset] = ts
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

        # FIX-2 (Codex round-3 P1): RESET the accumulators on a return-to-FLAT or a
        # sign FLIP. FIX-1/FIX-A accrue monotonically over the WHOLE file with no
        # notion of a position having closed, so a CLOSED round-trip's opening legs
        # stayed blended into a later re-entry's basis (and its fills stayed in
        # cumulative_fill) — the basis reflected dead lots the settlement FIFO
        # matcher no longer holds open. Evaluated AFTER this fill's contribution so
        # the transition is read off (prev_net -> new_net).
        if abs(prev_net) > flat_eps and abs(new_net) <= flat_eps:
            # FLAT: a position that HELD a non-zero net just fully closed. Wipe
            # basis + cumulative + carried entry state so any later opening leg
            # rebuilds for the NEW position only (cumulative snaps to a clean 0.0).
            # Gated on prev_net != 0 to match "RETURNS to ~0" — a first fill that
            # merely lands at 0 (a zero-size leg) never held a position to reset
            # (and would build no usable basis anyway). The common never-closed
            # case never reaches |net|<=eps, so its behavior is UNCHANGED.
            _reset_symbol(asset, net=0.0, poison=False)
        elif (prev_net > flat_eps and new_net < -flat_eps) or (
            prev_net < -flat_eps and new_net > flat_eps
        ):
            # OVER-CLOSE that flips the sign in one fill: the reversing fill is part
            # exit, part entry, so the residual position's cost basis is ambiguous.
            # SAFE simplification (spec): reset the dead side's accumulators and
            # fail-closed POISON the residual (=> blended_entry=None => skipped),
            # never auto-trade on a guessed basis. A later clean round-trip to flat
            # clears the poison.
            _reset_symbol(asset, net=new_net, poison=True)

    # Finalize the blended basis. Fail-closed: a poisoned symbol, a zero total
    # weight, or a non-finite/<=0 blend all leave blended_entry=None (the
    # per-symbol loop then skips them exactly like a bad mark).
    for asset, m in meta.items():
        if _poison.get(asset):
            m.blended_entry = None
            continue
        wq = _wq.get(asset, 0.0)
        if wq <= 0:
            m.blended_entry = None
            continue
        blended = _wsum.get(asset, 0.0) / wq
        m.blended_entry = blended if (math.isfinite(blended) and blended > 0) else None
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
        # decision_price sentinel for gated-anyway proposals). FIX-1: this is the
        # SIZE-WEIGHTED blended basis over all opening legs — a position added at
        # two prices is evaluated against its true average cost, matching the
        # cumulative quantity we close. None => fail-closed skip (same as a bad
        # mark): a poisoned/zero-weight/non-finite blend never fabricates a breach.
        entry = m.blended_entry
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
