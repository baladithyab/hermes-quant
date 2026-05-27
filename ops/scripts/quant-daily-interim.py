#!/usr/bin/env python3
"""
quant-daily-interim.py — INTERIM daily picker (Phase 0.2 of 2026-05-23 plan).

This is intentionally minimal: it scans the universe with the existing
hermes-quant advisor (equity directional bias only, no options yet) and
prints a brief that the cron LLM wrapper formats for Discord.

The full options picker (covered call / CSP / wheel / LEAPS / swing) lands
in Phase 4 Wave C and replaces this script. Until then this gives Codeseys
a daily artifact while we build the real thing.

Posture preserved from AGENTS.md:
- READ-ONLY (no orders, no state mutation)
- Silence-by-default (skips symbols with insufficient bars or stale data)
- Money never goes through tools (this script prints text; the LLM
  wrapper sends it; user approves any actual trade in HITL)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make sure we use the hermes-agent venv where hermes-quant is installed.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

UNIVERSE_PATH = Path.home() / ".hermes" / "scripts" / "quant-universe-interim.txt"
WATCHLIST_PATH = Path.home() / ".hermes" / "quant" / "watchlist" / "play-fit.json"


def load_universe() -> list[tuple[str, str, str]]:
    """Load the universe of symbols to score.

    Strategy (post-2026-05-26 watchlist-shadow fix):
      - Read the txt fallback first as the BASELINE universe (~38 symbols).
      - Then merge in active watchlist symbols, preserving their tier
        (defaults to "active" so they get scanned every day, not just EOD).
      - Symbols already in the txt fallback keep their txt tier; new
        symbols from the watchlist are appended as "active".

    This avoids the regression where a sparse watchlist (e.g. 7 symbols
    from a single onboarding wave) silently shadowed the 38-symbol
    txt-defined universe. Union semantics: txt is the floor, watchlist
    adds to it.

    Returns: list of (ticker, asset_class, tier).
      asset_class is always 'midcap' (we don't differentiate downstream).
      tier is 'active' for watchlist symbols, or per-row for the txt file.
    """
    # Baseline: txt fallback.
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    if UNIVERSE_PATH.exists():
        for line in UNIVERSE_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            ticker, klass, tier = parts[0].upper(), parts[1], parts[2]
            if ticker not in seen:
                seen.add(ticker)
                rows.append((ticker, klass, tier))

    # Merge: append active watchlist symbols not already present.
    if WATCHLIST_PATH.exists():
        try:
            data = json.loads(WATCHLIST_PATH.read_text())
            for play, entries in (data.get("plays") or {}).items():
                for entry in entries:
                    if entry.get("state") != "active":
                        continue
                    sym = (entry.get("symbol") or "").upper()
                    if sym and sym not in seen:
                        seen.add(sym)
                        rows.append((sym, "midcap", "active"))
        except Exception:
            # Watchlist parse failure is non-fatal — txt baseline is fine.
            pass

    return rows


def recommend_one(symbol: str, asset_class: str = "equity", timeframe: str = "1d") -> dict:
    """Wrap advisor.recommend with safe error handling.

    advisor.recommend returns a dict shaped like:
        {symbol, asset_class, timeframe, as_of, recipe, data_quality,
         analyst_views: list, aggregated_signal: dict|None,
         risk_gate: {pass, gated_reason, kelly_fraction, recommended_action},
         lessons, caveats, doctor}
    """
    try:
        from hermes_quant.advisor import recommend
        result = recommend(symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        agg = result.get("aggregated_signal") or {}
        gate = result.get("risk_gate") or {}
        dq = result.get("data_quality") or {}
        # Coerce dataclasses → dict if needed
        if hasattr(agg, "__dict__") and not isinstance(agg, dict):
            agg = {f: getattr(agg, f, None)
                   for f in ("direction", "confidence", "magnitude", "horizon")}
        return {
            "symbol": symbol,
            "ok": True,
            "asset_class": asset_class,
            "data_ok": bool(dq.get("bars_received", 0) > 0),
            "data_quality": dq,
            "direction": agg.get("direction") if isinstance(agg, dict) else None,
            "confidence": agg.get("confidence") if isinstance(agg, dict) else None,
            "magnitude": agg.get("magnitude") if isinstance(agg, dict) else None,
            "horizon": agg.get("horizon") if isinstance(agg, dict) else None,
            "kelly_fraction": gate.get("kelly_fraction"),
            "gate_pass": gate.get("pass", False),
            "gated_reason": gate.get("gated_reason"),
            "recommended_action": gate.get("recommended_action"),
            "rationale": (result.get("caveats", [None]) or [None])[0],
            "lessons": result.get("lessons", []),
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "ok": False,
            "asset_class": asset_class,
            "error": f"{type(exc).__name__}: {exc}",
            "trace_short": traceback.format_exc().splitlines()[-1] if traceback.format_exc() else None,
        }


def rank_picks(views: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Sort views into actionable / silent / data_blocked / failed.

    actionable    = ok + data_ok + risk_gate passed (recommended_action != gated)
    silent        = ok + data_ok + gate said hold cash (low conviction or rules tripped)
    data_blocked  = ok-but-no-data (weekend, listing changes, yfinance hiccup) — not an error
    failed        = exception during recommend()
    """
    actionable, silent, data_blocked, failed = [], [], [], []
    for v in views:
        if not v.get("ok"):
            failed.append(v)
            continue
        if not v.get("data_ok"):
            data_blocked.append(v)
            continue
        if v.get("gate_pass") and v.get("direction") in (-1, 1):
            actionable.append(v)
        else:
            silent.append(v)

    def score(v):
        c = v.get("confidence")
        return 0.0 if c is None else abs(c - 0.5)

    actionable.sort(key=score, reverse=True)
    silent.sort(key=score, reverse=True)
    return actionable, silent, data_blocked, failed


def format_brief(actionable: list[dict], silent: list[dict],
                 data_blocked: list[dict], failed: list[dict],
                 universe_size: int) -> str:
    now = datetime.now(timezone.utc).astimezone()
    lines = []
    lines.append(f"# 📊 Hermes-Quant Daily Brief — {now.strftime('%a %Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append("> ⚠️ **Interim build** — equity directional bias only. "
                 "Options layer (covered calls, CSP, wheel, LEAPS, swings) lands later this week.")
    lines.append("")
    lines.append(f"**Universe scanned:** {universe_size}  •  "
                 f"**Actionable:** {len(actionable)}  •  "
                 f"**Silent (low conviction):** {len(silent)}  •  "
                 f"**Data-blocked:** {len(data_blocked)}  •  "
                 f"**Errors:** {len(failed)}")
    lines.append("")

    if actionable:
        lines.append("## 🎯 Top picks (HITL — reply `approve <ticker>` to paper-fire)")
        lines.append("")
        for v in actionable[:5]:
            d = v.get("direction")
            arrow = "🟢 LONG" if d == 1 else "🔴 SHORT" if d == -1 else "⚪ FLAT"
            conf = v.get("confidence")
            mag = v.get("magnitude")
            kelly = v.get("kelly_fraction")
            rat = v.get("rationale") or ""
            lines.append(f"- **{v['symbol']}** {arrow}  "
                         f"conf={conf:.2f} mag={mag:+.3%} "
                         f"kelly={kelly:.3f} horizon={v.get('horizon','?')}")
            if rat:
                lines.append(f"  - {rat}")
        lines.append("")

    if silent:
        top_silent = ", ".join(f"{v['symbol']}({v.get('confidence',0):.2f})" for v in silent[:8])
        lines.append(f"## 💤 Watching (no conviction): {top_silent}")
        lines.append("")

    if data_blocked:
        blocked_syms = ", ".join(v["symbol"] for v in data_blocked[:15])
        lines.append(f"## 🚧 Data-blocked ({len(data_blocked)}, weekend / provider): {blocked_syms}")
        lines.append("")

    if failed:
        lines.append(f"## ⚠️ Errors ({len(failed)})")
        for v in failed[:5]:
            lines.append(f"- {v['symbol']}: {v.get('error','unknown')}")
        lines.append("")

    lines.append("---")
    lines.append("_Disclaimer: educational analysis only, not financial advice. "
                 "This is a paper-trading research system._")
    return "\n".join(lines)


def main() -> int:
    universe = load_universe()
    # Active tier scanned every day; watch tier only at EOD or when --eod is passed
    is_eod = "--eod" in sys.argv
    rows = [r for r in universe if r[2] == "active" or (is_eod and r[2] == "watch")]
    views = [recommend_one(t, "equity", "1d") for t, _klass, _tier in rows]
    actionable, silent, data_blocked, failed = rank_picks(views)
    brief = format_brief(actionable, silent, data_blocked, failed, len(rows))
    # Print to stdout — the cron LLM wrapper picks this up
    print(brief)
    # Also persist a structured copy for later retro-loop ingestion
    out_dir = Path.home() / ".hermes" / "quant" / "daily-briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}-interim.json").write_text(json.dumps(
        {"actionable": actionable, "silent": silent,
         "data_blocked": data_blocked, "failed": failed,
         "universe_size": len(rows), "is_eod": is_eod},
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
