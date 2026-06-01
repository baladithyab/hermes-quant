"""hermes_quant.research.risk_tier — defensive research-only RiskTier guard (B30).

The research plane proposes falsifiable hypotheses; it must NEVER claim live-trading
execution authority. This module is the keyword guard that runs at the
``HypothesisRegistry.register()`` seam: it classifies a hypothesis' free-text
(``claim`` / ``null_hypothesis`` / ``experiment_design``) into a :class:`RiskTier`.

Design (sibling of ``research/hypothesis_novelty.py``)
-----------------------------------------------------
Same gate SHAPE — a pure function returning a frozen :class:`RiskTierResult`, an
env-tunable behaviour knob, deterministic output, no IO. Here the signal is a
KEYWORD SET (phrases that imply live execution authority) rather than a textual
similarity metric.

FAIL-CLOSED safety semantics
----------------------------
The only two tiers are :attr:`RiskTier.RESEARCH_ONLY` and :attr:`RiskTier.FLAGGED`.
There is NO ``live`` tier the research plane can land in — by construction it can
only ever be *research_only* or *flagged-and-downgraded-to-research_only*. The guard
NEVER grants execution authority; it can only withhold it. Ambiguity therefore
resolves to ``research_only`` (the safe, authority-less default), and a flagged
hypothesis is still classified ``research_only`` (downgraded) — the FLAGGED tier is a
signal for the caller / audit trail, not a grant.

This module does NOT touch the deterministic risk gate, sizing ladder, or kill-switch.
It is an additive annotation on the research registry's append-only rows.

Configuration
~~~~~~~~~~~~~
    HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK (default "0"):
        When set to "1", :meth:`HypothesisRegistry.register` REFUSES (raises) a
        flagged hypothesis instead of silently downgrading + annotating it. Default-OFF
        so the existing register() flow is behaviour-preserving; opt-in hard-block for
        stricter deployments. Read at call time (mirrors research_loop._loop_enabled()).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


class RiskTier(StrEnum):
    """The authority a research artifact may carry.

    Deliberately only two members: the research plane can NEVER be assigned a
    'live' tier by this guard. See module docstring §FAIL-CLOSED.
    """

    RESEARCH_ONLY = "research_only"
    """Safe default. No live-trading / execution authority. Authority-less."""

    FLAGGED = "flagged"
    """Text implies live execution authority it must not have. The caller
    DOWNGRADES this to research_only; FLAGGED is an audit/inspection signal,
    not a grant of authority."""


# ---------------------------------------------------------------------------
# Keyword set — phrases that imply LIVE EXECUTION AUTHORITY.
# ---------------------------------------------------------------------------
# Kept small, deterministic, lowercase. Multi-word phrases are matched as
# whitespace-flexible regexes so "live-trade", "live  trade", and "live trade"
# all match. A single hit is enough to FLAG (fail-closed: we would rather flag a
# benign mention of "live trading" for human review than let one slip through).
#
# NOTE: research hypotheses legitimately discuss paper/backtest execution and
# *measuring* edge — those words alone (e.g. "backtest", "paper", "simulate")
# are intentionally NOT in this set, so normal research text stays research_only.

_LIVE_AUTHORITY_PHRASES: frozenset[str] = frozenset(
    {
        "live trade",
        "live trading",
        "live execution",
        "live order",
        "live orders",
        "execute live",
        "trade live",
        "real money",
        "real capital",
        "real funds",
        "real account",
        "production trading",
        "production account",
        "place an order",
        "place orders",
        "place a trade",
        "submit an order",
        "submit orders",
        "send order",
        "send orders",
        "route order",
        "route orders",
        "bypass the risk gate",
        "bypass risk gate",
        "skip the risk gate",
        "disable the kill switch",
        "disable kill switch",
        "override the kill switch",
        "override risk limits",
        "deploy to production",
        "go live",
        "trade with real",
        "execution authority",
        "trading authority",
        "live capital",
        "live account",
        "live broker",
        "deploy live",
    }
)


def _compile(phrases: frozenset[str]) -> dict[str, re.Pattern[str]]:
    r"""Compile each phrase into a whitespace-flexible, word-bounded regex.

    ``"live trade"`` -> ``r"\blive\s+trade\b"`` so any run of whitespace between
    the words matches and substrings of larger words ("alive", "traded") do not.
    """
    out: dict[str, re.Pattern[str]] = {}
    for phrase in phrases:
        parts = [re.escape(p) for p in phrase.split()]
        pattern = r"\b" + r"\s+".join(parts) + r"\b"
        out[phrase] = re.compile(pattern)
    return out


_COMPILED_PHRASES: dict[str, re.Pattern[str]] = _compile(_LIVE_AUTHORITY_PHRASES)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskTierResult:
    """Frozen result of :func:`classify_risk_tier`. Mirrors NoveltyResult's shape.

    Attributes:
        tier:           RESEARCH_ONLY (safe) or FLAGGED (downgrade signal).
        matched:        Sorted tuple of the live-authority phrases that matched
                        (empty when research_only).
        reason:         Human-readable explanation.
    """

    tier: RiskTier
    matched: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def is_research_only(self) -> bool:
        return self.tier is RiskTier.RESEARCH_ONLY

    @property
    def is_flagged(self) -> bool:
        return self.tier is RiskTier.FLAGGED


# ---------------------------------------------------------------------------
# Classifier — pure, deterministic, fail-closed.
# ---------------------------------------------------------------------------


def classify_risk_tier(*texts: str | None) -> RiskTierResult:
    """Classify free-text into a :class:`RiskTier`. FAIL-CLOSED.

    Any one of ``texts`` (claim, null_hypothesis, experiment_design, ...) matching
    a live-authority phrase -> FLAGGED. Otherwise -> RESEARCH_ONLY. Empty/None input
    and any ambiguity resolve to RESEARCH_ONLY — the authority-less default.

    This NEVER returns a tier that grants execution authority; it can only withhold.

    Args:
        *texts:  Free-text fields to scan. ``None`` entries are skipped.

    Returns:
        RiskTierResult with the matched phrases (deterministically sorted).
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return RiskTierResult(
            tier=RiskTier.RESEARCH_ONLY,
            matched=(),
            reason="empty_text -> research_only (fail-closed default)",
        )

    matched = sorted(
        phrase for phrase, rx in _COMPILED_PHRASES.items() if rx.search(blob)
    )
    if matched:
        return RiskTierResult(
            tier=RiskTier.FLAGGED,
            matched=tuple(matched),
            reason=(
                "flagged: text implies live-execution authority "
                f"(matched: {matched}); downgraded to research_only"
            ),
        )

    return RiskTierResult(
        tier=RiskTier.RESEARCH_ONLY,
        matched=(),
        reason="no live-authority keywords -> research_only",
    )


def block_on_flag_enabled() -> bool:
    """Resolve the opt-in hard-block flag at CALL TIME (default-OFF).

    Mirrors research_loop._loop_enabled() / hypothesis_novelty._default_threshold():
    read the env each call so tests/operators can toggle it without re-import.
    """
    return os.environ.get("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", "0") == "1"
