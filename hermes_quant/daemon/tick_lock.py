"""hermes_quant.daemon.tick_lock — per-symbol fire-path serialization lock.

Closes the read-modify-write (TOCTOU) race on the money ledger that the
ra10 / ADR-0078 incident analysis pinned as the 880%-gross mechanism.

The race
--------
The fire sequence is:

  1. reconstruct_portfolio_state() reads executions.jsonl (the book).
  2. decide / size against that book.
  3. PaperReactor.execute() flock-appends one line to executions.jsonl.
  4. PaperReactor.execute() updates state.db.

``signal_bus.append_locked`` (flock on the bus) serializes step (3) and
``portfolio_state`` BEGIN IMMEDIATE serializes step (4) — but NOTHING is held
across (1)->(4). Two armed crons (autonomous-tick + playbook-tick) can BOTH
reconstruct the same pre-fire book, BOTH decide to fire the same symbol, and
BOTH append — a classic read-decide-then-write race that the per-write locks
cannot see. The ``processed_fills`` idempotency table dedups an EXACT re-apply
of one proposal, but two DISTINCT proposals racing on one symbol have distinct
keys and both land.

The fix
-------
A per-``(account, asset_class, symbol)`` advisory file lock acquired BEFORE the
read-decide and held THROUGH the state.db update. It serializes the whole
sequence per symbol while letting DIFFERENT symbols proceed in parallel (one
lock file per symbol triple). This is purely additive: the existing flock on
the bus and the BEGIN IMMEDIATE / processed_fills guard stay intact and keep
doing their jobs underneath.

Safety model (NON-NEGOTIABLE — a deadlocking lock is worse than the race)
-------------------------------------------------------------------------
* FAIL-OPEN-SAFE: if the lock file cannot even be opened/created (e.g. a
  filesystem with no flock support, a permission error), we DEGRADE to today's
  behavior — yield with ``acquired=False`` and a WARNING. The caller proceeds
  exactly as it does today (the race re-opens, but the tick never hangs or
  crashes). Failing open on an *infrastructure* error is the documented posture
  (silence-by-default; never block money software on a transient fs error).
* NON-BLOCKING with a short timeout: acquisition uses non-blocking ``flock``
  polled up to ``timeout_s``. A second writer that cannot win the lock within
  the timeout gets ``acquired=False`` with ``contended=True`` so it can SKIP
  the symbol THIS tick — it neither blocks forever nor double-fires.
* The lock NEVER deadlocks: every acquired lock is released in ``finally``; an
  un-acquired attempt holds nothing.

WSL / 9p caveat (operator memory)
---------------------------------
The repo lives on a 9p/drvfs mount (/mnt/e) where ``fcntl.flock`` works but the
dirent cache can desync. Live runtime state is on a real Linux fs at
``~/.hermes/quant`` (``QUANT_HOME``). The lock file therefore lives alongside
the live ledger under ``QUANT_HOME/locks/`` — NOT on the 9p mount — so the lock
and the ledger it guards share one filesystem's coherency domain.

``QUANT_HOME`` is resolved at call time (not import time) and honors the
``HERMES_QUANT_HOME`` env override, so child processes in a multiprocessing
concurrency test (which do NOT inherit a monkeypatched module attribute) all
agree on the same lock directory as long as they agree on the env var.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import math
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Default lock-acquire timeout. Deliberately short: a tick that cannot win the
# per-symbol lock in this window SKIPS the symbol this tick rather than block.
# A whole tick budget is seconds; one symbol's lock contention must not eat it.
# Overridable via HERMES_QUANT_TICK_LOCK_TIMEOUT_S (env) for tests / tuning.
DEFAULT_TIMEOUT_S = 2.0
_TIMEOUT_ENV = "HERMES_QUANT_TICK_LOCK_TIMEOUT_S"


def _default_timeout_s() -> float:
    """Resolve the acquire timeout, honoring the env override; fail-safe to default."""
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    # ar10: reject non-finite. `inf` (a `1e400` typo) otherwise makes
    # deadline = monotonic() + inf, so the poll loop's `monotonic() >= deadline`
    # is never True and a contended lock spin-polls FOREVER instead of returning
    # contended=True after the short timeout. Fall back to the finite default.
    return val if (math.isfinite(val) and val >= 0.0) else DEFAULT_TIMEOUT_S

# Poll interval while waiting on a contended lock. Small enough to be responsive,
# large enough not to busy-spin the CPU.
_POLL_INTERVAL_S = 0.02

# Characters allowed verbatim in a lock-file stem. Everything else is escaped so
# a symbol like "BRK/B" or "BTC/USDT" can never escape the locks/ directory.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _quant_home() -> Path:
    """Resolve QUANT_HOME at call time, honoring HERMES_QUANT_HOME.

    Resolved lazily (not at import) so a multiprocessing concurrency test — whose
    children do not inherit monkeypatched module globals — can point every process
    at one shared lock directory purely via the env var.
    """
    override = os.environ.get("HERMES_QUANT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "quant"


def locks_dir() -> Path:
    """Directory holding the per-symbol lock files (under the LIVE-fs QUANT_HOME)."""
    return _quant_home() / "locks"


def _safe_component(value: str) -> str:
    """Escape one path component so it is filesystem-safe and cannot traverse."""
    cleaned = _SAFE_CHARS.sub("_", value.strip()) if value else ""
    return cleaned or "_"


def lock_path_for(account_id: str, asset_class: str, symbol: str) -> Path:
    """Return the lock-file path for one (account, asset_class, symbol) triple.

    Per-symbol granularity: different symbols get different files, so they never
    block each other. The same triple always maps to the same file (so two crons
    firing the same symbol contend on ONE lock).
    """
    stem = (
        f"{_safe_component(account_id)}__"
        f"{_safe_component(asset_class)}__"
        f"{_safe_component(symbol)}.lock"
    )
    return locks_dir() / stem


@dataclass(frozen=True)
class TickLockResult:
    """Outcome of a tick-lock acquisition attempt.

    Attributes:
        acquired: True iff the exclusive lock is held for the duration of the
            ``with`` block. False means the caller did NOT get the lock.
        contended: True iff acquisition failed specifically because another
            holder kept the lock past ``timeout_s`` (the SKIP-this-symbol case).
            False on a fail-open infrastructure error.
        fail_open: True iff acquisition degraded to today's behavior because the
            lock file itself could not be opened (no flock / permission / etc.).
        reason: human-readable detail for logs / journaling.
        path: the lock-file path (or None if we never got that far).
    """

    acquired: bool
    contended: bool = False
    fail_open: bool = False
    reason: str = ""
    path: Path | None = None


@contextmanager
def symbol_tick_lock(
    account_id: str,
    asset_class: str,
    symbol: str,
    *,
    timeout_s: float | None = None,
) -> Iterator[TickLockResult]:
    """Hold an exclusive per-symbol lock across the read-decide-fire-store window.

    Acquire this BEFORE reconstructing the book and hold it THROUGH the state.db
    update so the whole sequence is serialized per symbol. Different symbols use
    different lock files and never block one another.

    Always yields a :class:`TickLockResult`; it NEVER raises for a lock failure.

    Three outcomes (all non-blocking past ``timeout_s``):
      * ``acquired=True``                  — exclusive lock held; release on exit.
      * ``acquired=False, contended=True`` — another writer holds it; caller
                                             should SKIP this symbol this tick.
      * ``acquired=False, fail_open=True`` — lock file unopenable; caller
                                             proceeds as today (degrade, log).

    Args:
        account_id: account partition (e.g. "paper-default").
        asset_class: e.g. "equity", "us_option", "crypto".
        symbol: the instrument (e.g. "AAPL").
        timeout_s: max seconds to wait for a contended lock before giving up.
            None (default) resolves DEFAULT_TIMEOUT_S, honoring the
            HERMES_QUANT_TICK_LOCK_TIMEOUT_S env override.
    """
    if timeout_s is None:
        timeout_s = _default_timeout_s()
    path = lock_path_for(account_id, asset_class, symbol)
    fd: int | None = None

    # --- open/create the lock file -------------------------------------------
    # A failure HERE is an infrastructure problem (no fs, no perms) -> FAIL OPEN.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning(
            "tick-lock: could not open lock file %s (%s); FAILING OPEN — "
            "proceeding without the per-symbol lock (race window re-opens, "
            "but the tick is not blocked)",
            path,
            exc,
        )
        yield TickLockResult(
            acquired=False,
            fail_open=True,
            reason=f"lock_open_failed: {type(exc).__name__}: {exc}",
            path=path,
        )
        return

    # --- non-blocking acquire, polled to a short deadline --------------------
    deadline = time.monotonic() + max(0.0, timeout_s)
    acquired = False
    fail_open = False
    fail_reason = ""
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                # Held by another process — keep polling until the deadline.
                if time.monotonic() >= deadline:
                    break
                time.sleep(_POLL_INTERVAL_S)
                continue
            # flock not supported by this filesystem (e.g. ENOTSUP / EINVAL on
            # some network mounts) -> infrastructure failure -> FAIL OPEN.
            fail_open = True
            fail_reason = f"flock_unsupported: {type(exc).__name__}: {exc}"
            break

    if not acquired:
        # Either contended-past-timeout or a fail-open flock error. In BOTH cases
        # we hold nothing; just close the fd and report.
        try:
            os.close(fd)
        except OSError:
            pass
        if fail_open:
            logger.warning(
                "tick-lock: flock unusable on %s (%s); FAILING OPEN", path, fail_reason
            )
            yield TickLockResult(
                acquired=False, fail_open=True, reason=fail_reason, path=path
            )
        else:
            logger.warning(
                "tick-lock: %s contended; could not acquire within %.3fs — "
                "SKIPPING this symbol this tick (no double-fire, no block)",
                path,
                timeout_s,
            )
            yield TickLockResult(
                acquired=False,
                contended=True,
                reason=f"contended_timeout_{timeout_s}s",
                path=path,
            )
        return

    # --- held: yield, then ALWAYS release in finally (never deadlock) --------
    try:
        yield TickLockResult(acquired=True, reason="acquired", path=path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def account_tick_lock(
    account_id: str,
    *,
    timeout_s: float | None = None,
) -> Iterator[TickLockResult]:
    """Hold an exclusive per-ACCOUNT lock across the read-decide-fire-store window.

    cr04 (2026-06-14): the per-SYMBOL :func:`symbol_tick_lock` serializes two crons
    firing the SAME symbol, but a per-account gross-exposure cap is read against the
    WHOLE-account book, so two DIFFERENT symbols racing both see the same pre-fire
    headroom and both pass the cap (TOCTOU — ADR-0091 named this non-atomicity as why
    Option B was rejected). This lock serializes ALL symbols on an account through the
    cap-read so the second fire sees the first's consumed headroom.

    It is a thin wrapper over :func:`symbol_tick_lock` with EMPTY asset_class/symbol:
    ``_safe_component("")`` collapses both to ``"_"`` so the triple routes to a stable
    per-account lock file (``<account>______.lock``) that is DISTINCT from every
    ``(account, asset_class, symbol)`` symbol lock. Zero new flock code — it inherits
    the exact fail-open-safe / non-blocking-timeout / release-in-``finally`` machinery.

    Acquire order (NON-NEGOTIABLE): callers wrap this OUTSIDE the per-symbol lock
    (account-outer / symbol-inner) so the acquire order is fixed and no deadlock cycle
    is possible (the two locks are different files).
    """
    with symbol_tick_lock(account_id, "", "", timeout_s=timeout_s) as result:
        yield result
