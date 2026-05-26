#!/usr/bin/env python3
"""quant-playbook-tick.py — Daily decision tick for the 5-play playbook (ADR-0035 wave 2).

Schedule: `0 6 * * 1-5` (gateway host PT) = 09:00 ET, 1 hour before market open.
Deliver:  local (silent unless first-of-day summary or error).
Mode:     no_agent=True → empty stdout = SILENT; non-empty stdout delivered verbatim.

Per-tick flow (Half A — equity-only):
  1. Halt fail-closed: read ~/.hermes/quant/halt_state.json, abort if any active halt.
  2. Load watchlist: ~/.hermes/quant/watchlist/play-fit.json, filter to active rows.
  3. For each (symbol, play) where play in EQUITY_PLAYS and state=="active":
       a. Idempotent skip if same (symbol, play) already journaled today.
       b. Compute play snapshot (yfinance, cached) — gives spot, prior_close, ATR-14,
          days_since_earnings.
       c. Apply silence rules:
          - Overnight gap: |spot - prior_close| / prior_close > 1.5 * ATR-14/spot → silenced.
          - days_until_earnings < 5 → silenced (extends ADR-0035 §override-rules).
       d. Run advisor.recommend(symbol, asset_class="equity").
       e. If gate is FIRE, place an Alpaca paper market order (notional sized via
          kelly_fraction from the advisor result, capped to PER_FIRE_NOTIONAL_USD).
          Skipped under --dry-run.
       f. Append decision to ~/.hermes/quant/playbook/tick-journal.jsonl.
  4. First run of the day OR on errors: print one-line summary. Otherwise silent.

Half B (deferred — TODO once ADR-0029 multi-leg reactor lands):
  - Multi-leg covered_call/csp/wheel proposals routed through the option reactor.
  - For now those plays are filtered out cleanly (EQUITY_PLAYS = {"swing","leaps"}).

Environment overrides for testability (no live calls required):
  HERMES_QUANT_PLAYBOOK_TICK_MOCK=1 → use a stub advisor.recommend + stub snapshot.
                                       Used by unit tests.
  HERMES_QUANT_PLAYBOOK_DRY_RUN=1   → equivalent to --dry-run.

ADR refs: ADR-0014 (advisor surface), ADR-0009 (halt registry), ADR-0035 (cadence,
this script's reason for existence), ADR-0029 (multi-leg reactor — pending).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Silence noisy third-party loggers up front (yfinance/curl_cffi/urllib3 emit
# stderr noise that's not actionable from cron POV).
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("yfinance", "peewee", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

logger = logging.getLogger("quant-playbook-tick")

# ---------- paths ----------
HERMES_HOME = Path.home() / ".hermes"
QUANT_HOME = HERMES_HOME / "quant"
WATCHLIST_PATH = QUANT_HOME / "watchlist" / "play-fit.json"
HALT_MIRROR_PATH = QUANT_HOME / "halt_state.json"
PLAYBOOK_DIR = QUANT_HOME / "playbook"
JOURNAL_PATH = PLAYBOOK_DIR / "tick-journal.jsonl"
SECRETS_PATH = HERMES_HOME / "secrets" / "alpaca.env"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ---------- constants ----------
# Half A: only swing + leaps fire as equity proposals. Other plays (covered_call,
# csp, wheel) need ADR-0029 multi-leg reactor — silenced cleanly until that lands.
EQUITY_PLAYS = {"swing", "leaps"}
ALL_PLAYS = {"covered_call", "csp", "wheel", "leaps", "swing"}

# Risk envelope
PER_FIRE_NOTIONAL_USD = 1000.0  # cap per equity proposal (paper account, conservative).
PER_FIRE_NOTIONAL_FLOOR_USD = 100.0
GAP_ATR_MULTIPLIER = 1.5
EARNINGS_LOCKOUT_DAYS = 5  # silence if days_until_earnings < 5

# ---------- utilities ----------
def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_et_date() -> str:
    """Calendar date in ET — used as the idempotency key bucket."""
    return datetime.now(UTC).astimezone(ET).strftime("%Y-%m-%d")


def append_journal(record: dict[str, Any]) -> None:
    """Append-only JSONL journal. Never raises (cron must keep running)."""
    record.setdefault("ts", utcnow_iso())
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        sys.stderr.write(f"playbook-tick journal write failed: {e}\n")


# ---------- halt-state fail-closed gate ----------
def read_active_halts() -> list[dict]:
    """Read halt_state.json mirror. Returns active halts (empty list = OK).

    Per ADR-0009, halts are NEVER cleared by trading signals — only via
    `hermes quant resume` CLI or by halted_until passing. Corrupt mirror →
    treat as fail-closed halt.
    """
    if not HALT_MIRROR_PATH.exists():
        return []
    try:
        data = json.loads(HALT_MIRROR_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [{"reason": f"halt_state.json corrupt: {e}", "scope": "fail-closed"}]
    if not isinstance(data, list):
        return []
    # Filter to active halts only (cleared_at is None / missing)
    active = [h for h in data if isinstance(h, dict) and not h.get("cleared_at")]
    return active


# ---------- watchlist load ----------
def load_active_pairs() -> list[tuple[str, str, float]]:
    """Load play-fit.json, return list of (symbol, play, last_score) for active rows
    in EQUITY_PLAYS only. Non-equity plays are skipped silently.
    """
    if not WATCHLIST_PATH.exists():
        return []
    try:
        d = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"play-fit.json read failed: {e}\n")
        return []

    out: list[tuple[str, str, float]] = []
    for play, entries in (d.get("plays") or {}).items():
        if play not in EQUITY_PLAYS:
            continue  # Half A: equity only. Multi-leg plays deferred (ADR-0029).
        for e in entries:
            if e.get("state") != "active":
                continue
            sym = e.get("symbol")
            if not sym:
                continue
            score = float(e.get("last_score") or 0.0)
            out.append((sym, play, score))
    # Stable order: by symbol then play, so journal lines are reproducible.
    return sorted(out, key=lambda t: (t[0], t[1]))


# ---------- idempotency ----------
def fired_today_pairs() -> set[tuple[str, str]]:
    """Read tick-journal.jsonl, return {(symbol, play)} that already fired today (ET).

    "Fired" = decision == "fire" AND dry_run == False AND date_et == today.
    Dry-run "fires" intentionally do NOT count toward idempotency — they
    didn't actually place an order, so they shouldn't block a real cron
    fire later in the day. Other decisions (silenced, gate_reject,
    idempotent_skip, halt_abort) also don't block re-fires; only a
    real-fire blocks a re-fire.
    """
    today = today_et_date()
    if not JOURNAL_PATH.exists():
        return set()
    fired: set[tuple[str, str]] = set()
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("date_et") != today:
                    continue
                if row.get("decision") != "fire":
                    continue
                if row.get("dry_run"):
                    continue  # dry-run fires don't claim the slot
                sym = row.get("symbol")
                play = row.get("play")
                if sym and play:
                    fired.add((sym, play))
    except OSError:
        pass
    return fired


# ---------- alpaca creds + order placement ----------
def _load_creds() -> dict[str, str]:
    out: dict[str, str] = {}
    if not SECRETS_PATH.exists():
        return out
    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                k, v = line[len("export "):].split("=", 1)
                out[k] = v.strip().strip('"').strip("'")
    return out


def place_paper_market_order(symbol: str, notional_usd: float, *, side: str = "buy") -> dict[str, Any]:
    """Place a paper-account market order for `notional_usd` USD on `symbol`.

    Returns Alpaca's order JSON on 200/201, or {"error": ...} on failure.
    Never raises — a routing failure shouldn't crash the whole tick.
    """
    import urllib.request
    import urllib.error

    creds = _load_creds()
    key = creds.get("ALPACA_API_KEY_ID")
    secret = creds.get("ALPACA_API_SECRET_KEY")
    base = creds.get("ALPACA_BASE_URL")
    if not (key and secret and base):
        return {"error": "alpaca_creds_missing", "detail": "alpaca.env incomplete"}

    body = json.dumps({
        "symbol": symbol,
        "notional": f"{notional_usd:.2f}",
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }).encode("utf-8")
    req = urllib.request.Request(f"{base}/orders", data=body, method="POST")
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", secret)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body_txt = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_txt = ""
        return {"error": f"http_{e.code}", "detail": body_txt[:300]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:300]}


# ---------- snapshot + silence rules ----------
def _is_mock_mode() -> bool:
    return os.environ.get("HERMES_QUANT_PLAYBOOK_TICK_MOCK") == "1"


def _mock_snapshot(symbol: str) -> dict[str, Any]:
    """Deterministic snapshot for tests. Vary by symbol to exercise gap rule."""
    snaps = {
        "AAPL": {"last_close": 200.0, "prior_close": 199.0, "atr_14": 3.0, "days_until_earnings": 30, "days_since_earnings": 60, "available": True},
        "GAP1": {"last_close": 110.0, "prior_close": 100.0, "atr_14": 2.0, "days_until_earnings": 30, "days_since_earnings": 60, "available": True},  # 10% gap, 1.5*ATR=3 → SILENCE
        "EARN": {"last_close": 100.0, "prior_close": 99.5,  "atr_14": 2.0, "days_until_earnings": 2,  "days_since_earnings": 60, "available": True},  # earnings lockout
        "DARK": {"last_close": None,  "prior_close": None,  "atr_14": None, "days_until_earnings": None, "days_since_earnings": None, "available": False},  # data unavailable
    }
    return snaps.get(symbol, snaps["AAPL"])


def compute_silence_snapshot(symbol: str) -> dict[str, Any]:
    """Compute (last_close, prior_close, atr_14, days_until_earnings, days_since_earnings)
    for the silence-rule checks. Best-effort; on any failure returns
    {"available": False, "reason": ...} so the caller can fall through.

    Uses hermes_quant.playbook.scorers.compute_play_snapshot for the bulk
    (RSI/ATR/days_since_earnings); adds a tiny yfinance probe for
    days_until_earnings (forward-looking) which the existing snapshot
    intentionally doesn't compute (it goes the other direction).
    """
    if _is_mock_mode():
        return _mock_snapshot(symbol)

    try:
        from hermes_quant.playbook.scorers import compute_play_snapshot
    except Exception as e:
        return {"available": False, "reason": f"import_failed: {e}"}

    try:
        snap = compute_play_snapshot(symbol)
    except Exception as e:
        return {"available": False, "reason": f"snapshot_error: {type(e).__name__}: {e}"}

    last_close = snap.get("last_close")
    atr_14 = snap.get("atr_14")
    days_since_earnings = snap.get("days_since_earnings")

    # Prior close: fetch via yfinance 2-day daily history (cheap; market data is
    # already cached upstream by compute_play_snapshot's yfinance Ticker).
    prior_close: float | None = None
    try:
        import yfinance as yf
        import contextlib, io
        _err = io.StringIO()
        with contextlib.redirect_stderr(_err):
            t = yf.Ticker(symbol)
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if hist is not None and len(hist) >= 2:
                prior_close = float(hist["Close"].iloc[-2])
    except Exception:
        prior_close = None

    # days_until_earnings: forward-looking. Fall back to large-positive on miss.
    days_until_earnings: int | None = None
    try:
        import yfinance as yf
        import contextlib, io
        _err = io.StringIO()
        with contextlib.redirect_stderr(_err):
            t = yf.Ticker(symbol)
            ed = getattr(t, "earnings_dates", None)
            if ed is not None and len(ed) > 0:
                now = datetime.now(UTC)
                future_deltas: list[int] = []
                for ts in ed.index:
                    ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                    if isinstance(ts_dt, datetime):
                        if ts_dt.tzinfo is None:
                            ts_dt = ts_dt.replace(tzinfo=UTC)
                        delta = (ts_dt - now).days
                        if delta >= 0:
                            future_deltas.append(delta)
                if future_deltas:
                    days_until_earnings = min(future_deltas)
    except Exception:
        days_until_earnings = None
    if days_until_earnings is None:
        # Treat unknown-future as safe-large so we don't over-silence on missing data.
        days_until_earnings = 90

    return {
        "last_close": last_close,
        "prior_close": prior_close,
        "atr_14": atr_14,
        "days_until_earnings": days_until_earnings,
        "days_since_earnings": days_since_earnings,
        "available": last_close is not None and prior_close is not None and atr_14 is not None,
    }


def silence_check(snapshot: dict[str, Any]) -> str | None:
    """Apply silence rules. Returns silence reason string, or None to pass through.

    Rules (per ADR-0035 + spec extension):
      - Overnight gap: |spot - prior_close|/prior_close > 1.5 * (atr_14 / spot)
      - days_until_earnings < 5: silence (mirror of days_since_earnings >= 5)
    """
    if not snapshot.get("available"):
        return f"snapshot_unavailable: {snapshot.get('reason', 'data missing')}"

    spot = snapshot["last_close"]
    prior = snapshot["prior_close"]
    atr = snapshot["atr_14"]
    if not (spot and prior and atr and spot > 0 and prior > 0 and atr > 0):
        return "snapshot_unavailable: zero/null in critical field"

    gap_pct = abs(spot - prior) / prior
    atr_pct = atr / spot
    if gap_pct > GAP_ATR_MULTIPLIER * atr_pct:
        return (f"overnight_gap {gap_pct*100:.2f}% > {GAP_ATR_MULTIPLIER}×ATR "
                f"({GAP_ATR_MULTIPLIER * atr_pct * 100:.2f}%)")

    dte = snapshot.get("days_until_earnings")
    if dte is not None and dte < EARNINGS_LOCKOUT_DAYS:
        return f"days_until_earnings={dte} < {EARNINGS_LOCKOUT_DAYS}"

    return None


# ---------- advisor + decision ----------
def _mock_recommend(symbol: str, **kwargs: Any) -> dict[str, Any]:
    """Stub advisor.recommend for unit-test mode. Returns FIRE for AAPL, HOLD for everything else.

    Mimics the real ADR-0014 §D1 result shape (risk_gate + aggregated_signal).
    """
    if symbol == "AAPL":
        return {
            "risk_gate": {"pass": True, "recommended_action": "long", "kelly_fraction": 0.05,
                           "gated_reason": None},
            "aggregated_signal": {"direction": 1, "magnitude": 0.03, "confidence": 0.7,
                                   "horizon": "1d", "aggregator": "mock"},
            "as_of": utcnow_iso(),
            "caveats": [],
        }
    return {
        "risk_gate": {"pass": False, "recommended_action": "gated", "kelly_fraction": 0.0,
                       "gated_reason": "mock-hold"},
        "aggregated_signal": {"direction": 0, "magnitude": 0.0, "confidence": 0.5,
                               "horizon": "1d", "aggregator": "mock"},
        "as_of": utcnow_iso(),
        "caveats": [],
    }


def call_advisor(symbol: str) -> dict[str, Any]:
    """Run advisor.recommend(symbol, asset_class='equity'). Returns the advisor result dict
    (per ADR-0014 §D1) or a synthetic error dict on failure.
    """
    if _is_mock_mode():
        return _mock_recommend(symbol)

    try:
        from hermes_quant.advisor import recommend as _recommend
    except Exception as e:
        return {"gate": {"action": "ERROR", "reason": f"import_failed: {e}"}, "caveats": [str(e)]}

    try:
        return _recommend(symbol, asset_class="equity", timeframe="1d")
    except Exception as e:
        return {"gate": {"action": "ERROR", "reason": f"advisor_exception: {type(e).__name__}: {e}"},
                "caveats": [traceback.format_exc()]}


def extract_gate_decision(advisor_result: dict[str, Any], play: str | None = None) -> tuple[str, float, str]:
    """Extract (gate_action, confidence, reason) from advisor result.

    Honors the canonical ADR-0014 §D1 shape:
      result["risk_gate"] = {pass, recommended_action, kelly_fraction, gated_reason}
      result["aggregated_signal"] = {direction, magnitude, confidence, ...}

    gate_action returned: "FIRE" | "HOLD" | "ERROR".

    Per-play semantics (Half A — equity-only):
      * swing & leaps are LONG-bias plays — refuse to fire on short signals.
        (Short-leaps and short-swing are valid strategies, but they require
        the multi-leg options reactor to express via puts; ADR-0029 deferred.)
      * generic FIRE iff risk_gate.pass is True AND recommended_action ∈ LONG set
        AND aggregated_signal.direction > 0.
    """
    rg = advisor_result.get("risk_gate") or {}
    sig = advisor_result.get("aggregated_signal") or {}

    if rg.get("recommended_action") == "ERROR" or advisor_result.get("error"):
        reason = rg.get("gated_reason") or advisor_result.get("error") or "advisor_error"
        return "ERROR", 0.0, str(reason)

    passes = bool(rg.get("pass"))
    rec_action = (rg.get("recommended_action") or "gated").lower()
    confidence = float(sig.get("confidence") or 0.0)
    direction = int(sig.get("direction") or 0)

    long_set = ("long_with_stop", "long", "fire")
    short_set = ("short_with_stop", "short")

    if passes and rec_action in long_set and direction > 0:
        return "FIRE", confidence, f"recommended_action={rec_action} dir={direction}"

    if passes and rec_action in short_set:
        # Equity-only Half A: short paths require options reactor (ADR-0029).
        return "HOLD", confidence, (
            f"short_signal_deferred (recommended_action={rec_action}); "
            f"swing/leaps long-only until ADR-0029 multi-leg reactor lands"
        )

    reason = rg.get("gated_reason") or f"recommended_action={rec_action}"
    return "HOLD", confidence, str(reason)


def kelly_to_notional(advisor_result: dict[str, Any]) -> float:
    """Map advisor's kelly_fraction → notional USD, clamped to [floor, cap]."""
    rg = advisor_result.get("risk_gate") or {}
    kf = float(rg.get("kelly_fraction") or 0.05)
    # Default sizing: kelly_fraction × $20K nominal book → $1000 default at 5%.
    nominal = 20_000.0
    notional = max(PER_FIRE_NOTIONAL_FLOOR_USD, min(PER_FIRE_NOTIONAL_USD, kf * nominal))
    return notional


