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
from datetime import date, datetime, timedelta, timezone
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

# The account partition this weekly playbook manages. load_portfolio() reconstructs
# pf.positions from EXACTLY this account+equity slice (reconstruct_portfolio(
# account_id="paper-default", asset_class="equity"), cs24 below). But the per-asset
# raw-executions readers (_held_nav_fraction / _establishing_leg, which back
# find_entry_record + infer_play_tag) scan the SHARED executions.jsonl bus, which is
# ALSO written by other accounts — the alpaca-paper SHADOW reactor (account_id=
# "alpaca-paper", equity, the SAME tickers can collide e.g. AAPL) and the freqtrade
# crypto consumer (account_id="freqtrade", qty in RAW COINS). Scanning those readers
# WITHOUT an account filter pools a DIFFERENT account's record into the paper-default
# decision: e.g. a later alpaca-paper AAPL target=0.10 overrides the real paper-default
# target=0.30, so the armed close fires fill_size_pct=-0.10 and the live state.db delta
# fold (new_qty = 0.30 + (-0.10) = 0.20) leaves a position the weekly reported CLOSED
# still OPEN — a money fail-open on the live close path. We therefore filter every raw-
# executions reader to THIS account, resolving each record's account the SAME way the
# loader does (portfolio_loader._record_account / portfolio.state._record_account).
WEEKLY_ACCOUNT = "paper-default"


def _record_account(rec: dict) -> str:
    """Resolve the account partition a bus record belongs to.

    Verbatim mirror of hermes_quant.portfolio.state._record_account /
    daemon.portfolio_loader._record_account (operator-approved cs18/cs24 ladder):
    top-level ``account_id`` if truthy, else ``reactor_metadata.account_id`` if truthy,
    else the ``"paper-default"`` sentinel. So a legacy/test record with NO account
    stamp resolves to ``paper-default`` (byte-identical to the pre-filter behavior for
    the single-account bus), an ``alpaca_paper`` fill resolves to ``alpaca-paper``, and
    a freqtrade fill resolves to ``freqtrade`` — the latter two are then excluded from
    this weekly's paper-default partition.
    """
    acct = rec.get("account_id")
    if acct:
        return str(acct)
    meta_acct = (rec.get("reactor_metadata") or {}).get("account_id")
    if meta_acct:
        return str(meta_acct)
    return "paper-default"


ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ---------- exit thresholds (ADR-0035) ----------
SWING_MAX_DAYS_LOSING = 60
SWING_TP_ATR_MULT = 3.0
# cs29 (ADR-0035 §98): the 3xATR take-profit threshold must use ATR-14 AT ENTRY,
# not today's ATR. We fetch a daily-bar window ending at the entry date; ~40
# calendar days reliably nets >=15 trading bars (the _atr_pct_from_bars minimum).
ENTRY_ATR_LOOKBACK_DAYS = 40
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


# ---------- options-leg detection (cs37) ----------
def _is_option_symbol(asset: str) -> bool:
    """True iff ``asset`` is an options leg (OCC structure), NOT raw string length.

    cs37: the old test was ``len(asset) > 6`` — an OCC-21 symbol is ~19-21 chars, so
    that ACCIDENTALLY caught real options BUT also silently HELD any equity ticker
    longer than 6 chars (a 7+ char ticker, a class-share / ADR symbol) as an "option",
    skipping the equity close entirely. We detect an option by its OCC SHAPE instead:

      * the space-padded 21-char wire form (root left-justified to 6, then a space),
        which a plain equity ticker never carries; OR
      * a string that parses as a well-formed OCC-21 symbol (root + YYMMDD + C/P +
        8-digit strike). ``hermes_quant.options.occ.parse_occ`` is the canonical,
        pure (no-I/O) recognizer.

    A clean equity ticker — ``GOOGL`` (5), ``LONGTICKER`` (10), ``BRK.B`` (5),
    ``GOOGL.X`` (7) — is NOT an option: it has no internal space and does not parse as
    OCC-21. A real OCC option (compact ``NVDA260526C00145000`` or the space-padded wire
    form) IS. Falls back to the legacy len>6 heuristic ONLY if the options module is
    unavailable (defensive; the package is always importable in the live env).
    """
    if not isinstance(asset, str):
        return False
    if " " in asset:
        return True  # space-padded OCC wire form; a plain ticker never has a space
    try:
        from hermes_quant.options.occ import OccParseError, parse_occ
    except Exception:  # noqa: BLE001 — defensive only; options pkg is normally present
        return len(asset) > 6
    try:
        parse_occ(asset)
        return True
    except OccParseError:
        return False
    except Exception:  # noqa: BLE001 — any other parse failure -> treat as non-option
        return False


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


