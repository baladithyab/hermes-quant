#!/usr/bin/env python3
"""quant-hourly-tick.py — hourly market-hours health + performance + conditions check.

Schedule: `0 7-13 * * 1-5` (gateway host PT) = 10:00 AM - 4:00 PM ET hourly, 7 ticks/day.
Deliver:  origin (this Discord thread, where hermes-quant updates land).
Mode:     no_agent=True → empty stdout = SILENT; non-empty stdout delivered verbatim.

Each tick:
  1. Snapshot Alpaca paper state: /v2/account, /v2/positions, /v2/orders, /v2/clock
  2. Snapshot market conditions: SPY, VIX latest + day change (yfinance)
  3. Snapshot universe-of-interest: positions + last-actionable symbols, latest mark + day change
  4. Persist to ~/.hermes/quant/hourly-snapshots/<utc-ts>-hourly.json
  5. Compare to previous snapshot, detect: new fills, drawdown crossings,
     per-position stop breaches, VIX/SPY regime shifts.
  6. Emit:
     - First market-day tick → heartbeat
     - Threshold breach / new fill / regime shift → alert
     - Otherwise → silent (empty stdout)

Posture: DETERMINISTIC, no LLM. READ-ONLY against Alpaca. Silence-by-default.

ADR refs: ADR-0001 (sidecar/reproducibility), ADR-0004 (deterministic risk gate +
0.5%/5% drawdown halts), ADR-0014 (advisor surface), ADR-0027 (options-aware risk
gate — kill-switch hooks land here in a future revision).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# ---------- creds ----------
SECRETS_PATH = Path.home() / ".hermes" / "secrets" / "alpaca.env"


def _load_creds() -> dict[str, str]:
    out = {}
    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, v = line[len("export "):].split("=", 1)
                out[k] = v.strip().strip('"').strip("'")
    return out


CREDS = _load_creds()
KEY = CREDS["ALPACA_API_KEY_ID"]
SECRET = CREDS["ALPACA_API_SECRET_KEY"]
TRADING = CREDS["ALPACA_BASE_URL"]  # paper-api/v2

# ---------- paths ----------
SNAP_DIR = Path.home() / ".hermes" / "quant" / "hourly-snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
UNIVERSE_PATH = Path.home() / ".hermes" / "scripts" / "quant-universe-interim.txt"
DAILY_BRIEF_DIR = Path.home() / ".hermes" / "quant" / "daily-briefs"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Threshold defaults — match ADR-0004 risk envelope
INTRADAY_DD_HALT_PCT = 0.005      # 0.5% account drawdown intraday → halt-flag candidate
PER_POSITION_STOP_PCT = 0.10      # 10% unrealized loss → exceeds default stop
PER_POSITION_WARN_PCT = 0.05      # 5% unrealized loss → watch
VIX_SPIKE_PCT = 0.20              # +20% VIX move since last tick → regime watch
SPY_DROP_PCT = 0.02               # -2% SPY move since last tick → regime watch


# ---------- HTTP ----------
def call(url: str) -> tuple[int, Any]:
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SECRET)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:
        return -1, str(e)


# ---------- market-conditions probe ----------
def fetch_market_conditions() -> dict:
    """Fetch SPY + VIX last close + intraday day change. Best-effort; never fatal."""
    out: dict = {"spy": None, "vix": None, "errors": []}
    try:
        import yfinance as yf
    except ImportError as e:
        out["errors"].append(f"yfinance import failed: {e}")
        return out

    for symbol, key in [("SPY", "spy"), ("^VIX", "vix")]:
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d", interval="1d", auto_adjust=False)
            if hist is None or len(hist) == 0:
                out["errors"].append(f"{symbol}: no history")
                continue
            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_close
            day_change_pct = (last_close - prev_close) / prev_close if prev_close else 0.0
            out[key] = {
                "last_close": last_close,
                "prev_close": prev_close,
                "day_change_pct": day_change_pct,
                "asof_index": str(hist.index[-1]),
            }
        except Exception as e:
            out["errors"].append(f"{symbol}: {type(e).__name__}: {e}")
    return out


# ---------- snapshot ----------
def collect_snapshot() -> dict:
    code_account, account = call(f"{TRADING}/account")
    code_positions, positions = call(f"{TRADING}/positions")
    code_orders, orders = call(f"{TRADING}/orders?status=all&limit=50&direction=desc")
    code_clock, clock = call(f"{TRADING}/clock")

    now_utc = datetime.now(UTC)
    snap: dict = {
        "ts": now_utc.isoformat(),
        "asof_eastern": now_utc.astimezone(ET).isoformat(),
        "ok": all(c == 200 for c in [code_account, code_positions, code_orders, code_clock]),
        "errors": {},
        "account": account if code_account == 200 else None,
        "positions": positions if code_positions == 200 and isinstance(positions, list) else [],
        "recent_orders": orders[:20] if code_orders == 200 and isinstance(orders, list) else [],
        "clock": clock if code_clock == 200 else None,
        "market_conditions": fetch_market_conditions(),
    }
    if code_account != 200:
        snap["errors"]["account"] = (code_account, str(account)[:200])
    if code_positions != 200:
        snap["errors"]["positions"] = (code_positions, str(positions)[:200])
    if code_orders != 200:
        snap["errors"]["orders"] = (code_orders, str(orders)[:200])
    if code_clock != 200:
        snap["errors"]["clock"] = (code_clock, str(clock)[:200])
    return snap


# ---------- prev snapshot ----------
def latest_prev_snapshot() -> dict | None:
    files = sorted(SNAP_DIR.glob("*-hourly.json"))
    if not files:
        return None
    try:
        with open(files[-1]) as f:
            return json.load(f)
    except Exception:
        return None


def is_first_tick_of_day(prev: dict | None, now_utc: datetime) -> bool:
    if prev is None:
        return True
    try:
        prev_ts = datetime.fromisoformat(prev["ts"])
    except Exception:
        return True
    return prev_ts.astimezone(ET).date() != now_utc.astimezone(ET).date()


# ---------- alert detection ----------
def detect_alerts(prev: dict | None, snap: dict) -> list[str]:
    alerts: list[str] = []

    # API failures first — these block everything else
    if not snap["ok"]:
        for k, (code, body) in snap["errors"].items():
            alerts.append(f"⚠️ Alpaca API error: {k} HTTP {code} — {body[:80]}")
        return alerts

    acct = snap["account"]
    if not acct:
        return alerts

    # Account-level drawdown (ADR-0004 0.5% intraday / 5% all-time envelope)
    try:
        equity = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", 0))
        if last_equity > 0:
            intraday_pct = (equity - last_equity) / last_equity
            if intraday_pct <= -INTRADAY_DD_HALT_PCT:
                alerts.append(
                    f"🚨 ACCOUNT DRAWDOWN −{abs(intraday_pct)*100:.2f}% intraday "
                    f"(${equity:,.0f} from ${last_equity:,.0f}) — halt-flag candidate (ADR-0004)"
                )
    except (TypeError, ValueError):
        pass

    # Trading blocked / account locked
    if acct.get("trading_blocked"):
        alerts.append(f"🚨 trading_blocked=True — account locked, no orders will route")
    if acct.get("account_blocked"):
        alerts.append(f"🚨 account_blocked=True — account-level halt")

    # Per-position threshold breaches
    for p in snap["positions"]:
        sym = p.get("symbol", "?")
        try:
            upl_pct = float(p.get("unrealized_plpc", 0))
            upl = float(p.get("unrealized_pl", 0))
            mv = float(p.get("market_value", 0))
            qty = p.get("qty", "?")
            avg = p.get("avg_entry_price", "?")
            current = p.get("current_price") or p.get("market_value", "?")
            if upl_pct <= -PER_POSITION_STOP_PCT:
                alerts.append(
                    f"🚨 {sym} unrealized {upl_pct*100:+.2f}% (${upl:+,.0f}) "
                    f"qty={qty} @ ${avg} → ${current} — exceeds default stop"
                )
            elif upl_pct <= -PER_POSITION_WARN_PCT:
                alerts.append(
                    f"🟡 {sym} unrealized {upl_pct*100:+.2f}% (${upl:+,.0f}) "
                    f"qty={qty} @ ${avg} → ${current} — watch"
                )
        except (TypeError, ValueError):
            continue

    # New fills since last tick (paper play went live!)
    if prev is not None:
        prev_filled = {o.get("id") for o in prev.get("recent_orders", []) if o.get("status") == "filled"}
        for o in snap["recent_orders"]:
            if o.get("status") == "filled" and o.get("id") not in prev_filled:
                sym = o.get("symbol", "?")
                qty = o.get("filled_qty", "?")
                avg = o.get("filled_avg_price", "?")
                side = o.get("side", "?")
                cls = o.get("order_class", "simple")
                cls_label = f" [{cls}]" if cls and cls != "simple" else ""
                alerts.append(f"🟢 NEW FILL{cls_label}: {side} {qty} {sym} @ ${avg}")

    # Newly canceled / rejected orders
    if prev is not None:
        prev_status = {o.get("id"): o.get("status") for o in prev.get("recent_orders", [])}
        for o in snap["recent_orders"]:
            oid = o.get("id")
            cur_status = o.get("status")
            prev_st = prev_status.get(oid)
            if prev_st in ("accepted", "new", "pending_new", "partially_filled") and cur_status in ("rejected", "expired"):
                alerts.append(
                    f"⚠️ order {cur_status}: {o.get('side','?')} {o.get('qty','?')} {o.get('symbol','?')} "
                    f"(was {prev_st}) — reason={o.get('reject_reason') or o.get('failed_at') or 'n/a'}"
                )

    # Regime shifts via market conditions
    mc_now = snap.get("market_conditions") or {}
    mc_prev = (prev or {}).get("market_conditions") or {}
    try:
        spy_now = (mc_now.get("spy") or {}).get("last_close")
        spy_prev = (mc_prev.get("spy") or {}).get("last_close")
        if spy_now and spy_prev:
            move = (spy_now - spy_prev) / spy_prev
            if move <= -SPY_DROP_PCT:
                alerts.append(f"🟡 SPY {move*100:+.2f}% since last tick (${spy_prev:.2f} → ${spy_now:.2f}) — regime watch")
    except Exception:
        pass
    try:
        vix_now = (mc_now.get("vix") or {}).get("last_close")
        vix_prev = (mc_prev.get("vix") or {}).get("last_close")
        if vix_now and vix_prev and vix_prev > 0:
            move = (vix_now - vix_prev) / vix_prev
            if move >= VIX_SPIKE_PCT:
                alerts.append(f"🟡 VIX {move*100:+.2f}% since last tick ({vix_prev:.2f} → {vix_now:.2f}) — regime watch")
    except Exception:
        pass

    return alerts


# ---------- rendering ----------
def universe_size() -> int:
    try:
        with open(UNIVERSE_PATH) as f:
            return sum(1 for line in f if line.strip() and not line.lstrip().startswith("#"))
    except Exception:
        return 0


def latest_brief_actionables() -> list[str]:
    """Pull the actionable tickers from this morning's pre-market brief, if any."""
    try:
        files = sorted(DAILY_BRIEF_DIR.glob("*-interim.json"))
        if not files:
            return []
        with open(files[-1]) as f:
            brief = json.load(f)
        return [r.get("symbol") for r in brief.get("actionable", []) if r.get("symbol")][:10]
    except Exception:
        return []