# ---------- main per-pair processor ----------
def process_pair(
    symbol: str,
    play: str,
    score: float,
    *,
    today_et: str,
    tick_id: str,
    fired_set: set[tuple[str, str]],
    dry_run: bool,
) -> dict[str, Any]:
    """Process a single (symbol, play) pair. Returns the journal record."""
    base = {
        "tick_id": tick_id,
        "date_et": today_et,
        "symbol": symbol,
        "play": play,
        "score": score,
        "dry_run": dry_run,
    }

    # 1. Idempotency
    if (symbol, play) in fired_set:
        return {**base, "decision": "idempotent_skip", "reason": "already fired today"}

    # 2. Silence rules
    snap = compute_silence_snapshot(symbol)
    silence = silence_check(snap)
    if silence:
        return {**base,
                "decision": "silenced",
                "reason": silence,
                "snapshot": {k: snap.get(k) for k in
                             ("last_close", "prior_close", "atr_14",
                              "days_until_earnings", "days_since_earnings", "available")}}

    # 3. Advisor + gate
    advisor_result = call_advisor(symbol)
    action, confidence, reason = extract_gate_decision(advisor_result, play=play)

    if action == "ERROR":
        return {**base, "decision": "gate_reject", "gate": "ERROR",
                "confidence": confidence, "reason": reason}

    if action != "FIRE":
        return {**base, "decision": "gate_reject", "gate": action,
                "confidence": confidence, "reason": reason}

    # 4. FIRE — place order (or dry-run skip)
    notional = kelly_to_notional(advisor_result)
    if dry_run:
        return {**base,
                "decision": "fire",
                "gate": "FIRE",
                "confidence": confidence,
                "reason": reason,
                "notional_usd": notional,
                "order_id": None,
                "dry_run_note": "no order placed (--dry-run)"}

    order = place_paper_market_order(symbol, notional, side="buy")
    if "error" in order:
        return {**base, "decision": "gate_reject",
                "gate": "FIRE_BUT_ROUTE_FAILED",
                "confidence": confidence,
                "reason": f"alpaca route failed: {order.get('error')}: {order.get('detail','')}",
                "notional_usd": notional}

    return {**base,
            "decision": "fire",
            "gate": "FIRE",
            "confidence": confidence,
            "reason": reason,
            "notional_usd": notional,
            "order_id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "submitted_at": order.get("submitted_at")}


# ---------- main tick ----------
def run_tick(*, dry_run: bool) -> dict[str, Any]:
    tick_id = utcnow_iso()
    today_et = today_et_date()
    summary = {
        "event": "tick_summary",
        "tick_id": tick_id,
        "date_et": today_et,
        "dry_run": dry_run,
        "scanned": 0,
        "fired": 0,
        "silenced": 0,
        "gate_rejected": 0,
        "idempotent_skipped": 0,
        "errors": 0,
        "halt_aborted": False,
    }

    # Halt fail-closed
    halts = read_active_halts()
    if halts:
        summary["halt_aborted"] = True
        summary["halts"] = halts
        for h in halts:
            append_journal({
                "tick_id": tick_id,
                "date_et": today_et,
                "decision": "halt_abort",
                "reason": h.get("reason", "halt active"),
                "halt": h,
                "dry_run": dry_run,
            })
        return summary

    pairs = load_active_pairs()
    summary["scanned"] = len(pairs)
    if not pairs:
        # Empty watchlist — record one journal line, return.
        append_journal({
            "tick_id": tick_id,
            "date_et": today_et,
            "decision": "no_active_pairs",
            "reason": "watchlist empty for equity plays",
            "dry_run": dry_run,
        })
        return summary

    fired_set = fired_today_pairs()

    for symbol, play, score in pairs:
        try:
            rec = process_pair(
                symbol, play, score,
                today_et=today_et,
                tick_id=tick_id,
                fired_set=fired_set,
                dry_run=dry_run,
            )
        except Exception as e:
            summary["errors"] += 1
            rec = {
                "tick_id": tick_id,
                "date_et": today_et,
                "symbol": symbol,
                "play": play,
                "decision": "gate_reject",
                "reason": f"uncaught {type(e).__name__}: {e}",
                "trace": traceback.format_exc(),
                "dry_run": dry_run,
            }
        append_journal(rec)
        d = rec.get("decision")
        if d == "fire":
            summary["fired"] += 1
            # On real fire, mark fired_set so a duplicate (sym,play) later in
            # this tick can't double-fire (defensive — pairs() is unique per
            # (sym,play) but belt-and-suspenders matters for orders).
            fired_set.add((symbol, play))
        elif d == "silenced":
            summary["silenced"] += 1
        elif d == "idempotent_skip":
            summary["idempotent_skipped"] += 1
        elif d == "gate_reject":
            summary["gate_rejected"] += 1

    append_journal(summary)
    return summary


# ---------- summary rendering ----------
def render_summary(s: dict[str, Any]) -> str:
    et_now = datetime.now(UTC).astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET")
    suffix = ""
    if s["halt_aborted"]:
        suffix = " HALT-ABORTED"
    elif s["dry_run"]:
        suffix = " (dry-run)"
    return (f"📋 hermes-quant playbook tick {et_now}{suffix} "
            f"scanned={s['scanned']} fired={s['fired']} "
            f"silenced={s['silenced']} gate_rejected={s['gate_rejected']} "
            f"idempotent_skipped={s['idempotent_skipped']} errors={s['errors']}")


# ---------- entrypoint ----------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes-quant daily playbook decision tick (equity-only, ADR-0035 wave 2)"
    )
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Run pipeline without placing orders. Always safe.")
    parser.add_argument("--armed", dest="armed", action="store_true",
                        help="Real paper-mode firing. Required for live cron use.")
    parser.add_argument("--always-print", dest="always_print", action="store_true",
                        help="Print summary regardless of first-of-day state (debug).")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON summary instead of human-readable line.")
    args = parser.parse_args(argv)

    # Default mode: dry-run unless --armed (matches ADR-0015 paper-default posture).
    # ENV override for tests / safety pin from cron.
    env_dry = os.environ.get("HERMES_QUANT_PLAYBOOK_DRY_RUN") == "1"
    dry_run = bool(args.dry_run) or env_dry or not bool(args.armed)

    try:
        summary = run_tick(dry_run=dry_run)
    except Exception as e:
        append_journal({
            "ts": utcnow_iso(),
            "date_et": today_et_date(),
            "decision": "tick_uncaught_exception",
            "reason": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(),
            "dry_run": dry_run,
        })
        sys.stderr.write(f"quant-playbook-tick: uncaught: {e}\n")
        return 1

    # Output policy:
    #  - errors or halt → always speak (operator needs to know).
    #  - first tick_summary of the day → speak (heartbeat).
    #  - --always-print or --json → speak.
    #  - dry-run on the cli → speak (operator is interactively testing).
    #  - otherwise silent (no_agent cron drops empty stdout).
    speak = (
        summary["errors"] > 0
        or summary["halt_aborted"]
        or args.always_print
        or args.json
        or dry_run
        or is_first_summary_today_excluding(summary)
    )

    if not speak:
        return 0

    if args.json:
        print(json.dumps(summary, default=str), flush=True)
    else:
        print(render_summary(summary), flush=True)
    return 0


def is_first_summary_today_excluding(current: dict[str, Any]) -> bool:
    """True iff the just-written current summary is the only one today.

    The journal already contains the current summary (we appended it in
    run_tick), so check for 'exactly one tick_summary today'.
    """
    today = today_et_date()
    if not JOURNAL_PATH.exists():
        return True
    count = 0
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == "tick_summary" and row.get("date_et") == today:
                    count += 1
                    if count >= 2:
                        return False
    except OSError:
        return True
    return count <= 1


if __name__ == "__main__":
    sys.exit(main())