def _held_nav_fraction(
    executions: list[dict], asset: str, *, account: str = WEEKLY_ACCOUNT
) -> float:
    """Return the currently-held absolute NAV-fraction target for ``asset`` (0.0 absent).

    cs21: the armed close must fire ``fill_size_pct = -held`` (NOT 0.0) so it FLATTENS
    the LIVE default-regime ``state.db`` delta fold, where ``new_qty = old_qty +
    fill_size_pct`` (portfolio_state.py:1124; the flag ``HERMES_QUANT_DELTA_NORMALIZER``
    is unset == regime 0 == raw-delta fold). A ``_fire_equity_close`` Proposal carries
    NO ``reactor_metadata.quantity`` so the fill takes the NAV-fraction lane
    (``pos_delta = fill_size_pct``, portfolio_state.py:687) — the delta-fold path. With
    ``-held``: a held SHORT (-0.20) folds +0.20 -> 0 (buy-to-cover); a held LONG (+0.30)
    folds -0.30 -> 0 (sell). ``fill_size_pct=0.0`` would be a SILENT NO-OP on that ledger
    (proven: _update_position(-0.20,...,0.0) -> -0.20, unchanged) — the close would not
    flatten the live book.

    The held NAV-fraction == the LATEST ``target_position_pct`` for the asset — the SAME
    abs_latest record the loader keys QTY/sign off (portfolio_loader.py:316-326). We scan
    the raw ``executions`` list for the asset's MAX-``asof_execution`` (fall back to
    ``asof``) record and return its ``target_position_pct``. Mirror the loader's tie/order
    (``ts >= prior`` keeps the LAST of an equal-timestamp run). Returns 0.0 on a missing /
    unparseable target or no matching record (the close then no-ops, which is safe).
    """
    best_rec: dict | None = None
    best_ts = ""
    for rec in executions:
        if rec.get("asset") != asset:
            continue
        # Cross-account guard: the shared bus carries other accounts (alpaca-paper
        # shadow equity, freqtrade crypto). A different account's later record must
        # NOT override this account's held fraction — that would mis-size the armed
        # close on the live state.db delta fold. Skip records outside this partition.
        if _record_account(rec) != account:
            continue
        ts = rec.get("asof_execution") or rec.get("asof") or ""
        if best_rec is None or ts >= best_ts:
            best_rec, best_ts = rec, ts
    if best_rec is None:
        return 0.0
    try:
        return float(best_rec.get("target_position_pct"))
    except (TypeError, ValueError):
        return 0.0


