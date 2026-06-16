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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hermes_quant.daemon.signal_bus import append_locked

if TYPE_CHECKING:  # pragma: no cover - typing only (avoid import cost/cycles)
    from hermes_quant.options.multileg import MultiLegProposal
    from hermes_quant.risk.options_gate import OptionsGateResult

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
    created_at: str  # ISO UTC seconds
    expires_at: str  # ISO UTC seconds
    advisor_result: dict[str, Any]  # the full advisor.recommend() output

    # State-transition fields (None until applicable)
    approved_at: str | None = None
    approver_user_id: str | None = None
    size_override_pct: float | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    expired_at: str | None = None

    # Set when React fires (state=approved AND execution succeeded)
    execution: dict[str, Any] | None = None

    # ── B01 multi-leg producer seam (ADR-0029) ──────────────────────────────
    # proposal_kind discriminates the React routing (react/dispatch.select_reactor):
    #   'equity'    (default) -> PaperReactor; the existing equity path, UNCHANGED.
    #   'multi_leg'           -> MultiLegPaperReactor (default-OFF behind the flag).
    # When proposal_kind == 'multi_leg', ``multi_leg`` carries the serialized
    # MultiLegProposal payload (OCC option legs + stock leg + the OptionsGateResult
    # FIELDS) so store.get() can RE-MINT a MultiLegProposal via from_gate_result
    # (the #38 constructor-lock: risk_gate_pass=True is NEVER hand-set — it is
    # rebuilt by re-running from_gate_result over the persisted gate result). The
    # field is None for every equity proposal, so equity rows reconstruct unchanged
    # and serialize byte-identically (default omitted by the JSON encoder path).
    proposal_kind: str = "equity"
    multi_leg: dict[str, Any] | None = None


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
            self._migrate_add_proposal_kind(conn)

    @staticmethod
    def _migrate_add_proposal_kind(conn: sqlite3.Connection) -> None:
        """B01 additive migration: add the nullable, DEFAULT-'equity' ``proposal_kind``
        column to a pre-B01 ``proposals`` table.

        Backward-compatible by construction: a pre-B01 DB has no ``proposal_kind``
        column; ``ALTER TABLE ADD COLUMN ... DEFAULT 'equity'`` backfills every
        existing equity row with 'equity' and adds nothing to the record bytes (the
        authoritative shape stays in ``record_json``; this column is only a fast
        discriminator/index). The full record (incl. the multi-leg payload) always
        lives in ``record_json``, so even a DB that never ran this migration
        reconstructs correctly — the column is a convenience index, not the source
        of truth. Idempotent: skipped when the column already exists.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(proposals)")}
        if "proposal_kind" not in cols:
            conn.execute(
                "ALTER TABLE proposals ADD COLUMN proposal_kind TEXT "
                "NOT NULL DEFAULT 'equity'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposals_kind "
            "ON proposals(proposal_kind, state)"
        )

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

    def propose_multi_leg(
        self,
        *,
        proposal: MultiLegProposal,
        gate_result: OptionsGateResult,
        advisor_result: dict[str, Any] | None = None,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> Proposal:
        """Persist an ALREADY-GATED, ALREADY-MINTED ``MultiLegProposal`` (B01).

        The producer builds + mints the ``MultiLegProposal`` via
        ``MultiLegProposal.from_gate_result`` (the #38 constructor-lock — the ONLY
        way ``risk_gate_pass=True`` can exist) and hands it here together with the
        ``OptionsGateResult`` it came from. We serialize BOTH (legs via OCC + the
        gate RESULT fields) into the ``multi_leg`` payload so ``get()`` can RE-MINT
        the proposal on reconstruct by replaying ``from_gate_result`` over the
        rebuilt ``OptionsGateResult`` — ``risk_gate_pass`` is therefore NEVER
        hand-set on the read path either; it is always the gate's verdict copied by
        the blessed seam.

        ``proposal.proposal_id`` (already in the prop_<ISO>_<u>_<rand6> shape, minted
        by the producer) is reused as the store key so the multi_leg_id the reactor
        dedups on matches the proposal id end-to-end.

        Money-software rail: a ``risk_gate_pass != True`` (gate-rejected) proposal is
        still PERSISTED for replay/audit — but it carries the rejected verdict, and
        the reactor refuses to fill it (gate-is-final-authority). The producer
        decides whether to even call this on a reject (B01's producer does NOT, so an
        ungated/rejected structure never lands a *passing* proposal — see
        options/recipes.py).
        """
        now = _utc_now()
        expires = now + timedelta(minutes=ttl_minutes)

        record = Proposal(
            proposal_id=proposal.proposal_id,
            state="pending",
            symbol=proposal.underlying,
            asset_class="multi_leg",
            timeframe=proposal.timeframe if hasattr(proposal, "timeframe") else "",
            created_at=_iso(now),
            expires_at=_iso(expires),
            advisor_result=advisor_result or {},
            proposal_kind="multi_leg",
            multi_leg=_multi_leg_to_dict(proposal, gate_result),
        )
        self._persist(record, event="create")
        return record

    # -----------------------------------------------------------------
    # Lifecycle: approve / reject / expire
    # -----------------------------------------------------------------

    def claim_for_approval(
        self,
        proposal_id: str,
        *,
        approver_user_id: str | None = None,
        size_override_pct: float | None = None,
    ) -> Proposal:
        """ATOMICALLY claim a pending proposal for approval (ar16).

        This is the compare-and-set that closes the ``quant_approve`` TOCTOU
        double-fire window. The HITL approve flow used to do a non-atomic
        check-then-act — ``store.get()`` to read ``state == 'pending'``, then
        ``reactor.execute()`` to FIRE the order, then ``store.approve()`` to
        advance state AFTER the fire. Two concurrent approves of the SAME
        proposal_id both passed the read-state gate (state hadn't advanced
        yet), both fired the reactor, and — because the reactor stamps a FRESH
        ``asof_execution`` per call and the only idempotency is keyed on
        ``(proposal_id, asof_execution, ...)`` — BOTH fills were recorded.
        Capital moved twice.

        The fix: transition the proposal out of ``pending`` *before* the fire,
        in a single ``BEGIN IMMEDIATE`` transaction with a conditional
        ``UPDATE ... WHERE state='pending'`` (the SQLite index is the cross-
        process write-lock arbiter, matching ``daemon/halt_state`` and
        ``state/portfolio_state``). Exactly ONE concurrent caller wins the
        UPDATE (``rowcount == 1``); every other caller sees ``rowcount == 0``
        and raises :class:`ProposalStateError` — so it never reaches the fire.

        Safe-money polarity (per the ar16 brief): the claim advances the
        proposal to ``approved`` (claimed) BEFORE the fire. If the fire then
        fails, the proposal is left CLAIMED — a claimed-but-unfired proposal
        that needs operator attention/re-approval is strictly safer than a
        double-fire. The caller attaches the execution record afterward via
        :meth:`record_execution`.

        Raises:
            KeyError: proposal not found.
            ProposalExpiredError: TTL elapsed (auto-expired).
            ProposalStateError: not pending (already claimed/approved/rejected
                — i.e. a concurrent caller already won the claim).
        """
        approved_at = _iso(_utc_now())
        with self._lock:
            # Read current state for the TTL gate + to build the full record.
            current = self._get_or_raise(proposal_id)
            self._reject_if_expired(current)  # raises ProposalExpiredError if past TTL

            updated = Proposal(
                **{
                    **_proposal_to_dict(current),
                    "state": "approved",
                    "approved_at": approved_at,
                    "approver_user_id": approver_user_id,
                    "size_override_pct": size_override_pct,
                }
            )
            record = _proposal_to_dict(updated)

            # Atomic compare-and-set on the SQLite index. BEGIN IMMEDIATE takes
            # the write lock up-front so two processes serialize here; the
            # WHERE state='pending' guard means only the FIRST to commit flips
            # the row — every later contender updates 0 rows and loses.
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        "UPDATE proposals SET "
                        "  state = 'approved', "
                        "  approved_at = ?, "
                        "  record_json = ? "
                        "WHERE proposal_id = ? AND state = 'pending'",
                        (
                            approved_at,
                            json.dumps(record, separators=(",", ":"), sort_keys=True),
                            proposal_id,
                        ),
                    )
                    won = cur.rowcount == 1
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            if not won:
                # Either a concurrent caller already claimed it, or it is no
                # longer pending (rejected/expired/approved). Re-read to give a
                # precise error and to surface expiry as ProposalExpiredError.
                latest = self._get_or_raise(proposal_id)
                self._reject_if_expired(latest)
                raise ProposalStateError(
                    f"proposal {proposal_id} is in state {latest.state!r}; "
                    "expected 'pending' (already claimed/approved by a "
                    "concurrent approve)"
                )

            # We won the claim. Append the approve event to the JSONL audit log
            # (the source of truth) so the transition is durable + reconcilable.
            # The SQLite row is already flipped by the UPDATE above; the
            # append-only JSONL latest-event-per-id wins on reconcile.
            self._append_audit(updated, event="approve")
            return updated

    def record_execution(
        self,
        proposal_id: str,
        *,
        execution: dict[str, Any] | None,
    ) -> Proposal:
        """Attach the fired execution record onto an already-claimed proposal
        (ar16). Called AFTER :meth:`claim_for_approval` + ``reactor.execute``.

        The state transition already happened in ``claim_for_approval`` (the
        proposal is ``approved``); this only writes the execution payload onto
        the existing approved record so the audit trail carries the fill. It is
        a no-op-safe update of the ``execution`` field; it does NOT re-gate on
        state (the proposal is already terminal-approved and the fire is done).
        """
        with self._lock:
            current = self._get_or_raise(proposal_id)
            updated = Proposal(
                **{
                    **_proposal_to_dict(current),
                    "execution": execution,
                }
            )
            self._persist(updated, event="approve")
            return updated

    def release_claim(self, proposal_id: str) -> Proposal | None:
        """Roll a claimed (approved) proposal BACK to pending (ar16).

        Used ONLY when the fire was a PROVEN no-capital refusal — the reactor
        raised/refused before any fill landed on the executions bus (fill-size
        invariant rejection, or pre-trade admissibility rejection). In those
        cases no money moved, so the safest, least-surprising behavior is to
        restore the proposal to ``pending`` exactly as the pre-ar16 flow did,
        letting the operator revise + retry. This is NEVER called after a
        successful fill — only on the no-capital-moved refusal branches.

        Returns the re-pended proposal, or None if it is no longer in the
        ``approved`` claimed state (defensive — never resurrects a fill).
        """
        with self._lock:
            try:
                current = self._get_or_raise(proposal_id)
            except KeyError:
                return None
            # Only roll back a claim we made: approved with no execution
            # attached. If an execution is present, a fill happened — refuse to
            # re-pend (never resurrect a fired proposal).
            if current.state != "approved" or current.execution is not None:
                return None
            updated = Proposal(
                **{
                    **_proposal_to_dict(current),
                    "state": "pending",
                    "approved_at": None,
                    "approver_user_id": None,
                    "size_override_pct": None,
                }
            )
            self._persist(updated, event="create")
            return updated

    def approve(
        self,
        proposal_id: str,
        *,
        approver_user_id: str | None = None,
        size_override_pct: float | None = None,
        execution: dict[str, Any] | None = None,
    ) -> Proposal:
        """Advance pending → approved. Raises if not pending or expired.

        NOTE (ar16): this is the legacy non-atomic transition (read-then-write
        under the in-process lock only). The HITL fire path in
        ``tools.quant_approve`` no longer uses it — it claims atomically via
        :meth:`claim_for_approval` BEFORE firing, then attaches the execution
        via :meth:`record_execution`. ``approve`` is retained for direct
        single-threaded callers/tests that advance state without a fire.
        """
        with self._lock:
            current = self._get_or_raise(proposal_id)
            self._reject_if_expired(current)
            self._require_state(current, "pending")

            updated = Proposal(
                **{
                    **_proposal_to_dict(current),
                    "state": "approved",
                    "approved_at": _iso(_utc_now()),
                    "approver_user_id": approver_user_id,
                    "size_override_pct": size_override_pct,
                    "execution": execution,
                }
            )
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
                **{
                    **_proposal_to_dict(current),
                    "state": "rejected",
                    "rejected_at": _iso(_utc_now()),
                    "rejection_reason": reason.strip(),
                }
            )
            self._persist(updated, event="reject")
            return updated

    def expire_one(self, proposal_id: str) -> Proposal | None:
        """Force a pending proposal to expired (TTL sweep). Returns None
        if already non-pending.

        Cross-process compare-and-set (TOCTOU fix): ``expire_one`` is driven by
        callers in a DIFFERENT process from the HITL approve flow — the cron TTL
        sweep (:meth:`sweep_expired`), ``quant_list_proposals``
        (:meth:`list_pending` -> :meth:`sweep_expired`), and the lazy-expire
        inside :meth:`get`. The pre-fix code did a non-atomic check-then-act: it
        read ``state`` on one connection, checked ``state == 'pending'``, then ran
        an UNCONDITIONAL ``ON CONFLICT DO UPDATE SET state='expired'`` in
        :meth:`_persist`. The in-process ``self._lock`` gives NO cross-process
        protection, so a sweeper that read a proposal as pending and then was
        preempted while an approve advanced it to ``approved`` (and FIRED the
        reactor) would resume from its STALE pending read and clobber the
        approved+fired row back to ``expired`` (``execution=None``) — a
        capital-moved position misrepresented as expired, and (since the JSONL
        'expire' event is appended LAST and ``_reconcile_index`` is
        last-event-per-id) the audit source-of-truth corrupted too.

        The fix mirrors the SQLite-index-as-cross-process-arbiter pattern used by
        ``daemon/halt_state`` and ``state/portfolio_state``: the transition is a
        guarded ``UPDATE ... WHERE proposal_id=? AND state='pending'`` inside a
        single ``BEGIN IMMEDIATE`` transaction. Exactly ONE contender wins
        (``rowcount == 1``); a concurrent approve/reject/expire that already
        advanced the row makes the CAS update 0 rows, and we return None WITHOUT
        appending an 'expire' audit line — so an expire can never overwrite a
        non-pending (approved/fired) row. Idempotent by construction.
        """
        with self._lock:
            try:
                current = self._get_or_raise(proposal_id)
            except KeyError:
                return None
            if current.state != "pending":
                return None
            updated = Proposal(
                **{
                    **_proposal_to_dict(current),
                    "state": "expired",
                    "expired_at": _iso(_utc_now()),
                }
            )
            # Run the guarded compare-and-set on the SQLite index FIRST (it is the
            # cross-process write-lock arbiter), and append the 'expire' audit
            # event to the JSONL bus ONLY when we win — mirroring the atomic
            # transition idiom (CAS-then-audit). If a concurrent approve/reject/
            # expire already advanced the row out of 'pending', the CAS updates 0
            # rows and we return None WITHOUT appending a phantom 'expire' line
            # (so neither the SQLite index nor the JSONL latest-event-per-id
            # reconcile can resurrect the row as expired). No clobber.
            won = self._cas_expire(proposal_id, updated)
            if not won:
                return None
            self._append_audit(updated, event="expire")
            return updated

    def _cas_expire(self, proposal_id: str, updated: Proposal) -> bool:
        """Guarded SQLite transition pending -> expired. Returns True iff this
        caller won the compare-and-set (the row was still 'pending').

        BEGIN IMMEDIATE takes the write lock up-front so two processes serialize
        here; the ``WHERE state='pending'`` guard means only the FIRST contender
        flips the row — every later contender (or a concurrent approve/reject
        that already advanced it) updates 0 rows and loses.
        """
        record = _proposal_to_dict(updated)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE proposals SET "
                    "  state = 'expired', "
                    "  expired_at = ?, "
                    "  record_json = ? "
                    "WHERE proposal_id = ? AND state = 'pending'",
                    (
                        updated.expired_at,
                        json.dumps(record, separators=(",", ":"), sort_keys=True),
                        proposal_id,
                    ),
                )
                won = cur.rowcount == 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                logger.warning(
                    "proposals: SQLite expire CAS failed for %s; JSONL is "
                    "authoritative — re-read will reconstruct via JSONL scan",
                    proposal_id,
                    exc_info=True,
                )
                raise
        return won

    def sweep_expired(self) -> int:
        """Sweep pending proposals whose expires_at is in the past.

        Per ADR-0015 §D9: idempotent, returns count expired this call.
        Safe to call on every read or on a cron tick.
        """
        now_iso = _iso(_utc_now())
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT proposal_id FROM proposals WHERE state = 'pending' AND expires_at <= ?",
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

    def get(self, proposal_id: str) -> Proposal | StoredMultiLegProposal | None:
        """Look up a proposal by id. None if not found.

        Lazy expiration: if the proposal is pending but past its TTL,
        the call advances it to expired before returning.

        B01 multi-leg routing: a ``proposal_kind == 'multi_leg'`` row is returned as
        a :class:`StoredMultiLegProposal` wrapper that (a) carries the lifecycle
        fields ``quant_approve`` reads (``state``/``expires_at``/``advisor_result``/
        ``proposal_kind``) and (b) RE-MINTS the inner ``MultiLegProposal`` via
        ``from_gate_result`` (so ``risk_gate_pass`` is the gate's verdict, never
        hand-set) and delegates every structural attribute the reactor reads to it.
        ``react.dispatch.select_reactor`` routes it to ``MultiLegPaperReactor`` (it
        carries ``proposal_kind == 'multi_leg'`` AND ``option_legs``/``strategy_kind``).
        The equity path is unchanged — an equity row returns the plain ``Proposal``.
        """
        with self._lock:
            try:
                current = self._get_or_raise(proposal_id)
            except KeyError:
                return None
            if current.state == "pending":
                if _iso_in_past(current.expires_at):
                    current = self.expire_one(proposal_id) or current
            if current.proposal_kind == "multi_leg" and current.multi_leg is not None:
                # Re-mint + wrap so the reactor sees a real MultiLegProposal and
                # quant_approve sees the lifecycle fields. A malformed payload is
                # fail-closed: a wrapper that the reactor will refuse (risk_gate_pass
                # is rebuilt from the gate verdict — a rejected/garbled payload never
                # mints a passing proposal).
                return StoredMultiLegProposal.from_record(current)
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
    # Index reconciliation (JSONL is authoritative; rebuild SQLite)
    # -----------------------------------------------------------------

    def _reconcile_index(self) -> int:
        """Rebuild the SQLite index by replaying the JSONL from disk.

        The JSONL bus is the source of truth (append-only event log). If the
        SQLite index drifts — e.g. a crash between the JSONL append and the
        SQLite upsert in ``_persist``, or the .db file was deleted — this
        replays every event in file order and reduces to the latest record
        per ``proposal_id`` (the JSONL is append-only, so the last write for a
        given id wins).

        Money-software discipline (AGENTS.md §1 silence-by-default):
        idempotent, and tolerant of a partial/corrupt trailing line — a daily
        run must NEVER crash on one bad line. Malformed lines are logged and
        skipped; the rest of the file still reconciles. Reconciliation runs
        inside a single ``BEGIN IMMEDIATE`` transaction so a concurrent reader
        never observes a half-rebuilt index.

        Returns:
            Number of distinct proposals written into the index.
        """
        with self._lock:
            latest: dict[str, Proposal] = {}
            if self.bus_path.exists():
                # Read bytes and split on newline so a partial trailing line
                # (no terminating "\n", e.g. a crash mid-write) is handled by
                # the same json-decode skip as any other corrupt line.
                raw = self.bus_path.read_bytes()
                # Index of the LAST non-empty line: a malformed line THERE is the
                # benign writer-crash-mid-write case (tolerate + skip). A malformed
                # line ANYWHERE EARLIER is mid-file corruption that could drop a
                # terminal event (approve/reject/expire) and silently resurrect a
                # closed HITL proposal as `pending` — that must FAIL LOUD, not skip
                # (Codex Facet-2 P2). Money-software: never misrepresent HITL state.
                _lines = raw.split(b"\n")
                _last_nonempty = max(
                    (i for i, ln in enumerate(_lines) if ln.strip()), default=-1
                )
                for lineno, line in enumerate(_lines, start=1):
                    if not line.strip():
                        continue  # blank / trailing newline
                    try:
                        proposal = _proposal_from_json(line.decode("utf-8"))
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        KeyError,
                        TypeError,
                    ) as e:
                        is_trailing = (lineno - 1) == _last_nonempty
                        if is_trailing:
                            # Benign: a partial trailing write (writer crashed
                            # mid-line). Log + skip; the rest reconciles cleanly.
                            logger.warning(
                                "proposals: skipping malformed TRAILING JSONL line "
                                "%d in %s during index reconciliation: %s",
                                lineno,
                                self.bus_path,
                                e,
                            )
                            continue
                        # Mid-file corruption — a terminal event may be lost,
                        # which could make a closed proposal look approvable.
                        # Fail loud rather than silently resurrect it.
                        raise ProposalLogCorruptionError(
                            f"malformed non-trailing JSONL line {lineno} in "
                            f"{self.bus_path} during index reconciliation: {e} — "
                            "refusing to rebuild the index from a corrupt log "
                            "(a terminal approve/reject/expire event may be lost). "
                            "Inspect/repair the log before retrying."
                        ) from e
                    # Append-only log: later events for the same id supersede
                    # earlier ones (pending -> approved/rejected/expired).
                    latest[proposal.proposal_id] = proposal

            # Rebuild the index atomically: a single immediate transaction so a
            # concurrent reader sees either the old or the fully-rebuilt index,
            # never a partial one.
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM proposals")
                    for proposal in latest.values():
                        record = _proposal_to_dict(proposal)
                        conn.execute(
                            """
                            INSERT INTO proposals
                              (proposal_id, state, symbol, asset_class, timeframe,
                               created_at, expires_at, approved_at, rejected_at,
                               expired_at, record_json, proposal_kind)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                                json.dumps(
                                    record, separators=(",", ":"), sort_keys=True
                                ),
                                proposal.proposal_kind,
                            ),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    logger.warning(
                        "proposals: SQLite index reconciliation failed for %s; "
                        "index left unchanged (JSONL remains authoritative)",
                        self.bus_path,
                        exc_info=True,
                    )
                    raise
            return len(latest)

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
                f"proposal {proposal.proposal_id} is in state {proposal.state!r}; expected {want!r}"
            )

    def _reject_if_expired(self, proposal: Proposal) -> None:
        """If the proposal is pending but its TTL has elapsed, expire it."""
        if proposal.state == "pending" and _iso_in_past(proposal.expires_at):
            self.expire_one(proposal.proposal_id)
            raise ProposalExpiredError(
                f"proposal {proposal.proposal_id} expired at {proposal.expires_at}"
            )

    def _append_audit(self, proposal: Proposal, *, event: str) -> None:
        """Append one event line to the JSONL bus (the source of truth) and
        emit the create-time governance event.

        This is the JSONL half of :meth:`_persist`, factored out so the guarded
        compare-and-set paths — the ar16 atomic-claim (:meth:`claim_for_approval`)
        and the TTL-sweep (:meth:`expire_one`) — can write their audit line
        WITHOUT re-running the unconditional SQLite upsert. Each already flipped
        the SQLite row inside its own ``BEGIN IMMEDIATE`` compare-and-set, so a
        second unconditional upsert here would be redundant and would race (or
        defeat) the very window/guard we just closed. The append-only JSONL is
        latest-event-per-id, so the event lands durably for ``_reconcile_index``.
        """
        record = _proposal_to_dict(proposal)
        record["_event"] = event  # "create" | "approve" | "reject" | "expire"
        record["_event_at"] = _iso(_utc_now())

        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        with append_locked(self.bus_path) as fd:
            import os as _os

            _os.write(fd, line.encode("utf-8"))

        # Wave A wiring (ADR-0031 D2): emit proposal_emitted governance audit
        # event on the create transition. Failures NEVER block the proposal
        # write (silence-by-default observation).
        if event == "create":
            try:
                from hermes_quant.governance import audit_log

                advisor = proposal.advisor_result or {}
                aggregated = advisor.get("aggregated_signal") or {}
                direction = aggregated.get("direction") or advisor.get("direction") or 0
                target_size = (
                    advisor.get("target_size_pct_nav")
                    or advisor.get("kelly_size")
                    or aggregated.get("target_size_pct_nav")
                    or 0.0
                )
                asof_str = advisor.get("as_of") or aggregated.get("as_of") or proposal.created_at
                # Use creation-time for governance asof — that's when this
                # proposal landed on the bus, which is the auditable event.
                asof_dt = _utc_now()
                audit_log.append(
                    audit_log.GovernanceEvent(
                        kind="proposal_emitted",
                        asof=asof_dt,
                        source="proposals.create",
                        payload={
                            "proposal_id": proposal.proposal_id,
                            "asset": proposal.symbol,
                            "asset_class": proposal.asset_class,
                            "timeframe": proposal.timeframe,
                            "direction": int(direction),
                            "target_size_pct_nav": float(target_size),
                            "asof": asof_str,
                            "created_at": proposal.created_at,
                            "expires_at": proposal.expires_at,
                        },
                    )
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "audit_log.append failed for proposal %s: %s",
                    proposal.proposal_id,
                    e,
                )

    def _persist(self, proposal: Proposal, *, event: str) -> None:
        """Atomic dual-write: JSONL append (truth), SQLite upsert (index).

        JSONL is written first; if SQLite write fails, the index can be
        rebuilt from JSONL via _reconcile_index() (replays the append-only
        log, latest-event-per-id wins, tolerant of corrupt trailing lines).

        NOTE: this performs an UNCONDITIONAL upsert and is used by the
        single-state-machine transitions (create / approve / reject) that hold
        the in-process lock and read+write a single proposal. The TTL-sweep
        ``expire_one`` path, which is driven cross-process, does NOT use this —
        it runs a guarded compare-and-set via :meth:`_cas_expire` so a stale
        sweeper read can never clobber a concurrently-approved row.
        """
        # JSONL append (the source of truth) + create-time governance event.
        self._append_audit(proposal, event=event)

        record = _proposal_to_dict(proposal)
        record["_event"] = event
        record["_event_at"] = _iso(_utc_now())

        # SQLite upsert
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO proposals
                      (proposal_id, state, symbol, asset_class, timeframe,
                       created_at, expires_at, approved_at, rejected_at,
                       expired_at, record_json, proposal_kind)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(proposal_id) DO UPDATE SET
                      state = excluded.state,
                      expires_at = excluded.expires_at,
                      approved_at = excluded.approved_at,
                      rejected_at = excluded.rejected_at,
                      expired_at = excluded.expired_at,
                      record_json = excluded.record_json,
                      proposal_kind = excluded.proposal_kind
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
                        proposal.proposal_kind,
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