def render_heartbeat(snap: dict) -> str:
    acct = snap.get("account") or {}
    if not acct:
        return ""
    try:
        equity = float(acct.get("equity", 0))
        cash = float(acct.get("cash", 0))
        bp = float(acct.get("buying_power", 0))
        opt_bp = float(acct.get("options_buying_power", 0))
        last_eq = float(acct.get("last_equity", 0))
        intraday_pct = (equity - last_eq) / last_eq if last_eq > 0 else 0.0
    except (TypeError, ValueError):
        equity = cash = bp = opt_bp = 0.0
        intraday_pct = 0.0

    pos_count = len(snap.get("positions", []))
    open_orders = sum(
        1 for o in snap.get("recent_orders", [])
        if o.get("status") in ("accepted", "new", "pending_new", "partially_filled")
    )

    eastern = datetime.now(UTC).astimezone(ET)
    hh = eastern.strftime("%a %b %-d, %-I:%M %p ET")

    mc = snap.get("market_conditions") or {}
    spy = (mc.get("spy") or {})
    vix = (mc.get("vix") or {})
    spy_line = ""
    if spy.get("last_close"):
        spy_line = f"SPY ${spy['last_close']:.2f} ({spy.get('day_change_pct', 0)*100:+.2f}%)"
    vix_line = ""
    if vix.get("last_close"):
        vix_line = f"VIX {vix['last_close']:.2f} ({vix.get('day_change_pct', 0)*100:+.2f}%)"
    market_state = " | ".join(s for s in [spy_line, vix_line] if s)

    actionables = latest_brief_actionables()
    actionable_str = ", ".join(actionables) if actionables else "—"

    clock = snap.get("clock") or {}
    is_open = clock.get("is_open", False)
    market_label = "🟢 open" if is_open else "🔴 closed"

    lines = [
        f"📡 **hermes-quant hourly heartbeat** — {hh}",
        f"• Market: {market_label}",
        f"• Equity: ${equity:,.0f} ({intraday_pct*100:+.2f}% intraday) | Cash: ${cash:,.0f} | BP: ${bp:,.0f} | Opt BP: ${opt_bp:,.0f}",
        f"• Open positions: {pos_count} | Open orders: {open_orders}",
        f"• Universe: {universe_size()} symbols | Today's pre-market actionables: {actionable_str}",
    ]
    if market_state:
        lines.append(f"• Conditions: {market_state}")
    return "\n".join(lines)