# ---------- establishing-leg selection (cs27/cs28) ----------
def _establishing_leg(
    executions: list[dict],
    asset: str,
    position_qty: float = 0.0,
    *,
    account: str = WEEKLY_ACCOUNT,
) -> dict | None:
    """Return the leg that ESTABLISHED the CURRENTLY-held position (None if absent).

    cs27/cs28 root question: which execution is the entry of a multi-fill position?
    The loader keeps the LATEST target per asset and folds re-opens/flips silently
    (portfolio_loader.py:245-255 max asof_execution; the absolute-target path has NO
    flip/partial guard, :225-233). So the held SIGN is sign(latest target). The
    establishing leg is the FIRST fill of that held sign in the CURRENT run — the run
    bounded behind by the last flat (target==0 -> _rec_side=="") or flip (the opposite
    side). days_held and the play tag both anchor here so all readers AND the loader
    agree on ONE leg per held position.

    Walk the per-asset fills IN FILE ORDER (the list is already oldest-first; do NOT
    re-sort — preserve the loader's tie/order semantics). Track a candidate (the first
    desired_side fill of the current run) plus a "boundary seen since the candidate"
    flag. A boundary (flat or flip) only SETS the flag — it does NOT erase the
    candidate. When the NEXT desired_side fill arrives after a boundary it RE-OPENS the
    run and becomes the new candidate. So the post-loop candidate is the first
    desired_side fill after the LAST boundary that was actually followed by a same-sign
    re-open; a TRAILING flip with no subsequent same-sign fill leaves the prior run's
    opening leg intact (the position-flip case: querying the pre-flip sign still returns
    that run's opener rather than None — keeps cs26 `test_reshorted_asset_picks_held_sign_leg`
    green and is correct since run_weekly always queries the ACTUAL held sign).

    SINGLE-FILL LONG stays byte-identical: one buy, qty>=0 -> desired_side='buy', no
    flat/flip -> the single buy is both the first-ever match (old behavior) AND the
    establishing leg. A same-sign ADD (e.g. AVGO -0.1 then -0.2, no flat between) keeps
    the FIRST add of the run as the establishing leg — the exposure-honest anchor
    (a routine size-up must not silently reset the holding clock; PROVE §4). A re-open
    across a flat (BA -0.2, 0.0, -0.2) anchors on the post-flat re-open (PROVE §2).
    """
    desired_side = "sell" if position_qty < 0 else "buy"
    candidate: dict | None = None
    boundary_since_candidate = False
    for rec in executions:
        if rec.get("asset") != asset:
            continue
        # Cross-account guard (see _held_nav_fraction): another account's fill for the
        # SAME ticker must not be picked as this position's establishing leg — that
        # would corrupt days_held (the swing 60d stop) and the play classification
        # (leaps vs swing -> wrong exit ruleset). Skip records outside this partition.
        if _record_account(rec) != account:
            continue
        side = _rec_side(rec)
        if side == desired_side:
            if candidate is None or boundary_since_candidate:
                candidate = rec  # opens (or re-opens, post-boundary) the current run
                boundary_since_candidate = False
        else:
            # Boundary: a flat (side=="") or a flip (opposite side) closes the run.
            # Mark it; the NEXT desired_side fill re-opens the run. A trailing boundary
            # with no subsequent same-sign fill leaves the prior run's opener intact.
            boundary_since_candidate = True
    return candidate


# ---------- play_tag inference ----------
def infer_play_tag(
    executions: list[dict],
    asset: str,
    position_qty: float = 0.0,
    *,
    account: str = WEEKLY_ACCOUNT,
) -> str:
    """Infer the play from the leg that ESTABLISHED the currently-held position.

    The establishing leg (cs27/cs28: first fill of the held sign in the current run,
    after the last flat/flip — see ``_establishing_leg``) is the leg that OPENED the
    position held now. We classify off that leg so the play tag agrees with the leg
    days_held is measured from (replacing the pre-cs28 reversed()/newest-leg scan that
    let a same-sign add re-classify the play). flat/0 takes the long branch.

    Order of precedence:
      1. Explicit `play_tag` field on the execution (the 'advisor' sentinel — the
         producer default for unlabeled fires — falls through, since it carries no
         playbook meaning)
      2. Explicit `recipe` field
      3. Substring match on `signal_id`: 'leaps' or 'swing'
      4. Default: 'swing' (cautious — gets the swing exit rules)
    """
    rec = _establishing_leg(executions, asset, position_qty, account=account)
    if rec is not None:
        tag = rec.get("play_tag") or rec.get("recipe")
        if isinstance(tag, str) and tag and tag.lower() != "advisor":
            return tag.lower()
        sig = (rec.get("signal_id") or "").lower()
        for play in ("leaps", "swing", "covered_call", "csp", "wheel"):
            if play in sig:
                return play
    return "swing"


