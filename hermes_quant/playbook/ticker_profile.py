"""Unified TickerProfile fitness — ONE strategy-agnostic profile-fit model.

This module is the W1 seam of the watchlist re-architecture (target_profile_model):
ONE TickerProfile fitness that answers a single question — *does this ticker fit
WHAT WE TRADE?* (genuine, load-bearing tradeability) — and DELIBERATELY does NOT
answer *is this the right STRATEGY for it?* (strategy selection, which belongs to
the decision layer — structure_select + the deterministic gate, per ADR-0004).

Why one profile, not five
-------------------------
``profiles.py`` defines five per-play PlayProfiles (covered_call / csp / wheel /
leaps / swing). Their rules mix two concerns:

  * **profile-fit** — is the instrument tradeable at all? (equity, liquid enough,
    not a penny/illiquid/halt trap, finite volatility). These are GENUINELY shared
    across every play; they describe the universe, not the play.
  * **strategy-specific** — does this play *want* this ticker right now?
    (days_since_earnings for CC timing, debt_to_equity for csp/leaps credit,
    rsi_14/atr/five_d momentum for swing, the realized_vol BANDS, regime_gates).
    These are decision-layer concerns and MUST NOT pre-pick a strategy at the
    watchlist; the watchlist must stay strategy-agnostic.

This module distills ONLY the profile-fit rails into a single TICKER_PROFILE.
Across the five profiles it takes the **least-restrictive floor** so the scanner
is strategy-agnostic — the per-play *hard* floors (csp's 1e9 market_cap, leaps'
1e10 / 1e7 ADV, the 10/20 price floors, the 0.20-0.60 vol BANDS) are
strategy-tightened and DROP to the decision layer. We reuse the eviction-level
floors (the genuine "this is a trap" rails) verbatim from profiles.py so the
distilled traps can never drift from the per-play evictions.

Grammar reuse (no new rule engine)
----------------------------------
Scoring REUSES the existing rule grammar from ``scorers.py`` verbatim —
``_score_against`` (the 0.6*hard_frac + 0.4*soft_frac formula, eviction handling,
silence-by-default None semantics) over a single PlayProfile-shaped object. We do
NOT re-implement the formula. ``score_ticker_profile`` is a thin adapter that maps
the resulting ``PlayFitness`` onto a ``TickerFitness`` (the same fields, renamed
``score`` -> ``fit_score`` and ``play`` dropped — there is only one profile).

POSTURE (matches the per-play scorer, by reuse):
  * silence-by-default — a None HARD input fails the hard rule; a None SOFT input
    is a miss but does NOT reject; a missing eviction input does NOT fire.
  * fail-closed traps — penny / illiquid / non-equity / vol-runaway / not-tradable
    evict (eligibility forced False).
  * the watchlist NEVER pre-picks a strategy and NEVER pre-denies on regime
    (TICKER_PROFILE carries no regime_gates).

All thresholds here are **eval-gate-pending** constants — the operator tunes them
behind the default-OFF profile-scan flag; the OLD 5-bucket path stays the default.

This module is ADD-ONLY: it imports profiles.py / scorers.py and changes neither.
It performs NO I/O — pure scoring over a snapshot dict, same posture as
point_in_time.py and the advisor (read-only, no risk gate, no state.db).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .profiles import PlayProfile
from .scorers import _eval_rule, _is_none_or_nan, _score_against

# --------------------------------------------------------------------------- #
# Eval-gate-pending thresholds (the strategy-agnostic profile-fit floors)
# --------------------------------------------------------------------------- #
#
# Each constant is sourced from profiles.py's LEAST-restrictive rail for that
# concern so the scanner does not pre-tighten the universe to any one play.
# Tunable behind the default-OFF profile-scan flag (eval-gate-pending).

# Liquidity: the eviction-level ADV floor (cc/csp/swing all evict below 2e6). The
# per-play HARD floors (5e6, 1e7) are strategy-tightened -> decision layer.
_ADV_FLOOR_USD: float = 2e6

# Price band: floor 5.0 = the shared price_too_low eviction; ceiling 500.0 matches
# the universe scanner's own max_price filter (alpaca-daily.json max_price=500).
# The per-play 10/20 floors are strategy-tightened -> decision layer.
_PRICE_FLOOR: float = 5.0
_PRICE_CEIL: float = 500.0

# Small-cap trap: csp's eviction floor (5e8) is the loosest -> profile-fit minimum.
# The per-play 1.5e9 / 1e9 / 1e10 hard floors are strategy-specific -> decision layer.
# Expressed as an EVICTION (market_cap_too_small), NOT a hard rail: market_cap is
# yfinance-only enrichment absent from the universe artifact, so a hard ge-floor would
# reject every symbol on the standalone --no-fetch path. lt_field abstains on None
# (absent-abstains / present-rejects), preserving silence-by-default.
_MARKET_CAP_FLOOR_USD: float = 5e8

# Tight-spread tradeability (NAMED in the operator's profile-fit set; absent from
# profiles.py today). SOFT so missing data never rejects. Starting ceiling 1%.
_SPREAD_PCT_CEIL: float = 0.01

# Finite-and-tradeable volatility — NOT a band. The covered_call/swing/wheel
# realized_vol BANDS (0.20-0.60, 0.30-1.50, 0.25-0.50) are strategy-specific ->
# decision layer. The only profile-fit rail is the swing vol_runaway eviction
# (> 2.0 -> halt/blowup trap). As a SOFT rail we require finite-and-not-runaway:
# > 0.05 (a finite floor; ~5% annualized is the practical "has any signal" floor)
# and <= 1.5 (well inside the 2.0 runaway eviction; soft so missing/edge values
# never reject — the hard reject only ever comes from the vol_runaway eviction).
_VOL_FLOOR: float = 0.05
_VOL_CEIL: float = 1.5

# vol_runaway eviction threshold — reused verbatim from swing's eviction (2.0).
_VOL_RUNAWAY: float = 2.0


# --------------------------------------------------------------------------- #
# The ONE profile-fit profile (PlayProfile-shaped — reuses the existing grammar)
# --------------------------------------------------------------------------- #
#
# A single PlayProfile so _score_against / _eval_rule / _eval_eviction apply
# verbatim. No regime_gates (the watchlist NEVER pre-denies on regime — ADR-0004
# puts regime-vs-direction in the decision gate + structure_select). The name is
# "ticker_profile" purely for the failed-rule rationale strings.
TICKER_PROFILE: PlayProfile = PlayProfile(
    name="ticker_profile",
    # bias is meaningless for a strategy-agnostic profile; the field defaults to
    # the cautious "bullish" but is never read here (no direction routing in the
    # watchlist). The decision layer owns direction-vs-bias.
    bias="agnostic",
    hard_rules={
        "quote_type": ("eq", "EQUITY"),
        "avg_dollar_volume_30d": ("ge", _ADV_FLOOR_USD),
        "last_close": ("between", _PRICE_FLOOR, _PRICE_CEIL),
    },
    soft_rules={
        # SOFT so missing data (None) is a miss, never a reject (silence-by-default).
        "spread_pct": ("le", _SPREAD_PCT_CEIL),
        "realized_vol_30d": ("between", _VOL_FLOOR, _VOL_CEIL),
    },
    eviction_rules={
        # The composite halt/penny/illiquid/non-equity/vol-runaway/not-tradable/
        # small-cap trap. Each tuple is byte-identical to its profiles.py source so
        # the scanner traps can never drift from the per-play evictions. Missing-data
        # inputs do NOT fire (ne_field/lt_field/gt_field abstain on None).
        "non_equity": ("ne_field", "quote_type", "EQUITY"),
        "price_too_low": ("lt_field", "last_close", _PRICE_FLOOR),
        "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", _ADV_FLOOR_USD),
        "vol_runaway": ("gt_field", "realized_vol_30d", _VOL_RUNAWAY),
        # market_cap is an EVICTION, not a HARD rail (W3 integration fix): the
        # universe artifact does NOT carry market_cap (yfinance-only enrichment),
        # so a HARD ge-floor would reject EVERY symbol on the genuinely-standalone
        # --no-fetch path (can't prove a cap it never has). As an eviction it
        # PRESENT-rejects (an enriched sub-5e8 micro-cap is trapped) but ABSENT-
        # abstains (lt_field is None-safe) — silence-by-default. Money safety is
        # NOT weakened: the decision layer (profiles.py) re-HARD-gates market_cap
        # with full enriched data on every play (cc [2e9,1e11], csp >=1e9, leaps
        # >=1e10). Byte-identical to csp's own market_cap_too_small eviction
        # (("lt_field", "market_cap_usd", 5e8)).
        "market_cap_too_small": ("lt_field", "market_cap_usd", _MARKET_CAP_FLOOR_USD),
        # fail-closed not-tradable: tradable must be exactly True (an explicit
        # False evicts). Missing flag abstains (does not evict) — silence-friendly,
        # matching ne_field's None semantics in _eval_eviction.
        "not_tradable": ("ne_field", "tradable", True),
    },
    # NO regime_gates — the watchlist must not pre-deny a ticker on regime.
)


# --------------------------------------------------------------------------- #
# Output dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class TickerProfile:
    """The unified, strategy-agnostic profile of one ticker.

    Built once per symbol from the universe artifact + (optionally) the enriched
    snapshot. ``horizon_set`` is the multi-horizon list attached by the horizon
    model (W3); it defaults to None so this dataclass round-trips before the
    horizon layer is wired (ADD-ONLY, like WatchlistEntry.options_eligible).
    """

    symbol: str
    asof: str | None = None
    asset_class: str | None = None
    options_eligible: bool | None = None
    shortable: bool | None = None
    last_close: float | None = None
    avg_dollar_volume_30d: float | None = None
    market_cap_usd: float | None = None
    realized_vol_30d: float | None = None
    spread_pct: float | None = None
    quote_type: str | None = None
    tradable: bool | None = None
    horizon_set: list[str] | None = None


@dataclass
class TickerFitness:
    """Result of scoring one symbol against the single TICKER_PROFILE.

    Mirrors ``scorers.PlayFitness`` (symbol / pass_hard / eligible / failed_rules)
    but with ``score`` renamed to ``fit_score`` and the per-play ``play`` field
    dropped — there is exactly ONE profile, so a play label is meaningless.
    """

    symbol: str
    fit_score: float  # in [0, 1]
    pass_hard: bool
    pass_soft: bool
    eligible: bool
    failed_rules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Scoring (delegates to the scorers.py engine — no re-implementation)
# --------------------------------------------------------------------------- #


def score_ticker_profile(snapshot: dict) -> TickerFitness:
    """Score a snapshot against the single TICKER_PROFILE.

    REUSES ``scorers._score_against`` verbatim — the 0.6*hard + 0.4*soft formula,
    the eviction determination, the regime no-op, and the silence-by-default None
    semantics (a None HARD input fails the hard rule; a missing eviction input does
    NOT fire). This is a thin adapter from the resulting ``PlayFitness`` to a
    ``TickerFitness`` — same fields, ``score`` -> ``fit_score``, ``play`` dropped.
    ``fit_score`` is the engine score VERBATIM (the documented 0.6/0.4 formula).

    Soft-abstain eligibility (silence-by-default for SOFT rails)
    ------------------------------------------------------------
    ``_score_against`` divides soft passes by the TOTAL soft-rule count, so a soft
    rule whose input is ABSENT (None) is scored exactly like a soft rule that
    FAILED — it drags the 0.4 soft weight down even though "missing data must never
    reject." For the five per-play profiles this rarely bites (3+ soft rules, most
    snapshots carry some soft data). But this unified profile has only TWO soft
    rails, and the genuinely standalone ``--no-fetch`` mode produces snapshots where
    BOTH spread_pct and realized_vol_30d are absent — the raw engine score then
    collapses to 0.6 (= 0.6*1.0 + 0.4*0.0), below the 0.65 floor, rejecting a
    perfectly tradeable liquid equity purely on lack-of-data. That violates the
    contract ("missing spread/vol (None) must NOT reject — soft").

    So eligibility honors soft-abstain: if NO soft rule genuinely FAILED (every
    soft shortfall is an abstain on missing data, not a present-and-out-of-band
    value), eligibility falls back to ``pass_hard AND not-evicted`` — the hard
    rails and the trap evictions remain the only things that can reject. A soft
    rule that is PRESENT-but-out-of-band still pulls the score and is still
    weighed via the engine floor. fit_score itself is left unchanged (so it stays
    the documented formula); only the eligibility decision absorbs the abstain.
    """
    fit = _score_against(TICKER_PROFILE, snapshot)

    # A soft rule "genuinely failed" only when its input is PRESENT and evaluates
    # False/None-on-a-non-None-value — i.e. NOT an abstain-on-missing. Abstained
    # soft rules (input is None/NaN) leave eligibility to the hard + eviction rails.
    soft_genuinely_failed = False
    for fname, rule in TICKER_PROFILE.soft_rules.items():
        v = snapshot.get(fname)
        if _is_none_or_nan(v):
            continue  # abstain — never rejects (silence-by-default)
        if _eval_rule(v, rule) is not True:
            soft_genuinely_failed = True
            break

    evicted = any(
        r.startswith("evict:") or r.startswith("regime_gate:")
        for r in fit.failed_rules
    )

    if soft_genuinely_failed:
        # Some present soft input is out of band — defer to the engine's score floor.
        eligible = fit.eligible
    else:
        # All soft shortfalls (if any) are abstains; the hard rails + trap
        # evictions are the only legitimate rejecters.
        eligible = bool(fit.pass_hard and not evicted)

    return TickerFitness(
        symbol=fit.symbol,
        fit_score=fit.score,
        pass_hard=fit.pass_hard,
        pass_soft=fit.pass_soft,
        eligible=eligible,
        failed_rules=list(fit.failed_rules),
        notes=list(fit.notes),
    )