def render_position_perf(snap: dict) -> str:
    """Compact per-position P&L block, included in alerts when positions exist."""
    positions = snap.get("positions", [])
    if not positions:
        return ""
    rows = ["**Open positions:**"]
    for p in positions:
        try:
            sym = p.get("symbol", "?")
            qty = p.get("qty", "?")
            side = p.get("side", "?")
            avg = float(p.get("avg_entry_price", 0))
            cur = float(p.get("current_price", 0)) if p.get("current_price") else None
            upl = float(p.get("unrealized_pl", 0))
            upl_pct = float(p.get("unrealized_plpc", 0))
            mv = float(p.get("market_value", 0))
            cur_str = f"${cur:.2f}" if cur is not None else "—"
            rows.append(
                f"  {sym} {side} {qty} | entry ${avg:.2f} → {cur_str} | "
                f"P&L {upl_pct*100:+.2f}% (${upl:+,.0f}) | MV ${mv:,.0f}"
            )
        except (TypeError, ValueError):
            continue
    return "\n".join(rows)


def render_alerts(alerts: list[str], snap: dict) -> str:
    eastern = datetime.now(UTC).astimezone(ET)
    hh = eastern.strftime("%a %b %-d, %-I:%M %p ET")
    pieces = [f"⚠️ **hermes-quant hourly alert** — {hh}"]
    pieces.extend(f"  {a}" for a in alerts)
    perf = render_position_perf(snap)
    if perf:
        pieces.append("")
        pieces.append(perf)
    return "\n".join(pieces)


