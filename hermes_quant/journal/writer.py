"""hermes_quant.journal.writer — Atomic-rename markdown journal writer.

Per ADR-0010 §Decision §4: write `.tmp` → fsync file → rename → fsync
parent dir. Crash-safe.
Per §8: Pydantic-only mutator surface; markdown is a render derivative.

Locking: a single mutex on the file path so concurrent in-process writes
serialize. Cross-process safety is handled via the same flock pattern
the signal_bus uses (atomic rename is itself a hostile-thread-safe op
on POSIX, but flock prevents two writers materializing different .tmp
contents from racing the rename).
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import AnalystComponent, Reflection, SettlementEntry
from .reader import parse_journal
from .render import ENTRY_DELIM, JOURNAL_HEADER, render_journal

logger = logging.getLogger(__name__)


DEFAULT_JOURNAL_PATH = (
    Path(os.environ.get("HERMES_QUANT_JOURNAL_PATH", ""))
    if os.environ.get("HERMES_QUANT_JOURNAL_PATH")
    else Path.home() / ".hermes" / "quant" / "journal.md"
)


# Per-path in-process mutex. Cross-process via flock on the .lock sidecar.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_GLOBAL_LOCK = threading.Lock()


def _get_path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _GLOBAL_LOCK:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _flocked(path: Path) -> Iterator[None]:
    """Acquire flock on the journal's `.lock` sidecar for cross-process
    serialization. Released on exit."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JournalEntryNotFound(KeyError):
    """No entry with the requested entry_id exists in the journal."""


