#!/usr/bin/env python3
"""quant-strategy-retro-weekly.py — Sunday weekly P&L retrospection.

Schedule: `0 13 * * 0` (Sundays 13:00 PT = 16:00 ET, post-week-close).
Deliver:  origin (Discord thread).
Mode:     no_agent=True.

Reads ~/.hermes/quant/executions.jsonl + state.db.positions + the latest
hourly snapshot for marks, computes per-layer / per-direction / per-symbol
realized + unrealized P&L over the trailing 7 calendar days, and emits a
markdown report.

Layer attribution heuristic (since executions.jsonl has no play_tag):
  - Advisor-layer fires: proposal_id matches `prop_YYYYMMDDTHHMMSS_*` AND
    timestamp aligns with the daily-interim cron windows (05:30, 08:00, 12:30 PT).
  - Playbook-tick fires: proposal_id from playbook tick journal lookup.
  - Autonomous-tick fires: proposal_id from autonomous-tick.jsonl lookup.
  - Unknown: anything else.

Limitations honored:
  - Multi-leg options not yet shipped → all current fills are equity. The
    retrospection treats every fill as equity directional P&L.
  - Reflector is gated on HERMES_QUANT_REFLECTION=1 and reflections.jsonl
    is currently empty → reflection-quality stats degrade gracefully when
    no reflections exist (skipped section).

Posture: READ-ONLY. No state mutation. Silence-by-default if the trailing
7-day window has zero fills (e.g. system was down all week).
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

QUANT_DIR = Path.home() / ".hermes" / "quant"
EXECUTIONS_PATH = QUANT_DIR / "executions.jsonl"
REFLECTIONS_PATH = QUANT_DIR / "memory" / "reflections.jsonl"
STATE_DB_PATH = QUANT_DIR / "state.db"
HOURLY_SNAP_DIR = QUANT_DIR / "hourly-snapshots"
TICK_JOURNAL_PATH = QUANT_DIR / "playbook" / "tick-journal.jsonl"
AUTONOMOUS_TICK_PATH = QUANT_DIR / "autonomous-tick.jsonl"

# US equity-option contract multiplier (shares controlled per contract). Mirrors
# hermes_quant.state.portfolio_state._CONTRACT_MULTIPLIER so the true-unit
# (ADR-0088 multi-leg) option valuation here matches the ledger fold; a us_option
# position's dollar value is mark × contracts × 100.
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


def load_executions(window_start: datetime) -> list[dict]:
    """Load executions.jsonl entries with asof_execution >= window_start."""
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
        if ts is None or ts < window_start:
            continue
        out.append(ev)
    return out


def load_reflections() -> list[dict]:
    if not REFLECTIONS_PATH.exists():
        return []
    out: list[dict] = []
    for line in REFLECTIONS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_positions() -> list[tuple]:
    """Return current positions from state.db.

    Shape: ``[(symbol, quantity, avg_entry_price, asset_class), ...]``.

    ``asset_class`` is the only regime marker available at report time and is
    required to value rows honestly (see ``compute_unrealized_pnl``):
    ``us_option`` rows are TRUE-UNIT signed contracts (ADR-0088); every other
    class is the legacy NAV-FRACTION equity path (``quantity`` is a signed
    fraction of NAV, NOT shares). A missing/empty asset_class defaults to the
    NAV-fraction equity path. Backward-compatible: callers that unpack the first
    three elements still work, but the in-tree caller now consumes the 4th.
    """
    if not STATE_DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        rows = conn.execute(
            "SELECT symbol, quantity, avg_entry_price, asset_class FROM positions"
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def load_nav_ref() -> float | None:
    """NAV reference for valuing NAV-fraction rows (cash.equity_total).

    Mirrors ``PortfolioState.get_marked_equity`` (and the daily sibling
    quant-portfolio-daily.py), which sizes each NAV-fraction position against
    cash.equity_total (cost-basis equity). Returns None when no finite, positive
    cash row exists so the caller can drop NAV-fraction rows rather than value a
    fraction as if it were shares.
    """
    if not STATE_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        row = conn.execute(
            "SELECT equity_total FROM cash WHERE account_id = ?",
            ("paper-default",),
        ).fetchone()
        conn.close()
    except Exception:
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


def load_latest_marks(symbols: list[str]) -> dict[str, float]:
    """Fetch current marks for the given symbol list via yfinance.

    Falls back to the latest hourly snapshot positions if yfinance is unavailable.
    """
    marks: dict[str, float] = {}
    if not symbols:
        return marks

    # yfinance batch fetch — fast for ~50 symbols
    try:
        import yfinance as yf
        # Use `Tickers` for batch
        tickers_str = " ".join(symbols)
        data = yf.Tickers(tickers_str)
        for sym in symbols:
            try:
                tk = data.tickers.get(sym)
                if tk is None:
                    continue
                # fast_info is faster than info
                px = None
                fi = getattr(tk, "fast_info", None)
                if fi is not None:
                    px = fi.get("last_price") or fi.get("lastPrice") or fi.get("regular_market_price")
                if not px:
                    info = tk.info or {}
                    px = info.get("regularMarketPrice") or info.get("currentPrice")
                if px:
                    marks[sym] = float(px)
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: hourly snapshot
    if not marks:
        snaps = sorted(HOURLY_SNAP_DIR.glob("*.json"), reverse=True) if HOURLY_SNAP_DIR.exists() else []
        if snaps:
            try:
                data = json.loads(snaps[0].read_text())
                for p in data.get("positions", []) or []:
                    sym = p.get("symbol")
                    px = p.get("current_price")
                    if not px and p.get("market_value") and p.get("qty"):
                        try:
                            px = float(p["market_value"]) / float(p["qty"])
                        except (TypeError, ValueError, ZeroDivisionError):
                            px = None
                    if sym and px:
                        try:
                            marks[str(sym)] = float(px)
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass
    return marks


def attribute_layer(exec_row: dict) -> str:
    """Heuristic: which layer fired this execution?

    Advisor-layer proposal_ids look like `prop_YYYYMMDDTHHMMSS_SYM_xxxx` and
    the HHMMSS timestamps cluster at 12:34 (05:30 PT advisor) / 19:00 (12:00
    EOD) / 17:09 (10:09 PT — operator manual fire) etc. We detect via
    proposal_id prefix patterns first, fall back to "unknown".

    Future: when ADR-0029 lands, multi-leg fills will have explicit
    `play_tag` in reactor_metadata; consume that directly.
    """
    pid = exec_row.get("proposal_id", "")
    if not pid.startswith("prop_"):
        return "unknown"
    # All current fires go through proposals.db, which is advisor-layer.
    # Differentiation between advisor / playbook / autonomous would require
    # joining via the source journals — defer until play_tag is plumbed.
    return "advisor"


def attribute_direction(exec_row: dict) -> str:
    pct = exec_row.get("fill_size_pct") or exec_row.get("target_position_pct") or 0
    if pct > 0:
        return "LONG"
    if pct < 0:
        return "SHORT"
    return "FLAT"


def compute_unrealized_pnl(
    positions: list[tuple], marks: dict[str, float], nav_ref: float | None = None
) -> dict[str, dict]:
    """Per-symbol unrealized P&L from current positions vs latest marks, UNIT-AWARE.

    Returns: {symbol: {qty, avg_entry, mark, unrealized_pnl, unrealized_pct}}

    Two stored unit regimes share the positions table and MUST be valued
    differently — a single share-formula corrupts whichever regime it does not
    match (the 2026-06-02 unit-confusion class, ADR-0086; the daily sibling
    quant-portfolio-daily.py was fixed for this in ar60 but this weekly retro was
    not, leaving a fictional "Total unrealized" headline on the LIVE Sunday cron):

      • TRUE-UNIT rows (asset_class == 'us_option'): ``quantity`` is real signed
        contracts (ADR-0088 multi-leg path). Dollar value is
        mark × qty × 100 (the contract multiplier);
        unrealized = (mark - avg) × qty × 100.
      • NAV-FRACTION rows (every other asset_class — the legacy equity path that
        writes the vast majority of state.db): ``quantity`` is a SIGNED FRACTION
        OF NAV (0.20 = a 20%-of-NAV long). The documented get_marked_equity form
        (portfolio_state.py) is:  unrealized = qty × nav_ref × (mark / avg - 1).
        Treating that 0.20 as 0.20 SHARES — the old (mark-avg)×qty — was off by
        ~avg×nav_ref/qty and reported a meaningless dollar figure to Discord.

    ``nav_ref`` (cash.equity_total) is REQUIRED to value NAV-fraction rows. When
    it is unavailable, a NAV-fraction row is dropped rather than emitting the
    share-formula lie (silence-by-default).

    Accepts both the 4-tuple (symbol, qty, avg, asset_class) state.db read shape
    and the legacy 3-tuple (asset_class then defaults to the NAV-fraction equity
    path) for backward compatibility.
    """
    out = {}
    for row in positions:
        sym = str(row[0])
        qty = row[1]
        avg_entry = row[2]
        asset_class = str(row[3]) if len(row) > 3 and row[3] else "equity"
        if not qty or not avg_entry:
            continue
        mark = marks.get(sym)
        if mark is None:
            # No mark — skip; user can verify by symbol later.
            continue
        qty_f = float(qty)
        avg_f = float(avg_entry)
        mark_f = float(mark)
        true_unit = asset_class == "us_option"
        # NAV-fraction rows need a NAV reference; without one we cannot value them
        # honestly, so drop them rather than emit a share-formula garbage figure.
        if not true_unit and nav_ref is None:
            continue
        if true_unit:
            unreal = (mark_f - avg_f) * qty_f * _CONTRACT_MULTIPLIER
        else:
            # NAV-fraction: the documented get_marked_equity dollar form.
            unreal = qty_f * nav_ref * (mark_f / avg_f - 1.0)
        # Direction-aware %: SHORT positions (qty < 0) profit when mark < avg_entry.
        unreal_pct = (mark_f - avg_f) / avg_f * (1 if qty_f > 0 else -1)
        out[sym] = {
            "qty": qty_f,
            "avg_entry": avg_f,
            "mark": mark_f,
            "unrealized_pnl": unreal,
            "unrealized_pct": unreal_pct,
        }
    return out


def format_pnl(amt: float) -> str:
    sign = "+" if amt >= 0 else ""
    return f"{sign}${amt:,.2f}"


def format_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p*100:.2f}%"


def build_report(
    executions: list[dict],
    reflections: list[dict],
    positions: list[tuple],
    unrealized_by_symbol: dict[str, dict],
    window_days: int,
) -> str:
    """Markdown report for Discord delivery."""
    lines: list[str] = []
    lines.append(
        f"# 📊 Hermes-Quant Weekly Strategy Retrospection — "
        f"{utcnow().strftime('%a %Y-%m-%d')}"
    )
    lines.append("")
    lines.append(f"**Window:** trailing {window_days} days")
    lines.append(f"**Fills in window:** {len(executions)}")
    lines.append(f"**Open positions:** {len(positions)}")
    lines.append(f"**Reflections logged in window:** {len(reflections)}")
    lines.append("")

    if not executions:
        lines.append("> _No fills in the trailing window — system silence is healthy if the regime gate / advisor is silencing universe-wide. Otherwise, investigate cron health._")
        lines.append("")
        return "\n".join(lines)

    # --- by layer --- #
    lines.append("## 🔌 By layer")
    layer_counts = Counter(attribute_layer(e) for e in executions)
    lines.append("")
    lines.append("| Layer | Fills |")
    lines.append("|---|---|")
    for layer, n in sorted(layer_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {layer} | {n} |")
    lines.append("")
    lines.append(
        "_Note: until `play_tag` is plumbed (deferred with ADR-0029), all "
        "advisor-layer fires read as `advisor`. Playbook + autonomous-tick "
        "fires aren't yet distinguishable here._"
    )
    lines.append("")

    # --- by direction --- #
    lines.append("## 🎯 By direction")
    dir_counts = Counter(attribute_direction(e) for e in executions)
    lines.append("")
    lines.append("| Direction | Fills |")
    lines.append("|---|---|")
    for d, n in sorted(dir_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {d} | {n} |")
    lines.append("")

    # --- top symbols by fill count --- #
    sym_counts = Counter(e.get("asset", "?") for e in executions)
    if sym_counts:
        lines.append("## 🔝 Top symbols by fill count (window)")
        lines.append("")
        lines.append("| Symbol | Fills |")
        lines.append("|---|---|")
        for sym, n in sym_counts.most_common(10):
            lines.append(f"| {sym} | {n} |")
        lines.append("")

    # --- unrealized P&L: top winners and losers from current positions --- #
    if unrealized_by_symbol:
        sorted_pnl = sorted(
            unrealized_by_symbol.items(), key=lambda kv: -kv[1]["unrealized_pnl"]
        )
        winners = [kv for kv in sorted_pnl if kv[1]["unrealized_pnl"] > 0][:5]
        losers = [kv for kv in sorted_pnl if kv[1]["unrealized_pnl"] < 0][-5:][::-1]
        total_unreal = sum(v["unrealized_pnl"] for v in unrealized_by_symbol.values())

        lines.append("## 💰 Open positions P&L (mark-to-market)")
        lines.append("")
        lines.append(f"**Total unrealized:** {format_pnl(total_unreal)}")
        lines.append(
            f"**Symbols marked:** {len(unrealized_by_symbol)}/{len(positions)} "
            f"(others lacked a current mark)"
        )
        lines.append("")
        if winners:
            lines.append("### Top winners")
            lines.append("")
            lines.append("| Symbol | Qty | Avg Entry | Mark | Unrealized | % |")
            lines.append("|---|---|---|---|---|---|")
            for sym, v in winners:
                lines.append(
                    f"| {sym} | {v['qty']:+.2f} | ${v['avg_entry']:.2f} | "
                    f"${v['mark']:.2f} | {format_pnl(v['unrealized_pnl'])} | "
                    f"{format_pct(v['unrealized_pct'])} |"
                )
            lines.append("")
        if losers:
            lines.append("### Top losers")
            lines.append("")
            lines.append("| Symbol | Qty | Avg Entry | Mark | Unrealized | % |")
            lines.append("|---|---|---|---|---|---|")
            for sym, v in losers:
                lines.append(
                    f"| {sym} | {v['qty']:+.2f} | ${v['avg_entry']:.2f} | "
                    f"${v['mark']:.2f} | {format_pnl(v['unrealized_pnl'])} | "
                    f"{format_pct(v['unrealized_pct'])} |"
                )
            lines.append("")

    # --- reflections section (degrades gracefully) --- #
    lines.append("## 🪞 Reflections")
    lines.append("")
    if not reflections:
        lines.append(
            "_No reflections logged. The Reflector is gated on "
            "`HERMES_QUANT_REFLECTION=1`; if you want post-trade reflections "
            "feeding this report, set the env var on the auto-approve path._"
        )
        lines.append("")
    else:
        # Bucket by outcome_quality (1-5)
        quality = Counter(r.get("outcome_quality") for r in reflections)
        lines.append("| Outcome quality (1-5) | Count |")
        lines.append("|---|---|")
        for q in sorted(quality.keys(), key=lambda x: (x is None, x)):
            lines.append(f"| {q} | {quality[q]} |")
        lines.append("")
        # Top lesson categories if present
        cats = Counter(r.get("lesson_category", "uncategorized") for r in reflections)
        if cats:
            lines.append("### Top lesson categories")
            lines.append("")
            for cat, n in cats.most_common(5):
                lines.append(f"- **{cat}**: {n}")
            lines.append("")

    # --- next-week recommendation framing --- #
    lines.append("---")
    lines.append("")
    lines.append("## 🎯 Next-week framing")
    lines.append("")
    lines.append(
        "**Until ADR-0029 lands:** all fills are equity-direction trades from "
        "the advisor layer. Covered_call / CSP / wheel / LEAPS are scored + "
        "regime-gated, but don't fire because the multi-leg execution rail "
        "isn't built yet. This report's per-strategy breakdown will become "
        "meaningful once that lands."
    )
    lines.append("")
    lines.append(
        "**To improve report fidelity:** set `HERMES_QUANT_REFLECTION=1` on "
        "the cron prompts so the Reflector populates `reflections.jsonl`. "
        "This adds outcome_quality bucketing + LLM-extracted lesson categories."
    )
    lines.append("")
    lines.append(
        "_Disclaimer: educational analysis only, not financial advice. This "
        "is a paper-trading research system._"
    )
    return "\n".join(lines)


def main() -> int:
    window_days = 7
    window_start = utcnow() - timedelta(days=window_days)

    executions = load_executions(window_start)
    reflections_all = load_reflections()
    reflections = [
        r
        for r in reflections_all
        if (parse_iso(r.get("asof_resolution")) or utcnow()) >= window_start
    ]
    positions = load_positions()
    position_symbols = sorted(set(str(r[0]) for r in positions if r[0]))
    marks = load_latest_marks(position_symbols)
    nav_ref = load_nav_ref()
    unrealized = compute_unrealized_pnl(positions, marks, nav_ref=nav_ref)

    # Silence-by-default: if zero fills AND zero open positions, emit nothing.
    if not executions and not positions:
        return 0

    report = build_report(
        executions=executions,
        reflections=reflections,
        positions=positions,
        unrealized_by_symbol=unrealized,
        window_days=window_days,
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
