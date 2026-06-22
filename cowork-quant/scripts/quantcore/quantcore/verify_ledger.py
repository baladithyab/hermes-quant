"""quantcore.verify_ledger — ledger integrity + cross-module consistency (B-30, arch §4.3).

TradeTrap (2512.02261) shows the ledger is the worst attack surface: an adversary
appends fabricated-but-plausible position entries (real-looking timestamps, prices,
ids) that cascade across sessions. CrAIBench (2503.16248) corroborates that
prompt-level defenses fail against corrupted stored state.

THREAT MODEL (be honest about what the hash chain does and does NOT do):
  * `Ledger.verify_chain()` (already in ledger.py) recomputes seq + sha256(prev_line)
    head-to-tail. It DETECTS truncation, middle-edits, reordering, and accidental
    corruption.
  * It does NOT make the file tamper-proof: an attacker who can read the file can
    recompute the forward chain and append a well-formed poisoned line. The ledger
    lives in the user's workspace folder and is intentionally user-visible/auditable,
    not cryptographically sealed (no secret key in a user-visible setup).
  * The real defense against poisoning is therefore CROSS-MODULE CONSISTENCY: every
    fill must trace to a prior *approved* proposal, and every open position must trace
    to a validated fill. A line that doesn't (an "orphan") is flagged regardless of
    whether the hash chain is intact. This is TradeTrap's "state verification +
    cross-module consistency checking" recommendation.

Callers (SessionStart hook, every CLI entry) run `verify_ledger()` and ABSTAIN/halt
on any failure (rail R11). Never proceed on a broken or inconsistent ledger.

stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from quantcore.ledger import Ledger
from quantcore.schemas import PortfolioState


@dataclass
class VerifyReport:
    ok: bool
    chain_ok: bool
    chain_detail: str
    orphan_fills: list[str] = field(default_factory=list)  # proposal_ids
    unapproved_fills: list[str] = field(default_factory=list)
    untraceable_positions: list[str] = field(default_factory=list)  # assets
    spurious_resumes: list[int] = field(default_factory=list)  # seq of bad resumes
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "ledger OK: chain intact, all fills + positions traceable"
        bits = []
        if not self.chain_ok:
            bits.append(f"chain BROKEN ({self.chain_detail})")
        if self.orphan_fills:
            bits.append(f"orphan fills (no prior proposal): {self.orphan_fills}")
        if self.unapproved_fills:
            bits.append(f"fills without approval: {self.unapproved_fills}")
        if self.untraceable_positions:
            bits.append(f"untraceable positions: {self.untraceable_positions}")
        if self.spurious_resumes:
            bits.append(f"spurious resumes (no active halt) at seq {self.spurious_resumes}")
        return "LEDGER INTEGRITY FAILURE — " + "; ".join(bits)


def verify_ledger(state_dir: Path) -> VerifyReport:
    """Full integrity pass: hash chain + cross-module consistency.

    Cross-module rules:
      * a `fill` whose proposal_id was never `proposal`'d  -> orphan_fill
      * a `fill` whose proposal_id was never `approval`'d  -> unapproved_fill
        (the human must approve before a fill is recorded — rail #4/HITL)
      * an open position with no traceable approved+filled chain -> untraceable
    """
    led = Ledger(state_dir)
    chain_ok, chain_detail = led.verify_chain()

    proposed: set[str] = set()
    approved: set[str] = set()
    filled_pids: set[str] = set()
    orphan_fills: list[str] = []
    unapproved_fills: list[str] = []
    spurious_resumes: list[int] = []
    halted = False

    for rec in led.events():
        ev = rec.get("event")
        if ev == "proposal":
            proposed.add(rec["proposal"]["proposal_id"])
        elif ev == "approval":
            approved.add(rec["proposal_id"])
        elif ev == "fill":
            pid = rec["fill"]["proposal_id"]
            filled_pids.add(pid)
            if pid not in proposed:
                orphan_fills.append(pid)
            elif pid not in approved:
                unapproved_fills.append(pid)
        elif ev == "halt":
            halted = True
        elif ev == "resume":
            # a resume lifting no active halt is a poisoning signal: a chain-intact
            # appender could fabricate one to clear a circuit breaker (review
            # threat-model gap; TradeTrap state-tampering class, 2512.02261)
            if not halted:
                spurious_resumes.append(int(rec.get("seq", -1)))
            halted = False

    # positions must trace to an approved+filled proposal
    untraceable: list[str] = []
    try:
        portfolio: PortfolioState = led.portfolio()
        # map asset -> the proposal that justified its latest fill
        justified_assets: set[str] = set()
        prop_asset: dict[str, str] = {}
        for rec in led.events():
            if rec.get("event") == "proposal":
                p = rec["proposal"]
                prop_asset[p["proposal_id"]] = p["signal"]["asset"]
            elif rec.get("event") == "fill":
                pid = rec["fill"]["proposal_id"]
                if pid in approved and pid in proposed:
                    justified_assets.add(rec["fill"]["asset"])
        for pos in portfolio.positions:
            if pos.asset not in justified_assets:
                untraceable.append(pos.asset)
    except Exception as e:  # portfolio reconstruction itself failed -> hard fail
        return VerifyReport(
            ok=False,
            chain_ok=chain_ok,
            chain_detail=chain_detail,
            orphan_fills=orphan_fills,
            unapproved_fills=unapproved_fills,
            notes=[f"portfolio reconstruction failed: {e!r}"],
        )

    ok = (
        chain_ok
        and not orphan_fills
        and not unapproved_fills
        and not untraceable
        and not spurious_resumes
    )
    return VerifyReport(
        ok=ok,
        chain_ok=chain_ok,
        chain_detail=chain_detail,
        orphan_fills=orphan_fills,
        unapproved_fills=unapproved_fills,
        untraceable_positions=untraceable,
        spurious_resumes=spurious_resumes,
    )


def assert_view_matches_state(shown_positions: list[dict], state_dir: Path) -> None:
    """Cross-module consistency: the position state SHOWN to the committee must
    equal the gate-derived state from the verified ledger (TradeTrap Model-View vs
    Market-View divergence is the exact failure). Call this in the committee
    command before analysts reason about the book.
    """
    led = Ledger(state_dir)
    truth = {p.asset: round(p.position_pct, 9) for p in led.portfolio().positions}
    shown = {d["asset"]: round(float(d["position_pct"]), 9) for d in shown_positions}
    if truth != shown:
        raise AssertionError(
            f"committee book view diverges from ledger truth: shown={shown} "
            f"ledger={truth}"
        )
