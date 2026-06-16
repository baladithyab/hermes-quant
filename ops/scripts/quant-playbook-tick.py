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
import contextlib
import errno
import json
import logging
import math
import os
import re
import sys
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:  # fcntl is POSIX-only; degrade to a no-op lock on platforms without it.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

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
UTC = UTC

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
PLAYBOOK_AGGREGATE_CAP_ENV = "HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP"
# Canonical paper-account partition in state.db (matches the live producer's
# default sentinel — react/paper.py + state/portfolio_state.py both default to
# "paper-default" when no top-level/reactor account_id is present). The playbook
# fires land on this same Alpaca paper account, so the aggregate cap reads the
# real open book from this partition.
PAPER_ACCOUNT_ID = "paper-default"

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


# ---------- cross-process fire idempotency (ADR-0078 tick-lock parity) ----------
def build_client_order_id(today_et: str, symbol: str, play: str) -> str:
    """Deterministic client_order_id for a (date_et, symbol, play) logical fire.

    The playbook raw-Alpaca fire path does NOT route through the paper-reactor
    seam and so never acquires the per-symbol tick-lock that the reactor path
    holds. Two concurrent armed runs (CRON-REGISTRY job #6 daily --armed + job
    #10 hourly autonomous+armed, or an operator manual --armed run overlapping
    the cron) can both read an identical `fired_set` lacking the not-yet-
    journaled (symbol, play) and both POST. A *deterministic* client_order_id
    makes the broker reject the duplicate: Alpaca enforces client_order_id
    uniqueness per account, so the second POST of the same logical signal
    fails server-side even under a fully lockless race.

    Format: hq-playbook-<date>-<SYMBOL>-<play>, slugified to the broker-safe
    charset and clamped to <=128 chars (Alpaca's client_order_id limit).
    """
    raw = f"hq-playbook-{today_et}-{symbol}-{play}"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    return slug[:128]