# ---------- entry context lookup ----------
def find_entry_record(
    executions: list[dict],
    asset: str,
    position_qty: float = 0.0,
    *,
    account: str = WEEKLY_ACCOUNT,
) -> dict | None:
    """Establishing (run-opening) leg for the held position (= entry). None if absent.

    Returns the leg that ESTABLISHED the currently-held position: the first fill of
    the held sign in the CURRENT run, after the last flat (target==0) or flip — see
    ``_establishing_leg`` (cs27/cs28). days_held (run_weekly :535-536) reads age from
    when the CURRENT run opened, not the first-ever file-order match (which inflated
    age on a re-opened position and wrong-fired the armed 60d swing stop). A single
    buy with no flat/flip -> the establishing leg IS that buy, so a single-fill long
    is byte-identical to the pre-cs26 buy-only lookup.
    """
    return _establishing_leg(executions, asset, position_qty, account=account)


def days_between_iso(asof_iso: str, now_dt: datetime) -> int:
    """Calendar days from asof to now. Robust to ISO with or without 'Z'."""
    try:
        s = asof_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except (ValueError, TypeError, AttributeError):
        # cs30: a non-str asof (numeric / None / Timestamp / datetime) makes
        # .replace raise AttributeError or fromisoformat raise TypeError. Without
        # this widening the exception escapes the per-asset loop (no per-asset
        # try/except) into main()'s catch-all -> weekly_uncaught_exception,
        # return 1, killing the WHOLE weekly run. Degrade to days_held=0 for the
        # one bad position instead. The str-ISO success path is byte-unchanged.
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
    # cs24: manage the "paper-default" equity partition — the REAL synthetic book the
    # ADR-0035 playbook system trades (PaperReactor + DeterministicEquityReactor +
    # the autonomous tick all write to "paper-default"; see react/deterministic_equity.py:79
    # "shares the SAME book the autonomous tick + the legacy PaperReactor read/write").
    # The prior "alpaca-paper" request was the SEPARATE Alpaca SHADOW partition
    # (react/alpaca_paper.py:67, default-OFF behind HERMES_QUANT_ALPACA_PAPER=1) — NOT
    # the book the weekly is meant to manage. That wrong-account request was masked by a
    # loader set-OR (portfolio_loader.py: `in {account_id,"paper-default"}`) that pooled
    # the paper-default book into the alpaca-paper request; with the loader tightened to
    # account-EQUALITY (cs24), asking for "alpaca-paper" would now return ONLY the lone
    # alpaca shadow position and STOP managing the real paper-default book — so the
    # account request is corrected here in lockstep. Crypto / others are out of scope for
    # the ADR-0035 weekly rebalance (equity-only).
    pf = reconstruct_portfolio(
        account_id="paper-default",
        asset_class="equity",
        bus_path=executions_path,
    )
    return pf, raw


# ---------- mark price + ATR + fundamentals (best-effort) ----------
def _atr_pct_from_bars(h: Any) -> float | None:
    """ATR-14 as a fraction of the last-bar close, from a daily-bar OHLC frame.

    Pure (no I/O). Returns None for an empty / <15-bar frame or a non-positive
    last-bar close (so the caller can fall back rather than fabricate). Uses the
    same Wilder simplification the legacy fetch used — mean of the last-14
    (High-Low) true-range bars — over the bar window AS GIVEN. Feeding the wrong
    window (today's vs. the entry's) yields the wrong threshold; that is the cs29
    bug this helper makes testable.
    """
    try:
        if h is None or h.empty or len(h) < 15:
            return None
        close = float(h["Close"].iloc[-1])
        tr = (h["High"] - h["Low"]).abs()
        atr = float(tr.tail(14).mean())
        return atr / close if close > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _parse_entry_date(entry_date: Any) -> date | None:
    """Parse an ISO asof string to a date. Never raises — returns None on junk,
    None, or a non-str input (so the at-entry ATR lookup degrades gracefully)."""
    if not isinstance(entry_date, str) or not entry_date:
        return None
    try:
        s = entry_date.replace("Z", "+00:00")
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        return None