class ProposalLogCorruptionError(Exception):
    """The proposal JSONL bus is corrupt mid-file during index reconciliation.

    Raised by ``ProposalStore._reconcile_index`` when a malformed line is found
    that is NOT the trailing line. A trailing partial write (writer crashed
    mid-line) is benign and tolerated; a malformed line earlier in the log could
    drop a terminal event (approve/reject/expire) and silently resurrect a closed
    HITL proposal as ``pending`` — so we fail loud and refuse to rebuild the index
    from a corrupt log, rather than misrepresent HITL state. Inspect/repair first.
    """


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    """ISO 8601 UTC with seconds precision and `Z` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_in_past(iso_str: str) -> bool:
    """True if the parsed ISO timestamp is < now-UTC."""
    try:
        ts = datetime.strptime(iso_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        ts = ts.replace(tzinfo=UTC)
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
    """Convert Proposal dataclass to plain dict for serialization.

    B01 byte-identity rail: ``proposal_kind`` / ``multi_leg`` are emitted ONLY for
    a multi-leg proposal. An equity proposal (the default ``proposal_kind=='equity'``
    with ``multi_leg is None``) produces the EXACT pre-B01 dict — no new keys — so
    its JSONL/SQLite ``record_json`` bytes are unchanged (the equity path stays
    byte-identical, per the wave rail).
    """
    d = {
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
    # Additive-only: include the multi-leg discriminator + payload ONLY when this is
    # actually a multi-leg proposal. Omitting them on the equity path keeps the
    # serialized record byte-identical to the pre-B01 shape.
    if p.proposal_kind != "equity" or p.multi_leg is not None:
        d["proposal_kind"] = p.proposal_kind
        d["multi_leg"] = p.multi_leg
    return d


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
        # B01: default to the equity discriminator for any pre-B01 record (no
        # `proposal_kind` key) so old equity rows reconstruct unchanged.
        proposal_kind=d.get("proposal_kind", "equity"),
        multi_leg=d.get("multi_leg"),
    )


# ---------------------------------------------------------------------------
# B01 multi-leg payload (de)serialization + re-mint
# ---------------------------------------------------------------------------
#
# DESIGN (the #38 constructor-lock at the persistence boundary):
#   We persist the OptionsGateResult FIELDS (not a flag) plus the leg/structure
#   shape, and on read we REBUILD the OptionsGateResult and re-run
#   MultiLegProposal.from_gate_result. So ``risk_gate_pass`` is NEVER serialized as
#   an authoritative truth value we trust on read — it is ALWAYS recomputed as
#   ``gate_result.admitted`` copied by the blessed mint seam. A tampered/garbled
#   payload cannot resurrect a passing verdict: the worst case is a malformed
#   OptionsGateResult that mints a *rejected* (or unparseable -> raises) proposal,
#   which the reactor refuses. Fail-closed, by construction.


def _greeks_to_dict(g: Any) -> dict[str, Any] | None:
    if g is None:
        return None
    return {
        "delta": g.delta,
        "gamma": g.gamma,
        "theta": g.theta,
        "vega": g.vega,
        "rho": g.rho,
        "iv": g.iv,
        "iv_source": g.iv_source,
    }


def _multi_leg_to_dict(
    proposal: MultiLegProposal, gate_result: OptionsGateResult
) -> dict[str, Any]:
    """Serialize a minted MultiLegProposal + its OptionsGateResult to a JSON-safe
    payload. Money is stored as ``str`` (Decimal round-trips exactly via str)."""
    ng = gate_result.net_greeks
    return {
        "schema_version": 1,
        "strategy_kind": proposal.strategy_kind,
        "underlying": proposal.underlying,
        "asof": _iso(proposal.asof),
        "outer_qty": int(proposal.outer_qty),
        "net_debit_credit": str(proposal.net_debit_credit),
        "max_gain": (None if proposal.max_gain is None else str(proposal.max_gain)),
        "breakeven_underlying": [str(b) for b in proposal.breakeven_underlying],
        "rationale": proposal.rationale,
        "source_recipe_id": proposal.source_recipe_id,
        "option_legs": [
            {
                "symbol": leg.symbol,
                "side": leg.side,
                "position_intent": leg.position_intent,
                "ratio_qty": int(leg.ratio_qty),
                "greeks_at_decision": _greeks_to_dict(leg.greeks_at_decision),
                "fill_price": leg.fill_price,
            }
            for leg in proposal.option_legs
        ],
        "stock_leg": (
            None
            if proposal.stock_leg is None
            else {
                "underlying": proposal.stock_leg.underlying,
                "qty": int(proposal.stock_leg.qty),
                "basis_per_share": proposal.stock_leg.basis_per_share,
            }
        ),
        # The gate RESULT fields — the source of truth for the re-mint.
        "gate_result": {
            "admitted": bool(gate_result.admitted),
            "bucket": gate_result.bucket.value,
            "reason": gate_result.reason,
            "net_greeks": {
                "delta": ng.delta,
                "gamma": ng.gamma,
                "theta": ng.theta,
                "vega": ng.vega,
                "rho": ng.rho,
            },
            "bpr_estimate": float(gate_result.bpr_estimate),
            "max_loss": (
                None if gate_result.max_loss is None else float(gate_result.max_loss)
            ),
            "contracts": int(gate_result.contracts),
            "warnings": list(gate_result.warnings),
        },
    }


def _rebuild_gate_result(d: dict[str, Any]) -> OptionsGateResult:
    """Rebuild an OptionsGateResult from the persisted gate-result fields."""
    from hermes_quant.options.data import NetGreeks
    from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket

    ng = d["net_greeks"]
    return OptionsGateResult(
        admitted=bool(d["admitted"]),
        bucket=StructureBucket(d["bucket"]),
        reason=d.get("reason"),
        net_greeks=NetGreeks(
            delta=ng.get("delta", 0.0),
            gamma=ng.get("gamma", 0.0),
            theta=ng.get("theta", 0.0),
            vega=ng.get("vega", 0.0),
            rho=ng.get("rho", 0.0),
        ),
        bpr_estimate=float(d["bpr_estimate"]),
        max_loss=(None if d.get("max_loss") is None else float(d["max_loss"])),
        contracts=int(d.get("contracts", 0)),
        warnings=tuple(d.get("warnings", ())),
    )


def _multi_leg_from_dict(d: dict[str, Any]) -> MultiLegProposal:
    """Re-mint a MultiLegProposal from a persisted payload via from_gate_result.

    This is the ONLY read-path that produces a MultiLegProposal, and it ALWAYS goes
    through the blessed mint seam — so ``risk_gate_pass`` is the gate's verdict, never
    a value we trust off disk. ``proposal_id`` is supplied by the caller (the store
    key), keeping it authoritative even if the payload were tampered.
    """
    from decimal import Decimal

    from hermes_quant.options.data import OptionGreeksSnapshot, OptionLeg, StockLeg
    from hermes_quant.options.multileg import MultiLegProposal

    def _snap(g: dict[str, Any] | None) -> OptionGreeksSnapshot | None:
        if g is None:
            return None
        return OptionGreeksSnapshot(
            delta=g.get("delta"),
            gamma=g.get("gamma"),
            theta=g.get("theta"),
            vega=g.get("vega"),
            rho=g.get("rho"),
            iv=g.get("iv"),
            iv_source=g.get("iv_source"),
        )

    option_legs = tuple(
        OptionLeg(
            symbol=leg["symbol"],
            side=leg["side"],
            position_intent=leg["position_intent"],
            ratio_qty=int(leg.get("ratio_qty", 1)),
            greeks_at_decision=_snap(leg.get("greeks_at_decision")),
            fill_price=leg.get("fill_price"),
        )
        for leg in d["option_legs"]
    )
    sl = d.get("stock_leg")
    stock_leg = (
        None
        if sl is None
        else StockLeg(
            underlying=sl["underlying"],
            qty=int(sl["qty"]),
            basis_per_share=sl.get("basis_per_share"),
        )
    )
    gate_result = _rebuild_gate_result(d["gate_result"])
    return MultiLegProposal.from_gate_result(
        gate_result=gate_result,
        proposal_id=d["proposal_id"],
        asof=datetime.strptime(d["asof"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=UTC
        ),
        strategy_kind=d["strategy_kind"],
        underlying=d["underlying"],
        option_legs=option_legs,
        stock_leg=stock_leg,
        outer_qty=int(d["outer_qty"]),
        net_debit_credit=Decimal(str(d["net_debit_credit"])),
        max_gain=(None if d.get("max_gain") is None else Decimal(str(d["max_gain"]))),
        breakeven_underlying=tuple(
            Decimal(str(b)) for b in d.get("breakeven_underlying", ())
        ),
        rationale=d.get("rationale", ""),
        source_recipe_id=d.get("source_recipe_id", ""),
    )


class StoredMultiLegProposal:
    """Read-path wrapper for a persisted multi-leg proposal (B01).

    Carries BOTH faces a multi-leg proposal needs after ``store.get()``:

      * the HITL lifecycle fields ``quant_approve`` reads — ``state``,
        ``expires_at``, ``created_at``, ``advisor_result``, ``proposal_kind`` (='multi_leg'),
        ``symbol``/``asset_class``/``timeframe`` — taken from the persisted ``Proposal`` row;
      * the structural ``MultiLegProposal`` the reactor consumes — re-minted via
        ``from_gate_result`` and exposed by delegating every other attribute to it
        (``risk_gate_pass``, ``option_legs``, ``strategy_kind``, ``net_debit_credit``,
        ``net_greeks``, ``underlying``, ``outer_qty``, ``asof``, etc.).

    ``react.dispatch.select_reactor`` routes it to ``MultiLegPaperReactor`` because it
    exposes ``proposal_kind == 'multi_leg'`` AND ``option_legs``/``strategy_kind``. The
    lifecycle attributes are set explicitly on the instance, so ``__getattr__`` (which
    only fires for *missing* attributes) cleanly delegates the rest to the inner
    proposal without shadowing the lifecycle fields. No reactor change is needed.
    """

    proposal_kind = "multi_leg"

    def __init__(self, record: Proposal, inner: MultiLegProposal) -> None:
        self.proposal_id = record.proposal_id
        self.state = record.state
        self.symbol = record.symbol
        self.asset_class = record.asset_class
        self.timeframe = record.timeframe
        self.created_at = record.created_at
        self.expires_at = record.expires_at
        self.advisor_result = record.advisor_result
        self.approved_at = record.approved_at
        self.approver_user_id = record.approver_user_id
        self.size_override_pct = record.size_override_pct
        self.rejected_at = record.rejected_at
        self.rejection_reason = record.rejection_reason
        self.expired_at = record.expired_at
        self.execution = record.execution
        self.multi_leg = record.multi_leg
        # The re-minted structural proposal the reactor consumes.
        self.proposal = inner

    @classmethod
    def from_record(cls, record: Proposal) -> StoredMultiLegProposal:
        if record.multi_leg is None:
            raise ValueError(
                f"proposal {record.proposal_id} is multi_leg but has no payload"
            )
        # proposal_id authoritative from the row (not the payload).
        payload = dict(record.multi_leg)
        payload["proposal_id"] = record.proposal_id
        inner = _multi_leg_from_dict(payload)
        return cls(record, inner)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes NOT set on the instance (the lifecycle fields
        # above) and NOT on the class — i.e. the structural MultiLegProposal surface
        # the reactor reads. Delegate to the inner proposal.
        try:
            inner = object.__getattribute__(self, "proposal")
        except AttributeError as exc:  # pragma: no cover - during __init__ only
            raise AttributeError(name) from exc
        return getattr(inner, name)


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
