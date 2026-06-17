"""hermes_quant.shadow.rules — Shadow rule abstractions and 5 concrete rules.

Wave 8b / ADR-0049.

Each ShadowRule inspects a governance audit event (a dict representation
of a GovernanceEvent) and returns a ShadowDecision when the rule applies,
or None when it does not.

The five concrete rules implement common counterfactual questions:
  1. AlwaysFollowAdvisorRule   — follow the advisor every time
  2. InverseConsensusRule      — do the opposite of the advisor (contrarian)
  3. SemanticOnlyRule          — only fire when the semantic analyst voted
  4. SentimentOnlyRule         — only fire when the sentiment analyst voted
  5. TrendFollowingRule        — require TA + advisor agreement & vote_share > 0.6

Rules only read gate_approval events.  All other event kinds return None.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ShadowDecision:
    """A trading decision produced by a shadow rule.

    Attributes
    ----------
    rule_name:
        Name of the rule that produced this decision.
    asof:
        Timestamp of the originating audit event.
    ticker:
        The asset / ticker symbol.
    action:
        One of ``"buy"``, ``"sell"``, or ``"hold"``.
    size_fraction:
        Fraction of shadow-account equity to commit (0.0–1.0).
    reason:
        Human-readable rationale string (max 512 chars).
    """

    rule_name: str
    asof: datetime
    ticker: str
    action: Literal["buy", "sell", "hold"]
    size_fraction: float
    reason: str


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ShadowRule(ABC):
    """Abstract base for all shadow rules.

    Subclasses must implement :meth:`evaluate`.

    Attributes
    ----------
    name:
        Short identifier used as the SQLite DB file-name stem and in reports.
    description:
        One-sentence description of the counterfactual hypothesis.
    """

    name: str
    description: str

    # ------------------------------------------------------------------
    # Helpers shared by all rules
    # ------------------------------------------------------------------

    @staticmethod
    def _direction_from_payload(payload: dict) -> Optional[Literal["buy", "sell"]]:
        """Extract advisor direction from a gate_approval payload.

        Checks, in order:
        1. ``payload["advisor_result"]["direction"]``  (structured output path)
        2. ``payload["signal_provenance"]["advisor_direction"]``  (legacy path)
        3. ``payload["direction"]``  (flat) — the CANONICAL field the gate emits.

        ar122: the canonical gate emitter (risk/gate.py + pdr_core/gate.py
        _audit_approval) writes ``payload["direction"] = int(signal.direction)`` — a
        SIGNED INT (1=long/buy, -1=short/sell, 0=flat), NOT a "buy"/"sell" string. The
        prior string-only parse returned None for EVERY real gate_approval event, so the
        ADR-0049 shadow-counterfactual eval ledger (scripts/shadow-replay-daily.py →
        runner.replay_session → account.apply_signal) recorded ZERO decisions in
        production — a vacuous eval surface masked by string-only test fixtures. We now
        accept the int the producer actually emits AND keep the legacy string paths
        (and the bool guard so True/False is not mis-read as 1/0). Returns None for a
        flat (0) or undeterminable direction.
        """
        for path in (
            ("advisor_result", "direction"),
            ("signal_provenance", "advisor_direction"),
        ):
            obj = payload
            for key in path:
                obj = obj.get(key) if isinstance(obj, dict) else None
            mapped = _coerce_direction(obj)
            if mapped is not None:
                return mapped
        return _coerce_direction(payload.get("direction"))

    @staticmethod
    def _ticker_from_payload(payload: dict) -> str:
        """Extract ticker from payload (multiple candidate keys)."""
        for key in ("ticker", "asset", "symbol", "instrument"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val.upper()
        return "UNKNOWN"

    @staticmethod
    def _vote_share(payload: dict) -> float:
        """Return the vote_share from signal_provenance, or 0.0."""
        sp = payload.get("signal_provenance", {})
        if not isinstance(sp, dict):
            return 0.0
        raw = sp.get("vote_share", sp.get("advisor_vote_share", 0.0))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _analyst_voted(payload: dict, *substrings: str) -> bool:
        """True if a contributing analyst's name CONTAINS any of ``substrings``.

        ar126: the gate emits ``signal_provenance.contributing_analysts`` with the REAL
        analyst names — ``"hermes_semantic"`` (analysts/semantic.py:57) and
        ``"classical-ta"`` (hyphen; analysts/classical_ta.py:136) — NOT bare tokens like
        "semantic"/"classical_ta". The pre-ar126 rules matched a hardcoded alias TUPLE by
        EXACT equality (``a in ("semantic","semantic_analyst",...)``), so SemanticOnlyRule
        and the contributor half of TrendFollowingRule NEVER matched a real event →
        residually vacuous even after ar122 fixed the int-direction parse. Substring +
        separator-insensitive (hyphen/underscore stripped) matching tolerates the real
        names AND any future rename/prefix. (SentimentOnlyRule stays correctly inert —
        there is NO sentiment analyst in the codebase — until one is added.)
        """
        norm_subs = [s.replace("_", "").replace("-", "").lower() for s in substrings]
        for a in ShadowRule._contributing_analysts(payload):
            an = a.replace("_", "").replace("-", "").lower()
            if any(s in an for s in norm_subs):
                return True
        return False

    @staticmethod
    def _contributing_analysts(payload: dict) -> list[str]:
        """Return the list of contributing_analysts from signal_provenance."""
        sp = payload.get("signal_provenance", {})
        if not isinstance(sp, dict):
            return []
        analysts = sp.get("contributing_analysts", [])
        if isinstance(analysts, list):
            return [str(a).lower() for a in analysts]
        return []

    @staticmethod
    def _classical_ta_direction(payload: dict) -> Optional[str]:
        """Extract the classical-TA analyst's OWN direction from the payload.

        ar126: the canonical source is ``signal_provenance.per_analyst_directions``
        (the gate now emits {analyst_name: "buy"/"sell"/"flat"}), keyed by the REAL
        analyst name ``"classical-ta"`` (hyphen). Match it separator-insensitively. The
        legacy ``classical_ta_direction`` / ``analyst_votes`` paths are kept for any
        producer that emits them, but the gate emits neither — before ar126 this method
        always returned None, so TrendFollowingRule was vacuous.
        """
        sp = payload.get("signal_provenance", {})
        if isinstance(sp, dict):
            per_analyst = sp.get("per_analyst_directions")
            if isinstance(per_analyst, dict):
                for name, d in per_analyst.items():
                    norm = str(name).replace("_", "").replace("-", "").lower()
                    if "classicalta" in norm and isinstance(d, str) and d.lower() in ("buy", "sell"):
                        return d.lower()
            ta_dir = sp.get("classical_ta_direction")
            if isinstance(ta_dir, str) and ta_dir.lower() in ("buy", "sell"):
                return ta_dir.lower()
        # Also check analyst_votes sub-dict (legacy / alternative producer).
        analyst_votes = payload.get("analyst_votes", {})
        if isinstance(analyst_votes, dict):
            ta_vote = analyst_votes.get("classical_ta", analyst_votes.get("ta"))
            if isinstance(ta_vote, dict):
                d = ta_vote.get("direction")
                if isinstance(d, str) and d.lower() in ("buy", "sell"):
                    return d.lower()
        return None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        """Evaluate a governance audit event and return a ShadowDecision or None.

        Parameters
        ----------
        audit_event:
            A dict representation of a GovernanceEvent (from audit_log.read()
            or a synthetic dict in tests). The rule must return None for any
            event kind other than ``gate_approval``.

        Returns
        -------
        ShadowDecision or None if the rule doesn't apply to this event.
        """


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _parse_asof(audit_event: dict) -> datetime:
    """Parse asof from audit event dict; returns now() on failure."""
    asof_raw = audit_event.get("asof")
    if isinstance(asof_raw, datetime):
        return asof_raw
    if isinstance(asof_raw, str):
        try:
            return datetime.fromisoformat(asof_raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    from datetime import timezone
    return datetime.now(timezone.utc)


def _coerce_direction(obj: object) -> Optional[Literal["buy", "sell"]]:
    """Normalize a direction value to "buy"/"sell"/None.

    ar122: accepts BOTH the canonical signed-int form the gate emits
    (``int(signal.direction)``: 1=buy, -1=sell, 0=flat→None) AND the legacy
    "buy"/"sell" string. A bool is rejected before the int branch (``True``/``False``
    must not be silently read as 1/0). NaN/other → None.
    """
    if isinstance(obj, str):
        low = obj.lower()
        return low if low in ("buy", "sell") else None  # type: ignore[return-value]
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        if not math.isfinite(obj):
            return None
        if obj > 0:
            return "buy"
        if obj < 0:
            return "sell"
        return None  # flat (0)
    return None


def _opposite(direction: Literal["buy", "sell"]) -> Literal["buy", "sell"]:
    return "sell" if direction == "buy" else "buy"


# ---------------------------------------------------------------------------
# Concrete rules
# ---------------------------------------------------------------------------


class AlwaysFollowAdvisorRule(ShadowRule):
    """Rule 1: Always follow the advisor direction, 10% position size.

    Hypothesis: If we had mechanically followed every gate_approval at 10%
    of equity, would that have been better than the production (variable-size)
    allocation?
    """

    name = "always_follow_advisor"
    description = (
        "Always take the advisor's direction at a fixed 10% equity size. "
        "Tests whether mechanical rule-following beats dynamic sizing."
    )

    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        if audit_event.get("kind") != "gate_approval":
            return None
        payload = audit_event.get("payload", {})
        direction = self._direction_from_payload(payload)
        if direction is None:
            return None
        ticker = self._ticker_from_payload(payload)
        return ShadowDecision(
            rule_name=self.name,
            asof=_parse_asof(audit_event),
            ticker=ticker,
            action=direction,
            size_fraction=0.10,
            reason=f"AlwaysFollowAdvisor: advisor said {direction} → fixed 10% size",
        )


class InverseConsensusRule(ShadowRule):
    """Rule 2: Invert the advisor direction — the contrarian counterfactual.

    Hypothesis: Is the committee systematically wrong? Buying when they sell
    and vice-versa.
    """

    name = "inverse_consensus"
    description = (
        "Take the opposite of the advisor direction at 10% equity. "
        "The contrarian counterfactual: tests whether the committee is systematically wrong."
    )

    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        if audit_event.get("kind") != "gate_approval":
            return None
        payload = audit_event.get("payload", {})
        direction = self._direction_from_payload(payload)
        if direction is None:
            return None
        ticker = self._ticker_from_payload(payload)
        contrarian = _opposite(direction)
        return ShadowDecision(
            rule_name=self.name,
            asof=_parse_asof(audit_event),
            ticker=ticker,
            action=contrarian,
            size_fraction=0.10,
            reason=(
                f"InverseConsensus: advisor said {direction} → contrarian fires {contrarian} "
                "at 10% size"
            ),
        )


class SemanticOnlyRule(ShadowRule):
    """Rule 3: Only fire when the semantic analyst voted; follow advisor direction.

    Hypothesis: The semantic (news + fundamentals) analyst adds alpha. Ignoring
    all events where semantic analyst did NOT vote should improve quality.
    """

    name = "semantic_only"
    description = (
        "Only fire when the semantic analyst contributed a vote; take advisor "
        "direction at 10% equity. Tests semantic-analyst alpha isolation."
    )

    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        if audit_event.get("kind") != "gate_approval":
            return None
        payload = audit_event.get("payload", {})
        # ar126: match the REAL analyst name "hermes_semantic" (substring, separator-
        # insensitive) — the prior exact-tuple match never fired in prod.
        if not self._analyst_voted(payload, "semantic"):
            return None
        direction = self._direction_from_payload(payload)
        if direction is None:
            return None
        ticker = self._ticker_from_payload(payload)
        return ShadowDecision(
            rule_name=self.name,
            asof=_parse_asof(audit_event),
            ticker=ticker,
            action=direction,
            size_fraction=0.10,
            reason=(
                f"SemanticOnly: semantic analyst voted; advisor direction={direction}; "
                "10% size"
            ),
        )


class SentimentOnlyRule(ShadowRule):
    """Rule 4: Only fire when the sentiment analyst voted; follow their direction.

    Hypothesis: Pure sentiment signal (social media, news sentiment) might
    carry independent predictive power.

    Direction is taken from the sentiment analyst's own vote direction when
    available; falls back to the overall advisor direction.
    """

    name = "sentiment_only"
    description = (
        "Only fire when the sentiment analyst contributed a vote; take sentiment "
        "direction (or advisor fallback) at 10% equity."
    )

    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        if audit_event.get("kind") != "gate_approval":
            return None
        payload = audit_event.get("payload", {})
        # ar126: substring/separator-insensitive match. NOTE: there is currently NO
        # sentiment analyst in the codebase, so this rule is intentionally INERT (records
        # nothing) until one is added — that is a hypothesis-pending rule, not a bug.
        if not self._analyst_voted(payload, "sentiment"):
            return None

        # Try to get the sentiment analyst's specific direction
        direction: Optional[Literal["buy", "sell"]] = None
        analyst_votes = payload.get("analyst_votes", {})
        if isinstance(analyst_votes, dict):
            sent_vote = analyst_votes.get("sentiment", analyst_votes.get("sentiment_analyst"))
            if isinstance(sent_vote, dict):
                d = sent_vote.get("direction")
                if isinstance(d, str) and d.lower() in ("buy", "sell"):
                    direction = d.lower()  # type: ignore[assignment]

        if direction is None:
            direction = self._direction_from_payload(payload)
        if direction is None:
            return None

        ticker = self._ticker_from_payload(payload)
        return ShadowDecision(
            rule_name=self.name,
            asof=_parse_asof(audit_event),
            ticker=ticker,
            action=direction,
            size_fraction=0.10,
            reason=(
                f"SentimentOnly: sentiment analyst voted; direction={direction}; 10% size"
            ),
        )


class TrendFollowingRule(ShadowRule):
    """Rule 5: Fire only when TA + advisor agree AND vote_share > 0.6; 15% size.

    Hypothesis: High-conviction confluent signals (technical + committee
    majority) should produce better outcomes than low-conviction approvals.
    """

    name = "trend_following"
    description = (
        "Fire when classical-TA direction matches advisor direction AND "
        "vote_share > 0.6; take advisor direction at 15% equity. "
        "Tests high-conviction confluence alpha."
    )

    VOTE_SHARE_THRESHOLD: float = 0.6

    def evaluate(self, audit_event: dict) -> Optional[ShadowDecision]:
        if audit_event.get("kind") != "gate_approval":
            return None
        payload = audit_event.get("payload", {})
        direction = self._direction_from_payload(payload)
        if direction is None:
            return None
        ta_direction = self._classical_ta_direction(payload)
        if ta_direction is None or ta_direction != direction:
            return None
        vote_share = self._vote_share(payload)
        if vote_share <= self.VOTE_SHARE_THRESHOLD:
            return None
        ticker = self._ticker_from_payload(payload)
        return ShadowDecision(
            rule_name=self.name,
            asof=_parse_asof(audit_event),
            ticker=ticker,
            action=direction,
            size_fraction=0.15,
            reason=(
                f"TrendFollowing: TA={ta_direction} matches advisor={direction}, "
                f"vote_share={vote_share:.2f} > {self.VOTE_SHARE_THRESHOLD}; 15% size"
            ),
        )


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------


def default_rules() -> list[ShadowRule]:
    """Return the canonical set of 5 shadow rules."""
    return [
        AlwaysFollowAdvisorRule(),
        InverseConsensusRule(),
        SemanticOnlyRule(),
        SentimentOnlyRule(),
        TrendFollowingRule(),
    ]
