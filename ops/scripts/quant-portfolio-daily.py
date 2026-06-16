#!/usr/bin/env python3
"""quant-portfolio-daily.py — End-of-day portfolio snapshot for paper-default.

Schedule: `5 13 * * 1-5` (weekdays 13:05 PT = 16:05 ET, ~5 min after US close).
Deliver:  origin (Discord thread).
Mode:     no_agent=True.

Reads state.db.positions in the `paper-default` account, fetches live yfinance
marks, computes mark-to-market unrealized P&L, and emits a Discord-friendly
markdown report.

Output design:
  • Compact mode (default, used by cron): ≤2000 char Discord message with
    headline, summary block, today's top movers (3+3), lifetime top winners
    (3) + drawdowns (3), fills-today count. Full detail goes to a markdown
    attachment delivered via MEDIA: tag.
  • Verbose mode (--verbose, used by on-demand pull): full inline dump,
    no attachment.

After every run, a JSON snapshot is persisted to
  ~/.hermes/quant/daily-portfolio-snapshots/<YYYYMMDD>-portfolio.json
which enables day-over-day delta computation by tomorrow's run.

Posture: READ-ONLY (positions). Persists summary snapshot only.
Silence-by-default: if zero positions AND zero fills today, emit nothing.

Flags:
  --verbose      Full inline dump (no attachment).
  --dry-run      Skip snapshot persistence.
  --account ID   state.db account_id (default: paper-default).
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUANT_DIR = Path.home() / ".hermes" / "quant"
STATE_DB_PATH = QUANT_DIR / "state.db"
EXECUTIONS_PATH = QUANT_DIR / "executions.jsonl"
SNAPSHOT_DIR = QUANT_DIR / "daily-portfolio-snapshots"

# US equity-option contract multiplier (shares controlled per contract). Mirrors
# hermes_quant.state.portfolio_state._CONTRACT_MULTIPLIER (and options.data) so the
# true-unit (ADR-0088 multi-leg) option valuation here matches the ledger fold; a
# us_option position's dollar value is mark × contracts × 100.
_CONTRACT_MULTIPLIER = 100.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load_positions(account: str = "paper-default") -> list[dict]:
    if not STATE_DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        rows = conn.execute(
            "SELECT asset_class, symbol, quantity, avg_entry_price, last_update_at "
            "FROM positions WHERE account_id = ? AND quantity != 0",
            (account,),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"⚠️  state.db read failed: {e}", file=sys.stderr)
        return []
    out = []
    for acls, sym, qty, avg, ts in rows:
        try:
            out.append(
                {
                    "symbol": str(sym),
                    # asset_class is the only regime marker available at report time:
                    # 'us_option' rows are TRUE-UNIT (real signed contracts, ADR-0088),
                    # every other class is the legacy NAV-FRACTION equity path
                    # (Position.quantity = signed fraction of NAV — get_marked_equity).
                    "asset_class": str(acls) if acls else "equity",
                    "qty": float(qty),
                    "avg_entry": float(avg),
                    "last_update_at": str(ts) if ts else None,
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def load_nav_ref(account: str = "paper-default") -> float | None:
    """NAV reference for valuing NAV-fraction rows (cash.equity_total).

    Mirrors PortfolioState.get_marked_equity, which sizes each NAV-fraction
    position against cash.equity_total (cost-basis equity). Returns None when no
    finite, positive cash row exists so the caller can fail honestly rather than
    value a NAV-fraction as if it were shares.
    """
    if not STATE_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        row = conn.execute(
            "SELECT equity_total FROM cash WHERE account_id = ?",
            (account,),
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"⚠️  state.db cash read failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    try:
        nav = float(row[0])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(nav) or nav <= 0:
        return None
    return nav


def load_marks(symbols: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """Return (current_marks, prev_close_marks) via yfinance batch."""
    marks: dict[str, float] = {}
    prev_close: dict[str, float] = {}
    if not symbols:
        return marks, prev_close
    try:
        import yfinance as yf

        tickers_str = " ".join(symbols)
        data = yf.Tickers(tickers_str)
        for sym in symbols:
            try:
                tk = data.tickers.get(sym)
                if tk is None:
                    continue
                fi = getattr(tk, "fast_info", None)
                px = pcl = None
                if fi is not None:
                    px = (
                        fi.get("last_price")
                        or fi.get("lastPrice")
                        or fi.get("regular_market_price")
                    )
                    pcl = fi.get("previous_close") or fi.get("previousClose")
                if not px:
                    info = tk.info or {}
                    px = info.get("regularMarketPrice") or info.get("currentPrice")
                    pcl = pcl or info.get("regularMarketPreviousClose")
                if px:
                    marks[sym] = float(px)
                if pcl:
                    prev_close[sym] = float(pcl)
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️  yfinance batch fetch failed: {e}", file=sys.stderr)
    return marks, prev_close


def load_fills_today(today_et_date: str) -> list[dict]:
    """Load executions.jsonl entries with asof_execution date == today_et_date."""
    if not EXECUTIONS_PATH.exists():
        return []
    out: list[dict] = []
    for line in EXECUTIONS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_iso(ev.get("asof_execution"))
        if ts is None:
            continue
        et_offset = timedelta(hours=-4)
        et_date = (ts + et_offset).date().isoformat()
        if et_date == today_et_date:
            out.append(ev)
    return out


def load_yesterday_snapshot() -> dict | None:
    if not SNAPSHOT_DIR.exists():
        return None
    snaps = sorted(SNAPSHOT_DIR.glob("*-portfolio.json"))
    if not snaps:
        return None
    try:
        return json.loads(snaps[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None


def compute_position_pnl(
    positions: list[dict],
    marks: dict[str, float],
    prev_close: dict[str, float],
    nav_ref: float | None = None,
) -> list[dict]:
    """Mark-to-market each position, UNIT-AWARE.

    Two stored unit regimes share the positions table and MUST be valued
    differently — a single share-formula corrupts whichever regime it does not
    match (the 2026-06-02 unit-confusion class, ADR-0086):

      • TRUE-UNIT rows (asset_class == 'us_option'): Position.quantity is real
        signed contracts (ADR-0088 multi-leg path). Dollar value is
        mark × qty × 100 (the contract multiplier); unrealized = (mark-avg)×qty×100.
      • NAV-FRACTION rows (every other asset_class — the legacy equity path that
        writes the vast majority of state.db): Position.quantity is a SIGNED
        FRACTION OF NAV (0.20 = a 20%-of-NAV long). The documented
        get_marked_equity form (portfolio_state.py:805) is:
            unrealized = qty × nav_ref × (mark / avg - 1)
            market_value (signed notional) = qty × nav_ref
        Treating that 0.20 as 0.20 SHARES — the old (mark-avg)×qty — was off by
        ~avg×nav_ref/qty and reported meaningless dollar figures to Discord.

    nav_ref (cash.equity_total) is REQUIRED to value NAV-fraction rows. When it is
    unavailable, a NAV-fraction row's pnl fields are set to None — the report drops
    the row rather than emitting the share-formula lie (silence-by-default).
    """
    out = []
    for pos in positions:
        sym, qty, avg = pos["symbol"], pos["qty"], pos["avg_entry"]
        asset_class = pos.get("asset_class", "equity")
        true_unit = asset_class == "us_option"
        mult = _CONTRACT_MULTIPLIER if true_unit else 1.0
        mark = marks.get(sym)
        pcl = prev_close.get(sym)
        e = dict(pos)
        # NAV-fraction rows need a NAV reference; without one we cannot value them
        # honestly, so treat them as un-markable (None) rather than emit garbage.
        if mark is None or (not true_unit and nav_ref is None):
            e.update(
                {
                    "mark": mark if mark is not None else None,
                    "market_value": None,
                    "unrealized_pnl": None,
                    "unrealized_pct": None,
                    "today_pnl": None,
                    "today_pct": None,
                }
            )
        elif true_unit:
            unreal = (mark - avg) * qty * mult
            unreal_pct = (mark - avg) / avg * (1 if qty > 0 else -1) if avg else 0.0
            today_pnl = (mark - pcl) * qty * mult if pcl is not None else None
            today_pct = (
                (mark - pcl) / pcl * (1 if qty > 0 else -1)
                if pcl is not None and pcl
                else None
            )
            e.update(
                {
                    "mark": mark,
                    "market_value": mark * qty * mult,
                    "unrealized_pnl": unreal,
                    "unrealized_pct": unreal_pct,
                    "today_pnl": today_pnl,
                    "today_pct": today_pct,
                }
            )
        else:
            # NAV-fraction: the documented get_marked_equity dollar form.
            unreal = qty * nav_ref * (mark / avg - 1.0) if avg else None
            unreal_pct = (mark - avg) / avg * (1 if qty > 0 else -1) if avg else 0.0
            today_pnl = (
                qty * nav_ref * (mark / pcl - 1.0)
                if pcl is not None and pcl
                else None
            )
            today_pct = (
                (mark - pcl) / pcl * (1 if qty > 0 else -1)
                if pcl is not None and pcl
                else None
            )
            e.update(
                {
                    "mark": mark,
                    "market_value": qty * nav_ref,  # signed notional
                    "unrealized_pnl": unreal,
                    "unrealized_pct": unreal_pct,
                    "today_pnl": today_pnl,
                    "today_pct": today_pct,
                }
            )
        out.append(e)
    return out


def summarize(enriched: list[dict]) -> dict:
    total_unreal = sum(p["unrealized_pnl"] for p in enriched if p["unrealized_pnl"] is not None)
    total_today = sum(p["today_pnl"] for p in enriched if p["today_pnl"] is not None)
    long_count = sum(1 for p in enriched if p["qty"] > 0)
    short_count = sum(1 for p in enriched if p["qty"] < 0)
    long_mv = sum(
        p["market_value"] for p in enriched
        if p["market_value"] is not None and p["qty"] > 0
    )
    short_mv = sum(
        p["market_value"] for p in enriched
        if p["market_value"] is not None and p["qty"] < 0
    )
    marked = sum(1 for p in enriched if p["mark"] is not None)
    return {
        "n_positions": len(enriched),
        "n_marked": marked,
        "n_long": long_count,
        "n_short": short_count,
        "long_market_value": long_mv,
        "short_market_value": short_mv,
        "gross_exposure": long_mv + abs(short_mv),
        "net_exposure": long_mv + short_mv,
        "total_unrealized_pnl": total_unreal,
        "total_today_pnl": total_today,
    }


def fmt_pnl(amt: float | None) -> str:
    if amt is None:
        return "—"
    sign = "+" if amt >= 0 else ""
    return f"{sign}${amt:,.2f}"


def fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p*100:.2f}%"


def fmt_qty(q: float) -> str:
    if abs(q) < 0.01:
        return f"{q:+.4f}"
    if abs(q) < 1:
        return f"{q:+.2f}"
    return f"{q:+.1f}"


def emoji_pnl(amt: float | None) -> str:
    if amt is None:
        return "❓"
    if amt > 0:
        return "🟢"
    if amt < 0:
        return "🔴"
    return "⚪"


def render_compact(
    enriched: list[dict],
    summary: dict,
    fills_today: list[dict],
    yesterday: dict | None,
    asof: datetime,
    attachment_path: Path | None,
) -> str:
    """≤2000-char Discord message: headline + summary + tight tables."""
    lines: list[str] = []
    today_pnl = summary["total_today_pnl"]
    total_unreal = summary["total_unrealized_pnl"]
    today_em = emoji_pnl(today_pnl)

    lines.append(
        f"# {today_em} Portfolio — {asof.strftime('%a %Y-%m-%d')} (post-close)"
    )
    lines.append("")
    lines.append(
        f"**Today Δ:** {fmt_pnl(today_pnl)}   "
        f"**Open P&L:** {fmt_pnl(total_unreal)}"
    )
    if yesterday and "summary" in yesterday:
        prev_unreal = yesterday["summary"].get("total_unrealized_pnl")
        if prev_unreal is not None:
            dod = total_unreal - prev_unreal
            arrow = "📈" if dod > 0 else ("📉" if dod < 0 else "→")
            lines.append(
                f"**Δ vs yesterday:** {fmt_pnl(dod)} {arrow} "
                f"(was {fmt_pnl(prev_unreal)})"
            )
    lines.append(
        f"**Book:** {summary['n_long']}L / {summary['n_short']}S  "
        f"({summary['n_marked']}/{summary['n_positions']} marked)"
    )
    lines.append(
        f"**Exposure:** gross ${summary['gross_exposure']:,.0f}, "
        f"net ${summary['net_exposure']:+,.0f}"
    )
    lines.append(f"**Fills today:** {len(fills_today)}")
    lines.append("")

    today_ranked = sorted(
        [p for p in enriched if p.get("today_pnl") is not None],
        key=lambda p: -p["today_pnl"],
    )
    today_winners = [p for p in today_ranked if p["today_pnl"] > 0][:3]
    today_losers = [p for p in today_ranked if p["today_pnl"] < 0][-3:][::-1]

    if today_winners:
        lines.append("**🟢 Today's winners**")
        for p in today_winners:
            side = "L" if p["qty"] > 0 else "S"
            lines.append(
                f"  • {p['symbol']} ({side}): {fmt_pnl(p['today_pnl'])} "
                f"({fmt_pct(p['today_pct'])})"
            )
    if today_losers:
        lines.append("**🔴 Today's losers**")
        for p in today_losers:
            side = "L" if p["qty"] > 0 else "S"
            lines.append(
                f"  • {p['symbol']} ({side}): {fmt_pnl(p['today_pnl'])} "
                f"({fmt_pct(p['today_pct'])})"
            )
    if today_winners or today_losers:
        lines.append("")

    lifetime_ranked = sorted(
        [p for p in enriched if p.get("unrealized_pnl") is not None],
        key=lambda p: -p["unrealized_pnl"],
    )
    lifetime_winners = [p for p in lifetime_ranked if p["unrealized_pnl"] > 0][:3]
    lifetime_losers = [p for p in lifetime_ranked if p["unrealized_pnl"] < 0][-3:][::-1]
    if lifetime_winners:
        lines.append("**🏆 Lifetime winners**")
        for p in lifetime_winners:
            side = "L" if p["qty"] > 0 else "S"
            lines.append(
                f"  • {p['symbol']} ({side}): {fmt_pnl(p['unrealized_pnl'])} "
                f"({fmt_pct(p['unrealized_pct'])})"
            )
    if lifetime_losers:
        lines.append("**💔 Lifetime drawdowns**")
        for p in lifetime_losers:
            side = "L" if p["qty"] > 0 else "S"
            lines.append(
                f"  • {p['symbol']} ({side}): {fmt_pnl(p['unrealized_pnl'])} "
                f"({fmt_pct(p['unrealized_pct'])})"
            )

    if attachment_path is not None and attachment_path.exists():
        lines.append("")
        lines.append(
            f"_Full report attached. Marks via yfinance, paper account. "
            f"Educational analysis only — not financial advice._"
        )
        lines.append(f"MEDIA:{attachment_path}")
    else:
        lines.append("")
        lines.append(
            "_Marks via yfinance, paper account. Educational analysis only — "
            "not financial advice._"
        )
    return "\n".join(lines)


def render_full(
    enriched: list[dict],
    summary: dict,
    fills_today: list[dict],
    yesterday: dict | None,
    asof: datetime,
) -> str:
    """Full inline markdown report — used for verbose flag and attachment file."""
    lines: list[str] = []
    today_pnl = summary["total_today_pnl"]
    total_unreal = summary["total_unrealized_pnl"]
    today_em = emoji_pnl(today_pnl)

    lines.append(
        f"# {today_em} Hermes-Quant Daily Portfolio — "
        f"{asof.strftime('%a %Y-%m-%d')} (post-close)"
    )
    lines.append("")
    lines.append(f"**Today's mark-to-market Δ:** {fmt_pnl(today_pnl)}")
    lines.append(f"**Total unrealized P&L (open book):** {fmt_pnl(total_unreal)}")
    if yesterday and "summary" in yesterday:
        prev_unreal = yesterday["summary"].get("total_unrealized_pnl")
        if prev_unreal is not None:
            dod = total_unreal - prev_unreal
            arrow = "📈" if dod > 0 else ("📉" if dod < 0 else "→")
            lines.append(
                f"**Δ vs yesterday:** {fmt_pnl(dod)} {arrow} "
                f"(was {fmt_pnl(prev_unreal)})"
            )
    lines.append("")
    lines.append(
        f"**Positions:** {summary['n_long']} LONG / "
        f"{summary['n_short']} SHORT  "
        f"({summary['n_marked']}/{summary['n_positions']} marked)"
    )
    lines.append(
        f"**Gross exposure:** ${summary['gross_exposure']:,.0f}  "
        f"**Net:** ${summary['net_exposure']:+,.0f}  "
        f"(long ${summary['long_market_value']:,.0f} / "
        f"short ${summary['short_market_value']:,.0f})"
    )
    lines.append(f"**New fills today:** {len(fills_today)}")
    lines.append("")

    # Today's movers (5 each)
    today_ranked = sorted(
        [p for p in enriched if p.get("today_pnl") is not None],
        key=lambda p: -p["today_pnl"],
    )
    today_winners = [p for p in today_ranked if p["today_pnl"] > 0][:5]
    today_losers = [p for p in today_ranked if p["today_pnl"] < 0][-5:][::-1]
    if today_winners or today_losers:
        lines.append("## 📊 Today's movers")
        lines.append("")
        if today_winners:
            lines.append("**🟢 Winners**")
            lines.append("")
            lines.append("| Symbol | Side | Qty | Mark | Today Δ | Today % |")
            lines.append("|---|---|---|---|---|---|")
            for p in today_winners:
                side = "LONG" if p["qty"] > 0 else "SHORT"
                lines.append(
                    f"| {p['symbol']} | {side} | {fmt_qty(p['qty'])} | "
                    f"${p['mark']:.2f} | {fmt_pnl(p['today_pnl'])} | "
                    f"{fmt_pct(p['today_pct'])} |"
                )
            lines.append("")
        if today_losers:
            lines.append("**🔴 Losers**")
            lines.append("")
            lines.append("| Symbol | Side | Qty | Mark | Today Δ | Today % |")
            lines.append("|---|---|---|---|---|---|")
            for p in today_losers:
                side = "LONG" if p["qty"] > 0 else "SHORT"
                lines.append(
                    f"| {p['symbol']} | {side} | {fmt_qty(p['qty'])} | "
                    f"${p['mark']:.2f} | {fmt_pnl(p['today_pnl'])} | "
                    f"{fmt_pct(p['today_pct'])} |"
                )
            lines.append("")

    # Lifetime winners / drawdowns
    lifetime_ranked = sorted(
        [p for p in enriched if p.get("unrealized_pnl") is not None],
        key=lambda p: -p["unrealized_pnl"],
    )
    lifetime_winners = [p for p in lifetime_ranked if p["unrealized_pnl"] > 0][:5]
    lifetime_losers = [p for p in lifetime_ranked if p["unrealized_pnl"] < 0][-5:][::-1]
    if lifetime_winners or lifetime_losers:
        lines.append("## 💰 Lifetime open-book P&L")
        lines.append("")
        if lifetime_winners:
            lines.append("**🏆 Top winners**")
            lines.append("")
            lines.append("| Symbol | Side | Avg Entry | Mark | Unrealized | % |")
            lines.append("|---|---|---|---|---|---|")
            for p in lifetime_winners:
                side = "LONG" if p["qty"] > 0 else "SHORT"
                lines.append(
                    f"| {p['symbol']} | {side} | ${p['avg_entry']:.2f} | "
                    f"${p['mark']:.2f} | {fmt_pnl(p['unrealized_pnl'])} | "
                    f"{fmt_pct(p['unrealized_pct'])} |"
                )
            lines.append("")
        if lifetime_losers:
            lines.append("**💔 Worst drawdowns**")
            lines.append("")
            lines.append("| Symbol | Side | Avg Entry | Mark | Unrealized | % |")
            lines.append("|---|---|---|---|---|---|")
            for p in lifetime_losers:
                side = "LONG" if p["qty"] > 0 else "SHORT"
                lines.append(
                    f"| {p['symbol']} | {side} | ${p['avg_entry']:.2f} | "
                    f"${p['mark']:.2f} | {fmt_pnl(p['unrealized_pnl'])} | "
                    f"{fmt_pct(p['unrealized_pct'])} |"
                )
            lines.append("")

    # Full position table
    if enriched:
        lines.append(f"## 📋 Full positions ({len(enriched)})")
        lines.append("")
        lines.append("| Symbol | Side | Qty | Avg Entry | Mark | Today Δ | Unreal | % |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for p in sorted(enriched, key=lambda x: x["symbol"]):
            side = "LONG" if p["qty"] > 0 else "SHORT"
            mark_str = f"${p['mark']:.2f}" if p["mark"] is not None else "—"
            lines.append(
                f"| {p['symbol']} | {side} | {fmt_qty(p['qty'])} | "
                f"${p['avg_entry']:.2f} | {mark_str} | "
                f"{fmt_pnl(p['today_pnl'])} | {fmt_pnl(p['unrealized_pnl'])} | "
                f"{fmt_pct(p['unrealized_pct'])} |"
            )
        lines.append("")

    # Fills today
    if fills_today:
        lines.append(f"## 🆕 New fills today ({len(fills_today)})")
        lines.append("")
        lines.append("| Time (UTC) | Symbol | Side | Size | Price |")
        lines.append("|---|---|---|---|---|")
        for f in sorted(fills_today, key=lambda e: e.get("asof_execution", "")):
            ts = f.get("asof_execution", "?")[:16].replace("T", " ")
            sym = f.get("asset", "?")
            pct = f.get("fill_size_pct", 0)
            side = "LONG" if pct > 0 else ("SHORT" if pct < 0 else "FLAT")
            size = f"{abs(pct)*100:.0f}%"
            price = f.get("fill_price", "?")
            price_str = f"${price:.2f}" if isinstance(price, (int, float)) else str(price)
            lines.append(f"| {ts} | {sym} | {side} | {size} | {price_str} |")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Marks via yfinance. Data is point-in-time; refresh by re-running. "
        "Educational research analysis only — not financial advice._"
    )
    return "\n".join(lines)


def persist_snapshot(
    enriched: list[dict], summary: dict, fills_today: list[dict], asof: datetime
) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{asof.strftime('%Y%m%d')}-portfolio.json"
    payload = {
        "asof_utc": asof.isoformat(),
        "asof_et_date": (asof + timedelta(hours=-4)).date().isoformat(),
        "account": "paper-default",
        "summary": summary,
        "positions": enriched,
        "fills_today_count": len(fills_today),
    }
    path = SNAPSHOT_DIR / fname
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def write_full_report_attachment(report: str, asof: datetime) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{asof.strftime('%Y%m%d')}-portfolio.md"
    path = SNAPSHOT_DIR / fname
    path.write_text(report)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full inline report (no MEDIA: attachment).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip snapshot persistence (for on-demand pulls).",
    )
    parser.add_argument(
        "--account",
        default="paper-default",
        help="state.db account_id (default: paper-default).",
    )
    args = parser.parse_args()

    asof = utcnow()
    et_date = (asof + timedelta(hours=-4)).date().isoformat()

    positions = load_positions(account=args.account)
    fills_today = load_fills_today(et_date)

    if not positions and not fills_today:
        return 0

    symbols = sorted({p["symbol"] for p in positions})
    marks, prev_close = load_marks(symbols)
    nav_ref = load_nav_ref(account=args.account)
    enriched = compute_position_pnl(positions, marks, prev_close, nav_ref)
    summary = summarize(enriched)
    yesterday = load_yesterday_snapshot()

    full_report = render_full(enriched, summary, fills_today, yesterday, asof)

    if args.verbose:
        # Verbose: dump everything inline, no attachment.
        print(full_report)
    else:
        # Cron path: write full report to file, emit compact summary with MEDIA: tag.
        attachment_path = None
        if not args.dry_run:
            try:
                attachment_path = write_full_report_attachment(full_report, asof)
            except Exception as e:
                print(f"⚠️  Attachment write failed: {e}", file=sys.stderr)
        compact = render_compact(
            enriched, summary, fills_today, yesterday, asof, attachment_path
        )
        print(compact)

    if not args.dry_run:
        try:
            persist_snapshot(enriched, summary, fills_today, asof)
        except Exception as e:
            print(f"⚠️  Snapshot persistence failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
