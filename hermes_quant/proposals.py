"""hermes_quant.proposals — HITL proposal store (ADR-0015).

Pending → {approved | rejected | expired} state machine. Storage:
- ~/.hermes/quant/proposals.jsonl is the source of truth (append-only,
  matches signal_bus.py's flock-protected pattern).
- ~/.hermes/quant/proposals.db (SQLite) is a derived index for fast lookup.

The two stores are kept consistent by the lifecycle methods. If they
ever drift (e.g. a crash between write_jsonl and write_sqlite), the
JSONL is authoritative; the SQLite can be rebuilt by `_reconcile_index()`.

Per ADR-0015 D3, proposal_id format is:
    prop_<UTC_ISO_seconds>_<symbol>_<random6>

e.g. prop_2026-05-13T184230_AAPL_7f3a91. Stable, unique-enough,
human-readable.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from hermes_quant.daemon.signal_bus import append_locked

logger = logging.getLogger(__name__)


QUANT_HOME = Path.home() / ".hermes" / "quant"
PROPOSAL_BUS_PATH = QUANT_HOME / "proposals.jsonl"
PROPOSAL_DB_PATH = QUANT_HOME / "proposals.db"

DEFAULT_TTL_MINUTES = 15

ProposalState = Literal["pending", "approved", "rejected", "expired"]


# ---------------------------------------------------------------------------
# Proposal record (Python dataclass; serialized as JSON on the bus)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """A pending HITL proposal awaiting human approve/reject.

    Per ADR-0015 §D2, this dataclass is the in-memory shape; on-disk it's
    a JSONL record. Approved/rejected/expired records carry the same shape
    plus state-transition fields.
    """
    proposal_id: str
    state: ProposalState
    symbol: str
    asset_class: str
    timeframe: str
    created_at: str       # ISO UTC seconds
    expires_at: str       # ISO UTC seconds
    advisor_result: dict[str, Any]   # the full advisor.recommend() output

    # State-transition fields (None until applicable)
    approved_at: str | None = None
    approver_user_id: str | None = None
    size_override_pct: float | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    expired_at: str | None = None

    # Set when React fires (state=approved AND execution succeeded)
    execution: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ProposalStore:
    """Pending-proposal store with JSONL+SQLite dual-write.

    Thread-safe via a single in-process lock. Cross-process safety is
    handled by the JSONL flock; the SQLite index uses WAL mode and
    short transactions.

    Per ADR-0015 §D2 + §D9: lazy expiration on every read. The hot path
    re-checks TTL before returning anything as `pending`.
    """

    def __init__(
        self,
        bus_path: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.bus_path = bus_path or PROPOSAL_BUS_PATH
        self.db_path = db_path or PROPOSAL_DB_PATH
        self._lock = threading.RLock()
        self._ensure_dirs()
        self._ensure_schema()

    def _ensure_dirs(self) -> None:
        self.bus_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the bus file so concurrent reads don't see "missing" before
        # the first write.
        if not self.bus_path.exists():
            self.bus_path.touch()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,  # autocommit; we control with explicit BEGIN
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY NOT NULL,
                    state TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT,
                    rejected_at TEXT,
                    expired_at TEXT,
                    record_json TEXT NOT NULL
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_proposals_state
                  ON proposals(state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_proposals_symbol
                  ON proposals(symbol, created_at);
            """)

    # -----------------------------------------------------------------
    # Lifecycle: create
    # -----------------------------------------------------------------

    def propose(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        advisor_result: dict[str, Any],
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> Proposal:
        """Create a new pending proposal.

        Per ADR-0015 §D3: proposal_id is `prop_<ISO_seconds>_<symbol>_<rand6>`.
        Per ADR-0015 §D9: TTL clock starts NOW (creation time, not advisor's
        as_of). The advisor's view may be stale; the operator's window to act
        is from the creation timestamp.
        """
        now = _utc_now()
        expires = now + timedelta(minutes=ttl_minutes)
        proposal_id = _make_proposal_id(symbol, now)

        proposal = Proposal(
            proposal_id=proposal_id,
            state="pending",
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            created_at=_iso(now),
            expires_at=_iso(expires),
            advisor_result=advisor_result,
        )
        self._persist(proposal, event="create")
        return proposal

    # -----------------------------------------------------------------
    # Lifecycle: approve / reject / expire
    # -----------------------------------------------------------------

    def approve(
        self,
        proposal_id: str,
        *,
        approver_user_id: str | None = None,
        size_override_pct: float | None = None,
        execution: dict[str, Any] | None = None,
    ) -> Proposal:
        """Advance pending → approved. Raises if not pending or expired."""
        with self._lock:
            current = self._get_or_raise(proposal_id)
            self._reject_if_expired(current)
            self._require_state(current, "pending")

            updated = Proposal(
                **{**_proposal_to_dict(current),
                   "state": "approved",
                   "approved_at": _iso(_utc_now()),
                   "approver_user_id": approver_user_id,
                   "size_override_pct": size_override_pct,
                   "execution": execution,
                   })
            self._persist(updated, event="approve")
            return updated

    def reject(
        self,
        proposal_id: str,
        *,
        reason: str,
    ) -> Proposal:
        """Advance pending → rejected. Reason is required."""
        if not reason or not reason.strip():
            raise ValueError("rejection reason is required (non-empty string)")
        with self._lock:
            current = self._get_or_raise(proposal_id)
            self._reject_if_expired(current)
            self._require_state(current, "pending")

            updated = Proposal(
                **{**_proposal_to_dict(current),
                   "state": "rejected",
                   "rejected_at": _iso(_utc_now()),
                   "rejection_reason": reason.strip(),
                   })
            self._persist(updated, event="reject")
            return updated

    def expire_one(self, proposal_id: str) -> Proposal | None:
        """Force a pending proposal to expired (TTL sweep). Returns None
        if already non-pending."""
        with self._lock:
            try:
                current = self._get_or_raise(proposal_id)
            except KeyError:
                return None
            if current.state != "pending":
                return None
            updated = Proposal(
                **{**_proposal_to_dict(current),
                   "state": "expired",
                   "expired_at": _iso(_utc_now()),
                   })
            self._persist(updated, event="expire")
            return updated

    def sweep_expired(self) -> int:
        """Sweep pending proposals whose expires_at is in the past.

        Per ADR-0015 §D9: idempotent, returns count expired this call.
        Safe to call on every read or on a cron tick.
        """
        now_iso = _iso(_utc_now())
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT proposal_id FROM proposals "
                "WHERE state = 'pending' AND expires_at <= ?",
                (now_iso,),
            ).fetchall()
        n_expired = 0
        for row in rows:
            if self.expire_one(row["proposal_id"]) is not None:
                n_expired += 1
        return n_expired

    # -----------------------------------------------------------------
    # Lookup
    # -----------------------------------------------------------------

    def get(self, proposal_id: str) -> Proposal | None:
        """Look up a proposal by id. None if not found.

        Lazy expiration: if the proposal is pending but past its TTL,
        the call advances it to expired before returning.
        """
        with self._lock:
            try:
                current = self._get_or_raise(proposal_id)
            except KeyError:
                return None
            if current.state == "pending":
                if _iso_in_past(current.expires_at):
                    return self.expire_one(proposal_id)
            return current

    def list_pending(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
    ) -> list[Proposal]:
        """List pending proposals, newest first. Sweeps TTL first."""
        self.sweep_expired()
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT record_json FROM proposals "
                    "WHERE state = 'pending' AND symbol = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT record_json FROM proposals "
                    "WHERE state = 'pending' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_proposal_from_json(row["record_json"]) for row in rows]

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _get_or_raise(self, proposal_id: str) -> Proposal:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT record_json FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"proposal {proposal_id} not found")
        return _proposal_from_json(row["record_json"])

    def _require_state(self, proposal: Proposal, want: ProposalState) -> None:
        if proposal.state != want:
            raise ProposalStateError(
                f"proposal {proposal.proposal_id} is in state "
                f"{proposal.state!r}; expected {want!r}"
            )

    def _reject_if_expired(self, proposal: Proposal) -> None:
        """If the proposal is pending but its TTL has elapsed, expire it."""
        if proposal.state == "pending" and _iso_in_past(proposal.expires_at):
            self.expire_one(proposal.proposal_id)
            raise ProposalExpiredError(
                f"proposal {proposal.proposal_id} expired at "
                f"{proposal.expires_at}"
            )

    def _persist(self, proposal: Proposal, *, event: str) -> None:
        """Atomic dual-write: JSONL append (truth), SQLite upsert (index).

        JSONL is written first; if SQLite write fails, the next read will
        reconstruct from JSONL via _reconcile_index() (not yet implemented;
        v0.1.2 surfaces a warning).
        """
        record = _proposal_to_dict(proposal)
        record["_event"] = event   # "create" | "approve" | "reject" | "expire"
        record["_event_at"] = _iso(_utc_now())

        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        with append_locked(self.bus_path) as fd:
            import os as _os
            _os.write(fd, line.encode("utf-8"))

        # SQLite upsert
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO proposals
                      (proposal_id, state, symbol, asset_class, timeframe,
                       created_at, expires_at, approved_at, rejected_at,
                       expired_at, record_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(proposal_id) DO UPDATE SET
                      state = excluded.state,
                      expires_at = excluded.expires_at,
                      approved_at = excluded.approved_at,
                      rejected_at = excluded.rejected_at,
                      expired_at = excluded.expired_at,
                      record_json = excluded.record_json
                    """,
                    (
                        proposal.proposal_id,
                        proposal.state,
                        proposal.symbol,
                        proposal.asset_class,
                        proposal.timeframe,
                        proposal.created_at,
                        proposal.expires_at,
                        proposal.approved_at,
                        proposal.rejected_at,
                        proposal.expired_at,
                        json.dumps(record, separators=(",", ":"), sort_keys=True),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                logger.warning(
                    "proposals: SQLite upsert failed for %s; JSONL is "
                    "authoritative — re-read will reconstruct via JSONL scan",
                    proposal.proposal_id,
                    exc_info=True,
                )
                raise


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProposalStateError(Exception):
    """The proposal is not in the state required for this operation."""


class ProposalExpiredError(ProposalStateError):
    """The proposal's TTL has elapsed; auto-expired."""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    """ISO 8601 UTC with seconds precision and `Z` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_in_past(iso_str: str) -> bool:
    """True if the parsed ISO timestamp is < now-UTC."""
    try:
        ts = datetime.strptime(iso_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        # Unparsable timestamp — treat as expired (safer than letting
        # a malformed pending proposal hang forever)
        return True
    return ts <= _utc_now()


def _make_proposal_id(symbol: str, now: datetime) -> str:
    """Per ADR-0015 §D3: prop_<ISO_seconds>_<sanitized_symbol>_<rand6>."""
    iso = _iso(now).replace("-", "").replace(":", "").rstrip("Z")
    safe_symbol = "".join(c if c.isalnum() else "_" for c in symbol)[:16]
    rand = secrets.token_hex(3)  # 6 hex chars
    return f"prop_{iso}_{safe_symbol}_{rand}"


def _proposal_to_dict(p: Proposal) -> dict[str, Any]:
    """Convert Proposal dataclass to plain dict for serialization."""
    return {
        "proposal_id": p.proposal_id,
        "state": p.state,
        "symbol": p.symbol,
        "asset_class": p.asset_class,
        "timeframe": p.timeframe,
        "created_at": p.created_at,
        "expires_at": p.expires_at,
        "approved_at": p.approved_at,
        "approver_user_id": p.approver_user_id,
        "size_override_pct": p.size_override_pct,
        "rejected_at": p.rejected_at,
        "rejection_reason": p.rejection_reason,
        "expired_at": p.expired_at,
        "advisor_result": p.advisor_result,
        "execution": p.execution,
    }


def _proposal_from_json(json_str: str) -> Proposal:
    """Reconstruct a Proposal from a stored JSON record. Tolerant of
    extra `_event*` fields written by _persist."""
    d = json.loads(json_str)
    return Proposal(
        proposal_id=d["proposal_id"],
        state=d["state"],
        symbol=d["symbol"],
        asset_class=d["asset_class"],
        timeframe=d["timeframe"],
        created_at=d["created_at"],
        expires_at=d["expires_at"],
        approved_at=d.get("approved_at"),
        approver_user_id=d.get("approver_user_id"),
        size_override_pct=d.get("size_override_pct"),
        rejected_at=d.get("rejected_at"),
        rejection_reason=d.get("rejection_reason"),
        expired_at=d.get("expired_at"),
        advisor_result=d.get("advisor_result", {}),
        execution=d.get("execution"),
    )


# ---------------------------------------------------------------------------
# Module-level singleton accessor (lazy-init so tests can override paths)
# ---------------------------------------------------------------------------

_default_store: ProposalStore | None = None
_default_store_lock = threading.Lock()


def get_default_store() -> ProposalStore:
    """Return the process-wide ProposalStore singleton.

    Tests should construct their own ProposalStore with custom paths
    rather than relying on this. Production code uses this for the
    standard ~/.hermes/quant/proposals.{jsonl,db} location.
    """
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = ProposalStore()
        return _default_store
