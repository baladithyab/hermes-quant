"""quantcore.ledger — append-only JSONL paper ledger (rail #7).

One file: <quant-state>/ledger.jsonl. Every event is one line:
  {"event": <kind>, "ts": <iso-utc>, "seq": <int>, "prev_hash": <sha256-12>, ...payload}

Event kinds:
  proposal        — gate-approved Proposal awaiting the human
  approval        — human approved (AskUserQuestion outcome)
  rejection       — human rejected, or gate silence worth recording
  fill            — human-confirmed execution (manual / broker-readback)
  mark            — NAV mark (price snapshot for P&L)
  settle          — realized outcome for a closed (or horizon-expired) position
  halt / resume   — circuit-breaker state changes
  gate_decision   — every GateDecision, action or silence (full audit)

Integrity: each line carries seq + sha256(prev_line)[:12]. `verify_chain`
detects tampering/truncation in the user-visible state folder. Writes are
atomic-append with fsync (money-software crash-safety).

PortfolioState is RECONSTRUCTED from the ledger (hermes-quant ADR-0011 /
ADR-0085 ledger-authority posture) — never hand-edited.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any

from quantcore.config import StateConfig, load_state_config
from quantcore.schemas import Fill, PortfolioState, Position, Proposal

LEDGER_NAME = "ledger.jsonl"


def parse_iso(s: str) -> datetime:
    """fromisoformat that tolerates the 'Z' suffix on Python 3.10."""
    return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]


class Ledger:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / LEDGER_NAME

    # -- write ---------------------------------------------------------------

    def _last_line(self) -> str | None:
        if not self.path.exists():
            return None
        last = None
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        return last

    def append(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        last = self._last_line()
        record = {
            "event": event,
            "ts": _now().isoformat(),
            "seq": (json.loads(last)["seq"] + 1) if last else 0,
            "prev_hash": _hash_line(last) if last else "genesis",
            **payload,
        }
        line = json.dumps(record, default=str, sort_keys=True)
        with open(self.path, "a", buffering=1) as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record

    def record_proposal(self, proposal: Proposal) -> dict[str, Any]:
        return self.append("proposal", {"proposal": proposal.model_dump(mode="json")})

    def record_decision_on_proposal(
        self, proposal_id: str, decision: str, note: str = ""
    ) -> dict[str, Any]:
        assert decision in ("approval", "rejection", "expired")
        return self.append(decision, {"proposal_id": proposal_id, "note": note})

    def record_fill(self, fill: Fill) -> dict[str, Any]:
        return self.append("fill", {"fill": fill.model_dump(mode="json")})

    def record_mark(self, asset: str, price: float, nav: float) -> dict[str, Any]:
        return self.append("mark", {"asset": asset, "price": price, "nav": nav})

    # -- read ----------------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> tuple[bool, str]:
        """True if seq is contiguous and every prev_hash matches. O(n)."""
        prev_line: str | None = None
        if not self.path.exists():
            return True, "empty"
        with open(self.path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("seq") != i:
                    return False, f"seq gap at line {i}: got {rec.get('seq')}"
                expected = _hash_line(prev_line) if prev_line else "genesis"
                if rec.get("prev_hash") != expected:
                    return False, f"hash mismatch at line {i}"
                prev_line = line
        return True, "ok"

    def pending_proposals(
        self, now: datetime | None = None, ttl_hours: float | None = None
    ) -> list[Proposal]:
        """Proposals with no later approval/rejection/expiry/fill.

        With ttl_hours set, proposals whose created_at is older than the TTL
        are EXCLUDED even though they still look pending in the chain
        (deterministic expiry, R1-05). cmd_decide uses this to refuse stale
        approvals; the `expire` verb sweeps them into explicit 'expired'
        events. ttl_hours=None preserves the raw pending view.
        """
        now = now or _now()
        proposals: dict[str, Proposal] = {}
        closed: set[str] = set()
        for rec in self.events():
            if rec["event"] == "proposal":
                p = Proposal(**rec["proposal"])
                proposals[p.proposal_id] = p
            elif rec["event"] in ("approval", "rejection", "expired"):
                if rec["event"] != "approval":
                    closed.add(rec["proposal_id"])
            elif rec["event"] == "fill":
                closed.add(rec["fill"]["proposal_id"])
        out = []
        for pid, p in proposals.items():
            if pid in closed:
                continue
            if ttl_hours is not None and (now - p.created_at) > timedelta(hours=ttl_hours):
                continue  # stale: pending-looking but past the deterministic TTL
            out.append(p)
        return out

    def portfolio(self, config: StateConfig | None = None) -> PortfolioState:
        """Reconstruct PortfolioState from fills + marks. Ledger is authority."""
        cfg = config or load_state_config(self.state_dir)
        nav = cfg.paper_nav
        peak = nav
        day_start = nav
        day: str | None = None
        positions: dict[str, Position] = {}
        prop_by_id: dict[str, dict] = {}
        halted = False
        halt_reason: str | None = None
        halt_until: datetime | None = None
        last_loss_at: datetime | None = None
        asof = _now()

        for rec in self.events():
            ts = parse_iso(rec["ts"])
            d = ts.date().isoformat()
            if day is None or d != day:
                day = d
                day_start = nav
            if rec["event"] == "proposal":
                prop_by_id[rec["proposal"]["proposal_id"]] = rec["proposal"]
            elif rec["event"] == "fill":
                f = rec["fill"]
                prop = prop_by_id.get(f["proposal_id"], {})
                sig = prop.get("signal", {})
                asset = f["asset"]
                new_pct = float(f["filled_position_pct"])
                if abs(new_pct) < 1e-9:
                    positions.pop(asset, None)
                else:
                    positions[asset] = Position(
                        asset=asset,
                        asset_class=sig.get("asset_class", "equity"),
                        position_pct=new_pct,
                        avg_price=float(f["fill_price"]),
                        opened_at=parse_iso(f["filled_at"]),
                    )
            elif rec["event"] == "mark":
                nav = float(rec["nav"])
                peak = max(peak, nav)
            elif rec["event"] == "settle":
                if float(rec.get("realized_return", 0.0)) < 0:
                    last_loss_at = ts
            elif rec["event"] == "halt":
                halted = True
                halt_reason = rec.get("reason")
                hu = rec.get("halt_until")
                halt_until = parse_iso(hu) if hu else None
            elif rec["event"] == "resume":
                halted = False
                halt_reason = None
                halt_until = None
            asof = ts

        # auto-clear an expired timed halt
        if halted and halt_until is not None and _now() >= halt_until:
            halted = False

        return PortfolioState(
            nav=nav,
            peak_nav=peak,
            day_start_nav=day_start,
            positions=list(positions.values()),
            asof=asof,
            halted=halted,
            halt_reason=halt_reason,
            halt_until=halt_until,
            last_loss_at=last_loss_at,
        )


def new_proposal_id() -> str:
    return uuid.uuid4().hex[:16]
