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

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .profiles import PROFILES, PlayProfile

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
    raise ValueError(f"unknown eviction op: {op!r}")


# --------------------------------------------------------------------------- #
# Generic scorer
# --------------------------------------------------------------------------- #


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
# Public per-play wrappers
# --------------------------------------------------------------------------- #


def score_covered_call(snapshot: dict) -> PlayFitness:
    return _score_against(PROFILES["covered_call"], snapshot)


def score_csp(snapshot: dict) -> PlayFitness:
    return _score_against(PROFILES["csp"], snapshot)


def score_wheel(snapshot: dict) -> PlayFitness:
    """Wheel eligibility = both covered_call AND csp eligible.

    We *also* run the merged wheel profile to produce a single score, but the
    eligible flag is the AND of the two underlying plays so it's airtight.
    """
    cc = score_covered_call(snapshot)
    csp = score_csp(snapshot)
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


def score_leaps(snapshot: dict) -> PlayFitness:
    return _score_against(PROFILES["leaps"], snapshot)


def score_swing(snapshot: dict) -> PlayFitness:
    return _score_against(PROFILES["swing"], snapshot)


def score_all(snapshot: dict) -> dict[str, PlayFitness]:
    """Score a snapshot against every play. Returns dict keyed by play name."""
    return {
        "covered_call": score_covered_call(snapshot),
        "csp": score_csp(snapshot),
        "wheel": score_wheel(snapshot),
        "leaps": score_leaps(snapshot),
        "swing": score_swing(snapshot),
    }


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
    most_recent_past: datetime | None = None
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
    else:
        # Fall back to legacy calendar (next-earnings); flip sign sensibly.
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


def score_symbol(symbol: str, play: str) -> float:
    """Score one symbol on one play. Used by evolve_watchlist as the scorer.

    Caches snapshots per-symbol for the calendar day so the 5 plays don't
    each refetch yfinance fundamentals. The cache resets across days
    (asof key includes UTC date).

    Silence-by-default: any failure returns 0.0 (cannot pass either
    onboard or evict floor; symbol stays in current state).
    """
    asof_dt = datetime.now(UTC)
    cache_key = (symbol.upper(), asof_dt.strftime("%Y-%m-%d"))
    snap = _SNAPSHOT_CACHE.get(cache_key)
    if snap is None:
        try:
            snap = compute_play_snapshot(symbol, asof_dt)
        except Exception:
            return 0.0
        _SNAPSHOT_CACHE[cache_key] = snap

    try:
        all_fits = score_all(snap)
        fitness = all_fits.get(play)
        if fitness is None:
            return 0.0
        return float(fitness.score)
    except Exception:
        return 0.0