def fetch_mark_atr(symbol: str, *, entry_date: str | None = None) -> tuple[float | None, float | None]:
    """Return (mark_price, atr14_pct) via yfinance.

    The mark is ALWAYS the recent (today-ending) close. The ATR fraction is:
      * entry_date is None  -> ATR-14 at the recent close (legacy behavior,
        byte-identical to the pre-cs29 no-arg path).
      * entry_date given     -> ADR-0035 §98 ATR-14 AT ENTRY: a daily-bar window
        ending at/just-before the entry date, divided by the entry-date close.
        Anchoring the denominator to the FIXED entry-date close also removes the
        cs20 short moving-mark drift (the take-profit threshold no longer drifts
        with the live mark).

    Graceful fallback (NO fabrication): if entry_date is unparseable, or the
    entry-window fetch returns an empty/<15-bar frame (e.g. the entry predates
    available history), we fall back to the recent ATR rather than invent a
    number or return a None ATR. Returns (None, None) on any total fetch failure
    — the caller HOLDs on missing data.
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
        recent_atr_pct = _atr_pct_from_bars(h)
        if entry_date is None:
            return close, recent_atr_pct
        ed = _parse_entry_date(entry_date)
        if ed is None:
            return close, recent_atr_pct  # unparseable -> recent fallback
        # Fetch a window ending at the entry date. end is EXCLUSIVE in yfinance,
        # so +1 day includes the entry bar itself.
        start = (ed - timedelta(days=ENTRY_ATR_LOOKBACK_DAYS)).isoformat()
        end = (ed + timedelta(days=1)).isoformat()
        he = t.history(start=start, end=end, interval="1d", auto_adjust=False)
        entry_atr_pct = _atr_pct_from_bars(he)
        if entry_atr_pct is None:
            return close, recent_atr_pct  # short/empty window -> recent fallback
        return close, entry_atr_pct
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

        # Detect option-leg shape by OCC STRUCTURE (cs37), not raw string length:
        # "AAPL  240621C00200000" (wire form) or a parseable OCC-21 symbol. A 7+ char
        # equity ticker / class-share / ADR is NOT an option and must reach the exit
        # rules below — the old `len(asset) > 6` test silently HELD it. ADR-0029 punt.
        if _is_option_symbol(asset):
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

        # cs29: read the establishing-leg entry date ONCE; reuse it for both the
        # holding clock and the ATR-14-AT-ENTRY anchor (ADR-0035 §98).
        entry_asof = entry.get("asof_execution") or entry.get("asof", "")
        days_held = days_between_iso(entry_asof, now_dt)
        avg_entry = float(pos.avg_entry_price)
        mark, atr_pct = fetch_mark_atr(asset, entry_date=entry_asof)
        if mark is None:
            _fallback = float(pos.mark_price)
            # If pos.mark_price == avg_entry_price the absolute-target loader returned
            # a stale default (no mark_prices kwarg was passed — portfolio_loader.py:382
            # uses entry_price as the dict-get default when mark_prices is empty).
            # Treating a stale-default mark as the current price produces pnl_pct==0.0
            # for EVERY position, which silently suppresses the >60d swing stop and
            # the LEAPS -25% drawdown close (0.0 < 0 is False; 0.0 > 0.25 is False).
            # Skip the position for this weekly run rather than fire decisions with a
            # fabricated zero-pnl mark.  The HOLD/skip is strictly safer than the prior
            # silent-corruption path (docstring of fetch_mark_atr says "caller HOLDs on
            # missing data"; this makes it so).
            if abs(_fallback - avg_entry) < 1e-9 * max(1.0, abs(avg_entry)):
                summary["errors"] += 1
                append_journal({
                    "event": "decision",
                    "run_id": run_id,
                    "monday_et": monday,
                    "symbol": asset,
                    "play": play,
                    "action": "SKIP_NO_MARK",
                    "reason": (
                        "fetch_mark_atr failed and pos.mark_price == avg_entry "
                        "(stale loader default — no live mark available)"
                    ),
                    "armed": armed,
                })
                continue
            mark = _fallback
        # Operator visibility: 'entry' when the at-entry anchor was honored,
        # 'recent_fallback' when the entry date was missing/unparseable (the ATR
        # may then reflect the recent window or the entry window's graceful
        # fallback — either way the entry anchor could not be guaranteed).
        atr_basis = "entry" if _parse_entry_date(entry_asof) is not None else "recent_fallback"
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
            "atr_basis": atr_basis,
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
            # cs21: the close size is the NEGATIVE of the held NAV-fraction (the traded
            # delta that flattens the default-regime state.db fold), recovered from the
            # latest-target record — the SAME abs_latest record the loader reads.
            held = _held_nav_fraction(executions, asset)
            # cs36: plumb a REAL close decision_price (the recent close / mark) into the
            # fire. cr05 (LANDED) made PaperReactor REJECT a zero/missing decision_price
            # (silence record, no fill) — the old hardcoded 0.0 made EVERY armed weekly
            # close silently no-fill. `mark` is the same recent-close the exit rules used
            # above (fetch_mark_atr -> pos.mark_price fallback), mirroring the daily path
            # (advisor.py: final["decision_price"] = float(ctx.last_close)).
            placed = _fire_equity_close(
                asset,
                qty=float(pos.qty),
                target_position_pct=held,
                reason=decision.reason,
                decision_price=mark,
            )
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


def _fire_equity_close(
    symbol: str, *, qty: float, target_position_pct: float, reason: str, decision_price: float
) -> dict[str, Any]:
    """Flatten an equity position through the PaperReactor — fire ``-held`` to cover/sell.

    cs21: the close DIRECTION is the opposite of the held sign — a held SHORT
    (``qty < 0``) is closed by a BUY-to-cover, a held LONG (``qty > 0``) by a SELL.
    The OLD body hardcoded ``side="sell"`` for ANY position, so an armed close of a
    short would DEEPEN the short rather than cover it.

    We fire through the canonical ``hermes_quant.react.paper.PaperReactor`` (the OLD
    body imported the non-existent ``hermes_quant.reactor`` module — a guaranteed
    ModuleNotFoundError -> CLOSE_FAILED on every armed close). The reactor consumes a
    ``Proposal`` object (NOT a dict) and a KEYWORD-ONLY ``fill_size_pct``:
    ``execute(proposal, *, fill_size_pct, ...)`` (react/paper.py).

    DECISION_PRICE — pass the real close/mark, NOT the 0.0 sentinel (cs36)
    -----------------------------------------------------------------------------------
    cr05 (LANDED 2026-06-14) made PaperReactor REJECT a non-finite / <= 0
    ``decision_price`` UPSTREAM (react/paper.py:208): it returns a SILENCE record
    (``fill_size_pct=0.0``, NOT appended, no state.db write, ``silence_reason=
    'zero_decision_price'``). The OLD body hardcoded ``advisor_result['decision_price']
    = 0.0`` ("PaperReactor tolerates 0.0") — which is no longer true: post-cr05 EVERY
    armed weekly close is silenced and the position SURVIVES (a silent no-fill). We now
    plumb the caller-supplied ``decision_price`` (the recent close / mark — the SAME
    value the exit rules used, and the SAME source the daily path uses: advisor.py
    ``final['decision_price'] = float(ctx.last_close)``). If it is non-finite / <= 0 we
    fail CLOSED (return ``ok=False``) rather than fire a price cr05 would reject anyway,
    so the cron records a CLOSE_FAILED journal entry instead of a silent no-op.

    CLOSE SIZE — ``fill_size_pct = -target_position_pct`` (the NEGATIVE of the held
    NAV-fraction), NOT 0.0
    -----------------------------------------------------------------------------------
    A ``_fire_equity_close`` Proposal carries no ``reactor_metadata.quantity``, so the
    fill takes the NAV-fraction lane and folds into the LIVE state.db as a raw DELTA in
    the default regime (``new_qty = old_qty + fill_size_pct``, portfolio_state.py:1124;
    ``HERMES_QUANT_DELTA_NORMALIZER`` is unset == regime 0). Firing ``-held`` flattens it
    and the SIGN is automatically correct: short (held<0) -> +delta (buy-to-cover); long
    (held>0) -> -delta (sell). ``|-held| <= 1.0`` so the |fill_size|<=1.0 ceiling holds.
    ``fill_size_pct=0.0`` would be a SILENT NO-OP on this ledger — the short/long would
    survive the "close" (proven: _update_position(-0.20,...,0.0) -> -0.20, unchanged).

    DUAL-LEDGER DIVERGENCE / HAZARD (do NOT silently elide — concurrent-critique Finding A)
    ----------------------------------------------------------------------------------------
    There is NO single value that flattens BOTH ledgers the schema folds against:
      * ``0.0`` flattens the ``reconstruct_portfolio`` LATEST-TARGET loader (and the
        normalizer-ON state.db, where ``delta_from_net = 0 - held = -held``), because a
        non-zero latest target reconstructs as a NEW position of that size/sign rather
        than a flat (portfolio_loader.py:335-336 drops only ``|target| < 1e-12``).
      * ``-held`` flattens the DEFAULT-regime state.db raw-delta fold — the live money
        ledger TODAY (the flag is unset; the close fires a -held delta that cancels the
        carried net). This is the path PaperReactor.execute writes (react/paper.py:442).
    Finding A argues the close target must be 0.0 (correct for the loader/normalizer-ON
    paths). The scoped cs21 directive is authoritative: fire ``-held`` to flatten the
    LIVE default-regime state.db. The full reconciliation (normalizer-ON makes BOTH need
    0.0, or a close-specific override) is OUT OF cs21 SCOPE and is documented, not
    code-resolved. The det-equity reactor twin is irrelevant here (it carries
    reactor_metadata.quantity -> shares lane; ``_fire_equity_close`` uses PaperReactor).

    The whole body stays inside try/except so an armed cron records a CLOSE_FAILED
    journal entry on any failure (ImportError, etc.) rather than crashing the run.
    """
    try:
        import math
        import secrets

        from hermes_quant.proposals import Proposal
        from hermes_quant.react.paper import PaperReactor

        # cs36: fail CLOSED on a bad close price. cr05 would reject a non-finite / <= 0
        # decision_price (silence record, no fill); surface it as CLOSE_FAILED instead
        # of a silent no-op so an armed cron logs the failure.
        dp = float(decision_price)
        if not math.isfinite(dp) or dp <= 0.0:
            return {"ok": False, "error": f"bad decision_price={decision_price!r} (non-finite or <= 0)"}

        # Close direction = OPPOSITE the held sign: short (qty<0) -> BUY to cover;
        # long (qty>0) -> SELL. Flat (qty==0) takes the long/sell branch (a no-op
        # close-to-flat; harmless). Kept for the audit/pairing field; the SIZE sign
        # (close_size below) carries the direction into the actual fold.
        side = "buy" if qty < 0 else "sell"

        # Close DELTA = -held NAV-fraction (the traded delta that flattens the
        # default-regime state.db fold). short held -0.20 -> +0.20 (cover); long held
        # +0.30 -> -0.30 (sell). 0.0 (no held target) -> a harmless no-op close.
        close_size = -float(target_position_pct)

        # Mint a proposal_id in the ADR-0015 §D3 shape (prop_<ISO_seconds>_<symbol>_<rand6>)
        # without coupling to proposals.py private helpers; built from the script's own
        # UTC-ISO-seconds clock so the close is self-contained + auditable.
        iso_seconds = utcnow_iso()
        iso_compact = iso_seconds.replace("-", "").replace(":", "").rstrip("Z")
        safe_symbol = "".join(c if c.isalnum() else "_" for c in symbol)[:16]
        proposal_id = f"prop_{iso_compact}_{safe_symbol}_{secrets.token_hex(3)}"

        prop = Proposal(
            proposal_id=proposal_id,
            state="approved",
            symbol=symbol,
            asset_class="equity",
            timeframe="1d",
            created_at=iso_seconds,
            expires_at=iso_seconds,
            advisor_result={
                "decision_price": dp,  # cs36: the real close/mark; cr05 rejects 0.0
                "as_of": iso_seconds,
                "close_side": side,
                "reason": reason,
            },
        )
        # fill_size_pct = -held = the close DELTA that flattens the live state.db fold
        # (KEYWORD-ONLY). NOT 0.0 (which would be a silent no-op on the default regime).
        rec = PaperReactor().execute(prop, fill_size_pct=close_size, play_tag="playbook")
        # ExecutionRecord carries .proposal_id (NOT .execution_id).
        return {"ok": True, "execution_id": getattr(rec, "proposal_id", None), "side": side}
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
