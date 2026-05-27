"""hermes_quant.shadow.runner — ShadowAccountRunner: orchestrates all shadow rules.

Wave 8b / ADR-0049.

The runner processes the production audit log in lockstep across all N shadow
rules and produces a ShadowComparisonReport showing which rules beat real P&L.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_quant.shadow.account import ShadowAccount
from hermes_quant.shadow.rules import ShadowRule, default_rules

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ShadowComparisonReport
# ---------------------------------------------------------------------------


@dataclass
class ShadowComparisonReport:
    """Comparison of real P&L vs every shadow rule's P&L for a given session.

    Attributes
    ----------
    real_pnl:
        The actual realized P&L of the production portfolio for this session.
    shadow_pnls:
        Dict mapping rule_name → P&L for that shadow account in this session.
    counterfactual_winners:
        Rules whose shadow P&L exceeded real_pnl.
    counterfactual_losers:
        Rules whose shadow P&L fell below real_pnl.
    biggest_alpha:
        (rule_name, alpha_value) for the rule with the highest P&L advantage
        over real.  alpha = shadow_pnl - real_pnl.
    evidence_summary:
        Human-readable text summary of findings (max 2048 chars).
    asof:
        The session end date.
    """

    real_pnl: float
    shadow_pnls: dict[str, float]
    counterfactual_winners: list[str]
    counterfactual_losers: list[str]
    biggest_alpha: tuple[str, float]
    evidence_summary: str
    asof: date = field(default_factory=date.today)

    def to_dict(self) -> dict:
        return {
            "asof": self.asof.isoformat(),
            "real_pnl": self.real_pnl,
            "shadow_pnls": self.shadow_pnls,
            "counterfactual_winners": self.counterfactual_winners,
            "counterfactual_losers": self.counterfactual_losers,
            "biggest_alpha": list(self.biggest_alpha),
            "evidence_summary": self.evidence_summary,
        }


# ---------------------------------------------------------------------------
# ShadowAccountRunner
# ---------------------------------------------------------------------------


class ShadowAccountRunner:
    """Orchestrates all shadow rules in lockstep with the production audit log.

    Parameters
    ----------
    rules:
        List of ShadowRule instances to maintain.  Defaults to all 5 canonical
        rules from :func:`~hermes_quant.shadow.rules.default_rules`.
    db_dir:
        Directory for shadow SQLite databases.  Each rule gets its own file at
        ``<db_dir>/<rule.name>.db``.  Defaults to ``~/.hermes/quant/shadow/``.
    initial_cash:
        Starting cash for each shadow account (default 100 000 USD).
    cost_model_bps:
        One-way cost applied to every shadow fill (default 10 bps).
    """

    def __init__(
        self,
        rules: Optional[list[ShadowRule]] = None,
        *,
        db_dir: Optional[Path] = None,
        initial_cash: float = 100_000.0,
        cost_model_bps: float = 10.0,
    ) -> None:
        self.rules: list[ShadowRule] = rules if rules is not None else default_rules()
        self.db_dir = db_dir
        self.initial_cash = initial_cash
        self.cost_model_bps = cost_model_bps

        # Build one ShadowAccount per rule
        self.accounts: dict[str, ShadowAccount] = {}
        for rule in self.rules:
            db_path = (db_dir / f"{rule.name}.db") if db_dir else None
            self.accounts[rule.name] = ShadowAccount(
                rule,
                initial_cash=initial_cash,
                cost_model_bps=cost_model_bps,
                db_path=db_path,
            )

    # ------------------------------------------------------------------
    # replay_session
    # ------------------------------------------------------------------

    def replay_session(
        self,
        audit_events: list[dict],
        prices_by_ticker: dict[str, dict[date, float]],
    ) -> dict[str, ShadowAccount]:
        """Replay a sequence of audit events across all shadow accounts.

        Parameters
        ----------
        audit_events:
            Ordered list of governance audit event dicts.  Shadow accounts
            only act on ``gate_approval`` events; other kinds are silently
            passed through (rules return None).
        prices_by_ticker:
            Nested price lookup: ``prices_by_ticker[ticker][date]`` → float.
            Used to resolve the price at the time of each event.

        Returns
        -------
        Dict mapping rule_name → ShadowAccount (updated in-place).
        """
        for event in audit_events:
            # Resolve prices for this event's date
            asof_raw = event.get("asof")
            event_date: date
            if isinstance(asof_raw, datetime):
                event_date = asof_raw.date()
            elif isinstance(asof_raw, str):
                try:
                    event_date = datetime.fromisoformat(
                        asof_raw.replace("Z", "+00:00")
                    ).date()
                except ValueError:
                    event_date = date.today()
            else:
                event_date = date.today()

            # Build a flat prices dict for this date
            prices: dict[str, float] = {}
            for ticker, date_prices in prices_by_ticker.items():
                price = date_prices.get(event_date)
                if price is not None:
                    prices[ticker] = float(price)

            # Apply event to each shadow account
            for account in self.accounts.values():
                try:
                    account.apply_signal(event, prices)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ShadowAccountRunner: account %s failed on event %s: %s",
                        account.rule.name,
                        event.get("event_id", "?"),
                        exc,
                    )

        return self.accounts

    # ------------------------------------------------------------------
    # compare_to_real
    # ------------------------------------------------------------------

    def compare_to_real(
        self,
        real_pnl: float,
        *,
        asof: Optional[date] = None,
    ) -> ShadowComparisonReport:
        """Produce a ShadowComparisonReport for the given session date.

        Parameters
        ----------
        real_pnl:
            The actual realized P&L of the production portfolio for this session.
        asof:
            Session end date.  Defaults to today.

        Returns
        -------
        ShadowComparisonReport with full comparison including biggest_alpha.
        """
        session_date = asof or date.today()
        shadow_pnls: dict[str, float] = {}
        for rule_name, account in self.accounts.items():
            history = account.pnl_history
            if history:
                # Sum pnl_today for all entries on or before session_date
                total = sum(
                    h["pnl_today"]
                    for h in history
                    if _parse_date_str(h["asof"]) <= session_date
                )
                shadow_pnls[rule_name] = total
            else:
                shadow_pnls[rule_name] = 0.0

        winners = [name for name, pnl in shadow_pnls.items() if pnl > real_pnl]
        losers = [name for name, pnl in shadow_pnls.items() if pnl <= real_pnl]

        # biggest_alpha
        if shadow_pnls:
            best_rule = max(shadow_pnls, key=lambda k: shadow_pnls[k] - real_pnl)
            best_alpha = shadow_pnls[best_rule] - real_pnl
        else:
            best_rule = "none"
            best_alpha = 0.0

        # Build evidence summary
        lines = [
            f"Shadow Account Comparison — {session_date.isoformat()}",
            f"Real P&L: ${real_pnl:+,.2f}",
            "",
            "Rule Performance:",
        ]
        for rule_name, pnl in sorted(shadow_pnls.items(), key=lambda x: x[1], reverse=True):
            alpha = pnl - real_pnl
            tag = "✓ WINNER" if pnl > real_pnl else "✗ LOSER"
            lines.append(f"  {tag}  {rule_name:<30} shadow_pnl=${pnl:+,.2f}  alpha=${alpha:+,.2f}")

        lines.extend([
            "",
            f"Biggest alpha: {best_rule} (${best_alpha:+,.2f} vs real)",
            f"Winners: {len(winners)}  Losers: {len(losers)}",
            "",
            "POST-HOC RATIONALIZATION DEFENSE:",
            "Rules that beat real P&L provide counterfactual evidence that the",
            "production decision rule left alpha on the table.  Rules that lose",
            "confirm the production rule was not systematically wrong for the period.",
        ])

        summary = "\n".join(lines)
        if len(summary) > 2048:
            summary = summary[:2045] + "..."

        return ShadowComparisonReport(
            real_pnl=real_pnl,
            shadow_pnls=shadow_pnls,
            counterfactual_winners=winners,
            counterfactual_losers=losers,
            biggest_alpha=(best_rule, best_alpha),
            evidence_summary=summary,
            asof=session_date,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date_str(s: str) -> date:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return date.today()