class JournalEntryAlreadyResolved(ValueError):
    """The entry exists but has already been resolved (Phase B applied)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_pending(
    entry: SettlementEntry,
    *,
    path: Path | None = None,
) -> None:
    """Append a Phase-A pending entry to the journal.

    Per ADR-0010 §Lifecycle Phase A. Atomic rename: writes `.tmp`,
    fsyncs, renames. If the entry's id already exists in the journal,
    raises ValueError (idempotency contract — pending entries are unique).

    Concurrent writers serialize via per-path RLock + flock on `.lock`.
    """
    target = path or DEFAULT_JOURNAL_PATH
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    if entry.is_resolved():
        raise ValueError(
            f"append_pending requires a Phase-A entry; entry {entry.entry_id} is already resolved"
        )

    with _get_path_lock(target):
        with _flocked(target):
            entries = _load_entries_safe(target)
            for existing in entries:
                if existing.entry_id == entry.entry_id:
                    raise ValueError(
                        f"entry_id {entry.entry_id!r} already in journal; duplicate append_pending"
                    )
            entries.append(entry)
            _atomic_write(target, render_journal(entries))


def resolve(
    entry_id: str,
    *,
    asof_settlement: datetime,
    exit_price: float,
    raw_return: float,
    alpha_return: float,
    hold_minutes: int,
    reflection: Reflection,
    path: Path | None = None,
) -> SettlementEntry:
    """Patch a pending entry with its Phase-B realized outcome.

    Per ADR-0010 §Lifecycle Phase B. Returns the resolved entry.

    Raises:
        JournalEntryNotFound: no entry with this id.
        JournalEntryAlreadyResolved: entry exists but Phase-B already applied.
    """
    target = path or DEFAULT_JOURNAL_PATH
    target = Path(target)

    with _get_path_lock(target):
        with _flocked(target):
            entries = _load_entries_safe(target)
            target_idx = -1
            for idx, e in enumerate(entries):
                if e.entry_id == entry_id:
                    target_idx = idx
                    break
            if target_idx < 0:
                raise JournalEntryNotFound(entry_id)
            existing = entries[target_idx]
            if existing.is_resolved():
                raise JournalEntryAlreadyResolved(entry_id)

            # Patch
            data = _entry_to_dict(existing)
            data.update(
                {
                    "asof_settlement": asof_settlement,
                    "exit_price": exit_price,
                    "raw_return": raw_return,
                    "alpha_return": alpha_return,
                    "hold_minutes": hold_minutes,
                    "reflection": reflection,
                }
            )
            patched = SettlementEntry.from_dict(data)
            entries[target_idx] = patched
            _atomic_write(target, render_journal(entries))
            return patched


def append_human_override(
    proposal: object,  # hermes_quant.proposals.Proposal
    *,
    kind: str,  # "approve" | "reject" | "expire"
    reason: str | None = None,
    path: Path | None = None,
) -> SettlementEntry:
    """HITL Wave A integration: render a proposal lifecycle event into the
    journal even when the daemon isn't running.

    Per ADR-0015 §D8: rejection events become journal entries so the
    calibrator can learn from them at the next settlement tick. Approval
    events also land here so the operator's audit trail is complete in
    one file.

    The rendered entry has `hitl_kind` set so consumers (advisor's
    `get_recent_lessons`, future LLMAnalyst RAG) can distinguish between
    autonomous-daemon decisions and HITL operator-driven decisions.
    """
    if kind not in {"approve", "reject", "expire"}:
        raise ValueError(f"kind must be approve|reject|expire, got {kind!r}")

    advisor_result = getattr(proposal, "advisor_result", None) or {}
    sig = advisor_result.get("aggregated_signal") or {}
    rg = advisor_result.get("risk_gate") or {}
    components_dicts = advisor_result.get("analyst_views") or []

    components = [
        AnalystComponent(
            analyst=c.get("analyst", "unknown"),
            direction=int(c.get("direction", 0)),
            confidence=float(c.get("confidence", 0.0)),
            weight=1.0 / max(1, len(components_dicts)),
        )
        for c in components_dicts
    ]

    decision_price = float(advisor_result.get("decision_price") or 0.0)
    # jw1: tolerate a MultiLegProposal (asset_class/symbol absent; carries `underlying`).
    # An equity Proposal has both fields, so this is byte-identical for it; a multi-leg
    # close/origination journals as asset_class='multi_leg', symbol=underlying instead of
    # raising AttributeError into the swallowing BLE001 (which silently dropped the audit
    # entry on EVERY autonomous options fire — the ADR-0029 evidence trail).
    _asset_class = getattr(proposal, "asset_class", None) or "multi_leg"
    _symbol = getattr(proposal, "symbol", None) or getattr(proposal, "underlying", "") or ""
    benchmark_symbol = _benchmark_for(_asset_class)

    entry = SettlementEntry(
        entry_id=proposal.proposal_id,
        # ADR-0068: prefer wall-clock `decision_wall_clock` (truthful "when did
        # the model run"), fall back to `as_of` (= bar_ts, replay anchor) for
        # advisor-result dicts produced before the ADR-0068 split.
        asof_decision=(
            _parse_iso_safe(advisor_result.get("decision_wall_clock"))
            or _parse_iso_safe(advisor_result.get("as_of"))
            or _utc_now()
        ),
        symbol=_symbol,
        asset_class=_asset_class,
        direction=int(sig.get("direction", 0)),
        confidence=float(sig.get("confidence", 0.0)),
        target_position_pct=float(rg.get("kelly_fraction", 0.0)),
        decision_price=decision_price,
        benchmark_symbol=benchmark_symbol,
        per_analyst_components=components,
        reason=_hitl_reason_text(kind, reason, sig, rg),
        hitl_kind=kind,
        hitl_reason=reason,
        hitl_approver=getattr(proposal, "approver_user_id", None),
    )

    target = path or DEFAULT_JOURNAL_PATH
    target = Path(target)

    with _get_path_lock(target):
        with _flocked(target):
            entries = _load_entries_safe(target)
            # Idempotent: if the same proposal_id is already in the journal,
            # update it (operator might approve then later we'd resolve it
            # at settlement; we want one entry per proposal not two).
            existing_idx = -1
            for idx, e in enumerate(entries):
                if e.entry_id == entry.entry_id:
                    existing_idx = idx
                    break
            if existing_idx >= 0:
                entries[existing_idx] = entry
            else:
                entries.append(entry)
            _atomic_write(target, render_journal(entries))
            return entry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write(target: Path, content: str) -> None:
    """Write content to target.tmp, fsync file, rename, fsync parent dir.

    Crash-safe. This is the ADR-0010 settlement ledger (module docstring §1-4):
    settlement_loop derives the always-on ADR-0016 kill-switch realized-P&L
    basis from these entries, so a lost append/resolve understates the drawdown
    and the rail fails OPEN. POSIX rename(2) only guarantees the new directory
    entry survives a crash AFTER the CONTAINING DIRECTORY is itself fsync'd —
    fsyncing only the file data leaves a window after os.replace in which a
    power-loss can revert the rename. So we fsync the parent dir too (best
    effort; mirrors the sibling durable writer governance/kill_switch).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    # Atomic rename. On POSIX this is hostile-thread-safe.
    os.replace(tmp, target)
    # Parent-dir fsync so the rename itself survives a crash.
    try:
        dfd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        logger.warning(
            "journal: parent-dir fsync failed for %s; rename may not be "
            "crash-durable",
            target.parent,
        )