# ---------- main ----------
def main() -> int:
    now_utc = datetime.now(UTC)
    prev = latest_prev_snapshot()
    snap = collect_snapshot()

    # Always persist
    out_path = SNAP_DIR / f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}-hourly.json"
    try:
        with open(out_path, "w") as f:
            json.dump(snap, f, indent=2, default=str)
    except Exception as e:
        # snapshot persist failure is itself worth alerting on
        print(f"⚠️ hermes-quant hourly snapshot failed to persist: {e}", flush=True)
        return 1

    # Holiday / market-closed guard: stay silent when market is closed.
    # (Exception: surface API failures even on closed days so you know something's wrong.)
    clock = snap.get("clock") or {}
    if not snap["ok"]:
        # API error — always speak even on holidays
        alerts = detect_alerts(prev, snap)
        if alerts:
            print(render_alerts(alerts, snap), flush=True)
        return 0
    if not clock.get("is_open"):
        # Market closed (holiday or after-hours) — silent
        return 0

    alerts = detect_alerts(prev, snap)
    first_tick = is_first_tick_of_day(prev, now_utc)

    pieces: list[str] = []
    if first_tick:
        hb = render_heartbeat(snap)
        if hb:
            pieces.append(hb)
    if alerts:
        pieces.append(render_alerts(alerts, snap))

    if pieces:
        print("\n\n".join(pieces), flush=True)
    # else: empty stdout = silent (no_agent=True cron drops it per send_message gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
