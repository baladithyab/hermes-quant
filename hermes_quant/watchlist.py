"""hermes_quant.watchlist — Config-driven watchlist for autonomous mode.

Per ADR-0016 §D5. Watchlist lives in `~/.hermes/config.yaml::quant.autonomous.watchlist`
as a list of `{symbol, asset_class, timeframe?}` objects. Profile-aware
per ADR-0013 §D4 (live-binance and paper-binance profiles can have
distinct watchlists).

Public API:
- list_watchlist() -> list[WatchlistEntry]
- add_to_watchlist(symbol, asset_class, timeframe=None) -> WatchlistEntry
- remove_from_watchlist(symbol) -> bool
- clear_watchlist()
- get_config_path() -> Path

The functions read/write config.yaml directly (no SQLite or JSONL — the
watchlist is small operator state, not high-frequency).

Concurrent-write safety: a per-config-path lock + atomic-rename
(`.tmp` → `fsync` → `rename`). Same pattern the journal writer uses.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_TF_BY_ASSET_CLASS = {
    "equity": "1d",
    "etf": "1d",
    "crypto": "1h",
    "fx": "1h",
}

_VALID_ASSET_CLASSES = {"crypto", "equity", "etf", "fx"}
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    timeframe: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------


def get_config_path() -> Path:
    """Return the active Hermes config.yaml path. Profile-aware.

    Per ADR-0013 §D4: when HERMES_PROFILE env is set, config lives at
    `~/.hermes/profiles/<name>/config.yaml`; otherwise the global
    `~/.hermes/config.yaml`.
    """
    profile = os.environ.get("HERMES_PROFILE", "")
    if profile:
        return Path.home() / ".hermes" / "profiles" / profile / "config.yaml"
    return Path.home() / ".hermes" / "config.yaml"


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

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
    """Acquire flock on a `.lock` sidecar for cross-process serialization."""
    lock_path = path.with_suffix(path.suffix + ".watchlist.lock")
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
# Public API
# ---------------------------------------------------------------------------


def list_watchlist(path: Path | None = None) -> list[WatchlistEntry]:
    """Return the configured watchlist. Empty list if config missing or
    no watchlist key."""
    cfg_path = path or get_config_path()
    cfg = _load_config(cfg_path)
    raw = (cfg.get("quant") or {}).get("autonomous", {}).get("watchlist") or []
    out: list[WatchlistEntry] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        asset_class = entry.get("asset_class")
        if not symbol or not asset_class:
            continue
        timeframe = entry.get("timeframe") or _DEFAULT_TF_BY_ASSET_CLASS.get(asset_class, "1d")
        out.append(
            WatchlistEntry(
                symbol=str(symbol),
                asset_class=str(asset_class),
                timeframe=str(timeframe),
            )
        )
    return out


def add_to_watchlist(
    symbol: str,
    asset_class: str,
    timeframe: str | None = None,
    *,
    path: Path | None = None,
) -> WatchlistEntry:
    """Add or update an entry in the watchlist. Idempotent on (symbol,
    asset_class) — duplicates with the same key are replaced."""
    if asset_class not in _VALID_ASSET_CLASSES:
        raise ValueError(
            f"asset_class must be one of {sorted(_VALID_ASSET_CLASSES)}, got {asset_class!r}"
        )
    tf = timeframe or _DEFAULT_TF_BY_ASSET_CLASS.get(asset_class, "1d")
    if tf not in _VALID_TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}, got {tf!r}")

    cfg_path = path or get_config_path()
    new_entry = WatchlistEntry(
        symbol=str(symbol).strip(),
        asset_class=asset_class,
        timeframe=tf,
    )
    if not new_entry.symbol:
        raise ValueError("symbol must be non-empty")

    with _get_path_lock(cfg_path):
        with _flocked(cfg_path):
            cfg = _load_config(cfg_path)
            quant = cfg.setdefault("quant", {})
            autonomous = quant.setdefault("autonomous", {})
            existing = autonomous.get("watchlist") or []

            # Remove any existing entry with the same (symbol, asset_class)
            filtered = [
                e
                for e in existing
                if not (
                    isinstance(e, dict)
                    and e.get("symbol") == new_entry.symbol
                    and e.get("asset_class") == new_entry.asset_class
                )
            ]
            filtered.append(new_entry.to_dict())
            autonomous["watchlist"] = filtered
            _save_config(cfg_path, cfg)
            return new_entry


def remove_from_watchlist(
    symbol: str,
    *,
    asset_class: str | None = None,
    path: Path | None = None,
) -> bool:
    """Remove entries matching `symbol` (and optionally `asset_class`).

    Returns True if at least one entry was removed.
    """
    cfg_path = path or get_config_path()
    with _get_path_lock(cfg_path):
        with _flocked(cfg_path):
            cfg = _load_config(cfg_path)
            quant = cfg.get("quant", {})
            autonomous = quant.get("autonomous", {}) if isinstance(quant, dict) else {}
            existing = autonomous.get("watchlist") or []

            def _matches(e):
                if not isinstance(e, dict):
                    return False
                if e.get("symbol") != symbol:
                    return False
                if asset_class is not None and e.get("asset_class") != asset_class:
                    return False
                return True

            filtered = [e for e in existing if not _matches(e)]
            if len(filtered) == len(existing):
                return False
            autonomous["watchlist"] = filtered
            cfg.setdefault("quant", {})["autonomous"] = autonomous
            _save_config(cfg_path, cfg)
            return True


def clear_watchlist(path: Path | None = None) -> int:
    """Remove all watchlist entries. Returns the count removed."""
    cfg_path = path or get_config_path()
    with _get_path_lock(cfg_path):
        with _flocked(cfg_path):
            cfg = _load_config(cfg_path)
            quant = cfg.get("quant", {})
            autonomous = quant.get("autonomous", {}) if isinstance(quant, dict) else {}
            existing = autonomous.get("watchlist") or []
            n = len(existing)
            autonomous["watchlist"] = []
            cfg.setdefault("quant", {})["autonomous"] = autonomous
            _save_config(cfg_path, cfg)
            return n


# ---------------------------------------------------------------------------
# Internal — YAML load/save with atomic-rename
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml

        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        return yaml.safe_load(text) or {}
    except ImportError:
        logger.warning("watchlist: pyyaml not installed; cannot read config — returning empty")
        return {}
    except Exception as exc:
        logger.warning(
            "watchlist: failed to parse %s: %s — treating as empty",
            path,
            exc,
        )
        return {}


def _save_config(path: Path, cfg: dict) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "pyyaml is required to write watchlist config; "
            "install hermes-quant[yfinance] (which pulls pyyaml)"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)

    # Atomic rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # ar87 (atomic-write-durability family): fsync the PARENT DIR so the rename
    # itself survives a crash. The watchlist config is the persisted tradeable
    # universe (admit/evict from evolve_watchlist); a lost rename reverts an
    # admit/evict on reboot. fsyncing only the file fd flushes DATA, not the
    # directory entry the rename creates. Best-effort: warn, never mask the write.
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:  # pragma: no cover - platform/fs dependent
        logger.warning(
            "watchlist: parent-dir fsync failed for %s; the config rename may not "
            "survive a crash: %s",
            path.parent,
            e,
        )