@contextlib.contextmanager
def fire_lock(symbol: str, play: str, *, blocking: bool = True) -> Iterator[bool]:
    """Per-(symbol, play) advisory flock that serializes the read->POST->journal
    fire sequence across processes — the same intra-host coordination
    react/paper.py's symbol_tick_lock provides for the reactor path.

    Mirrors the repo's fcntl idiom (daemon/signal_bus.append_locked,
    daemon/lock.DaemonLock): open O_RDWR|O_CREAT without O_TRUNC, flock LOCK_EX.

    Yields True if the lock was acquired (caller may proceed), False if it was
    contended under non-blocking mode (caller must treat as "another run owns
    this fire" and skip). Fail-open-SAFE: if fcntl is unavailable on the
    platform, yields True (no worse than the prior lockless behavior, and the
    deterministic client_order_id still closes the broker-level double-order).
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX fallback
        yield True
        return

    # Derive the lock dir from PLAYBOOK_DIR at call time (not a frozen module
    # constant) so the test fixtures' rebind of PLAYBOOK_DIR is honored and
    # lock files stay inside the isolated fake-home.
    fire_lock_dir = PLAYBOOK_DIR / "fire-locks"
    fire_lock_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", f"{symbol}-{play}")[:120]
    lock_path = fire_lock_dir / f"{slug}.lock"
    flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        try:
            _fcntl.flock(fd, flags)
            acquired = True
        except OSError as e:
            if not blocking and e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                yield False
                return
            raise
        yield True
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


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
        # 2026-05-27 hardening: a non-list mirror (e.g. {} from a partial
        # atomic-replace race or a misconfigured tool) used to fall through
        # as "no halt active". That violates the fail-closed contract from
        # ADR-0009 — a corrupt-shape mirror is indistinguishable from an
        # absent halt registry, which is operationally unsafe. Now we
        # treat shape mismatch as an active fail-closed halt.
        return [{"reason": f"halt_state.json wrong shape: {type(data).__name__}", "scope": "fail-closed"}]
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
                if not isinstance(row, dict):
                    continue  # silence-by-default: a valid-JSON non-dict line (corrupt append)
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


def place_paper_market_order(
    symbol: str,
    notional_usd: float,
    *,
    side: str = "buy",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Place a paper-account market order for `notional_usd` USD on `symbol`.

    Returns Alpaca's order JSON on 200/201, or {"error": ...} on failure.
    Never raises — a routing failure shouldn't crash the whole tick.

    `client_order_id`, when supplied, is sent in the POST body so the broker
    can enforce order-id uniqueness and reject a duplicate of the same logical
    fire (see build_client_order_id). This is the broker-level half of the
    concurrent double-fire guard; the local advisory flock (fire_lock) is the
    other half.
    """
    import urllib.error
    import urllib.request

    creds = _load_creds()
    key = creds.get("ALPACA_API_KEY_ID")
    secret = creds.get("ALPACA_API_SECRET_KEY")
    base = creds.get("ALPACA_BASE_URL")
    if not (key and secret and base):
        return {"error": "alpaca_creds_missing", "detail": "alpaca.env incomplete"}

    payload: dict[str, Any] = {
        "symbol": symbol,
        "notional": f"{notional_usd:.2f}",
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
    body = json.dumps(payload).encode("utf-8")
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


# ---------- tick-level aggregate cap ----------
def _aggregate_cap_enabled() -> bool:
    return os.environ.get(PLAYBOOK_AGGREGATE_CAP_ENV, "").strip() == "1"


def read_alpaca_account_equity() -> float | None:
    """Return Alpaca paper account equity from /account, or None on any uncertainty.

    This is used only when HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP=1. None is a
    fail-closed input: callers silence would-be fires rather than assuming a
    nominal account size.
    """
    import urllib.error
    import urllib.request

    creds = _load_creds()
    key = creds.get("ALPACA_API_KEY_ID")
    secret = creds.get("ALPACA_API_SECRET_KEY")
    base = creds.get("ALPACA_BASE_URL")
    if not (key and secret and base):
        return None

    req = urllib.request.Request(f"{base.rstrip('/')}/account", method="GET")
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", secret)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            account = json.loads(r.read())
    except (urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
    except Exception:
        return None

    try:
        equity = float(account.get("equity"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(equity) or equity <= 0:
        return None
    return equity


def read_real_open_positions_gross_usd(equity_usd: float) -> float | None:
    """Return the canonical open book's gross exposure in USD, or None on failure.

    Used only when HERMES_QUANT_PLAYBOOK_AGGREGATE_CAP=1. The aggregate cap
    ceiling is denominated in USD (account equity × gross_headroom). The
    consumed side must be seeded with the REAL open positions' gross exposure
    in the SAME USD unit — otherwise the cap counts only this tick's own fires
    and admits fires that breach the gross ceiling against the true book (cap2).

    UNIT (verified against the canonical money-state code):
        Position.quantity is a SIGNED NAV-FRACTION, NOT a share count. The
        PaperReactor persists fill_size_pct (a signed fraction of NAV) straight
        into the position's quantity field (state.portfolio_state §3; react/
        paper.py:69 with reactor_metadata.quantity ABSENT for an equity fill —
        the playbook fires equity only, EQUITY_PLAYS). The canonical ADR-0071
        gross cap measures the existing book as Σ |Position.quantity| (react/
        multileg.py:461-466 feeds bare quantity into RiskPortfolioState whose
        gross_exposure_pct = Σ|p|; risk.portfolio_normalize:116-117) and
        compares it against caps.max_gross_exposure_pct — a NAV-fraction.

        So the book's gross NAV-fraction is Σ |quantity|, and its USD gross
        (matching this cap's USD ceiling = equity × gross_headroom) is
        equity_usd × Σ |quantity|. Multiplying quantity by avg_entry_price (a
        per-share price) would mix a NAV-fraction with a price — a unit error
        that under-counts by orders of magnitude and re-introduces cap2.

    Args:
        equity_usd: the SAME validated, finite-positive account equity the cap
            ceiling is denominated against (build_aggregate_tick_budget passes
            the already-checked equity), so the consumed side and the ceiling
            share one NAV reference.

    Returns:
        0.0 for an empty/absent book (BYTE-IDENTICAL to the prior consumed=0
        behavior — equity × 0 = 0). The positive gross USD when positions are
        open. None is a fail-closed input on ANY error (unreadable state.db,
        schema drift) or a non-finite/negative result: callers silence would-be
        fires rather than assuming an empty book.
    """
    try:
        from hermes_quant.state.portfolio_state import get_portfolio_state

        positions = get_portfolio_state().get_positions(PAPER_ACCOUNT_ID)
        gross_nav_fraction = sum(abs(float(pos.quantity)) for pos in positions.values())
        gross = equity_usd * gross_nav_fraction
    except Exception:
        return None
    if not math.isfinite(gross) or gross < 0:
        return None
    return gross


class AggregateTickBudget:
    """Tick-local notional accumulator for playbook/hourly direct Alpaca fires.

    This is intentionally a tick-level guard, not the durable ADR-0087 seam
    implementation. It reuses the canonical PortfolioCaps/PortfolioState
    headroom math to derive the gross cap, then counts only successful orders
    placed by this cron invocation.
    """

    def __init__(
        self,
        *,
        ceiling_usd: float | None,
        consumed_usd: float = 0.0,
        failure_reason: str | None = None,
    ) -> None:
        self.ceiling_usd = ceiling_usd
        self.consumed_usd = consumed_usd
        self.failure_reason = failure_reason

    @staticmethod
    def finite_positive_or_none(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or f <= 0:
            return None
        return f

    def check(self, notional_usd: float) -> tuple[bool, str | None]:
        if self.failure_reason:
            return False, f"portfolio_cap_aggregate_breach: {self.failure_reason}"

        notional = self.finite_positive_or_none(notional_usd)
        if notional is None:
            return (
                False,
                f"portfolio_cap_aggregate_breach: non_finite_notional notional_usd={notional_usd!r}",
            )

        if (
            self.ceiling_usd is None
            or not math.isfinite(self.ceiling_usd)
            or self.ceiling_usd <= 0
        ):
            return (
                False,
                f"portfolio_cap_aggregate_breach: invalid_ceiling ceiling_usd={self.ceiling_usd!r}",
            )

        if not math.isfinite(self.consumed_usd) or self.consumed_usd < 0:
            return (
                False,
                f"portfolio_cap_aggregate_breach: invalid_consumed consumed_usd={self.consumed_usd!r}",
            )

        prospective = self.consumed_usd + notional
        if not math.isfinite(prospective):
            return (
                False,
                "portfolio_cap_aggregate_breach: non_finite_prospective_notional",
            )

        if prospective > self.ceiling_usd + 1e-9:
            return (
                False,
                "portfolio_cap_aggregate_breach: "
                f"requested_notional_usd={notional:.2f} "
                f"consumed_usd={self.consumed_usd:.2f} "
                f"ceiling_usd={self.ceiling_usd:.2f}",
            )

        return True, None

    def record_placed(self, notional_usd: float) -> None:
        notional = self.finite_positive_or_none(notional_usd)
        if notional is not None:
            self.consumed_usd += notional

    def journal_fields(self) -> dict[str, Any]:
        return {
            "aggregate_cap_ceiling_usd": self.ceiling_usd,
            "aggregate_cap_consumed_usd": self.consumed_usd,
        }


def build_aggregate_tick_budget() -> AggregateTickBudget | None:
    """Build the tick-local aggregate cap budget when the operator flag is ON.

    OFF returns None without importing risk helpers or reading Alpaca, preserving
    the current per-fire-only behavior.
    """
    if not _aggregate_cap_enabled():
        return None

    equity = read_alpaca_account_equity()
    if equity is None or not math.isfinite(equity) or equity <= 0:
        return AggregateTickBudget(
            ceiling_usd=None,
            failure_reason="account_equity_unavailable_or_non_finite",
        )

    try:
        from hermes_quant.risk.portfolio_normalize import (
            PortfolioCaps,
            PortfolioState,
            headroom_summary,
        )

        caps = PortfolioCaps.standard()
        state = PortfolioState()
        gross_headroom = float(headroom_summary(state, caps).get("gross_headroom"))
    except Exception as exc:
        return AggregateTickBudget(
            ceiling_usd=None,
            failure_reason=f"portfolio_headroom_unavailable:{type(exc).__name__}",
        )

    if not math.isfinite(gross_headroom) or gross_headroom <= 0:
        return AggregateTickBudget(
            ceiling_usd=None,
            failure_reason=f"gross_headroom_non_finite_or_non_positive:{gross_headroom!r}",
        )

    ceiling = equity * gross_headroom
    if not math.isfinite(ceiling) or ceiling <= 0:
        return AggregateTickBudget(
            ceiling_usd=None,
            failure_reason=f"aggregate_ceiling_non_finite_or_non_positive:{ceiling!r}",
        )

    # cap2: seed consumed_usd from the REAL open book so the gross ceiling is
    # enforced against the true exposure, not just this tick's own fires. The
    # ceiling is USD (equity × gross_headroom); the consumed side is the same-unit
    # USD gross of the open positions = equity × Σ|quantity| (quantity is a
    # NAV-fraction — see read_real_open_positions_gross_usd). We pass the SAME
    # validated finite-positive `equity` the ceiling uses so both share one NAV
    # reference. An empty/absent book yields 0.0 → byte-identical to the prior
    # consumed=0 behavior. A None (unreadable book) is a fail-closed input:
    # silence every fire rather than under-count.
    real_consumed = read_real_open_positions_gross_usd(equity)
    if real_consumed is None:
        return AggregateTickBudget(
            ceiling_usd=None,
            failure_reason="open_book_unavailable_or_non_finite",
        )

    return AggregateTickBudget(ceiling_usd=ceiling, consumed_usd=real_consumed)


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
        import contextlib
        import io

        import yfinance as yf
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
        import contextlib
        import io

        import yfinance as yf
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

    Env-gated extensions per ADR-0036 (multi-timeframe) and ADR-0037 (LLM committee):

    HERMES_QUANT_HORIZONS=1d,1w  → run advisor.recommend_multi_horizon to
        collect views across multiple horizons. Comma-separated list.
        Default behavior (env unset) is the legacy single-horizon `1d` path
        — bit-identical. Allowed horizons: 1d, 1w, 1M, 1Q. Anything else
        is silently dropped with a caveat. The aggregated dict format is
        unchanged; the new keys 'horizons_present' and 'horizons_attempted'
        are added.

    HERMES_QUANT_DELIBERATIVE=1  → after the advisor produces the result,
        invoke the LLM committee (ADR-0037 — bull/bear/judge) and attach
        the resulting CommitteeTurn objects to the result dict under
        'committee_turns' for journal audit. This is **shadow mode** by
        default — committee output is logged, but the BMA-driven
        risk_gate is what drives firing.

    HERMES_QUANT_DELIBERATIVE_PROMOTE=1 → tag committee_decision into the
        result dict, but **DO NOT** override the gate decision (ADR-0037
        promotion path requires a deeper integration deferred to a later
        wave; safer to keep promotion as a journal-only signal for now).

    HERMES_QUANT_DELIBERATIVE_RISK=1 → enable the risk-mgmt triumvirate
        (aggressive/conservative/neutral). Only fires when DELIBERATIVE=1.

    All extensions fail-closed: import errors, LLM failures, or invalid
    env-var values cause the function to fall back to the existing single-
    horizon path. Caveats are surfaced in the result.
    """
    if _is_mock_mode():
        return _mock_recommend(symbol)

    horizons_env = os.environ.get("HERMES_QUANT_HORIZONS", "").strip()
    deliberative_env = os.environ.get("HERMES_QUANT_DELIBERATIVE", "").strip() == "1"

    # Always go through recommend() to get the canonical result dict.
    # Multi-horizon and committee are layered on top.
    try:
        from hermes_quant.advisor import recommend as _recommend
    except Exception as e:
        return {"gate": {"action": "ERROR", "reason": f"import_failed: {e}"}, "caveats": [str(e)]}

    # Pick the primary horizon to run through recommend().
    # If multi-horizon is requested, the BMA inside recommend() doesn't yet
    # see views from other horizons — that requires the recipe-runtime
    # integration. For Wave C, we run on the LONGEST requested horizon
    # (so e.g. "1d,1w" → run on 1w which the existing playbook's signal
    # is built around) and tag the result with all attempted horizons.
    horizons, dropped = _parse_horizons(horizons_env) if horizons_env else (["1d"], [])
    primary_timeframe = horizons[-1] if horizons else "1d"

    # ADR-0079 PDR-1: build the ONE PerceptionFrame and hand it to
    # recommend(perception_frame=). The frame absorbs the semantic slice (the old
    # catalyst->advisor wiring seam), so HERMES_QUANT_SEMANTIC_ENABLED=1 takes
    # effect through the single producer on this path. build_perception_frame_live
    # never raises (returns None on any error); a None frame is identical to not
    # passing one — recommend's None branch behaves exactly as today. The
    # _mock_recommend path above never reaches here.
    try:
        from hermes_quant.perception import build_perception_frame_live
        _frame = build_perception_frame_live(
            symbol, asset_class="equity", timeframe=primary_timeframe
        )
    except Exception:  # noqa: BLE001 — never block the tick on frame building
        _frame = None
    try:
        result = _recommend(symbol, asset_class="equity",
                            timeframe=primary_timeframe, perception_frame=_frame)
    except Exception as e:
        return {"gate": {"action": "ERROR", "reason": f"advisor_exception: {type(e).__name__}: {e}"},
                "caveats": [traceback.format_exc()]}

    # Attach multi-horizon metadata.
    if horizons_env:
        result.setdefault("caveats", [])
        if dropped:
            result["caveats"].append(f"horizons_env_dropped: {dropped}")
        result["horizons_attempted"] = horizons
        result["primary_timeframe"] = primary_timeframe
        # Fan out to collect per-horizon views for audit trail (no aggregation
        # yet — that's the recipe-runtime integration path deferred to a
        # follow-up wave).
        result["multi_horizon_views"] = _collect_multi_horizon_views_safe(symbol, horizons)

    # Optional LLM committee phase (shadow mode).
    if deliberative_env:
        committee_summary = _run_committee_safe(
            symbol=symbol,
            advisor_result=result,
            risk_mgmt_enabled=os.environ.get("HERMES_QUANT_DELIBERATIVE_RISK", "").strip() == "1",
        )
        result["committee_turns"] = committee_summary.get("turns", [])
        result["committee_decision"] = committee_summary.get("decision")
        if committee_summary.get("error"):
            result.setdefault("caveats", []).append(
                f"deliberative_failed: {committee_summary['error']}"
            )

    return result


_ALLOWED_HORIZONS = ("1d", "1w", "1M", "1Q")


def _parse_horizons(horizons_env: str) -> tuple[list[str], list[str]]:
    """Parse HERMES_QUANT_HORIZONS env var. Returns (valid, dropped)."""
    if not horizons_env:
        return ["1d"], []
    valid: list[str] = []
    dropped: list[str] = []
    for raw in horizons_env.split(","):
        h = raw.strip()
        if not h:
            continue
        if h in _ALLOWED_HORIZONS and h not in valid:
            valid.append(h)
        else:
            dropped.append(h)
    if not valid:
        valid = ["1d"]
    return valid, dropped


def _collect_multi_horizon_views_safe(symbol: str, horizons: list[str]) -> list[dict[str, Any]]:
    """Best-effort fan-out to recommend_multi_horizon. Returns view summaries
    or empty list on any failure."""
    try:
        from hermes_quant.advisor import recommend_multi_horizon
        views = recommend_multi_horizon(symbol, horizons=horizons, asset_class="equity")
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for v in views:
        try:
            out.append({
                "analyst": getattr(v, "analyst", "?"),
                "horizon": getattr(v, "horizon", "?"),
                "direction": getattr(getattr(v, "direction", None), "value",
                                      str(getattr(v, "direction", "?"))),
                "magnitude": float(getattr(v, "magnitude", 0.0)),
                "confidence": float(getattr(v, "confidence", 0.0)),
            })
        except Exception:
            continue
    return out


def _run_committee_safe(
    *,
    symbol: str,
    advisor_result: dict[str, Any],
    risk_mgmt_enabled: bool,
) -> dict[str, Any]:
    """Best-effort LLM committee invocation. Never raises. Returns a dict
    with 'turns' (list of turn dicts), 'decision' (judge summary or None),
    and 'error' (string or None).

    Threads the dict-shape ``advisor_result`` through dataclass
    reconstruction so ``run_llm_committee`` can consume it. The advisor
    stores analyst_views and aggregated_signal as dicts (for JSONL
    portability); we rebuild minimal MarketContext / AnalystView /
    AggregatedSignal dataclasses with just the fields the committee
    callers reference, then invoke the LLM committee.

    Failure-closed (ADR-0037): any exception during reconstruction or
    committee call -> empty result with the error captured. Never raises
    because this runs inside the daily playbook tick which must stay
    silent on partial failures.
    """
    try:
        # Short-circuits ---------------------------------------------------
        env_flag = os.environ.get("HERMES_QUANT_DELIBERATIVE", "").strip()
        if env_flag != "1":
            return {
                "turns": [],
                "decision": None,
                "error": None,
                "deferred_reason": "deliberative_env_not_set",
            }

        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key or api_key == "test-placeholder":
            return {
                "turns": [],
                "decision": None,
                "error": None,
                "deferred_reason": "openrouter_key_unset_or_placeholder",
            }

        # Lazy imports — keep module-load light when DELIBERATIVE=0.
        import pandas as pd

        from hermes_quant.aggregators.deliberative import DeliberativeConfig
        from hermes_quant.aggregators.llm_committee import run_llm_committee
        from hermes_quant.protocol import (
            AggregatedSignal,
            AnalystView,
            MarketContext,
        )

        # Reconstruct MarketContext (minimal — only fields the committee
        # prompts cite; bars is required by the dataclass but the
        # committee prompt path never reads it past last_close).
        sig_d = advisor_result.get("aggregated_signal") or {}
        timeframe = str(sig_d.get("timeframe") or advisor_result.get("timeframe") or "1d")
        asset_class = str(sig_d.get("asset_class") or advisor_result.get("asset_class") or "equity")
        last_close = float(advisor_result.get("last_close") or 0.0)
        asof = pd.to_datetime(
            sig_d.get("asof") or advisor_result.get("asof") or pd.Timestamp.utcnow(),
            utc=True,
        ).tz_localize(None) if pd.to_datetime(
            sig_d.get("asof") or advisor_result.get("asof") or pd.Timestamp.utcnow(),
            utc=True,
        ).tzinfo is not None else pd.to_datetime(
            sig_d.get("asof") or advisor_result.get("asof") or pd.Timestamp.utcnow()
        )

        # Minimal stub bars frame so MarketContext(bars=...) constructor
        # validation (if any) doesn't reject. Only last_close is read by
        # the committee prompt template.
        empty_bars = pd.DataFrame(
            {
                "timestamp": [asof],
                "open": [last_close or 1.0],
                "high": [last_close or 1.0],
                "low": [last_close or 1.0],
                "close": [last_close or 1.0],
                "volume": [0.0],
            }
        )

        ctx = MarketContext(
            asset=symbol,
            timeframe=timeframe,
            asset_class=asset_class,
            exchange=None,
            bars=empty_bars,
            last_close=last_close or 1.0,
            last_volume=0.0,
            asof=asof,
        )

        # Reconstruct list[AnalystView] from the dict form. Drop entries
        # that don't carry the minimum required fields rather than raise.
        analyst_view_dicts = advisor_result.get("analyst_views") or []
        views: list[AnalystView] = []
        for vd in analyst_view_dicts:
            try:
                # NOTE: Direction is a Literal[-1, 0, 1], not an Enum/class.
                # Don't `Direction(int(...))` — that raises TypeError because
                # you can't call a Literal. Pass the int directly; Pydantic
                # validates the Literal at construction time.
                dir_int = int(vd["direction"])
                if dir_int not in (-1, 0, 1):
                    continue
                views.append(
                    AnalystView(
                        analyst=str(vd["analyst"]),
                        direction=dir_int,  # type: ignore[arg-type]
                        magnitude=float(vd["magnitude"]),
                        confidence=float(vd["confidence"]),
                        confidence_raw=float(vd.get("confidence_raw", vd["confidence"])),
                        horizon=str(vd.get("horizon", timeframe)),
                        rationale=vd.get("rationale"),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue

        if not views:
            return {
                "turns": [],
                "decision": None,
                "error": None,
                "deferred_reason": "no_reconstructable_analyst_views",
            }

        # Reconstruct AggregatedSignal from the dict form.
        # See note above re Direction Literal — pass int directly.
        baseline_dir_int = int(sig_d.get("direction", 0))
        if baseline_dir_int not in (-1, 0, 1):
            baseline_dir_int = 0
        baseline_signal = AggregatedSignal(
            asset=str(sig_d.get("asset", symbol)),
            timeframe=timeframe,
            asset_class=asset_class,
            asof=asof,
            direction=baseline_dir_int,  # type: ignore[arg-type]
            magnitude=float(sig_d.get("magnitude", 0.0)),
            confidence=float(sig_d.get("confidence", 0.0)),
            confidence_raw=float(sig_d.get("confidence_raw", sig_d.get("confidence", 0.0))),
            horizon=str(sig_d.get("horizon", timeframe)),
            aggregator=str(sig_d.get("aggregator", "bma")),
            components=tuple(views),  # placeholder reconstruction is fine for the LLM
        )

        # DeliberativeConfig — pull defaults; only enable_llm_turns + the
        # risk-mgmt switch + max_debate_rounds matter to the committee.
        cfg = DeliberativeConfig(
            enable_llm_turns=True,
            enable_risk_mgmt=risk_mgmt_enabled,
            quick_model=os.environ.get(
                "HERMES_QUANT_DELIBERATIVE_QUICK_MODEL",
                "anthropic/claude-haiku-4.5",
            ),
            deep_model=os.environ.get(
                "HERMES_QUANT_DELIBERATIVE_DEEP_MODEL",
                "anthropic/claude-sonnet-4.6",
            ),
        )

        # Run the LLM committee. Failure-closed inside run_llm_committee
        # already; we wrap once more in case the constructor itself raises.
        turns = run_llm_committee(
            market_context=ctx,
            analyst_views=views,
            baseline_signal=baseline_signal,
            config=cfg,
        )

        # Project turns to dicts for the JSONL audit log.
        turn_dicts: list[dict[str, Any]] = []
        decision: dict[str, Any] | None = None
        for t in turns:
            try:
                td = {
                    "role": getattr(t, "role", "?"),
                    "model": getattr(t, "model", None),
                    "stance": getattr(t, "stance", None),
                    "confidence": getattr(t, "confidence", None),
                    "decision_multiplier": getattr(t, "decision_multiplier", 1.0),
                    "prompt_hash": (t.metadata or {}).get("prompt_hash") if hasattr(t, "metadata") else None,
                }
                turn_dicts.append(td)
                if td["role"] == "portfolio_manager":
                    decision = {
                        "rating": getattr(t, "rating", None),
                        "decision_multiplier": td["decision_multiplier"],
                        "stance": td["stance"],
                    }
            except Exception:  # noqa: BLE001 — never raise from audit projection
                continue

        return {
            "turns": turn_dicts,
            "decision": decision,
            "error": None,
            "n_turns": len(turn_dicts),
        }
    except Exception as e:  # noqa: BLE001 — fail-closed
        return {"turns": [], "decision": None, "error": f"{type(e).__name__}: {e}"}


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
    aggregate_budget: AggregateTickBudget | None = None,
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
        # Dry-run never POSTs, so it needs neither the fire-lock nor a
        # client_order_id — keep this path byte-identical to the prior behavior.
        return {**base,
                "decision": "fire",
                "gate": "FIRE",
                "confidence": confidence,
                "reason": reason,
                "notional_usd": notional,
                "order_id": None,
                "dry_run_note": "no order placed (--dry-run)"}

    if aggregate_budget is not None:
        cap_ok, cap_reason = aggregate_budget.check(notional)
        if not cap_ok:
            journal_notional = AggregateTickBudget.finite_positive_or_none(notional)
            return {**base,
                    "decision": "silenced",
                    "gate": "FIRE",
                    "confidence": confidence,
                    "reason": cap_reason or "portfolio_cap_aggregate_breach",
                    "notional_usd": journal_notional,
                    **aggregate_budget.journal_fields()}

    # Cross-process fire serialization (parity with react/paper.py symbol_tick_lock):
    # acquire a per-(symbol, play) advisory flock, then RE-READ the authoritative
    # on-disk fired set inside the lock. This closes the read-once-fired_set TOCTOU
    # — a concurrent armed run that already POSTed + journaled this (symbol, play)
    # is now visible, so we skip instead of double-firing. The deterministic
    # client_order_id is the broker-level backstop if flock is unavailable.
    with fire_lock(symbol, play) as locked:
        if locked and (symbol, play) in fired_today_pairs():
            return {**base, "decision": "idempotent_skip",
                    "reason": "already fired today (concurrent run won the fire-lock)"}

        client_order_id = build_client_order_id(today_et, symbol, play)
        order = place_paper_market_order(
            symbol, notional, side="buy", client_order_id=client_order_id,
        )
        if "error" in order:
            return {**base, "decision": "gate_reject",
                    "gate": "FIRE_BUT_ROUTE_FAILED",
                    "confidence": confidence,
                    "reason": f"alpaca route failed: {order.get('error')}: {order.get('detail','')}",
                    "notional_usd": notional,
                    "client_order_id": client_order_id}

        if aggregate_budget is not None:
            aggregate_budget.record_placed(notional)

        fire_rec = {**base,
                    "decision": "fire",
                    "gate": "FIRE",
                    "confidence": confidence,
                    "reason": reason,
                    "notional_usd": notional,
                    "order_id": order.get("id"),
                    "client_order_id": order.get("client_order_id") or client_order_id,
                    "submitted_at": order.get("submitted_at")}
        # Journal the fire BEFORE releasing the lock so the NEXT contender's
        # in-lock fired_today_pairs() re-read sees this slot as claimed. The
        # _journaled marker tells run_tick not to append a duplicate line.
        append_journal(fire_rec)
        return {**fire_rec, "_journaled": True}


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
    aggregate_budget = build_aggregate_tick_budget() if not dry_run else None

    for symbol, play, score in pairs:
        try:
            rec = process_pair(
                symbol, play, score,
                today_et=today_et,
                tick_id=tick_id,
                fired_set=fired_set,
                dry_run=dry_run,
                aggregate_budget=aggregate_budget,
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
        # A real fire is journaled inside process_pair under the fire-lock (so
        # the next contender's in-lock re-read sees the claimed slot). The
        # _journaled marker means "already on disk" — don't double-append it.
        if rec.pop("_journaled", False):
            pass
        else:
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
    """Render the daily playbook tick summary as Discord-friendly markdown.

    Tiered emit (matches autonomous-tick + hourly-tick):
      - halt_aborted → 🚨 loud halt notice
      - errors > 0   → ⚠️ loud error notice
      - fired > 0    → 📈 lead with fire count + per-(symbol, play) detail
      - silenced > 0 OR gate_rejected > 0 (real signals) → 🔕 single-line summary
      - all-clear and nothing-tried → "" (silent heartbeat)

    --always-print bypasses the silent path for debug.
    """
    et_now = datetime.now(UTC).astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET")
    halt_aborted = s.get("halt_aborted", False)
    fired = s.get("fired", 0)
    silenced = s.get("silenced", 0)
    gate_rejected = s.get("gate_rejected", 0)
    skipped = s.get("idempotent_skipped", 0)
    errors = s.get("errors", 0)
    scanned = s.get("scanned", 0)
    dry_run = s.get("dry_run", False)
    mode_tag = "🧪 dry-run" if dry_run else "📦 paper"

    # Hard-fail headlines — loud and explicit
    if halt_aborted:
        return f"🚨 **playbook tick HALT-ABORTED** ({mode_tag}, {et_now})\n> Active halts present in halt_state.json — fail-closed."
    if errors > 0:
        return (
            f"⚠️ **playbook tick: {errors} error(s)** ({mode_tag}, {et_now})\n"
            f"```\nscanned={scanned} fired={fired} silenced={silenced} "
            f"gate_rejected={gate_rejected} errors={errors}\n```"
        )

    # Fires — call out the count + stats. Per-(symbol, play) detail lives
    # in tick-journal.jsonl; the brief surfaces just the rollup.
    if fired > 0:
        return (
            f"📈 **playbook tick: {fired} fire(s)** ({mode_tag}, {et_now})\n"
            f"```\n"
            f"scanned={scanned} fired={fired} silenced={silenced} "
            f"gate_rejected={gate_rejected} idempotent_skipped={skipped}\n```"
        )

    # No fire but real signals processed — terse single-line summary
    if silenced > 0 or gate_rejected > 0:
        return (
            f"🔕 playbook tick: scanned={scanned}, "
            f"silenced={silenced}, gate_rejected={gate_rejected} "
            f"(no fire) — {mode_tag}, {et_now}"
        )

    # Truly nothing happened (empty universe, no signals at all) — silent
    return ""


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
        rendered = render_summary(summary)
        if rendered:  # render_summary may return "" for fully-silent cases
            print(rendered, flush=True)
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
