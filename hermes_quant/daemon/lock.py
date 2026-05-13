"""hermes_quant.daemon.lock — Singleton daemon lock with PID tracking.

Per ADR-0009 §P1-α: open WITHOUT O_TRUNC, acquire flock, THEN ftruncate(0)
+ write PID. The original implementation truncated before lock acquisition,
which racies if two daemons start simultaneously.

Lock file: ~/.hermes/quant/daemon-<account_id>.lock
- Holds an exclusive flock for the daemon's lifetime
- Contains the PID + start_time as text for human inspection
- Released via close() OR process death (kernel cleans up flock)

Singleton check from external readers (e.g., quant_status, quant_doctor):
read the file, parse the PID, check os.kill(pid, 0). If kill raises
ProcessLookupError, the lock is stale.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path

from hermes_quant.protocol import DaemonAlreadyRunning

logger = logging.getLogger(__name__)

DEFAULT_LOCK_DIR = Path.home() / ".hermes" / "quant"


class DaemonLock:
    """Singleton lock file for the daemon.

    Use as a context manager:

        with DaemonLock(account_id="alpaca-paper"):
            run_daemon_loop()

    On enter:
      - Open lock file with O_RDWR|O_CREAT (NOT O_TRUNC)
      - Acquire fcntl.flock LOCK_EX | LOCK_NB
      - On EWOULDBLOCK / EAGAIN: read existing PID; raise DaemonAlreadyRunning
      - On success: ftruncate(0) + write PID + start time

    On exit:
      - Close fd (flock releases automatically)
      - Optionally unlink the lock file (we keep it for post-mortem PID lookup)
    """

    def __init__(
        self,
        account_id: str = "default",
        lock_dir: Path = DEFAULT_LOCK_DIR,
    ):
        self.account_id = account_id
        self.lock_dir = lock_dir
        self.lock_path = lock_dir / f"daemon-{account_id}.lock"
        self._fd: int | None = None

    def __enter__(self) -> "DaemonLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    def acquire(self) -> None:
        """Per synthesis-v2 §P1-α ordering: open → flock → truncate → write."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # Open WITHOUT O_TRUNC
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                # Read existing PID for the error message
                existing = self._read_existing_pid()
                raise DaemonAlreadyRunning(
                    f"daemon-{self.account_id} already running "
                    f"(pid={existing}, lock={self.lock_path})"
                ) from e
            raise

        # Got the lock. NOW it's safe to truncate.
        os.ftruncate(fd, 0)
        import time
        content = f"{os.getpid()} {time.time():.6f}\n".encode()
        os.write(fd, content)
        os.fsync(fd)

        self._fd = fd
        logger.info("daemon lock acquired: pid=%d path=%s", os.getpid(), self.lock_path)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)  # flock released on close
        except OSError:
            pass
        finally:
            self._fd = None

    def _read_existing_pid(self) -> int | None:
        """Read PID from existing lock file (for error message)."""
        if not self.lock_path.exists():
            return None
        try:
            content = self.lock_path.read_text().strip()
            return int(content.split()[0])
        except (ValueError, OSError):
            return None
