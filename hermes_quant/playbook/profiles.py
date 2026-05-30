"""Concrete PlayProfile definitions for the hermes-quant 5-play playbook.

Each profile encodes:
    hard_rules     — gates a symbol MUST clear to be eligible (silence-by-default).
    soft_rules     — nice-to-haves; scored but never reject.
    eviction_rules — if ANY is True, drop the symbol from the watchlist entirely.

Rules are stored as plain dicts of structured tuples so they're trivially
introspectable and serializable. Each rule value is one of:

    ("between", lo, hi)   — pass if lo <= x <= hi
    ("ge", v)             — pass if x >= v
    ("gt", v)             — pass if x > v
    ("le", v)             — pass if x <= v
    ("lt", v)             — pass if x < v
    ("nonzero_window", lo, hi)  — pass if lo < x < hi AND x != 0
    ("or", rule_a, rule_b)      — pass if either child rule passes
    ("any_of", *rules)          — pass if any child passes

Eviction rules use the same grammar; a symbol is evicted when ANY eviction
rule evaluates True.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlayProfile:
    """A static fitness profile for one play in the playbook.

    Attributes:
        name: One of {'covered_call','csp','wheel','leaps','swing'}.
        bias: Directional bias of the play's STRUCTURE — one of
            {'bullish','bearish','agnostic'}. A SHORT (direction<0) advisor
            signal may ONLY route through an 'agnostic' or 'bearish' play; a
            LONG (direction>0) signal may ONLY route through a 'bullish' or
            'agnostic' play. covered_call / csp / wheel / leaps are all
            bullish-bias structures (you profit when the underlying holds or
            rises); swing is direction-agnostic (the entry can be long or
            short). Defaults to 'bullish' — the cautious default: an unknown
            or mis-specified play is treated as bullish-only so a SHORT signal
            can never silently route through it (silence-by-default). Read by
            ``direction_play_compatible()`` BEFORE any signal is propagated.
        hard_rules: dict[field_name, rule_tuple] — must all pass.
        soft_rules: dict[field_name, rule_tuple] — used for scoring.
        eviction_rules: dict[field_name, rule_tuple] — any True ⇒ drop symbol.
        regime_gates: dict[regime_label, action] where regime_label is the
            string value from RegimeState (`bull`, `bear`, `volatile`, `unknown`)
            and action is one of {`allow`, `warn`, `deny`}. When the current
            regime maps to `deny`, score_symbol() returns 0.0 with a
            regime-related rationale; `warn` allows scoring but lowers
            confidence by 30%; `allow` is a no-op. Missing labels default to
            `allow` (backward-compat with pre-2026-05-28 profiles that have
            no regime_gates field). Read by `score_symbol()` in scorers.py
            after eviction-rule check, before final score computation.
    """

    name: str
    hard_rules: dict
    soft_rules: dict
    eviction_rules: dict
    regime_gates: dict = field(default_factory=dict)
    # Cautious default: an unspecified play is bullish-only, so a SHORT signal
    # can never silently route through it (silence-by-default). Each concrete
    # profile below sets this explicitly.
    bias: str = "bullish"


# --- covered_call -----------------------------------------------------------

profile_covered_call = PlayProfile(
    name="covered_call",
    # Bullish-bias: you own the underlying and sell upside calls — you profit
    # when the underlying holds or rises. A SHORT signal must never route here.
    bias="bullish",
    hard_rules={
        "quote_type": ("eq", "EQUITY"),
        "market_cap_usd": ("between", 2e9, 1e11),
        "avg_dollar_volume_30d": ("ge", 5e6),
        "last_close": ("between", 10.0, 500.0),
        "days_since_earnings": ("ge", 5),
    },
    soft_rules={
        "realized_vol_30d": ("between", 0.20, 0.60),
        "rsi_14": ("between", 40.0, 70.0),
        "distance_from_52w_high_pct": ("ge", -0.15),
    },
    eviction_rules={
        "non_equity": ("ne_field", "quote_type", "EQUITY"),
        "market_cap_too_small": ("lt_field", "market_cap_usd", 1.5e9),
        "market_cap_too_large": ("gt_field", "market_cap_usd", 1.5e11),
        "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", 2e6),
        "price_too_low": ("lt_field", "last_close", 5.0),
    },
    # In BEAR regime the underlying loses value faster than premium collected;
    # in VOLATILE the premium is rich but assignment risk spikes. Bull/unknown OK.
    regime_gates={"bull": "allow", "bear": "deny", "volatile": "warn", "unknown": "allow"},
)


# --- csp --------------------------------------------------------------------

profile_csp = PlayProfile(
    name="csp",
    # Bullish-bias: a cash-secured put is willingness to BUY the underlying at
    # the strike — you profit when it holds or rises. A SHORT signal must never
    # route here (this is the AXP B04 bug: SHORT fired through 'csp').
    bias="bullish",
    hard_rules={
        "quote_type": ("eq", "EQUITY"),
        "market_cap_usd": ("ge", 1e9),
        "avg_dollar_volume_30d": ("ge", 5e6),
        "debt_to_equity": ("lt", 2.0),
        "last_close": ("between", 10.0, 500.0),
    },
    soft_rules={
        "dividend_yield": ("gt", 0.0),
        "free_cash_flow_yield": ("gt", 0.03),
        "beta": ("between", 0.5, 1.5),
    },
    eviction_rules={
        "non_equity": ("ne_field", "quote_type", "EQUITY"),
        "market_cap_too_small": ("lt_field", "market_cap_usd", 5e8),
        "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", 2e6),
        "price_too_low": ("lt_field", "last_close", 5.0),
    },
    # In BEAR you'll get assigned at strikes that mark down further; VOLATILE
    # is rich-premium but high pin-risk; BULL is the natural regime.
    regime_gates={"bull": "allow", "bear": "deny", "volatile": "warn", "unknown": "allow"},
)


# --- wheel ------------------------------------------------------------------
# Wheel = covered_call AND csp. Hard rules merged from both.

_wheel_hard: dict = {}
_wheel_hard.update(profile_covered_call.hard_rules)
# csp's debt_to_equity is unique; market_cap & adv & last_close get tightened
# to whichever is the *more* restrictive between CC and CSP.
_wheel_hard["debt_to_equity"] = ("lt", 2.0)
# market_cap: CC requires [2e9, 1e11], CSP requires >=1e9. Intersect → [2e9, 1e11].
# Already from CC. Same for ADV (both 5e6) and last_close (both [10,500]).
# days_since_earnings only from CC.

_wheel_eviction: dict = {}
for k, v in profile_covered_call.eviction_rules.items():
    _wheel_eviction[f"cc_{k}"] = v
for k, v in profile_csp.eviction_rules.items():
    _wheel_eviction[f"csp_{k}"] = v

profile_wheel = PlayProfile(
    name="wheel",
    # Bullish-bias: wheel = CSP then covered_call, both bullish legs.
    bias="bullish",
    hard_rules=_wheel_hard,
    soft_rules={
        # Sweet spot for both legs: enough vol to pay premium, not so high it whipsaws.
        "realized_vol_30d": ("between", 0.25, 0.50),
    },
    eviction_rules=_wheel_eviction,
    # Wheel inherits the more restrictive gate of CC + CSP. Both are deny-on-BEAR
    # so the wheel inherits deny-on-BEAR. Both are warn-on-VOLATILE → warn.
    regime_gates={"bull": "allow", "bear": "deny", "volatile": "warn", "unknown": "allow"},
)


# --- leaps ------------------------------------------------------------------

profile_leaps = PlayProfile(
    name="leaps",
    # Bullish-bias: a LEAPS long-call thesis profits when the underlying rises
    # over a multi-year horizon. A SHORT signal must never route here.
    bias="bullish",
    hard_rules={
        "quote_type": ("eq", "EQUITY"),
        "market_cap_usd": ("ge", 1e10),
        "avg_dollar_volume_30d": ("ge", 1e7),
        "last_close": ("between", 20.0, 500.0),
        "debt_to_equity": ("lt", 1.5),
    },
    soft_rules={
        "revenue_growth_yoy": ("gt", 0.10),
        "return_on_equity": ("gt", 0.15),
        "gross_margin": ("gt", 0.40),
        "distance_from_52w_high_pct": ("ge", -0.20),
    },
    eviction_rules={
        "non_equity": ("ne_field", "quote_type", "EQUITY"),
        "market_cap_too_small": ("lt_field", "market_cap_usd", 5e9),
        "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", 5e6),
        "leverage_too_high": ("gt_field", "debt_to_equity", 2.0),
    },
    # LEAPS = long-call thesis on multi-year horizon. BEAR = lose theta + delta;
    # VOLATILE = high IV is bad for long calls but thesis is multi-year so warn-not-deny.
    regime_gates={"bull": "allow", "bear": "deny", "volatile": "warn", "unknown": "allow"},
)


# --- swing ------------------------------------------------------------------

profile_swing = PlayProfile(
    name="swing",
    # Direction-agnostic: a swing entry can be long OR short. Both LONG and
    # SHORT advisor signals may route here; the deterministic risk gate handles
    # direction-vs-regime alignment per ADR-0004.
    bias="agnostic",
    hard_rules={
        "quote_type": ("eq", "EQUITY"),
        "avg_dollar_volume_30d": ("ge", 1e7),
        "last_close": ("between", 10.0, 500.0),
        "realized_vol_30d": ("between", 0.30, 1.50),
    },
    soft_rules={
        # RSI extreme (oversold or overbought) → momentum opportunity
        "rsi_14": ("or", ("lt", 30.0), ("gt", 70.0)),
        # Recent move within ±15% but nonzero
        "five_d_return_pct": ("nonzero_window", -0.15, 0.15),
        # ATR ratio — needs daily range to be tradeable
        "atr_pct_of_spot": ("gt", 0.02),
    },
    eviction_rules={
        "non_equity": ("ne_field", "quote_type", "EQUITY"),
        "adv_too_thin": ("lt_field", "avg_dollar_volume_30d", 2e6),
        "vol_runaway": ("gt_field", "realized_vol_30d", 2.0),
    },
    # Swing exists for both directions — the gate is on direction (handled
    # downstream), NOT whether to swing. All regimes ALLOW. The deterministic
    # risk gate handles direction-vs-regime alignment per ADR-0004.
    regime_gates={"bull": "allow", "bear": "allow", "volatile": "allow", "unknown": "allow"},
)


PROFILES: dict[str, PlayProfile] = {
    "covered_call": profile_covered_call,
    "csp": profile_csp,
    "wheel": profile_wheel,
    "leaps": profile_leaps,
    "swing": profile_swing,
}
