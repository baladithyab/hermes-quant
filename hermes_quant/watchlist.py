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

# W4: the canonical multi-horizon rung labels (the horizon_model / W2 contract).
# Kept as a local default so this module COMPOSES with W2's playbook.horizons
# without hard-importing it — the cron integration can pass
# ``known_rungs=horizons.HORIZONS.keys()`` verbatim into the adapter so the two
# stay a single source of truth. 0D membership in a ticker's set is itself
# flag-gated upstream (HERMES_QUANT_ZERO_DTE); the LABEL is always valid here.
_CANONICAL_HORIZON_RUNGS = frozenset({"0D", "1D", "7D", "14D", "30D"})


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    timeframe: str
    # agperc1: opt-in flag — only options_eligible symbols are candidates for the
    # PERCEIVE-layer options origination (IV-rank sourcing -> structure selection).
    # ADD-ONLY, default False so every existing entry round-trips byte-identical and
    # no symbol becomes an options candidate without an explicit watchlist opt-in.
    options_eligible: bool = False
    # W4: the multi-horizon rung set (e.g. ["1D", "7D", "14D", "30D"]) attached to
    # a profile-fit ticker so the decision layer can pick WHICH rung trades per
    # tick. ADD-ONLY, default None -> every existing entry round-trips
    # byte-identical (the operator-watchlist add path never sets it, so the dict
    # carries `horizon_set: null`, exactly the agperc1 options_eligible contract).
    # The watchlist entry NEVER names a strategy — only profile-fit + horizons.
    horizon_set: list[str] | None = None

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
                # agperc1: thread the opt-in through the dict loader so a watchlist.json
                # entry can set it; default False keeps every existing entry byte-identical.
                options_eligible=bool(entry.get("options_eligible", False)),
                # W4: thread the multi-horizon rung set through the loader. Default
                # None (key absent) keeps every existing entry byte-identical; a row
                # that carries horizon_set loads it back verbatim as a list of labels.
                horizon_set=_coerce_horizon_set(entry.get("horizon_set")),
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
# W4 — horizon_set coercion + profile-fit.json materialization adapter
# ---------------------------------------------------------------------------


def _coerce_horizon_set(raw: object) -> list[str] | None:
    """Coerce a loaded ``horizon_set`` value into ``list[str] | None``.

    ADD-ONLY / byte-identical: a missing key (``None``) stays ``None`` so every
    pre-W4 entry round-trips unchanged. A list is materialized as a list of
    string labels. Anything else (a scalar, a malformed value) collapses to
    ``None`` (silence-by-default — a malformed horizon never crashes the loader,
    matching ``list_watchlist``'s defensive dict handling).
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return None


def materialize_profile_fit_entries(
    payload: dict,
    *,
    known_rungs: set[str] | frozenset[str] | None = None,
) -> list[WatchlistEntry]:
    """Materialize a profile-fit watchlist payload into ``WatchlistEntry`` rows.

    The profile-fit watchlist (the single watchlist the W3 ``profile_scan``
    emits to ``~/.hermes/quant/watchlist/profile-fit.json``) carries ``active``
    rows each shaped ``{symbol, asset_class, options_eligible, shortable,
    horizon_set, fit_score, asof}`` — ONE list, no per-play bucketing. This
    adapter is the seam the autonomous tick consumes: it turns those rows into
    config-watchlist ``WatchlistEntry`` objects carrying
    ``symbol / asset_class / timeframe / options_eligible / horizon_set``.

    The materialized entry NEVER names a strategy — it carries only profile-fit
    state and the multi-horizon rung set; the decision layer (``structure_select``
    + the gate) picks WHICH structure and WHICH rung trades per tick.

    Defensive, mirroring ``list_watchlist``:
    - rows missing ``symbol`` or ``asset_class`` are dropped, not crashed on;
    - ``timeframe`` defaults from the asset_class map (the watchlist never
      pre-picks a timeframe-as-strategy here);
    - silence-by-default on an empty / absent ``active`` list -> ``[]``.

    Validation (money-software fail-closed): every label in a row's
    ``horizon_set`` MUST be a known rung. ``known_rungs`` defaults to the
    canonical W2 set (``0D/1D/7D/14D/30D``) but is injectable so the caller can
    pass ``horizons.HORIZONS.keys()`` verbatim and keep a single source of
    truth. An unknown label raises ``ValueError`` so it can never silently flow
    to the decision layer's DTE resolver.
    """
    rungs = frozenset(known_rungs) if known_rungs is not None else _CANONICAL_HORIZON_RUNGS
    rows = payload.get("active") or []
    out: list[WatchlistEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        asset_class = row.get("asset_class")
        if not symbol or not asset_class:
            continue
        asset_class = str(asset_class)
        timeframe = row.get("timeframe") or _DEFAULT_TF_BY_ASSET_CLASS.get(asset_class, "1d")
        horizon_set = _coerce_horizon_set(row.get("horizon_set"))
        if horizon_set is not None:
            unknown = [label for label in horizon_set if label not in rungs]
            if unknown:
                raise ValueError(
                    f"profile-fit row {symbol!r} has unknown horizon rung(s) "
                    f"{unknown!r}; known rungs are {sorted(rungs)}"
                )
        out.append(
            WatchlistEntry(
                symbol=str(symbol),
                asset_class=asset_class,
                timeframe=str(timeframe),
                options_eligible=bool(row.get("options_eligible", False)),
                horizon_set=horizon_set,
            )
        )
    return out


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
