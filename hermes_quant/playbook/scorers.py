"""Per-play fitness scorers + enriched-snapshot helper.

Scoring contract (per play):
    score = 0.6 * (n_hard_passed / n_hard) + 0.4 * (n_soft_passed / n_soft)
    pass_hard  = all hard rules pass
    pass_soft  = at least 50% of soft rules pass
    eligible   = score >= 0.65 AND pass_hard

Silence-by-default:
    * If a hard rule's input field is None, the rule FAILS.
    * If a soft rule's input field is None, the rule fails (counts as a miss)
      but the symbol is not rejected on that basis alone.
    * Eviction rules that need a missing field do NOT fire (we don't evict on
      lack-of-data; we just deny eligibility via failed hard rules).

This module is intentionally light on dependencies. yfinance is imported
lazily inside compute_play_snapshot so unit tests don't require it.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .profiles import PROFILES, PlayProfile

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Canonical non-equity quote_type vocabulary (single source of truth)
# --------------------------------------------------------------------------- #
#
# The yfinance ``quoteType`` strings that denote a NON-equity instrument, i.e. one
# that has no earnings cycle and whose equity-specific fundamentals (P/E, D/E, FCF,
# revenue YoY, …) are meaningless. The provider writes ANY ``info["quoteType"]``
# verbatim into the snapshot (fundamentals_provider.py: ``str(info.get("quoteType")
# or "")``), so every consumer that treats a snapshot as equity-shaped MUST gate on
# this set, not on a single string.
#
# cs47: previously this set was inlined TWICE below (the earnings-lookup skip and
# its legacy-calendar fallback) and a THIRD, narrower copy ('ETF' only) lived in
# the FundamentalsAnalyst post-fetch gate — so a MUTUALFUND/INDEX/CURRENCY/
# CRYPTOCURRENCY snapshot reached the analyst and was scored as a stock (an
# ADR-0004 gate input). Hoisting to ONE frozenset, consumed by both sites here AND
# by ``analysts.fundamentals`` (which imports THIS name), makes the abstain
# vocabulary single-sourced and keeps the analyst's gate in lockstep with the
# provider's own enumeration. Strings are upper-cased on write (line ~585), so
# membership is case-exact against upper-case members.
NON_EQUITY_QUOTE_TYPES: frozenset[str] = frozenset(
    {"ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY"}
)

# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #


@dataclass
class PlayFitness:
    """Result of scoring one symbol against one PlayProfile."""

    play: str
    symbol: str
    score: float  # in [0, 1]
    pass_hard: bool
    pass_soft: bool
    eligible: bool
    failed_rules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rule evaluation
# --------------------------------------------------------------------------- #


def _is_none_or_nan(x: Any) -> bool:
    if x is None:
        return True
    try:
        if isinstance(x, float) and math.isnan(x):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _eval_rule(value: Any, rule: tuple) -> bool | None:
    """Evaluate a single rule against a value.

    Returns:
        True  — rule passed
        False — rule failed
        None  — input was None/NaN; caller decides hard vs soft semantics

    'or' / 'any_of' / 'lt_field' / 'gt_field' rules are evaluated specially.
    """
    op = rule[0]

    if op == "or":
        # ("or", rule_a, rule_b)
        a = _eval_rule(value, rule[1])
        b = _eval_rule(value, rule[2])
        if a is True or b is True:
            return True
        if a is None and b is None:
            return None
        return False

    if op == "any_of":
        any_passed = False
        any_unknown = False
        for sub in rule[1:]:
            r = _eval_rule(value, sub)
            if r is True:
                any_passed = True
            elif r is None:
                any_unknown = True
        if any_passed:
            return True
        if any_unknown:
            return None
        return False

    if _is_none_or_nan(value):
        return None

    if op == "between":
        lo, hi = rule[1], rule[2]
        return lo <= value <= hi
    if op == "ge":
        return value >= rule[1]
    if op == "gt":
        return value > rule[1]
    if op == "le":
        return value <= rule[1]
    if op == "lt":
        return value < rule[1]
    if op == "eq":
        return value == rule[1]
    if op == "in":
        # ("in", (allowed_a, allowed_b, ...))
        allowed = rule[1]
        return value in allowed
    if op == "nonzero_window":
        lo, hi = rule[1], rule[2]
        return (lo < value < hi) and value != 0

    raise ValueError(f"unknown rule op: {op!r}")


def _eval_eviction(snapshot: dict, rule: tuple) -> bool:
    """Eviction rules use 'lt_field'/'gt_field' which name the field inline.

    Missing data does NOT trigger eviction (we'd rather hold than evict on
    silence — eligibility will fail elsewhere).
    """
    op = rule[0]
    if op == "lt_field":
        _, field_name, threshold = rule
        v = snapshot.get(field_name)
        if _is_none_or_nan(v):
            return False
        return v < threshold
    if op == "gt_field":
        _, field_name, threshold = rule
        v = snapshot.get(field_name)
        if _is_none_or_nan(v):
            return False
        return v > threshold
    if op == "ne_field":
        # ("ne_field", field_name, expected_value) — evict if field != expected.
        # Treats missing data as NOT triggering eviction (silence-friendly).
        _, field_name, expected = rule
        v = snapshot.get(field_name)
        if _is_none_or_nan(v):
            return False
        return v != expected
    if op == "not_in_field":
        # ("not_in_field", field_name, (allowed_a, ...)) — evict if not in set.
        _, field_name, allowed = rule
        v = snapshot.get(field_name)
        if _is_none_or_nan(v):
            return False
        return v not in allowed
    raise ValueError(f"unknown eviction op: {op!r}")


# --------------------------------------------------------------------------- #
# Generic scorer
# --------------------------------------------------------------------------- #


def _eval_regime_gate(profile: PlayProfile, snapshot: dict) -> tuple[str, str | None]:
    """Evaluate the play's regime gate against the current snapshot regime.

    Returns:
        (action, reason)
        - action ∈ {"allow", "warn", "deny"} — derived from profile.regime_gates[label]
          where label is the str-value of the snapshot's RegimeState. If the
          profile has no regime_gates entries (legacy profiles), or the snapshot
          has no regime info, returns ("allow", None) — bit-identical to
          pre-2026-05-28 behavior.
        - reason: short human-readable string when action != "allow"; else None.

    The snapshot "regime" key MAY be a RegimePacket (post-ADR-0063) OR a plain
    string label (legacy / synthetic snapshots). We normalize both.
    """
    if not profile.regime_gates:
        return ("allow", None)
    regime = snapshot.get("regime")
    if regime is None:
        return ("allow", None)
    # Extract string label from RegimePacket OR plain string.
    if hasattr(regime, "label"):
        label = str(regime.label).lower()
    elif isinstance(regime, str):
        label = regime.lower()
    elif isinstance(regime, dict):
        label = str(regime.get("label", "")).lower()
    else:
        return ("allow", None)
    action = profile.regime_gates.get(label, "allow")
    if action == "allow":
        return ("allow", None)
    reason = f"regime={label} → {action} for play={profile.name}"
    return (action, reason)


def _score_against(profile: PlayProfile, snapshot: dict) -> PlayFitness:
    symbol = snapshot.get("symbol", "?")
    failed: list[str] = []
    notes: list[str] = []

    # --- evictions ------------------------------------------------------- #
    evicted = False
    for ev_name, ev_rule in profile.eviction_rules.items():
        if _eval_eviction(snapshot, ev_rule):
            failed.append(f"evict:{ev_name}")
            evicted = True
    if evicted:
        notes.append("symbol marked for eviction; eligibility forced false")

    # --- regime gate (ADR-0063 + ADR-0035 amendment 2026-05-28) ---------- #
    # Read the regime classifier output from snapshot["regime"]. Profiles that
    # don't define regime_gates (legacy) get bit-identical pre-2026-05-28 scoring.
    regime_action, regime_reason = _eval_regime_gate(profile, snapshot)
    regime_warned = False
    if regime_action == "deny":
        failed.append(f"regime_gate:{regime_reason}")
        evicted = True  # treat as eviction — score 0, eligible False, with rationale
        notes.append(regime_reason or "regime gate denied")
    elif regime_action == "warn":
        regime_warned = True
        notes.append(regime_reason or "regime gate warning")

    # --- hard rules ------------------------------------------------------ #
    n_hard = len(profile.hard_rules)
    n_hard_pass = 0
    for fname, rule in profile.hard_rules.items():
        v = snapshot.get(fname)
        result = _eval_rule(v, rule)
        if result is True:
            n_hard_pass += 1
        else:
            # None or False both count as fail for hard rules (silence-by-default)
            if result is None:
                failed.append(f"hard:{fname}=None")
            else:
                failed.append(f"hard:{fname}={v!r}")
    pass_hard = (n_hard_pass == n_hard) and not evicted

    # --- soft rules ------------------------------------------------------ #
    n_soft = len(profile.soft_rules)
    n_soft_pass = 0
    for fname, rule in profile.soft_rules.items():
        v = snapshot.get(fname)
        result = _eval_rule(v, rule)
        if result is True:
            n_soft_pass += 1
        else:
            if result is None:
                notes.append(f"soft:{fname}=None (no data)")
            else:
                notes.append(f"soft:{fname}={v!r} miss")
    pass_soft = (n_soft_pass / n_soft) >= 0.5 if n_soft else True

    # --- score ----------------------------------------------------------- #
    hard_frac = (n_hard_pass / n_hard) if n_hard else 1.0
    soft_frac = (n_soft_pass / n_soft) if n_soft else 1.0
    score = 0.6 * hard_frac + 0.4 * soft_frac
    score = max(0.0, min(1.0, score))

    # Apply 30% score penalty for regime-warn (per ADR-0035 amendment 2026-05-28).
    if regime_warned:
        score = score * 0.7
        notes.append(f"score reduced by 30% due to regime warn (final={score:.4f})")

    eligible = bool(pass_hard and score >= 0.65 and not evicted)

    return PlayFitness(
        play=profile.name,
        symbol=str(symbol),
        score=round(score, 4),
        pass_hard=pass_hard,
        pass_soft=pass_soft,
        eligible=eligible,
        failed_rules=failed,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Play registry — derived from PROFILES (single source of truth, ADR-0082 Pt A)
# --------------------------------------------------------------------------- #
#
# Every play's eligibility/score is computed by the generic `_score_against`
# scorer over its PlayProfile. ONE play (wheel) layers extra cross-play logic
# on top of the generic result; that logic lives in `_score_wheel` and is wired
# in via the `_SPECIAL_SCORERS` override map below. Everything else — the
# `score_all` dict, the `PLAY_NAMES` tuple, and the per-play public wrappers —
# is DERIVED from `PROFILES` so the hand-maintained parallel lists can no longer
# drift (closing the >=4-file-edit-to-add-a-play footgun). Adding a profile to
# `PROFILES` flows through `score_all`/`PLAY_NAMES`/`score_play` automatically;
# only a play needing non-generic eligibility (like wheel) registers an override.


def _score_wheel(snapshot: dict) -> PlayFitness:
    """Wheel eligibility = both covered_call AND csp eligible.

    We *also* run the merged wheel profile to produce a single score, but the
    eligible flag is the AND of the two underlying plays so it's airtight.
    """
    cc = _score_against(PROFILES["covered_call"], snapshot)
    csp = _score_against(PROFILES["csp"], snapshot)
    merged = _score_against(PROFILES["wheel"], snapshot)
    # Wheel eligibility = both legs eligible AND merged profile eligible.
    # The merged eligibility already enforces pass_hard AND score>=0.65 AND
    # not-evicted; we ALSO require both component plays to be eligible.
    merged.eligible = bool(cc.eligible and csp.eligible and merged.eligible)
    if not (cc.eligible and csp.eligible):
        merged.notes.append(
            f"wheel requires CC+CSP both eligible (cc.eligible={cc.eligible}, "
            f"csp.eligible={csp.eligible})"
        )
    return merged


# Override map: play name → custom scorer for plays whose eligibility is NOT a
# plain `_score_against` over their own profile. Plays absent from this map use
# the generic scorer. Keep this map as small as possible — it is the ONLY place
# a play diverges from the registry-derived default.
_SPECIAL_SCORERS: dict[str, Callable[[dict], PlayFitness]] = {
    "wheel": _score_wheel,
}


def score_play(play: str, snapshot: dict) -> PlayFitness:
    """Score a snapshot against one play by name (registry-derived dispatch).

    Looks the play up in PROFILES; applies the play's override scorer if it has
    one (see `_SPECIAL_SCORERS`), else the generic `_score_against`. Raises
    KeyError for an unknown play name — callers should pass a name from
    `PLAY_NAMES` / `PROFILES`.
    """
    profile = PROFILES[play]  # KeyError on unknown play — fail loud, not silent
    special = _SPECIAL_SCORERS.get(play)
    if special is not None:
        return special(snapshot)
    return _score_against(profile, snapshot)


def score_all(snapshot: dict) -> dict[str, PlayFitness]:
    """Score a snapshot against every play. Returns dict keyed by play name.

    Derived from PROFILES (insertion order preserved), so a new profile flows
    through with no edit here — this is ADR-0082 Part A's anti-drift guarantee.
    """
    return {play: score_play(play, snapshot) for play in PROFILES}


# Ordered tuple of every play name, derived from PROFILES insertion order. This
# is the canonical source for `watchlist_evolution.PLAY_NAMES` (re-exported there
# for backward compatibility) so the two can never disagree.
PLAY_NAMES: tuple[str, ...] = tuple(PROFILES.keys())


# --------------------------------------------------------------------------- #
# Public per-play wrappers (backward-compat; thin shims over `score_play`)
# --------------------------------------------------------------------------- #
# These named entry points are preserved for existing callers/tests. Each just
# dispatches through the registry, so they cannot drift from `score_all`.


def score_covered_call(snapshot: dict) -> PlayFitness:
    return score_play("covered_call", snapshot)


def score_csp(snapshot: dict) -> PlayFitness:
    return score_play("csp", snapshot)


def score_wheel(snapshot: dict) -> PlayFitness:
    return score_play("wheel", snapshot)


def score_leaps(snapshot: dict) -> PlayFitness:
    return score_play("leaps", snapshot)


def score_swing(snapshot: dict) -> PlayFitness:
    return score_play("swing", snapshot)


# --------------------------------------------------------------------------- #
# Snapshot builder (yfinance-backed, best-effort)
# --------------------------------------------------------------------------- #

# Sentinel keys for the snapshot. Documented for callers/tests.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "symbol",
    "asof",
    "last_close",
    "market_cap_usd",
    "avg_dollar_volume_30d",
    "realized_vol_30d",
    "rsi_14",
    "atr_14",
    "atr_pct_of_spot",
    "distance_from_52w_high_pct",
    "five_d_return_pct",
    "dividend_yield",
    "debt_to_equity",
    "beta",
    "free_cash_flow_yield",
    "return_on_equity",
    "gross_margin",
    "revenue_growth_yoy",
    "days_since_earnings",
    "quote_type",
)


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / period


def _realized_vol(closes: list[float], window: int = 30) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = []
    for i in range(-window, 0):
        if closes[i - 1] <= 0:
            return None
        rets.append(math.log(closes[i] / closes[i - 1]))
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def compute_play_snapshot(symbol: str, asof: date | datetime | None = None) -> dict:
    """Build an enriched per-symbol snapshot using yfinance.

    All fundamentals are best-effort: any unavailable value falls back to None
    and the scorer treats it as a hard-rule fail / soft-rule miss.

    This function is intentionally pragmatic: yfinance has rate limits and
    inconsistent fundamentals coverage; we don't try to be heroic.
    """
    import yfinance as yf  # imported lazily

    if asof is None:
        asof_dt = datetime.now(tz=UTC)
    elif isinstance(asof, date) and not isinstance(asof, datetime):
        asof_dt = datetime(asof.year, asof.month, asof.day, tzinfo=UTC)
    else:
        asof_dt = asof

    snap: dict[str, Any] = {f: None for f in SNAPSHOT_FIELDS}
    snap["symbol"] = symbol
    snap["asof"] = asof_dt.isoformat()

    try:
        tk = yf.Ticker(symbol)
    except Exception:
        return snap

    # --- price history ------------------------------------------------- #
    try:
        hist = tk.history(period="1y", auto_adjust=False)
    except Exception:
        hist = None

    if hist is not None and len(hist) > 0:
        closes = [float(c) for c in hist["Close"].tolist()]
        highs = [float(c) for c in hist["High"].tolist()]
        lows = [float(c) for c in hist["Low"].tolist()]
        volumes = [float(c) for c in hist["Volume"].tolist()]

        snap["last_close"] = closes[-1]

        # ADV30 in dollars
        if len(closes) >= 30:
            recent_dollar = [closes[i] * volumes[i] for i in range(-30, 0)]
            snap["avg_dollar_volume_30d"] = sum(recent_dollar) / 30.0

        snap["realized_vol_30d"] = _realized_vol(closes, window=30)
        snap["rsi_14"] = _rsi(closes, period=14)
        atr = _atr(highs, lows, closes, period=14)
        snap["atr_14"] = atr
        if atr is not None and snap["last_close"]:
            snap["atr_pct_of_spot"] = atr / snap["last_close"]

        # Regime classifier (ADR-0063 + ADR-0035 amendment 2026-05-28).
        # Populates snap["regime"] with a RegimePacket so playbook scorers'
        # regime_gates can deny/warn defensively. Failure-safe: any exception
        # leaves snap["regime"] absent and scorer falls back to allow-everything.
        try:
            from hermes_quant.regime.extras_builder import build_regime_extras
            import pandas as pd
            regime_extras = build_regime_extras(symbol, hist, asof=pd.Timestamp(asof_dt))  # type: ignore[arg-type]
            if regime_extras and regime_extras.get("regime") is not None:
                snap["regime"] = regime_extras["regime"]
        except Exception:
            pass  # silence-by-default; scorer treats missing regime as "allow"

        # 52-week high distance
        try:
            window_52 = closes[-min(252, len(closes)) :]
            hi_52 = max(window_52)
            snap["distance_from_52w_high_pct"] = (snap["last_close"] - hi_52) / hi_52
        except (ValueError, ZeroDivisionError):
            pass

        # 5-day return
        if len(closes) >= 6 and closes[-6] > 0:
            snap["five_d_return_pct"] = (closes[-1] - closes[-6]) / closes[-6]

    # --- fundamentals from .info -------------------------------------- #
    info: dict[str, Any] = {}
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    snap["market_cap_usd"] = _safe_float(info.get("marketCap"))
    snap["dividend_yield"] = _safe_float(info.get("dividendYield"))
    # yfinance reports debtToEquity as a percentage (e.g. 75 = 0.75); divide.
    de_raw = _safe_float(info.get("debtToEquity"))
    if de_raw is not None:
        snap["debt_to_equity"] = de_raw / 100.0 if de_raw > 5 else de_raw
    snap["beta"] = _safe_float(info.get("beta"))
    snap["return_on_equity"] = _safe_float(info.get("returnOnEquity"))
    snap["gross_margin"] = _safe_float(info.get("grossMargins"))
    snap["revenue_growth_yoy"] = _safe_float(info.get("revenueGrowth"))

    # quoteType lets us skip earnings lookups (and other equity-specific paths)
    # for ETFs / mutual funds / indices that don't have earnings cycles.
    qt = info.get("quoteType")
    if isinstance(qt, str):
        snap["quote_type"] = qt.upper()

    # FCF yield = freeCashflow / marketCap (best-effort)
    fcf = _safe_float(info.get("freeCashflow"))
    mcap = snap["market_cap_usd"]
    if fcf is not None and mcap and mcap > 0:
        snap["free_cash_flow_yield"] = fcf / mcap

    # --- earnings date ------------------------------------------------- #
    # We want days SINCE the most recent earnings report, not days until next.
    # yfinance's Ticker.calendar gives the *next* earnings date; we want the
    # most recent past one. Fall back to earnings_dates DataFrame which has
    # both past and future events.
    #
    # ETFs / funds / indices don't have earnings — skip the lookup entirely
    # to avoid (a) HTTP 404 spam on stderr that yfinance can't be told to
    # silence and (b) wasted round-trips on hundreds of universe symbols.
    most_recent_past: datetime | None = None
    if snap["quote_type"] in NON_EQUITY_QUOTE_TYPES:
        # Non-equity: leave days_since_earnings as None and let the
        # post-block default fill in the safe-large placeholder. No HTTP.
        pass
    else:
        # Defense in depth: even for equities, yfinance can spuriously 404 on
        # earnings endpoints and print to stderr regardless of try/except.
        # Suppress stderr around the earnings calls so cron logs stay clean.
        import contextlib as _ctxlib
        import io as _io

        _err_buf = _io.StringIO()
        with _ctxlib.redirect_stderr(_err_buf):
            try:
                ed = tk.earnings_dates  # type: ignore[attr-defined]
                if ed is not None and len(ed) > 0:
                    for ts in ed.index:
                        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                        if isinstance(ts_dt, datetime):
                            if ts_dt.tzinfo is None:
                                ts_dt = ts_dt.replace(tzinfo=UTC)
                            if ts_dt <= asof_dt and (most_recent_past is None or ts_dt > most_recent_past):
                                most_recent_past = ts_dt
            except Exception:
                most_recent_past = None

    if most_recent_past is not None:
        snap["days_since_earnings"] = (asof_dt - most_recent_past).days
    elif snap["quote_type"] not in NON_EQUITY_QUOTE_TYPES:
        # Fall back to legacy calendar (next-earnings); flip sign sensibly.
        # Same stderr-suppression rationale as above.
        import contextlib as _ctxlib
        import io as _io

        _err_buf2 = _io.StringIO()
        with _ctxlib.redirect_stderr(_err_buf2):
            try:
                cal = tk.calendar
                earnings_date: Any = None
                if isinstance(cal, dict):
                    earnings_date = cal.get("Earnings Date")
                    if isinstance(earnings_date, list) and earnings_date:
                        earnings_date = earnings_date[0]
                if earnings_date is not None:
                    to_py = getattr(earnings_date, "to_pydatetime", None)
                    if callable(to_py):
                        earnings_date = to_py()
                    if isinstance(earnings_date, datetime):
                        if earnings_date.tzinfo is None:
                            earnings_date = earnings_date.replace(tzinfo=UTC)
                        delta = (asof_dt - earnings_date).days
                        # Only trust this if it's a past date (positive delta).
                        if delta >= 0:
                            snap["days_since_earnings"] = delta
                    elif isinstance(earnings_date, date):
                        delta = (asof_dt.date() - earnings_date).days
                        if delta >= 0:
                            snap["days_since_earnings"] = delta
            except Exception:
                pass

    # If no earnings date is available, default to a safe-large value so
    # covered_call's days_since_earnings>=5 doesn't auto-fail purely on
    # data-availability. We lean conservative here only because earnings
    # cadence is *the* most commonly missing field in yfinance.
    if snap["days_since_earnings"] is None:
        snap["days_since_earnings"] = 30  # placeholder; treat as "not recent"

    return snap


# ---------------------------------------------------------------------------
# Cron-friendly adapter for the watchlist evolution module.
# evolve_watchlist expects a callable (symbol: str, play: str) -> float.
# ---------------------------------------------------------------------------

# Module-level cache to avoid recomputing the snapshot for the same symbol
# across all 5 plays in one evolution tick. Keyed by (symbol, asof_date_str).
_SNAPSHOT_CACHE: dict[tuple[str, str], dict] = {}

# B14(b): guard _SNAPSHOT_CACHE against concurrent-cron corruption. The cache
# is a process-global dict shared by every caller in the process. When two
# crons (e.g. the prewarm cron and the watchlist-evolution cron, or two
# overlapping evolution ticks) run in the same interpreter they mutate this
# dict from different threads. CPython makes a single dict get/set atomic, but
# score_symbol's read-miss-then-write and prewarm's membership-check-then-write
# are NOT atomic as a unit — interleaving them can double-fetch or, worse,
# expose a half-built snapshot built off a stale asof. The lock makes each
# get-or-build and each membership-check-or-write a single critical section.
# It is a plain in-process Lock (not an RLock) because no path re-enters while
# holding it, and the compute happens OUTSIDE the lock so we never serialize
# the slow yfinance fetch — only the dict touches.
_SNAPSHOT_CACHE_LOCK = threading.Lock()


# Default worker count for the parallel prewarm. yfinance is HTTP-bound (the
# GIL releases on socket I/O), so threads scale well, but we keep this modest
# to avoid hammering Yahoo. Override with HERMES_QUANT_PREWARM_WORKERS.
_DEFAULT_PREWARM_WORKERS = 12


def prewarm_snapshot_cache(
    symbols: list[str],
    asof: date | datetime | None = None,
    *,
    max_workers: int | None = None,
    timeout_per_symbol: float = 30.0,
) -> dict[str, Any]:
    """Pre-populate ``_SNAPSHOT_CACHE`` for ``symbols`` in parallel.

    Each ``compute_play_snapshot`` call is 3 yfinance HTTP requests; running
    them serially across a 500-symbol universe takes 3-5 minutes and routinely
    blows the cron's hard timeout wall. This helper fans the calls out across
    a thread pool (HTTP-bound work, GIL releases on sockets) so the same
    universe completes in 30-60s.

    The cache key matches ``score_symbol`` exactly — ``(SYMBOL, YYYY-MM-DD)``
    in UTC — so subsequent ``score_symbol`` calls for those symbols hit the
    cache and never touch yfinance again that day.

    Silence-by-default: per-symbol failures (yfinance 404s, network blips)
    are caught and logged at debug level only. The symbol's cache entry is
    NOT populated on failure, so ``score_symbol`` will retry it serially
    later — this is the right shape for partial-prewarm robustness.

    Args:
        symbols: Universe to prewarm. Order doesn't matter; duplicates fine.
        asof: As-of date, defaults to ``datetime.now(UTC)``. Used for cache
            keying — pass the same value here that ``score_symbol`` will see
            (i.e. don't prewarm with yesterday's date or the cache misses).
        max_workers: Thread pool size. Defaults to
            ``HERMES_QUANT_PREWARM_WORKERS`` env var, then 12.
        timeout_per_symbol: Per-future ``.result()`` timeout in seconds.
            Slow symbols are skipped, not allowed to wedge the whole pool.

    Returns:
        ``{"prewarmed": int, "skipped": int, "errors": int, "elapsed_s": float}``
        — summary suitable for caller logs / cron stdout.
    """
    if not symbols:
        return {"prewarmed": 0, "skipped": 0, "errors": 0, "elapsed_s": 0.0}

    # Resolve asof once so both prewarm and downstream score_symbol agree on
    # the cache-key date. score_symbol uses datetime.now(UTC) at call time;
    # passing the same instant here keeps them aligned within the run.
    if asof is None:
        asof_dt: datetime = datetime.now(UTC)
    elif isinstance(asof, date) and not isinstance(asof, datetime):
        asof_dt = datetime(asof.year, asof.month, asof.day, tzinfo=UTC)
    else:
        asof_dt = asof
    asof_key = asof_dt.strftime("%Y-%m-%d")

    if max_workers is None:
        env_val = os.environ.get("HERMES_QUANT_PREWARM_WORKERS", "").strip()
        if env_val:
            try:
                max_workers = max(1, int(env_val))
            except ValueError:
                max_workers = _DEFAULT_PREWARM_WORKERS
        else:
            max_workers = _DEFAULT_PREWARM_WORKERS

    # Dedupe + drop already-cached symbols so prewarm is idempotent.
    # B14(b): the membership check reads the shared cache, so take the lock
    # for the read snapshot. We hold it only for the dict gets (fast), not the
    # surrounding loop bookkeeping.
    todo: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for s in symbols:
        if not isinstance(s, str) or not s:
            continue
        upper = s.upper()
        if upper in seen:
            continue
        seen.add(upper)
        with _SNAPSHOT_CACHE_LOCK:
            already_cached = (upper, asof_key) in _SNAPSHOT_CACHE
        if already_cached:
            skipped += 1
            continue
        todo.append(upper)

    if not todo:
        return {"prewarmed": 0, "skipped": skipped, "errors": 0, "elapsed_s": 0.0}

    import time

    t0 = time.perf_counter()
    prewarmed = 0
    errors = 0

    def _fetch_one(sym: str) -> tuple[str, dict | None, BaseException | None]:
        try:
            snap = compute_play_snapshot(sym, asof_dt)
            return sym, snap, None
        except BaseException as exc:  # noqa: BLE001 — we never propagate from a worker
            return sym, None, exc

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="prewarm") as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in todo}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                _, snap, exc = fut.result(timeout=timeout_per_symbol)
            except BaseException as outer_exc:  # noqa: BLE001 — timeout / cancellation
                errors += 1
                logger.debug(
                    "prewarm: future for %s raised on result(): %s", sym, outer_exc
                )
                continue
            if exc is not None or snap is None:
                errors += 1
                logger.debug("prewarm: %s failed inside worker: %s", sym, exc)
                continue
            # B14(b): single atomic write under the shared-cache lock.
            with _SNAPSHOT_CACHE_LOCK:
                _SNAPSHOT_CACHE[(sym, asof_key)] = snap
            prewarmed += 1

    elapsed = time.perf_counter() - t0
    logger.info(
        "prewarm_snapshot_cache: %d prewarmed, %d skipped (already cached), "
        "%d errors in %.1fs (workers=%d, asof=%s)",
        prewarmed,
        skipped,
        errors,
        elapsed,
        max_workers,
        asof_key,
    )
    return {
        "prewarmed": prewarmed,
        "skipped": skipped,
        "errors": errors,
        "elapsed_s": round(elapsed, 2),
    }


def score_symbol(symbol: str, play: str) -> float:
    """Score one symbol on one play. Used by evolve_watchlist as the scorer.

    Caches snapshots per-symbol for the calendar day so the 5 plays don't
    each refetch yfinance fundamentals. The cache resets across days
    (asof key includes UTC date).

    **Critical:** this returns 0.0 (well below evict_floor=0.45) when the
    symbol is *ineligible* for the play (eviction rule fired or hard rule
    failed). This is what bridges PlayFitness.eligible into the watchlist
    evolution loop's float-score abstraction. Without this, the watchlist
    would onboard mega-caps to covered_call etc. \u2014 the eviction-rules-
    not-firing bug Codex review caught (HIGH, 2026-05-26).

    Silence-by-default: any failure returns 0.0 (cannot pass either
    onboard or evict floor; symbol stays in current state).
    """
    asof_dt = datetime.now(UTC)
    cache_key = (symbol.upper(), asof_dt.strftime("%Y-%m-%d"))
    # B14(b): read the shared cache under the lock. The slow compute happens
    # OUTSIDE the lock so concurrent crons never serialize on yfinance; only
    # the dict get/set are inside the critical section.
    with _SNAPSHOT_CACHE_LOCK:
        snap = _SNAPSHOT_CACHE.get(cache_key)
    if snap is None:
        try:
            snap = compute_play_snapshot(symbol, asof_dt)
        except Exception:
            return 0.0
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_CACHE[cache_key] = snap

    try:
        all_fits = score_all(snap)
        fitness = all_fits.get(play)
        if fitness is None:
            return 0.0
        # If the symbol is INELIGIBLE for the play (eviction OR hard-rule
        # failure), return 0.0 so the evolution loop's evict_floor
        # (default 0.45) treats it as a hard reject, not a score blip.
        if not fitness.eligible:
            return 0.0
        return float(fitness.score)
    except Exception:
        return 0.0
