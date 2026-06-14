#!/usr/bin/env python3
"""quant-playbook-weekly.py — Weekly rebalance / position-management cron.

Schedule: Mondays 06:30 PT (= 09:30 ET, 30 min after the open).
  Cron: '30 6 * * 1'  (deliver=local; silent unless something fires)
  Per ADR-0035 wave 3.

Responsibilities (equity-focused; options rolling deferred to ADR-0029):
  1. Reconstruct the portfolio from ~/.hermes/quant/executions.jsonl (uses
     hermes_quant.daemon.portfolio_loader.reconstruct_portfolio).
  2. Determine `play_tag` for each open position. Position objects don't
     carry that field today, so we infer from the most-recent opening
     execution's `signal_id` (e.g. "sig-swing-AAPL-...") or the optional
     `play_tag` / `recipe` fields if a future writer adds them. Fallback:
     'swing' (the cautious default — gives every equity the swing exit
     rules per ADR-0035 §90-101).
  3. For each EQUITY position with play_tag in {'swing'}:
        - days_held > 60 AND pnl_pct < 0      -> CLOSE (stop-out)
        - pnl_pct > 3 * ATR-14_at_entry      -> CLOSE (take-profit)
        - else                                -> HOLD
  4. For LEAPS-tagged equity positions (proxy: deep-ITM equity exposure
     since the options reactor isn't landed yet):
        - revenue_growth_yoy < 0.05          -> CLOSE (thesis broken)
        - debt_to_equity > 2.0               -> CLOSE (balance-sheet risk)
        - drawdown_from_entry > 0.25         -> CLOSE (-25% rule)
        - else                                -> HOLD
  5. Options positions: log a TODO and skip. (ADR-0029 reactor lands later.)
  6. Idempotency: a (symbol, play, monday_iso) entry in
     ~/.hermes/quant/playbook/weekly-journal.jsonl with action != 'noop'
     blocks a second fire that same Monday.
  7. Halt-state guard: abort silently if any halt is active.
  8. DRY-RUN by default. --armed flips the switch.

Decisions (HOLD/CLOSE/skip) and any orders are appended to
  ~/.hermes/quant/playbook/weekly-journal.jsonl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Silence noisy third-party loggers.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("yfinance", "peewee", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

logger = logging.getLogger("quant-playbook-weekly")

# ---------- paths ----------
HERMES_HOME = Path.home() / ".hermes"
QUANT_HOME = HERMES_HOME / "quant"
EXECUTIONS_PATH = QUANT_HOME / "executions.jsonl"
HALT_MIRROR_PATH = QUANT_HOME / "halt_state.json"
PLAYBOOK_DIR = QUANT_HOME / "playbook"
WEEKLY_JOURNAL_PATH = PLAYBOOK_DIR / "weekly-journal.jsonl"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ---------- exit thresholds (ADR-0035) ----------
SWING_MAX_DAYS_LOSING = 60
SWING_TP_ATR_MULT = 3.0
LEAPS_MIN_REV_GROWTH = 0.05
LEAPS_MAX_DEBT_TO_EQUITY = 2.0
LEAPS_MAX_DRAWDOWN = 0.25


# ---------- utilities ----------
def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_et_date() -> str:
    return datetime.now(UTC).astimezone(ET).strftime("%Y-%m-%d")


def monday_iso_et() -> str:
    """ISO date of the Monday-in-ET that owns this run.

    For the cron's intended trigger (Mon 09:30 ET) this is just today's date.
    For an ad-hoc rerun on Tuesday/etc., we still bucket under *this week's*
    Monday so idempotency works as a "one fire per week per (symbol, play)".
    """
    now_et = datetime.now(UTC).astimezone(ET).date()
    # weekday(): Mon=0
    return (now_et.fromordinal(now_et.toordinal() - now_et.weekday())).isoformat()


def append_journal(record: dict[str, Any]) -> None:
    """Append-only JSONL. Never raises."""
    record.setdefault("ts", utcnow_iso())
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(WEEKLY_JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        sys.stderr.write(f"weekly journal write failed: {e}\n")


# ---------- halt-state fail-closed gate ----------
def read_active_halts() -> list[dict]:
    """Return halts that are relevant to EQUITY operations.

    Wave 1d fix (2026-05-27): the original version returned ALL halts,
    including crypto-only halts (e.g. BTC/USDT daily_loss_breaker). A
    crypto halt should not abort the equity weekly rebalance. We now
    filter to halts whose asset_class is '*', 'equity', or None (account-
    wide / unspecified). Crypto-only halts are ignored here.
    """
    if not HALT_MIRROR_PATH.exists():
        return []
    try:
        data = json.loads(HALT_MIRROR_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [{"reason": f"halt_state.json corrupt: {e}", "scope": "fail-closed"}]
    if not isinstance(data, list):
        return []
    equity_halts = []
    for h in data:
        ac = h.get("asset_class")
        # Only halt equity playbook for account-wide ('*', None, or 'equity') halts.
        # Crypto-only halts (asset_class='crypto') do not affect equity operations.
        if ac in (None, "*", "equity"):
            equity_halts.append(h)
    return equity_halts


# ---------- exit-rule logic (pure, unit-tested) ----------
@dataclass(frozen=True)
class SwingContext:
    days_held: int
    pnl_pct: float            # decimal: 0.05 = +5%
    atr14_at_entry_pct: float # decimal: 0.02 = 2% ATR/price


@dataclass(frozen=True)
class LeapsContext:
    revenue_growth_yoy: float | None  # decimal
    debt_to_equity: float | None
    drawdown_from_entry: float        # decimal, positive number = drawdown


@dataclass(frozen=True)
class ExitDecision:
    action: str    # "CLOSE" or "HOLD"
    reason: str    # human-readable rule label


def decide_swing(ctx: SwingContext) -> ExitDecision:
    """Apply ADR-0035 §96-98 swing exit rules. Pure function."""
    if ctx.days_held > SWING_MAX_DAYS_LOSING and ctx.pnl_pct < 0:
        return ExitDecision(
            "CLOSE",
            f"swing_stop: days_held={ctx.days_held}>{SWING_MAX_DAYS_LOSING} and pnl_pct={ctx.pnl_pct:.4f}<0",
        )
    tp_threshold = SWING_TP_ATR_MULT * ctx.atr14_at_entry_pct
    if ctx.pnl_pct > tp_threshold and tp_threshold > 0:
        return ExitDecision(
            "CLOSE",
            f"swing_tp: pnl_pct={ctx.pnl_pct:.4f}>{SWING_TP_ATR_MULT}*ATR={tp_threshold:.4f}",
        )
    return ExitDecision("HOLD", "swing_hold")


def decide_leaps(ctx: LeapsContext) -> ExitDecision:
    """Apply ADR-0035 LEAPS thesis-check rules. Pure function.

    Missing fundamentals (None) are treated as 'thesis still intact for that
    factor' — we do NOT close on missing data alone (the data-fetch layer
    will fall over before this if the symbol is delisted etc.). Drawdown is
    always available because it's just price vs. entry.
    """
    if ctx.revenue_growth_yoy is not None and ctx.revenue_growth_yoy < LEAPS_MIN_REV_GROWTH:
        return ExitDecision(
            "CLOSE",
            f"leaps_revgrowth: yoy={ctx.revenue_growth_yoy:.4f}<{LEAPS_MIN_REV_GROWTH}",
        )
    if ctx.debt_to_equity is not None and ctx.debt_to_equity > LEAPS_MAX_DEBT_TO_EQUITY:
        return ExitDecision(
            "CLOSE",
            f"leaps_de: d/e={ctx.debt_to_equity:.4f}>{LEAPS_MAX_DEBT_TO_EQUITY}",
        )
    if ctx.drawdown_from_entry > LEAPS_MAX_DRAWDOWN:
        return ExitDecision(
            "CLOSE",
            f"leaps_drawdown: dd={ctx.drawdown_from_entry:.4f}>{LEAPS_MAX_DRAWDOWN}",
        )
    return ExitDecision("HOLD", "leaps_hold")


# ---------- sign-aware P&L / drawdown (cs20) ----------
def compute_pnl_drawdown(avg_entry: float, mark: float, qty: float) -> tuple[float, float]:
    """Return (pnl_pct, drawdown_from_entry) honoring the position sign.

    The live book is short-dominated (cs14 emits Position.qty<0 for the -0.2 NAV
    targets), so a long-only P&L is actively wrong on it. Both metrics drive the
    downstream exit rules:
      * pnl_pct  -> swing >60d loss-stop (pnl_pct<0) and the 3*ATR take-profit
      * drawdown -> the LEAPS -25% close

    For a LONG (qty >= 0) we keep the original formula byte-identical:
        pnl_pct  = (mark - avg_entry) / avg_entry
        drawdown = max(0, (avg_entry - mark) / avg_entry)   # adverse = price fell

    For a SHORT (qty < 0) a position profits as the mark falls below entry, so we
    flip both: the short's unrealized profit rises when mark < avg_entry (matching
    portfolio_loader.unrealized = (mark-avg_entry)*qty, which is >0 for qty<0 when
    mark<avg_entry), and the adverse drawdown is the mark rising above entry:
        pnl_pct  = (avg_entry - mark) / avg_entry
        drawdown = max(0, (mark - avg_entry) / avg_entry)   # adverse = price rose

    A non-positive avg_entry (bad data) yields (0.0, 0.0) -> the rules HOLD.
    """
    if avg_entry <= 0:
        return 0.0, 0.0
    if qty < 0:  # short
        pnl_pct = (avg_entry - mark) / avg_entry
        drawdown = max(0.0, (mark - avg_entry) / avg_entry)
    else:  # long (or flat — treated as long; byte-identical to the original)
        pnl_pct = (mark - avg_entry) / avg_entry
        drawdown = max(0.0, (avg_entry - mark) / avg_entry)
    return pnl_pct, drawdown


# ---------- record-side derivation (cs17) ----------
def _rec_side(rec: dict) -> str:
    """Derive the buy/sell side of an execution record.

    The LIVE producer (react.paper._record_to_dict) emits NO ``side`` key — it
    carries a signed ``target_position_pct`` (and ``fill_size_pct``) instead. The
    legacy hand-rolled / settlement records carry an explicit ``side``. Honor an
    explicit non-empty ``side`` verbatim; otherwise derive it from the sign of the
    signed NAV fraction (a long target is a 'buy', a short target a 'sell').
    """
    side = rec.get("side")
    if isinstance(side, str) and side:
        return side
    frac = rec.get("target_position_pct")
    if frac is None:
        frac = rec.get("fill_size_pct")
    try:
        f = float(frac)
    except (TypeError, ValueError):
        return ""
    if f > 0:
        return "buy"
    if f < 0:
        return "sell"
    return ""


# ---------- play_tag inference ----------
def infer_play_tag(executions: list[dict], asset: str, position_qty: float = 0.0) -> str:
    """Find the most-recent OPENING execution for `asset` and infer the play.

    The opening leg shares the HELD position's sign: a long (position_qty>=0)
    opens with a 'buy', a short (position_qty<0) opens with a 'sell'. Match the
    leg by direction so a short's opening leg is read (mirrors cs20 :228 `if qty
    < 0`; flat/0 takes the long branch -> byte-identical to the pre-cs26 lookup).

    Order of precedence:
      1. Explicit `play_tag` field on the execution (the 'advisor' sentinel — the
         producer default for unlabeled fires — falls through, since it carries no
         playbook meaning)
      2. Explicit `recipe` field
      3. Substring match on `signal_id`: 'leaps' or 'swing'
      4. Default: 'swing' (cautious — gets the swing exit rules)
    """
    desired_side = "sell" if position_qty < 0 else "buy"
    for rec in reversed(executions):
        if rec.get("asset") != asset:
            continue
        if _rec_side(rec) != desired_side:  # opening leg (long->buy, short->sell)
            continue
        tag = rec.get("play_tag") or rec.get("recipe")
        if isinstance(tag, str) and tag and tag.lower() != "advisor":
            return tag.lower()
        sig = (rec.get("signal_id") or "").lower()
        for play in ("leaps", "swing", "covered_call", "csp", "wheel"):
            if play in sig:
                return play
        break
    return "swing"


# ---------- entry context lookup ----------
def find_entry_record(executions: list[dict], asset: str, position_qty: float = 0.0) -> dict | None:
    """First opening leg (long->buy, short->sell) for asset (= entry). None if not found.

    The opening leg shares the HELD position's sign: a long (position_qty>=0)
    opens with a 'buy', a short (position_qty<0) with a 'sell'. flat/0 takes the
    long branch -> byte-identical to the pre-cs26 buy-only lookup.
    """
    desired_side = "sell" if position_qty < 0 else "buy"
    for rec in executions:
        if rec.get("asset") == asset and _rec_side(rec) == desired_side:
            return rec
    return None


def days_between_iso(asof_iso: str, now_dt: datetime) -> int:
    """Calendar days from asof to now. Robust to ISO with or without 'Z'."""
    try:
        s = asof_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return 0
    return max(0, (now_dt - dt).days)


# ---------- idempotency ----------
def fired_this_week(symbol: str, play: str, monday: str) -> bool:
    """True iff (symbol, play, monday) already has a non-noop journal entry."""
    if not WEEKLY_JOURNAL_PATH.exists():
        return False
    try:
        with open(WEEKLY_JOURNAL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    row.get("event") == "decision"
                    and row.get("symbol") == symbol
                    and row.get("play") == play
                    and row.get("monday_et") == monday
                    and row.get("action") in ("CLOSE",)  # only suppressing real fires
                    and row.get("armed") is True
                ):
                    return True
    except OSError:
        pass
    return False


# ---------- portfolio reconstruction ----------
def load_portfolio(executions_path: Path | None = None) -> tuple[Any, list[dict]]:
    """Use hermes_quant's portfolio_loader; also return raw executions list."""
    if executions_path is None:
        executions_path = EXECUTIONS_PATH
    try:
        from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
        from hermes_quant.daemon.signal_bus import read_jsonl_tail
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"hermes_quant import failed: {exc}") from exc

    if not executions_path.exists():
        return None, []

    raw = read_jsonl_tail(executions_path, n=100_000)
    # We iterate over equity partitions (alpaca-paper). Crypto / others
    # are out of scope for the ADR-0035 weekly rebalance.
    pf = reconstruct_portfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        bus_path=executions_path,
    )
    return pf, raw


# ---------- mark price + ATR + fundamentals (best-effort) ----------
def fetch_mark_atr(symbol: str) -> tuple[float | None, float | None]:
    """Return (mark_price, atr14_pct_at_recent_close) via yfinance.

    Returns (None, None) on any fetch failure — the caller should HOLD on
    missing data rather than risk a bad-decision close.
    """
    try:
        import yfinance as yf  # type: ignore
        import pandas as pd  # noqa: F401
    except Exception:  # noqa: BLE001
        return None, None
    try:
        t = yf.Ticker(symbol)
        h = t.history(period="60d", interval="1d", auto_adjust=False)
        if h is None or h.empty or len(h) < 15:
            return None, None
        close = float(h["Close"].iloc[-1])
        # ATR-14 (Wilder simplification: rolling mean of true range)
        tr = (h["High"] - h["Low"]).abs()
        atr = float(tr.tail(14).mean())
        atr_pct = atr / close if close > 0 else None
        return close, atr_pct
    except Exception:  # noqa: BLE001
        return None, None


def fetch_fundamentals(symbol: str) -> tuple[float | None, float | None]:
    """Return (revenue_growth_yoy, debt_to_equity). Best-effort yfinance."""
    try:
        import yfinance as yf  # type: ignore
    except Exception:  # noqa: BLE001
        return None, None
    try:
        t = yf.Ticker(symbol)
        info = getattr(t, "info", {}) or {}
        rev = info.get("revenueGrowth")
        de = info.get("debtToEquity")
        rev_f = float(rev) if rev is not None else None
        # yfinance reports debtToEquity as a percentage-style number (e.g.
        # 150 = 1.5). Normalize to a ratio.
        de_f = float(de) / 100.0 if de is not None else None
        return rev_f, de_f
    except Exception:  # noqa: BLE001
        return None, None


# ---------- main weekly run ----------
def run_weekly(*, armed: bool) -> dict[str, Any]:
    run_id = utcnow_iso()
    monday = monday_iso_et()
    summary: dict[str, Any] = {
        "event": "weekly_summary",
        "run_id": run_id,
        "monday_et": monday,
        "armed": armed,
        "scanned": 0,
        "closes_proposed": 0,
        "closes_fired": 0,
        "holds": 0,
        "options_skipped": 0,
        "skipped_idempotent": 0,
        "errors": 0,
        "halt_aborted": False,
    }

    halts = read_active_halts()
    if halts:
        summary["halt_aborted"] = True
        summary["halts"] = halts
        append_journal({
            "event": "weekly_aborted_halt",
            "run_id": run_id,
            "monday_et": monday,
            "halts": halts,
            "armed": armed,
        })
        return summary

    # ---- Portfolio reconstruction
    try:
        pf, executions = load_portfolio()
    except Exception as exc:  # noqa: BLE001
        summary["errors"] += 1
        append_journal({
            "event": "weekly_portfolio_error",
            "run_id": run_id,
            "monday_et": monday,
            "error": str(exc),
            "trace": traceback.format_exc(),
        })
        return summary

    if pf is None or not pf.positions:
        append_journal({
            "event": "weekly_empty_portfolio",
            "run_id": run_id,
            "monday_et": monday,
            "armed": armed,
        })
        return summary

    now_dt = datetime.now(UTC)

    for asset, pos in pf.positions.items():
        summary["scanned"] += 1

        # Detect option-leg shape: "AAPL  240621C00200000" or anything
        # that isn't a clean equity ticker. ADR-0029 punt.
        if " " in asset or len(asset) > 6:
            summary["options_skipped"] += 1
            append_journal({
                "event": "decision",
                "run_id": run_id,
                "monday_et": monday,
                "symbol": asset,
                "play": "options",
                "action": "TODO_ADR0029",
                "reason": "options-leg detected; rolling deferred to ADR-0029",
                "armed": armed,
            })
            continue

        play = infer_play_tag(executions, asset, pos.qty)
        entry = find_entry_record(executions, asset, pos.qty)

        if entry is None:
            # Phantom position — log error, hold.
            summary["errors"] += 1
            append_journal({
                "event": "decision",
                "run_id": run_id,
                "monday_et": monday,
                "symbol": asset,
                "play": play,
                "action": "ERROR",
                "reason": "no opening execution found for held position",
                "armed": armed,
            })
            continue

        # Idempotency check (only matters when armed).
        if armed and fired_this_week(asset, play, monday):
            summary["skipped_idempotent"] += 1
            append_journal({
                "event": "decision",
                "run_id": run_id,
                "monday_et": monday,
                "symbol": asset,
                "play": play,
                "action": "SKIP_IDEMPOTENT",
                "reason": "already fired this week",
                "armed": armed,
            })
            continue

        days_held = days_between_iso(
            entry.get("asof_execution") or entry.get("asof", ""), now_dt
        )
        avg_entry = float(pos.avg_entry_price)
        mark, atr_pct = fetch_mark_atr(asset)
        if mark is None:
            mark = float(pos.mark_price)
        # cs20: sign-aware P&L/drawdown. The live book is short-dominated
        # (cs14 emits Position.qty<0); a long-only formula falsely marks a
        # losing short as profitable (LEAPS -25% / 60d-loss stop never fire)
        # and a winning short as losing (60d stop wrong-fires). For a long the
        # values are byte-identical to the pre-cs20 formula.
        pnl_pct, drawdown = compute_pnl_drawdown(avg_entry, mark, float(pos.qty))

        decision: ExitDecision
        details: dict[str, Any] = {
            "days_held": days_held,
            "avg_entry": avg_entry,
            "mark": mark,
            "pnl_pct": pnl_pct,
            "atr14_pct": atr_pct,
            "drawdown_from_entry": drawdown,
        }

        if play == "leaps":
            rev_g, de = fetch_fundamentals(asset)
            details["revenue_growth_yoy"] = rev_g
            details["debt_to_equity"] = de
            decision = decide_leaps(LeapsContext(
                revenue_growth_yoy=rev_g,
                debt_to_equity=de,
                drawdown_from_entry=drawdown,
            ))
        else:
            # swing default (also catches unknown play_tag values)
            decision = decide_swing(SwingContext(
                days_held=days_held,
                pnl_pct=pnl_pct,
                atr14_at_entry_pct=atr_pct or 0.0,
            ))

        if decision.action == "HOLD":
            summary["holds"] += 1
            append_journal({
                "event": "decision",
                "run_id": run_id,
                "monday_et": monday,
                "symbol": asset,
                "play": play,
                "action": "HOLD",
                "reason": decision.reason,
                "details": details,
                "armed": armed,
            })
            continue

        # CLOSE branch
        summary["closes_proposed"] += 1
        rec = {
            "event": "decision",
            "run_id": run_id,
            "monday_et": monday,
            "symbol": asset,
            "play": play,
            "action": "CLOSE",
            "reason": decision.reason,
            "details": details,
            "armed": armed,
        }

        if not armed:
            rec["action"] = "DRY_RUN_CLOSE"
            append_journal(rec)
            continue

        # Armed: fire the close through the same paper-API path the daily
        # tick uses (multi-leg paper reactor / Alpaca paper). We reuse the
        # high-level helper if available; otherwise we record a CLOSE_FAILED
        # journal entry rather than crashing the cron.
        try:
            placed = _fire_equity_close(asset, qty=float(pos.qty), reason=decision.reason)
            if placed.get("ok"):
                summary["closes_fired"] += 1
                rec["execution_id"] = placed.get("execution_id")
                append_journal(rec)
            else:
                summary["errors"] += 1
                rec["action"] = "CLOSE_FAILED"
                rec["error"] = placed.get("error", "unknown")
                append_journal(rec)
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            rec["action"] = "CLOSE_FAILED"
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["trace"] = traceback.format_exc()
            append_journal(rec)

    append_journal(summary)
    return summary


def _fire_equity_close(symbol: str, *, qty: float, reason: str) -> dict[str, Any]:
    """Place a market sell to flatten an equity position.

    Reuses hermes_quant.reactor.PaperReactor when available. The ADR-0029
    multi-leg reactor is the eventual home; for now we go through the same
    Alpaca paper path the daily tick uses.
    """
    try:
        from hermes_quant.reactor import PaperReactor  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"PaperReactor import failed: {exc}"}

    try:
        reactor = PaperReactor()  # type: ignore[call-arg]
        # Best-effort call — if the API differs, surface the error clearly.
        if hasattr(reactor, "close_position"):
            res = reactor.close_position(symbol=symbol, qty=qty, reason=reason)  # type: ignore[attr-defined]
        elif hasattr(reactor, "execute"):
            res = reactor.execute(  # type: ignore[attr-defined]
                {"symbol": symbol, "side": "sell", "qty": qty, "reason": reason}
            )
        else:
            return {"ok": False, "error": "PaperReactor lacks close_position/execute"}
        if isinstance(res, dict):
            return {"ok": True, "execution_id": res.get("execution_id"), "raw": res}
        return {"ok": True, "execution_id": getattr(res, "execution_id", None)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------- CLI ----------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="hermes-quant weekly rebalance / position-management cron"
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Run pipeline without placing orders (default).")
    g.add_argument("--armed", dest="armed", action="store_true",
                   help="Real paper-mode firing. Required for the cron when fully ramped.")
    parser.add_argument("--json", action="store_true",
                        help="Emit single-line JSON summary on stdout.")
    args = parser.parse_args()
    armed = bool(args.armed) and not bool(args.dry_run)

    try:
        summary = run_weekly(armed=armed)
    except Exception as exc:  # noqa: BLE001
        append_journal({
            "event": "weekly_uncaught_exception",
            "ts": utcnow_iso(),
            "monday_et": monday_iso_et(),
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "armed": armed,
        })
        sys.stderr.write(f"quant-playbook-weekly: uncaught: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(summary, default=str), flush=True)
    else:
        suffix = ""
        if summary["halt_aborted"]:
            suffix = " HALT-ABORTED"
        elif not armed:
            suffix = " (dry-run)"
        # cron deliver=local + empty stdout suppresses the message; we still
        # print so an operator running by hand sees the summary.
        proposed = summary["closes_proposed"]
        if proposed == 0 and summary["scanned"] == 0 and not summary["halt_aborted"]:
            # Truly silent for the cron — nothing fired, nothing to report.
            return 0
        print(
            f"weekly: scanned={summary['scanned']} "
            f"closes_proposed={proposed} closes_fired={summary['closes_fired']} "
            f"holds={summary['holds']} options_skipped={summary['options_skipped']} "
            f"skipped_idempotent={summary['skipped_idempotent']} errors={summary['errors']}"
            f"{suffix}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