def _load_entries_safe(target: Path) -> list[SettlementEntry]:
    """Read existing entries; tolerate a missing, empty, or partially-corrupt
    file WITHOUT discarding the entries that still parse.

    Per ADR-0010 this is the settlement ledger: silently dropping prior
    PENDING entries is a data-loss defect (ar22). A torn write — an
    invalid-UTF-8 tail, a truncated mid-entry block, or one mangled meta
    line — must recover every entry whose META_BEGIN/META_END block is
    still intact. ``parse_journal`` is already block-by-block tolerant; the
    only thing that defeats it is ``read_text(encoding="utf-8")`` raising a
    ``UnicodeDecodeError`` on the bad bytes *before* the parser ever runs.

    So we read bytes and decode with ``errors="replace"``: the corrupt tail
    becomes replacement chars (which fail to form a valid meta block and are
    skipped), while the valid entries ahead of it survive. We still keep a
    ``.bak`` safety copy when bytes were undecodable — but we COPY rather
    than rename, so the live ledger keeps the recovered entries and the next
    atomic write rewrites a clean file. Only a hard read error (the bytes
    themselves are unreadable) falls back to ``[]``.
    """
    if not target.exists():
        return []
    try:
        raw = target.read_bytes()
    except OSError as exc:
        logger.warning(
            "journal: could not read %s (%s); treating as empty without "
            "touching the file",
            target,
            exc,
        )
        return []

    if not raw.strip():
        return []

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Torn write: salvage every entry whose block is still valid UTF-8
        # rather than discarding the whole ledger.
        logger.warning(
            "journal: %s has undecodable bytes (%s); recovering parseable "
            "entries and backing up the corrupt file as %s.bak",
            target,
            exc,
            target.name,
        )
        text = raw.decode("utf-8", errors="replace")
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            backup.write_bytes(raw)  # copy, don't rename — keep the live file
        except OSError:
            logger.warning("journal: backup copy failed; continuing recovery")

    try:
        return parse_journal(text)
    except Exception as exc:  # noqa: BLE001 — defensive; parse_journal is tolerant
        logger.warning(
            "journal: parse failed for %s — %s. Backing up as %s.bak and "
            "starting fresh.",
            target,
            exc,
            target.name,
        )
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            if not backup.exists():
                backup.write_bytes(raw)
        except OSError:
            logger.warning("journal: backup copy also failed; skipping")
        return []


def _entry_to_dict(e: SettlementEntry) -> dict:
    """Pydantic.model_dump or dataclass asdict; tolerant of either backing."""
    if hasattr(e, "model_dump"):
        return e.model_dump()
    from dataclasses import asdict

    return asdict(e)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso_safe(s: object) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if not isinstance(s, str):
        return None
    try:
        # Tolerate "...Z" suffix
        s_norm = s.rstrip("Z")
        # Try fromisoformat first (3.11+)
        try:
            dt = datetime.fromisoformat(s_norm)
        except ValueError:
            dt = datetime.strptime(s_norm, "%Y-%m-%dT%H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _benchmark_for(asset_class: str) -> str:
    return {
        "equity": "SPY",
        "etf": "SPY",
        "crypto": "BTC/USDT",
        "fx": "DXY",
        "futures": "ES=F",
    }.get(asset_class, "SPY")


def _hitl_reason_text(
    kind: str,
    reason: str | None,
    sig: dict,
    rg: dict,
) -> str:
    """Compose a human-readable reason field for the journal."""
    direction = int(sig.get("direction", 0))
    conf = float(sig.get("confidence", 0.0))
    kelly = float(rg.get("kelly_fraction", 0.0))
    base = f"HITL {kind} — direction={direction:+d}, confidence={conf:.2f}, kelly={kelly:+.4f}"
    if reason:
        base += f". Operator note: {reason}"
    return base
